"""When Kai's jaw is open, and how far.

Kai mimes what it is saying: the mouth ramps open at the start of each sentence, holds while the
sentence is spoken, and closes at the end, with a short closed pause between sentences.
face_track.py reads the result every frame and maps it onto the jaw servo.

Entirely pure — text and timestamps in, a schedule out — which is why it is its own module. There
are two ways to build the schedule and the difference matters: _speak_segments times it from the
WORDS (used when synthesis is off or failed, so the mouth still moves), while
_speak_segments_for_duration stretches it to the real synthesized audio length so the jaw stops the
instant the sound does.

The SPEAK_* envelope constants live in config/voice.py.
"""

from __future__ import annotations

import re

from config.voice import (
    SPEAK_AMP, SPEAK_CLOSE_S, SPEAK_GAP_S, SPEAK_MAX_S, SPEAK_MIN_SENTENCE_S, SPEAK_OPEN_S,
    SPEAK_SEC_PER_WORD,
)


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
