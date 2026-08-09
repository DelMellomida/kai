import unittest
from unittest.mock import patch

from ai.wake_phrase import match_wake_phrase, normalize_tokens


def strict_two_word():
    """Turn the bare-prefix form (C) off for the duration of a test.

    Everything it guards — the name slot, the blocklists, the ratio thresholds — belongs to the
    two-word path, and form C bypasses all of it by design. Without this, a test asserting that
    "hey Kayla" is rejected for the right reason would pass or fail on form C instead, and the
    measurement it encodes would quietly stop being checked.
    """
    return patch("ai.wake_phrase.WAKE_PHRASE_SOLO_PREFIXES", ())


class TestNormalizeTokens(unittest.TestCase):
    def test_empty_and_none(self):
        self.assertEqual(normalize_tokens(""), [])
        self.assertEqual(normalize_tokens(None), [])

    def test_punctuation_only(self):
        self.assertEqual(normalize_tokens("...,!?"), [])

    def test_casefolds_and_keeps_offsets(self):
        toks = normalize_tokens("Hey, Kai!")
        self.assertEqual([t.text for t in toks], ["hey", "kai"])
        self.assertEqual((toks[0].start, toks[0].end), (0, 3))
        self.assertEqual((toks[1].start, toks[1].end), (5, 8))

    def test_offsets_index_the_original_string(self):
        text = "Hey Kai, what time is it?"
        toks = normalize_tokens(text)
        # The whole point of carrying offsets: slicing the original must reproduce it exactly.
        self.assertEqual(text[toks[1].start:toks[1].end], "Kai")

    def test_accents_folded_without_breaking_offsets(self):
        # NFKD is not length-preserving, so accents are folded PER TOKEN. If offsets were mapped
        # through a decomposed copy of the whole string they would drift.
        text = "Héy Kaí, ano?"
        toks = normalize_tokens(text)
        self.assertEqual([t.text for t in toks][:2], ["hey", "kai"])
        self.assertEqual(text[toks[0].start:toks[0].end], "Héy")
        self.assertEqual(text[toks[1].start:toks[1].end], "Kaí")

    def test_digits_are_tokens(self):
        self.assertEqual([t.text for t in normalize_tokens("set a timer for 5")][-1], "5")

    def test_underscore_is_a_separator(self):
        self.assertEqual([t.text for t in normalize_tokens("hey_kai")], ["hey", "kai"])


class TestMatchAccepts(unittest.TestCase):
    """The table from the plan, verbatim — these are the renderings faster-whisper actually emits."""

    def assert_match(self, text, command, msg=None):
        m = match_wake_phrase(text)
        self.assertIsNotNone(m, msg or f"{text!r} should match")
        self.assertEqual(m.command, command, f"command from {text!r}")
        self.assertGreater(m.score, 0.0)
        return m

    def test_bare_phrase_with_punctuation(self):
        self.assert_match("Hey, Kai.", "")

    def test_phrase_plus_command(self):
        self.assert_match("Hey Kai, what time is it?", "what time is it?")

    def test_sentence_split_between_the_two_words(self):
        self.assert_match("Hey. Kai. Turn on the light.", "Turn on the light.")

    def test_hi_variant(self):
        self.assert_match("Hi Kai", "")

    def test_misheard_name_ky(self):
        self.assert_match("Hey Ky what's the weather in Manila", "what's the weather in Manila")

    def test_misheard_name_chi(self):
        self.assert_match("hey chi", "")

    def test_tagalog_prefix_and_command(self):
        self.assert_match("Hoy Kai, kumusta?", "kumusta?")

    def test_joined_single_token(self):
        self.assert_match("heykai", "")

    def test_joined_with_command(self):
        self.assert_match("heykai what time is it", "what time is it")

    def test_command_preserves_casing_punctuation_and_digits(self):
        m = self.assert_match("Hey Kai, set a timer for 5 minutes — OK?",
                              "set a timer for 5 minutes — OK?")
        self.assertIn("5", m.command)

    def test_command_preserves_accents(self):
        self.assert_match("Hey Kai, sino si José?", "sino si José?")

    def test_phrase_field_is_the_raw_text(self):
        m = self.assert_match("Hey, Kai! hello", "hello")
        self.assertEqual(m.phrase, "Hey, Kai")

    def test_leading_filler_within_the_scan_window(self):
        # "so hey kai ..." — phrase starts at token 1, inside the window.
        self.assert_match("So hey Kai what time is it", "what time is it")

    def test_score_is_the_weaker_ratio(self):
        # "chi" is an exact entry in WAKE_PHRASE_NAMES, so it scores 1.0 — a drawn-out prefix is the
        # genuinely fuzzy case.
        exact = match_wake_phrase("hey kai")
        fuzzy = self.assert_match("heyy kai", "")
        self.assertEqual(exact.score, 1.0)
        self.assertLess(fuzzy.score, 1.0)


class TestMatchRejects(unittest.TestCase):
    def assert_no_match(self, text, why):
        self.assertIsNone(match_wake_phrase(text), f"{text!r} must NOT match ({why})")

    def test_name_alone_is_not_a_wake(self):
        # Otherwise every mention of the robot's name wakes it.
        self.assert_no_match("Kai", "no prefix")
        self.assert_no_match("Kai, what time is it?", "no prefix")

    def test_okay_google(self):
        self.assert_no_match("Okay Google, play music", "name ratio + length guard")

    def test_tagalog_kaya(self):
        # Form A only — bare "hey" at token 0 wakes Kai in the live config regardless of what
        # follows it. The blocklist still has to hold, or "kaya" would match the NAME slot and the
        # command would be silently truncated to "may tanong ako".
        with strict_two_word():
            self.assert_no_match("hey kaya may tanong ako", "blocklist")

    def test_tagalog_kayo(self):
        self.assert_no_match("hi kayo", "blocklist")

    def test_third_person_reference(self):
        self.assert_no_match("Sabihin mo kay Kai na kumain", "kay<->hey ratio below threshold")

    def test_phrase_too_late_in_the_utterance(self):
        self.assert_no_match("So anyway Kai told me something", "starts past the scan window")

    def test_phrase_beyond_the_scan_window_even_when_exact(self):
        # Exact "hey kai" but at token 3 — outside WAKE_PHRASE_SCAN_TOKENS.
        self.assert_no_match("one two three hey kai", "outside the scan window")

    def test_whisper_silence_hallucinations(self):
        for text in ("Thank you.", "You", "Thanks for watching!", ".", " "):
            self.assert_no_match(text, "whisper filler on near-silence")

    def test_empty_input(self):
        self.assert_no_match("", "empty")
        self.assert_no_match(None, "none")

    def test_long_transcript_is_skipped(self):
        # A wake phrase plus a short command is never 30 words. Skipping long transcripts is both a
        # false-accept guard and a cost guard.
        text = "hey kai " + " ".join(f"word{i}" for i in range(30))
        self.assert_no_match(text, "word cap")

    def test_prefix_without_a_plausible_name(self):
        # Form A only. With the bare-prefix form on this DOES wake Kai — see TestSoloPrefix.
        with strict_two_word():
            self.assert_no_match("hey there everyone", "no name token")

    def test_unrelated_speech(self):
        for text in ("what time is it", "turn off the lights please",
                     "magandang umaga sa lahat", "the quick brown fox"):
            self.assert_no_match(text, "no phrase at all")


class TestAdversarialFalseAccepts(unittest.TestCase):
    """Every phrase here fired during an adversarial sweep and is now fixed. They are the reason the
    name slot excludes prefix words, the length slack is 1, "kye" is not an alias, and form B has a
    length floor. Each was measured, not guessed."""

    CASES = [
        ("Okay Google, play music", "bare prefix word matched the joined form (0.89 vs 'okkay')"),
        ("okay okay i get it", "'okay' matched the NAME slot (0.86 vs 'kay')"),
        ("hi hi hi", "'hi' matched the NAME slot (0.80 vs 'chi')"),
        ("Sabihin mo kay Kai", "'kay' matched the PREFIX slot (0.86 vs 'okay') — third person"),
        ("kaya natin ito", "blocklist"),
        ("kayo na bahala", "blocklist"),
        ("si Kai ang nagsabi", "'si' is a prefix-blocklisted Tagalog marker"),
        ("ni Kai yan", "'ni' is a prefix-blocklisted Tagalog marker"),
    ]
    # Same regressions, but these open with a bare "hey", so form C matches them in the live config
    # and only the two-word path can still be checked. Kept because the NAME-slot guard they pin is
    # what stops the command being truncated: without it "hey Kyle can you help" would wake Kai AND
    # send the LLM "can you help", losing a word.
    CASES_TWO_WORD_ONLY = [
        ("hey Kyle can you help", "'kyle' matched the 'kye' alias (0.86)"),
        ("hey Kayla", "'kayla' matched 'kay' (0.75) at length slack 2"),
    ]

    def test_none_of_them_fire(self):
        # None of these start with a bare "hey", so form C cannot rescue them either — asserted
        # without strict_two_word() on purpose, as an end-to-end check of the live configuration.
        for text, why in self.CASES:
            with self.subTest(text=text):
                self.assertIsNone(match_wake_phrase(text), f"regression: {why}")

    def test_the_name_slot_still_rejects_the_hey_prefixed_cases(self):
        with strict_two_word():
            for text, why in self.CASES_TWO_WORD_ONLY:
                with self.subTest(text=text):
                    self.assertIsNone(match_wake_phrase(text), f"regression: {why}")

    def test_hey_prefixed_cases_keep_their_whole_command(self):
        # The consequence of the guard above, stated positively: form C matches only "hey", so
        # everything after it survives into the command.
        m = match_wake_phrase("hey Kyle can you help")
        self.assertEqual(m.phrase, "hey")
        self.assertEqual(m.command, "Kyle can you help")

    def test_common_names_do_not_fire_in_the_name_slot(self):
        # Form A only: proves the NAME slot rejects these, which is what the length slack and the
        # "kye" removal were tuned for. The "hey ..." entries reach form C in the live config and
        # wake Kai there — that is the accepted cost, pinned in TestSoloPrefix.
        with strict_two_word():
            for text in ("hi Kate", "hi Katie", "hey Cara", "hey Chris", "hi Casey", "hey Carl",
                         "hey Kim", "hey Ken", "hi Kenny", "hey there"):
                with self.subTest(text=text):
                    self.assertIsNone(match_wake_phrase(text))

    def test_hi_greetings_still_do_not_fire_in_the_live_config(self):
        # "hi" is deliberately NOT in WAKE_PHRASE_SOLO_PREFIXES — it is how people greet each other
        # in the room. These must stay rejected with form C enabled.
        for text in ("hi Kate", "hi Katie", "hi Casey", "hi Kenny", "hi there", "hi everyone"):
            with self.subTest(text=text):
                self.assertIsNone(match_wake_phrase(text))

    def test_real_variants_still_match(self):
        # The guards above must not have cost us any genuine rendering.
        for text in ("hey kai", "Hey Kai!", "hi kai", "hoy kai", "oy kai", "ok kai", "okay kai",
                     "Hey Ky", "hey chi", "hey cai", "Hey Kaii", "Hey Chai", "heyy kai", "heykai"):
            with self.subTest(text=text):
                self.assertIsNotNone(match_wake_phrase(text))


class TestKnownAmbiguity(unittest.TestCase):
    """Accepted false accepts. Each is a deliberate trade recorded here, not an oversight."""

    def test_kaye_is_accepted(self):
        # "hey Kaye" and "hey Kai" are near-homophones; the acoustic tiers cannot separate them
        # either. Blocklisting "kaye" would reject legitimate wakes Whisper renders that way.
        self.assertIsNotNone(match_wake_phrase("hey Kaye"))

    def test_hey_guys_is_accepted_because_tiny_hears_kai_as_guy(self):
        # Measured on the robot: the tiny scan model renders "hey Kai" as 'Hey guys!', 'Hey, Guy.',
        # 'Hey guy' and 'Hẹc gai!'. Without these aliases the whisper tier essentially never fires.
        # The cost is that "hey guys" said to the room wakes Kai — an ack and a self-ending listening
        # window. A false reject, by contrast, means the feature does nothing at all.
        for text in ("hey guys", "Hey guys!", "Hey, Guy.", "hey guy",
                     "Hey guys, what time is it?"):
            with self.subTest(text=text):
                self.assertIsNotNone(match_wake_phrase(text))

    def test_the_guy_alias_did_not_widen_to_other_g_names(self):
        # Listing "guys" and "gai" as separate aliases DID drag these in; a single "guy" does not.
        # Form A only — the "hey ..." entries now wake Kai through form C instead, which is a
        # different mechanism and must not be allowed to mask a regression in this one.
        with strict_two_word():
            for text in ("hey Gail", "hey Greg", "hey Gus", "hi girls", "hey good morning",
                         "okay go ahead", "hey guess what", "hi Gus"):
                with self.subTest(text=text):
                    self.assertIsNone(match_wake_phrase(text))


class TestScanWindowBoundary(unittest.TestCase):
    """WAKE_PHRASE_SCAN_TOKENS is the single most load-bearing false-accept guard, so pin its edges."""

    def test_at_the_last_allowed_index(self):
        with patch("ai.wake_phrase.WAKE_PHRASE_SCAN_TOKENS", 3):
            self.assertIsNotNone(match_wake_phrase("one two hey kai"))   # phrase starts at token 2

    def test_one_past_the_window(self):
        with patch("ai.wake_phrase.WAKE_PHRASE_SCAN_TOKENS", 3):
            self.assertIsNone(match_wake_phrase("one two three hey kai"))

    def test_window_of_one_requires_the_very_start(self):
        with patch("ai.wake_phrase.WAKE_PHRASE_SCAN_TOKENS", 1):
            self.assertIsNotNone(match_wake_phrase("hey kai"))
            self.assertIsNone(match_wake_phrase("so hey kai"))


class TestWordCapBoundary(unittest.TestCase):
    def test_exactly_at_the_cap_still_matches(self):
        with patch("ai.wake_phrase.WAKE_WHISPER_MAX_WORDS", 5):
            self.assertIsNotNone(match_wake_phrase("hey kai a b c"))

    def test_one_over_the_cap_is_skipped(self):
        with patch("ai.wake_phrase.WAKE_WHISPER_MAX_WORDS", 5):
            self.assertIsNone(match_wake_phrase("hey kai a b c d"))


class TestBlocklistPrecedence(unittest.TestCase):
    def test_blocklist_wins_over_a_passing_ratio(self):
        # "kaya" is close enough to "kai" to pass the name ratio; the blocklist must be consulted
        # BEFORE the ratio, not after. Form C off — "hey kaya" starts with a bare "hey" and would
        # otherwise match for a reason that has nothing to do with the blocklist.
        with strict_two_word(), \
             patch("ai.wake_phrase.WAKE_PHRASE_NAME_RATIO", 0.1), \
             patch("ai.wake_phrase.WAKE_PHRASE_PREFIX_RATIO", 0.1):
            self.assertIsNone(match_wake_phrase("hey kaya"))

    def test_a_non_blocklisted_word_matches_at_the_same_low_threshold(self):
        # Proves the rejection above came from the blocklist and not from the loosened ratios.
        with patch("ai.wake_phrase.WAKE_PHRASE_NAME_RATIO", 0.1), \
             patch("ai.wake_phrase.WAKE_PHRASE_PREFIX_RATIO", 0.1):
            self.assertIsNotNone(match_wake_phrase("hey kai"))


class TestThresholdsAreHonoured(unittest.TestCase):
    def test_raising_the_prefix_ratio_rejects_a_loose_variant(self):
        self.assertIsNotNone(match_wake_phrase("heyy kai"))
        # Form C off: "heyy" scores 0.86 against "hey", above WAKE_PHRASE_SOLO_RATIO, so with it on
        # the utterance still matches — via the solo path, not the prefix ratio under test.
        with strict_two_word(), patch("ai.wake_phrase.WAKE_PHRASE_PREFIX_RATIO", 0.99):
            self.assertIsNone(match_wake_phrase("heyy kai"))

    def test_raising_the_joined_ratio_rejects_a_loose_join(self):
        with patch("ai.wake_phrase.WAKE_PHRASE_JOINED_RATIO", 0.99):
            self.assertIsNone(match_wake_phrase("haykai"))


class TestSoloPrefix(unittest.TestCase):
    """Form C: a bare "hey", no name. Added because the NAME slot is where the whisper tier loses
    real wakes — "tiny" renders "Kai" as guy/gai/chi/嘿哀 and WAKE_PHRASE_NAMES only ever catches up
    after someone has been ignored. The prefix is one common English word the model gets right."""

    def test_bare_hey_wakes(self):
        for text in ("hey", "Hey!", "Hey.", "  hey  "):
            with self.subTest(text=text):
                self.assertIsNotNone(match_wake_phrase(text))

    def test_bare_hey_with_a_command_in_one_breath(self):
        m = match_wake_phrase("Hey, what time is it?")
        self.assertIsNotNone(m)
        self.assertEqual(m.command, "what time is it?")
        self.assertEqual(m.phrase, "Hey")

    def test_the_name_is_not_leaked_into_the_command(self):
        # The ordering guard: form A must win on "Hey Kai, ..." even though form C would also match
        # at token 0. If C ran first the LLM would receive "Kai, what time is it?".
        m = match_wake_phrase("Hey Kai, what time is it?")
        self.assertEqual(m.command, "what time is it?")
        self.assertEqual(m.phrase, "Hey Kai")

    def test_a_mangled_name_now_wakes_instead_of_being_lost(self):
        # The whole point. None of these names are in WAKE_PHRASE_NAMES, and before form C every
        # one of them was a silent false reject.
        for text in ("hey kaz", "hey tie", "hey pie", "hey kite", "hey khai"):
            with self.subTest(text=text):
                self.assertIsNotNone(match_wake_phrase(text))

    def test_mid_sentence_hey_does_not_wake(self):
        # WAKE_PHRASE_SOLO_SCAN_TOKENS is 1: this is the entire safety argument for form C.
        for text in ("and I was like hey, no", "so hey what's up", "well hey there",
                     "I said hey to him"):
            with self.subTest(text=text):
                self.assertIsNone(match_wake_phrase(text))

    def test_other_prefixes_are_not_solo_wake_words(self):
        # WAKE_PHRASE_PREFIXES is much wider than WAKE_PHRASE_SOLO_PREFIXES, and these open ordinary
        # sentences. If this ever fails, someone widened the solo list — that is a false-accept
        # firehose, not a tuning tweak.
        for text in ("okay, so what happened was", "ok let's go", "hi everyone", "oy sandali lang",
                     "ey pare"):
            with self.subTest(text=text):
                self.assertIsNone(match_wake_phrase(text))

    def test_near_misses_are_rejected(self):
        # "they" is the reason form C matches exactly instead of by ratio: it scores 0.857 against
        # "hey" — the SAME score as "heyy", a genuine drawn-out wake. No threshold separates them,
        # so if this test ever fails because someone reintroduced a ratio here, that is why.
        for text in ("they went home", "hay naku", "say that again", "the quick brown fox",
                     "her name is Ana", "hell no", "hen", "heh"):
            with self.subTest(text=text):
                self.assertIsNone(match_wake_phrase(text))

    def test_drawn_out_hey_still_wakes(self):
        for text in ("heyy", "heyyy", "heeey", "Heeeyyy!"):
            with self.subTest(text=text):
                self.assertIsNotNone(match_wake_phrase(text))

    def test_score_is_always_exact(self):
        # Form C has no ratio to report — it matched literally or it did not match.
        self.assertEqual(match_wake_phrase("hey").score, 1.0)
        self.assertEqual(match_wake_phrase("heyy").score, 1.0)

    def test_disabling_the_solo_list_restores_strict_two_word_matching(self):
        # The documented rollback: WAKE_PHRASE_SOLO_PREFIXES = () in config/wake.py.
        with strict_two_word():
            self.assertIsNone(match_wake_phrase("hey"))
            self.assertIsNone(match_wake_phrase("Hey, what time is it?"))
            self.assertIsNotNone(match_wake_phrase("hey kai"))

    def test_solo_respects_the_word_cap(self):
        # The cap is checked before any form runs, so a long transcript starting with "hey" is still
        # skipped without paying for matching.
        self.assertIsNone(match_wake_phrase("hey " + " ".join(f"word{i}" for i in range(30))))

    def test_widening_the_solo_window_is_possible_but_costly(self):
        # Pins that WAKE_PHRASE_SOLO_SCAN_TOKENS is honoured rather than hardcoded to 0, and shows
        # exactly what raising it buys and costs.
        with patch("ai.wake_phrase.WAKE_PHRASE_SOLO_SCAN_TOKENS", 3):
            self.assertIsNotNone(match_wake_phrase("so hey what's up"))


class TestSoloPrefixKnownFalseAccepts(unittest.TestCase):
    """Accepted cost of form C, recorded rather than hidden. Greeting any person by name wakes Kai:
    the utterance is indistinguishable from a wake at token 0, and no amount of tuning separates
    "hey Chris" from "hey Kai" when the scan model renders the name unreliably in the first place.

    Each costs an ack and a listening window that self-ends. A false REJECT costs the whole feature.
    If this becomes intolerable in a shared room, the fix is tier 2 (openWakeWord), not a stricter
    matcher — see wake/README.md."""

    def test_greeting_a_person_wakes_kai(self):
        for text in ("hey Chris", "hey Carl", "hey Greg", "hey Gail", "hey guys",
                     "hey everyone", "hey good morning", "hey there"):
            with self.subTest(text=text):
                self.assertIsNotNone(match_wake_phrase(text))


if __name__ == "__main__":
    unittest.main()
