# R8 — No liveness watchdog on the inference loop itself

| | |
|---|---|
| **Tier** | 2 |
| **Severity** | Medium |
| **Effort** | Medium |
| **Confidence** | Medium |
| **Lens** | Robotics |

## Location

- `face_track.py` — `run()`'s `while True` inference loop (the unwatched thread)
- Existing watchdogs, for contrast: `app/control_loop.py` (`CONTROL_STALE_TIMEOUT`),
  `app/camera_supervisor.py` (`CAMERA_STALL_S`), `ai/mic_stream.py` (`MIC_STALL_S` +
  `MIC_REOPEN_BACKOFF_S`), `ai/session.py` (`SESSION_BUSY_MAX_S`, `SESSION_SPEAK_MAX_UNKNOWN_S`),
  `app/lifecycle.py` (`arm_restart_deadline`)
- `web/state.py` / `web/server.py` — where a heartbeat would be published

## Problem

Every subsystem in this robot is watched by something except the one that does the perception.

The control thread holds if its target goes stale. The camera supervisor notices a feed that stops
delivering. The mic worker reopens after `MIC_STALL_S` of silence. The session has a busy timeout, a
speak deadline and a mic-lost check. `lifecycle` will force-exit a shutdown that wedges. But if
`face_track.run()`'s loop stops iterating — MediaPipe or cv2 hung in native code, where nothing
raises and `KeyboardInterrupt` cannot land until the call returns — nothing notices.

The process stays convincingly alive: Flask answers, the session ticks, the wake word fires, the
servos hold their last commanded position, the camera supervisor keeps reporting a healthy source.
The only symptom is that the head stops following anyone and `presence` goes stale, which the
session is explicitly designed to read as "unknown, fail open."

## Why it matters

A half-working robot is the exact failure mode the rest of this codebase works hardest to
eliminate — the `_release()` stall check exists so the dashboard stops "claiming a feed it no longer
has", and `lifecycle.arm_restart_deadline`'s docstring calls a restart that reports success and does
nothing "the worst possible outcome for a recovery control." A dead inference loop is the same
category and is currently undetectable from `/params`.

It is also the one failure that the existing recovery ladder would actually fix: `/restart` rebuilds
MediaPipe from scratch.

## Acceptance criteria

- [ ] The inference loop publishes a monotonic heartbeat (timestamp and/or iteration counter) on
      every pass, at negligible cost — a single assignment, no lock on the hot path.
- [ ] A supervising thread that is **not** the inference loop evaluates that heartbeat. The camera
      supervisor thread is the natural host: it already runs for the life of the process, already
      holds `_cam_thread`, and already owns staleness logic.
- [ ] `/params` exposes the loop's health — at minimum `loop_alive` (bool) and `loop_stale_s`
      (float) — so a wedged loop is visible over ssh and on the dashboard rather than inferred from
      motionless servos.
- [ ] The staleness threshold accounts for the loop's legitimate slow paths: a 10 s Argus probe does
      **not** block this loop (it is on the supervisor thread), but frame-processing spikes and the
      no-frame path do vary the cadence. Set the threshold well above the worst legitimate gap and
      document the measurement, in the style of `CAMERA_STALL_S`.
- [ ] The frontend surfaces a stale loop as an explicit degraded state — the same treatment
      `cam_source="none"` + `cam_reason` gets — not a frozen-looking normal UI.
- [ ] Escalation is **configurable and off by default**: a new `config/tracking.py` constant decides
      whether a persistently stale loop calls `lifecycle.request_restart()` or only reports. The
      default reports, for the same reason `REBOOT_ENABLED` defaults off — an automatic restart loop
      on a mis-tuned threshold is worse than the fault it chases.
- [ ] A test drives the detector with an injected clock and a stalled heartbeat and asserts the
      state transition, with no camera and no MediaPipe. (Pairs naturally with **S8**, which
      introduces the supervisor's test harness.)

## Suggested approach

Add the heartbeat where the loop already touches shared state. The cheapest correct shape is a
module-level float in `face_track.py` updated at the top of each iteration, mirroring
`vision/presence.py`'s design — a module-level lock plus module functions, one instance per process
for free, importable without dragging in the tracking stack:

```
# vision/loop_health.py, sketch — same shape as vision/presence.py
_lock = threading.Lock()
_last_tick = 0.0
_iterations = 0

def mark(now=None): ...
def snapshot(now=None) -> tuple[float, int, bool]: ...   # (age, iterations, is_fresh)
```

`face_track.run()` calls `loop_health.mark()` once per iteration. `CameraSupervisor.run()` reads
`snapshot()` on each of its passes (it already wakes at least every 0.25 s during its wait loop) and
folds `loop_alive` / `loop_stale_s` into `set_state()`, which `_publish_status()` already forwards
to `/params`.

Deliberately **not** in scope: making the loop itself interruptible. A hang inside MediaPipe's
native call cannot be unwound from Python — `install_signal_handlers()`'s `KeyboardInterrupt` cannot
land until the call returns, which is why the escalation path is a process restart rather than a
recovery in place. Say so in the comment so the next reader does not try.

One measurement to take first: instrument the loop for a session and record the p99 inter-iteration
gap with a camera live, with `--no-camera`, and during a `/video` client's JPEG load. The threshold
should sit comfortably above the worst of those; guessing it is how this becomes a source of
spurious restarts.
