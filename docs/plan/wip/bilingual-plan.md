# Kai Bilingual (English + Tagalog) — Implementation Plan

**Status:** PLANNING ONLY — nothing here is implemented yet.
**Scope (decided):** Kai understands & replies in **English _or_ Tagalog**, auto-detected **per utterance**. We are *not* optimizing for mid-sentence Taglish code-switching (see Limitations). **Tagalog RAG is in scope** (Tagalog questions should retrieve from the documents).

---

## Goal
The user speaks English or Tagalog; Kai transcribes it correctly, replies in the **same language**, and can answer document-grounded (RAG) questions in either language.

## Current pipeline (baseline)
| Stage | Today | Change needed |
|---|---|---|
| **STT** — faster-whisper | `"base"` model, CPU/int8, **`language="en"` hardcoded** (`ai/voice_assistant.py` `_transcribe`) | Auto-detect language; bump model to `small` |
| **LLM** — Ollama `gemma3:4b` | Multilingual; persona already Taglish-flavored | One-line persona instruction to mirror the user's language |
| **RAG embeddings** | `BAAI/bge-small-en-v1.5` (**English-only**) + `search_document:`/`search_query:` prefixes (`config/rag.py`) | Swap to a **multilingual** model + matching prefixes, then **re-index** |
| **Output** — jaw pantomime | Text-timed, no audio (`config/voice.py` SPEAK_* envelope) | None (language-agnostic) |

---

## Phase 1 — Conversational bilingual (STT + LLM)
High impact, small change. Gets EN/TL conversation working.

1. **STT language → auto-detect.** In `ai/voice_assistant.py` `_transcribe`, change `language="en"` to auto-detect (`language=None`). faster-whisper returns the detected language in the `_info` object (currently discarded) — log it for debugging. Optionally add a `LANG_MODE = "auto" | "en" | "tl"` knob in `config/voice.py` so it can be forced.
2. **Whisper `base` → `small`** in `config/voice.py` (`WHISPER_MODEL`). `base` Tagalog accuracy is poor; `small` is the realistic minimum. Keep `WHISPER_DEVICE="cpu"` / `int8` (leaves iGPU memory for Ollama). **Verify memory** after (see Memory budget).
3. **Persona.** Add to `ai/persona.txt`: *"Understand and reply in the same language the person used — English or Tagalog."* (gemma3:4b already handles both; keep replies short per the existing persona.)
4. **Test:** clear English utterance, clear Tagalog utterance → check transcript accuracy and that the reply language matches.

## Phase 2 — Tagalog RAG (multilingual embeddings)
So Tagalog questions retrieve from the (currently English) documents = cross-lingual retrieval.

1. **Swap `EMBED_MODEL`** in `config/rag.py` from `bge-small-en-v1.5` to a fastembed-supported **multilingual** model. Leading candidate: **`intfloat/multilingual-e5-small`** (~118M, strong EN↔TL, modest footprint). `bge-m3` is stronger but ~2 GB → likely too heavy for this box.
2. **Update the task prefixes together with the model** (the config comment warns about this): e5 uses `query: ` / `passage: ` instead of bge's `search_query:` / `search_document:`. Model + prefixes must match.
3. **Re-index:** run `python3 -m ai.index_documents` from the project root to rebuild `documents/.rag_index.json` (it stores the model name + embeddings; a mismatch silently wrecks retrieval).
4. **Test:** ask a document question (e.g. about DEVCON) in **English** and in **Tagalog**; confirm the right chunk is retrieved both times.

## Phase 3 — Deferred / future
- **Tagalog TTS / audio output** — Kai has no audio yet; the "speaking" jaw is a text-timed pantomime. Revisit when/if audio output is added.
- **Per-language jaw pacing** — `SPEAK_SEC_PER_WORD` (0.34, ~175 wpm English); Tagalog words run a touch longer. Not worth tuning now.

---

## Memory budget — the real constraint
This is an **8 GB unified-memory Jetson**; `gemma3:4b` alone holds ~4.3 GB, and the CSI camera OOMs when memory is tight. Phase 1 (`small` Whisper, ~250 MB int8 CPU) + Phase 2 (multilingual-e5-small, ~400–500 MB ONNX CPU via fastembed) both add pressure on top of gemma3 + camera NVMM + MediaPipe.
- **Measure free memory** after each phase; watch for the Argus `InsufficientMemory` camera-crash pattern.
- **`medium` Whisper (best Tagalog) almost certainly won't fit** alongside gemma3 — `small` is the practical ceiling. This caps how good Tagalog STT can get on this hardware.
- Prefer `multilingual-e5-small` over the heavier `bge-m3` for the same reason.

## Limitations to set expectations
- **Taglish (mid-sentence code-switching) is out of scope and inherently imperfect** here: Whisper detects/commits to **one language per utterance**, so a mixed sentence forces one language and degrades the other half. EN-only and TL-only utterances are handled well; heavily mixed ones won't be.
- Small models: gemma3:4b Tagalog is understandable but not perfectly fluent; `small` Whisper Tagalog is decent, not flawless. Fine for a companion demo, not production dictation.

## Change summary (for when we implement)
- `config/voice.py`: `WHISPER_MODEL` `base`→`small`; (optional) add `LANG_MODE`.
- `ai/voice_assistant.py`: `_transcribe` language `"en"`→auto; log detected language.
- `ai/persona.txt`: add the language-mirroring line.
- `config/rag.py`: `EMBED_MODEL` + prefixes → multilingual.
- Re-run `python3 -m ai.index_documents` (Phase 2).

## Open questions
- Does `small` Whisper Tagalog accuracy suffice on CPU, or is latency/quality a problem? (Test before committing.)
- Confirm `multilingual-e5-small` fits the memory budget with gemma3 resident (measure).

---

## CHANGE LOG — Phase 1 applied 2026-07-09 (HOW TO REVERT)
Phase 1 (STT auto-detect + `small` model + persona language line) is now live. The repo is **not** under git, so revert manually with the originals below:

| File | Line | Changed to | **Revert to (original)** |
|---|---|---|---|
| `config/voice.py` | `OLLAMA_MODEL` | `"gemma2:2b"` (switched from `gemma3:4b` to free ~2.7 GB so the camera + `small` Whisper fit in 8 GB) | `"gemma3:4b"` |
| `config/voice.py` | `WHISPER_MODEL` | `"small"` (first tried, reverted to `base` for OOM, then restored to `small` once the LLM shrank to gemma2:2b) | `"base"` |
| `config/voice.py` | `WHISPER_LANGUAGE` | `None` (new line — added) | delete the line, **or** set `"en"` |
| `ai/voice_assistant.py` | import block | added `WHISPER_LANGUAGE` to `from config.voice import (...)` | remove `WHISPER_LANGUAGE` from the import list |
| `ai/voice_assistant.py` | `_transcribe()` | `transcribe(..., language=WHISPER_LANGUAGE, ...)` + detected-language log + `text` var | restore `transcribe(..., language="en", ...)` and `return " ".join(seg.text.strip() for seg in segments).strip()` |
| `ai/persona.txt` | end of paragraph | appended: *"Understand and reply in the same language the person used — English or Tagalog."* | delete that sentence |

**Fastest partial revert (no code edit):** set `WHISPER_LANGUAGE = "en"` and `WHISPER_MODEL = "base"` in `config/voice.py`, then restart face_track — that reverts the *behavior* (English-only, base model) while leaving the plumbing in place.
Phase 2 (multilingual RAG) is **not** applied — `config/rag.py` is unchanged.
