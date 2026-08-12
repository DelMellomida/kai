# A2 — Nothing measures what Kai actually says

| | |
|---|---|
| **Tier** | 2 |
| **Severity** | Medium-High |
| **Effort** | Medium |
| **Confidence** | High |
| **Lens** | AI |

## Location

- `scripts/rag_eval.py`, `scripts/rag_accuracy.py` — the two harnesses that exist, and where they
  stop: both end at `retrieve_context()`
- `scripts/filler_check.py` — the synthesis→ASR round trip, which covers the canned bank only
- `ai/persona.txt` — re-read on every LLM call (`ai/llm.load_persona`, see **S11d**), edited freely,
  never measured
- `config/voice.py` — `OLLAMA_NUM_PREDICT`'s history ("192 was tried first … on-device that produced
  134-word replies"), `MAX_HISTORY_TURNS`'s history ("at 3, Kai forgot the opening question by turn
  4")
- `ai/rag.py` — `format_context()`'s docstring, which records two failed length experiments in prose
- `ai/delivery.py` — `DELIVERY_*`, judged "by ear across several turns"

## Problem

Retrieval is measured well. `scripts/rag_accuracy.py` carries 31 questions with literal ground-truth
needles and a recorded baseline (30/31 at `SIMILARITY_THRESHOLD = 0.55`); `scripts/rag_eval.py`
carries a fixed 16-query on-topic/off-topic split and a threshold sweep. Both are read-only, safe on
a live robot, and both say "keep these cases exact if you are comparing against the baseline".

Neither of them, and nothing else in the tree, looks at the **reply**. `retrieve_context()` returning
the right chunk is where the measurement stops. What the model then does with that chunk — whether
the number in the answer is the number in the chunk, whether it stayed inside persona.txt's four
sentences, whether it answered in the language it was asked in, whether it said "I'm not sure" when
the facts were absent — is judged by trying a handful of turns and writing the impression into a
comment.

The comments are honest about this and they are the evidence. `OLLAMA_NUM_PREDICT`'s history is a
sequence of hand A/Bs (96 → 192 → 160). `format_context()`'s docstring records two length
instructions that were tried on-device and both found wrong, with the word counts quoted from a small
number of replies. `MAX_HISTORY_TURNS` moved 3 → 6 on one observed failure. Every one of those is a
real measurement, taken carefully, and none of them can be re-run.

## Why it matters

The AI half of this robot is the half being changed most often, and it is the half with no
before/after number. Three concrete consequences:

- **`persona.txt` is live-reloaded on every call** (**S11d**). A well-meant edit at a venue changes
  every reply from the next turn onward, and there is no way to tell whether it made things better
  than by listening to a few answers — which is exactly the method that produced the 134-word replies
  the file's own comment records.
- **A retrieval improvement cannot be told from a generation regression.** `rag_accuracy` can go to
  31/31 while Kai's answers get worse, because the harness stops one stage short. The eval's own
  docstring already caught a version of this: a needle passed "for months" while the retrieved
  document was factually wrong, because the check only proved the document reached the model.
- **Every ticket that changes what Kai says has no acceptance measurement available to it.** S12,
  S13, S14 and A4 all alter the prompt or the turn; each of their acceptance criteria has to fall
  back on a manual demo. **R5**'s criteria go further and ask whether the filler bank becomes dead
  code, which is a question about what a listener hears.

The absence is the finding. This is not a defect in any line of code — it is the one measurement
discipline the rest of this codebase applies everywhere and this subsystem does not.

## Acceptance criteria

- [ ] A `scripts/reply_eval.py` exists in the shape of the two RAG harnesses: read-only against the
      live index and a running Ollama, safe on the robot, prints a score table and a total, with the
      cases and the baseline recorded in the docstring and the instruction to keep them exact.
- [ ] Cases are `(question, checks)` where every check is mechanical — no model-as-judge. The four
      that matter, each derived from a failure already recorded in this repo:
      - **grounded**: a literal needle that must appear in the reply (the same needles
        `rag_accuracy` already uses, moved one stage downstream);
      - **no invention**: a list of strings that must NOT appear, seeded from real observed
        hallucinations (the "Micro:bit / Qwen / Google AI Suite" case in `rag_accuracy`'s comment);
      - **length**: sentence count within persona.txt's stated bound, and character count against
        `TTS_MAX_SPOKEN_CHARS`, with the truncation rate against `OLLAMA_NUM_PREDICT` reported
        separately — that pair is what the 96/192/160 history was about;
      - **language**: a Tagalog question gets a Tagalog reply, checked by a cheap function-word count,
        not by a model.
- [ ] A "should refuse" set: questions whose answer is provably absent from `documents/`, where the
      reply must contain a hedge and must not contain a specific-looking fact. This is the direct
      test of `NO_CONTEXT_NOTICE` and of persona.txt's "say you are not sure", neither of which is
      currently tested at all.
- [ ] Determinism is handled honestly. gemma2:2b is sampled, so the harness runs each case N times
      (N small, 3 is enough) and reports pass rate rather than pass/fail — and the docstring says so,
      so nobody reads a 1-run difference as a regression.
- [ ] Cost is stated up front, like `rag_accuracy`'s "~20 seconds": one full turn per case per repeat
      at ~27 tok/s, so a 30-case set at N=3 is minutes, not seconds. It is a bench tool, not a test,
      and it must not be run during a demo.
- [ ] The harness does **not** synthesise or play audio. `filler_check.py` already owns the
      text→speech→text direction, and pulling Piper in here would make this unrunnable off the robot
      and would put a second synth next to a live reply — the 2026-08-07 incident.
- [ ] A baseline is recorded on the robot, dated, before any of A4 / S13 / S14 lands.
- [ ] `docs/README.md` (or the scripts' own listing) names it alongside the two RAG harnesses so the
      three are discoverable as a set.

## Suggested approach

Model it on `scripts/rag_accuracy.py` almost line for line — same `CASES` list at module level, same
`contextlib.redirect_stdout` around the noisy layers, same hit/miss table, same "re-run with
--verbose to see what the failing queries received". That file is the house pattern and it works.

The one new piece is the turn itself. Do **not** import `VoiceAssistant`: it owns the mic, the epoch
machinery and the speech path, and constructing it drags in `sounddevice`. Call the two pure pieces
directly —

```
# sketch
context = rag.retrieve_context(question)
system, user = load_persona(), (f"{context}\n\n{question}" if context else question)
data = _ollama_request(build_chat_messages(system, history, user))
reply = data["message"]["content"]
```

— which is exactly what `_call_ollama` assembles, minus the identity injection and the timing
bookkeeping. Keep a `--history` mode that replays a fixed multi-turn script, because the two
questions this harness is most needed for (does the name survive, does the follow-up resolve) are
both multi-turn and are what S12–S14 change.

Sentence counting: reuse `ai/speak_envelope`'s existing split rather than writing a second one, so
"four sentences" means the same thing here as it does to the jaw.

**Scope note.** Two adjacent gaps are deliberately *not* in this ticket, so it stays landable:
wake-word false-accept / false-reject rate (`scripts/wake_test.py` is the protocol, and
`WAKE_SENSITIVITIES = 0.5` is the one constant in `config/` with no measurement recorded behind it),
and STT word-error rate (only latency was ever measured, in `WHISPER_MODEL`'s comment). Both want the
same treatment and both need the robot and a room; note them where they belong rather than growing
this.
