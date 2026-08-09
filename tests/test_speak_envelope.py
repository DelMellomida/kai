"""The jaw open/close schedule.

Moved out of tests/test_voice_assistant.py with ai/speak_envelope.py. Text and timestamps in, a
schedule out — no servo, no audio, no clock.
"""

import unittest

from ai.speak_envelope import (
    _speak_segments, _speak_segments_for_duration, _split_sentences, speaking_openness_at,
)
from config.voice import (
    SPEAK_AMP, SPEAK_GAP_S, SPEAK_MAX_S, SPEAK_MIN_SENTENCE_S,
)


class TestSplitSentences(unittest.TestCase):
    def test_splits_on_terminal_punctuation(self):
        self.assertEqual(_split_sentences("Hi there. How are you?"), ["Hi there.", "How are you?"])

    def test_no_punctuation_is_one_sentence(self):
        self.assertEqual(_split_sentences("didn't catch that"), ["didn't catch that"])

    def test_empty_returns_empty(self):
        self.assertEqual(_split_sentences("   "), [])


class TestSpeakSegments(unittest.TestCase):
    def test_one_segment_per_sentence(self):
        _, segs = _speak_segments("First one. Second one here.", 0.0)
        self.assertEqual(len(segs), 2)

    def test_short_sentence_hits_min_floor(self):
        _, segs = _speak_segments("Hi.", 0.0)
        self.assertAlmostEqual(segs[0][1] - segs[0][0], SPEAK_MIN_SENTENCE_S)

    def test_gap_between_sentences(self):
        _, segs = _speak_segments("One two three. Four five six.", 0.0)
        gap = segs[1][0] - segs[0][1]
        self.assertAlmostEqual(gap, SPEAK_GAP_S)

    def test_longer_sentence_stays_open_longer(self):
        _, short = _speak_segments("Hi there now.", 0.0)
        _, long  = _speak_segments("Hi there now this is a much longer sentence indeed.", 0.0)
        self.assertLess(short[0][1] - short[0][0], long[0][1] - long[0][0])

    def test_runaway_reply_capped_at_max(self):
        text = ". ".join(["word " * 5 for _ in range(100)])
        _, segs = _speak_segments(text, 0.0)
        self.assertLessEqual(segs[-1][1], SPEAK_MAX_S)

    def test_empty_text_still_produces_a_segment(self):
        _, segs = _speak_segments("", 0.0)
        self.assertEqual(len(segs), 1)


class TestSpeakingOpennessAt(unittest.TestCase):
    def test_none_when_no_schedule(self):
        self.assertIsNone(speaking_openness_at(5.0, None, ()))
        self.assertIsNone(speaking_openness_at(5.0, 0.0, ()))

    def test_none_before_start(self):
        _, segs = _speak_segments("Hello there friend.", 10.0)
        self.assertIsNone(speaking_openness_at(9.0, 10.0, segs))

    def test_none_after_last_sentence(self):
        _, segs = _speak_segments("Hello there friend.", 0.0)
        end = segs[-1][1]
        self.assertIsNone(speaking_openness_at(end, 0.0, segs))
        self.assertIsNone(speaking_openness_at(end + 1.0, 0.0, segs))

    def test_openness_within_bounds(self):
        _, segs = _speak_segments("Hello there friend. Nice to meet you.", 0.0)
        t = 0.0
        while t < segs[-1][1]:
            o = speaking_openness_at(t, 0.0, segs)
            self.assertIsNotNone(o)
            self.assertGreaterEqual(o, 0.0)
            self.assertLessEqual(o, SPEAK_AMP + 1e-9)
            t += 0.02

    def test_opens_at_start_holds_then_closes(self):
        # A single long sentence: closed-ish at the very edges, wide open in the middle.
        _, segs = _speak_segments("This is one nice long spoken sentence for testing.", 0.0)
        s0, s1 = segs[0]
        mid   = speaking_openness_at((s0 + s1) / 2.0, 0.0, segs)
        start = speaking_openness_at(s0 + 0.001, 0.0, segs)
        end   = speaking_openness_at(s1 - 0.001, 0.0, segs)
        self.assertAlmostEqual(mid, SPEAK_AMP)   # held fully open through the middle
        self.assertLess(start, mid * 0.5)         # ramps open from ~closed
        self.assertLess(end, mid * 0.5)           # ramps closed at the end

    def test_mouth_closed_between_sentences(self):
        _, segs = _speak_segments("First sentence here. Second sentence here.", 0.0)
        gap_t = (segs[0][1] + segs[1][0]) / 2.0   # midpoint of the between-sentence pause
        self.assertEqual(speaking_openness_at(gap_t, 0.0, segs), 0.0)


class TestSpeakSegmentsForDuration(unittest.TestCase):
    def test_fills_requested_duration_exactly(self):
        _, segs = _speak_segments_for_duration("One two. Three four.", 0.0, 5.0)
        self.assertAlmostEqual(segs[-1][1], 5.0, places=6)

    def test_one_segment_per_sentence(self):
        _, segs = _speak_segments_for_duration("A here. B here. C here.", 0.0, 3.0)
        self.assertEqual(len(segs), 3)

    def test_longer_sentence_gets_more_time(self):
        _, segs = _speak_segments_for_duration("Hi. This one is quite a bit longer indeed.", 0.0, 6.0)
        self.assertLess(segs[0][1] - segs[0][0], segs[1][1] - segs[1][0])

    def test_gap_between_sentences_and_still_fills_duration(self):
        _, segs = _speak_segments_for_duration("One two three. Four five six.", 0.0, 4.0)
        self.assertAlmostEqual(segs[1][0] - segs[0][1], SPEAK_GAP_S, places=6)
        self.assertAlmostEqual(segs[-1][1], 4.0, places=6)

    def test_zero_duration_is_empty(self):
        _, segs = _speak_segments_for_duration("Hello.", 0.0, 0.0)
        self.assertEqual(segs, ())

    def test_negative_duration_is_empty(self):
        _, segs = _speak_segments_for_duration("Hello.", 0.0, -2.0)
        self.assertEqual(segs, ())

    def test_empty_text_single_segment_filling_duration(self):
        _, segs = _speak_segments_for_duration("   ", 0.0, 2.0)
        self.assertEqual(len(segs), 1)
        self.assertAlmostEqual(segs[-1][1], 2.0, places=6)

    def test_start_time_is_passed_through(self):
        start, _ = _speak_segments_for_duration("Hello there.", 12.5, 2.0)
        self.assertEqual(start, 12.5)


if __name__ == "__main__":
    unittest.main()
