# S1 — The session RLock is held across disk I/O on two paths

| | |
|---|---|
| **Tier** | 1 |
| **Severity** | Medium |
| **Effort** | Small |
| **Confidence** | Medium-High |
| **Lens** | Software |

## Location

- `ai/session.py` — `tick()`, the `STATE_LISTEN_SPEECH` branch calling `_finish_utterance(now, reason="max_utterance")`
- `ai/session.py` — `tick()`, the `STATE_SCAN_SPEECH` branch calling `_finish_scan(now, reason="too_long")`
- `ai/session.py` — `_finish_utterance()`, `_finish_scan()` and their post-`with` bodies
- `ai/audio_debug.py` — `UtteranceRecorder.record()` (the WAV write)

## Problem

`_finish_utterance()` and `_finish_scan()` both do their state work inside `with self._lock:` and
then deliberately perform the expensive part — `process_utterance()` / `transcribe_async()` and
`self._recorder.record(...)` — *after* the block. The comments state the intent explicitly:
"After the dispatch: … it stays off the tick thread's critical section" and "recording inside the
lock above would put a WAV write on the tick thread in the middle of a state transition."

That holds when the caller is `_on_audio`, which releases the lock before dispatching. It does
**not** hold when the caller is `tick()`: `tick()` runs its whole body inside `with self._lock:`,
and because `self._lock` is an `RLock`, the inner `with` is re-entrant and the code "outside" the
inner block is still inside the outer one. So on the `max_utterance` and `too_long` timeout paths,
the WAV write happens with the session lock held.

The lock is correctly re-entrant by design (the comments at both call sites acknowledge the
re-entry) — what was missed is that re-entrancy also extends the critical section over the
post-lock code.

## Why it matters

`UtteranceRecorder.record()` writes up to `CAPTURE_HARD_CAP_S` of 16 kHz mono int16 — on the order
of a megabyte — through `wave` plus a JSONL append. The audio worker needs the same lock ~30 times
a second in `_on_audio` to run the VAD. A slow write (a full `/tmp`, a busy SD card, and this box
is documented as mounting its rootfs with known ext4 errors) stalls the audio path, which shows up
as dropped blocks (`sess_blocks_dropped`), input overflows, and potentially a spurious mic-stall
reopen.

It only bites when debug capture is enabled, which is off by default — but debug capture is exactly
the feature you turn on when something is already going wrong, so the failure mode is "the
diagnostic tool degrades the thing being diagnosed."

## Acceptance criteria

- [ ] No `UtteranceRecorder.record()` or `annotate()` call executes while the session lock is held,
      from **any** caller — including `tick()`.
- [ ] Verified structurally, not by inspection: a test that patches the recorder with a double
      whose `record()` asserts the session lock is not held (e.g. by attempting a non-blocking
      acquire from another thread and requiring success) passes for all four entry paths —
      `_on_audio` hangover, `tick` max-utterance, `_on_audio` scan hangover, `tick` scan too-long.
- [ ] The same guarantee covers `process_utterance()` and `transcribe_async()` dispatch.
- [ ] State-machine behaviour is unchanged: `max_utterance` still transitions
      `LISTEN_SPEECH → BUSY` on the same tick, `too_long` still transitions `SCAN_SPEECH → IDLE`
      with the long cooldown applied, and the existing `tests/test_session.py` cases for both keep
      passing untouched.
- [ ] The misleading "outside the lock" / "off the tick thread's critical section" comments are
      corrected to describe what the code now actually guarantees.

## Suggested approach

Split each method into a locked half and a dispatch half, so the guarantee is structural rather
than a property of who called:

- `_close_utterance_locked(now, reason) -> dict | None` — caller must hold the lock; does the state
  checks, `harvest_utterance()`, the short-blip discard, `_set_state(BUSY)`, and returns everything
  the dispatch needs (audio, rate, epoch, context, spoken_s, manual, truncated) or `None` if there
  is nothing to dispatch.
- `_dispatch_utterance(payload)` — caller must NOT hold the lock; calls `process_utterance`,
  `recorder.record`, and handles the `"error" in result` re-entry.

`_finish_utterance()` becomes the lock-free composition of the two (for `_on_audio`), and `tick()`
gains a small pending-dispatch mechanism: set a local, fall out of the `with` block at the end of
`tick()`, then dispatch. Mirror the same split for `_finish_scan`.

The simplest version of the pending mechanism keeps `tick()`'s shape intact: collect a
`pending: list[tuple[str, dict]]` inside the lock and drain it immediately after the `with` block,
which is also where `_heartbeat` could eventually move. Note that `_on_audio` already uses exactly
this pattern (`pending = "scan" | "turn"`, dispatched after the lock) — this makes `tick()` consistent
with it rather than inventing a new idiom.
