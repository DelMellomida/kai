"""Choosing which filler line to play, and when.

config/filler.py holds the words; this holds the decisions. Kept separate and PURE — no audio,
no clock, no session state, every random draw taken from an injected Random — because the
interesting failures here are statistical (the same opener twice, a stall bank that repeats
after two draws, a language that can never be reached) and those are only cheap to test if the
choosing is separable from the speaking. Same reason config/thinking.py's sweep shape is drawn
by the caller: see tests/test_thinking.py.

ai/session.py is the only consumer.

The keys produced here are the SAME namespace as the canned lines in session._canned_lines(),
which is what lets the whole bank ride the existing prewarm path (tts.prewarm_canned takes
{key: text} and writes one WAV per key) without changing that contract at all.
"""

import random

from config.filler import (
    FILLER_CEB_SHARE, FILLER_DEFAULT_LANG, FILLER_OPENERS, FILLER_STALLS,
)

OPENER_PREFIX = "filler_op"
STALL_PREFIX = "filler_st"


def _keys(bank: dict[str, list[str]], prefix: str) -> dict[str, str]:
    return {f"{prefix}_{lang}_{i}": text
            for lang, lines in bank.items()
            for i, text in enumerate(lines)}


def canned_lines() -> dict[str, str]:
    """The whole bank as {key: text}, ready to hand to tts.prewarm_canned.

    Deterministic ordering (dict insertion order is stable here because both banks are literals),
    so a key means the same line across restarts and a stale WAV from a previous run is never
    mistaken for a different line."""
    return {**_keys(FILLER_OPENERS, OPENER_PREFIX), **_keys(FILLER_STALLS, STALL_PREFIX)}


def warm_counts(warm: set[str]) -> dict[str, tuple[int, int]]:
    """{lang: (openers cached, stalls cached)} for the given set of warm keys.

    Exists because the bank total that _prewarm_bank prints is the wrong number to judge the
    feature by. A turn draws from ONE language's stalls, so what decides whether a line repeats is
    that language's pool alone — and "41/44 cached" reads as healthy while hiding an English stall
    pool of two, which repeats inside a single wait. The length cap silently drops long lines
    (session._within_length_cap), so a pool can shrink without anyone editing this file: the
    per-language number has to be printed, not inferred."""
    return {lang: (sum(1 for i in range(len(FILLER_OPENERS[lang]))
                       if f"{OPENER_PREFIX}_{lang}_{i}" in warm),
                   sum(1 for i in range(len(FILLER_STALLS.get(lang, ())))
                       if f"{STALL_PREFIX}_{lang}_{i}" in warm))
            for lang in FILLER_OPENERS}


def pick_lang(detected: str, rng: random.Random) -> str:
    """Which language bank to draw this turn from, given what Whisper labelled the utterance.

    Whisper is constrained to config/voice.WHISPER_LANGUAGES, which is ("en", "tl") — there is no
    "ceb" label, so detection can NEVER select the Bisaya bank on its own. Rather than leave 8 of
    the 52 lines permanently dead, a Tagalog turn draws from the Bisaya bank FILLER_CEB_SHARE of
    the time. That is a deliberate product choice, not an inference about the speaker: the room is
    Philippine, Bisaya reads as playful rather than wrong to a Tagalog speaker, and the two are
    adjacent enough that a filler line is a low-stakes place to mix them. Set FILLER_CEB_SHARE to
    0.0 to switch it off; English turns are never affected.
    """
    lang = detected if detected in FILLER_OPENERS else FILLER_DEFAULT_LANG
    if lang == "tl" and "ceb" in FILLER_OPENERS and rng.random() < FILLER_CEB_SHARE:
        return "ceb"
    return lang


def _available(keys: list[str], have: set[str] | None = None,
               spent: tuple[set[str] | None, ...] = ()) -> list[str]:
    """Warm keys, preferring ones the caller has not spent yet.

    Two kinds of filter with very different standing, which is why they are not one argument:

    `have` is HARD. An uncached key is a silent one (filler is never synthesised live — see
    session._speak_filler), so selecting one would spend a slot and play nothing, leaving a gap
    exactly where FILLER_MAX_SILENCE_S says there must not be one.

    `spent` is SOFT, and is a LADDER of sets from strongest preference to weakest, each tried only
    when the one before it has nothing left. Not repeating is what keeps canned audio from
    announcing itself, but it can never be a promise: a conversation can outlast the bank, and
    going silent because everything has been heard once would trade a small tell for the exact dead
    air the module exists to prevent.

    The ladder exists because a single set collapses too early. The stall caller passes
    (conversation-spent, turn-spent): once a conversation has been through the bank the first rung
    is empty on EVERY later turn, and with one set that meant the whole bank came straight back —
    including the line that just played. The second rung keeps the weaker promise that still
    matters, nothing twice inside ONE turn, instead of dropping to no promise at all. A rung that
    is None or empty means nothing is spent at that level, so everything below it is moot and the
    full list is already the answer."""
    if have is not None:
        keys = [k for k in keys if k in have]
    for used in spent:
        if not used:
            return keys
        fresh = [k for k in keys if k not in used]
        if fresh:
            return fresh
    return keys


def pick_opener(lang: str, rng: random.Random, avoid: str = "",
                have: set[str] | None = None, used: set[str] | None = None) -> str:
    """Key of the opener to play, never `avoid` (the one used last turn) unless it is the only one.

    Back-to-back repeats are the single most noticeable tell that audio is canned — far more than
    a repeat two or three turns apart — so that one case is excluded explicitly rather than left
    to a uniform draw that hits it 1-in-12. `used` makes the stronger promise for the ordinary
    case (nothing twice in one conversation); `avoid` is what still holds at the seam between two
    conversations, where `used` has just been cleared.

    Returns "" when nothing is warm, which is what lets the session fall back to the old "Hmm"."""
    keys = _available([f"{OPENER_PREFIX}_{lang}_{i}"
                       for i in range(len(FILLER_OPENERS.get(lang, ())))], have, (used,))
    if not keys:
        return ""
    fresh = [k for k in keys if k != avoid] or keys
    return rng.choice(fresh)


def stall_queue(lang: str, rng: random.Random, have: set[str] | None = None,
                used: set[str] | None = None, turn_used: set[str] | None = None,
                avoid: str = "") -> list[str]:
    """The turn's stalls, shuffled, to be consumed FROM THE END and refilled when exhausted.

    A shuffle rather than an independent draw per gap: independent draws repeat far more often
    than they feel like they should (a 1-in-12 draw hits the same line within three tries about a
    quarter of the time), and a repeat inside one wait is exactly when the ear is listening
    hardest. Consuming a shuffled queue guarantees the whole bank before any line is heard twice.

    `used` (conversation) and `turn_used` (this turn) are the soft ladder — see _available. The
    queue is rebuilt whenever it empties, so these are what carry the no-repeat promise ACROSS
    rebuilds: without them "Sandali ha" could open the stalls of three turns in a row, since each
    shuffle knows nothing about the last one's.

    `avoid` is the line that just played, and it guards the seam a shuffle cannot. When a lap ends
    and the ladder bottoms out, the full bank comes back — and the shuffle is free to put the
    line that just finished at the end of the list, which is the next one popped. That is the
    back-to-back repeat heard on the robot 2026-08-09: the small banks (ceb and en hold a handful
    of stalls each) lap inside a single wait, so it landed within one exchange rather than
    "eventually". Same case pick_opener has always guarded; stalls simply never had it.

    Rotated to the front rather than dropped, because dropping it would cost the lap a line for
    the sake of the gap it is least likely to be heard in. With one key there is nothing to
    rotate and the repeat stands: repeating beats going silent, exactly as in pick_opener."""
    keys = _available([f"{STALL_PREFIX}_{lang}_{i}"
                       for i in range(len(FILLER_STALLS.get(lang, ())))],
                      have, (used, turn_used))
    rng.shuffle(keys)
    if avoid and len(keys) > 1 and keys[-1] == avoid:
        keys.insert(0, keys.pop())
    return keys


def text_for(key: str) -> str:
    """The words behind a key, for the live-synthesis fallback when a WAV is missing. "" if the
    key is not ours — the caller passes it straight to TTS, and speaking a stray key aloud would
    be worse than staying silent."""
    bank = (FILLER_OPENERS if key.startswith(OPENER_PREFIX + "_")
            else FILLER_STALLS if key.startswith(STALL_PREFIX + "_") else None)
    if bank is None:
        return ""
    try:
        _prefix, lang, index = key.rsplit("_", 2)
        return bank[lang][int(index)]
    except (KeyError, ValueError, IndexError):
        return ""
