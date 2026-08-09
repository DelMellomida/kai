"""Did somebody just say "Hey Kai"? — fuzzy wake-phrase matching over a Whisper transcript.

Only the whisper wake tier uses this (see ai/audio.py, ai/session.py); the Porcupine and
openWakeWord tiers spot the phrase acoustically and never produce text.

Deliberately pure stdlib — difflib, re, unicodedata, and config.wake for the thresholds. No numpy,
no audio stack, no whisper. This is the part of the feature with all the bugs in it (false accepts,
unicode, offset arithmetic), so it must be testable anywhere, instantly, with plain strings.

Why fuzzy at all: faster-whisper renders the same two words as "Hey, Kai.", "Hey Ky", "hey chi",
"Hi Kai", "Hey. Kai." — and with WHISPER_LANGUAGE=None it sometimes decodes Tagalog. Exact matching
is useless. But `"kai" in text` is *worse* than useless: it fires on "kaya", "okay", "kayo", and on
anyone mentioning Kai in the third person. So the rule is narrower than either: a PREFIX word
followed by a NAME word, beginning near the START of the utterance.

The name slot is also where this tier loses real wakes — "tiny" mangles "Kai" in ways no alias list
finishes catching. So there is a third, deliberately narrow form: a bare prefix ("hey") at token 0,
matched near-exactly. See WAKE_PHRASE_SOLO_* in config/wake.py for the trade and how to turn it off.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import NamedTuple

from config.wake import (
    WAKE_PHRASE_BLOCKLIST, WAKE_PHRASE_JOINED_RATIO, WAKE_PHRASE_NAME_RATIO, WAKE_PHRASE_NAMES,
    WAKE_PHRASE_PREFIX_BLOCKLIST, WAKE_PHRASE_PREFIX_RATIO, WAKE_PHRASE_PREFIXES,
    WAKE_PHRASE_SCAN_TOKENS, WAKE_PHRASE_SOLO_PREFIXES, WAKE_PHRASE_SOLO_SCAN_TOKENS,
    WAKE_WHISPER_MAX_WORDS,
)

# Word characters, excluding underscore. Unicode-aware so accented renderings ("Héy Kaí") tokenize
# as single words rather than fragmenting.
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Leading punctuation to strip off the extracted command. Whisper loves to emit "Hey Kai, ..." and
# "Hey Kai. ..." and the comma/period must not reach the LLM.
_LEAD_PUNCT = " ,.!?;:—–-…\"'“”‘’"

# Every prefix+name concatenation, for one-token renderings like "heykai". Built once.
_JOINED = tuple(p + n for p in WAKE_PHRASE_PREFIXES for n in WAKE_PHRASE_NAMES)

# The name is a short word. Rejecting anything far from that length is what stops "google" from
# scoring as "kai" purely on shared letters.
_NAME_LEN = len("kai")
# Slack of 1, not 2: at 2 the name slot accepts 5-letter words, and "kayla" scores 0.75 against
# "kay" — so "hey Kayla" woke the robot. Every real rendering of the name is 2-4 characters.
_NAME_LEN_SLACK = 1

# Form B (the two words run together) needs a length floor. Every real joined form is at least 5
# characters ("okkai", "heykai"), while short common words score alarmingly high against them:
# "okay" is 0.89 against "okkay". Without this floor, "Okay Google, play music" wakes the robot.
_JOINED_MIN_LEN = 5


class WakeMatch(NamedTuple):
    """A confirmed wake phrase, and whatever the speaker said after it.

    `command` is sliced out of the ORIGINAL text, so casing, punctuation, digits and accents survive
    intact — it is fed straight to the LLM as a turn. Empty when the phrase was the whole utterance.
    """
    score: float     # 0..1 — the weaker of the two token ratios, i.e. the confidence floor
    phrase: str      # the raw matched text, e.g. "Hey, Kai."
    command: str     # the remainder, e.g. "what time is it?"


class Token(NamedTuple):
    text: str        # normalized (casefolded, accents stripped)
    start: int       # offset into the ORIGINAL string
    end: int


def _strip_accents(text: str) -> str:
    """Fold accents away so "Kaí" matches "kai". Applied per token, never to the whole string —
    NFKD is not length-preserving, so decomposing the input would invalidate every offset."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def normalize_tokens(text: str) -> list[Token]:
    """Split `text` into normalized word tokens that remember where they came from.

    The offsets are the whole point: they let the caller slice the command out of the original
    string. Rebuilding it from the tokens instead (`" ".join(tokens[i:])`) would hand the LLM
    lowercased, punctuation-stripped, accent-stripped text — and silently mangle anything numeric.
    """
    if not text:
        return []
    out: list[Token] = []
    for m in _TOKEN_RE.finditer(text):
        norm = _strip_accents(m.group()).casefold()
        if norm:
            out.append(Token(norm, m.start(), m.end()))
    return out


def _best_ratio(token: str, candidates) -> float:
    """Closeness of `token` to its nearest candidate, 0..1. Exact membership short-circuits to 1.0
    (both cheaper and exact — difflib on identical strings is wasted work)."""
    if token in candidates:
        return 1.0
    best = 0.0
    for cand in candidates:
        ratio = SequenceMatcher(None, token, cand).ratio()
        if ratio > best:
            best = ratio
            if best == 1.0:
                break
    return best


def _build(text: str, tokens: list[Token], first: int, last: int, score: float) -> WakeMatch:
    """Assemble the result, slicing the command out of the original text after the matched span."""
    phrase = text[tokens[first].start:tokens[last].end]
    command = text[tokens[last].end:].lstrip(_LEAD_PUNCT).strip()
    return WakeMatch(score=score, phrase=phrase, command=command)


def _collapse_runs(token: str) -> str:
    """"heyy" -> "hey", "heeeey" -> "hey". Drawn-out vowels are the one spelling variation a bare
    "hey" actually needs to absorb, and collapsing them is exact where a fuzzy ratio is not."""
    out = []
    for ch in token:
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)


def _match_solo_prefix(text: str, tokens: list[Token]) -> WakeMatch | None:
    """Form C: a bare prefix ("hey") with no name after it.

    Only reached once forms A and B have both failed across the whole scan window, which is what
    keeps the name out of the command: for "Hey Kai, what time is it?" form A wins at token 0 and
    returns command="what time is it?". If this ran first it would match "Hey" and hand the LLM
    "Kai, what time is it?" instead.

    Exact membership, not _best_ratio — see WAKE_PHRASE_SOLO_PREFIXES in config/wake.py. "they"
    and "heyy" score identically against "hey" (0.857 both), so no threshold can accept the real
    wake and reject the ordinary word. Score is therefore always 1.0.
    """
    scan = min(WAKE_PHRASE_SOLO_SCAN_TOKENS, len(tokens))
    for i in range(scan):
        # No blocklist consulted here, unlike forms A and B: the solo list is a handful of literals
        # chosen by hand rather than a fuzzy neighbourhood, so there is nothing to subtract from it.
        if _collapse_runs(tokens[i].text) in WAKE_PHRASE_SOLO_PREFIXES:
            return _build(text, tokens, i, i, 1.0)
    return None


def match_wake_phrase(text: str) -> WakeMatch | None:
    """Find "hey kai" (however Whisper spelled it) near the start of `text`. None if it isn't there.

    Three accepted forms:
      A. two tokens — a prefix ("hey"/"hi"/"hoy"/...) then a name ("kai"/"ky"/"chi"/...)
      B. one token — the two run together, "heykai"
      C. one token — a bare prefix ("hey"), no name at all

    A and B must begin within WAKE_PHRASE_SCAN_TOKENS of the start; C within the much tighter
    WAKE_PHRASE_SOLO_SCAN_TOKENS. That constraint is what makes the matcher safe on conversational
    speech: "sabihin mo kay Kai na..." and "...and then Kai said..." put the candidate too late to
    fire, and a mid-sentence "hey" is not at token 0.

    C is tried LAST, after A and B have been given every index in their window — see
    _match_solo_prefix for why the order is load-bearing rather than stylistic.
    """
    tokens = normalize_tokens(text)
    if not tokens or len(tokens) > WAKE_WHISPER_MAX_WORDS:
        return None

    scan = min(WAKE_PHRASE_SCAN_TOKENS, len(tokens))
    for i in range(scan):
        # Form A: prefix + name.
        if i + 1 < len(tokens):
            prefix, name = tokens[i].text, tokens[i + 1].text
            # Both blocklists are consulted BEFORE any ratio. Several Tagalog words score close
            # enough to pass their threshold and are far too common to risk: "kaya"/"kayo" in the
            # name slot, and "kay" in the prefix slot ("sabihin mo kay Kai" = talking *about* Kai).
            # `name not in PREFIXES` matters more than it looks: "okay" is 0.86 against "kay" and
            # "hi" is 0.80 against "chi", so without it "okay okay i get it" and "hi hi hi" both fire.
            if (name not in WAKE_PHRASE_BLOCKLIST
                    and name not in WAKE_PHRASE_PREFIXES
                    and prefix not in WAKE_PHRASE_PREFIX_BLOCKLIST
                    and abs(len(name) - _NAME_LEN) <= _NAME_LEN_SLACK):
                pr = _best_ratio(prefix, WAKE_PHRASE_PREFIXES)
                if pr >= WAKE_PHRASE_PREFIX_RATIO:
                    nr = _best_ratio(name, WAKE_PHRASE_NAMES)
                    if nr >= WAKE_PHRASE_NAME_RATIO:
                        return _build(text, tokens, i, i + 1, min(pr, nr))

        # Form B: joined into one token. Guarded by a length floor and by rejecting bare prefix
        # words — on its own, "okay" is 0.89 against "okkay" and would fire.
        token = tokens[i].text
        if (len(token) >= _JOINED_MIN_LEN
                and token not in WAKE_PHRASE_BLOCKLIST
                and token not in WAKE_PHRASE_PREFIXES):
            jr = _best_ratio(token, _JOINED)
            if jr >= WAKE_PHRASE_JOINED_RATIO:
                return _build(text, tokens, i, i, jr)

    return _match_solo_prefix(text, tokens)
