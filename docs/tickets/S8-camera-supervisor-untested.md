# S8 — `app/camera_supervisor.py` has no tests

> **Status: FIXED** — `test/camera-supervisor`. `tests/test_camera_supervisor.py`, 31 tests across 10
> classes, driven through a new `_step()` against a fake clock and a fake `try_open_camera`. Suite
> green (1270 passed, was 1239).
>
> **Verified by mutation, not by passing.** A test-only ticket that reports "31 tests, all green" has
> proved nothing — green is the state an empty test file is in too. Ten deliberate regressions were
> applied to `camera_supervisor.py` one at a time, each the undoing of a behaviour this ticket names,
> and the suite was re-run against each: **10 of 10 caught.**
>
> | mutation | caught |
> |---|---|
> | backoff ignores `cheap` (punishes free failures) | yes |
> | stall check ignores `showing_live` (kills the camera during video playback) | yes |
> | stall check drops the `and last` guard (kills a camera before its first frame) | yes |
> | swap queue back to depth 1 (silently evicts an unapplied swap) | yes |
> | `_acquire` stops priming the staleness clock | yes |
> | `_report_failure` logs unconditionally | yes |
> | retry probe keeps the 10 s startup Argus budget | yes |
> | `probe_now` is not consumed by the pass | yes |
> | `_acquire` forgets to reset `presence` | yes |
> | `--no-camera` no longer beats the stored setting | yes |
>
> Every one of those is a bug the module was written to prevent, three of them from recorded past
> incidents. The source was restored afterwards and the committed file is unchanged apart from the
> `_step()` extraction.

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

- [x] `tests/test_camera_supervisor.py` exists and runs in the standard suite. No camera, no
      GStreamer, no OpenCV device access, and nothing sleeps — 31 tests in 0.009 s.
- [x] Driven pass-by-pass against an injected clock and a fake `try_open_camera`. `run()`'s while
      body was extracted into `_step() -> float` exactly as the ticket suggested; `run()` keeps only
      the waiting, and `_interval`/`_first` moved onto the instance so a pass is complete on its own.
- [x] Covered: a successful probe enqueues `("live_source", cam)`, sets `_live`, clears `reason`,
      calls `note_frame_time` and resets `presence` — plus that it resets the accumulated backoff.
- [x] Covered: `camera_mode == "off"` releases a live camera and enqueues a `NullCamera`, and does
      nothing but report when there was none. `--no-camera` beats the stored setting, never probes,
      reports `locked=True` with the `"locked off by --no-camera"` reason, and surfaces it through
      `settings_locked()`. Also the case the ticket did not name: `--no-camera` releasing a camera
      already held must give the *locked* reason, not `"camera off (settings)"`, or the dashboard
      blames the wrong thing.
- [x] Covered: a live camera stale beyond `CAMERA_STALL_S` is released; one inside the window is
      not; one with `showing_live == False` is not. Plus **the `and last` guard** — `last_frame_t`
      of `0.0` means "no frame yet", not "a frame at time zero", and without it a camera is judged
      dead before it has had a chance to deliver anything.
- [x] Covered: backoff doubles to `CAMERA_RETRY_MAX_S` on expensive failures only, stays at
      `CAMERA_RETRY_INTERVAL_S` for a failure with an empty `device_signature()`, and drops back to
      base when an expensive run is followed by a cheap one. Monotonicity asserted over 12 passes.
- [x] Covered: `_replace_swap` fills to 3 then drops the oldest; `probe_now()` sets `force=True` on
      the open, is consumed by the pass, and short-circuits the wait loop.
- [x] Covered: `_report_failure` logs only on a change — and, separately, that it publishes the
      reason to the dashboard *every* time regardless, since only the log is rate-limited.

## Note on the `_step()` extraction

The one production change, and it is the ticket's own suggestion. Worth being explicit that it is a
pure move: the decision logic is byte-identical, the ordering is unchanged, and the two loop-carried
locals became instance attributes because that is what makes a single pass meaningful in isolation.
`_step()` also consumes `_probe_now` and publishes `next_probe_at`, so what a test drives is exactly
what the running supervisor does rather than a subset of it. `run()` is now the wait and nothing else.

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
