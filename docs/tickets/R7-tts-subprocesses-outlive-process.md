# R7 — TTS subprocesses outlive the process

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

- [ ] `run()`'s `finally` calls `tts.stop()` before `_session.stop()`.
- [ ] After `SIGTERM` during a spoken reply, no `paplay` or `piper` process belonging to the
      exiting PID survives — verifiable with `pgrep -af 'paplay|piper'` immediately after
      `[face_track] Stopped.` appears in the log.
- [ ] A dashboard `POST /restart` issued mid-reply produces silence between the two processes,
      not overlapping speech.
- [ ] The forced-exit path (`lifecycle.arm_restart_deadline` → `os._exit`) is documented as NOT
      covered by this fix, since `os._exit` skips the `finally` by design — note in the code
      comment that `scripts/autostart.sh` is the backstop there.
- [ ] A regression test asserts that the shutdown sequence invokes `tts.stop()` (patch `ai.tts.stop`
      and drive the teardown, in the style of the existing `tests/test_tts.py` handle assertions).

## Suggested approach

Add `tts.stop()` as the first statement of `run()`'s `finally`, ahead of `stop_evt.set()` — killing
playback first means the ~1–2 s of teardown that follows happens in silence rather than under a
reply. `tts.stop()` already terminates both the synth and the playback handle and is a documented
no-op when nothing is running, so it is safe unconditionally.

Consider also calling it from `ConversationSession.stop()` for symmetry with `_end_session()`, so
any future caller that stops a session without going through `face_track.run()` gets the same
guarantee. Keep both: `run()` must not depend on the session existing (the `--no-wake` path still
constructs one, but a future headless mode might not).
