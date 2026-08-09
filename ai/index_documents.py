#!/usr/bin/env python3
"""
Build the RAG index from documents/.

Usage, from the project root:
  python3 -m ai.index_documents

Must be run as a module from the root, not as `python3 ai/index_documents.py` — the imports below
are absolute (`from ai import rag`), so running the file directly puts ai/ on sys.path instead of
the project root and the package cannot be found.

Drop .txt/.md/.pdf files into documents/, then run this whenever files are added or changed.
Rebuilds documents/.rag_index.json from scratch each time (no incremental diffing).
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from ai import rag
from ai.query_alias import mentions_devcon
from ai.wake_phrase import normalize_tokens
from config.rag import (
    GAZETTEER_MAX_DF_ABS, GAZETTEER_MAX_DF_RATIO, GAZETTEER_MAX_TOKENS,
    GAZETTEER_MIN_CONTENT_WORDS, GAZETTEER_MIN_DF_ABS, GAZETTEER_MIN_WORD_LEN,
)

SUPPORTED_SUFFIXES = {".txt", ".md", ".pdf"}


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md"):
        return path.read_text(errors="ignore")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("Reading .pdf files requires pypdf — run: pip3 install pypdf")
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise ValueError(f"Unsupported file type: {suffix}")


# A block that is nothing but a section heading — an ATX heading ("## DEVCON Kids") or a
# lone bold line ("**DEVCON Nationwide Chapters**"), which the source markdown uses the same way.
_ATX_RE       = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BOLD_ONLY_RE = re.compile(r"^\*\*(.+?)\*\*:?$")


def _heading_of(block: str) -> tuple[str, int] | None:
    """(heading text, depth) if `block` is only a heading line, else None."""
    if "\n" in block:
        return None
    m = _ATX_RE.match(block)
    if m:
        return m.group(2).strip(), len(m.group(1))
    m = _BOLD_ONLY_RE.match(block)
    if m:
        return m.group(1).strip(), 7   # bold headings nest under any ATX level
    return None


# Noise in the filenames that is not part of the document's title: version stamps and the
# duplicate-download suffix ("(v1 July 9 2026)", "(1)").
_FILENAME_NOISE_RE = re.compile(r"\s*\((?:v\d[^)]*|\d+)\)")


def document_title(path: Path) -> str:
    """A human-readable title for `path`, from its filename. "Pioneering_Programs_-_DEVCON_PH.md"
    -> "Pioneering Programs - DEVCON PH".

    Used as a per-chunk embedding prefix (see main). documents/ is named descriptively enough
    that this is free provenance; nothing here has to be right for indexing to work, it only has
    to be closer to the truth than nothing."""
    stem = _FILENAME_NOISE_RE.sub("", path.stem)
    # Whichever separator the file actually uses. Splitting on both would break "AI-Ready".
    stem = stem.replace("_", " ") if "_" in stem else stem.replace("-", " ")
    return " ".join(stem.split())


# ── Q&A knowledge-base entries (documents/devcon_faq_rag.md) ──────────────────
# The corpus is now one file of ~50 entries shaped like this:
#
#     ---
#     id: about-history
#     section: About DEVCON
#     synonyms: [origin, founding, how it began, PSIA, first event]
#     ---
#     **Q: When and how did DEVCON start?**
#     A: DEVCON began in 2009 as a project of ...
#
# Left to the generic chunker that block is one chunk of literal text, which is wrong twice over.
# The `id:`/`synonyms:` lines go into the EMBEDDING as noise the question will never match, and
# they also go into the PROMPT, where gemma2:2b has to be told to ignore YAML it can see. Both
# jobs want the opposite split: the synonyms are the single most valuable thing here for
# retrieval (they are literally the paraphrases a visitor will speak) and worthless for
# generation, while the Q&A pair is what the model should see and nothing else.
#
# So an entry is parsed once and stored twice: `embed` (section + question + synonyms + answer)
# is what gets vectorized, `text` (the bare Q&A) is what reaches the prompt. This is the same
# trick main() already plays with the document title, generalized.
_ENTRY_RE = re.compile(r"^---\s*\n(?P<meta>.*?)\n---\s*\n(?P<body>.*)$", re.DOTALL)
_META_RE  = re.compile(r"^(?P<key>[a-z_]+):\s*(?P<value>.*)$")


def parse_entry(block: str) -> dict | None:
    """Parse one front-matter-delimited Q&A block, or None if `block` isn't one."""
    m = _ENTRY_RE.match(block.strip())
    if m is None:
        return None
    meta: dict[str, str] = {}
    for line in m.group("meta").splitlines():
        field = _META_RE.match(line.strip())
        if field:
            meta[field.group("key")] = field.group("value").strip()
    body = m.group("body").strip()
    if not body:
        return None
    # The Q line is written bold so the source file reads well; the asterisks are markdown Kai
    # must never speak, and tts.py is already stripping these downstream. Do it once, here.
    body = body.replace("**", "")
    synonyms = [s.strip() for s in meta.get("synonyms", "").strip("[]").split(",") if s.strip()]
    return {"text": body, "section": meta.get("section", ""),
            "entry_id": meta.get("id", ""), "synonyms": synonyms}


def chunk_entries(path: Path, text: str) -> list[dict] | None:
    """Split a Q&A knowledge base into per-entry chunks, or None if this file isn't one.

    Returns None rather than [] on a non-matching file so main() can tell "not this shape" from
    "this shape, no content" and fall back to chunk_file() for everything else — .pdf scans, and
    any prose document dropped into documents/ later."""
    if path.suffix.lower() not in (".txt", ".md"):
        return None
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    entries = [e for e in (parse_entry(b) for b in blocks) if e is not None]
    # A stray "---" fence in an ordinary markdown file could parse as one entry by accident. A
    # real knowledge base is mostly entries, so require a majority before claiming the file.
    if len(entries) < 3 or len(entries) * 2 < len(blocks):
        return None
    out: list[dict] = []
    for entry in entries:
        crumb = f"DEVCON PH > {entry['section']}" if entry["section"] else "DEVCON PH"
        # Long answers still get split; every piece keeps the entry's metadata, so a split
        # answer's second half is still findable by the question's synonyms.
        for piece in rag.chunk_text(entry["text"]):
            joined = ", ".join(entry["synonyms"])
            out.append({
                "text": piece,
                "section": entry["section"],
                "entry_id": entry["entry_id"],
                "embed": f"{crumb}\n{piece}" + (f"\nAlso asked as: {joined}" if joined else ""),
            })
    return out


def chunk_file(path: Path, text: str) -> tuple[list[str], list[str]]:
    """Split a document into chunks, and collect its headings for the gazetteer.

    Prose (.pdf) uses fixed-size chunking. Structured
    text/markdown knowledge files are split **per paragraph** (blank-line separated), or
    **per line** when there are no blank lines (e.g. one-fact-per-line files). A single
    blended chunk dilutes the embedding, so a specific question (e.g. "when is your
    birthday") scores below SIMILARITY_THRESHOLD and retrieves nothing; per-fact chunks
    each embed cleanly and clear the threshold for their own topic.

    Heading blocks are NOT indexed on their own — they are prepended as a breadcrumb to the
    content blocks beneath them. Indexed alone, a heading is a pure topic label with no fact in
    it, and it embeds closer to a short question than the paragraph that actually answers it:
    "when is your birthday?" retrieved '### Theme', '### When in doubt' and '### Other-platform
    quick rules' as its whole context, leaving the model to invent a date. The breadcrumb also
    repairs the opposite failure — a paragraph that says "with 10 active chapters" without
    naming DEVCON now carries "DEVCON PH > Nationwide Chapters" into its embedding.

    The heading text is returned alongside the chunks as the raw material for the gazetteer —
    see build_gazetteer, which is where the ordinary headings get filtered back out."""
    if path.suffix.lower() in (".txt", ".md"):
        blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
        if len(blocks) <= 1:
            blocks = [ln.strip() for ln in text.splitlines() if ln.strip()]
        pieces: list[str] = []
        headings: list[str] = []
        trail: list[tuple[str, int]] = []   # active heading breadcrumb, shallowest first
        for b in blocks:
            if b.strip("-—_* ") == "":      # horizontal rule / decorative separator
                continue
            heading = _heading_of(b)
            if heading is not None:
                while trail and trail[-1][1] >= heading[1]:
                    trail.pop()
                trail.append(heading)
                headings.append(heading[0])
                continue
            crumb = " > ".join(h for h, _depth in trail)
            pieces.extend(rag.chunk_text(f"{crumb}\n{b}" if crumb else b))  # cap over-long blocks
        return pieces, headings
    return rag.chunk_text(text), []


# Words a title is allowed to leave lowercase. Grammar, not vocabulary — a closed list that
# cannot go stale when documents/ changes, unlike a stoplist of content words would.
_MINOR_WORDS = frozenset({"a", "an", "the", "of", "for", "and", "or", "in", "on", "to", "at",
                          "with", "by", "from", "as", "is", "its"})


def _is_name_shaped(heading: str) -> bool:
    """True if `heading` is written like a name rather than like a sentence.

    Every word that is not a minor word must be capitalized: "DEVCON Code Camps" and "Movers and
    Shakers Award" pass, "When in doubt", "How to use this document" and "Before delivering:
    final checklist" do not. Blunt, but it is the distinction that matters here and the omnibus
    style guide is consistent about it — it capitalizes what it names and sentence-cases what it
    instructs."""
    words = [w for w in re.findall(r"[^\W_]+", heading, re.UNICODE)]
    content = [w for w in words if w.casefold() not in _MINOR_WORDS]
    return bool(content) and all(w[0].isupper() for w in content)


# Minor words a name may contain WITHOUT ending the run ("Geeks on a Beach", "Movers and
# Shakers Award"). Deliberately excludes "is": with it, "What is DEVCON Philippines?" parses as
# one four-word name, and every Q line in the knowledge base starts "What is ...".
_NAME_MINOR = r"(?:of|for|and|or|in|on|to|the|a|an|de)"
_NAME_RUN_RE = re.compile(
    r"\b[A-Z][\w.&/'-]*(?:\s+(?:" + _NAME_MINOR + r"|[A-Z][\w.&/'-]*)){1,4}")

# Runs are extracted per SENTENCE. The token pattern has to allow an internal period ("NMBLR.AI",
# "Inc."), which without a sentence split lets a run stride straight over a full stop and glue two
# names together: "...at the TechBar in Cebu City. It grew into..." yielded "TechBar in Cebu
# City. It", and "...Iligan. Bacolod, Bohol..." yielded "Iligan. Bacolod". Requiring whitespace
# after the stop keeps "Inc.," and "NMBLR.AI" intact, since neither is followed by a space.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Function words that only lead a run because they started a sentence — "Is DEVCON a non-profit?"
# is a question, not the name of anything, but "Is DEVCON" carries the brand and so bypasses the
# document-frequency filter downstream. Stripped from the front until a real word leads. Closed
# and grammatical, like _MINOR_WORDS: it cannot go stale when documents/ changes.
_RUN_OPENERS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "do", "does", "did", "can", "could", "will",
    "would", "should", "yes", "no", "not", "to", "it", "its", "this", "that", "these", "those",
    "and", "but", "so", "if", "for", "in", "on", "at", "with", "by", "from", "as", "what", "how",
    "why", "when", "where", "who", "which", "each", "every", "also", "they", "we", "you", "he",
    "she", "there", "their", "our", "your", "his", "her",
})


def name_candidates(text: str) -> list[str]:
    """Runs of capitalized words in `text` — raw material for the gazetteer.

    The gazetteer used to be harvested from markdown headings, which worked because documents/
    was six long structured documents whose section headings WERE the program names. The Q&A
    knowledge base has exactly one heading in the whole file, so that source dried up completely
    and the entity layer — the one that recognizes "Campus DEVCON" through Whisper mangling it —
    silently had nothing to match against.

    Capitalized runs are the replacement source. They are much noisier than headings, which is
    fine: build_gazetteer's document-frequency and name-shape filters were already built to
    reject exactly this kind of noise, and they are strictly better at it than a regex would be.
    This function's only job is to not MISS a name; deciding which candidates are real is still
    build_gazetteer's."""
    out: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        for match in _NAME_RUN_RE.finditer(sentence):
            words = match.group(0).strip(" .,;:").split()
            # A run may end on a minor word it swept up ("DEVCON Kids and") and may begin on a
            # word that is only capitalized because the sentence was ("Is DEVCON"). Neither
            # belongs to the name.
            while len(words) > 1 and re.fullmatch(_NAME_MINOR, words[-1]):
                words.pop()
            while len(words) > 1 and words[0].casefold() in _RUN_OPENERS:
                words.pop(0)
            if len(words) > 1:
                out.append(" ".join(words))
    return out


def build_gazetteer(headings: list[str], chunks: list[dict]) -> list[str]:
    """Keep the headings that name something, drop the ones that are just prose.

    Every heading in documents/ is a candidate — but the omnibus style guide alone contributes
    "Theme", "When in doubt", "Voice" and dozens more, and a gazetteer containing those would
    flag "what's the theme of your talk?" as a DEVCON question and expand the query with it.

    Two filters, and both are needed. Document frequency: a name is rare by definition, so a
    heading is kept only if its rarest content word sits under GAZETTEER_MAX_DF_RATIO. That alone
    was measured letting "When in doubt" and "Color palette (official, exact)" through — a style
    guide's section headings are rare words too. Name shape (_is_name_shaped) is the second half,
    and it is what actually separates a label from an instruction. Anything naming the brand
    skips the DF test: "Campus DEVCON" is an entity whatever its word counts say.

    Derived rather than hand-listed on purpose: a hand-written gazetteer would go stale silently
    on the next content drop, and silent staleness is the exact failure this layer exists to
    prevent.

    The DF ceiling is capped in absolute terms as well as proportionally — see
    GAZETTEER_MAX_DF_ABS for the measurement that forced this. A pure ratio relaxes as documents/
    grows, so the filter was at its weakest exactly when the corpus was at its most diverse."""
    if not chunks:
        return []
    df: dict[str, int] = {}
    for chunk in chunks:
        for word in {t.text for t in normalize_tokens(chunk["text"])}:
            df[word] = df.get(word, 0) + 1
    ceiling = max(GAZETTEER_MIN_DF_ABS,
                  min(int(len(chunks) * GAZETTEER_MAX_DF_RATIO), GAZETTEER_MAX_DF_ABS))

    out: list[str] = []
    seen: set[str] = set()
    for heading in headings:
        key = heading.casefold()
        if key in seen:
            continue
        # A trailing colon marks a form field, not a name: the chapter-stats submissions are
        # written as "Proudest Work/Milestone:" and "Other Year-End Reflections:", which are
        # prompts to whoever filled the form in. They are Title Case and their words are rare,
        # so both filters below pass them; the punctuation is what actually gives them away.
        if heading.rstrip().endswith(":"):
            continue
        words = [t.text for t in normalize_tokens(heading)]
        content = [w for w in words if len(w) >= GAZETTEER_MIN_WORD_LEN]
        if not words or len(words) > GAZETTEER_MAX_TOKENS or not content:
            continue
        if not _is_name_shaped(heading):
            continue
        if len(content) < GAZETTEER_MIN_CONTENT_WORDS and not mentions_devcon(heading):
            continue
        if mentions_devcon(heading) or min(df.get(w, 0) for w in content) <= ceiling:
            seen.add(key)
            out.append(heading)
    return out


def main() -> None:
    rag.DOCUMENTS_DIR.mkdir(exist_ok=True)
    files = sorted(
        p for p in rag.DOCUMENTS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )
    if not files:
        print(f"[index_documents] No files found in {rag.DOCUMENTS_DIR} — nothing to index.")
        return

    t0 = time.time()
    all_chunks: list[dict] = []
    all_headings: list[str] = []
    for path in files:
        try:
            text = extract_text(path)
        except Exception as exc:
            print(f"[index_documents] Skipping {path.name}: {exc}")
            continue
        title = document_title(path)
        entries = chunk_entries(path, text)
        if entries is not None:
            seen_text: set[str] = set()
            kept = [e for e in entries if not (e["text"] in seen_text or seen_text.add(e["text"]))]
            print(f"[index_documents] {path.name}: {len(kept)} Q&A entr(ies)")
            for i, entry in enumerate(kept):
                all_chunks.append({"source": path.name, "title": title, "chunk_index": i,
                                   "text": entry["text"], "section": entry["section"],
                                   "entry_id": entry["entry_id"], "embed": entry["embed"]})
                all_headings.extend(name_candidates(entry["text"]))
            continue

        pieces, headings = chunk_file(path, text)
        all_headings.extend(headings)
        # Identical text embeds to an identical vector, so duplicates arrive as a block at the
        # same score and eat the whole of TOP_K: the chapters page repeats its one-line summary
        # 9 times, which alone filled every retrieval slot for "how many chapters". Dedupe is
        # per-file and order-preserving — the first occurrence keeps its position.
        seen: set[str] = set()
        pieces = [p for p in pieces if not (p in seen or seen.add(p))]
        if not pieces:
            print(f"[index_documents] {path.name}: no extractable text, skipping")
            continue
        print(f"[index_documents] {path.name}: {len(pieces)} chunk(s)")
        for i, piece in enumerate(pieces):
            all_chunks.append({"source": path.name, "title": title,
                               "chunk_index": i, "text": piece})

    if not all_chunks:
        print("[index_documents] No chunks produced — nothing to embed.")
        return

    gazetteer = build_gazetteer(all_headings, all_chunks)
    print(f"[index_documents] Gazetteer: {len(gazetteer)} name(s) from "
          f"{len(all_headings)} heading(s)")

    print(f"[index_documents] Embedding {len(all_chunks)} chunk(s)...")
    try:
        # The title is embedded but NOT stored in "text" — retrieval needs it, the prompt does
        # not. Breadcrumbs only reach chunks that sit under a heading; a mid-document paragraph,
        # a .pdf, or a line of kai_facts.txt can carry no mention of DEVCON at all, and those are
        # the chunks a perfectly-transcribed "what does DEVCON do?" scores worst against. The
        # filename is the one piece of provenance every chunk has. Note this shifts every score
        # slightly — SIMILARITY_THRESHOLD is worth re-checking after a reindex.
        # A Q&A entry brings its own, better embedding text (see chunk_entries): section
        # breadcrumb + answer + the spoken paraphrases from `synonyms:`. Everything else keeps
        # the filename-title prefix, which is the only provenance a loose paragraph has.
        prefixed = [rag.DOCUMENT_PREFIX + (c.get("embed") or f"{c['title']}\n{c['text']}")
                    for c in all_chunks]
        embeddings = rag.embed_batch(prefixed)
    except Exception as exc:
        print(f"[index_documents] ERROR: embedding failed ({exc})")
        sys.exit(1)

    for chunk, embedding in zip(all_chunks, embeddings):
        chunk["embedding"] = embedding
        # Query-time never needs it and it is the largest string on the chunk — dropping it keeps
        # the index roughly the size it was before entries carried their synonyms.
        chunk.pop("embed", None)

    # Fingerprint of what was indexed, so load_index() can say out loud when documents/ has moved
    # on. Size and mtime rather than a content hash: this has to be free at startup on the Jetson,
    # and the failure being caught is "someone edited a document and forgot to reindex", not a
    # deliberate forgery. Advisory only — a stale index still loads and still answers.
    manifest = {p.name: [p.stat().st_size, int(p.stat().st_mtime)] for p in files}
    rag.INDEX_PATH.write_text(json.dumps({"model": rag.EMBED_MODEL, "chunks": all_chunks,
                                          "entities": gazetteer, "sources": manifest}))
    elapsed = time.time() - t0
    print(f"[index_documents] Wrote {len(all_chunks)} chunk(s) from {len(files)} file(s) "
          f"to {rag.INDEX_PATH} in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
