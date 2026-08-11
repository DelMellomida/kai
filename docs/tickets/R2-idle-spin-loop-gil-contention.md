# R2 — 200 Hz idle spin in the main loop

> **Status: FIXED** — `perf/idle-loop-frame-event`. Approach 2 (frame event), not the adaptive sleep:
> `CameraThread` sets a `threading.Event` when it stores a frame and the loop calls
> `wait_for_frame(NO_FRAME_WAIT)` instead of polling. Measured **185 Hz → 22 Hz, 8.4× fewer
> iterations** on the idle path. Suite green (1248 passed, was 1239).
>
> **Measured on the dev box, not the Jetson.** The reduction is a property of the loop and carries
> over, but the *point* of it — GIL headroom for the 15 Hz control thread — is a Jetson claim and
> the two acceptance criteria that would confirm it need the robot. They are unchecked below.
>
> One correction to the ticket: the wait is bounded by `WEB_PUBLISH_INTERVAL` (0.04), **not**
> `JAW_SEND_INTERVAL` (0.05) as the suggested approach proposed. 0.05 would have dropped
> `_publish_status` from 25 Hz to 20 Hz, which criterion 3 forbids — the ticket's own two criteria
> disagreed with each other, and the tighter one wins.

| | |
|---|---|
| **Tier** | 1 |
| **Severity** | Medium |
| **Effort** | Small |
| **Confidence** | Medium-High |
| **Lens** | Robotics |

## Location

- `face_track.py` — `run()`, the no-frame path: `if frame is None: time.sleep(NO_FRAME_SLEEP); continue`
- `config/tracking.py` — `NO_FRAME_SLEEP = 0.005`, and the GIL-ceiling note on `INFERENCE_FPS`
- `vision/camera.py` — `CameraThread.latest()`
- Per-iteration cost sites: `_jaw_on(args)` (two `settings.get()` calls), `_voice.speaking_openness()`,
  `_publish_status()`

## Problem

`CameraThread.latest()` is non-blocking and returns `None` until a fresh frame arrives. With no
camera at all — or between frames at the CSI's 30 fps — the main loop therefore iterates roughly
200 times a second, and each iteration does real Python work before it decides there is nothing to
do: two `settings.get()` calls under an `RLock` (`_jaw_on`), a `speaking_openness()` call that
takes the assistant lock and evaluates the envelope, and a `_publish_status()` time comparison.

All of that holds the GIL.

## Why it matters

`config/tracking.py` records the measurement that makes this matter: the bottleneck on this box is
the GIL, not CPU, memory or thermals. Raising `INFERENCE_FPS` from 15 to 22 collapsed the pure-Python
control thread from ~14 Hz to 6–10 Hz and produced visibly jerkier actuation. A permanent 200 Hz
Python loop is contending for exactly that resource, alongside the 15 Hz control thread, the 20 Hz
session tick and the ~30 blocks/s audio worker.

It is worst in the case where there is least to do: `--no-camera`, a camera that has not been
probed yet, or a stalled feed — precisely the degraded states where the servo and voice paths are
the only things still working and most need the headroom.

## Acceptance criteria

- [x] Iteration rate drops from ~200 Hz. Measured with a counter over a real `CameraThread` +
      `NullCamera` (the `--no-camera` path): **185.0 Hz → 22.0 Hz, 8.4×**. Lands at 25 Hz rather
      than the ~20 Hz the criterion predicted, for the reason in the banner. The 22 vs 25 gap is
      the dev box's timer granularity, not the design.
- [ ] **DEFERRED — needs the robot.** Jaw animation during a spoken reply unchanged to the eye, and
      `servo.send_jaw` still landing at its 20 Hz gate. The mechanism is sound — the loop still
      iterates at 25 Hz, comfortably above the 20 Hz gate, so no jaw frame can be *starved* — but
      "unchanged to the eye" is not something a unit test can report.
- [x] `_publish_status` still holds its 25 Hz gate. This is the criterion that chose the constant:
      `NO_FRAME_WAIT = WEB_PUBLISH_INTERVAL`, written as an alias so the two cannot drift, with
      `tests/test_settings.py::TestIdleWaitBounds` pinning it against `JAW_SEND_INTERVAL` as well.
      `/params` cadence and the `cam_retry_in_s` countdown are unaffected.
- [x] Frame-to-processing latency is not increased — and is in fact slightly *better*. The old path
      slept a fixed 5 ms and could not notice a frame that arrived 0.1 ms in; the loop now wakes on
      the store itself. `tests/test_camera.py::TestFrameReadyEvent` covers both halves (wakes early
      on a frame, returns promptly when none comes).
      **The `[face_track] … Nfps` half is unverified** — that needs a live camera.
- [ ] **DEFERRED — needs the robot.** `[control] N Hz` with `--no-camera` at or above its current
      value. This is the whole *point* of the change and the one number that would prove it: the GIL
      contention argument is a Jetson claim, and 8.4× fewer GIL-holding iterations should show up
      here as a higher control-thread rate. Worth capturing before and after on the next deploy.
- [x] Shutdown latency is unchanged. The wait is bounded at 0.04 s — 12× shorter than the previous
      worst case of a full loop pass — and `close()` sets the event so a parked waiter is released
      immediately rather than paying out its timeout. Both are covered by tests.

## Suggested approach

Two viable shapes; the second is cleaner but touches more:

1. **Adaptive sleep (smallest change).** Track consecutive `None` returns from `latest()` and scale
   the sleep from `NO_FRAME_SLEEP` up to a ceiling of `JAW_SEND_INTERVAL` (0.05 s). Reset to the
   floor on the first real frame, so a live camera keeps today's responsiveness and only a genuinely
   idle loop backs off. Keep `NO_FRAME_SLEEP` as the floor constant and add the ceiling next to it
   in `config/tracking.py` with a comment pointing at the GIL note.

2. **Frame event (cleaner).** Give `CameraThread` a `threading.Event` set in `_loop()` when a frame
   is stored and cleared by `latest()`. The main loop then does
   `cam_thread.wait_for_frame(timeout=JAW_SEND_INTERVAL)` instead of sleeping — it wakes immediately
   on a new frame and otherwise at the jaw rate, which is exactly the two things it needs to do.
   This removes the polling entirely rather than tuning it.

Either way, the per-iteration work is worth trimming too: hoist `_jaw_on(args)` behind a single
`settings.get("jaw_enabled")` read and keep `args.jaw` out of the loop (it is constant for the run),
and note that `speaking_openness()` returning `None` is the common case — it is cheap, but it is on
the hottest path in the process.

> **Not done, deliberately.** Two reasons. First, the premise is slightly off: `_jaw_on()` makes
> **one** `settings.get()` call, not two, so the saving is one attribute lookup rather than a lock
> acquisition. Second and mainly, the same work now happens 8.4× less often, which is a larger
> improvement than trimming it could be — and inlining `_jaw_on()` would cost the docstring
> explaining why `--jaw` is a hard AND ("no setting can conjure a jaw servo that isn't there"),
> which is the kind of comment this codebase is careful to keep next to the code it explains.
> If the loop ever needs to be cheaper per pass, this is still the place to look.
