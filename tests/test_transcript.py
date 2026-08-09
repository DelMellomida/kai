"""The gates that decide whether a Whisper transcript is worth acting on.

Moved out of tests/test_voice_assistant.py with ai/transcript.py. Pure text and numbers — no
model, no audio, no assistant.
"""

import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from ai.transcript import _best_allowed_language, latin_letter_ratio, transcript_rejection


class _Seg:
    """A faster-whisper segment, only the fields the sanity gate reads."""

    def __init__(self, text="hello", avg_logprob=-0.2, no_speech_prob=0.05):
        self.text = text
        self.avg_logprob = avg_logprob
        self.no_speech_prob = no_speech_prob


class TestLatinLetterRatio(unittest.TestCase):
    def test_plain_english(self):
        self.assertEqual(latin_letter_ratio("what time is it"), 1.0)

    def test_tagalog_with_accents_is_latin(self):
        # Tagalog's only extras are ñ and Spanish loanword accents — all Latin. This must never
        # be mistaken for a foreign script.
        for text in ("anong oras na", "magandáng umaga", "señor", "kumustá ka"):
            with self.subTest(text=text):
                self.assertEqual(latin_letter_ratio(text), 1.0)

    def test_cjk_is_not_latin(self):
        self.assertEqual(latin_letter_ratio("嘿哀"), 0.0)

    def test_punctuation_digits_and_emoji_are_ignored(self):
        # They are not alphabetic, so they can neither trip the check nor dilute a bad transcript
        # into passing it.
        self.assertEqual(latin_letter_ratio("set a timer for 5 minutes — ok? 🎉"), 1.0)
        self.assertEqual(latin_letter_ratio("123 !?, —"), 1.0)

    def test_no_letters_at_all_is_treated_as_fine(self):
        self.assertEqual(latin_letter_ratio(""), 1.0)
        self.assertEqual(latin_letter_ratio("..."), 1.0)

    def test_mixed_script_is_proportional(self):
        self.assertAlmostEqual(latin_letter_ratio("hey嘿"), 3 / 4)


class TestTranscriptRejection(unittest.TestCase):
    """The hole this closes: WHISPER_LANGUAGES restricts the detected-language LABEL only. A clip
    labelled 'en' is never re-transcribed, so a decode that emitted '嘿哀' — or invented a sentence out
    of fan noise — reached the LLM and was answered as though someone had asked it."""

    def test_good_transcript_is_kept(self):
        self.assertEqual(transcript_rejection("what time is it", [_Seg()]), "")

    def test_foreign_script_is_rejected(self):
        reason = transcript_rejection("嘿哀", [_Seg(text="嘿哀")])
        self.assertIn("Latin", reason)

    def test_the_measured_real_world_hallucination(self):
        # Observed on the robot: "hey kai" decoded as these. The first is caught by script; the
        # second is Latin and must fall to the confidence gate instead, not slip through.
        self.assertNotEqual(transcript_rejection("嘿哀", [_Seg(text="嘿哀")]), "")
        self.assertNotEqual(
            transcript_rejection("Hẹc gai!", [_Seg(text="Hẹc gai!", avg_logprob=-1.6)]), "")

    def test_low_confidence_is_rejected_even_in_english(self):
        reason = transcript_rejection("open the door", [_Seg(avg_logprob=-2.0)])
        self.assertIn("unintelligible", reason)

    def test_the_worst_segment_decides(self):
        # One confidently-wrong stretch is enough to make the transcript a different question than
        # the one that was asked, so the minimum governs — not the mean.
        segs = [_Seg(avg_logprob=-0.1), _Seg(avg_logprob=-3.0), _Seg(avg_logprob=-0.1)]
        self.assertNotEqual(transcript_rejection("a b c", segs), "")

    def test_words_decoded_out_of_silence_are_rejected(self):
        reason = transcript_rejection("Thank you.", [_Seg(text="Thank you.", no_speech_prob=0.95)])
        self.assertIn("no_speech_prob", reason)

    def test_empty_text_is_not_a_rejection(self):
        # Empty is handled upstream as "didn't catch that"; calling it a rejection would double-log.
        self.assertEqual(transcript_rejection("", [_Seg(avg_logprob=-9.0)]), "")

    def test_missing_fields_disable_only_that_gate(self):
        # A faster-whisper version that renames or omits a field must degrade to "gate off", never
        # to a TypeError that fails every turn.
        class Bare:
            text = "what time is it"

        self.assertEqual(transcript_rejection("what time is it", [Bare()]), "")

    def test_non_numeric_fields_are_ignored_rather_than_compared(self):
        self.assertEqual(
            transcript_rejection("hello", [_Seg(avg_logprob=MagicMock(),
                                                no_speech_prob=MagicMock())]), "")

    def test_numpy_floats_are_honoured(self):
        # np.float64 is a numbers.Real, so it must NOT be skipped as "not a number".
        segs = [_Seg(avg_logprob=np.float64(-2.5))]
        self.assertNotEqual(transcript_rejection("hello", segs), "")

    def test_script_guard_can_be_switched_off(self):
        with patch("ai.transcript.TRANSCRIPT_SCRIPT_GUARD", False):
            self.assertEqual(transcript_rejection("嘿哀", [_Seg(text="嘿哀")]), "")

    def test_each_gate_can_be_disabled_with_none(self):
        with patch("ai.transcript.TRANSCRIPT_MIN_AVG_LOGPROB", None):
            self.assertEqual(transcript_rejection("hello", [_Seg(avg_logprob=-9.0)]), "")
        with patch("ai.transcript.TRANSCRIPT_MAX_NO_SPEECH_PROB", None):
            self.assertEqual(transcript_rejection("hello", [_Seg(no_speech_prob=0.99)]), "")


def _info(language, probs=None, prob=0.9):
    """A stand-in for faster-whisper's TranscriptionInfo."""
    info = MagicMock()
    info.language = language
    info.language_probability = prob
    info.all_language_probs = probs
    return info


class TestBestAllowedLanguage(unittest.TestCase):
    def test_picks_the_highest_scoring_allowed_language(self):
        info = _info("cy", [("cy", 0.5), ("tl", 0.3), ("en", 0.1), ("nn", 0.05)])
        self.assertEqual(_best_allowed_language(info, ("en", "tl")), "tl")

    def test_ignores_disallowed_languages_however_confident(self):
        info = _info("nn", [("nn", 0.99), ("en", 0.001)])
        self.assertEqual(_best_allowed_language(info, ("en", "tl")), "en")

    def test_falls_back_to_the_first_allowed_when_probs_are_missing(self):
        # Older faster-whisper builds don't populate all_language_probs; guess nothing.
        self.assertEqual(_best_allowed_language(_info("cy", None), ("en", "tl")), "en")
        self.assertEqual(_best_allowed_language(_info("cy", []), ("tl", "en")), "tl")


if __name__ == "__main__":
    unittest.main()
