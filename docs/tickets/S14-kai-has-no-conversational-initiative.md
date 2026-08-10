# S14 — Kai only ever speaks when spoken to, and its sessions die silently

| | |
|---|---|
| **Tier** | 2 |
| **Severity** | Medium (enhancement, not a defect) |
| **Effort** | Medium |
| **Confidence** | Medium |
| **Lens** | Software |

## Location

- `ai/session.py` — `tick()`'s `STATE_LISTEN_WAIT` branch (the `SESSION_NO_SPEECH_S` and
  `SESSION_NO_FACE_S` timeouts, lines ~1270–1275), `_end_session()`, `_speak_canned()`
- `config/wake.py` — `SESSION_NO_SPEECH_S = 25.0`, `SESSION_NO_FACE_S = 8.0`,
  `GREETING_ENABLED` / `GREETING_TEXT` (the one existing unprompted line), `WAKE_ALLOW_BARGE_IN =
  False`
- `ai/persona.txt` — the "offer the rest" instruction and the playful follow-up examples
- `config/filler.py` — the existing bank machinery, no-repeat sets, and the pre-synthesis pattern

## Problem

Every line Kai says is triggered by something the person did. The wake ack, the fillers, the canned
error and no-speech lines, and the reply itself are all responses. The single exception is
`GREETING_TEXT` at boot, which fires once per process.

The state machine has no path from "waiting" to "saying something". `STATE_LISTEN_WAIT` has exactly
two exits: speech arrives, or a timeout ends the session. The `no_speech` path at
`SESSION_NO_SPEECH_S = 25.0` and the `no_face` path at `SESSION_NO_FACE_S = 8.0` both run
`_end_session()`, which speaks nothing.

This puts the state machine in direct disagreement with the persona. `ai/persona.txt` instructs Kai
to hand over two or three items and offer the rest — *"we run a few more, gusto mo marinig?"* — and
to ask playful follow-ups like *"tapos ano nangyari?"*. Kai duly asks. Nothing in `ai/session.py`
knows a question was asked, so if the person does not answer, the offer evaporates: twenty-five
seconds later the session ends without a word.

## Why it matters

Inputs → observable behaviour, two cases:

1. Kai asks "gusto mo marinig?", the person hesitates, and Kai never mentions it again. The offer
   reads as rhetorical, which trains people not to answer the next one.
2. The person stops talking — thinking, or drifting away. Kai goes silent for 25 s and then simply
   stops being in a conversation. There is no sign-off, so from the person's side there is no
   difference between "the conversation ended" and "it broke". At a venue this is the most common way
   a conversation actually terminates, and it is the one with no audible outcome.

The `GREETING_ENABLED` comment already accepts the underlying premise — that a robot which says
something unprompted reads as awake — and it is the only place the system acts on it.

**This is judgement-heavy, which is why Confidence is Medium.** A nudge that fires too eagerly reads
as nagging, and the failure is worse than the silence it replaces. The bar is one short line at one
well-chosen moment, not conversational filler.

## Two constraints that bound the design

- **Presence is three-valued and the third value is load-bearing.** `ai/session.py`'s header records
  that "no face" and "no idea" are deliberately distinct, because the face feed stops entirely on a
  camera stall or under `--no-camera`. A nudge gated on "not absent" would make Kai monologue into an
  empty room on every camera stall and continuously under `--no-camera`. It must be gated on presence
  being positively **true**.
- **Kai is deaf while it speaks.** There is no echo cancellation, `WAKE_ALLOW_BARGE_IN = False`, and
  the mic is gated shut for the whole reply plus `TTS_TAIL_MUTE_S`. A nudge spends part of the very
  window it is trying to keep open — and if the person starts answering during it, that answer is
  lost. This caps the nudge's length far below an ordinary reply, and it is why the nudge cannot
  simply be a normal LLM turn.

## Acceptance criteria

- [ ] At most **one** nudge per session, fired from `STATE_LISTEN_WAIT` at a documented fraction of
      `SESSION_NO_SPEECH_S`, never in any other state.
- [ ] The nudge fires only when presence is positively true. Under `--no-camera`, during a camera
      stall, or on any "no idea" presence reading, it never fires. Tested explicitly against all
      three presence values.
- [ ] The nudge is short enough that the resulting deaf spell is a fraction of the remaining listen
      window — measure the spoken duration, do not estimate it from the string. `config/filler.py`'s
      rejected-opener history is the precedent: all twenty original openers measured over the cap and
      the whole tier was dead.
- [ ] Firing the nudge does not extend or reset `SESSION_NO_SPEECH_S`. A session that was going to end
      still ends; the nudge is a last word, not a way to keep a dead conversation alive.
- [ ] Sessions ending on `no_speech` or `no_face` speak a short sign-off before teardown, so the end
      is audible. Sessions ending on `error_streak`, `busy_timeout`, `mic_lost` or manual end stay
      silent — an error is not a goodbye.
- [ ] Lines are pre-synthesised at startup like the other canned audio (`ai/tts.py prewarm_canned`),
      so nothing here costs latency at speak time, and they follow `config/filler.py`'s writing rules
      verbatim: ASCII only, no repeated-letter runs, brand names written as spoken.
- [ ] Lines exist per language and are chosen by the same language key the filler bank uses, so a
      Tagalog conversation does not end in English.
- [ ] No line repeats within a conversation, reusing the `_filler_used_*` pattern rather than a new
      mechanism.
- [ ] Every line is synthesised through Piper and transcribed back with Whisper before shipping —
      the check `config/thinking.py` and `config/filler.py` both mandate, for the recorded reason that
      a mangled line sounds fine as text.
- [ ] Both halves are independent dashboard toggles, defaulting from `config/`, in the style of
      `config/thinking.py`'s two toggles and for the same stated reason: nobody knows yet how they
      feel in the room.
- [ ] Judged by ear across a full event session, not a single conversation. Record the verdict in the
      config comment.

## Suggested approach

Deliberately **not** LLM-driven. A nudge is a fixed short line at a fixed moment; routing it through
Ollama adds a multi-second generation to a moment defined by its timing, and lands on the latency
path R5 exists to shorten.

**Step 1 — a nudge bank.** `config/initiative.py`, modelled on `config/thinking.py`: every tunable
for the feature in one file so the whole thing is easy to find and easy to delete. Two small
language-keyed banks — nudges and sign-offs — plus the toggles, the fire fraction, and the measured
durations.

**Step 2 — the timer.** One deadline armed on entry to `STATE_LISTEN_WAIT` and destroyed on exit,
per the header's rule that timers are state-scoped deadlines rather than accumulators. Fire through
`_speak_canned()`, which already owns the pre-synthesised path and the speech-ownership flags.

**Step 3 — the sign-off.** In `_end_session()`, before the teardown, and only for the two "went
quiet" reasons. Note the ordering hazard: `_end_session()` currently calls `_cut_speech()` early, so
a sign-off has to be sequenced against that rather than dropped into the middle of it.

**Step 4 — callbacks are the persona's job, not the state machine's.** "You already asked me that"
and "earlier you mentioned X" are things the model can do from the rolling history it already
receives; if they are wanted, they are a `persona.txt` edit and a measurement, not code. Explicitly
out of scope here, and worth trying first precisely because it costs nothing.

**Sequencing.** Worth doing after S13. A resumable conversation changes what a sign-off should say —
"balik ka ha" is right when the conversation can be resumed and misleading when it cannot.

**Rollback.** Both toggles default from `config/initiative.py` and both off restores today's
behaviour exactly.
