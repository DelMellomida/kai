# Plans

Design and implementation plans, filed by whether the work they describe is finished.

```
docs/plan/
  completed/   the plan reached a conclusion; no work is pending against it
  wip/         work remains — either unapplied steps, or applied steps not yet verified on hardware
```

**"Completed" means concluded, not necessarily shipped.** A plan that ran to its own abort criteria
and was deliberately abandoned is finished work: the decision is made and recorded, and the file
exists so nobody re-opens the question without new information. That is the case for the one file
in `completed/` today.

A file moves from `wip/` to `completed/` when nothing in it is still pending — including
verification. "Tests green" is not enough on its own for anything that touches audio, servos or the
camera; this robot has a documented history of features that passed their suite and failed in the
room.

Separate from these: [`docs/tickets/`](../tickets/) holds the individual engineering tickets from
the 2026-08-10 codebase review. Plans are narrative and describe an intended arc; tickets are
discrete units of work with acceptance criteria.

---

## completed/

| Plan | Outcome |
|---|---|
| [expressive-voice-plan.md](completed/expressive-voice-plan.md) | **Concluded — abandoned at its own abort gate.** The GPU expressive-TTS path was measured and rejected: 29 voices across 7 engine families all failed the same way, the shipping voice already measures 11.0 semitones of intonation range (near the top of human conversational), and the only model class that genuinely models expression is ~0.5B and OOMs beside Ollama's 2.37 GB. What shipped instead is delivery shaping (`ai/delivery.py`) — breaths, an occasional opener, per-reply tempo jitter — which is the "what ships if this is abandoned" section of the plan itself. Read this before re-opening the voice question. |

## wip/

Ordered roughly by how much is outstanding.

| Plan | What remains |
|---|---|
| [natural-audio-plan.md](wip/natural-audio-plan.md) | **Steps 0–2 done and shipped; step 3 belongs to [R5](../tickets/R5-serialised-first-audio-latency.md); the Tagalog branch is closed.** The second pass at "Kai sounds AI-generated", constrained to stay offline. What worked: Piper's `--sentence-silence` was never passed and defaults to **0**, so Kai ran whole paragraphs together without a breath. What did not: the VITS noise parameters move intonation by less than the run-to-run noise floor, and there is **no Filipino voice anywhere offline** (Piper 173 voices, espeak-ng, sherpa-onnx 642 assets all checked) — a native speaker judged the phonemizer workaround no better. **Gate B is still open**: the reverb can only be judged in a venue, not at a desk. |
| [latency-plan.md](wip/latency-plan.md) | Steps 1, 3 and 5a **done** (per-stage timings, Ollama's own counters, RAG context moved into the user turn, `/api/ps` placement logging). **Step 2 — stream generation and pipeline TTS per sentence — not started**, and it is the largest win in the plan. Step 4 (`TOP_K` 3→2, persona tightening) not started. Step 2 is the same work as ticket [R5](../tickets/R5-serialised-first-audio-latency.md). |
| [known-issues.md](wip/known-issues.md) | Backlog from the 2026-08-07 review. P1, P3, P4, P5, P6, P7 resolved and live. **P2, P8, P9 deliberately not applied** — each changes the LLM prompt, the audio path or the boot chain. P2's measurement (`scripts/rag_eval.py`) was built and run instead of the fix. Paired with `resolution-plan.md`; the two are read together. |
| [resolution-plan.md](wip/resolution-plan.md) | The companion plan to `known-issues.md`, sorted by what a fix can break rather than by effort. **Tiers A and B applied**; **tier C not applied** and gated on a measurement. Note `config/rag.py` records that P2 is *reduced, not closed* — `SIMILARITY_THRESHOLD` moved to 0.55 and half the off-topic queries still carry a documents block. |
| [filler-responses.md](wip/filler-responses.md) | The design as intended. The feature is wired in and the suite is green, but see the companion below before treating it as done. |
| [filler-responses-wip.md](wip/filler-responses-wip.md) | The honest status of the above: **tests green, never verified on hardware.** `scripts/filler_check.py` — the on-robot synthesise-then-transcribe check — has still never been run, and nothing in the bank has been heard out loud. Two bugs from the single deployed run have fixes written but unverified in the room. |
| [tensorrt-plan.md](wip/tensorrt-plan.md) | **Everything — planning only, nothing measured for it.** Concludes that only Whisper is worth a TensorRT port, and that the deciding constraint is iGPU memory rather than compute: `face_track.py` holds no CUDA context today, and creating one risks pushing Ollama onto the documented half-speed CPU/GPU split. Rejects MediaPipe (a GIL ceiling, not a compute ceiling) and TensorRT-LLM with reasons. **Step 0 blocks the rest** and is the same unfinished action as `latency-plan.md`'s: read the `[turn]` log for ten real turns. Read as a prerequisite for `bilingual-plan.md`'s `base`→`small` bump, not as a latency project on its own. |
| [bilingual-plan.md](wip/bilingual-plan.md) | **Phase 1 applied** 2026-07-09 (STT auto-detect, model bump, persona language line) with a full revert table in the file. **Phase 2 — multilingual RAG — not applied**; `config/rag.py` still uses the English-only `BAAI/bge-small-en-v1.5`, so a Tagalog question does not retrieve well from the documents. ⚠️ The file's own header still reads "PLANNING ONLY — nothing here is implemented yet", which its own change log contradicts. Left as found rather than silently rewritten — fix it when the file is next touched. |

---

## Notes on this arrangement

- **`known-issues.md` is a backlog, not a plan**, and normally would not live here. It is filed with
  `resolution-plan.md` because the two link to each other in both directions and are meaningless
  apart. Splitting them across two directories would have been worse than the category mismatch.
- **Nothing was fully finished except one abandoned plan.** That is a real signal, not a filing
  accident: every other plan in this repo has applied steps and unapplied steps, and several have
  applied-but-unverified steps. The `wip/` column above is deliberately specific about which is
  which, because "the plan is in progress" is not actionable and "step 2 is not started" is.
- **Inbound references were updated with the move.** `ai/delivery.py`, `config/voice.py`,
  `config/rag.py`, `scripts/rag_eval.py`, `scripts/tts_setup_models.sh`,
  `scripts/tts_setup_kokoro.sh` and `tests/test_voice_assistant.py` all cite these documents by
  path in comments; those paths now point here. `ai/rag.py` refers to "known-issues P7" by name
  rather than by path and was left alone.
