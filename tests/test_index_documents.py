import unittest
from pathlib import Path

from ai.index_documents import (
    build_gazetteer, chunk_entries, chunk_file, document_title, name_candidates, parse_entry,
)


ENTRY = """---
id: about-history
section: About DEVCON
synonyms: [origin, founding, first event]
---
**Q: When did DEVCON start?**
A: DEVCON began in 2009 in Cebu City."""


class TestParseEntry(unittest.TestCase):
    """The knowledge base's front matter is worth opposite things to the two consumers: the
    synonyms are the best retrieval signal in the file and pure noise in the prompt."""

    def test_splits_metadata_from_body(self):
        entry = parse_entry(ENTRY)
        self.assertEqual(entry["section"], "About DEVCON")
        self.assertEqual(entry["entry_id"], "about-history")
        self.assertEqual(entry["synonyms"], ["origin", "founding", "first event"])

    def test_body_is_the_qa_pair_without_markdown(self):
        # The asterisks are markdown Kai must never speak; stripping them here means tts.py is
        # not the only thing standing between the source file and the speaker.
        entry = parse_entry(ENTRY)
        self.assertEqual(entry["text"], "Q: When did DEVCON start?\nA: DEVCON began in 2009 in Cebu City.")

    def test_an_ordinary_paragraph_is_not_an_entry(self):
        self.assertIsNone(parse_entry("Just a paragraph of prose."))

    def test_missing_metadata_keys_degrade_rather_than_raise(self):
        entry = parse_entry("---\nid: x\n---\nA: body")
        self.assertEqual(entry["section"], "")
        self.assertEqual(entry["synonyms"], [])


class TestChunkEntries(unittest.TestCase):
    def test_claims_a_knowledge_base_file(self):
        entries = chunk_entries(Path("kb.md"), "\n\n".join([ENTRY] * 3))
        self.assertEqual(len(entries), 3)

    def test_synonyms_reach_the_embedding_but_not_the_prompt(self):
        entry = chunk_entries(Path("kb.md"), "\n\n".join([ENTRY] * 3))[0]
        self.assertIn("origin, founding, first event", entry["embed"])
        self.assertNotIn("origin", entry["text"])
        self.assertIn("About DEVCON", entry["embed"])

    def test_declines_a_file_that_is_mostly_prose(self):
        # A stray "---" fence in an ordinary document must not make it claim the whole file.
        text = ENTRY + "\n\n" + "\n\n".join(f"Paragraph {i} of ordinary prose." for i in range(8))
        self.assertIsNone(chunk_entries(Path("notes.md"), text))

    def test_declines_a_pdf(self):
        self.assertIsNone(chunk_entries(Path("scan.pdf"), ENTRY))


class TestNameCandidates(unittest.TestCase):
    """Replaces markdown headings as the gazetteer's raw material — the knowledge base has one
    heading in the whole file. Noisy by design; build_gazetteer does the rejecting."""

    def test_finds_a_multi_word_name(self):
        self.assertIn("DEVCON Kids", name_candidates("The DEVCON Kids programme teaches code."))

    def test_does_not_stride_across_a_full_stop(self):
        # The token pattern must allow an internal period ("NMBLR.AI"), which without a sentence
        # split glued two names into "TechBar in Cebu City. It".
        names = name_candidates("It began at the TechBar in Cebu City. It grew from there.")
        self.assertIn("TechBar in Cebu City", names)
        self.assertNotIn("TechBar in Cebu City. It", names)

    def test_strips_a_sentence_opener_that_is_only_capitalized_by_position(self):
        # "Is DEVCON" carries the brand, which bypasses the frequency filter downstream — so it
        # has to be rejected here or not at all.
        self.assertNotIn("Is DEVCON", name_candidates("Is DEVCON a non-profit?"))

    def test_keeps_minor_words_inside_a_name(self):
        self.assertIn("Geeks on a Beach", name_candidates("We run Geeks on a Beach yearly."))

    def test_a_single_capitalized_word_is_not_a_candidate(self):
        self.assertEqual(name_candidates("Kai answered the question."), [])


class TestDocumentTitle(unittest.TestCase):
    """The per-chunk embedding prefix. Every chunk gets one, including the ones that never
    mention DEVCON — which are exactly the ones a DEVCON question scores worst against."""

    def test_underscore_separated_filename(self):
        self.assertEqual(document_title(Path("Pioneering_Programs_-_DEVCON_PH.md")),
                         "Pioneering Programs - DEVCON PH")

    def test_hyphen_is_not_split_when_the_file_uses_underscores(self):
        # "AI-Ready" is one word; splitting on both separators would break it.
        self.assertEqual(document_title(Path("17_years_of_DEVCON_AI-Ready_Nation.md")),
                         "17 years of DEVCON AI-Ready Nation")

    def test_hyphen_separated_filename(self):
        self.assertEqual(document_title(Path("DEVCON-Philippines-Omnibus.md")),
                         "DEVCON Philippines Omnibus")

    def test_version_and_duplicate_suffixes_are_dropped(self):
        self.assertEqual(document_title(Path("DEVCON-Omnibus (v1 July 9 2026) (1).md")),
                         "DEVCON Omnibus")


class TestChunkFileHeadings(unittest.TestCase):
    def test_headings_are_returned_alongside_the_chunks(self):
        text = "# DEVCON Kids\n\nA programme for children.\n\n## Campus DEVCON\n\nFor students.\n"
        pieces, headings = chunk_file(Path("doc.md"), text)
        self.assertEqual(headings, ["DEVCON Kids", "Campus DEVCON"])
        # Headings are still not indexed on their own — they ride along as breadcrumbs.
        self.assertEqual(len(pieces), 2)
        self.assertTrue(pieces[0].startswith("DEVCON Kids\n"))

    def test_prose_files_contribute_no_headings(self):
        pieces, headings = chunk_file(Path("scan.pdf"), "just some extracted prose")
        self.assertEqual(headings, [])
        self.assertEqual(pieces, ["just some extracted prose"])


class TestBuildGazetteer(unittest.TestCase):
    """Which headings are names, and which are just a style guide talking to its reader. Both
    filters are load-bearing — see build_gazetteer for what each one alone let through."""

    # A corpus where "beach" and "kids" are rare and "brand"/"post"/"tone" are everywhere.
    CHUNKS = ([{"source": "style.md", "text": f"Keep the brand tone consistent in every post {i}"}
               for i in range(40)]
              + [{"source": "programs.md", "text": "Geeks on a Beach is an annual conference"},
                 {"source": "programs.md", "text": "DEVCON Kids teaches children to code"},
                 {"source": "style.md", "text": "When in doubt, keep the tone warm"}])

    def test_keeps_a_rare_name(self):
        self.assertIn("Geeks on a Beach", build_gazetteer(["Geeks on a Beach"], self.CHUNKS))

    def test_drops_a_sentence_shaped_heading(self):
        # "doubt" is rare enough to pass the frequency filter on its own; the name-shape test is
        # the only thing standing between it and flagging "when in doubt" as a DEVCON question.
        self.assertEqual(build_gazetteer(["When in doubt", "How to use this document"],
                                         self.CHUNKS), [])

    def test_drops_a_common_name_shaped_heading(self):
        # Title Case is not enough either: this one is capitalized but says nothing rare.
        self.assertEqual(build_gazetteer(["Brand Tone"], self.CHUNKS), [])

    def test_brand_headings_bypass_the_frequency_filter(self):
        # "DEVCON Kids" is an entity whatever its word counts say — and in a DEVCON corpus the
        # word DEVCON is by definition not rare.
        self.assertIn("DEVCON Kids", build_gazetteer(["DEVCON Kids"], self.CHUNKS))

    def test_long_headings_are_not_names(self):
        self.assertEqual(build_gazetteer(["Geeks On A Beach Is An Annual Conference"],
                                         self.CHUNKS), [])

    def test_duplicates_are_collapsed(self):
        self.assertEqual(build_gazetteer(["DEVCON Kids", "devcon kids"], self.CHUNKS),
                         ["DEVCON Kids"])

    def test_empty_corpus_yields_nothing(self):
        self.assertEqual(build_gazetteer(["DEVCON Kids"], []), [])

    def test_a_one_word_topic_label_is_not_a_name(self):
        # The regression that motivated GAZETTEER_MIN_CONTENT_WORDS. "Theme" is Title Case and
        # rare, so both original filters passed it, and with it in the gazetteer "what's the
        # theme of your talk?" was flagged as a DEVCON question.
        chunks = self.CHUNKS + [{"source": "style.md", "text": "The theme is announced yearly"}]
        self.assertEqual(build_gazetteer(["Theme"], chunks), [])

    def test_a_one_word_heading_still_counts_when_it_carries_the_brand(self):
        self.assertEqual(build_gazetteer(["DEVCON"], self.CHUNKS), ["DEVCON"])

    def test_short_words_do_not_count_toward_the_content_minimum(self):
        # "Cagayan De Oro": only "cagayan" is long enough to identify anything, so this is a
        # one-content-word heading despite having three words.
        chunks = self.CHUNKS + [{"source": "stats.md", "text": "Cagayan de Oro chapter report"}]
        self.assertEqual(build_gazetteer(["Cagayan De Oro"], chunks), [])

    def test_a_trailing_colon_marks_a_form_field_not_a_name(self):
        # The chapter-stats submissions are written as form prompts. Title Case, rare words —
        # only the punctuation distinguishes them.
        chunks = self.CHUNKS + [{"source": "stats.md", "text": "Proudest milestone this year"}]
        self.assertEqual(build_gazetteer(["Proudest Work/Milestone:"], chunks), [])

    def test_the_df_ceiling_does_not_relax_as_the_corpus_grows(self):
        # A ratio alone gets looser with more chunks. "Common Award Name" appears in 8 chunks;
        # at 400 chunks a 2% ratio would admit it (ceiling 8) and the absolute cap must not.
        big = ([{"source": "a.md", "text": f"Common Award Name mentioned here {i}"}
                for i in range(8)]
               + [{"source": "b.md", "text": f"filler text number {i}"} for i in range(392)])
        self.assertEqual(build_gazetteer(["Common Award Name"], big), [])


if __name__ == '__main__':
    unittest.main()
