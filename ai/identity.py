"""Who is Kai talking to? — pulling a first name out of an ordinary spoken turn.

The prompt is persona + rolling history + the question (see ai/llm.build_chat_messages), and there
is no slot in it for a fact ABOUT the speaker that is not itself a conversational turn. So a name
offered in speech lands in the history as an ordinary user message and is evicted by the rolling cap
MAX_HISTORY_TURNS turns later, after which it is unrecoverable. This module extracts it once so
ai/voice_assistant.py can pin it for the life of the session. See docs/tickets/S12.

Deliberately pure stdlib — re and config.voice, nothing else. Like ai/wake_phrase.py, this is the
part of the feature where all the bugs are (false accepts, casing, offsets), so it must be testable
anywhere, instantly, with plain strings and no models.

Deliberately NOT the LLM's job. Asking Ollama to extract the name is a second round-trip on the
latency path docs/tickets/R5 exists to shorten, for a question a regex answers.

THE ASYMMETRY THAT SHAPES EVERYTHING HERE: a missed name costs nothing — Kai carries on exactly as
it does today. A WRONG name is spoken out loud, repeatedly, to somebody standing in front of the
robot. So every judgement call in this file is made in favour of extracting nothing, and the anchors
are narrow rather than clever.

Two tiers, because the anchors are not equally trustworthy:

  STRONG  the phrase exists to introduce a name and does nothing else: "my name is X", "call me X",
          "ako si X", "pangalan ko ay X". Tagalog "si" is a personal-name marker — introducing a
          name is its grammatical job — so it carries as much weight as the English forms.
  WEAK    "I'm X" / "ako'y X". Enormously more common in speech and almost never about a name:
          "I'm fine", "I'm from Cebu", "I'm a developer", "I'm not sure". These are accepted ONLY
          with corroboration (see _WEAK_REQUIRES_CAPITAL).

Only the FIRST token after the anchor is taken, so "my name is Juan Dela Cruz" yields "Juan". That
is the name Kai should be saying out loud anyway, and it avoids having to decide where a surname
stops.
"""

from __future__ import annotations

import re

from config.voice import (
    IDENTITY_MAX_LEN, IDENTITY_MIN_LEN, IDENTITY_STOPWORDS, IDENTITY_WEAK_ANCHORS_NEED_CAPITAL,
)

# Anchors that exist to introduce a name. Matched case-insensitively against the raw transcript.
# "my name's" and "my name is" are separate alternatives rather than an optional group because
# Whisper renders the contraction inconsistently and a sloppy optional group also matches "my name".
_STRONG_ANCHORS = re.compile(
    r"\b(?:"
    r"my\s+name\s+is|my\s+name's|the\s+name's|"
    r"call\s+me|you\s+can\s+call\s+me|"
    r"pangalan\s+ko\s+(?:ay|si)|ang\s+pangalan\s+ko\s+(?:ay|si)|"
    r"ako\s+(?:po\s+)?si|si\s+ako"
    r")\s+",
    re.IGNORECASE,
)

# Anchors that are usually about anything except a name. Gated by _WEAK_REQUIRES_CAPITAL.
# NOT included: "this is X" (matches "this is nice"), "I go by X" (rare enough not to earn the
# surface), and bare "X here" (matches half of everything).
_WEAK_ANCHORS = re.compile(
    r"\b(?:i'?m|i\s+am|ako'?y)\s+",
    re.IGNORECASE,
)

# The candidate: one run of letters, allowing the apostrophes and hyphens real names carry
# ("N'Golo", "Mary-Jane"). Digits are excluded outright — Whisper writes numbers as digits, and no
# first name it will ever hear contains one.
_CANDIDATE = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", re.UNICODE)


def _looks_like_name(word: str) -> bool:
    """Shape and vocabulary checks that do not depend on which anchor matched."""
    if not (IDENTITY_MIN_LEN <= len(word) <= IDENTITY_MAX_LEN):
        return False
    # Casefold for the stop-list so "Fine" and "fine" are the same word. The stop-list is the part
    # that will need editing after a false accept is heard at an event — it lives in config/voice.py
    # for that reason.
    return word.casefold() not in IDENTITY_STOPWORDS


def extract_name(text: str) -> str | None:
    """Return the first name offered in `text`, or None.

    Returns None far more often than a human would: that is the intended bias (see the module
    docstring). The result is Title-cased, because Whisper's casing of a spoken name is not reliable
    enough to pass through and the string is going into a prompt that will be read out loud.
    """
    if not text:
        return None

    for pattern, needs_capital in ((_STRONG_ANCHORS, False),
                                   (_WEAK_ANCHORS, IDENTITY_WEAK_ANCHORS_NEED_CAPITAL)):
        for anchor in pattern.finditer(text):
            candidate = _CANDIDATE.match(text, anchor.end())
            if candidate is None:
                continue
            word = candidate.group()
            if not _looks_like_name(word):
                continue
            # Corroboration for the weak tier: Whisper capitalises proper nouns fairly reliably
            # mid-sentence, and "I'm Jhondel" differs from "I'm fine" in exactly that way. An
            # all-caps rendering ("I'M JHONDEL") is not evidence of anything, so it does not count.
            if needs_capital and not (word[0].isupper() and not word.isupper()):
                continue
            return _titled(word)
    return None


def _titled(word: str) -> str:
    """Title-case without str.title(), which mangles the punctuation real names carry:
    "o'brien".title() is "O'Brien" but "mary-jane".title() is "Mary-Jane" only by luck — title()
    capitalises after EVERY non-alpha, so "d'angelo" becomes "D'Angelo" and "jo-ann's" becomes
    "Jo-Ann'S". Capitalise the first letter and the letter after each separator, and leave the rest
    of each run alone so "McKenzie" survives being passed through."""
    out, capitalize_next = [], True
    for ch in word:
        if capitalize_next and ch.isalpha():
            out.append(ch.upper())
            capitalize_next = False
        else:
            out.append(ch)
            if ch in "-'’":
                capitalize_next = True
    return "".join(out)
