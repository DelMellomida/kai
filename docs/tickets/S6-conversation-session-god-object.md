# S6 — `ConversationSession` is a god object

| | |
|---|---|
| **Tier** | 3 |
| **Severity** | Medium |
| **Effort** | Large |
| **Confidence** | High |
| **Lens** | Software |

## Location

- `ai/session.py` — `ConversationSession`, 1587 lines, one class
- `tests/test_session.py` — 2398 lines, the largest test file in the repo
- Candidate extraction boundaries:
  - filler policy: `_arm_filler`, `_filler_gap`, `_filler_enabled`, `_speak_filler`, `_tick_filler`,
    and the eight `_filler_*` instance fields plus `_filler_used_openers` / `_filler_used_stalls` /
    `_filler_turn_stalls`
  - warm lifecycle: `_canned_lines`, `_prewarm_canned`, `_warm_all`, `_speak_greeting`,
    `_within_length_cap`, `_quiet_for_synth`, `_warm_one`, `_prewarm_bank`, `reprewarm_canned`,
    `_rewarm_when_quiet`

## Problem

One class owns: the conversation FSM and every timer; the whisper wake-scan tier (`_finish_scan`,
`_on_scan_done`, `_scan_token`, the cooldown accounting); filler *policy* (per-turn queues, language
latching, three separate "already used" sets, back-to-back guards, gap clamping); the canned-line
prewarm/rewarm lifecycle including the greeting and the multi-minute bank warm; debug-capture wiring;
the dashboard status projection (`_project_status`, `get_status`'s ~60 keys); mic recovery
(`reresolve_mic`); and the live settings setters.

The filler subsystem alone is roughly 250 lines of session code and eight instance fields. The
state machine — the part that decides whether Kai is listening, thinking or speaking, and the part
whose bugs are conversation-ending — is interleaved with policy that is essentially about which
canned audio file to play next.

## Why it matters

Two concrete costs, both already being paid:

- **The bugs cluster in the extracted-shaped parts.** The recorded incident history in the file's own
  comments is dominated by filler and warm issues: the same opener twice, the same stall twice in
  one exchange, three stalls talking over each other, a bank burst corrupting a live reply's synth,
  a re-warm verifying only `ack`, an unconditional latch spending the turn's one `False` tick. Each
  fix added another field or another set, and each is correct in isolation.
- **The FSM is hard to read in isolation**, which matters because it is the safety-critical part —
  `_SPEECH_STATES` drives the self-hearing mute gate, and a state added to the wrong tuple deafens
  the robot or makes it answer itself.

The 2398-line test file is the symptom: filler-policy tests and state-machine tests sit in one
module because they share one object.

## Acceptance criteria

- [ ] A `FillerDriver` (or equivalently-named) collaborator owns: the RNG, the per-turn queue,
      `_filler_lang`, `_filler_delay`, `_filler_opened`, `_filler_next_at`, `_filler_recent_openers`,
      `_filler_last_stall`, and the three used-key sets. Its interface is roughly
      `arm(rng_seedless)`, `tick(now, elapsed, warm_keys, speaking: bool) -> str | None`,
      `reset_conversation()`.
- [ ] `FillerDriver` is **pure with respect to time and audio**: `now` is passed in, "is something
      speaking" is passed in, and it returns a key to play rather than calling into the assistant.
      No clock reads, no `tts` import, no session lock.
- [ ] A `VoiceWarmer` collaborator owns `_prewarm_canned`, `_prewarm_bank`, `_warm_one`,
      `_within_length_cap`, `_rewarm_when_quiet`, `_speak_greeting` and the `_canned` dict, exposing
      the cache as a read-only view to the session.
- [ ] The `_quiet_for_synth` gate is expressed as an injected predicate, so `VoiceWarmer` does not
      need to know about session states. (If **S4** lands first, this gate may reduce to a CPU-pacing
      concern rather than a correctness one — note the dependency.)
- [ ] `ConversationSession` retains the FSM, the lock, the timers, the wake tiers, the status
      projection and mic recovery, and is materially shorter — target under ~900 lines, with the
      exact figure recorded rather than negotiated after the fact.
- [ ] `tests/test_session.py` splits along the same seam into `test_filler_driver.py`,
      `test_voice_warmer.py` and a slimmer `test_session.py`, with **no loss of coverage**: every
      existing assertion survives in one of the three files.
- [ ] Every recorded filler regression keeps a named test: no back-to-back opener across a
      conversation seam; no repeated stall within one turn; no repeated stall within one wait after
      a queue rebuild; no overlap while `speech_in_flight()`; the `_played_thinking` latch behaviour
      when no opener is warm (both the "latch unconditionally" and "never latch" failures).
- [ ] Observable behaviour is unchanged end to end: the same `/params` keys with the same values,
      the same log lines, and a hands-free conversation is indistinguishable before and after.
- [ ] The extraction lands as its own commit(s) with no behaviour change mixed in, so a regression is
      bisectable.

## Suggested approach

Both candidates are *already almost pure* — that is what makes this a refactor rather than a
redesign, and it is the reason to do it in this order:

**1. `FillerDriver` first.** `ai/filler.py` already holds the choosing logic as pure functions with
an injected `Random`; what lives in the session is the state those functions are threaded through.
Move that state into a class in a new `ai/filler_driver.py` (keep `ai/filler.py` as the pure
policy layer it already is). `_tick_filler`'s body becomes the driver's `tick()`, returning a key
or `None`; the session keeps the two-line shim that calls `_speak_filler(key)` and the fallback to
the "Hmm". Preserve the docstrings verbatim — they carry the incident history and are the most
valuable thing in that code.

**2. `VoiceWarmer` second.** Mostly a move: the methods are already sequential, best-effort and
free of FSM logic apart from the `_quiet_for_synth` check. Inject that check as a callable.
`reprewarm_canned` stays a session method that delegates, because `face_track._register_settings_callbacks`
binds to it by name.

**3. Do not extract the wake-scan tier in this ticket.** It is genuinely entangled with the FSM
(`_scan_token` invalidation mirrors `_epoch`, and `_on_scan_done` re-enters `on_wake`), and pulling
it out is a separate design decision with its own risk. Note it as a possible follow-up.

Sequencing note: **R5** (streaming synthesis) may make a large fraction of the filler bank
unnecessary. If R5 is scheduled soon, consider doing it first — refactoring code that is about to
shrink is wasted motion. If R5 is not scheduled, this refactor stands on its own merits.
