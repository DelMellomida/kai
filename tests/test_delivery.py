"""ai/delivery.py — the spoken-text delivery shaper.

Everything here is a pure string/float function, so these tests are exact-output assertions rather
than "it did something". That is the point of the module living apart from ai/tts.py: the bugs in a
transform like this are all in the offsets and the thresholds, and they are invisible from listening
to one line.

The shaping toggle is read live from settings, so each test sets it explicitly — a test that
inherits DELIVERY_ENABLED would silently start passing for the wrong reason the day that default
flips.
"""

import unittest
from unittest.mock import patch

import settings
from ai import delivery


# Config constants are module attributes read at call time, so a test pins one with patch.object on
# `delivery`. The one exception is the conjunction list: its regex is compiled at import
# (delivery._CONJ_RE), so changing the list alone would have no effect — patch _CONJ_RE instead.
_DEFAULTS = {"delivery_shaping": True, "tts_length_scale": 1.0}


class _Shaped(unittest.TestCase):
    """Base: shaping ON, settings.get stubbed so no real settings file is touched."""

    ON = True

    def setUp(self):
        self._p = patch.object(
            settings, "get",
            side_effect=lambda name: {**_DEFAULTS, "delivery_shaping": self.ON}[name])
        self._p.start()
        self.addCleanup(self._p.stop)


class TestDisabled(_Shaped):
    ON = False

    def test_shape_is_a_passthrough(self):
        text = "The DEVCON program runs all year but the internships open in March."
        self.assertEqual(delivery.shape(text), text)

    def test_length_scale_declines_to_override(self):
        # None, not the base value: ai/tts._run_piper then reads the live setting itself, which keeps
        # shaping-off byte-identical to the behaviour before this module existed.
        self.assertIsNone(delivery.length_scale("anything at all"))


class TestEmptyInput(_Shaped):
    def test_empty_and_none_survive(self):
        self.assertEqual(delivery.shape(""), "")
        self.assertEqual(delivery.shape(None), "")
        self.assertIsNone(delivery.length_scale(""))


class _NoOpeners(_Shaped):
    """Breath tests with the opener transform off. The two are independent, and leaving openers on
    would make each assertion depend on the CRC of the sample line — it would pass today and start
    failing the day someone reworded a fixture."""

    def setUp(self):
        super().setUp()
        p = patch.object(delivery, "DELIVERY_OPENER_RATE", 0)
        p.start()
        self.addCleanup(p.stop)


class TestBreaths(_NoOpeners):
    def test_inserts_a_pause_before_a_late_conjunction(self):
        # Asserted against DELIVERY_PAUSE, not a literal: which token gives the longest pause is a
        # measured property of the voice (see config/voice.py) and has already changed once.
        out = delivery.shape("The DEVCON program runs all year long but the internships open in March.")
        self.assertEqual(
            out,
            f"The DEVCON program runs all year long{delivery.DELIVERY_PAUSE} but the internships "
            f"open in March.")

    def test_no_pause_when_the_run_is_too_short(self):
        # Only three words before "but" — breathing there is a stutter, not a breath.
        text = "It runs today but the internships open in March."
        self.assertEqual(delivery.shape(text), text)

    def test_no_pause_when_it_would_strand_the_tail(self):
        # Long enough run, but only two words follow the conjunction.
        text = "The DEVCON program runs all year long and then stops."
        self.assertEqual(delivery.shape(text), text)

    def test_does_not_double_up_on_an_existing_comma(self):
        text = ("The DEVCON program runs all year long, but the internships for students "
                "open in March.")
        self.assertEqual(delivery.shape(text), text)

    def test_existing_comma_resets_the_run(self):
        # "which" is only two words past the comma, so it does not earn a second break even though
        # it is far from the start of the sentence.
        text = ("The DEVCON program, a nationwide volunteer network which supports students "
                "across the country, is free.")
        self.assertEqual(delivery.shape(text), text)

    def test_at_most_one_per_sentence(self):
        out = delivery.shape("Kai answers questions about the program but the documents are limited "
                             "so the answers can be short.")
        self.assertEqual(out.count(delivery.DELIVERY_PAUSE), 1)

    def test_each_sentence_is_shaped_independently(self):
        out = delivery.shape(
            "The DEVCON program runs all year long but the internships open in March. "
            "Jumpstart takes applications every single semester so the deadlines move around.")
        self.assertEqual(out.count(delivery.DELIVERY_PAUSE), 2)

    def test_multi_word_conjunction_wins_over_its_first_word(self):
        out = delivery.shape("Kai listens for the wake word first and then records what you asked.")
        self.assertEqual(
            out,
            f"Kai listens for the wake word first{delivery.DELIVERY_PAUSE} and then records "
            f"what you asked.")

    def test_a_conjunction_starting_the_sentence_is_left_alone(self):
        text = "But the internships open in March for students across the country."
        self.assertEqual(delivery.shape(text), text)

    def test_sentence_terminators_survive(self):
        out = delivery.shape("Does the DEVCON program run all year long or does it stop in March? "
                             "Ask me again!")
        self.assertTrue(out.endswith("Ask me again!"))
        self.assertIn("?", out)

    def test_disabled_by_config(self):
        with patch.object(delivery, "DELIVERY_BREATH_MAX_PER_SENTENCE", 0):
            text = "The DEVCON program runs all year long but the internships open in March."
            self.assertEqual(delivery.shape(text), text)


class TestOpener(_Shaped):
    """The opener is CRC-gated, so a test cannot assume any given line gets one. These pin the gates
    that must hold for every line, and use a forced rate for the positive cases."""

    LONG = "The DEVCON program runs all year long and takes volunteers from every chapter here."

    def test_short_replies_never_get_one(self):
        with patch.object(delivery, "DELIVERY_OPENER_RATE", 100):
            self.assertEqual(delivery.shape("Yes at 9 AM."), "Yes at 9 AM.")

    def test_a_reply_that_already_opens_conversationally_is_left_alone(self):
        with patch.object(delivery, "DELIVERY_OPENER_RATE", 100):
            for start in ("Actually", "Sorry", "Yes", "Hello", "Oo"):
                text = f"{start}, the program runs all year long and takes volunteers from chapters."
                self.assertEqual(delivery.shape(text), text, start)

    def test_forced_rate_prepends_a_known_opener(self):
        with patch.object(delivery, "DELIVERY_OPENER_RATE", 100):
            out = delivery.shape(self.LONG)
        self.assertTrue(any(out.startswith(o + " ") for o in delivery.DELIVERY_OPENERS), out)
        self.assertTrue(out.endswith("chapter here."), out)

    def test_rate_zero_never_prepends(self):
        with patch.object(delivery, "DELIVERY_OPENER_RATE", 0):
            self.assertFalse(delivery.shape(self.LONG).startswith(tuple(delivery.DELIVERY_OPENERS)))

    def test_empty_opener_list_is_safe(self):
        with patch.object(delivery, "DELIVERY_OPENERS", ()), \
             patch.object(delivery, "DELIVERY_OPENER_RATE", 100):
            self.assertEqual(delivery.shape("Yes at 9 AM."), "Yes at 9 AM.")

    def test_the_same_reply_always_gets_the_same_treatment(self):
        # Determinism is the contract — CRC, never random(). A canned line must not drift between
        # calls, or between process restarts (which is exactly what hash() would have done).
        self.assertEqual(delivery.shape(self.LONG), delivery.shape(self.LONG))

    def test_the_rate_is_roughly_honoured_across_many_replies(self):
        lines = [f"The DEVCON chapter in city number {i} runs events all year for local students."
                 for i in range(400)]
        opened = sum(1 for t in lines
                     if delivery.shape(t).startswith(tuple(delivery.DELIVERY_OPENERS)))
        # Wide band: this asserts the CRC gate is neither stuck open nor stuck shut, not that a
        # checksum is a uniform RNG.
        self.assertGreater(opened, len(lines) * 0.15)
        self.assertLess(opened, len(lines) * 0.60)

    def test_more_than_one_opener_is_actually_used(self):
        with patch.object(delivery, "DELIVERY_OPENER_RATE", 100):
            picked = {delivery.shape(f"The DEVCON chapter number {i} runs events for local students "
                                     f"across the country.").split(",")[0] for i in range(60)}
        self.assertGreater(len(picked), 1)


class TestLengthScale(_Shaped):
    def test_stays_within_the_jitter_band(self):
        for i in range(200):
            got = delivery.length_scale(f"reply number {i} about the program")
            self.assertGreaterEqual(got, 1.0 * (1 - delivery.DELIVERY_TEMPO_JITTER) - 1e-9)
            self.assertLessEqual(got, 1.0 * (1 + delivery.DELIVERY_TEMPO_JITTER) + 1e-9)

    def test_is_deterministic(self):
        self.assertEqual(delivery.length_scale("hello there"), delivery.length_scale("hello there"))

    def test_differs_between_replies(self):
        got = {delivery.length_scale(f"reply number {i}") for i in range(50)}
        self.assertGreater(len(got), 10)

    def test_zero_jitter_declines_to_override(self):
        with patch.object(delivery, "DELIVERY_TEMPO_JITTER", 0.0):
            self.assertIsNone(delivery.length_scale("hello there"))

    def test_clamped_to_the_slider_bounds(self):
        # A mis-set jitter must never reach Piper as a voice-smearing scale.
        with patch.object(delivery, "DELIVERY_TEMPO_JITTER", 10.0):
            for i in range(50):
                got = delivery.length_scale(f"reply {i}")
                self.assertGreaterEqual(got, delivery.DELIVERY_TEMPO_MIN)
                self.assertLessEqual(got, delivery.DELIVERY_TEMPO_MAX)

    def test_scales_with_the_dashboard_rate(self):
        with patch.object(settings, "get",
                          side_effect=lambda n: {"delivery_shaping": True,
                                                 "tts_length_scale": 1.5}[n]):
            self.assertGreater(delivery.length_scale("hello there"), 1.3)


class TestTagalog(_NoOpeners):
    TL = "Ang programa ng DEVCON ay tumatakbo buong taon para sa mga estudyante sa buong bansa."

    def test_no_breath_is_inserted(self):
        # The conjunction list is English by design; a Tagalog reply matches nothing and passes
        # through, which is the intended degradation rather than a gap.
        self.assertEqual(delivery.shape(self.TL), self.TL)

    def test_an_opener_may_still_be_added_and_that_is_deliberate(self):
        # Documented, not accidental: "So," / "Okay," open Tagalog sentences idiomatically in PH
        # code-switching, so the opener is the one half that is safe to apply cross-language.
        # If this is ever judged wrong by ear, the fix is DELIVERY_OPENER_RATE = 0, not a hack here.
        with patch.object(delivery, "DELIVERY_OPENER_RATE", 100):
            out = delivery.shape(self.TL)
        self.assertTrue(out.endswith(self.TL), out)
        self.assertTrue(any(out.startswith(o + " ") for o in delivery.DELIVERY_OPENERS), out)


class TestNoRunawayPunctuation(_NoOpeners):
    def test_shaping_never_adds_more_than_one_break_per_sentence(self):
        p = delivery.DELIVERY_PAUSE
        replies = [
            "Kai runs entirely on the robot so nothing you say leaves the device at any point.",
            "The documents are indexed locally and then searched when you ask a question about them.",
            "I can answer questions about DEVCON programs but I only know what is in the documents.",
        ]
        for r in replies:
            self.assertLessEqual(delivery.shape(r).count(p) - r.count(p), 1, r)

    def test_never_produces_a_doubled_or_dangling_break(self):
        p = delivery.DELIVERY_PAUSE
        for r in ("Kai answers questions, but the documents are limited so answers can be short.",
                  "It runs all year long, and then it stops, but only for a while in December.",
                  "Kai runs on the robot alone so nothing you say ever leaves it at any point."):
            out = delivery.shape(r)
            for bad in (p + p, " " + p, p + ".", p + ",", "," + p):
                self.assertNotIn(bad, out, f"{bad!r} in {out!r}")


if __name__ == "__main__":
    unittest.main()
