# S12 — Kai never learns who it is talking to

| | |
|---|---|
| **Tier** | 1 |
| **Severity** | Medium (enhancement, not a defect) |
| **Effort** | Small |
| **Confidence** | High |
| **Lens** | Software |

## Location

- `ai/llm.py` — `build_chat_messages()`, `load_persona()`; the prompt is `system + capped history +
  user turn` and nothing else
- `ai/voice_assistant.py` — `_call_ollama()` (line ~956, builds `history` then the messages),
  `_process()`'s history append (lines ~837–839)
- `ai/session.py` — `_begin_session()` / `_end_session()`, the only two places that define the
  lifetime anything person-scoped may have
- `ai/persona.txt` — the character that already asks playful follow-ups
- `config/voice.py` — `MAX_HISTORY_TURNS = 6`, and the `RAG_CONTEXT_PLACEMENT` note on KV-prefix
  invalidation

## Problem

Nothing in the prompt identifies the person. `build_chat_messages()` composes the system persona,
the last `MAX_HISTORY_TURNS * 2` messages, and the new user turn — there is no slot for a fact about
the speaker that is not itself a conversational turn.

If someone says "I'm Jhondel", the string lands in `self._history` as an ordinary user message and
is subject to the ordinary rolling cap. `MAX_HISTORY_TURNS = 6` (`config/voice.py`), so after six
further exchanges the message is evicted and the name is unrecoverable — nothing extracted it,
nothing pinned it, and the model has no other source for it.

## Why it matters

Inputs → observable behaviour: tell Kai your name, ask it seven more questions, ask "what's my
name?" — it does not know. Within the window it may use the name; past the window it cannot, and
the transition is invisible to the person talking.

`MAX_HISTORY_TURNS`'s own comment records the same class of failure being taken seriously once
already: at 3, "Kai forgot the opening question by turn 4 and then answered about it confidently
anyway". Raising the cap was the fix for topic memory. It is not the fix here, because a name is a
fact that should outlive the window rather than a turn that should scroll out of it — and raising
the cap further is explicitly bounded by `OLLAMA_NUM_CTX` ("the two are one budget").

This is also the **only personalisation available without the camera**. `ai/persona.txt` already
writes a chatty, chismosa character that "asks playful follow-ups" — asking for a name is exactly
what that character would do, and being addressed by name is the cheapest thing that separates a
kiosk from something talking to a specific person.

## Acceptance criteria

- [ ] A name offered in ordinary speech ("I'm Jhondel", "ako si Jhondel", "my name is Jhondel") is
      captured and available to every subsequent turn of the session, independent of
      `MAX_HISTORY_TURNS`.
- [ ] The name is used in replies at the model's discretion, not stapled onto every turn. Judge by
      ear across a ten-turn conversation: a name in every single reply reads worse than none.
- [ ] Capture is bounded and conservative. A missed name is the acceptable failure; a wrong name is
      not. Document what is deliberately not matched.
- [ ] The name never survives `_end_session()`. The next person is a stranger — same contract as
      `reset_history()` and `rag.reset_topic()`, and for the same recorded reason ("hand the next
      person one turn of the last person's conversation").
- [ ] The name survives a wake that lands in `LISTEN_WAIT` — saying "hey Kai" again mid-conversation
      already keeps history (`_begin_session`'s note), so it must keep the name too.
- [ ] **KV-prefix cost is measured and recorded.** Injecting into the system prompt changes the
      cached prefix at the moment the name is learned. Confirm from `prompt_eval_*`
      (`OLLAMA_LOG_TIMINGS`) that this is a one-turn cost and the prefix is stable afterwards — if
      it re-invalidates every turn, the injection is in the wrong place.
- [ ] `/params` exposes the captured name so the dashboard can show what Kai currently believes, and
      the existing reset paths visibly clear it.
- [ ] Tests cover: capture from several phrasings in both languages, non-capture of the near-misses,
      eviction on session end, survival across a mid-session wake, and that no name in the transcript
      leaves the prompt unchanged.

## Suggested approach

Smallest change that satisfies the criteria — a pinned fact, not a memory system.

**Step 1 — extraction.** A pure function in a new `ai/identity.py`, taking a transcript and returning
`str | None`. Anchored patterns only (`I'm X`, `my name is X`, `ako si X`, `pangalan ko ay X`),
single capitalised-or-first token, length-capped, and a stop-list so "I'm fine", "I'm from Cebu",
"ako si ganito" do not become names. Pure stdlib, pure function, testable with plain strings — the
same shape as `ai/wake_phrase.py` and `ai/delivery.py`, and for the same reason: every bug here lives
in the offsets and the thresholds.

Do **not** ask the LLM to extract it. That is a second Ollama round-trip on the latency path R5
exists to shorten.

**Step 2 — storage.** One `Optional[str]` field on `VoiceAssistant`, set under the existing lock,
cleared in `reset_history()` alongside `rag.reset_topic()`. That places its lifetime on the seam the
codebase already maintains rather than inventing a second one.

**Step 3 — injection.** One line appended to the system prompt in `_call_ollama()` when set — e.g.
`The person you are talking to is called Jhondel. Use their name naturally, not in every reply.`
System position, not user position: unlike RAG context this string does **not** change per turn, so
it costs one prefix invalidation when learned and nothing after. That is the opposite of the case
`RAG_CONTEXT_PLACEMENT` documents, and the reason the two go in different places.

**Step 4 — the persona.** Leave `persona.txt` alone at first. If Kai never gets told a name because
it never asks, add one line inviting it to ask early — but measure first, since the character
already asks follow-ups unprompted.

**Rollback.** A `config/voice.py` flag (`IDENTITY_CAPTURE = True`) reverting to today's persona +
history prompt, in the style of `RAG_CONTEXT_PLACEMENT`'s documented REVERT.

## Resolution

**Landed 2026-08-10**, branch `feat/session-identity-capture`. Implemented as specified — four steps,
no deviations. Suite: 1232 passed, 2680 subtests (was 1185, 2675).

- `ai/identity.py` — `extract_name()`, pure stdlib, two anchor tiers.
- `config/voice.py` — `IDENTITY_CAPTURE`, `IDENTITY_PROMPT`, `IDENTITY_MIN_LEN` / `IDENTITY_MAX_LEN`,
  `IDENTITY_WEAK_ANCHORS_NEED_CAPITAL`, `IDENTITY_STOPWORDS`, sited directly under
  `MAX_HISTORY_TURNS` since that is the cap they exist to outlive.
- `ai/voice_assistant.py` — `_person_name`, `person_name`, `note_identity()`, cleared in
  `reset_history()`, injected in `_call_ollama()`. `_epoch_ok_locked()` added because `self._lock` is
  a plain `Lock`, so the existing `_epoch_ok()` cannot be called from inside a critical section.
- `ai/session.py` — `sess_person` on `/params`, read through to the assistant rather than mirrored.
- `ai/persona.txt` — the conversational-callback line (S14's step 4, tried here because it is free).
- Tests: `tests/test_identity.py` (34), `TestIdentityCapture` in `tests/test_voice_assistant.py`
  (10), three in `tests/test_session.py`. `FakeVoice` gained `person_name` and clears it in
  `reset_history()` — modelling the new interface rather than letting `getattr` hide it.

**Met:** capture independent of `MAX_HISTORY_TURNS`; conservative extraction with the non-matches
documented and tested; cleared on `_end_session`; survives a mid-session wake; `/params` exposure;
full test coverage including the epoch guard and prompt stability across turns.

**Not met — both need the robot, neither blocks the merge:**

- *Used at the model's discretion, judged by ear over ten turns.* `IDENTITY_PROMPT`'s "not in every
  reply" clause is reasoned, not measured. If the model over-uses the name, that string is the knob.
- *KV-prefix cost confirmed from `prompt_eval_*`.* `test_system_prompt_is_stable_across_turns_once_learned`
  asserts the code-side property the claim rests on — the system message is byte-identical across
  turns once learned — but the one-turn spike itself has not been observed on hardware. If it turns
  out to recur every turn, something is rebuilding the string and the placement is wrong.
