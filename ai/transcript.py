"""Deciding whether a Whisper transcript is worth acting on.

Pure text and numbers — no model, no audio, no state. Split out of ai/voice_assistant.py because
these gates answer a question the assistant only asks: "did the decoder produce something a person
actually said?" WHISPER_LANGUAGES constrains the LABEL a clip is given, not the OUTPUT, and a clip
labelled "en" is never re-transcribed — so a decode that emitted a line of Chinese, or invented a
sentence out of fan noise, reached the LLM unchallenged and got answered as though someone had
asked it.

Every gate degrades to "off" rather than to an exception when faster-whisper's fields move around;
see _segment_floats.
"""

from __future__ import annotations

import math
import numbers
import unicodedata

from config.voice import (
    TRANSCRIPT_MAX_NO_SPEECH_PROB, TRANSCRIPT_MIN_AVG_LOGPROB, TRANSCRIPT_MIN_LATIN_RATIO,
    TRANSCRIPT_SCRIPT_GUARD,
)


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
