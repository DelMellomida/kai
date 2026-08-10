# R4 — Firmware clamps to 0–180 while the host clamps to 10–170

> **Status: FIXED** — `fix/firmware-servo-limits`. The sketch gained `ANGLE_MIN`/`ANGLE_MAX` (10/170)
> and a strict `parseAngle()` that replaces every `String::toInt()` coercion; a line is now applied
> whole or not at all. Suite green (1244 passed, was 1239).
>
> **Two things this ticket cannot close from a keyboard.** The sketch was not compiled — there is no
> Arduino toolchain on the Jetson or the dev box, so the C++ is reviewed but unbuilt. And the change
> does nothing until someone **flashes the board**; until then the robot is running the old firmware
> regardless of what is on `main`. The on-hardware criterion below stays unchecked for both reasons.

| | |
|---|---|
| **Tier** | 1 |
| **Severity** | Medium |
| **Effort** | Small |
| **Confidence** | Medium |
| **Lens** | Robotics |

## Location

- `arduino/servo_serial/servo_serial.ino` — `loop()`, the three `constrain(..., 0, 180)` calls
  (`J<angle>` branch, 3-field jaw branch, pan)
- `config/servo.py` — `SERVO_MIN = 10`, `SERVO_MAX = 170`
- `servo/servo.py` — `send()`, `send_jaw()` (where those limits are currently enforced)

## Problem

`SERVO_MIN`/`SERVO_MAX` are documented as protecting the Tower Pro SG90 from overshooting into its
mechanical stop, and the host applies them in `send()` and `send_jaw()`. The firmware does not: it
constrains to the full `0..180`, so the limit exists only as long as every byte on the wire arrives
intact.

The link is deliberately fire-and-forget — no checksum, no echo, no ack — and `String::toInt()`
returns `0` for anything it cannot parse. A single corrupted or truncated line therefore commands a
hard slam to 0°, which is outside the protected range and into the stop.

## Why it matters

The CH340 adapter is documented as flapping on/off the bus under servo brownout, so corruption on
this link is not hypothetical — it is correlated with the exact condition (high current draw) that
makes a stall worse. The consequence is a stall current spike on the same shared rail that caused
the flap, which is a self-reinforcing failure. The one component positioned to enforce the
mechanical limit unconditionally is the one that doesn't.

## Acceptance criteria

- [x] Firmware constrains pan and jaw to the same window as the host (10–170), named as
      `ANGLE_MIN`/`ANGLE_MAX` at the top of the sketch with a comment pointing at `config/servo.py`.
      **The comment is backed by a test**, which the criterion did not ask for and should have:
      `tests/test_servo.py::TestFirmwareAngleLimits` reads the real `.ino` and fails if the two
      constants ever drift. A promise to keep two files in step, in two languages, one of which
      nothing in this repo executes, is exactly the kind that quietly stops being true.
      One window covers both axes — `JAW_OPEN` (config/tracking.py) is already pinned at `SERVO_MAX`.
- [x] A line containing any character outside `[0-9,]` is rejected outright rather than coerced.
      `parseAngle()` returns false on any non-digit and the line is dropped with no servo write.
      Applied whole or not at all: a good pan with a corrupt jaw moves nothing. **The tilt field is
      validated too, then discarded** — there is no tilt hardware (R10), but garbage in tilt means
      the line is corrupt and the pan field beside it has no better claim to being intact.
- [x] An empty numeric field is rejected rather than treated as 0 — `parseAngle()` fails on
      `length() == 0`, covering `",90"`, `"90,"` and `"J"`.
- [x] `G:` gesture lines and the `J` prefix continue to parse exactly as before. Both branches are
      untouched apart from the `J` branch's parse call; `G:` is matched and dispatched first, ahead
      of any numeric handling, exactly as it was.
- [ ] **DEFERRED — needs the robot.** Verified on hardware: deliberately corrupted lines over
      `servo/servo_serial.py`'s interactive mode produce no motion, and the head never travels past
      the mechanical limits. **Also unbuilt** — no Arduino toolchain was available, so the sketch has
      not been compiled. Flash and run this before trusting the change.
- [x] The host-side clamps in `servo/servo.py` are left in place — defence in depth, not a
      relocation. `servo/servo.py` is not modified by this ticket at all, and the existing
      `SERVO_MIN`/`SERVO_MAX` assertions pass unchanged.

## Suggested approach

Two independent changes in the sketch, both small:

1. **Limits.** Add `const int ANGLE_MIN = 10; const int ANGLE_MAX = 170;` and use them in every
   `constrain()`. Note in the comment that these mirror `config/servo.py` and that the host applies
   them too — the firmware copy is the one that survives a corrupted wire.
2. **Validation.** Replace the `toInt()` coercion with a small helper that parses a field and
   reports failure, e.g. `bool parseAngle(const char *s, int len, int &out)` returning `false` on
   an empty field or any non-digit character. Any parse failure aborts the whole line (no partial
   application — a good pan with a corrupt jaw must not move the pan either, or a truncated line
   becomes a half-command).

This ticket pairs naturally with **R3** (non-blocking firmware parser), which replaces
`Serial.readStringUntil` with a fixed `char` buffer — the validation above is easier to write
against that buffer than against an Arduino `String`. If both are scheduled, do R3 first and fold
this in; if only one, this one is the safety-relevant half and stands alone.
