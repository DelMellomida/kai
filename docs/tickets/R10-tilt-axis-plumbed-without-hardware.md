# R10 — The tilt axis is plumbed everywhere but has no hardware

| | |
|---|---|
| **Tier** | 4 |
| **Severity** | Low |
| **Effort** | Small |
| **Confidence** | High |
| **Lens** | Robotics |

## Location

- `arduino/servo_serial/servo_serial.ino` — `const int TILT_PIN = 10;   // no tilt hardware — pin left undriven`
- `face_track.py` — `--tilt` / `--flip-y` CLI flags, `_compute_targets()`'s `target_tilt`,
  `_annotate_frame()`, `_log_face()`
- `config/tracking.py` — `TILT_SCALE = 90`
- `vision/controller.py` — `TrackingTarget._tilt`, `set()`, `snapshot()`
- `app/control_loop.py` — `run()`: `pan_t, tilt_t, ... = target.snapshot()`,
  `servo.send(..., int(round(tilt_t)))`, and the hold branch's `servo.last_tilt`
- `servo/servo.py` — `send()`'s tilt clamp/deadband, `_last_tilt`, `last_tilt`
- `web/server.py` / `face_track._publish_status` — the `"tilt"` dashboard field
- `tests/test_servo.py`, `tests/test_controller.py` — tilt assertions

## Problem

A complete, fully-maintained vertical slice exists for an axis that drives nothing. The CLI flag,
the EMA smoothing, the PD target, the thread-safe holder, the serial wire format's second field,
the deadband, the dashboard field and the tests are all live. The firmware declares the pin and
never attaches a servo to it — the comment says so plainly.

This is not accidental dead code: the three-field protocol is deliberate and `send()`'s two-field
form exists specifically so pan/tilt commands don't clobber the jaw. But `TILT_SCALE`, `--flip-y`,
the tilt clamp and the tilt EMA are all computing a number that is transmitted and then discarded.

## Why it matters

Carrying cost on every refactor of the control path — every change to `TrackingTarget`,
`control_loop.run()` or `ServoSerial.send()` has to keep an unused axis correct, and every reader has
to work out which axes are real. It is also a small but nonzero runtime cost on the 15 Hz control
loop and the serial link (a wider line to write and parse on every send).

Low severity because it is harmless today. Listed because "which axes actually exist" should be
answerable from the code, and right now it is only answerable from one comment in the firmware.

## Acceptance criteria

Pick **one** of the two outcomes and make it explicit — the failure state is leaving it ambiguous.

**Option A — wire it.**
- [ ] A tilt servo is attached on `TILT_PIN` in the firmware, with the same limits and idle-detach
      handling as pan (see **R4**).
- [ ] `TILT_SCALE`, `--flip-y` and the tilt deadband are tuned on hardware and the values recorded
      in `config/tracking.py` / `config/servo.py` in the existing measured-comment style.
- [ ] Current draw on the shared rail is re-checked — `config/servo.py` documents that
      `SEND_INTERVAL` and `CONTROL_FPS` are brownout-sensitive, and a second SG90 changes that
      budget. `dmesg | grep "USB disconnect"` clean over a sustained tracking session.
- [ ] The tilt axis gets its own PD instance rather than riding the pan target unfiltered, matching
      how pan is driven.

**Option B — collapse it.**
- [ ] `--tilt` and `--flip-y` are removed or documented as no-ops; `TILT_SCALE` becomes a single
      documented constant (or goes).
- [ ] `TrackingTarget` keeps or drops its tilt field as a deliberate choice, documented.
- [ ] The **wire format keeps its second field reserved** — the firmware's `"pan,tilt"` and
      `"pan,tilt,jaw"` forms must continue to parse, so a future tilt does not need a protocol
      change and so an older firmware still works with a newer host.
- [ ] `servo.last_tilt` remains readable (the control loop's hold branch and the dashboard both use
      it) even if it never changes from 90.
- [ ] The dashboard stops displaying a tilt value that cannot move, or labels it as unwired.
- [ ] The firmware's `TILT_PIN` comment is updated to point at whichever decision was taken.

## Suggested approach

Option B is the honest default: there is no tilt hardware, the mount is not designed for it, and
`config/servo.py` records that the real fix for faster motion is a separate servo power supply
(docs plan Phase 6) — adding a second servo to the shared rail before that lands would make the
documented brownout worse.

Do it as a subtraction that stops at the protocol boundary. Everything above `ServoSerial.send()`
can lose the axis; `send()` itself keeps emitting the second field (as `self._last_tilt`, i.e. 90),
and the firmware keeps parsing it. That way the change is entirely host-side, is revertible, and a
future tilt servo needs only the parts that were removed — not a new wire format on both sides.

Whichever option is chosen, record the decision as a comment in `config/tracking.py` next to
`TILT_SCALE`, in the same style as the other measured notes there. The point of this ticket is
that the answer stops being implicit.
