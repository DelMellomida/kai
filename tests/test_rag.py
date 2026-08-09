import json
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from ai import rag


class TestChunkText(unittest.TestCase):
    def test_empty_string_returns_empty_list(self):
        self.assertEqual(rag.chunk_text(""), [])

    def test_short_text_returns_single_chunk(self):
        text = "hello world"
        self.assertEqual(rag.chunk_text(text, chunk_size=800, overlap=150), [text])

    def test_long_text_produces_multiple_chunks(self):
        text = "a" * 2000
        chunks = rag.chunk_text(text, chunk_size=800, overlap=150)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 800)

    def test_overlap_shares_content_between_chunks(self):
        text = "0123456789" * 200  # 2000 chars
        chunks = rag.chunk_text(text, chunk_size=800, overlap=150)
        # end of chunk[0] should reappear at the start of chunk[1]
        self.assertEqual(chunks[0][-150:], chunks[1][:150])

    def test_covers_full_text(self):
        text = "x" * 1700
        chunks = rag.chunk_text(text, chunk_size=800, overlap=150)
        self.assertTrue("".join(chunks).endswith("x" * 100))
        self.assertEqual(chunks[-1][-1], "x")


class TestCosineSimilarity(unittest.TestCase):
    def test_identical_vectors(self):
        a = np.array([1.0, 2.0, 3.0])
        self.assertAlmostEqual(rag.cosine_similarity(a, a), 1.0)

    def test_orthogonal_vectors(self):
        a = np.array([1.0, 0.0])
        b = np.array([0.0, 1.0])
        self.assertAlmostEqual(rag.cosine_similarity(a, b), 0.0)

    def test_opposite_vectors(self):
        a = np.array([1.0, 0.0])
        b = np.array([-1.0, 0.0])
        self.assertAlmostEqual(rag.cosine_similarity(a, b), -1.0)

    def test_zero_vector_returns_zero(self):
        a = np.array([0.0, 0.0])
        b = np.array([1.0, 1.0])
        self.assertEqual(rag.cosine_similarity(a, b), 0.0)


class TestRankChunks(unittest.TestCase):
    def setUp(self):
        rag._INDEX_CHUNKS = [
            {"source": "a.txt", "text": "close match"},
            {"source": "b.txt", "text": "far match"},
            {"source": "c.txt", "text": "irrelevant"},
        ]
        # Deliberately spread apart: these tests are about score ordering and the threshold, so
        # the vectors must not also be near-duplicates of each other or MMR_MAX_SIMILARITY would
        # be the thing under test. Diversity has its own case in TestResultDiversity.
        rag._INDEX_VECTORS = np.array([
            [1.0, 0.0],
            [0.7, 0.7],
            [-1.0, 0.0],
        ])

    def tearDown(self):
        rag._INDEX_CHUNKS = []
        rag._INDEX_VECTORS = None

    def test_returns_top_k_sorted_descending(self):
        results = rag.rank_chunks([1.0, 0.0], threshold=-1.0, top_k=2)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["source"], "a.txt")

    def test_excludes_below_threshold(self):
        results = rag.rank_chunks([1.0, 0.0], threshold=0.5, top_k=3)
        sources = [c["source"] for c in results]
        self.assertNotIn("c.txt", sources)

    def test_no_index_returns_empty(self):
        rag._INDEX_CHUNKS = []
        rag._INDEX_VECTORS = None
        self.assertEqual(rag.rank_chunks([1.0, 0.0]), [])


class TestPrimerCap(unittest.TestCase):
    """Measured after the reindex: one dense on-brand fact per line is the ideal shape for this
    embedder, so primer lines took all three slots on "what is DEVCON?" and crowded out the
    document that answered the specific question. Barring it entirely overcorrected."""

    def setUp(self):
        rag._INDEX_CHUNKS = [
            {"source": rag.PRIMER_SOURCE, "text": "primer line one"},
            {"source": rag.PRIMER_SOURCE, "text": "primer line two"},
            {"source": rag.PRIMER_SOURCE, "text": "primer line three"},
            {"source": "programs.md", "text": "the specific answer"},
        ]
        # Spread apart for the same reason as TestRankChunks — the primer cap is what is under
        # test here, not the diversity filter. Score order stays primer one > two > answer >
        # three, and every pair sits under MMR_MAX_SIMILARITY.
        rag._INDEX_VECTORS = np.array([[1.0, 0.0], [0.9, 0.436], [0.3, 0.954], [0.6, 0.8]])

    def tearDown(self):
        rag._INDEX_CHUNKS = []
        rag._INDEX_VECTORS = None

    def test_primer_keeps_its_capped_slots_and_gives_the_rest_away(self):
        results = rag.rank_chunks([1.0, 0.0], threshold=-1.0, top_k=3)
        sources = [c["source"] for c in results]
        self.assertEqual(sources.count(rag.PRIMER_SOURCE), rag.PRIMER_MAX_IN_RANKING)
        self.assertIn("the specific answer", [c["text"] for c in results])

    def test_the_slot_goes_to_the_primers_best_line(self):
        # Applied after the sort: the cap must not drop the primer to wherever top_k lands.
        results = rag.rank_chunks([1.0, 0.0], threshold=-1.0, top_k=3)
        self.assertEqual(results[0]["text"], "primer line one")

    def test_the_last_resort_injection_uses_its_own_cap(self):
        # primer_chunks() bypasses ranking — when it fires, retrieval has already found nothing
        # and there is nothing left to crowd out, so PRIMER_MAX_CHUNKS governs and
        # PRIMER_MAX_IN_RANKING must not. This used to assert the injection was simply LARGER,
        # which stopped saying anything when PRIMER_MAX_CHUNKS came down to 2 (a Q&A entry is
        # several times the size of the one-line primer facts the old number was chosen for) and
        # the two knobs happened to meet. Asserting which knob applies is the real invariant.
        self.assertEqual(len(rag.primer_chunks()), rag.PRIMER_MAX_CHUNKS)

    def test_the_primer_is_matched_by_section_as_well_as_filename(self):
        # The primer moved from its own file into a section of the one knowledge base; a
        # filename-only test silently matched nothing and took the whole failsafe offline.
        rag._INDEX_CHUNKS = [{"source": "devcon_faq_rag.md", "section": rag.PRIMER_SECTION,
                              "text": "DEVCON is a non-profit."}]
        self.assertEqual(len(rag.primer_chunks()), 1)


class TestResultDiversity(unittest.TestCase):
    """Measured on the 505-chunk index: 1347 chunk pairs sit above cosine 0.97, and 4 of the 16
    eval queries returned two chunks above 0.90 similar to each other. At TOP_K=3 in a 1024-token
    context, a chunk that restates its neighbour costs a third of everything Kai gets to see."""

    def setUp(self):
        rag._INDEX_CHUNKS = [
            {"source": "a.md", "text": "DEVCON has 11 active chapters nationwide"},
            {"source": "a.md", "text": "DEVCON has eleven active chapters nationwide"},
            {"source": "b.md", "text": "DEVCON was founded in 2009"},
        ]
        # First two are restatements (cos 0.999); the third is a different fact.
        rag._INDEX_VECTORS = np.array([[1.0, 0.0], [0.999, 0.045], [0.6, 0.8]])

    def tearDown(self):
        rag._INDEX_CHUNKS = []
        rag._INDEX_VECTORS = None

    def test_a_restatement_does_not_spend_a_slot(self):
        results = rag.rank_chunks([1.0, 0.0], threshold=-1.0, top_k=3)
        texts = [c["text"] for c in results]
        self.assertIn("DEVCON has 11 active chapters nationwide", texts)
        self.assertNotIn("DEVCON has eleven active chapters nationwide", texts)

    def test_the_freed_slot_goes_to_a_different_fact(self):
        results = rag.rank_chunks([1.0, 0.0], threshold=-1.0, top_k=2)
        self.assertEqual([c["text"] for c in results][1], "DEVCON was founded in 2009")

    def test_distinct_chunks_are_all_kept(self):
        # Mutually far apart — 0.0 to 0.707 pairwise, nowhere near MMR_MAX_SIMILARITY.
        rag._INDEX_VECTORS = np.array([[1.0, 0.0], [0.7, 0.7], [0.0, 1.0]])
        self.assertEqual(len(rag.rank_chunks([1.0, 0.0], threshold=-1.0, top_k=3)), 3)


class TestLexicalRescue(unittest.TestCase):
    """The lexical layer was fallback-only, so it never fired on the queries it is best at —
    dense always returns something. Measured: "what is DEVCON CREST?" missed a chunk containing
    the only occurrence of "crest" in 505. Answer-in-context accuracy 83% -> 92%."""

    CHUNKS = [
        {"source": "primer.txt", "text": "DEVCON is a developer community"},
        {"source": "a.md", "text": "DEVCON runs many programmes nationwide"},
        {"source": "b.md", "text": "DEVCON CREST is the research arm"},
    ]

    def setUp(self):
        # On filler, so "crest" is genuinely rare and clears LEXICAL_MAX_DF_RATIO the way it does
        # in the real corpus (1 chunk in 505).
        _install_with_filler(self.CHUNKS, [[1.0, 0.0], [0.7, 0.7], [0.0, 1.0]])

    def tearDown(self):
        _install_index([], [])

    def _chunk(self, needle):
        # _install_with_filler prepends filler, so index positions are not the fixture's own.
        return next(c for c in rag._INDEX_CHUNKS if needle in c["text"])

    def test_a_rare_token_hit_displaces_the_weakest_dense_chunk(self):
        dense = [self._chunk("developer community"), self._chunk("many programmes")]
        out = rag.lexical_rescue("what is DEVCON CREST?", dense)
        texts = [c["text"] for c in out]
        self.assertIn("DEVCON CREST is the research arm", texts)
        self.assertEqual(len(out), len(dense))          # slot displaced, not appended
        self.assertIn("DEVCON is a developer community", texts)   # strongest dense kept

    def test_no_rare_token_leaves_the_dense_result_untouched(self):
        dense = [self._chunk("developer community"), self._chunk("many programmes")]
        self.assertEqual(rag.lexical_rescue("what is DEVCON?", dense), dense)

    def test_a_chunk_dense_already_found_is_not_duplicated(self):
        dense = [self._chunk("CREST"), self._chunk("developer community")]
        out = rag.lexical_rescue("what is DEVCON CREST?", dense)
        self.assertEqual([c["text"] for c in out].count("DEVCON CREST is the research arm"), 1)

    def test_empty_dense_result_is_left_to_the_fallback_chain(self):
        # retrieve_context only calls this when dense found something; the lexical-only fallback
        # below it is a separate layer and must stay that way.
        self.assertEqual(rag.lexical_rescue("what is DEVCON CREST?", []), [])


class TestStaleIndexWarning(unittest.TestCase):
    """Silent staleness is this module's worst failure — Kai keeps answering fluently from
    documents that have since been corrected. Advisory only: it must never block a load."""

    def test_a_matching_manifest_is_silent(self):
        real = {p.name: [p.stat().st_size, int(p.stat().st_mtime)]
                for p in rag.DOCUMENTS_DIR.iterdir()
                if p.is_file() and p.suffix.lower() in {".txt", ".md", ".pdf"}}
        with patch("builtins.print") as printed:
            rag._warn_if_stale(real)
        printed.assert_not_called()

    def test_a_changed_document_is_reported(self):
        stale = {"kai_facts.txt": [1, 1]}
        with patch("builtins.print") as printed:
            rag._warn_if_stale(stale)
        self.assertTrue(printed.called)
        self.assertIn("stale", printed.call_args[0][0])

    def test_an_index_without_a_manifest_skips_the_check(self):
        with patch("builtins.print") as printed:
            rag._warn_if_stale(None)
        printed.assert_not_called()


class TestFormatContext(unittest.TestCase):
    def test_empty_chunks_returns_empty_string(self):
        self.assertEqual(rag.format_context([]), "")

    def test_includes_source_and_text(self):
        out = rag.format_context([{"source": "doc.txt", "text": "the answer is 42"}])
        self.assertIn("doc.txt", out)
        self.assertIn("the answer is 42", out)

    def test_instructions_prioritize_documents_and_keep_kai_voice(self):
        # Both halves matter: without the first the model answers DEVCON questions from
        # pretraining, without the second it recites the chunk and loses the persona.
        out = rag.format_context([{"source": "doc.txt", "text": "x"}]).lower()
        self.assertIn("answer from them", out)
        self.assertIn("kai's own voice", out)

    def test_instructions_come_after_the_facts(self):
        # Position, not decoration — gemma2 has no system role, so the last lines before the
        # question are the strongest slot available. See format_context.
        out = rag.format_context([{"source": "doc.txt", "text": "the answer is 42"}])
        self.assertLess(out.index("the answer is 42"), out.index("Answer from them"))

    def test_labels_a_chunk_by_section_when_it_has_one(self):
        # One file holds the whole corpus now, so the filename says nothing; the section does.
        out = rag.format_context([{"source": "devcon_faq_rag.md", "section": "Chapters",
                                   "text": "x"}])
        self.assertIn("(Chapters)", out)


class TestEmbedBatch(unittest.TestCase):
    def test_calls_fastembed_model(self):
        mock_model = MagicMock()
        mock_model.embed.return_value = [np.array([0.1, 0.2])]
        rag._embed_model = mock_model
        result = rag.embed_batch(["hello"])
        self.assertEqual(result, [[0.1, 0.2]])
        mock_model.embed.assert_called_once_with(["hello"])
        rag._embed_model = None


class TestEmbedQuery(unittest.TestCase):
    def test_applies_query_prefix(self):
        with patch("ai.rag.embed_batch", return_value=[[1.0, 2.0]]) as mock_embed:
            result = rag.embed_query("what is Kai?")
        self.assertEqual(result, [1.0, 2.0])
        mock_embed.assert_called_once_with(["search_query: what is Kai?"])

    def test_returns_none_on_failure(self):
        with patch("ai.rag.embed_batch", side_effect=RuntimeError("boom")):
            self.assertIsNone(rag.embed_query("hello"))


class TestLoadIndex(unittest.TestCase):
    def tearDown(self):
        rag._INDEX_CHUNKS = []
        rag._INDEX_VECTORS = None

    def test_loads_well_formed_index(self, ):
        data = {"chunks": [{"source": "a.txt", "text": "hi", "embedding": [1.0, 2.0]}]}
        with patch("ai.rag.INDEX_PATH") as mock_path:
            mock_path.read_text.return_value = json.dumps(data)
            rag.load_index()
        self.assertEqual(len(rag._INDEX_CHUNKS), 1)
        self.assertEqual(rag._INDEX_VECTORS.shape, (1, 2))

    def test_missing_file_leaves_empty_cache(self):
        with patch("ai.rag.INDEX_PATH") as mock_path:
            mock_path.read_text.side_effect = FileNotFoundError()
            rag.load_index()
        self.assertEqual(rag._INDEX_CHUNKS, [])
        self.assertIsNone(rag._INDEX_VECTORS)


class TestRetrieveContext(unittest.TestCase):
    def tearDown(self):
        rag._INDEX_CHUNKS = []
        rag._INDEX_VECTORS = None

    def test_no_index_returns_empty_string(self):
        rag._INDEX_CHUNKS = []
        rag._INDEX_VECTORS = None
        with patch("ai.rag.embed_query", return_value=[1.0, 0.0]):
            self.assertEqual(rag.retrieve_context("anything"), "")

    def test_embedding_failure_returns_empty_string(self):
        with patch("ai.rag.embed_query", side_effect=RuntimeError("boom")):
            self.assertEqual(rag.retrieve_context("anything"), "")

    def test_embed_query_none_returns_empty_string(self):
        with patch("ai.rag.embed_query", return_value=None):
            self.assertEqual(rag.retrieve_context("anything"), "")

    def test_query_is_canonicalized_before_embedding(self):
        # The point of the whole alias path: a mistranscribed brand name must be embedded as the
        # spelling the documents use, or every score falls under SIMILARITY_THRESHOLD.
        with patch("ai.rag.embed_query", return_value=None) as mock_embed:
            rag.retrieve_context("what does def con do?")
        mock_embed.assert_called_once_with("what does DEVCON do?")

    def test_relevant_query_returns_formatted_context(self):
        rag._INDEX_CHUNKS = [{"source": "a.txt", "text": "Kai is a robot"}]
        rag._INDEX_VECTORS = np.array([[1.0, 0.0]])
        with patch("ai.rag.embed_query", return_value=[1.0, 0.0]):
            result = rag.retrieve_context("who is Kai?")
        self.assertIn("Kai is a robot", result)


class TestFollowUpExpansion(unittest.TestCase):
    """A follow-up must carry its subject into the embedded query, and nothing else may."""

    def test_pronoun_turn_is_expanded_with_previous_turn(self):
        with patch("ai.rag.embed_query", return_value=None) as mock_embed:
            rag.retrieve_context("who runs it?", previous_user_text="Tell me about DEVCON Kids.")
        mock_embed.assert_called_once_with("Tell me about DEVCON Kids. who runs it?")

    def test_topic_change_is_not_expanded(self):
        # No pronoun -> a new subject. Dragging the last one in would pull the whole DEVCON
        # corpus into a question that has nothing to do with it.
        with patch("ai.rag.embed_query", return_value=None) as mock_embed:
            rag.retrieve_context("what's the weather like today?",
                                 previous_user_text="What is DEVCON Philippines?")
        mock_embed.assert_called_once_with("what's the weather like today?")

    def test_first_turn_has_nothing_to_expand_with(self):
        with patch("ai.rag.embed_query", return_value=None) as mock_embed:
            rag.retrieve_context("who runs it?")
        mock_embed.assert_called_once_with("who runs it?")

    def test_both_turns_are_canonicalized_when_expanded(self):
        # The brand name can be mistranscribed in either half, and the previous turn is where the
        # subject actually lives — leaving it as Whisper spelled it defeats the expansion.
        with patch("ai.rag.embed_query", return_value=None) as mock_embed:
            rag.retrieve_context("what does it do?", previous_user_text="tell me about defcon")
        mock_embed.assert_called_once_with("tell me about DEVCON what does it do?")

    def test_points_backwards(self):
        for text in ("who runs it?", "What did he say?", "sino sila?", "And that one?",
                     "What do they do exactly?"):
            with self.subTest(text=text):
                self.assertTrue(rag.points_backwards(text))
        for text in ("What is DEVCON Philippines?", "Kumusta ka na?", "what's the weather?",
                     "Tell me a joke.", ""):
            with self.subTest(text=text):
                self.assertFalse(rag.points_backwards(text))


def _filler(n=12):
    """Background chunks. IDF is a property of the corpus, not of one chunk, so a two-chunk
    fixture makes every word look rare and tests nothing real — these give the common words a
    document frequency high enough to weigh nothing, which is the premise lexical_rank rests on.
    All orthogonal to the [1.0, 0.0] query vector the tests use, so they never match by cosine."""
    return [{"source": f"style{i}.md",
             "text": f"This is a page about the brand and the tone of voice in post {i}"}
            for i in range(n)]


def _install_index(chunks, vectors, entities=()):
    """Load a fake index the way load_index would — including the derived lexical tables, which
    the older tests in this file predate and set up implicitly by leaving empty."""
    rag._INDEX_CHUNKS = chunks
    rag._INDEX_VECTORS = np.array(vectors, dtype=np.float64) if len(chunks) else None
    rag._INDEX_ENTITIES = list(entities)
    rag._build_lexical_tables()


def _install_with_filler(chunks, vectors, entities=()):
    """`chunks` on top of _filler(), so document frequency behaves like a real corpus."""
    filler = _filler()
    _install_index(filler + list(chunks),
                   [[0.0, 1.0]] * len(filler) + list(vectors), entities)


class TestLexicalRank(unittest.TestCase):
    """The non-semantic half of retrieval: rare literal tokens, which is exactly what a dense
    embedder smooths away."""

    def setUp(self):
        _install_with_filler(
            [{"source": "chapters.md", "text": "The Iligan chapter runs workshops in Mindanao"},
             {"source": "about.md", "text": "DEVCON is a volunteer community of developers"}],
            [[1.0, 0.0], [0.0, 1.0]],
        )

    def tearDown(self):
        _install_index([], np.empty((0, 2)))

    def test_rare_token_finds_its_chunk(self):
        results = rag.lexical_rank("what happens in Iligan?")
        self.assertEqual([c["source"] for c in results], ["chapters.md"])

    def test_query_of_only_common_words_matches_nothing(self):
        # No rare token means nothing to be found by. Returning "the" chunk would be worse than
        # returning none — dense retrieval is the right tool for a paraphrase.
        self.assertEqual(rag.lexical_rank("is this a page about the brand"), [])

    def test_empty_index_returns_empty(self):
        _install_index([], np.empty((0, 2)))
        self.assertEqual(rag.lexical_rank("Iligan"), [])


class TestFailsafeChain(unittest.TestCase):
    """A turn that is provably about DEVCON must never come back with "" — that is the state in
    which the model answers from pretraining. An unrelated turn must still come back with ""."""

    PRIMER = {"source": rag.PRIMER_SOURCE, "text": "DEVCON Philippines is a non-profit."}

    def setUp(self):
        rag.reset_topic()
        # Filler only: orthogonal to every query vector the tests supply, and sharing no rare
        # token with the queries, so neither the dense nor the lexical layer can fire unless a
        # test adds a chunk that should make it.
        _install_with_filler([], [])

    def tearDown(self):
        rag.reset_topic()
        _install_index([], np.empty((0, 2)))

    def test_brand_turn_falls_back_to_a_weak_match(self):
        # 0.4 cosine: under SIMILARITY_THRESHOLD, over FALLBACK_THRESHOLD. Ordinarily discarded;
        # on a brand-flagged turn a weak answer beats an invented one.
        _install_with_filler([{"source": "about.md", "text": "chapters everywhere"}],
                             [[0.4, 0.9165]])
        with patch("ai.rag.embed_query", return_value=[1.0, 0.0]):
            result = rag.retrieve_context("what is defcon?")
        self.assertIn("chapters everywhere", result)

    def test_unrelated_turn_with_a_weak_match_still_returns_nothing(self):
        # Same index, same scores — only the flag differs. The gate is the whole design.
        _install_with_filler([{"source": "about.md", "text": "chapters everywhere"}],
                             [[0.4, 0.9165]])
        with patch("ai.rag.embed_query", return_value=[1.0, 0.0]):
            self.assertEqual(rag.retrieve_context("what's the weather like?"), "")

    def test_brand_turn_falls_back_to_the_primer(self):
        # lexical_rank is stubbed out to isolate the layer under test: the primer says "DEVCON",
        # so on a brand question the lexical layer would legitimately find it first, and then
        # this test would pass without the primer layer existing at all.
        _install_with_filler([self.PRIMER], [[0.0, 1.0]])
        with patch("ai.rag.embed_query", return_value=[1.0, 0.0]), \
             patch("ai.rag.lexical_rank", return_value=[]):
            result = rag.retrieve_context("who founded dev com?")
        self.assertIn("DEVCON Philippines is a non-profit.", result)

    def test_brand_turn_with_no_primer_returns_the_dont_guess_notice(self):
        with patch("ai.rag.embed_query", return_value=[1.0, 0.0]):
            result = rag.retrieve_context("what is devcon?")
        self.assertEqual(result, rag.NO_CONTEXT_NOTICE)

    def test_notice_survives_a_completely_broken_index(self):
        # Worst case on the actual robot: someone deletes .rag_index.json. The brand question is
        # the one that still must not be answered from pretraining.
        _install_index([], np.empty((0, 2)))
        with patch("ai.rag.embed_query", return_value=[1.0, 0.0]):
            self.assertEqual(rag.retrieve_context("tell me about devcon"), rag.NO_CONTEXT_NOTICE)

    def test_unrelated_turn_is_unaffected_by_the_whole_chain(self):
        with patch("ai.rag.embed_query", return_value=[1.0, 0.0]):
            self.assertEqual(rag.retrieve_context("what time is it?"), "")

    def test_lexical_layer_runs_before_the_failsafes(self):
        # A retrievable answer must win over the primer — the failsafes are a floor, not a
        # shortcut. Orthogonal vector, so only the lexical layer can find this.
        _install_with_filler([{"source": "chapters.md", "text": "There is a chapter in Iligan"},
                              self.PRIMER],
                             [[0.0, 1.0], [0.0, 1.0]])
        with patch("ai.rag.embed_query", return_value=[1.0, 0.0]):
            result = rag.retrieve_context("does devcon have a chapter in Iligan?")
        self.assertIn("There is a chapter in Iligan", result)


class TestGazetteerExpansion(unittest.TestCase):
    def setUp(self):
        rag.reset_topic()
        _install_index([{"source": "programs.md", "text": "filler"}], [[0.0, 1.0]],
                       entities=["DEVCON Kids"])

    def tearDown(self):
        rag.reset_topic()
        _install_index([], np.empty((0, 2)))

    def test_program_name_is_canonicalized_into_the_query(self):
        with patch("ai.rag.embed_query", return_value=None) as mock_embed:
            rag.retrieve_context("what is dev con kits?")
        # The brand matcher repairs "dev con" and leaves "kits" mangled; only the gazetteer can
        # put the name the documents actually use into the embedded query.
        self.assertIn("DEVCON Kids", mock_embed.call_args[0][0])

    def test_program_name_alone_flags_the_turn(self):
        # No DEVCON token anywhere in this question — the gazetteer is the only thing that can
        # tell it is on-topic, and without the flag it would fall through to "".
        _install_index([], np.empty((0, 2)), entities=["Geeks on a Beach"])
        with patch("ai.rag.embed_query", return_value=[1.0, 0.0]):
            self.assertEqual(rag.retrieve_context("when is geeks on the beach?"),
                             rag.NO_CONTEXT_NOTICE)


class TestStickyTopic(unittest.TestCase):
    """Follow-ups that carry no pronoun and no brand — "how many chapters?" — which the anaphora
    path cannot see and which retrieve nothing on their own."""

    def setUp(self):
        rag.reset_topic()
        _install_index([{"source": "chapters.md", "text": "DEVCON has 11 active chapters"}],
                       [[1.0, 0.0]])

    def tearDown(self):
        rag.reset_topic()
        _install_index([], np.empty((0, 2)))

    @staticmethod
    def _embedder(query):
        """Only a query naming the brand lands on the chunk; anything else is orthogonal."""
        return [1.0, 0.0] if "devcon" in query.casefold() else [0.0, 1.0]

    def test_pronoun_free_followup_is_retried_with_the_brand(self):
        with patch("ai.rag.embed_query", side_effect=self._embedder):
            self.assertIn("11 active chapters", rag.retrieve_context("what is DEVCON?"))
            result = rag.retrieve_context("how many active ones are there?")
        self.assertIn("11 active chapters", result)

    def test_sticky_expires(self):
        with patch("ai.rag.embed_query", side_effect=self._embedder):
            rag.retrieve_context("what is DEVCON?")
            for _ in range(rag.STICKY_TURNS):
                rag.retrieve_context("something else entirely")
            self.assertEqual(rag.retrieve_context("and now for something different"), "")

    def test_reset_topic_forgets_the_subject(self):
        # A new person walks up: reset_history() clears this with the conversation.
        with patch("ai.rag.embed_query", side_effect=self._embedder):
            rag.retrieve_context("what is DEVCON?")
            rag.reset_topic()
            self.assertEqual(rag.retrieve_context("how many active ones are there?"), "")


if __name__ == '__main__':
    unittest.main()
