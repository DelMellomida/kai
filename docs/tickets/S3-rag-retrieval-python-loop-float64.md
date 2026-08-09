# S3 — RAG retrieval is a Python loop over float64 vectors, from a JSON index

| | |
|---|---|
| **Tier** | 2 |
| **Severity** | Medium |
| **Effort** | Medium |
| **Confidence** | High |
| **Lens** | Software |

## Location

- `ai/rag.py` — `load_index()` (`np.array([c["embedding"] for c in chunks], dtype=np.float64)`),
  `rank_chunks()`, `cosine_similarity()`, `INDEX_PATH`
- `ai/index_documents.py` — `main()`, which writes `documents/.rag_index.json` including every
  embedding
- `ai/voice_assistant.py` — `_call_ollama()`, which calls `rag.retrieve_context()` on every turn
  inside the latency budget

## Problem

Three separate inefficiencies stack in the same path.

1. **`dtype=np.float64`.** fastembed produces float32; `load_index` widens every vector to float64,
   doubling resident memory for no precision that matters to a cosine ranking.
2. **Python-level scoring.** `rank_chunks` builds a list comprehension calling `cosine_similarity`
   once per chunk. Each call recomputes `np.linalg.norm(q)` — for the *same* query vector — plus
   `np.linalg.norm(vec)` for a stored vector that could have been normalised once at load. The MMR
   diversity pass then does another O(k·n) of the same function against the picked vectors.
3. **JSON as the index format.** `documents/.rag_index.json` stores every embedding as decimal text.
   `load_index()` parses the whole thing on every startup, and the parse allocates a Python list of
   floats per chunk before the array is built.

## Why it matters

Retrieval runs on every turn, inside the latency budget that R5 is separately about — so the
scoring cost is paid by the user directly. The load cost and the memory cost land at startup on an
8 GB shared-memory board, in the same window where Ollama is deciding its CPU/GPU split from free
memory (see **R6**): a fatter, slower index load makes a partial GPU offload more likely, and that
costs every turn for the rest of the run.

None of this is currently a *correctness* problem, and at a few hundred chunks none of it is
catastrophic — but all three get worse linearly as `documents/` grows, and the fix is well-understood.

## Acceptance criteria

- [ ] Stored vectors are float32 and L2-normalised at index build time; `load_index()` performs no
      per-chunk widening or normalisation.
- [ ] `rank_chunks()` scores with a single matrix-vector product (`M @ q_hat`) plus a vectorised
      threshold/selection (`np.argpartition` or equivalent) — no Python loop calling
      `cosine_similarity` per chunk.
- [ ] `cosine_similarity()` is retained for the MMR comparison (and any external caller), but MMR
      operates on the already-normalised rows so it reduces to a dot product.
- [ ] `SOURCE_BOOST`/`SECTION_BOOST` are still applied **before** the threshold, not after ranking —
      the existing comment states this is deliberate (a boosted source must be able to *enter* the
      results, not just reorder them) and the vectorised form must preserve it.
- [ ] `PRIMER_MAX_IN_RANKING` capping and `MMR_MAX_SIMILARITY` de-duplication produce the same
      selections as today for a fixed index and query set.
- [ ] **Retrieval output is unchanged**: `python3 -m scripts.rag_eval` and
      `python3 -m scripts.rag_accuracy` produce identical chunk selections and scores (within
      float32 tolerance) before and after.
- [ ] Embeddings are stored in a `.npy` sidecar; the JSON keeps chunk text and metadata only.
      `load_index()` remains fail-open — a missing or mismatched sidecar yields an empty cache and a
      logged warning, never an exception.
- [ ] The sidecar and the JSON are version-checked against each other (row count and dimension), so
      a half-updated pair is detected rather than silently mis-indexed.
- [ ] Measured and recorded in the code comment: index load time, resident index size, and
      per-query ranking time, before and after.
- [ ] Existing `tests/test_rag.py` cases pass unchanged; a new case covers the sidecar/JSON
      mismatch path.

## Suggested approach

**Build side** (`ai/index_documents.py`): after `rag.embed_batch()`, stack the embeddings into an
`(n, d)` float32 array, L2-normalise the rows, and `np.save()` it next to the JSON (e.g.
`documents/.rag_index.npy`). Drop the per-chunk `"embedding"` key from the JSON — it is the bulk of
the file — and add `{"vectors": "<filename>", "rows": n, "dim": d}` to the manifest so the loader
can validate.

**Load side** (`ai/rag.py`): read the JSON for chunks/entities/manifest as today, then `np.load` the
sidecar with `mmap_mode=None` (the array is small enough to want resident) and assign to
`_INDEX_VECTORS`. Validate `rows`/`dim` against `len(_INDEX_CHUNKS)` and the embedder's dimension;
on any mismatch, log and fall back to the empty-cache path that already exists.

**Query side** (`rank_chunks`):

```
# sketch
q = np.asarray(query_vec, dtype=np.float32)
q /= (np.linalg.norm(q) or 1.0)
scores = _INDEX_VECTORS @ q                  # rows already normalised -> cosine
scores = scores + _BOOSTS                    # precomputed per-chunk boost vector, built at load
idx = np.nonzero(scores >= threshold)[0]
idx = idx[np.argsort(-scores[idx])]
```

then walk `idx` with the existing primer-cap and MMR loop, which stays in Python because it is
inherently sequential and only ever touches a handful of rows.

Precompute the `_BOOSTS` vector in `_build_lexical_tables()` (or a sibling) so `source_boost()` is
not called per chunk per query.

**Backwards compatibility**: keep `load_index()` able to read a legacy JSON that still carries
`"embedding"` keys, logging a "reindex for the faster format" warning — the same advisory shape
`_warn_if_stale()` already uses. That way a robot with an old index still answers.
