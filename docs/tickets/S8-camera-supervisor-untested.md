# S8 — `app/camera_supervisor.py` has no tests

| | |
|---|---|
| **Tier** | 1 |
| **Severity** | Medium |
| **Effort** | Small |
| **Confidence** | High |
| **Lens** | Software |

## Location

- `app/camera_supervisor.py` — the whole module (231 lines): `CameraSupervisor.run()`,
  `_acquire()`, `_release()`, `_report_failure()`, `_replace_swap()`, `effective_mode()`
- `tests/` — no file imports `CameraSupervisor`

## Problem

No test in the suite imports or exercises `CameraSupervisor`. The module docstring explicitly
advertises testability — "it knows about `vision/` and `settings.py`; it does not know about Flask,
MediaPipe or the servo, so it can be driven with a fake camera and a fake clock" — and nothing
takes it up. `tests/test_camera.py` covers `vision/camera.py` (`CameraThread`, `NullCamera`,
`try_open_camera`) but stops at the supervisor boundary.

The untested logic is not trivial. It includes: the hot-swap ordering in `_acquire`/`_release`, the
`note_frame_time` grace period that stops a freshly-acquired camera being judged dead, the
`CAMERA_STALL_S` staleness check gated on `showing_live`, the exponential backoff that applies only
to *expensive* failures (`cheap = not device_signature()`), the `_probe_now` early-wake loop, and
the drop-oldest `_replace_swap` against a depth-3 queue.

## Why it matters

This is the only substantial module in the repo with this gap, and it is the one that decides
whether the robot believes it has a camera. Several of its behaviours exist because of specific
past bugs (a depth-1 queue silently evicting an unapplied swap; a stalled CSI pipeline reported as
a live feed at 0 fps; a backoff that punished free failures), and there is currently nothing
stopping a refactor from reintroducing any of them.

## Acceptance criteria

- [ ] `tests/test_camera_supervisor.py` exists and runs in the standard suite with no camera, no
      GStreamer and no OpenCV device access.
- [ ] `run()` is driven iteration-by-iteration against an injected clock and a fake
      `try_open_camera`, not by sleeping.
- [ ] Covered: a probe that succeeds swaps a `("live_source", cam)` item onto the queue, sets
      `_live`, clears `reason`, calls `note_frame_time` and resets `presence`.
- [ ] Covered: `camera_mode == "off"` on a live camera releases it and enqueues a `NullCamera`;
      `--no-camera` (`_forced_off`) wins over the stored setting and reports
      `locked=True` with the `"locked off by --no-camera"` reason.
- [ ] Covered: a live camera whose `last_frame_t` is older than `CAMERA_STALL_S` is released, and
      one that is `showing_live == False` (uploaded video playing) is **not**.
- [ ] Covered: backoff doubles to `CAMERA_RETRY_MAX_S` on expensive failures only — a failure with
      an empty `device_signature()` stays at `CAMERA_RETRY_INTERVAL_S`.
- [ ] Covered: `_replace_swap` on a full queue drops the oldest and keeps the newest, and
      `probe_now()` short-circuits the backoff wait.
- [ ] Covered: `_report_failure` logs only when the reason changes.

## Suggested approach

Follow the fake-collaborator style already used in `tests/test_session.py` and
`tests/test_camera.py`. Build the supervisor, call `configure(...)` with CLI facts, then invoke a
single pass of the loop body per test — the cleanest way is to extract the body of the `while` in
`run()` into a `_step(stop_evt) -> float` (returning the interval) so tests can call it directly
without the wait loop; the existing loop then becomes `while not stop_evt.is_set(): interval =
self._step(...)` plus the wait. That refactor is small, keeps `run()` readable, and is the same
shape as `ConversationSession.tick(now)` which the session tests already rely on.

Patch `app.camera_supervisor.try_open_camera` and `device_signature` at module scope, inject a
`_cam_thread` double exposing `last_frame_t` / `showing_live` / `note_frame_time`, and monkeypatch
`time.monotonic` or pass a clock. Assert on `swap_queue` contents and on `snapshot()`.
