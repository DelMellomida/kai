"""
Voice assistant pipeline: mic capture -> faster-whisper STT -> Ollama LLM -> Piper TTS + jaw.

Call VoiceAssistant.start_recording() / stop_recording() from Flask handlers.
Call VoiceAssistant.get_status() each dashboard SSE tick to read live state.

Capture has two modes. By default this class owns nothing: attach_mic() hands it a shared,
always-open stream (ai/session.py) and capture becomes arm/harvest calls against that. With no mic
attached it falls back to opening its own sd.InputStream per turn — the original push-to-talk path,
kept as the rollback for MIC_LEGACY_CAPTURE. Either way start_recording()/stop_recording() keep the
same names, guards and return shapes, so the Flask routes and the dashboard don't care which is live.

Anything asynchronous (STT, LLM, synth, playback) carries the turn `epoch` it was started under and
is dropped on mismatch. That one mechanism is what stops an abandoned reply from speaking into a
session that has already ended, and what stops a cleared history from being repopulated by a
request that was already in flight when it was cleared.
"""

from __future__ import annotations

import math
import numbers
import re
import subprocess
import threading
import time
import unicodedata
from collections import namedtuple
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
from scipy.signal import resample_poly

from ai import rag
from ai import tts
from ai.audio import normalize_for_asr

# ── Tuning ────────────────────────────────────────────────────────────────────
# All tunable knobs (audio, Whisper, Ollama, history, jaw-speaking envelope) live in
# config/voice.py; re-imported so the names stay module-level for internal use and so
# existing `from ai.voice_assistant import ...` callers keep working.
from config.voice import (
    SAMPLE_RATE, CHANNELS, LIVE_PROBE_DURATION_S, LIVE_PROBE_RMS_THRESHOLD,
    LIVE_PROBE_TIMEOUT_S, I2S_PROBE_SILENT_RETRIES, I2S_PROBE_RETRY_DELAY_S,
    FALLBACK_CAPTURE_RATES,
    I2S_MIC_NAME_HINTS, USB_MIC_NAME_HINTS, I2S_CAPTURE_CHANNELS, I2S_TAKE_CHANNEL,
    I2S_CAPTURE_RATE, I2S_SUSPEND_PULSE, I2S_PULSE_SOURCE, PULSE_SUSPEND_ALL_SOURCES,
    I2S_APPLY_ROUTE_ON_STARTUP, I2S_ROUTE_CARD, I2S_ROUTE_CONTROLS,
    WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE, WHISPER_LANGUAGE, WHISPER_LANGUAGES,
    WHISPER_CPU_THREADS, WHISPER_BEAM_SIZE, WHISPER_INITIAL_PROMPT,
    ASR_NORMALIZE, ASR_NORMALIZE_MAX_GAIN, ASR_NORMALIZE_SCAN,
    TRANSCRIPT_SCRIPT_GUARD, TRANSCRIPT_MIN_LATIN_RATIO, TRANSCRIPT_MIN_AVG_LOGPROB,
    TRANSCRIPT_MAX_NO_SPEECH_PROB,
    OLLAMA_URL, OLLAMA_MODEL, OLLAMA_TIMEOUT_S, OLLAMA_KEEP_ALIVE, OLLAMA_NUM_CTX, OLLAMA_NUM_GPU,
    OLLAMA_NUM_PREDICT, OLLAMA_PS_TIMEOUT_S, OLLAMA_LOG_TIMINGS, RAG_CONTEXT_PLACEMENT,
    MAX_HISTORY_TURNS,
    SPEAK_SEC_PER_WORD, SPEAK_MIN_SENTENCE_S, SPEAK_MAX_S, SPEAK_GAP_S,
    SPEAK_AMP, SPEAK_OPEN_S, SPEAK_CLOSE_S,
)
from config.wake import (
    CAPTURE_HARD_CAP_S, MIXER_TIMEOUT_S, TTS_MAX_SPOKEN_CHARS, TTS_TAIL_MUTE_S,
    WAKE_WHISPER_SCAN_LANGUAGE, WAKE_WHISPER_SCAN_MODEL,
)

# resolve_input_device() uses this to parse ALSA card ids so it can dedupe the many subdevice
# entries one card exposes. Card ids can be numeric ("hw:1,0") or named ("hw:APE,0"), so match
# everything up to the subdevice comma / closing paren (structural, not a tunable).
_HW_CARD_RE = re.compile(r"hw:([^,\)]+)")

# The resolved mic: which device index/rate to open, how many channels to capture, which channel
# holds the real audio (the INMP441 puts it only in the left slot), the sample dtype, and whether
# it's the raw I2S device (which needs pulseaudio suspended before each open).
MicChoice = namedtuple("MicChoice", "device rate channels take_channel dtype is_i2s")
MicChoice.__new__.__defaults__ = (False,)   # is_i2s defaults False (keeps non-i2s call sites terse)

# Fallback only — the real, editable persona lives in persona.txt (see load_persona()) so it
# can be tuned without touching code or restarting the service.
_DEFAULT_PERSONA = (
    "You are Kai, a small friendly companion robot built by Devcon Philippines. "
    "You have a camera for eyes and a servo neck that lets you look toward whoever "
    "is talking to you. Speak warmly and simply, like a curious, upbeat companion — "
    "not like a generic assistant. Keep replies short: 1-3 sentences, plain "
    "conversational text, no markdown, no lists, no code blocks."
)
PERSONA_PATH = Path(__file__).parent / "persona.txt"

NO_SPEECH_RESPONSE = "(didn't catch that — try again)"


def _best_allowed_language(info, allowed: tuple[str, ...]) -> str:
    """Pick the most probable of `allowed` from a TranscriptionInfo's language scores.

    `all_language_probs` is produced for free by any transcribe(language=None) call, so restricting
    the language set costs nothing until it is actually needed. Falls back to the first allowed entry
    when the field is missing (older faster-whisper) rather than guessing."""
    probs = getattr(info, "all_language_probs", None) or ()
    scores = {lang: p for lang, p in probs if lang in allowed}
    if not scores:
        return allowed[0]
    return max(scores, key=scores.get)


def latin_letter_ratio(text: str) -> float:
    """Fraction of `text`'s ALPHABETIC characters that are Latin. 1.0 when there are no letters.

    Only letters are counted: punctuation, digits, spaces and emoji are ignored, so they can neither
    trip the check nor dilute a genuinely non-Latin transcript into passing it.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 1.0
    latin = sum(1 for c in letters if "LATIN" in unicodedata.name(c, ""))
    return latin / len(letters)


def _segment_floats(segments, attr: str) -> list[float]:
    """Collect a per-segment numeric field, skipping anything that isn't a real finite number.

    Strict on purpose. A faster-whisper version that renames or omits one of these fields must
    degrade to "this gate is off", exactly as _best_allowed_language degrades when
    all_language_probs is missing — never to a TypeError that fails every turn. bool is excluded
    because it is a Real and would silently read as 0/1.
    """
    out: list[float] = []
    for seg in segments:
        value = getattr(seg, attr, None)
        if isinstance(value, numbers.Real) and not isinstance(value, bool):
            as_float = float(value)
            if math.isfinite(as_float):
                out.append(as_float)
    return out


def transcript_rejection(text: str, segments) -> str:
    """Why this transcript should be thrown away, or "" to keep it.

    Exists because WHISPER_LANGUAGES only constrains the detected-language LABEL. A clip labelled
    "en" is never re-transcribed, so a decode that emitted '嘿哀' — or invented a sentence out of fan
    noise — reached the LLM unchallenged and got answered as though someone had asked it. Checking the
    label is not the same as checking the output.

    Returns a human-readable reason so the log says which gate fired and with what number; a silent
    rejection would be indistinguishable from the mic being broken.
    """
    if not text:
        return ""                     # empty is already handled upstream, and is not a rejection
    if TRANSCRIPT_SCRIPT_GUARD:
        ratio = latin_letter_ratio(text)
        if ratio < TRANSCRIPT_MIN_LATIN_RATIO:
            return (f"only {ratio:.0%} of letters are Latin "
                    f"(< {TRANSCRIPT_MIN_LATIN_RATIO:.0%}) — not English or Tagalog")
    # Both of these come per-segment; the worst segment is what matters, since one confidently-wrong
    # stretch is enough to turn a transcript into a different question than the one that was asked.
    if TRANSCRIPT_MIN_AVG_LOGPROB is not None:
        logprobs = _segment_floats(segments, "avg_logprob")
        if logprobs and min(logprobs) < TRANSCRIPT_MIN_AVG_LOGPROB:
            return (f"decode confidence {min(logprobs):.2f} "
                    f"(< {TRANSCRIPT_MIN_AVG_LOGPROB}) — unintelligible")
    if TRANSCRIPT_MAX_NO_SPEECH_PROB is not None:
        nsp = _segment_floats(segments, "no_speech_prob")
        if nsp and max(nsp) > TRANSCRIPT_MAX_NO_SPEECH_PROB:
            return (f"no_speech_prob {max(nsp):.2f} "
                    f"(> {TRANSCRIPT_MAX_NO_SPEECH_PROB}) — words decoded out of silence")
    return ""

STATUS_IDLE         = "idle"
STATUS_RECORDING    = "recording"
STATUS_TRANSCRIBING = "transcribing"
STATUS_THINKING     = "thinking"
STATUS_DONE         = "done"
STATUS_ERROR        = "error"

# ── Jaw "speaking" pantomime ────────────────────────────────────────────────────
# Kai has no audio (yet), so when a reply is produced we drive the jaw servo for a
# window sized to how long that text would take to say aloud. The mouth opens once
# per sentence: it ramps open at the start of the sentence, holds open while the
# sentence is "spoken", then closes at the end, with a short closed pause between
# sentences. face_track.py reads speaking_openness() each frame and maps the 0..1
# result onto the jaw servo (overriding the human-mouth mirror during a reply).
# The SPEAK_* envelope values are imported from config/voice.py (see the import block above).

_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]*")


def _split_sentences(text: str) -> list[str]:
    """Split a reply into sentences on . ! ? — falls back to the whole text as one."""
    parts = [m.group().strip() for m in _SENTENCE_RE.finditer(text)]
    parts = [p for p in parts if p]
    if parts:
        return parts
    stripped = text.strip()
    return [stripped] if stripped else []


def _speak_segments(text: str, now: float) -> tuple[float, tuple[tuple[float, float], ...]]:
    """Build the per-sentence open/close schedule. Returns (start, segments) where each
    segment is (rel_start, rel_end) seconds from start — one per sentence, separated by
    SPEAK_GAP_S closed pauses, and truncated so the whole reply never exceeds SPEAK_MAX_S."""
    sentences = _split_sentences(text) or ["…"]
    segs: list[tuple[float, float]] = []
    t = 0.0
    for s in sentences:
        if t >= SPEAK_MAX_S:
            break
        words = max(1, len(s.split()))
        dur   = max(SPEAK_MIN_SENTENCE_S, words * SPEAK_SEC_PER_WORD)
        end   = min(t + dur, SPEAK_MAX_S)
        segs.append((t, end))
        t = end + SPEAK_GAP_S
    return now, tuple(segs)


def _speak_segments_for_duration(text: str, now: float, duration: float
                                 ) -> tuple[float, tuple[tuple[float, float], ...]]:
    """Like _speak_segments, but the per-sentence open/close schedule is stretched to fill exactly
    `duration` — the real synthesized-audio length — so the jaw stops the instant the sound does.
    Each sentence's span is apportioned by its word count; SPEAK_GAP_S closed pauses sit between
    sentences (dropped if they alone would exceed the audio). Returns an empty window for a
    non-positive duration (caller then falls back to the text-timed pantomime)."""
    if duration <= 0:
        return now, ()
    sentences = _split_sentences(text) or ["…"]
    n = len(sentences)
    gap = SPEAK_GAP_S if SPEAK_GAP_S * (n - 1) < duration else 0.0
    speak_total = duration - gap * (n - 1)
    words = [max(1, len(s.split())) for s in sentences]
    total_words = sum(words)
    segs: list[tuple[float, float]] = []
    t = 0.0
    for i, w in enumerate(words):
        dur = speak_total * (w / total_words)
        segs.append((t, t + dur))
        t += dur + (gap if i < n - 1 else 0.0)
    return now, tuple(segs)


def speaking_openness_at(now: float, start: float | None,
                         segments: tuple[tuple[float, float], ...]) -> float | None:
    """Jaw openness in [0, SPEAK_AMP] at time `now`, or None when the reply is finished /
    not started. Within a sentence the mouth ramps open, holds, then ramps closed
    (a trapezoid); between sentences it returns 0.0 (closed but still 'speaking')."""
    if start is None or not segments:
        return None
    t = now - start
    if t < 0 or t >= segments[-1][1]:
        return None
    for s0, s1 in segments:
        if s0 <= t < s1:
            dur     = s1 - s0
            open_t  = min(SPEAK_OPEN_S, dur / 2.0)
            close_t = min(SPEAK_CLOSE_S, dur / 2.0)
            local   = t - s0
            if open_t > 0 and local < open_t:
                env = local / open_t
            elif close_t > 0 and local > dur - close_t:
                env = (dur - local) / close_t
            else:
                env = 1.0
            return SPEAK_AMP * max(0.0, min(1.0, env))
    return 0.0  # in a between-sentence pause — mouth closed, still mid-reply


def load_persona() -> str:
    """Read persona.txt fresh on every call — cheap relative to STT+LLM latency, and gives
    free 'edits apply on the next turn' behavior with no file-watcher or caching needed.
    Falls back to _DEFAULT_PERSONA on any read failure or empty content — must never hard-fail."""
    try:
        content = PERSONA_PATH.read_text().strip()
    except OSError as exc:
        print(f"[voice_assistant] WARNING: could not read {PERSONA_PATH} ({exc}) — using default persona")
        return _DEFAULT_PERSONA
    if not content:
        print(f"[voice_assistant] WARNING: {PERSONA_PATH} is empty — using default persona")
        return _DEFAULT_PERSONA
    return content


def build_chat_messages(system_prompt: str, history: list[dict], user_text: str) -> list[dict]:
    """Pure helper: system prompt + capped rolling history + new user turn."""
    capped = history[-(MAX_HISTORY_TURNS * 2):]
    return [{"role": "system", "content": system_prompt}, *capped, {"role": "user", "content": user_text}]


# Ollama reports its own per-request timings, in nanoseconds, on every non-streamed response.
# They were being discarded, which is what made "Kai feels slow" unactionable: these three numbers
# are the only thing that tells the causes apart, and each has a different fix.
#   prompt_eval_*  — how long the PROMPT took to evaluate. Large means the KV cache prefix is being
#                    invalidated every turn (see the context placement note in _call_ollama).
#   eval_*         — token generation. A tok/s well under the GPU figure means the model got placed
#                    on CPU (see ensure_llm_warm / log_model_placement).
#   load_duration  — non-zero means the model was evicted and reloaded, which is ~48 s on this box.
_NS_PER_MS = 1_000_000


def _tok_per_s(count, duration_ns) -> float:
    """Tokens per second from Ollama's (count, nanoseconds) pair. 0.0 when either is missing or
    zero — this only ever feeds a log line, so it must not raise on a partial response."""
    try:
        return (count / (duration_ns / 1e9)) if count and duration_ns else 0.0
    except (TypeError, ZeroDivisionError):
        return 0.0


def _log_llm_timings(data: dict, label: str = "turn") -> dict:
    """Log Ollama's own timings for one response and return them as milliseconds.

    Best-effort and never raises: a mocked or partial response (no timing fields at all) logs
    nothing and returns zeros, so this can sit on the hot path without being able to cost a reply."""
    if not isinstance(data, dict):
        return {}
    prompt_n  = data.get("prompt_eval_count") or 0
    prompt_ns = data.get("prompt_eval_duration") or 0
    gen_n     = data.get("eval_count") or 0
    gen_ns    = data.get("eval_duration") or 0
    load_ns   = data.get("load_duration") or 0
    out = {
        "llm_prompt_ms": int(prompt_ns // _NS_PER_MS),
        "llm_gen_ms":    int(gen_ns // _NS_PER_MS),
        "llm_load_ms":   int(load_ns // _NS_PER_MS),
        "llm_prompt_tokens": int(prompt_n),
        "llm_gen_tokens":    int(gen_n),
        "llm_tok_s":         round(_tok_per_s(gen_n, gen_ns), 1),
    }
    if not (prompt_ns or gen_ns):
        return out          # nothing measured (e.g. a stubbed response) — don't print an empty line
    # The load line is separate and only printed when it fires: a reload is a different event from a
    # slow turn, and burying it in the common case is how it stayed invisible.
    if out["llm_load_ms"]:
        print(f"[llm] MODEL RELOADED: {out['llm_load_ms']}ms — placement was re-decided, "
              f"check `ollama ps` for the GPU/CPU split", flush=True)
    if not OLLAMA_LOG_TIMINGS:
        return out          # the reload warning above stays regardless — it is rare and actionable
    print(f"[llm] {label}: prompt {out['llm_prompt_tokens']} tok in {out['llm_prompt_ms']}ms "
          f"({_tok_per_s(prompt_n, prompt_ns):.0f} tok/s) | "
          f"gen {out['llm_gen_tokens']} tok in {out['llm_gen_ms']}ms "
          f"({out['llm_tok_s']:.1f} tok/s)", flush=True)
    return out


def _ollama_request(messages: list[dict]) -> dict:
    """POST to Ollama's chat endpoint and return the parsed response body. Raises RuntimeError with
    a human-readable message on failure.

    Returns the decoded JSON rather than the Response object so callers read the body — and the
    timing fields above — exactly once."""
    options = {"num_ctx": OLLAMA_NUM_CTX}
    if OLLAMA_NUM_GPU is not None:      # None = let Ollama auto-decide the GPU/CPU split (fast path)
        options["num_gpu"] = OLLAMA_NUM_GPU
    if OLLAMA_NUM_PREDICT is not None:  # bound the reply: unbounded generation was paid for twice,
        options["num_predict"] = OLLAMA_NUM_PREDICT   # once generating and again synthesizing it
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "messages": messages,
                "stream": False,
                "keep_alive": OLLAMA_KEEP_ALIVE,
                "options": options,
            },
            timeout=OLLAMA_TIMEOUT_S,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            f"Ollama unavailable — check `ollama pull {OLLAMA_MODEL}` "
            f"and that the service is running ({exc})"
        ) from exc
    return resp.json()


def log_model_placement() -> dict:
    """Log whether Ollama actually put the model on the GPU, by asking `/api/ps`.

    This is the failure config/voice.py already documents but nothing ever reported: Ollama decides
    the CPU/GPU split at LOAD time from whatever memory is free, OLLAMA_KEEP_ALIVE=-1 then pins that
    choice for the life of the service, and after hours of uptime GPU fragmentation produces a
    partial offload that runs at roughly half speed. From the outside that is indistinguishable from
    "Kai is being slow today", which is exactly why it needs a line in the log.

    Purely diagnostic and entirely best-effort — every failure path returns {} after at most a
    warning, so this can never affect a reply. Returns the parsed size/size_vram pair when known."""
    # Derived from OLLAMA_URL so there is one host to configure, not two. The chat endpoint is
    # ".../api/chat"; the process list is its sibling.
    ps_url = OLLAMA_URL.rsplit("/", 1)[0] + "/ps"
    try:
        resp = requests.get(ps_url, timeout=OLLAMA_PS_TIMEOUT_S)
        resp.raise_for_status()
        models = resp.json().get("models") or []
    except (requests.exceptions.RequestException, ValueError, AttributeError) as exc:
        print(f"[llm] could not read model placement from {ps_url} ({exc})", flush=True)
        return {}
    entry = next((m for m in models if m.get("name", "").startswith(OLLAMA_MODEL.split(":")[0])),
                 None)
    if entry is None:
        print(f"[llm] {OLLAMA_MODEL} is not loaded — the next reply pays the model load", flush=True)
        return {}
    total = entry.get("size") or 0
    vram  = entry.get("size_vram") or 0
    if not total:
        return {}
    pct = 100.0 * vram / total
    mb = 1024 * 1024
    if pct >= 99.0:
        print(f"[llm] {entry.get('name')} fully on GPU ({vram // mb} MB VRAM)", flush=True)
    else:
        # The actionable half: this is the ~2x slowdown, and the fix is a restart/reboot to
        # defragment, not a config change.
        print(f"[llm] WARNING: {entry.get('name')} is only {pct:.0f}% on GPU "
              f"({vram // mb} of {total // mb} MB) — generation will run roughly half speed. "
              f"Restart ollama (or reboot) to defragment GPU memory before relying on it.",
              flush=True)
    return {"size": total, "size_vram": vram, "gpu_pct": round(pct, 1)}


def _classify_device(name: str) -> str:
    """Bucket an input device by its name: 'i2s' (the preferred INMP441/APE mic), 'usb' (the
    fallback), or 'other'. Case-insensitive substring match; I2S is checked before USB."""
    lowered = (name or "").lower()
    if any(hint.lower() in lowered for hint in I2S_MIC_NAME_HINTS):
        return "i2s"
    if any(hint.lower() in lowered for hint in USB_MIC_NAME_HINTS):
        return "usb"
    return "other"


def _capture_rates_for(kind: str, advertised: int) -> tuple[int, ...]:
    """Capture rates to try for a device, in order. Every one of them is usable by the pipeline.

    The I2S mic is pinned: the route runs I2S2 at a fixed clock and the advertised rate is a lie
    (the real APE device reports 44100 while the route runs at 48 kHz), so there is exactly one
    candidate and it comes from config.

    For everything else the filter is arithmetic. MicStream resamples with an integer-ratio
    decimator, so a rate that does not divide SAMPLE_RATE is not merely suboptimal — it fails at
    Decimator construction and takes the whole session down (see FALLBACK_CAPTURE_RATES in
    config/voice.py for the incident). The advertised rate is therefore offered ONLY when it is
    divisible, and it goes first when it is, because opening a device at its native rate avoids a
    driver-side resample. The rest of the list follows as fallbacks, and the liveness probe — which
    opens the device for real — is what decides which of them the hardware actually accepts.
    """
    if kind == "i2s" and I2S_CAPTURE_RATE:
        return (I2S_CAPTURE_RATE,)
    rates = [r for r in FALLBACK_CAPTURE_RATES if r > 0 and r % SAMPLE_RATE == 0]
    if advertised > 0 and advertised % SAMPLE_RATE == 0 and advertised not in rates:
        rates.insert(0, advertised)
    elif advertised in rates:
        rates.insert(0, rates.pop(rates.index(advertised)))
    return tuple(rates)


def _capture_channels_for(kind: str) -> int:
    """How many channels to open for a device kind — the INMP441 must be captured in stereo
    (real audio is only in the left slot); everything else is mono."""
    return I2S_CAPTURE_CHANNELS if kind == "i2s" else CHANNELS


def _candidate_input_devices(devices: list[dict]) -> list[int]:
    """Distinct input-capable devices to probe, in preference order: I2S (INMP441) first, then
    USB, then everything else — with the system default heading the 'other' bucket. Keeps one
    representative per underlying ALSA card (avoids probing 20+ duplicate subdevice entries some
    cards expose). When no I2S/USB device is present this collapses to 'default first, then cards
    in order' — the historical behavior."""
    buckets: dict[str, list[int]] = {"i2s": [], "usb": [], "other": []}
    seen: set[int] = set()

    try:
        default_idx = sd.default.device[0]
    except Exception:
        default_idx = None
    # Seed 'other' with the system default so it leads the non-preferred devices.
    if isinstance(default_idx, int) and default_idx >= 0:
        buckets["other"].append(default_idx)
        seen.add(default_idx)

    seen_cards: set[str] = set()
    for idx, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) <= 0 or idx in seen:
            continue
        m = _HW_CARD_RE.search(dev.get("name", ""))
        if m:
            card = m.group(1)
            if card in seen_cards:
                continue
            seen_cards.add(card)
        buckets[_classify_device(dev.get("name", ""))].append(idx)
        seen.add(idx)
    return buckets["i2s"] + buckets["usb"] + buckets["other"]


def _probe_is_live(device: int, rate: int, channels: int = CHANNELS,
                   take_channel: int = 0, retries: int = 0) -> bool:
    """Record a brief burst and check for real signal — silent/disconnected inputs read as zero.

    `retries` re-reads a device that came back SILENT, up to that many extra times. It exists for
    the I2S mic, which can return exact digital silence on the first capture after its route is
    applied and read normally moments later (see I2S_PROBE_SILENT_RETRIES in config/voice.py).
    Silence is the only outcome worth retrying: a device that refuses to open, or that hangs, has
    given a definite answer, and re-asking costs a multiple of LIVE_PROBE_TIMEOUT_S on the session
    start path for no gain. Defaults to 0 so every other caller keeps the old single-read behavior.
    """
    for attempt in range(max(0, retries) + 1):
        if attempt:
            time.sleep(I2S_PROBE_RETRY_DELAY_S)
        live, silent = _probe_once(device, rate, channels, take_channel)
        if live:
            if attempt:
                print(f"[mic] device {device} came back on retry {attempt} — the first read was "
                      f"taken before the mic was delivering samples", flush=True)
            return True
        if not silent:
            return False        # refused to open, or hung: a definite answer, not a warm-up
    return False


def _probe_once(device: int, rate: int, channels: int = CHANNELS,
                take_channel: int = 0) -> tuple[bool, bool]:
    """One liveness read. Returns (live, was_silent) — the second flag separates "this device
    delivered nothing but zeros" from "this device would not give us samples at all", which is what
    lets the caller retry only the former.

    Captures `channels` channels (a mono open of the stereo-only INMP441 device fails outright)
    and measures RMS on `take_channel` only, so the mic's silent right channel can't dilute it."""
    result: dict = {}

    def _capture() -> None:
        try:
            rec = sd.rec(int(LIVE_PROBE_DURATION_S * rate), samplerate=rate,
                          channels=channels, dtype="int16", device=device)
            sd.wait()
        except Exception as exc:
            # RECORDED, not swallowed. This was a bare `return`, which made "the device refused to
            # open" indistinguishable from "the mic is silent" — both produced `i2s=False` with no
            # reason anywhere in the log. On 2026-08-07 that cost a full hardware investigation to
            # establish the mic was fine and startup contention had simply lost the probe. The
            # timeout path below already logs; this one has to as well.
            result["error"] = f"{type(exc).__name__}: {exc}"
            return                       # no "rec" key — reads as "not live" below
        result["rec"] = rec

    # Bounded on its own thread: sd.wait() has no timeout, and a device that opens but never
    # delivers frames blocks it forever rather than raising, so the except above cannot catch it.
    # See LIVE_PROBE_TIMEOUT_S — a hang here strands the whole session start.
    probe = threading.Thread(target=_capture, daemon=True, name="kai-mic-probe")
    probe.start()
    probe.join(LIVE_PROBE_TIMEOUT_S)
    if probe.is_alive():
        try:
            sd.stop()                    # abort the wedged stream so the thread can unwind
        except Exception:
            pass
        probe.join(1.0)
        print(f"[mic] WARNING: live probe on device {device} did not return within "
              f"{LIVE_PROBE_TIMEOUT_S:.0f}s — treating it as not live", flush=True)
        return False, False

    rec = result.get("rec")
    if rec is None:
        print(f"[mic] device {device} rejected the probe "
              f"({rate} Hz x{channels}): {result.get('error', 'unknown error')}", flush=True)
        return False, False
    if rec.ndim > 1 and rec.shape[1] > take_channel:
        rec = rec[:, take_channel]
    rms = float(np.sqrt(np.mean(rec.astype(np.float64) ** 2)))
    # Both outcomes logged, at one line per candidate device. "Read as silent" and "refused to open"
    # are different problems with different fixes (check the wiring vs. check what holds the card),
    # and telling them apart afterwards is only possible if the log said which happened.
    if rms <= LIVE_PROBE_RMS_THRESHOLD:
        print(f"[mic] device {device} read as silent "
              f"(rms={rms:.1f} <= {LIVE_PROBE_RMS_THRESHOLD})", flush=True)
        return False, True
    return True, False


def apply_i2s_route() -> bool:
    """Bring up the ALSA XBAR/I2S2 capture route the INMP441 needs (see mictest/RESULTS.md), so
    the mic works on every app start without the external i2s-mic-route.service or a manual SSH
    session. Runs the exact `amixer` control sequence from config. Best-effort and never raises:
    if `amixer` or the APE card is missing (dev box, or before the device-tree overlay loads) it
    logs and returns False, and resolve_input_device() falls back to the USB mic. Returns True
    only if the full route applied."""
    if not I2S_APPLY_ROUTE_ON_STARTUP:
        return False
    for name, value in I2S_ROUTE_CONTROLS:
        try:
            subprocess.run(
                ["amixer", "-c", I2S_ROUTE_CARD, "cset", f"name={name}", value],
                check=True, capture_output=True, text=True, timeout=MIXER_TIMEOUT_S,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            # The dominant failure is "no APE card / no amixer" — one control failing means the
            # rest will too, so stop after the first instead of emitting nine warnings.
            print(f"[voice_assistant] WARNING: could not apply I2S capture route "
                  f"('{name}' on card {I2S_ROUTE_CARD}: {exc}) — I2S mic may be unavailable; "
                  f"selection will fall back to the USB/default mic")
            return False
    print(f"[voice_assistant] applied I2S capture route on card {I2S_ROUTE_CARD}")
    return True


def _pactl_suspend(source: str, on: bool) -> None:
    """Suspend/resume a pulseaudio source via pactl. Best-effort; raises nothing.

    Bounded by a timeout because an unresponsive pulseaudio would otherwise block here forever, and
    this now runs on every mic open AND on every watchdog reopen — not just once per turn."""
    try:
        subprocess.run(["pactl", "suspend-source", source, "1" if on else "0"],
                       check=True, capture_output=True, text=True, timeout=MIXER_TIMEOUT_S)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"[voice_assistant] WARNING: pactl suspend-source {source} "
              f"{'1' if on else '0'} failed ({exc})")


def _pactl_source_names() -> list[str]:
    """Every pulseaudio capture source, monitors excluded. Empty if pactl/pulse is unavailable.

    Monitors are skipped deliberately: they are taps on an OUTPUT and hold no capture hardware, so
    suspending them would gain nothing and would break anything listening to what Kai plays.
    """
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"], check=True,
                             capture_output=True, text=True, timeout=MIXER_TIMEOUT_S).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    names = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and not parts[1].endswith(".monitor"):
            names.append(parts[1])
    return names


def free_i2s_device() -> None:
    """Release the capture cards from pulseaudio so the app can open a raw hw device directly at its
    true rate. pulse otherwise locks the APE card to 44100 and injects noise that garbles speech
    (Whisper hears nothing). No-op if disabled or pactl/pulse is absent.

    Every source is released, not just the I2S one: a source pulse holds makes the liveness probe of
    that device block until it times out, and a timed-out probe reads as "not live" — which is enough
    to skip the real mic and fall back to a 44.1 kHz pulse device that cannot be resampled to 16 kHz.
    See PULSE_SUSPEND_ALL_SOURCES in config/voice.py.
    """
    # I2S_SUSPEND_PULSE stays the master switch: it is documented as the way to opt out entirely (e.g.
    # pulse is not installed), so nothing here may touch pactl when it is False.
    if not I2S_SUSPEND_PULSE:
        return
    _pactl_suspend(I2S_PULSE_SOURCE, True)
    if PULSE_SUSPEND_ALL_SOURCES:
        for src in _pactl_source_names():
            if src != I2S_PULSE_SOURCE:
                _pactl_suspend(src, True)


def resume_pulse_source() -> None:
    """Hand the card back to pulseaudio — used when we end up NOT capturing from the raw I2S device
    (fallback to USB/system default), so the pulse-backed path isn't left muted."""
    if I2S_SUSPEND_PULSE:
        _pactl_suspend(I2S_PULSE_SOURCE, False)


def resolve_input_device() -> MicChoice:
    """Find a mic that actually captures signal, preferring the INMP441 I2S mic, then a USB mic,
    then the system default. Returns how to open it. NOTE: for the raw I2S device to probe live,
    pulseaudio must already be suspended (see free_i2s_device) — ensure_input_resolved does this."""
    try:
        devices = sd.query_devices()
    except Exception:
        return MicChoice(None, SAMPLE_RATE, CHANNELS, 0, "int16", False)
    for idx in _candidate_input_devices(devices):
        kind     = _classify_device(devices[idx].get("name", ""))
        channels = _capture_channels_for(kind)
        advertised = int(devices[idx].get("default_samplerate") or SAMPLE_RATE)
        # Every rate here is one MicStream can actually resample; a device that opens at none of
        # them is skipped rather than returned. Returning an unusable rate is what took the session
        # down on 2026-08-09 — see _capture_rates_for and FALLBACK_CAPTURE_RATES.
        # Only the I2S mic gets the silence retries: it is the one device with a warm-up, and it is
        # also the preferred one, so a single mistimed read there costs the whole session its best
        # mic. Silence from the USB/default devices is taken at face value.
        retries = I2S_PROBE_SILENT_RETRIES if kind == "i2s" else 0
        for rate in _capture_rates_for(kind, advertised):
            if _probe_is_live(idx, rate, channels, I2S_TAKE_CHANNEL, retries):
                return MicChoice(idx, rate, channels, I2S_TAKE_CHANNEL, "int16", kind == "i2s")
    print("[voice_assistant] WARNING: every candidate input device read as silent or refused every "
          "usable rate — falling back to system default mic (recordings may be empty)")
    return MicChoice(None, SAMPLE_RATE, CHANNELS, 0, "int16", False)


class VoiceAssistant:
    """Owns mic capture, STT, and LLM state. Independent of Flask/face_track globals."""

    def __init__(self) -> None:
        self._lock       = threading.Lock()
        self._status     = STATUS_IDLE
        self._transcript = ""
        self._response   = ""
        self._error      = ""
        self._history: list[dict] = []
        self._audio_chunks: list[np.ndarray] = []
        self._audio_samples = 0        # running total, so the legacy buffer can be bounded
        self._stream: sd.InputStream | None = None
        # Shared always-open capture (ai/session.py). None = own a stream per turn instead.
        self._mic = None
        # Turn generation. Every async result carries the epoch it began under; a mismatch means a
        # newer turn or a session reset happened meanwhile, and the result is discarded.
        self._epoch = 0
        self._tts_active = False       # True from before synthesis starts until playback ends
        # Which utterance owns _tts_active. Bumped by _begin_speech for every new line, and checked
        # by each worker before it clears the flag — because _tts_active is ONE boolean shared by
        # every speech path (reply, ack, canned, filler) and its workers finish out of order.
        # Observed 2026-08-09: a 7.5 s filler opener was still playing when the 2.8 s reply started,
        # and the opener's worker then cleared the flag out from under the reply. speech_in_flight()
        # went False mid-answer, which dropped SPEAKING straight to COOLDOWN and told the filler
        # loop that nothing was playing — so it started more lines on top. Everything talked at once.
        self._speech_gen = 0
        self._last_language = ""       # last Whisper language label; read by the filler bank
        self._gate_until = 0.0         # monotonic deadline covering the sink drain + amp settle
        self._capture_device: int | None = None
        self._capture_rate = SAMPLE_RATE
        self._capture_channels = CHANNELS
        self._capture_channel  = 0
        self._capture_dtype    = "int16"
        self._capture_is_i2s   = False
        self._device_resolved = False
        self._whisper_model = None
        self._scan_model = None        # tiny model, wake-phrase spotting only
        self._speak_start: float | None = None
        self._speak_segments: tuple[tuple[float, float], ...] = ()
        # Monotonic count of COMPLETED turns that produced something to show. Published as
        # voice_turn_id and used by the dashboard to decide when to append a chat bubble.
        #
        # It exists because inferring that from `voice_status` reaching "done" is not safe: that field
        # is driven by both this class and ai/session.py's projection, so any momentary gap between
        # them reads as a brand-new completed turn and re-posts the previous exchange verbatim. Three
        # separate races produced exactly that bug. A counter that only ever moves when a turn really
        # finished cannot be faked by a gap.
        self._turn_id = 0
        # Per-stage latency for the LAST completed turn, in milliseconds. Measured HERE, next to the
        # stages themselves, because this is the only place that can see them apart: ai/session.py
        # only observes "turn started" and "turn finished", which is why its sess_last_llm_ms was
        # really STT+RAG+LLM under an LLM label, and its stt_ms was never written at all.
        #
        # llm_ms is the wall time of the POST; llm_prompt_ms/llm_gen_ms/llm_tok_s come from Ollama's
        # own counters (see _log_llm_timings) and are what separate a slow prompt from slow
        # generation. first_audio_ms is the one a person actually feels: utterance handed over ->
        # first sample sent to the speaker.
        self._stage_ms = {
            "stt_ms": 0, "rag_ms": 0, "llm_ms": 0,
            "llm_prompt_ms": 0, "llm_gen_ms": 0, "llm_load_ms": 0,
            "llm_prompt_tokens": 0, "llm_gen_tokens": 0, "llm_tok_s": 0.0,
            "tts_synth_ms": 0, "first_audio_ms": 0,
        }
        # ASR input level diagnostics, kept per path because they answer different questions: the
        # turn entry says how far away the person Kai is talking to was, the scan entry says the
        # same for whoever the wake tier last overheard. See ASR_NORMALIZE in config/voice.py.
        self._norm_gain = {"turn": 1.0, "scan": 1.0}
        self._norm_rms  = {"turn": 0.0, "scan": 0.0}

    def input_levels(self) -> dict:
        """Copy of the last measured ASR input RMS and applied gain, per path. Any thread.

        This is the distance readout. A turn RMS well under ASR_NORMALIZE_TARGET_RMS with the gain
        pinned at ASR_NORMALIZE_MAX_GAIN means the speaker is further away than level correction can
        cover, and the next lever is the mic, not a constant."""
        with self._lock:
            return {"gain": dict(self._norm_gain), "rms": dict(self._norm_rms)}

    def stage_timings(self) -> dict:
        """Copy of the last turn's per-stage latency (see _stage_ms). Safe to call from any thread."""
        with self._lock:
            return dict(self._stage_ms)

    # ── Whisper model (lazy singleton, can be pre-warmed at startup) ──────────

    def ensure_model_loaded(self) -> None:
        if self._whisper_model is None:
            from faster_whisper import WhisperModel
            kwargs = {}
            if WHISPER_CPU_THREADS:
                # Left at ctranslate2's default this uses every core, which starves the servo
                # control loop and the tracking loop — see config/voice.py.
                kwargs["cpu_threads"] = WHISPER_CPU_THREADS
            self._whisper_model = WhisperModel(
                WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE, **kwargs
            )

    def ensure_scan_model_loaded(self) -> None:
        """Load the small, fast model used ONLY for wake-phrase spotting.

        Separate from the turn model on purpose: spotting two known words is a much easier job than
        transcribing a question, and doing it with "small" costs seconds per check — enough to make
        the whisper wake tier unusable. Falls back to the turn model when they're configured the same,
        so nothing is loaded twice."""
        if not WAKE_WHISPER_SCAN_MODEL or WAKE_WHISPER_SCAN_MODEL == WHISPER_MODEL:
            self.ensure_model_loaded()
            return
        if self._scan_model is None:
            from faster_whisper import WhisperModel
            kwargs = {}
            if WHISPER_CPU_THREADS:
                kwargs["cpu_threads"] = WHISPER_CPU_THREADS
            self._scan_model = WhisperModel(
                WAKE_WHISPER_SCAN_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE,
                **kwargs
            )
            print(f"[voice_assistant] wake-scan model loaded: {WAKE_WHISPER_SCAN_MODEL}"
                  f"/{WHISPER_COMPUTE}", flush=True)

    @property
    def scan_ready(self) -> bool:
        """True when wake-phrase spotting can run without paying a model load inside the check."""
        return self._scan_model is not None or self._whisper_model is not None

    def ensure_input_resolved(self) -> None:
        """Probe for a live mic once (the probe takes real time); safe to call repeatedly. First
        applies the I2S capture route and suspends pulseaudio so the raw INMP441 device is live and
        openable before it's probed; if we don't end up on the I2S device, pulse is handed back."""
        if not self._device_resolved:
            apply_i2s_route()   # best-effort; no-op / graceful fallback when the APE card is absent
            free_i2s_device()   # release the card from pulse so the raw hw probe can open at 48 kHz
            choice = resolve_input_device()
            if not choice.is_i2s:
                resume_pulse_source()   # not using the raw device — don't leave pulse muted
            with self._lock:
                self._capture_device   = choice.device
                self._capture_rate     = choice.rate
                self._capture_channels = choice.channels
                self._capture_channel  = choice.take_channel
                self._capture_dtype    = choice.dtype
                self._capture_is_i2s   = choice.is_i2s
                self._device_resolved  = True

    def ensure_llm_warm(self) -> None:
        """Best-effort: force Ollama to load the model now, off the push-to-talk hot path.
        Uses load_persona() (not the raw default) so a broken/missing persona.txt surfaces
        in the startup log, not on the user's first spoken query.

        This matters for more than latency on this box. Ollama chooses CPU-vs-GPU placement at LOAD
        time, from whatever memory is free at that moment, and OLLAMA_KEEP_ALIVE pins the choice for
        the rest of the run. Warming here — while memory is still fresh — is what gets the model onto
        the GPU. If it fails, the first spoken query loads the model later under whatever pressure
        exists by then, and can be stuck on CPU (measured: 9.6 tok/s on CPU vs 19.2 on GPU)."""
        try:
            data = _ollama_request(build_chat_messages(load_persona(), [], "hello"))
        except RuntimeError as exc:
            # Logged rather than swallowed: a real request would surface the same error to the user
            # eventually, but silence also hides the CPU-vs-GPU consequence described above.
            print(f"[voice_assistant] WARNING: LLM warm-up failed ({exc}) — the first reply will pay "
                  f"the model load, and may end up on CPU instead of GPU", flush=True)
            return
        _log_llm_timings(data, label="warmup")
        # Now that the placement is decided and pinned, say out loud which way it went. This is the
        # whole point of warming here rather than on the first query, and until now the outcome was
        # invisible — a partial offload just showed up later as "Kai is slow today".
        log_model_placement()

    # ── Shared capture + turn epoch ─────────────────────────────────────────

    def attach_mic(self, mic) -> None:
        """Hand over an always-open capture stream to record from, instead of opening one per turn.

        `mic` needs arm_utterance(preroll: bool) -> bool and harvest_utterance() -> (audio, rate).
        The raw I2S hw device admits exactly one opener, so once a session owns the stream this class
        must never open its own — see start_recording()."""
        with self._lock:
            self._mic = mic

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def bump_epoch(self) -> int:
        """Invalidate every result still in flight and return the new epoch."""
        with self._lock:
            self._epoch += 1
            return self._epoch

    def _epoch_ok(self, epoch: int | None) -> bool:
        """True if `epoch` is still current. None means "unversioned" (legacy push-to-talk, say())
        and always passes, so the old call paths behave exactly as before."""
        if epoch is None:
            return True
        with self._lock:
            return self._epoch == epoch

    def speech_in_flight(self) -> bool:
        """True from just before synthesis begins until playback has finished or been cut.

        This is the "is Kai still saying something" signal. tts.is_playing() alone is not it: there is
        a 0.5-1.5 s Piper run before any playback process exists, and a caller that watched only
        is_playing() would conclude the reply was already over and move on."""
        with self._lock:
            return self._tts_active

    def last_language(self) -> str:
        """The language Whisper labelled the most recent transcription, or "" before the first one.

        Exists for the filler bank (ai/filler.pick_lang), which has to choose a language BEFORE the
        current turn's transcript is necessarily ready — the opener fires ~0.5 s into a turn, and
        STT may still be running. So it deliberately reports the PREVIOUS utterance's language: in a
        conversation that is almost always the same one, and being one turn stale is far cheaper
        than either blocking on STT or defaulting every opener to Tagalog.

        Note this can only ever be a member of config/voice.WHISPER_LANGUAGES ("en", "tl") — the
        detector has no label outside that set, which is why pick_lang reaches the Bisaya bank by
        its own route rather than by waiting for a "ceb" that never arrives."""
        with self._lock:
            return self._last_language

    def mic_muted(self, now: float | None = None) -> bool:
        """True while Kai's own audio could reach the mic. The audio thread drops blocks on this.

        Deliberately NOT derived from voice_speaking: that is the jaw envelope, which can be a silent
        text-timed pantomime, and which is only set AFTER synthesis finishes — so it says nothing
        about the Piper run that precedes playback. _tts_active covers exactly that gap."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._tts_active or now < self._gate_until:
                return True
        return tts.is_playing() or tts.quiet_since(now) < TTS_TAIL_MUTE_S

    # ── Public API ──────────────────────────────────────────────────────────

    def start_recording(self) -> dict:
        with self._lock:
            if self._status not in (STATUS_IDLE, STATUS_DONE, STATUS_ERROR):
                return {"error": f"cannot start recording while {self._status}"}
            self._audio_chunks = []
            self._transcript = ""
            self._response   = ""
            self._error      = ""
            self._status     = STATUS_RECORDING
            self._audio_samples = 0
            self._speak_start = None   # stop the jaw if Kai was still 'speaking' a previous reply
            mic = self._mic
        tts.stop()                     # and cut off its audio so it doesn't bleed into the recording

        if mic is not None:
            # A session owns the stream. Opening a second one on the same raw hw device is the
            # failure this branch exists to prevent — it surfaces as intermittent silent capture.
            if mic.arm_utterance(preroll=True):
                return {"status": "ok"}
            with self._lock:
                self._status = STATUS_ERROR
                self._error  = "Could not arm the microphone"
            return {"error": self._error}

        self.ensure_input_resolved()
        with self._lock:
            device, capture_rate = self._capture_device, self._capture_rate
            channels, dtype = self._capture_channels, self._capture_dtype
            is_i2s = self._capture_is_i2s
        if is_i2s:
            free_i2s_device()   # re-assert in case pulse re-grabbed the card since we resolved
        try:
            stream = sd.InputStream(
                samplerate=capture_rate, channels=channels, dtype=dtype,
                device=device, callback=self._on_audio_chunk,
            )
            stream.start()
        except Exception as exc:
            with self._lock:
                self._status = STATUS_ERROR
                self._error  = f"Could not open microphone: {exc}"
            return {"error": self._error}
        with self._lock:
            self._stream = stream
        return {"status": "ok"}

    def stop_recording(self) -> dict:
        with self._lock:
            if self._status != STATUS_RECORDING:
                return {"error": f"cannot stop recording while {self._status}"}
            stream = self._stream
            self._stream = None
            mic = self._mic
            self._status = STATUS_TRANSCRIBING

        if mic is not None:
            audio, rate = mic.harvest_utterance()
        else:
            if stream is not None:
                stream.stop()
                stream.close()
            with self._lock:
                chunks = self._audio_chunks
                self._audio_chunks = []
                self._audio_samples = 0
                rate = self._capture_rate
            audio = np.concatenate(chunks, axis=0) if chunks else np.zeros((0, CHANNELS), dtype="int16")

        threading.Thread(target=self._process, args=(audio,), kwargs={"rate": rate},
                         daemon=True).start()
        return {"status": "ok"}

    def process_utterance(self, audio: np.ndarray, rate: int, epoch: int | None = None,
                          on_done=None) -> dict:
        """Run one already-captured utterance through STT -> LLM -> speech on a worker thread.

        The hands-free entry point, and the same code path stop_recording() takes — so a
        wake-word turn and a button turn cannot drift apart. `on_done(epoch, outcome)` fires when the
        worker finishes, with outcome one of "done" / "empty" / "error", which is how the session
        learns a turn ended without polling status."""
        with self._lock:
            if self._status not in (STATUS_IDLE, STATUS_DONE, STATUS_ERROR, STATUS_RECORDING):
                return {"error": f"busy: {self._status}"}
            self._transcript = ""
            self._response   = ""
            self._error      = ""
            self._status     = STATUS_TRANSCRIBING
        threading.Thread(target=self._process, args=(audio,),
                         kwargs={"rate": rate, "epoch": epoch, "on_done": on_done},
                         daemon=True, name="kai-turn").start()
        return {"status": "ok"}

    def get_status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            speaking = speaking_openness_at(now, self._speak_start, self._speak_segments) is not None
            return {
                "voice_status":     self._status,
                "voice_transcript": self._transcript,
                "voice_response":   self._response,
                "voice_error":      self._error,
                "voice_speaking":   speaking,
                "voice_turn_id":    self._turn_id,
            }

    def speaking_openness(self, now: float | None = None) -> float | None:
        """Jaw openness 0..SPEAK_AMP while Kai is 'speaking' its reply, else None.
        Called each frame by face_track.py to animate the jaw servo."""
        now = time.monotonic() if now is None else now
        with self._lock:
            start, segments = self._speak_start, self._speak_segments
        return speaking_openness_at(now, start, segments)

    def clear_turn_status(self) -> None:
        """Retire a finished turn's status back to IDLE, keeping its transcript/response for display.

        Needed because the dashboard appends a chat bubble on the *transition into* "done"
        (web/frontend/dashboard.html). A hands-free session projects "recording" while listening, so
        when it ends and the projection stops overriding, voice_status falls back to this stale "done"
        — the dashboard reads that as a brand-new completed turn and posts the same question and answer
        a second time. Clearing the status removes the false edge; the bubbles already shown stay."""
        with self._lock:
            if self._status in (STATUS_DONE, STATUS_ERROR):
                self._status = STATUS_IDLE

    def reset_history(self) -> None:
        """Forget the conversation. Bumps the epoch too, so a reply already in flight can't append
        itself afterwards and hand the next person one turn of the last person's conversation."""
        with self._lock:
            self._history = []
            self._epoch += 1
        # The retrieval side keeps its own one-line memory (the sticky DEVCON topic, see
        # config/rag.py STICKY_TURNS). It has to be forgotten with the history, or the next
        # person's first question inherits the last person's subject.
        rag.reset_topic()

    def say(self, text: str, use_llm: bool = True, epoch: int | None = None,
            on_done=None) -> dict:
        """Trigger a spoken reply (jaw animation) from text, with NO microphone involved.
        If use_llm (default) `text` is sent to Ollama as a prompt and the model's reply
        drives the mouth; otherwise `text` is spoken verbatim. Sets the same speak window
        face_track reads each frame, so the jaw animates identically to a voice turn — but
        completely decoupled from the audio-capture path (which can abort the process; see
        start_recording). The LLM call runs on a background thread so the caller returns fast.

        `epoch`/`on_done` mirror process_utterance and are what make a one-breath hands-free turn
        possible: the whisper wake tier already holds the transcript, so running the turn from text
        avoids transcribing the same audio twice. Both default to None → exactly today's behaviour,
        so the /voice/say route is unaffected."""
        text = (text or "").strip()
        if not text:
            return {"error": "no text provided"}
        with self._lock:
            if epoch is not None and self._epoch != epoch:
                return {"error": "stale"}
            if self._status not in (STATUS_IDLE, STATUS_DONE, STATUS_ERROR):
                return {"error": f"busy: {self._status}"}
            self._transcript = text if use_llm else ""
            self._response   = ""
            self._error      = ""
            self._status     = STATUS_THINKING if use_llm else STATUS_DONE

        def _run() -> None:
            outcome = "error"
            try:
                reply = self._call_ollama(text) if use_llm else text
                if not self._epoch_ok(epoch):
                    # A cold model load takes ~50 s; the session can easily have ended meanwhile.
                    outcome = "stale"
                    return
                with self._lock:
                    if use_llm:
                        self._history.append({"role": "user", "content": text})
                        self._history.append({"role": "assistant", "content": reply})
                        self._history = self._history[-(MAX_HISTORY_TURNS * 2):]
                    self._response = reply
                    self._status   = STATUS_DONE
                    self._turn_id += 1
                outcome = "done"
                self._speak(reply, epoch=epoch)
            except Exception as exc:
                if self._epoch_ok(epoch):
                    with self._lock:
                        self._status = STATUS_ERROR
                        self._error  = str(exc)
                else:
                    outcome = "stale"
            finally:
                if outcome == "stale":
                    # Releasing the status is what makes a stale turn survivable. say() refuses
                    # while _status is mid-flight, so a turn abandoned here would leave THINKING
                    # latched forever: every later reply is rejected with "busy: thinking", and Kai
                    # degrades to ack-only — replies synthesized, never spoken, nothing logged.
                    # Only THINKING is cleared, so a newer turn that has already moved the status
                    # on is left strictly alone.
                    with self._lock:
                        if self._status == STATUS_THINKING:
                            self._status = STATUS_IDLE
                if on_done is not None:
                    try:
                        on_done(epoch, outcome)
                    except Exception as exc:
                        print(f"[voice_assistant] WARNING: say callback failed ({exc})")

        if use_llm:
            threading.Thread(target=_run, daemon=True).start()
        else:
            _run()   # verbatim: no network call, set the speak window immediately
        return {"status": "ok"}

    # ── Internal ────────────────────────────────────────────────────────────

    def _on_audio_chunk(self, indata, frames, time_info, status) -> None:
        # Keep only the live channel so everything downstream stays mono (N, 1): for the stereo
        # INMP441 that's the left slot; for a mono USB/default mic the slice is the whole buffer.
        # .copy() is required — PortAudio reuses indata's buffer after the callback returns.
        col = self._capture_channel
        chunk = indata[:, col:col + 1].copy()
        with self._lock:
            self._audio_chunks.append(chunk)
            self._audio_samples += len(chunk)
            # Nothing auto-stops a push-to-talk recording, so a stuck button or a dashboard tab that
            # went away grows this list until the process dies. Drop the oldest audio past the cap.
            cap = int(CAPTURE_HARD_CAP_S * max(1, self._capture_rate))
            while self._audio_samples > cap and len(self._audio_chunks) > 1:
                self._audio_samples -= len(self._audio_chunks.pop(0))

    def _speak(self, reply: str, epoch: int | None = None, turn_t0: float | None = None) -> None:
        """Drive the jaw 'speak window' and, when TTS is available, play `reply` aloud through the
        speaker. Non-blocking: synthesis + playback run on a daemon thread so no caller is held for
        the audio's length (notably the Flask verbatim say() path, which runs on the request thread).
        _speak_start is set right before playback so the jaw and the audio start together. Any TTS
        failure (disabled, engine/model missing, bad synth) falls back to the text-timed pantomime so
        the mouth still moves. MUST be called OUTSIDE self._lock — it acquires the lock itself.

        NOTE: `reply` (with emoji) is what the UI shows; the SPOKEN text has emoji/symbols stripped
        (tts.clean_for_speech) so they aren't read aloud, and the jaw is timed to that spoken text.

        `epoch` versions the whole worker. Synthesis takes 0.5-1.5 s and playback can take many
        seconds, so a session can end mid-flight; without the checks below this reply would be spoken
        into whatever came next. None keeps the old unversioned behaviour.

        `turn_t0` is the monotonic time the turn began, passed only from _process. It exists purely
        to record first_audio_ms — how long the person waited between finishing their sentence and
        hearing anything back, which is the number that actually describes "Kai feels slow". None
        (the say()/canned paths, which have no turn behind them) just skips the measurement."""
        # Clamp only what gets SPOKEN — the dashboard still shows the whole reply. Kai is deaf while
        # talking (barge-in is off), so an over-long reply is an over-long deaf spell.
        spoken = tts.clamp_for_speech(tts.clean_for_speech(reply), TTS_MAX_SPOKEN_CHARS)
        jaw_text = spoken or reply           # if a reply is emoji-only, still mime to the original

        def _pantomime() -> None:
            with self._lock:
                self._speak_start, self._speak_segments = _speak_segments(jaw_text, time.monotonic())

        if not tts.enabled():
            _pantomime()
            return

        def _worker() -> None:
            tts.stop()                       # cut off any previous reply still playing
            try:
                synth_t0 = time.monotonic()
                wav = tts.synthesize(spoken)
                if turn_t0 is not None:
                    with self._lock:
                        self._stage_ms["tts_synth_ms"] = int((time.monotonic() - synth_t0) * 1000)
                if not self._epoch_ok(epoch):
                    # Silent by design, but indistinguishable in the log from a working speaker —
                    # say so, or a stale epoch reads as "the speaker is broken".
                    print(f"[tts] reply dropped after synthesis: epoch {epoch} is stale "
                          f"(now {self.epoch})", flush=True)
                    return                   # abandoned while Piper ran — say nothing at all
                if wav is None:              # synth failed — animate the jaw anyway, just silently
                    _pantomime()
                    return
                duration = tts.wav_duration(wav)
                if not self._epoch_ok(epoch):
                    print(f"[tts] reply dropped before playback: epoch {epoch} is stale "
                          f"(now {self.epoch})", flush=True)
                    return
                with self._lock:
                    if duration > 0:
                        self._speak_start, self._speak_segments = _speak_segments_for_duration(
                            jaw_text, time.monotonic(), duration)
                    else:                    # unknown length — best-effort text-timed window
                        self._speak_start, self._speak_segments = _speak_segments(jaw_text, time.monotonic())
                    # Hold the mic shut until the audio's own end plus the settle tail. Timed from
                    # the WAV's length, not from paplay exiting: paplay returns once the file is in
                    # the Pulse sink buffer, which is before the amp actually goes quiet.
                    if duration > 0:
                        self._gate_until = time.monotonic() + duration + TTS_TAIL_MUTE_S
                    if turn_t0 is not None:
                        self._stage_ms["first_audio_ms"] = int((time.monotonic() - turn_t0) * 1000)
                if turn_t0 is not None:
                    t = self.stage_timings()
                    # One line with the whole turn broken out, so the dead air is attributable at a
                    # glance instead of being one undifferentiated "llm_ms" number.
                    print(f"[turn] {t['first_audio_ms']}ms to first audio = "
                          f"stt {t['stt_ms']} + rag {t['rag_ms']} + llm {t['llm_ms']} "
                          f"(prompt {t['llm_prompt_ms']}, gen {t['llm_gen_ms']}) "
                          f"+ synth {t['tts_synth_ms']}", flush=True)
                tts.play(wav)                # blocks this worker thread only, until audio ends/cut
            finally:
                self._end_speech(gen)

        # Claimed BEFORE the worker starts, so the gate covers the synthesis window too — that's the
        # stretch voice_speaking can't see, since the jaw window only opens after synth returns.
        gen = self._begin_speech()
        threading.Thread(target=_worker, daemon=True, name="kai-tts").start()

    def _begin_speech(self) -> int:
        """Claim the speaker for a new utterance and return that utterance's generation token.

        Two jobs, both of which exist because ONE boolean and ONE paplay handle are shared by every
        speech path — reply, ack, canned and filler — whose workers finish out of order:

        1. Cut whatever is still playing. tts.play() does not replace the current process, it just
           starts a second one, so without this a filler that outlives its turn keeps talking
           straight over the answer it was covering. The session docstring always claimed the
           arriving reply cut the filler; nothing ever did, because the reply starts speaking inside
           the turn worker BEFORE on_done reaches _enter_speaking, so the session has no seam late
           enough to stop it and early enough to matter. Here is that seam.

        2. Hand back a token so only the NEWEST utterance may clear _tts_active. A worker that
           finishes late must not report silence on behalf of a line that is still going.
        """
        with self._lock:
            self._speech_gen += 1
            self._tts_active = True
            gen = self._speech_gen
        # Outside the lock: stop() waits on a process, and the worker it cuts takes _lock in its
        # finally. Holding _lock across that is a deadlock.
        tts.stop()
        return gen

    def _end_speech(self, gen: int) -> None:
        """Release the speaker, but only if no newer utterance has claimed it since."""
        with self._lock:
            if gen == self._speech_gen:
                self._tts_active = False

    def _speak_wav(self, wav: Path, jaw_text: str, epoch: int | None = None) -> None:
        """Speak an already-synthesized WAV (see tts.prewarm_canned) with the jaw synced to it.

        Used for the wake acknowledgement, where live synthesis would put 0.5-1.5 s of dead air
        between "Hey Kai" and "Yes?". Deliberately does NOT touch _status or _response: routing the
        ack through say() would make the dashboard post a "Kai: Yes?" chat bubble on every wake."""
        def _worker() -> None:
            try:
                duration = tts.wav_duration(wav)
                if not self._epoch_ok(epoch):
                    return
                with self._lock:
                    if duration > 0:
                        self._speak_start, self._speak_segments = _speak_segments_for_duration(
                            jaw_text, time.monotonic(), duration)
                        self._gate_until = time.monotonic() + duration + TTS_TAIL_MUTE_S
                    else:
                        self._speak_start, self._speak_segments = _speak_segments(jaw_text, time.monotonic())
                tts.play(wav)
            finally:
                self._end_speech(gen)

        gen = self._begin_speech()
        threading.Thread(target=_worker, daemon=True, name="kai-tts-canned").start()

    def transcribe_async(self, audio: np.ndarray, rate: int, on_done, token=None,
                         log_language: bool = True) -> None:
        """Transcribe on a worker thread and hand back the text. STT only.

        No LLM, no speech, and deliberately **no writes to _status/_transcript/_response**. The
        whisper wake tier needs a transcript and nothing else — writing turn state here would make
        the dashboard post a chat bubble for a sentence nobody addressed to Kai.

        Threading lives here, next to the model, so the session never spawns an STT thread and tests
        can fake this the way process_utterance is already faked. Calls
        `on_done(token, text, error)` exactly once, and never raises to the thread."""
        def _worker() -> None:
            text, error = "", ""
            try:
                text = self._transcribe(audio, rate=rate, log_language=log_language, scan=True)
            except Exception as exc:
                error = str(exc)
            try:
                on_done(token, text, error)
            except Exception as exc:   # a broken callback must not kill the thread silently
                print(f"[voice_assistant] WARNING: transcribe callback failed ({exc})")

        threading.Thread(target=_worker, daemon=True, name="kai-wakescan").start()

    def _process(self, audio: np.ndarray, rate: int | None = None, epoch: int | None = None,
                 on_done=None) -> None:
        """One turn: STT -> LLM -> speech. Runs on its own thread; never raises to the caller.

        `outcome` reported through on_done is "done" (a real reply), "empty" (nothing transcribed) or
        "error". A stale epoch reports "stale" and touches no shared state at all — the session has
        moved on, and writing _status/_response here would stomp on the turn that replaced this one."""
        outcome = "error"
        # Start of the turn as the person experiences it: everything from here to the first sample
        # out of the speaker is dead air. Threaded down to _speak so first_audio_ms measures the
        # whole pipeline (STT + RAG + LLM + synthesis) rather than any one stage of it.
        turn_t0 = time.monotonic()
        try:
            transcript = self._transcribe(audio, rate=rate)
            stt_ms = int((time.monotonic() - turn_t0) * 1000)
            if not self._epoch_ok(epoch):
                outcome = "stale"
                return
            with self._lock:
                # Committed only past the epoch check, like every other write here: a stale turn
                # must touch no shared state, and that includes the timings the dashboard reads.
                self._stage_ms["stt_ms"] = stt_ms
                self._transcript = transcript

            if not transcript.strip():
                outcome = "empty"
                with self._lock:
                    self._response = NO_SPEECH_RESPONSE
                    self._status   = STATUS_DONE
                    self._turn_id += 1
                # The session speaks its own cached line here, so it isn't voiced twice.
                if on_done is None:
                    self._speak(NO_SPEECH_RESPONSE, epoch=epoch)
                return

            with self._lock:
                self._status = STATUS_THINKING
            reply = self._call_ollama(transcript)

            if not self._epoch_ok(epoch):
                # A cold model load takes ~50 s; plenty of time for the session to have ended. The
                # guard also stops this append from resurrecting a history that was just cleared.
                outcome = "stale"
                return
            with self._lock:
                self._history.append({"role": "user", "content": transcript})
                self._history.append({"role": "assistant", "content": reply})
                self._history = self._history[-(MAX_HISTORY_TURNS * 2):]
                self._response = reply
                self._status   = STATUS_DONE
                self._turn_id += 1
            outcome = "done"
            self._speak(reply, epoch=epoch, turn_t0=turn_t0)
        except Exception as exc:
            if self._epoch_ok(epoch):
                with self._lock:
                    self._status = STATUS_ERROR
                    self._error  = str(exc)
            else:
                outcome = "stale"
        finally:
            if on_done is not None:
                try:
                    on_done(epoch, outcome)
                except Exception as exc:   # a broken callback must not take the turn thread down
                    print(f"[voice_assistant] WARNING: turn callback failed ({exc})")

    def _transcribe(self, audio: np.ndarray, rate: int | None = None,
                    log_language: bool = True, scan: bool = False) -> str:
        """Transcribe int16 audio captured at `rate` (default: this instance's resolved capture rate,
        which is what the legacy per-turn stream path uses). The shared always-open stream already
        emits 16 kHz and passes rate explicitly, so the resample below is skipped there.

        `log_language=False` for the wake-phrase scan — otherwise the detected-language line prints
        for every overheard sentence in the room and buries everything useful in the log.

        `scan=True` uses the small fast spotting model instead of the turn model. Not an optimisation:
        at "small" a check costs ~3 s per 1.6 s of audio, which made the whole tier time out."""
        if scan:
            self.ensure_scan_model_loaded()
            model = self._scan_model or self._whisper_model
        else:
            self.ensure_model_loaded()
            model = self._whisper_model
        if audio.size == 0:
            return ""
        rate = self._capture_rate if rate is None else rate
        samples = audio.astype(np.float32).reshape(-1) / 32768.0
        if rate != SAMPLE_RATE:
            g = math.gcd(SAMPLE_RATE, rate)
            samples = resample_poly(samples, SAMPLE_RATE // g, rate // g)
        # Level, not noise: a talker across the room arrives several times quieter than the audio
        # every constant here was tuned against, and Whisper decodes quiet input worse for no reason
        # other than the level. See ASR_NORMALIZE in config/voice.py. AFTER the resample so the
        # measured RMS is of the 16 kHz signal the decoder actually sees.
        if ASR_NORMALIZE and (ASR_NORMALIZE_SCAN or not scan):
            samples, gain, level = normalize_for_asr(samples)
            with self._lock:
                self._norm_gain["scan" if scan else "turn"] = gain
                self._norm_rms["scan" if scan else "turn"] = level
            # Logged only on the turn path and only when it did something: the scan runs on every
            # nearby utterance, so logging it would bury the log the way log_language would.
            if log_language and gain > 1.05:
                print(f"[voice_assistant] input level {level:.4f} rms — normalised x{gain:.1f}"
                      + (" (at the gain ceiling — the speaker is too far or too quiet)"
                         if gain >= ASR_NORMALIZE_MAX_GAIN - 1e-6 else ""), flush=True)
        # vad_filter drops non-speech stretches — extra defense against Whisper
        # hallucinating filler text (e.g. "You") on silence/near-silence audio.
        # The scan pins the language: auto-detect on a weak model and a 1-3 s clip produces confident
        # nonsense in random languages (see WAKE_WHISPER_SCAN_LANGUAGE).
        language = WAKE_WHISPER_SCAN_LANGUAGE if scan else WHISPER_LANGUAGE
        # Turn transcribes only — see WHISPER_INITIAL_PROMPT for why the wake scan stays unbiased.
        prompt = None if scan else (WHISPER_INITIAL_PROMPT or None)
        # list(), not a generator: the sanity gate below needs each segment's avg_logprob and
        # no_speech_prob, and joining the text would have already consumed them.
        segments, info = model.transcribe(samples, language=language, vad_filter=True,
                                          beam_size=WHISPER_BEAM_SIZE, initial_prompt=prompt)
        segs = list(segments)
        text = " ".join(seg.text.strip() for seg in segs).strip()

        # Auto-detect wandered outside the languages Kai is actually spoken to. Redo the pass, forced
        # to the most likely allowed language — the first result was decoded as e.g. Welsh and is not
        # worth keeping. Reusing the first pass when it lands on an allowed language is what keeps
        # this free in the normal case.
        if language is None and WHISPER_LANGUAGES and info is not None:
            allowed = tuple(WHISPER_LANGUAGES)
            if info.language not in allowed:
                forced = _best_allowed_language(info, allowed)
                if log_language:
                    print(f"[voice_assistant] detected '{info.language}' "
                          f"({info.language_probability:.2f}) — not in {allowed}; "
                          f"re-transcribing as '{forced}'")
                segments, info = model.transcribe(samples, language=forced, vad_filter=True,
                                                  beam_size=WHISPER_BEAM_SIZE,
                                                  initial_prompt=prompt)
                segs = list(segments)
                text = " ".join(seg.text.strip() for seg in segs).strip()

        # The label said an allowed language; that is not the same as the OUTPUT being one. Checked
        # AFTER any forced re-transcribe, so a rejection means both passes failed, not just the first.
        # Discarded rather than repaired: unintelligible audio does not become intelligible on a
        # second look, and "Sorry, I didn't catch that" is the honest answer. Feeding it onward means
        # Kai confidently answers a question nobody asked.
        reason = transcript_rejection(text, segs)
        if reason:
            # Always logged, even with log_language off (the wake scan): a discard that leaves no
            # trace is indistinguishable from a broken mic, which is exactly the class of bug that
            # took a hardware investigation to find on 2026-08-07.
            print(f"[voice_assistant] discarded transcript — {reason}: {text[:60]!r}", flush=True)
            return ""

        # Recorded only for a transcript that SURVIVED the rejection check above — a discarded
        # transcript's language label came from audio Kai decided was unintelligible, and letting
        # that steer the next turn's filler would propagate the noise.
        if info is not None and not scan and getattr(info, "language", None):
            with self._lock:
                self._last_language = info.language

        if log_language and language is None and info is not None:
            # Guarded: this is only a log line, and letting it raise would turn a perfectly good
            # transcript into a failed turn inside _process's except.
            print(f"[voice_assistant] detected language: {info.language} ({info.language_probability:.2f})")
        return text

    def _call_ollama(self, text: str) -> str:
        with self._lock:
            history = list(self._history)
        persona = load_persona()
        # The previous user turn, so a follow-up that only says "it" still retrieves its subject.
        previous_user_text = next((m["content"] for m in reversed(history) if m["role"] == "user"),
                                  None)
        rag_t0 = time.monotonic()
        context = rag.retrieve_context(text, previous_user_text=previous_user_text)
        rag_ms = int((time.monotonic() - rag_t0) * 1000)

        # WHERE the context goes decides whether Ollama can reuse its KV cache between turns. It
        # caches the longest common PREFIX of the prompt, and the retrieved context changes every
        # turn — so putting it in the system message (the original behaviour, kept as the "system"
        # placement) parks a per-turn-varying block at the very front and forces the persona AND all
        # MAX_HISTORY_TURNS of history to be re-evaluated on every single turn. Prepending it to the
        # user turn instead leaves that whole prefix byte-identical, so only the new context and
        # question are evaluated. See RAG_CONTEXT_PLACEMENT in config/voice.py for the revert.
        #
        # Safe because self._history stores the RAW transcript for user turns (see _process), never
        # the injected context — so what got prepended this turn does not perturb the prefix next
        # turn, which is the property the whole optimisation rests on.
        if context and RAG_CONTEXT_PLACEMENT == "system":
            system_prompt, user_content = f"{persona}\n\n{context}", text
        elif context:
            system_prompt, user_content = persona, f"{context}\n\n{text}"
        else:
            system_prompt, user_content = persona, text

        messages = build_chat_messages(system_prompt, history, user_content)
        llm_t0 = time.monotonic()
        data = _ollama_request(messages)
        llm_ms = int((time.monotonic() - llm_t0) * 1000)
        timings = _log_llm_timings(data, label="turn")
        with self._lock:
            self._stage_ms.update(timings)
            self._stage_ms["rag_ms"] = rag_ms
            self._stage_ms["llm_ms"] = llm_ms
        return data["message"]["content"]
