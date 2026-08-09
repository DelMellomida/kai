#!/usr/bin/env python3
"""Measure the median speaking pitch of rendered voice samples, so a multi-speaker sweep can be
filtered to the ones that actually sound female or androgynous.

Kai's voice is she/they. Several of the candidate models are multi-speaker and ship NO usable
gender metadata: en_US-libritts_r-medium has **904** speakers keyed by opaque LibriTTS ids, and the
sherpa Kitten/Supertonic packages document none at all. Rendering all of them for a human to sort
by ear is the obvious approach and the wrong one — it is hundreds of files, most of them male.

Pitch is the measurement that answers the question directly, and it needs no metadata: "sounds
female" is largely median f0. So: render a sweep, run it through here, keep the ones in range,
and only then spend anyone's ears.

    python3 -m scripts.tts_pitch /tmp/kai_pitch/*.wav
    python3 -m scripts.tts_pitch --min 165 --max 260 /tmp/kai_pitch/*.wav   # female only
    python3 -m scripts.tts_pitch --names-only --min 150 out/*.wav | head -20

This is a SHORTLISTING tool, not a verdict. f0 separates voice ranges reliably; it says nothing
about whether a voice is pleasant, expressive, or right for Kai. Everything it passes still has to
be listened to.
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

# Conventional adult speaking-f0 bands. The boundaries overlap in reality — which is the point of
# having an androgynous band rather than a line — so treat these as bucket labels for a shortlist,
# not as claims about any speaker.
BANDS = (
    (0,   145, "masculine"),
    (145, 175, "androgynous"),
    (175, 300, "feminine"),
    (300, 1e9, "very high"),
)

# Search range for the estimator. Below 70 Hz is room rumble and the amp's noise floor; above
# 400 Hz a speaking voice would have to be shouting, and it is usually the first harmonic being
# tracked instead of the fundamental.
F0_MIN, F0_MAX = 70.0, 400.0

FRAME_S = 0.040        # 40 ms — several periods of even a low voice, short enough to track pitch
HOP_S = 0.020
# Frames quieter than this fraction of the file's peak RMS are treated as silence and skipped.
# Piper and friends pad every WAV with leading/trailing silence (see config/filler.py:198-207),
# and autocorrelation on near-silence returns confident nonsense.
SILENCE_REL = 0.15
# An unvoiced frame (a fricative, a stop) has no periodicity to find. Below this normalised
# autocorrelation peak the frame is dropped rather than contributing a wrong number to the median.
VOICED_MIN_CORR = 0.30


def read_mono(path: Path) -> tuple[np.ndarray, int]:
    """WAV -> float32 mono in [-1, 1], plus its sample rate. Rate comes from the header rather than
    being assumed: the candidates run at 22.05 kHz (Piper/Matcha) and 24 kHz (Kokoro/Kitten)."""
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        n_ch = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit PCM, got {width * 8}-bit")
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if n_ch > 1:
        # The post-processed files are stereo duplicates of a mono synth (TTS_POST_CHANNELS), so
        # averaging is lossless here rather than a compromise.
        audio = audio.reshape(-1, n_ch).mean(axis=1)
    return audio, rate


def frame_f0(frame: np.ndarray, rate: int) -> float | None:
    """Fundamental of one frame by normalised autocorrelation, or None if unvoiced.

    Autocorrelation rather than anything fancier because the input is clean synthetic speech with
    no background noise — the failure modes that justify YIN/pYIN (reverb, overlapping talkers)
    are not present, and this keeps the script dependency-free beyond numpy, which the robot
    already has for the audio pipeline."""
    frame = frame - frame.mean()
    if not np.any(frame):
        return None
    corr = np.correlate(frame, frame, mode="full")[len(frame) - 1:]
    if corr[0] <= 0:
        return None
    corr /= corr[0]                       # normalise so the voiced-ness threshold is scale-free
    lo = int(rate / F0_MAX)
    hi = min(int(rate / F0_MIN), len(corr) - 1)
    if hi <= lo:
        return None
    seg = corr[lo:hi]
    # First strong PEAK, not the global max: the autocorrelation of a periodic signal peaks at every
    # multiple of the period, and taking the largest can lock onto 2T (halving the reported pitch).
    peak = int(np.argmax(seg))
    if seg[peak] < VOICED_MIN_CORR:
        return None
    lag = lo + peak
    return rate / lag if lag else None


def analyse(path: Path) -> dict | None:
    """Pitch statistics for one file, or None when nothing voiced was found.

    Returns median f0, voiced fraction, and — the number that actually matters for "it sounds
    robotic" — the pitch RANGE in semitones.

    Median f0 answers "does this voice sound female"; it says nothing about whether the voice has
    any intonation. A monotone and a lively delivery can share a median exactly. The p10-p90 spread
    in semitones is the standard measure of intonation, and semitones rather than Hz because pitch
    is perceived logarithmically: a 30 Hz wobble is dramatic on a 100 Hz voice and barely audible
    on a 250 Hz one, so comparing candidates in Hz would systematically flatter the low ones.

    `dyn_db` is the amplitude counterpart — loudness variation across the utterance. Both feed the
    same impression: speech that never changes pitch OR volume is what people mean by "flat"."""
    audio, rate = read_mono(path)
    n = int(FRAME_S * rate)
    hop = int(HOP_S * rate)
    if len(audio) < n:
        return None
    frames = [audio[i:i + n] for i in range(0, len(audio) - n, hop)]
    rms = np.array([float(np.sqrt(np.mean(f ** 2))) for f in frames])
    if not len(rms) or rms.max() <= 0:
        return None
    loud = rms >= SILENCE_REL * rms.max()
    f0s = np.array([f for f, ok in zip((frame_f0(f, rate) for f in frames), loud) if ok and f])
    if not len(f0s):
        return None
    median = float(np.median(f0s))
    # p10-p90 rather than min-max: one octave-halving estimation error at a single frame would
    # otherwise dominate the range and report a monotone as expressive.
    lo, hi = np.percentile(f0s, [10, 90])
    semitones = float(12.0 * np.log2(hi / lo)) if lo > 0 else 0.0
    voiced_rms = rms[loud]
    dyn_db = (float(20 * np.log10(np.percentile(voiced_rms, 90) /
                                  max(np.percentile(voiced_rms, 10), 1e-9)))
              if len(voiced_rms) else 0.0)
    return {
        "f0": median,
        "voiced": len(f0s) / max(1, int(loud.sum())),
        "semitones": semitones,
        "dyn_db": dyn_db,
    }


def band(f0: float) -> str:
    for lo, hi, label in BANDS:
        if lo <= f0 < hi:
            return label
    return "?"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="WAV files to measure")
    ap.add_argument("--min", type=float, default=0.0, help="only report f0 at or above this (Hz)")
    ap.add_argument("--max", type=float, default=1e9, help="only report f0 at or below this (Hz)")
    ap.add_argument("--names-only", action="store_true",
                    help="print just the matching paths, for piping into cp/rm")
    ap.add_argument("--by-range", action="store_true",
                    help="sort by intonation range (most expressive last) instead of by pitch — "
                         "this is the ordering to use when the complaint is 'it sounds flat'")
    ap.add_argument("--raw-too", action="store_true",
                    help="include the *_raw.wav twins, to see what the sox chain does to dynamics")
    args = ap.parse_args()

    rows = []
    for f in args.files:
        p = Path(f)
        if not p.is_file() or (p.name.endswith("_raw.wav") and not args.raw_too):
            continue     # skip the pre-post-process twins: same voice, twice the work
        try:
            st = analyse(p)
        except (OSError, ValueError, wave.Error) as exc:
            print(f"  ! {p.name}: {exc}", file=sys.stderr)
            continue
        if st is None or not (args.min <= st["f0"] <= args.max):
            continue
        rows.append((st, p))

    rows.sort(key=lambda r: r[0]["semitones" if args.by_range else "f0"])
    for st, p in rows:
        if args.names_only:
            print(p)
        else:
            print(f"{st['f0']:6.1f} Hz  {band(st['f0']):<12} "
                  f"range {st['semitones']:5.1f} st  dyn {st['dyn_db']:5.1f} dB  "
                  f"voiced {st['voiced']:4.0%}  {p.name}")
    if not args.names_only:
        print(f"\n{len(rows)} file(s). 'range' is the p10-p90 pitch spread in semitones — the "
              f"intonation measure.\nHuman conversational speech typically runs 6-12 st; under "
              f"~4 st reads as monotone.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
