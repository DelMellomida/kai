# R7 — TTS subprocesses outlive the process

> **Status: FIXED** — `fix/tts-outlives-shutdown`. `tts.stop()` is now the first statement of
> `face_track.run()`'s `finally`, and `ConversationSession.stop()` cancels speech before releasing
> the mic. Suite green (1176 passed). **Two acceptance criteria are deferred**: both are on-robot
> observations (`pgrep` after SIGTERM, and a mid-reply `POST /restart`) — see the checklist.

| | |
|---|---|
| **Tier** | 1 |
| **Severity** | Medium |
| **Effort** | Small |
| **Confidence** | High |
| **Lens** | Robotics |

## Location

- `face_track.py` — `run()`, the `finally` block
- `ai/tts.py` — `stop()`, `play()`, `_run_piper()`
- `ai/session.py` — `ConversationSession.stop()`

## Problem

`run()`'s `finally` releases the mic and Porcupine (`_session.stop()`), MediaPipe, the camera and
the serial port — but never cancels speech. `ConversationSession.stop()` sets the tick-thread stop
event, joins the tick thread and calls `self._mic.stop()`; it does not call `tts.stop()`. The only
paths that cancel playback are `_end_session()` and `_begin_speech()`, neither of which runs on the
shutdown path.

The speak workers are daemon threads, so the interpreter kills them at exit — but `paplay` and
Piper are *child processes*, not threads. They are re-parented and keep running after
`[face_track] Stopped.` is printed.

## Why it matters

`scripts/autostart.sh` relaunches on `EXIT_RESTART` within seconds, so the replacement process
starts talking over audio the previous process left in the air. The new process's `_last_end` /
`_gate_until` mute gate knows nothing about that audio, so the self-hearing gate is open while it
is playing — which is exactly the condition under which Kai hears and answers itself. It is also
invisible in the log: the old process has already printed its clean shutdown line.

## Acceptance criteria

- [x] `run()`'s `finally` calls `tts.stop()` before `_session.stop()` — it is the **first**
      statement of the block, ahead of `stop_evt.set()`, so the 1–2 s of teardown that follows also
      happens in silence rather than under a half-spoken reply. `face_track.py` now imports
      `tts` alongside `rag`.
- [ ] **DEFERRED — needs the robot.** After `SIGTERM` during a spoken reply, no `paplay` or `piper`
      process belonging to the exiting PID survives (`pgrep -af 'paplay|piper'` immediately after
      `[face_track] Stopped.`).
- [ ] **DEFERRED — needs the robot.** A dashboard `POST /restart` issued mid-reply produces silence
      between the two processes, not overlapping speech.
- [x] The forced-exit path (`lifecycle.arm_restart_deadline` → `os._exit`) is documented as NOT
      covered by this fix, since `os._exit` skips the `finally` by design — recorded in the code
      comment at the call site, naming `scripts/autostart.sh`'s `wait_for_capture_device` as the
      backstop.
- [x] A regression test asserts that the shutdown sequence invokes `tts.stop()`.
      **Scoped differently from the ticket's suggestion**, and worth being explicit about:
      `face_track.run()` has no test harness — it is an unbounded loop wiring live hardware — so the
      assertion lives on `ConversationSession.stop()`, which the existing `SessionCase` fixture
      already patches `ai.session.tts.stop` for. Three cases in `tests/test_session.py::TestStop`:
      speech is cancelled, cancelled **before** the mic is released (ordering), and `stop()` is
      idempotent. The one line inside `run()`'s `finally` remains covered by inspection only.
- [x] **Added beyond the ticket:** `ConversationSession.stop()` also cancels speech, placed after
      the tick-thread join (so nothing can start a new line behind it) and before `self._mic.stop()`.
      The ticket floated this as "consider"; it is what the regression test can actually reach, and
      `stop()` is reachable independently of `face_track.run()`.

## Suggested approach

Add `tts.stop()` as the first statement of `run()`'s `finally`, ahead of `stop_evt.set()` — killing
playback first means the ~1–2 s of teardown that follows happens in silence rather than under a
reply. `tts.stop()` already terminates both the synth and the playback handle and is a documented
no-op when nothing is running, so it is safe unconditionally.

Consider also calling it from `ConversationSession.stop()` for symmetry with `_end_session()`, so
any future caller that stops a session without going through `face_track.run()` gets the same
guarantee. Keep both: `run()` must not depend on the session existing (the `--no-wake` path still
constructs one, but a future headless mode might not).
