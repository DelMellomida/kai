import unittest
from unittest.mock import patch

from ai.identity import extract_name


def no_capital_gate():
    """Turn the weak tier's capitalisation corroboration off for the duration of a test.

    IDENTITY_WEAK_ANCHORS_NEED_CAPITAL is what makes "I'm X" safe at all, so most of the weak-tier
    tests assert behaviour WITH it on. This is for the handful that check the stop-list is doing its
    own share of the work rather than being masked by the casing gate — without it, a stop-list
    regression would pass because the casing check happened to reject the same string.
    """
    return patch("ai.identity.IDENTITY_WEAK_ANCHORS_NEED_CAPITAL", False)


class TestStrongAnchors(unittest.TestCase):
    """Phrases whose job is to introduce a name. No corroboration required."""

    def test_my_name_is(self):
        self.assertEqual(extract_name("My name is Jhondel"), "Jhondel")

    def test_my_name_contraction(self):
        self.assertEqual(extract_name("my name's Jhondel"), "Jhondel")

    def test_call_me(self):
        self.assertEqual(extract_name("You can call me Kim"), "Kim")

    def test_tagalog_ako_si(self):
        self.assertEqual(extract_name("Ako si Jhondel"), "Jhondel")

    def test_tagalog_ako_po_si(self):
        self.assertEqual(extract_name("Ako po si Maria"), "Maria")

    def test_tagalog_pangalan_ko(self):
        self.assertEqual(extract_name("Ang pangalan ko ay Jhondel"), "Jhondel")

    def test_strong_anchor_does_not_need_capital(self):
        # Whisper's casing of a Tagalog utterance is not reliable, and "si" is a personal-name
        # marker — introducing a name is its grammatical job, so it corroborates by itself.
        self.assertEqual(extract_name("ako si jhondel"), "Jhondel")

    def test_mid_sentence(self):
        self.assertEqual(extract_name("Hi there, my name is Ana, nice to meet you"), "Ana")

    def test_first_token_only(self):
        # Documented behaviour: the first name is what Kai should be saying out loud, and it avoids
        # having to decide where a surname stops.
        self.assertEqual(extract_name("My name is Juan Dela Cruz"), "Juan")


class TestWeakAnchors(unittest.TestCase):
    """"I'm X" — common in speech, almost never about a name. Accepted only with corroboration."""

    def test_capitalised_name_accepted(self):
        self.assertEqual(extract_name("I'm Jhondel"), "Jhondel")

    def test_i_am_spelled_out(self):
        self.assertEqual(extract_name("I am Jhondel"), "Jhondel")

    def test_lowercase_rejected(self):
        # The corroboration IS the safety of this tier: without it every "I'm something" is a name.
        self.assertIsNone(extract_name("i'm jhondel"))

    def test_all_caps_is_not_evidence(self):
        # A shouted or mis-cased transcript capitalises everything, so capitalisation stops carrying
        # information. Rejecting is the right failure — a missed name costs nothing.
        self.assertIsNone(extract_name("I'M JHONDEL"))

    def test_state_of_being_rejected(self):
        for text in ("I'm fine", "I'm good", "I'm okay", "I'm tired", "I'm just looking"):
            with self.subTest(text=text):
                self.assertIsNone(extract_name(text))

    def test_capitalised_stopword_still_rejected(self):
        # Whisper capitalises the first word of an utterance, so "I'm Fine." reaches the gate with a
        # capital and only the stop-list stands between it and becoming a name.
        self.assertIsNone(extract_name("I'm Fine"))

    def test_place_rejected_by_stoplist_not_only_casing(self):
        with no_capital_gate():
            self.assertIsNone(extract_name("I'm from Cebu"))
            self.assertIsNone(extract_name("I'm a developer"))
            self.assertIsNone(extract_name("I'm not sure"))

    def test_tagalog_akoy_state_rejected(self):
        with no_capital_gate():
            self.assertIsNone(extract_name("ako'y masaya"))


class TestRejections(unittest.TestCase):
    def test_empty_and_none(self):
        self.assertIsNone(extract_name(""))
        self.assertIsNone(extract_name(None))

    def test_no_anchor(self):
        self.assertIsNone(extract_name("How many chapters does DEVCON have?"))

    def test_third_person_is_not_an_introduction(self):
        # "his name is" must not match through the "my name is" anchor — Kai is being told about
        # somebody else, and adopting it would address the wrong person by the wrong name.
        self.assertIsNone(extract_name("His name is Jhondel"))
        self.assertIsNone(extract_name("Her name is Ana"))

    def test_question_about_the_name(self):
        self.assertIsNone(extract_name("What is my name?"))
        self.assertIsNone(extract_name("Do you remember my name?"))

    def test_digits_never_a_name(self):
        self.assertIsNone(extract_name("My name is 2024"))

    def test_too_short(self):
        self.assertIsNone(extract_name("My name is K"))

    def test_too_long(self):
        self.assertIsNone(extract_name("My name is " + "a" * 40))

    def test_anchor_at_end_of_string(self):
        # No candidate follows. Must return None rather than indexing past the end.
        self.assertIsNone(extract_name("my name is"))
        self.assertIsNone(extract_name("I'm"))

    def test_anchor_followed_by_punctuation_only(self):
        self.assertIsNone(extract_name("my name is ...?"))


class TestNameShape(unittest.TestCase):
    def test_title_cased(self):
        # The result goes into a prompt that will be read out loud; Whisper's casing of a spoken
        # name is not reliable enough to pass through.
        self.assertEqual(extract_name("my name is jhondel"), "Jhondel")

    def test_apostrophe_name(self):
        self.assertEqual(extract_name("my name is o'brien"), "O'Brien")

    def test_hyphenated_name(self):
        self.assertEqual(extract_name("my name is mary-jane"), "Mary-Jane")

    def test_internal_capitals_survive(self):
        # str.title() would flatten this to "Mckenzie". The letter after each separator is
        # capitalised; everything else is left alone.
        self.assertEqual(extract_name("my name is McKenzie"), "McKenzie")

    def test_accented_name(self):
        self.assertEqual(extract_name("my name is josé"), "José")

    def test_trailing_punctuation_not_captured(self):
        self.assertEqual(extract_name("My name is Jhondel."), "Jhondel")
        self.assertEqual(extract_name("My name is Jhondel, and you?"), "Jhondel")


class TestPreferenceOrder(unittest.TestCase):
    def test_strong_anchor_wins_over_earlier_weak_one(self):
        # "I'm not sure" would be rejected anyway, but the ordering is what matters: a strong anchor
        # anywhere in the utterance beats a weak one earlier in it.
        self.assertEqual(extract_name("I'm not sure, my name is Ana"), "Ana")

    def test_first_strong_anchor_wins(self):
        self.assertEqual(extract_name("my name is Ana, call me Anne"), "Ana")


if __name__ == "__main__":
    unittest.main()
