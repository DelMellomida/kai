# R2 — 200 Hz idle spin in the main loop

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

- [ ] With `--no-camera`, the main loop's iteration rate drops to approximately the jaw update rate
      (~20 Hz, i.e. `1 / JAW_SEND_INTERVAL`) rather than ~200 Hz. Measure with a temporary counter
      or `py-spy`.
- [ ] Jaw animation during a spoken reply is unchanged to the eye and `servo.send_jaw` still lands
      at its 20 Hz gate — no dropped jaw frames introduced, `sess_*`/servo behaviour identical.
- [ ] `_publish_status` still runs often enough to hold its 25 Hz gate, so `/params` cadence and
      `cam_retry_in_s` countdowns are unaffected.
- [ ] With a live camera at 30 fps, frame-to-processing latency is not increased — a frame is picked
      up within one jaw tick of becoming available, and `[face_track] … Nfps` is unchanged.
- [ ] `[control] N Hz` measured with `--no-camera` is at or above its current value (this change
      should help it, never hurt it).
- [ ] Shutdown latency is unchanged — the loop must still notice `KeyboardInterrupt`/`SIGTERM`
      promptly, so any new wait is bounded well under a second.

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
