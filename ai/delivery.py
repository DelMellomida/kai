"""Delivery shaping — make Kai's speech less uniformly narrated, without changing the voice model.

Called by ai/voice_assistant._speak on the SPOKEN text only, after tts.clean_for_speech and before
tts.clamp_for_speech. The dashboard/UI text is never touched, exactly like the emoji stripping.

WHY THIS EXISTS, and why it is not a voice swap. docs/plan/completed/expressive-voice-plan.md records the
measurement: 29 voices across 7 engine families were benchmarked on this Jetson and rejected the same
way, the shipping voice already measures 11.0 semitones of intonation range (human conversational is
6-12), and the only model class that actually models expression is ~0.5B, which OOMs on load beside
Ollama's 2.37 GB. The complaint ("no ups and downs, fixed tone") survives every knob the *model* has.
Research attributes what is left to missing breaths, uniform pacing and generic emphasis — delivery,
not timbre. So this module attacks delivery from the text side, where it costs nothing:

  breaths  a comma before a clause-initial conjunction in a long run, so Piper takes a breath where
           a person would instead of running the whole sentence out on one contour
  opener   a short discourse marker on SOME replies, so Kai starts a turn the way a person answers a
           question rather than the way a page starts a paragraph. The first second is what a
           listener judges, and it is currently always the same shape.
  tempo    a small deterministic per-reply jitter on Piper's --length-scale, so consecutive replies
           are not delivered at byte-identical speed. Free: it is one CLI argument, not a re-synth.

NONE OF THIS RAISES PITCH RANGE and it is not meant to — that was measured and is not the problem.
It makes the pacing non-uniform. Judge it by ear across several turns, not on one line.

Pure stdlib and pure functions, deliberately: everything here is string handling where all the bugs
live in the offsets and the thresholds, so it stays testable with plain strings and no audio.

Determinism matters and is not incidental. The variation is keyed on a CRC of the text, never on
random(), so the same reply is always delivered the same way — a canned line does not drift between
runs, a bug reproduces, and tests can assert exact output. `hash()` would not do: Python salts it per
process, so the same text would shape differently after a restart.

TAGALOG: the conjunction and opener lists are English (see config/voice.py). A Tagalog reply simply
matches nothing and passes through unshaped, which is the intended degradation — Kai reads Tagalog in
an English-accented voice already (the voice is en_US — see config/voice.py TTS_VOICE_MODEL), and
inventing Tagalog discourse markers is a bigger claim than this module should make.

VERIFYING A CHANGE TO THE WORD/PUNCTUATION LISTS: espeak-ng (Piper's phonemizer) voices some strings
in ways you cannot hear from reading them — config/thinking.py records "Hmmmm..." coming back as
"H-A-M-A-M-M". The check is to synthesize through Piper and transcribe the result with Whisper, not
to eyeball the string. DELIVERY_PAUSE was picked that way and the first guess was wrong: a comma is
unmistakably a prosodic break *typographically*, but on this voice it measured ~10 ms — no breath at
all — in half the sentences tested. The semicolon it was replaced with measures ~3x a no-break
control and, unlike the comma, does it consistently. The numbers are in config/voice.py.
"""

from __future__ import annotations

import re
from zlib import crc32

from config.voice import (
    DELIVERY_BREATH_CONJUNCTIONS, DELIVERY_BREATH_MAX_PER_SENTENCE, DELIVERY_BREATH_MIN_WORDS,
    DELIVERY_BREATH_MIN_TAIL_WORDS, DELIVERY_OPENERS, DELIVERY_OPENER_MIN_WORDS,
    DELIVERY_OPENER_RATE, DELIVERY_OPENER_SKIP_STARTS, DELIVERY_PAUSE,
    DELIVERY_TEMPO_JITTER, DELIVERY_TEMPO_MAX, DELIVERY_TEMPO_MIN,
)
# DELIVERY_ENABLED deliberately NOT imported: it is dashboard-settable, so it is read live via
# settings.get("delivery_shaping") at the point of use. settings.py takes its default from it.
import settings


def enabled() -> bool:
    """True when delivery shaping should be applied. Read live so the dashboard toggle takes effect
    on the very next thing Kai says — which is the whole point of the toggle: this is a change you
    can only judge by ear, by flipping it back and forth while Kai talks."""
    return bool(settings.get("delivery_shaping"))


# Sentence boundary: a terminator followed by whitespace. clean_for_speech has already collapsed all
# whitespace to single spaces by the time we see the text, so this needs no multiline handling.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")

# Anything that is already a prosodic break. Used to measure "words since the last breath" — an
# existing comma resets the run, so we never stack a second break onto one the LLM already wrote.
_BREAK_CHARS = ",;:—–…"


def _conjunction_re() -> re.Pattern[str] | None:
    """Word-boundary alternation over DELIVERY_BREATH_CONJUNCTIONS, longest first so a multi-word
    entry ('and then') wins over the single word it starts with ('and'). None when the list is
    empty, which is how breath insertion is turned off from config alone."""
    words = [w.strip() for w in DELIVERY_BREATH_CONJUNCTIONS if w and w.strip()]
    if not words:
        return None
    words.sort(key=len, reverse=True)
    # Internal spaces become \s+ so "and then" still matches however the text spaced it.
    alts = [r"\s+".join(re.escape(part) for part in w.split()) for w in words]
    return re.compile(r"\b(?:" + "|".join(alts) + r")\b", re.IGNORECASE)


_CONJ_RE = _conjunction_re()


def _add_breaths(sentence: str) -> str:
    """Insert DELIVERY_PAUSE before clause-initial conjunctions in `sentence`.

    A conjunction only earns a break when the run leading up to it is long enough to be worth
    breathing after (DELIVERY_BREATH_MIN_WORDS since the sentence start or the last existing break)
    AND enough words follow it that the break does not strand a two-word tail
    (DELIVERY_BREATH_MIN_TAIL_WORDS). Both gates exist because the failure mode of this transform is
    over-punctuation: a comma every few words is not a breath, it is a stutter, and it sounds worse
    than the flat reading it replaced. At most DELIVERY_BREATH_MAX_PER_SENTENCE per sentence for the
    same reason."""
    if _CONJ_RE is None or DELIVERY_BREATH_MAX_PER_SENTENCE <= 0:
        return sentence
    cuts: list[int] = []
    for m in _CONJ_RE.finditer(sentence):
        if len(cuts) >= DELIVERY_BREATH_MAX_PER_SENTENCE:
            break
        # Start of the current run: after the latest existing break, the latest one we inserted, or
        # the sentence start — whichever is closest to this conjunction.
        run_start = max([0, *cuts] + [sentence.rfind(c, 0, m.start()) + 1 for c in _BREAK_CHARS])
        before = sentence[run_start:m.start()]
        if before.strip().endswith(tuple(_BREAK_CHARS)):
            continue                      # a break is already sitting right here
        if len(before.split()) < DELIVERY_BREATH_MIN_WORDS:
            continue
        if len(sentence[m.end():].split()) < DELIVERY_BREATH_MIN_TAIL_WORDS:
            continue
        cuts.append(m.start())
    if not cuts:
        return sentence
    out, prev = [], 0
    for cut in cuts:
        # Land the pause against the preceding word, not floating before the conjunction's space.
        out.append(sentence[prev:cut].rstrip())
        out.append(DELIVERY_PAUSE + " ")
        prev = cut
    out.append(sentence[prev:])
    return "".join(out)


def _pick_opener(text: str) -> str:
    """The discourse marker for `text`, or "" for none.

    Rate-limited on purpose. An opener on EVERY reply is its own tic — worse than none, because it
    turns into the fixed shape it was meant to break up. DELIVERY_OPENER_RATE is the percentage of
    replies that get one, and which replies is decided by the text's CRC, so it is stable per reply
    and spread evenly over a conversation without being random."""
    if not DELIVERY_OPENERS or DELIVERY_OPENER_RATE <= 0:
        return ""
    stripped = text.lstrip()
    if len(stripped.split()) < DELIVERY_OPENER_MIN_WORDS:
        return ""                          # a short answer ("Yes, at 9 AM.") does not want a preamble
    first = stripped.split(maxsplit=1)[0].strip(",.!?").lower()
    if first in DELIVERY_OPENER_SKIP_STARTS:
        return ""                          # already opens conversationally, or is a greeting/apology
    seed = crc32(stripped.encode("utf-8", "replace"))
    if seed % 100 >= DELIVERY_OPENER_RATE:
        return ""
    # A second, independent slice of the same CRC picks WHICH opener, so the choice does not
    # correlate with the reply that happened to clear the rate gate above.
    return DELIVERY_OPENERS[(seed // 100) % len(DELIVERY_OPENERS)]


def shape(text: str) -> str:
    """Return `text` with delivery shaping applied — the one entry point ai/voice_assistant uses.

    Returns the input unchanged when shaping is off or the text is empty, so the caller never has to
    branch. Idempotent in the way that matters: re-shaping already-shaped text inserts no second
    breath (the commas it added now count as existing breaks) and picks the same opener decision only
    for the same string — so do not feed output back in, feed the clean text."""
    if not text or not enabled():
        return text or ""
    shaped = " ".join(_add_breaths(s) for s in _SENTENCE_SPLIT_RE.split(text)).strip()
    opener = _pick_opener(shaped)
    return f"{opener} {shaped}" if opener else shaped


def length_scale(text: str) -> float | None:
    """Piper --length-scale for this one reply: the dashboard's rate nudged by up to
    ±DELIVERY_TEMPO_JITTER, keyed on the text so it is stable per reply. None means "no override",
    which leaves ai/tts._run_piper reading the live setting exactly as it always has — so shaping-off
    is byte-identical to the old behaviour rather than merely equivalent.

    This is the cheapest lever in the module — one CLI argument, no extra synthesis — and it is
    aimed squarely at "uniform pacing": two replies in a row currently come out at exactly the same
    tempo, which is a tell no single-reply improvement can fix. Keep the jitter small; past a few
    percent it stops reading as natural variation and starts reading as a rate bug.

    Clamped to DELIVERY_TEMPO_MIN/MAX — the same bounds as the dashboard's Speaking rate slider — so
    a mis-set jitter can never hand Piper a scale that smears the voice."""
    if not text or not enabled() or DELIVERY_TEMPO_JITTER <= 0:
        return None
    base = float(settings.get("tts_length_scale"))
    # CRC -> [-1, 1], shifted so it does not reuse _pick_opener's low bits: every opened reply also
    # being the slow one would be an audible correlation.
    signed = ((crc32(text.encode("utf-8", "replace")) >> 8) % 2001) / 1000.0 - 1.0
    return max(DELIVERY_TEMPO_MIN,
               min(DELIVERY_TEMPO_MAX, base * (1.0 + DELIVERY_TEMPO_JITTER * signed)))
