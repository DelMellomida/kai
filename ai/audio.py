"""Audio primitives for hands-free listening: resampling, framing, pre-roll, capture, wake, VAD.

Deliberately pure and dependency-light — no FSM, no Flask, no HTTP, no sounddevice, and no scipy.
Everything here is a small stateful object that takes samples in and gives samples or decisions out,
with `now` passed in rather than read from the clock. That's what makes the frame arithmetic (where
every bug in this feature will live) testable off the Jetson, on a machine with none of the audio
stack installed. ai/session.py owns the device and wires these together.

pvporcupine and webrtcvad are imported behind flags, mirroring face_track.py's flask handling: on a
dev box neither is installed, and importing this module must still work.
"""

from __future__ import annotations

import importlib.util
import os
from collections import deque, namedtuple
from pathlib import Path

import numpy as np

from config.voice import (
    ASR_NORMALIZE_MAX_GAIN, ASR_NORMALIZE_MIN_RMS, ASR_NORMALIZE_PEAK_CEILING,
    ASR_NORMALIZE_TARGET_RMS, SAMPLE_RATE,
)
from config.wake import (
    CAPTURE_HARD_CAP_S, MIC_HIGHPASS_HZ, MIC_HIGHPASS_TAPS, MIC_RESAMPLE_CUTOFF, MIC_RESAMPLE_TAPS,
    VAD_AGGRESSIVENESS, VAD_DC_BLOCK, VAD_FRAME_MS, VAD_HANGOVER_S, VAD_ONSET_FRAMES,
    VAD_RMS_FLOOR, VAD_RMS_FLOOR_HOLD, WAKE_ACCESS_KEY_ENV, WAKE_ACCESS_KEY_FILE,
    WAKE_AMBIENT_ADAPT, WAKE_AMBIENT_HOLD_MULT, WAKE_AMBIENT_MAX_LIFT, WAKE_AMBIENT_OPEN_MULT,
    WAKE_AMBIENT_SMOOTH, WAKE_AMBIENT_WINDOW_S,
    WAKE_CPU_PART_OVERRIDE,
    WAKE_ENGINE_FORCE, WAKE_ENGINE_ORDER, WAKE_FRAME_LENGTH, WAKE_KEYWORD_PATHS, WAKE_MODEL_PATH,
    WAKE_OWW_EMBEDDING_PATH, WAKE_OWW_FRAME_LENGTH, WAKE_OWW_FRAMEWORK, WAKE_OWW_MELSPEC_PATH,
    WAKE_OWW_MODEL_PATH, WAKE_OWW_NOISE_SUPPRESS, WAKE_OWW_THRESHOLD, WAKE_OWW_VAD_THRESHOLD,
    WAKE_SENSITIVITIES, WAKE_WHISPER_ENABLED, WAKE_WHISPER_MAX_UTTERANCE_S,
    WAKE_WHISPER_MIN_UTTERANCE_S,
)

def _import_pvporcupine() -> tuple[object | None, str | None, bool]:
    """Import pvporcupine, working around its CPU-detection table. Returns (module, error, patched).

    pvporcupine picks which bundled .so to load by matching `/proc/cpuinfo`'s "CPU part" against a
    short hardcoded list, and **raises at import time** on anything it doesn't recognise. The Jetson
    Orin's Cortex-A78AE reports `0xd42`, which is absent from every published version (checked 2.1
    through 4.0) — even though the `cortex-a76-aarch64` build it ships runs on it correctly, since
    A76 and A78 share the same ARMv8.2-A baseline.

    So: try the honest import first. Only if it fails *specifically* on CPU detection do we retry
    with WAKE_CPU_PART_OVERRIDE substituted, by intercepting the single `cat /proc/cpuinfo` call it
    makes. The patch is reversed immediately and nothing else in the process ever sees it.
    """
    try:
        import pvporcupine
        return pvporcupine, None, False
    except Exception as exc:
        first_error = f"{type(exc).__name__}: {exc}"
        if not WAKE_CPU_PART_OVERRIDE or "Unsupported CPU" not in str(exc):
            return None, first_error, False

    import subprocess
    import sys

    real_check_output = subprocess.check_output

    def _patched(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and "/proc/cpuinfo" in [str(c) for c in cmd]:
            return f"CPU part\t: {WAKE_CPU_PART_OVERRIDE}\n".encode()
        return real_check_output(cmd, *args, **kwargs)

    # The failed attempt leaves half-initialised modules behind; they must go or the retry is a no-op.
    for name in [n for n in sys.modules if n == "pvporcupine" or n.startswith("pvporcupine.")]:
        del sys.modules[name]

    subprocess.check_output = _patched
    try:
        import pvporcupine
        return pvporcupine, None, True
    except Exception as exc:
        return None, f"{first_error} (override also failed: {type(exc).__name__}: {exc})", False
    finally:
        subprocess.check_output = real_check_output


# Catch every exception, not just ImportError: the CPU-table failure above is a NotImplementedError,
# and letting that escape would take the whole robot down at startup. An optional wake word must
# never be able to do that.
pvporcupine, _WAKE_IMPORT_ERROR, _WAKE_CPU_PATCHED = _import_pvporcupine()
_WAKE_OK = pvporcupine is not None
if _WAKE_CPU_PATCHED:
    print(f"[wake] this CPU is missing from pvporcupine's table — using the "
          f"{WAKE_CPU_PART_OVERRIDE} build instead (see config/wake.py)", flush=True)

_VAD_IMPORT_ERROR: str | None = None
try:
    import webrtcvad
    _VAD_OK = True
except Exception as _exc:
    _VAD_OK = False
    _VAD_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

# Same broad catch, same reason: some openWakeWord versions probe for tflite_runtime at import (there
# is no aarch64/py3.10 wheel), and that raise must not escape into face_track's startup.
_OWW_IMPORT_ERROR: str | None = None
try:
    import openwakeword
    from openwakeword.model import Model as _OWWModel
    _OWW_OK = True
except Exception as _exc:
    _OWW_OK, _OWWModel, openwakeword = False, None, None
    _OWW_IMPORT_ERROR = f"openwakeword unusable ({type(_exc).__name__}: {_exc})"

# Project root (…/kai) — keyword paths are stored relative to it so they resolve the same under
# scripts/run.sh and the @reboot autostart regardless of cwd (same convention as ai/tts.py).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_INT16_MIN, _INT16_MAX = -32768, 32767


def rms(samples: np.ndarray) -> float:
    """Root-mean-square level of an int16 buffer, as a float in int16 units. 0.0 when empty."""
    if samples.size == 0:
        return 0.0
    x = samples.astype(np.float64)
    return float(np.sqrt(np.mean(x * x)))


def _to_int16(x: np.ndarray) -> np.ndarray:
    """Round and clip a float buffer back to int16, saturating rather than wrapping."""
    return np.clip(np.rint(x), _INT16_MIN, _INT16_MAX).astype(np.int16)


Normalized = namedtuple("Normalized", "samples gain rms")


def normalize_for_asr(samples: np.ndarray,
                      target_rms: float = ASR_NORMALIZE_TARGET_RMS,
                      max_gain: float = ASR_NORMALIZE_MAX_GAIN,
                      peak_ceiling: float = ASR_NORMALIZE_PEAK_CEILING,
                      min_rms: float = ASR_NORMALIZE_MIN_RMS) -> Normalized:
    """Lift a quiet utterance toward `target_rms` before it reaches Whisper.

    Takes and returns float samples in -1..1 (what faster-whisper wants), NOT the int16 the rest of
    this module deals in — it sits at the very end of the chain, after the resample in
    ai/voice_assistant._transcribe, and is the last thing to touch the audio.

    Returns the measured input level alongside the applied gain because both are published for
    tuning: the input RMS is how far away the speaker effectively was, the gain is how much of that
    was recovered, and a gain pinned at `max_gain` means the answer is "not enough".

    Three guards, each covering a way this could make things worse rather than better:

      * `min_rms` — below it the buffer is left alone. Amplifying near-silence produces loud noise,
        and loud noise is the raw material Whisper hallucinates filler out of. Doing nothing leaves
        an obviously-empty clip that the existing rejection gates already discard cleanly.
      * gain <= 1.0 is a no-op, never an attenuation. Close-mic audio already decodes well; there is
        no evidence turning it down helps, so the close case is left bit-identical rather than
        perturbed on a hunch.
      * `peak_ceiling` — the lift is re-derived from the actual peak if the RMS-based one would
        clip. A quiet utterance with one transient in it (a door, a knock on the table) has a low
        RMS and a high peak; without this the transient would be squared off into broadband noise
        sitting right on top of the speech.
    """
    if samples.size == 0:
        return Normalized(samples, 1.0, 0.0)
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    level = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    if level < min_rms or level <= 0.0:
        return Normalized(x, 1.0, level)
    gain = min(target_rms / level, max_gain)
    peak = float(np.max(np.abs(x)))
    if peak > 0.0 and peak * gain > peak_ceiling:
        gain = peak_ceiling / peak
    if gain <= 1.0:
        return Normalized(x, 1.0, level)
    return Normalized((x * gain).astype(np.float32), gain, level)


def design_lowpass(numtaps: int, cutoff_hz: float, fs: int) -> np.ndarray:
    """Windowed-sinc FIR low-pass, normalized to unity DC gain.

    Hand-rolled rather than scipy.signal.firwin so the always-on audio thread pulls in nothing but
    numpy — and so these tests run on a dev box without scipy. An odd tap count keeps the response
    symmetric (linear phase, integer group delay)."""
    if numtaps % 2 == 0:
        numtaps += 1                       # force symmetry
    n = np.arange(numtaps) - (numtaps - 1) / 2.0
    fc = cutoff_hz / fs                    # normalized cutoff, cycles/sample
    h = 2.0 * fc * np.sinc(2.0 * fc * n)   # ideal brick-wall impulse response…
    h *= np.hamming(numtaps)               # …windowed, to bound the stopband ripple
    return h / h.sum()


class Decimator:
    """Integer-ratio downsampler that is exact across block boundaries.

    Uses overlap-save: each call prepends the previous block's tail before convolving, so the output
    is bit-identical to filtering one infinite stream. This matters more than it looks — calling a
    stateless resampler (scipy's resample_poly) per block zero-pads every block's edges
    independently, putting a filter discontinuity into the signal every 32 ms. Porcupine's accuracy
    drops measurably and no unit test that only checks output length would notice.

    The decimation phase is carried too, so the picked samples stay on one grid even when block
    lengths aren't multiples of the ratio.
    """

    def __init__(self, in_rate: int, out_rate: int = SAMPLE_RATE,
                 taps: int = MIC_RESAMPLE_TAPS, cutoff_hz: float = MIC_RESAMPLE_CUTOFF,
                 gain: float = 1.0) -> None:
        if in_rate % out_rate != 0:
            raise ValueError(f"decimation needs an integer ratio, got {in_rate} -> {out_rate}")
        self.ratio = in_rate // out_rate
        self.in_rate, self.out_rate = in_rate, out_rate
        # Never let the anti-alias cutoff exceed the output Nyquist, whatever the config says.
        self._h = design_lowpass(taps, min(cutoff_hz, 0.95 * out_rate / 2), in_rate) * gain
        self._overlap = len(self._h) - 1
        self.reset()

    def reset(self) -> None:
        """Drop all filter history. Called when the mic un-mutes, so residue from before the mute
        can't leak into the first frames back."""
        self._tail = np.zeros(self._overlap, dtype=np.float64)
        self._phase = 0

    def feed(self, block: np.ndarray) -> np.ndarray:
        """Filter+decimate one block of int16 samples at in_rate. Returns int16 at out_rate —
        length varies by ±1 around len(block)/ratio as the phase walks."""
        if block.size == 0:
            return np.zeros(0, dtype=np.int16)
        x = np.concatenate((self._tail, block.astype(np.float64).ravel()))
        y = np.convolve(x, self._h, mode="valid")     # exactly len(block) samples
        self._tail = x[len(x) - self._overlap:]
        out = y[self._phase::self.ratio]
        self._phase = (self._phase - len(y)) % self.ratio
        return _to_int16(out)


def design_highpass(numtaps: int, cutoff_hz: float, fs: int) -> np.ndarray:
    """Windowed-sinc FIR high-pass, by spectral inversion of the low-pass above.

    Negating a unity-DC-gain low-pass and adding 1 at the centre tap gives unity gain in the
    passband and exactly ZERO at DC — which is the whole point here, since the DC offset is the
    largest single thing being removed. Odd tap count keeps it symmetric (linear phase).
    """
    if numtaps % 2 == 0:
        numtaps += 1
    h = -design_lowpass(numtaps, cutoff_hz, fs)
    h[(numtaps - 1) // 2] += 1.0
    return h


class HighPass:
    """Stateful FIR high-pass over a block stream. Removes the mic's DC offset and low rumble.

    Overlap-save with a carried tail, for the same reason Decimator does it: filtering each block
    independently zero-pads its edges and injects a discontinuity every 32 ms. A step at the block
    boundary is broadband, so it would land squarely in the speech band — the exact opposite of what
    this filter is for.

    A one-pole IIR blocker would be cheaper, but it cannot be vectorised in numpy without either a
    per-sample Python loop on the always-on audio thread or an a**n rescaling trick that loses
    precision over a 512-sample block. An FIR is a plain convolve, reuses design_lowpass, and is
    linear phase.
    """

    def __init__(self, cutoff_hz: float = MIC_HIGHPASS_HZ, rate: int = SAMPLE_RATE,
                 taps: int = MIC_HIGHPASS_TAPS) -> None:
        self.cutoff_hz = float(cutoff_hz)
        self._h = design_highpass(taps, self.cutoff_hz, rate)
        self._overlap = len(self._h) - 1
        self.reset()

    def reset(self) -> None:
        """Drop filter history — called on un-mute, so pre-mute residue can't ring into the first
        frames back and read as an onset."""
        self._tail = np.zeros(self._overlap, dtype=np.float64)

    def feed(self, block: np.ndarray) -> np.ndarray:
        """int16 in, int16 out, same length as the input."""
        if block.size == 0:
            return np.zeros(0, dtype=np.int16)
        x = np.concatenate((self._tail, block.astype(np.float64).ravel()))
        y = np.convolve(x, self._h, mode="valid")     # exactly len(block) samples
        self._tail = x[len(x) - self._overlap:]
        return _to_int16(y)


class FrameAssembler:
    """Re-chunks a variable-length sample stream into fixed-size frames.

    Porcupine wants exactly 512 samples per call and webrtcvad exactly 320 (20 ms @ 16 kHz); with
    CAPTURE_BLOCKSIZE chosen as a multiple of 512 the Porcupine case needs no buffering at all, but
    the VAD case always does, and neither should depend on that arithmetic holding."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._buf = np.zeros(0, dtype=np.int16)

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.int16)

    @property
    def pending(self) -> int:
        """Samples held back waiting for a full frame."""
        return int(self._buf.size)

    def push(self, block: np.ndarray) -> list[np.ndarray]:
        """Add samples, return every complete frame now available (possibly none, possibly many)."""
        if block.size:
            self._buf = np.concatenate((self._buf, block.ravel()))
        n = self._buf.size // self.size
        if n == 0:
            return []
        frames = [self._buf[i * self.size:(i + 1) * self.size] for i in range(n)]
        self._buf = self._buf[n * self.size:]
        return frames


class RingPreroll:
    """A few hundred ms of rolling recent audio, kept so an utterance isn't clipped at the front.

    The VAD only declares onset after VAD_ONSET_FRAMES of confirmed speech, and a talker's first
    syllable arrives before that — without this, Whisper receives "…ey Kai, what time is it" with the
    beginning of the actual question missing."""

    def __init__(self, seconds: float, rate: int) -> None:
        self.capacity = max(0, int(seconds * rate))
        self._chunks: deque[np.ndarray] = deque()
        self._n = 0

    def reset(self) -> None:
        self._chunks.clear()
        self._n = 0

    @property
    def samples(self) -> int:
        return self._n

    def push(self, block: np.ndarray) -> None:
        if self.capacity == 0 or block.size == 0:
            return
        self._chunks.append(block.ravel())
        self._n += block.size
        while self._n - len(self._chunks[0]) >= self.capacity:
            self._n -= len(self._chunks.popleft())

    def take(self) -> np.ndarray:
        """Drain the ring and return its contents, trimmed to capacity, oldest first."""
        if not self._chunks:
            return np.zeros(0, dtype=np.int16)
        out = np.concatenate(list(self._chunks))
        self.reset()
        return out[-self.capacity:] if out.size > self.capacity else out


class CaptureBuffer:
    """The utterance being recorded, with a hard ceiling.

    The cap is enforced here rather than in the session FSM on purpose: the mic is now open all the
    time, so an append that isn't bounded independently of the FSM turns any wedged state into an
    out-of-memory on an 8 GB board. Past the cap the oldest audio is dropped and `truncated` is set,
    which the session reports rather than swallowing."""

    def __init__(self, rate: int, cap_s: float = CAPTURE_HARD_CAP_S) -> None:
        self.rate = rate
        self.cap = max(1, int(cap_s * rate))
        self._chunks: deque[np.ndarray] = deque()
        self._n = 0
        self.truncated = False

    def reset(self) -> None:
        self._chunks.clear()
        self._n = 0
        self.truncated = False

    @property
    def samples(self) -> int:
        return self._n

    @property
    def seconds(self) -> float:
        return self._n / self.rate if self.rate else 0.0

    def push(self, block: np.ndarray) -> None:
        if block.size == 0:
            return
        self._chunks.append(block.ravel())
        self._n += block.size
        while self._n > self.cap and self._chunks:
            self._n -= len(self._chunks.popleft())
            self.truncated = True

    def prepend(self, block: np.ndarray) -> None:
        """Put `block` in front of what's already buffered — used for the pre-roll at onset."""
        if block.size == 0:
            return
        self._chunks.appendleft(block.ravel())
        self._n += block.size
        while self._n > self.cap and self._chunks:
            self._n -= len(self._chunks.pop())   # trim from the NEW end, keep the speech onset
            self.truncated = True

    def take(self) -> np.ndarray:
        """Drain and return the utterance as one mono int16 array."""
        if not self._chunks:
            self.reset()
            return np.zeros(0, dtype=np.int16)
        out = np.concatenate(list(self._chunks))
        self.reset()
        return out


def resolve_access_key() -> str | None:
    """Find the Porcupine access key: environment first, then the key file. None if neither.

    Never read from config/ — that's committed. The FILE is the durable source because
    scripts/autostart.sh runs under @reboot cron with no login shell, so an exported variable would
    have to be written into a tracked file to survive a reboot."""
    key = (os.environ.get(WAKE_ACCESS_KEY_ENV) or "").strip()
    if key:
        return key
    try:
        key = Path(WAKE_ACCESS_KEY_FILE).expanduser().read_text(encoding="utf-8").strip()
    except (OSError, RuntimeError):
        # RuntimeError: expanduser() raises when HOME is unset — which is precisely the @reboot cron
        # environment this file is meant to serve, so it must degrade rather than crash startup.
        return None
    return key or None


def _resolve_path(p) -> Path:
    """Resolve a configured path against the project root when it's relative, so it survives any cwd
    (the autostart runs from cron). Same convention as ai/tts.py's voice_model_path()."""
    path = Path(p)
    return path if path.is_absolute() else _PROJECT_ROOT / path


def keyword_paths() -> list[Path]:
    """Configured .ppn paths resolved against the project root."""
    return [_resolve_path(p) for p in WAKE_KEYWORD_PATHS]


class WakeEngine:
    """One wake-word tier. Degrades to permanently-off rather than raising.

    Every reason a tier can't run — package missing, no key, model absent, wrong platform — is a
    reason to log once and let the chain try the next tier, never to take the robot down.
    `unavailable` carries that reason so /params can explain it.

    Two KINDS of engine, distinguished explicitly rather than by duck-typing:
      "frame"     — process(frame) -> bool, driven by MicStream on the audio thread.
      "utterance" — cannot decide until a whole utterance exists; driven by the session FSM, and
                    process() never fires.
    """

    name = "?"
    kind = "frame"

    def __init__(self) -> None:
        self.unavailable: str | None = None
        self.frame_length = WAKE_FRAME_LENGTH
        self.sample_rate = SAMPLE_RATE
        self.last_score = 0.0
        # Normalised 0..1 SENSITIVITY: higher always means more detections and more false accepts,
        # whatever the underlying tier calls it. Porcupine's own knob already works that way;
        # openWakeWord's is a threshold and runs the other way, so it inverts internally. Presenting
        # one meaning is the whole point — a slider that reversed direction depending on which tier
        # happened to win would be unusable.
        self.sensitivity = WAKE_SENSITIVITIES[0] if WAKE_SENSITIVITIES else 0.5

    def set_sensitivity(self, value: float) -> bool:
        """Retune sensitivity. Returns True if the engine must be reopened for it to take effect.

        Tiers that compare per frame can apply this for free; tiers that bake it into a native handle
        at create() time cannot, and say so rather than silently ignoring the change.
        """
        self.sensitivity = max(0.0, min(1.0, float(value)))
        return False

    def open(self) -> bool:
        raise NotImplementedError

    @property
    def ready(self) -> bool:
        raise NotImplementedError

    def process(self, frame: np.ndarray) -> bool:
        return False

    def reset(self) -> None:
        """Drop any internal audio/feature history. Called when the mic un-mutes."""

    def close(self) -> None:
        """Release native resources. Safe to call twice, and on a tier that never opened."""

    def _fail(self, reason: str) -> bool:
        self.unavailable = reason
        return False


class PorcupineEngine(WakeEngine):
    """Picovoice Porcupine — the best tier when it can run: instant, per-32 ms, very low CPU.

    Also the most demanding to set up, which is why the chain exists: it needs a cloud access key, a
    `.ppn` compiled for this exact platform, and a CPU that appears in pvporcupine's hardcoded table.
    """

    name = "porcupine"
    kind = "frame"

    def __init__(self) -> None:
        super().__init__()
        self._handle = None

    def set_sensitivity(self, value: float) -> bool:
        """Porcupine bakes sensitivities into the native handle at create() time, so a live change
        needs a reopen. Reported honestly rather than stored and quietly ignored."""
        super().set_sensitivity(value)
        return self._handle is not None

    def open(self) -> bool:
        """Create the Porcupine handle. True if the detector is live. Idempotent."""
        if self._handle is not None:
            return True
        if not _WAKE_OK:
            if _WAKE_IMPORT_ERROR and "Unsupported CPU" in _WAKE_IMPORT_ERROR:
                return self._fail(
                    f"{_WAKE_IMPORT_ERROR} — this CPU is missing from pvporcupine's table; "
                    f"set WAKE_CPU_PART_OVERRIDE in config/wake.py")
            if _WAKE_IMPORT_ERROR:
                return self._fail(f"pvporcupine unusable ({_WAKE_IMPORT_ERROR})")
            return self._fail("pvporcupine not installed — run: pip3 install pvporcupine")
        key = resolve_access_key()
        if not key:
            return self._fail(f"no access key (set ${WAKE_ACCESS_KEY_ENV} or {WAKE_ACCESS_KEY_FILE})")
        paths = keyword_paths()
        missing = [str(p) for p in paths if not p.is_file()]
        if missing:
            return self._fail(f"keyword file(s) not found: {', '.join(missing)}")
        kwargs = {
            "access_key": key,
            "keyword_paths": [str(p) for p in paths],
            # self.sensitivity, not the config constant: a live change has to survive the reopen that
            # applies it. Porcupine's scale already matches ours (higher = more detections).
            "sensitivities": [self.sensitivity] * len(paths),
        }
        if WAKE_MODEL_PATH:
            kwargs["model_path"] = str(WAKE_MODEL_PATH)
        try:
            handle = pvporcupine.create(**kwargs)
        except Exception as exc:   # pvporcupine raises its own exception family, plus OSError
            # By far the most likely cause: a .ppn generated for Windows/macOS instead of aarch64.
            return self._fail(f"{type(exc).__name__}: {exc} "
                              f"(is the .ppn built for this platform?)")
        self._handle = handle
        self.frame_length = handle.frame_length
        self.sample_rate = handle.sample_rate
        if handle.sample_rate != SAMPLE_RATE:
            self.close()
            return self._fail(f"expects {handle.sample_rate} Hz but the pipeline produces {SAMPLE_RATE}")
        print(f"[wake] porcupine: listening for 'Hey Kai' "
              f"({handle.frame_length} samples @ {handle.sample_rate} Hz)", flush=True)
        return True

    @property
    def ready(self) -> bool:
        return self._handle is not None

    def process(self, frame: np.ndarray) -> bool:
        """True when the wake word is detected in this exactly-frame_length int16 frame."""
        if self._handle is None:
            return False
        try:
            return self._handle.process(frame) >= 0
        except Exception as exc:
            self._fail(f"process failed ({type(exc).__name__}: {exc})")
            self.close()
            return False

    def close(self) -> None:
        """Release Porcupine's native memory. Must be called on shutdown — leaking it across
        restarts is a real leak, not just untidy."""
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.delete()
            except Exception:
                pass


class OpenWakeWordEngine(WakeEngine):
    """openWakeWord — the backup that needs no account and no per-platform binary.

    Pure onnxruntime (already installed here for Piper), a ~1 MB custom-trained model, and no CPU
    allow-list to fall foul of. Costs a little more CPU than Porcupine and works on 80 ms frames
    rather than 32 ms, so wake latency quantizes coarser.
    """

    name = "openwakeword"
    kind = "frame"

    def __init__(self) -> None:
        super().__init__()
        self._model = None
        self._key: str | None = None
        self.frame_length = WAKE_OWW_FRAME_LENGTH

    def _front_end_paths(self) -> tuple[Path, Path] | None:
        """Where the shared melspectrogram + embedding models should be, or None if unknowable."""
        if WAKE_OWW_MELSPEC_PATH and WAKE_OWW_EMBEDDING_PATH:
            return _resolve_path(WAKE_OWW_MELSPEC_PATH), _resolve_path(WAKE_OWW_EMBEDDING_PATH)
        try:
            base = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
        except Exception:
            return None
        return base / "melspectrogram.onnx", base / "embedding_model.onnx"

    def open(self) -> bool:
        if self._model is not None:
            return True
        if not _OWW_OK:
            return self._fail(_OWW_IMPORT_ERROR or
                              "openwakeword not installed — pip3 install --no-deps openwakeword "
                              "(see wake/README.md)")
        model_path = _resolve_path(WAKE_OWW_MODEL_PATH)
        if not model_path.is_file():
            return self._fail(f"custom model not found: {model_path} — train it (wake/README.md)")

        # Check the shared front-end exists BEFORE constructing anything. openWakeWord downloads
        # these on first use, and a startup that reaches for the network is a startup that hangs —
        # "construct and catch" would block for the HTTP timeout on an offline boot. Check-first
        # ordering is load-bearing, not tidiness.
        front = self._front_end_paths()
        if front is not None:
            missing = [str(p) for p in front if not p.is_file()]
            if missing:
                return self._fail(
                    f"openwakeword's shared front-end is not downloaded ({', '.join(missing)}) — run: "
                    f"python3 -c 'import openwakeword.utils as u; u.download_models()' "
                    f"(needs network once; see wake/README.md)")

        kwargs = {"wakeword_models": [str(model_path)],
                  "inference_framework": WAKE_OWW_FRAMEWORK}
        extra = {"enable_speex_noise_suppression": WAKE_OWW_NOISE_SUPPRESS,
                 "vad_threshold": WAKE_OWW_VAD_THRESHOLD}
        if WAKE_OWW_MELSPEC_PATH:
            extra["melspec_model_path"] = str(_resolve_path(WAKE_OWW_MELSPEC_PATH))
        if WAKE_OWW_EMBEDDING_PATH:
            extra["embedding_model_path"] = str(_resolve_path(WAKE_OWW_EMBEDDING_PATH))
        try:
            try:
                model = _OWWModel(**kwargs, **extra)
            except TypeError as exc:
                # Model.__init__'s kwargs have drifted across openWakeWord releases. Retry with the
                # minimal signature rather than failing for a reason that reads like a missing model.
                print(f"[wake] openwakeword: dropping unsupported options ({exc})", flush=True)
                model = _OWWModel(**kwargs)
            # Prove it works before declaring the tier live. Model() constructing and predict()
            # throwing on a mis-shaped .onnx is the realistic failure, and accepting a tier that
            # merely constructs would leave Kai deaf while reporting sess_wake_ok=True.
            scores = model.predict(np.zeros(self.frame_length, dtype=np.int16))
            keys = list(scores) if isinstance(scores, dict) else []
        except Exception as exc:
            return self._fail(f"{type(exc).__name__}: {exc} (is {model_path.name} a valid "
                              f"openWakeWord model?)")

        self._model = model
        self._key = keys[0] if len(keys) == 1 else (model_path.stem if model_path.stem in keys else None)
        print(f"[wake] openwakeword: listening for {self._key or model_path.stem!r} "
              f"({self.frame_length} samples @ {SAMPLE_RATE} Hz, "
              f"threshold {WAKE_OWW_THRESHOLD})", flush=True)
        return True

    @property
    def ready(self) -> bool:
        return self._model is not None

    def process(self, frame: np.ndarray) -> bool:
        if self._model is None:
            return False
        try:
            scores = self._model.predict(frame)
        except Exception as exc:
            self._fail(f"predict failed ({type(exc).__name__}: {exc})")
            self.close()
            return False
        if self._key is not None:
            score = float(scores.get(self._key, 0.0))
        else:
            score = float(max(scores.values(), default=0.0))
        # Published as sess_wake_score — this is how the threshold actually gets tuned.
        self.last_score = score
        # No debounce here: the score stays high for several frames after a hit, and the session's
        # WAKE_REFRACTORY_S already collapses that into one wake.
        return score >= self.threshold

    @property
    def threshold(self) -> float:
        """WAKE_OWW_THRESHOLD, shifted by however far sensitivity has moved off its default.

        This tier's native knob is a THRESHOLD (higher = fewer detections) while ours is a SENSITIVITY
        (higher = more), so the offset is subtracted. Expressed as an offset rather than 1-sensitivity
        so that WAKE_OWW_THRESHOLD in config/wake.py still sets the resting point — otherwise tuning it
        there would silently stop having any effect.
        """
        default = WAKE_SENSITIVITIES[0] if WAKE_SENSITIVITIES else 0.5
        return max(0.0, min(1.0, WAKE_OWW_THRESHOLD + (default - self.sensitivity)))

    def reset(self) -> None:
        # openWakeWord keeps an internal audio/feature buffer; residue from before a mute is exactly
        # what trips a false wake on the first frames back.
        if self._model is not None:
            try:
                self._model.reset()
            except Exception:
                pass

    def close(self) -> None:
        self._model, self._key = None, None


class WhisperWakeEngine(WakeEngine):
    """Last resort: spot the phrase in a Whisper transcript. No key, no model, no download.

    Fundamentally different from the frame tiers — it cannot decide until a complete utterance
    exists, so the session FSM drives it (capture in idle, transcribe, match) and process() never
    fires. In exchange it needs zero setup and matches the real phrase immediately, and because the
    transcript already contains whatever followed the wake words, it can answer in one breath.

    Deliberately does NOT touch the model: the session's `ready` already gates on the assistant's
    pre-warmed instance, and loading a second small/int8 copy into ~2 GB of headroom would be a real
    regression.
    """

    name = "whisper"
    kind = "utterance"

    def __init__(self) -> None:
        super().__init__()
        self._open = False

    def open(self) -> bool:
        if self._open:
            return True
        if not WAKE_WHISPER_ENABLED:
            return self._fail("whisper phrase spotting is disabled (WAKE_WHISPER_ENABLED)")
        if importlib.util.find_spec("faster_whisper") is None:
            return self._fail("faster-whisper not installed")
        self._open = True
        print(f"[wake] whisper: phrase spotting active — utterances between "
              f"{WAKE_WHISPER_MIN_UTTERANCE_S}s and {WAKE_WHISPER_MAX_UTTERANCE_S}s are "
              f"transcribed and matched against the wake phrase", flush=True)
        return True

    @property
    def ready(self) -> bool:
        return self._open

    def close(self) -> None:
        self._open = False


class WakeDetector:
    """The fallback chain. Keeps this name so MicStream and the session need no rewiring.

    Tries each tier in WAKE_ENGINE_ORDER at open() and keeps the FIRST that initializes; the rest are
    never constructed. Exposes exactly the interface its callers already consume, plus `engine`,
    `kind`, `frame_ready` and `tiers` for the log and /params.

    A tier failing is normal and expected — that is the entire point. `unavailable` becomes a single
    string naming every reason only when ALL tiers fail, which is what the dashboard's push-to-talk
    tooltip should say.
    """

    _FACTORIES = {
        "porcupine": PorcupineEngine,
        "openwakeword": OpenWakeWordEngine,
        "whisper": WhisperWakeEngine,
    }

    def __init__(self, order=WAKE_ENGINE_ORDER, force=WAKE_ENGINE_FORCE) -> None:
        self._order = (force,) if force else tuple(order)
        self._engine: WakeEngine | None = None
        self.tiers: dict[str, str] = {}
        self.unavailable: str | None = None
        self.frame_length = WAKE_FRAME_LENGTH
        self.sample_rate = SAMPLE_RATE
        # Held HERE, not just on the engine: open() constructs a fresh engine from the factory, which
        # would otherwise come up at the config default and silently discard a live change — including
        # on the very reopen performed to apply that change.
        self.sensitivity = WAKE_SENSITIVITIES[0] if WAKE_SENSITIVITIES else 0.5

    def open(self) -> bool:
        if self._engine is not None:
            return True
        self.tiers = {}
        for name in self._order:
            factory = self._FACTORIES.get(name)
            if factory is None:
                self.tiers[name] = "unknown engine name in WAKE_ENGINE_ORDER"
                continue
            engine = factory()
            engine.set_sensitivity(self.sensitivity)   # before open(): porcupine bakes it into create()
            try:
                ok = engine.open()
            except Exception as exc:
                # A tier must never be able to raise past here — that is what took the robot down
                # when pvporcupine started raising NotImplementedError at import.
                ok = False
                engine.unavailable = f"{type(exc).__name__}: {exc}"
            if ok:
                self._engine = engine
                self.frame_length = max(1, engine.frame_length)
                self.sample_rate = engine.sample_rate
                self.unavailable = None
                self.tiers[name] = "ok"
                skipped = [n for n in self.tiers if n != name]
                print(f"[wake] engine: {name} ({engine.kind}, {self.frame_length} samples)"
                      + (f" — skipped {', '.join(skipped)}" if skipped else ""), flush=True)
                return True
            self.tiers[name] = engine.unavailable or "failed"
            engine.close()   # never leak a half-initialised tier's native memory
            print(f"[wake] tier '{name}' unavailable — {self.tiers[name]}; trying next", flush=True)

        self.unavailable = ("; ".join(f"{k}: {v}" for k, v in self.tiers.items())
                            or "no engines configured")
        print(f"[wake] WARNING: no wake engine could start — {self.unavailable}. "
              f"Push-to-talk is unaffected.", flush=True)
        return False

    @property
    def ready(self) -> bool:
        return self._engine is not None

    @property
    def frame_ready(self) -> bool:
        """Ready AND frame-driven — what MicStream gates its framing loop on. With the utterance
        tier the chain is ready but there are no frames to push, and framing blocks to feed a no-op
        would be pure waste."""
        return self._engine is not None and self._engine.kind == "frame"

    @property
    def engine(self) -> str:
        return self._engine.name if self._engine is not None else ""

    @property
    def kind(self) -> str:
        return self._engine.kind if self._engine is not None else ""

    @property
    def last_score(self) -> float:
        return self._engine.last_score if self._engine is not None else 0.0

    def set_sensitivity(self, value: float) -> bool:
        """Retune the live tier. Returns True if the engine needs reopening for it to take effect —
        the caller must do that under MicStream's wake lock, never from an arbitrary thread."""
        self.sensitivity = max(0.0, min(1.0, float(value)))
        if self._engine is None:
            return False
        return self._engine.set_sensitivity(self.sensitivity)

    def process(self, frame: np.ndarray) -> bool:
        return self._engine.process(frame) if self._engine is not None else False

    def reset(self) -> None:
        if self._engine is not None:
            self._engine.reset()

    def close(self) -> None:
        engine, self._engine = self._engine, None
        if engine is not None:
            engine.close()


class SpeechGate:
    """Decides when an utterance starts and stops, from webrtcvad plus an RMS floor.

    webrtcvad alone is not usable here. It was tuned for telephony-level speech, while the INMP441 is
    a quiet MEMS mic with a DC offset sitting next to a Jetson fan and a PAM8403 amp — is_speech()
    fires on all of that. So a frame counts as speech only if the VAD says so AND it clears
    `rms_floor`, and an utterance only opens after `onset_frames` consecutive such frames.

    With webrtcvad absent the RMS floor is used on its own, so a dev box still exercises the state
    machine (worse discrimination, same transitions).
    """

    IDLE = "idle"
    SPEECH = "speech"

    def __init__(self, rate: int = SAMPLE_RATE, aggressiveness: int = VAD_AGGRESSIVENESS,
                 frame_ms: int = VAD_FRAME_MS, rms_floor: float = VAD_RMS_FLOOR,
                 onset_frames: int = VAD_ONSET_FRAMES, hangover_s: float = VAD_HANGOVER_S,
                 dc_block: bool = VAD_DC_BLOCK, rms_floor_hold: float | None = None) -> None:
        if frame_ms not in (10, 20, 30):
            raise ValueError(f"webrtcvad accepts 10/20/30 ms frames, got {frame_ms}")
        self.rate = rate
        self.frame_ms = frame_ms
        self.frame_size = rate * frame_ms // 1000
        self.rms_floor = rms_floor
        # Hysteresis: the bar to KEEP an utterance open is lower than the bar to open one. Never
        # higher than the open floor, whatever the config says.
        self.rms_floor_hold = min(
            rms_floor, VAD_RMS_FLOOR_HOLD if rms_floor_hold is None else rms_floor_hold)
        self.onset_frames = max(1, onset_frames)
        self.hangover_s = hangover_s
        self._dc_block = dc_block
        self._vad = webrtcvad.Vad(aggressiveness) if _VAD_OK else None
        self._frames = FrameAssembler(self.frame_size)
        # Observability: the last frame's level is the single most useful number for tuning
        # rms_floor on a headless board, so it's kept even when nothing consumes it.
        self.last_rms = 0.0
        self.speech_frames = 0
        self.onsets = 0
        # Ambient noise estimate, and the sliding-window minimum feeding it. Deliberately NOT reset
        # by reset(): reset() runs at every state change and on every un-mute, while this is a
        # property of the ROOM and must survive all of that. 0.0 means "not yet measured", which
        # reads as no lift at all — so a fresh gate behaves exactly like the fixed-floor version
        # until it has seen a full window.
        self.ambient = 0.0
        self._amb_window_frames = max(1, int(WAKE_AMBIENT_WINDOW_S * 1000 / self.frame_ms))
        self._amb_window_min = float("inf")
        self._amb_frames = 0
        self.reset()

    @property
    def vad_available(self) -> bool:
        return self._vad is not None

    def _lift(self, base: float, mult: float) -> float:
        """A configured floor raised to clear the measured room, but never lowered below it and
        never lifted past WAKE_AMBIENT_MAX_LIFT — see config/wake.py for why the cap is a safety
        feature rather than a tuning knob."""
        if not WAKE_AMBIENT_ADAPT or self.ambient <= 0.0:
            return base
        return min(max(base, self.ambient * mult), base * WAKE_AMBIENT_MAX_LIFT)

    @property
    def open_floor(self) -> float:
        """The live bar to OPEN an utterance. `rms_floor` is the configured floor under it."""
        return self._lift(self.rms_floor, WAKE_AMBIENT_OPEN_MULT)

    @property
    def hold_floor(self) -> float:
        """The live bar to KEEP one open. The outer min preserves the hysteresis invariant: hold
        must never sit above open, or an utterance could stay open on audio too quiet to have
        started it."""
        return min(self.open_floor, self._lift(self.rms_floor_hold, WAKE_AMBIENT_HOLD_MULT))

    def _track_ambient(self) -> None:
        """Fold the last frame into the room estimate. Called once per frame from update().

        Frozen while an utterance is OPEN, which matters more than it looks. Continuous speech has
        no true silence in it, so a minimum taken during a turn would settle on the speaker's own
        quietest syllable and lift the hold floor out from under them — re-creating the exact bug
        VAD_RMS_FLOOR_HOLD was added to fix. The room is measured when nobody is talking; an
        utterance runs on whatever calibration was in force when it opened.
        """
        if not WAKE_AMBIENT_ADAPT or self.state != self.IDLE:
            return
        self._amb_window_min = min(self._amb_window_min, self.last_rms)
        self._amb_frames += 1
        if self._amb_frames < self._amb_window_frames:
            return
        window_min = self._amb_window_min
        # The first window seeds directly — smoothing up from 0.0 would spend half a minute
        # pretending a loud room is quiet, which is the state this whole mechanism exists to escape.
        self.ambient = (window_min if self.ambient <= 0.0 else
                        (1.0 - WAKE_AMBIENT_SMOOTH) * self.ambient + WAKE_AMBIENT_SMOOTH * window_min)
        self._amb_window_min = float("inf")
        self._amb_frames = 0

    def set_rms_floor(self, floor: float) -> None:
        """Retune the speech-onset floor while running (dashboard-settable).

        Re-derives the hold floor too, or lowering the open floor below the configured hold would
        break the hysteresis invariant asserted in __init__ — hold must never sit above open, else an
        utterance could stay open on audio too quiet to have started it.
        """
        self.rms_floor = float(floor)
        self.rms_floor_hold = min(self.rms_floor, VAD_RMS_FLOOR_HOLD)

    def set_hangover(self, seconds: float) -> None:
        """Retune the trailing-silence clock while running.

        One gate serves two jobs with genuinely different answers: a turn needs 1.5 s so a speaker
        thinking mid-sentence isn't cut off, a wake scan needs ~0.45 s because it is one short phrase
        and the clock is pure latency. The session sets this at each capture's onset rather than
        keeping two gates — the value is only ever read while an utterance is open, so setting it as
        the utterance opens is unambiguous.
        """
        self.hangover_s = float(seconds)

    def reset(self) -> None:
        """Return to idle and drop all history — called on un-mute and at every state change, so
        pre-mute residue and half-frames can't count toward a new onset."""
        self.state = self.IDLE
        self._run = 0
        self._last_speech_t: float | None = None
        self._speech_started_t: float | None = None
        self._frames.reset()

    def _is_speech(self, frame: np.ndarray) -> bool:
        x = frame
        if self._dc_block:
            # Subtract this frame's own mean. MEMS mics carry a standing offset that inflates RMS,
            # and a pure offset would otherwise clear the floor on its own. Done per frame rather
            # than as a tracking one-pole deliberately: a slow filter needs ~20 frames to converge,
            # by which point an onset has already fired. At 20 ms this only removes content below
            # ~50 Hz, which is not speech.
            x = x.astype(np.float64) - float(np.mean(x))
        self.last_rms = rms(x)
        # Hysteresis: once an utterance is open, hold it on much quieter audio. Using the open floor
        # here made most syllables of real speech read as silence, so the hangover clock restarted
        # constantly and turns ended after ~0.7 s — cutting the speaker off mid-sentence.
        floor = self.hold_floor if self.state == self.SPEECH else self.open_floor
        if self.last_rms < floor:
            return False                       # below the floor: never speech, whatever the VAD says
        if self._vad is None:
            return True                        # no webrtcvad on this box — the floor is all we have
        try:
            return self._vad.is_speech(_to_int16(x).tobytes(), self.rate)
        except Exception:
            return True                        # a VAD error must not make Kai deaf

    def update(self, block: np.ndarray, now: float) -> str | None:
        """Feed 16 kHz mono int16 audio. Returns an event, or None if nothing changed:
          "onset"    — speech began; the caller should start capturing (with pre-roll)
          "hangover" — trailing silence reached VAD_HANGOVER_S; the utterance is over

        `now` is the timestamp of the END of this block, passed in so tests drive a fake clock."""
        event = None
        frames = self._frames.push(block)
        if not frames:
            return None
        # Timestamp each frame within the block so hangover timing doesn't quantize to block size.
        step = self.frame_ms / 1000.0
        first_t = now - (len(frames) - 1) * step
        for i, frame in enumerate(frames):
            t = first_t + i * step
            speech = self._is_speech(frame)
            # After classification, so a frame never raises the floor it was just judged against,
            # and before the transitions below, so `state` still reads IDLE for the frame that opens
            # an utterance.
            self._track_ambient()
            if speech:
                self.speech_frames += 1
                self._last_speech_t = t
            if self.state == self.IDLE:
                self._run = self._run + 1 if speech else 0
                if self._run >= self.onset_frames:
                    self.state = self.SPEECH
                    self.onsets += 1
                    self._run = 0
                    # Credit the onset to where the speech actually began, not to the frame that
                    # finally confirmed it — otherwise every utterance reads onset_frames too short.
                    self._speech_started_t = t - (self.onset_frames - 1) * step
                    event = "onset"
            elif not speech and self._last_speech_t is not None:
                if t - self._last_speech_t >= self.hangover_s:
                    self.state = self.IDLE
                    self._run = 0
                    event = "hangover"
                    break
        return event

    def speech_duration(self, now: float) -> float:
        """Seconds from the start of the current/just-ended utterance to its last speech frame.
        Silence in the hangover tail is excluded, so this is what MIN_UTTERANCE_S should be
        compared against."""
        if self._speech_started_t is None:
            return 0.0
        end = self._last_speech_t if self._last_speech_t is not None else now
        return max(0.0, end - self._speech_started_t)
