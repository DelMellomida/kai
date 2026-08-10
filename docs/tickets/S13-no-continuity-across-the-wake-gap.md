# S13 — A conversation is forgotten the instant the session ends

| | |
|---|---|
| **Tier** | 2 |
| **Severity** | Medium (enhancement, not a defect) |
| **Effort** | Medium |
| **Confidence** | Medium-High |
| **Lens** | Software |

## Location

- `ai/session.py` — `_end_session()` (line ~1322), and the eight call sites that reach it:
  `no_speech`, `no_face`, `busy_timeout`, `no_speech_streak`, `error_streak`, `ptt_stop`,
  `mic_lost`, `hands_free_off`
- `ai/voice_assistant.py` — `reset_history()` (line ~461): clears `_history`, bumps the epoch, calls
  `rag.reset_topic()`
- `ai/rag.py` — `reset_topic()`, `_sticky_turns`, and the follow-up expansion at line ~406
- `ai/session.py` — `_begin_session()` (line ~621) and its note that a wake landing in
  `LISTEN_WAIT` deliberately keeps history
- `config/wake.py` — `SESSION_NO_SPEECH_S = 25.0`, `SESSION_NO_FACE_S = 8.0`

## Problem

Session end is total and immediate. `_end_session()` calls `_voice.reset_history()`, which empties
`_history`, bumps the epoch, and calls `rag.reset_topic()` to drop the sticky subject. There is no
intermediate state between "mid-conversation" and "never met".

Two of the eight end reasons fire on ordinary human behaviour rather than on departure:

- `no_speech` at `SESSION_NO_SPEECH_S = 25.0` — someone thinking, reading a badge, or being talked
  to by a third person for twenty-six seconds;
- `no_face` at `SESSION_NO_FACE_S = 8.0` — someone turning to a colleague, or the camera losing
  them for eight continuous seconds.

Either one discards the whole conversation. The person then says "hey Kai" again and is a stranger.

## Why it matters

The sharpest consequence is retrieval, not politeness. `ai/rag.py` keeps a sticky subject for
`STICKY_TURNS` and expands a pronoun-free follow-up using the previous user turn — that machinery is
what makes "how many chapters?" retrieve DEVCON rather than nothing. `reset_topic()` clears it at
session end, so the identical question asked twenty-six seconds later retrieves against no subject
and Kai answers that it is not sure — having answered it correctly a moment earlier.

Inputs → observable behaviour: ask about DEVCON chapters, pause 26 s, say "hey Kai", ask "and how
many are there now?" — the follow-up no longer resolves, and the answer degrades from a fact to a
hedge. Nothing tells the person why.

`reset_history()`'s comment states the constraint that makes this non-trivial and must not be
weakened: forgetting exists so Kai cannot "hand the next person one turn of the last person's
conversation". The gap between the two behaviours is the whole ticket — the current code resolves it
by always assuming the next person is a different person, which is right at a venue queue and wrong
for the same person pausing to think.

**Without the camera there is no identity signal**, so this cannot be solved by recognising the
person. It can only be bounded by time, which is why the window is the design and not an incidental
constant.

## Acceptance criteria

- [ ] A session ending on `no_speech` or `no_face` retains history and sticky topic for a bounded
      resume window, and a wake inside that window continues the conversation instead of starting a
      new one.
- [ ] The window is a single documented constant in `config/wake.py`, annotated with the reasoning
      and the venue risk, in the style of its neighbours. Start short — comfortably under a minute —
      and record what it was tuned against.
- [ ] Resume is **opt-in per end reason**. `error_streak`, `busy_timeout`, `mic_lost`,
      `hands_free_off`, `ptt_stop` and manual end all discard immediately as they do today; only the
      two "the person went quiet" reasons are resumable. Justify any addition in the constant's
      comment.
- [ ] Epoch safety is preserved. `reset_history()` bumps the epoch specifically so an in-flight reply
      cannot append itself to the next person's conversation — a resume must not reinstate a stale
      epoch, and a reply abandoned by the ending session must still never surface after the resume.
- [ ] The retained state is dropped on window expiry by the tick thread, not lazily at the next wake.
      A conversation must not sit resumable for an hour because nobody came back.
- [ ] `/params` publishes whether a resumable conversation is pending and how long it has left, and
      the dashboard offers a one-click discard. At a venue this is the operator's escape hatch and it
      is required, not optional.
- [ ] Kai signals a resume audibly — a distinct short line ("balik ka pala") rather than the ordinary
      ack — so the person can tell which conversation they are in. A silent resume is
      indistinguishable from a fresh session until the answers start referring to things they did not
      just say.
- [ ] The filler bank's used-line sets follow the same rule as history: a resumed conversation does
      not get the whole bank back, since `_begin_session`'s comment defines the scope as "the span
      over which one listener would actually notice the repeat" — which a resume does not end.
- [ ] Tests cover: resume inside the window, no resume outside it, no resume after a non-resumable
      end reason, expiry by tick, sticky-topic survival across a resume, and that an in-flight reply
      from the ended session is still dropped.

## Suggested approach

**Step 1 — split forgetting from ending.** Today `_end_session()` and `reset_history()` are welded
together. Introduce a *pending* state: `_end_session(reason)` decides, from the reason, whether to
call `reset_history()` now or to park the conversation with a deadline. Parking keeps `_history` and
`_sticky_turns` where they are and records `_resumable_until`; it still bumps the epoch, still cuts
speech, still resets the gate and DSP. Nothing about the teardown changes except which state
survives it.

**Step 2 — resume at the wake.** `_begin_session()` already has the shape for this: it distinguishes
a wake that starts a conversation from a wake that lands in `LISTEN_WAIT` mid-conversation and keeps
history. A resume is a third case with the same outcome as the second. Route it through the same
branch rather than adding a parallel one.

**Step 3 — expiry on the tick.** The 20 Hz tick thread already owns every deadline in this file, and
`ai/session.py`'s header states why ("timers are deadlines scoped to a single state, never
accumulators"). The resume window is one more deadline; it must not become the exception that
introduces a `threading.Timer`.

**Step 4 — the RAG side.** `_sticky_turns` is module state in `ai/rag.py`, not session state, so
parking it means *not* calling `reset_topic()` rather than saving and restoring anything. Make that
explicit in the comment there, because "reset_topic is called with the history" is currently stated
as an invariant and would stop being one.

**Rollback.** `config/wake.py` `SESSION_RESUME_S = 0.0` disables the whole path and restores today's
behaviour exactly — the window being a duration means the off switch is already in the type.
