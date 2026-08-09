"""The filler bank: shape, speakability, and the 2 s dead-air ceiling.

Pure data under test -- config/filler.py imports nothing, so these run with no audio device, no
Piper and no clock. What they cannot check is how a line SOUNDS; that needs the synthesise-then-
transcribe pass described in config/filler.py's docstring. These guard the failures that are
mechanical enough to catch in text: a language bank quietly losing a line, a non-ASCII character
sneaking in from a paste, a letter-run that espeak-ng would spell out, and a timing constant
drifting past the ceiling the whole feature exists to hold.
"""

import random
import re
import unittest
import unittest.mock

from ai import filler
from config.filler import (
    FILLER_DEFAULT_LANG, FILLER_DELAY_JITTER_S, FILLER_MAX_SILENCE_S, FILLER_MIN_GAP_S,
    FILLER_OPENERS, FILLER_PLAYBACK_START_BUDGET_S, FILLER_STALL_GAP_JITTER_S, FILLER_STALLS,
)
from config.thinking import THINKING_SOUND_DELAY_S

# The distribution the bank was commissioned with: 60% Tagalog, 20% Bisaya, 20% English, in
# both tiers. Asserted rather than derived, so dropping a line is a test failure and not a
# silently thinner bank.
EXPECTED_COUNTS = {"tl": 12, "ceb": 4, "en": 4}

BANKS = (("openers", FILLER_OPENERS), ("stalls", FILLER_STALLS))


def every_line():
    for tier, bank in BANKS:
        for lang, lines in bank.items():
            for line in lines:
                yield tier, lang, line


class TestBankShape(unittest.TestCase):
    def test_both_tiers_cover_the_same_languages(self):
        # A turn picks its opener and its stalls from the same language key; a key present in one
        # tier and missing from the other would strand a turn mid-way with nothing to say.
        self.assertEqual(set(FILLER_OPENERS), set(FILLER_STALLS))

    def test_counts_match_the_commissioned_split(self):
        for tier, bank in BANKS:
            with self.subTest(tier=tier):
                self.assertEqual({k: len(v) for k, v in bank.items()}, EXPECTED_COUNTS)

    def test_default_language_exists_in_both_tiers(self):
        # The opener usually fires BEFORE transcription finishes, so the fallback key is the one
        # actually used most of the time, not an edge case.
        for tier, bank in BANKS:
            with self.subTest(tier=tier):
                self.assertIn(FILLER_DEFAULT_LANG, bank)

    def test_no_line_is_duplicated(self):
        # Within a tier: a duplicate halves the perceived variety without changing the count.
        for tier, bank in BANKS:
            lines = [ln for lines in bank.values() for ln in lines]
            with self.subTest(tier=tier):
                self.assertEqual(len(lines), len(set(lines)))


class TestSpeakability(unittest.TestCase):
    """The espeak-ng hazards documented in config/thinking.py:72-86 and config/filler.py."""

    def test_ascii_only(self):
        for tier, lang, line in every_line():
            with self.subTest(tier=tier, lang=lang, line=line):
                self.assertTrue(line.isascii(), "non-ASCII punctuation reaches the phonemizer")

    def test_no_ellipses_or_dashes(self):
        # "..." and "--" are the two that survive an ASCII check and still change phonemization.
        for tier, lang, line in every_line():
            with self.subTest(tier=tier, lang=lang, line=line):
                self.assertNotIn("...", line)
                self.assertNotIn("--", line)

    def test_no_long_repeated_letter_runs(self):
        # "Hmmmm..." came back from Whisper as "H-A-M-A-M-M": espeak-ng reads a long run as an
        # initialism and spells it out. Three of the same letter is already past the safe point.
        for tier, lang, line in every_line():
            with self.subTest(tier=tier, lang=lang, line=line):
                self.assertIsNone(re.search(r"([A-Za-z])\1{2,}", line))

    def test_no_dotted_brand_names(self):
        # Written the way it should be SPOKEN. "NMBLR.AI" phonemizes as a sentence break plus an
        # initialism; "NMBLR dot AI" is what a person actually says.
        for tier, lang, line in every_line():
            with self.subTest(tier=tier, lang=lang, line=line):
                self.assertIsNone(re.search(r"[A-Za-z]\.[A-Za-z]", line))

    def test_openers_are_one_or_two_sentences(self):
        for lang, lines in FILLER_OPENERS.items():
            for line in lines:
                with self.subTest(lang=lang, line=line):
                    self.assertIn(len(re.findall(r"[.!?]", line)), (1, 2))

    def test_stalls_stay_short_enough_to_interrupt(self):
        # A stall gets cut off the moment the real answer lands, so it has to be short enough
        # that being cut off reads as natural. Word count is the cheap proxy for the ~1.2 s
        # spoken budget; the real number comes from the Piper measurement pass.
        for lang, lines in FILLER_STALLS.items():
            for line in lines:
                with self.subTest(lang=lang, line=line):
                    self.assertLessEqual(len(line.split()), 6)


class TestSilenceCeiling(unittest.TestCase):
    """The one number the whole feature is accountable to."""

    def test_jitter_ranges_are_ordered_and_positive(self):
        for name, (lo, hi) in (("delay", FILLER_DELAY_JITTER_S),
                               ("stall gap", FILLER_STALL_GAP_JITTER_S)):
            with self.subTest(name=name):
                self.assertGreater(lo, 0.0)
                self.assertLess(lo, hi)

    def test_worst_case_draw_plus_playback_start_stays_under_the_ceiling(self):
        # The upper bound of each range is the worst a single draw can be. Adding the playback
        # start reservation is what makes this a real bound on AUDIBLE silence rather than on the
        # timer alone -- that gap is where a ceiling like this normally leaks.
        for name, (_lo, hi) in (("delay", FILLER_DELAY_JITTER_S),
                                ("stall gap", FILLER_STALL_GAP_JITTER_S)):
            with self.subTest(name=name):
                self.assertLess(hi + FILLER_PLAYBACK_START_BUDGET_S, FILLER_MAX_SILENCE_S)

    def test_playback_budget_leaves_room_to_grow(self):
        # If the reservation ever eats most of the ceiling, the drawn delay gets clamped to
        # nothing and the opener fires on every fast reply. Catch that here, not on stage.
        self.assertLess(FILLER_PLAYBACK_START_BUDGET_S, FILLER_MAX_SILENCE_S / 2)


class TestGapFloor(unittest.TestCase):
    """The counterweight to the ceiling: at least FILLER_MIN_GAP_S of quiet before every line.

    The ceiling on its own pushes every gap toward zero, since the safest way to never be quiet
    too long is to never be quiet. These pin the other side, so filler reads as thinking rather
    than as a queue draining."""

    def test_every_drawn_gap_starts_at_or_above_the_floor(self):
        for name, (lo, _hi) in (("delay", FILLER_DELAY_JITTER_S),
                                ("stall gap", FILLER_STALL_GAP_JITTER_S)):
            with self.subTest(name=name):
                self.assertGreaterEqual(lo, FILLER_MIN_GAP_S)

    def test_the_floor_and_the_ceiling_leave_room_between_them(self):
        # session._filler_gap resolves a conflict in the ceiling's favour, so a floor raised past
        # it would not break the dead-air promise -- it would silently pin every gap to one
        # constant instead, and the jitter that keeps filler off a metronome would stop existing.
        self.assertLess(FILLER_MIN_GAP_S, FILLER_MAX_SILENCE_S - FILLER_PLAYBACK_START_BUDGET_S)

    def test_the_floor_clears_the_hmm_so_the_fallback_still_gets_its_tick(self):
        # The "Hmm" fires at THINKING_SOUND_DELAY_S on a tick where the bank had nothing to say.
        # With the opener drawn under that, a cold bank could consume the turn's only chance at it.
        self.assertGreater(FILLER_DELAY_JITTER_S[0], THINKING_SOUND_DELAY_S)


class TestKeys(unittest.TestCase):
    """The key namespace, which is shared with session._canned_lines() and therefore with the WAV
    filenames on disk. A collision here would make two different lines share one WAV."""

    def test_every_line_has_a_unique_key(self):
        lines = filler.canned_lines()
        self.assertEqual(len(lines), sum(EXPECTED_COUNTS.values()) * 2)

    def test_keys_do_not_collide_with_the_core_canned_names(self):
        # "ack", "no_speech", "error", "thinking" live in the same dict and the same directory.
        for key in filler.canned_lines():
            with self.subTest(key=key):
                self.assertTrue(key.startswith("filler_"))

    def test_text_for_round_trips_every_key(self):
        for key, text in filler.canned_lines().items():
            with self.subTest(key=key):
                self.assertEqual(filler.text_for(key), text)

    def test_text_for_refuses_keys_that_are_not_ours(self):
        # _speak_filler passes this straight to TTS on a cache miss. A stray key must produce ""
        # and therefore silence, never a key name spoken aloud.
        for key in ("ack", "thinking", "", "filler_op_zz_0", "filler_op_tl_99", "filler_op_tl_x"):
            with self.subTest(key=key):
                self.assertEqual(filler.text_for(key), "")


class TestSelection(unittest.TestCase):
    """Choosing, with the randomness injected so these are deterministic."""

    def test_opener_never_repeats_back_to_back(self):
        rng = random.Random(7)
        last = ""
        for _ in range(200):
            key = filler.pick_opener("tl", rng, avoid=last)
            self.assertNotEqual(key, last)
            last = key

    def test_opener_still_returns_something_when_the_bank_has_one_line(self):
        # avoid= would exclude the only candidate. Repeating beats going silent.
        rng = random.Random(1)
        with unittest.mock.patch.dict(filler.FILLER_OPENERS, {"xx": ["only"]}, clear=False):
            key = filler.pick_opener("xx", rng, avoid="filler_op_xx_0")
            self.assertEqual(key, "filler_op_xx_0")

    def test_opener_is_empty_for_an_unknown_language(self):
        # Signals "nothing to say", which is what makes the session fall back to the old "Hmm".
        self.assertEqual(filler.pick_opener("zz", random.Random(1)), "")

    def test_opener_draws_the_whole_bank_eventually(self):
        rng = random.Random(3)
        seen = {filler.pick_opener("tl", rng) for _ in range(500)}
        self.assertEqual(len(seen), EXPECTED_COUNTS["tl"])

    def test_stall_queue_holds_every_line_exactly_once(self):
        # The reason it is a shuffled queue and not an independent draw per gap: this guarantees
        # all 12 are heard before any is heard twice.
        for lang, n in EXPECTED_COUNTS.items():
            with self.subTest(lang=lang):
                q = filler.stall_queue(lang, random.Random(5))
                self.assertEqual(sorted(q), sorted(f"filler_st_{lang}_{i}" for i in range(n)))

    def test_stall_queue_order_varies(self):
        orders = {tuple(filler.stall_queue("tl", random.Random(s))) for s in range(20)}
        self.assertGreater(len(orders), 1, "a fixed order is a metronome")

    def test_stall_queue_is_empty_for_an_unknown_language(self):
        self.assertEqual(filler.stall_queue("zz", random.Random(1)), [])

    def test_english_never_becomes_bisaya(self):
        rng = random.Random(11)
        self.assertEqual({filler.pick_lang("en", rng) for _ in range(500)}, {"en"})

    def test_unknown_detection_falls_back_to_the_default(self):
        # The common case, not an edge one: the opener fires before STT has necessarily finished,
        # so last_language() is "" on the very first turn after boot.
        rng = random.Random(13)
        got = {filler.pick_lang(d, rng) for d in ("", "de", "zz") for _ in range(50)}
        self.assertTrue(got <= {FILLER_DEFAULT_LANG, "ceb"})

    def test_used_openers_are_skipped(self):
        rng = random.Random(23)
        used = {f"filler_op_tl_{i}" for i in range(EXPECTED_COUNTS["tl"] - 1)}
        for _ in range(50):
            self.assertEqual(filler.pick_opener("tl", rng, used=used),
                             f"filler_op_tl_{EXPECTED_COUNTS['tl'] - 1}")

    def test_used_stalls_are_skipped(self):
        rng = random.Random(29)
        used = {f"filler_st_tl_{i}" for i in range(4)}
        q = filler.stall_queue("tl", rng, used=used)
        self.assertEqual(set(q) & used, set())
        self.assertEqual(len(q), EXPECTED_COUNTS["tl"] - 4)

    def test_an_exhausted_bank_starts_a_second_lap_rather_than_going_quiet(self):
        # `used` is a preference, not a promise. A conversation can outlast 12 lines, and refusing
        # to repeat would trade a small tell for the exact dead air the module exists to prevent.
        rng = random.Random(31)
        every_opener = {f"filler_op_tl_{i}" for i in range(EXPECTED_COUNTS["tl"])}
        every_stall = {f"filler_st_tl_{i}" for i in range(EXPECTED_COUNTS["tl"])}
        self.assertIn(filler.pick_opener("tl", rng, used=every_opener), every_opener)
        self.assertEqual(sorted(filler.stall_queue("tl", rng, used=every_stall)),
                         sorted(every_stall))

    def test_an_uncached_line_is_never_selected_even_when_everything_is_used(self):
        # The fallback relaxes `used`, which is soft. It must NOT relax `have`, which is hard: an
        # uncached key plays nothing, so selecting one puts a gap where the ceiling forbids it.
        rng = random.Random(37)
        have = {"filler_op_tl_0", "filler_st_tl_0"}
        every = {f"filler_op_tl_{i}" for i in range(EXPECTED_COUNTS["tl"])}
        every |= {f"filler_st_tl_{i}" for i in range(EXPECTED_COUNTS["tl"])}
        for _ in range(50):
            self.assertEqual(filler.pick_opener("tl", rng, have=have, used=every), "filler_op_tl_0")
        self.assertEqual(filler.stall_queue("tl", rng, have=have, used=every), ["filler_st_tl_0"])

    def test_tagalog_reaches_the_bisaya_bank(self):
        # Whisper has no "ceb" label (config/voice.WHISPER_LANGUAGES is ("en", "tl")), so without
        # this route the 8 Bisaya lines would be synthesised at startup and never played.
        rng = random.Random(17)
        got = [filler.pick_lang("tl", rng) for _ in range(400)]
        self.assertIn("ceb", got)
        self.assertIn("tl", got)
        self.assertGreater(got.count("tl"), got.count("ceb"), "Tagalog must stay the majority")


if __name__ == "__main__":
    unittest.main()
