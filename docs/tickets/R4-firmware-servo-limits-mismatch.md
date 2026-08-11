# R4 — Firmware clamps to 0–180 while the host clamps to 10–170

> **Status: FIXED** — `fix/firmware-servo-limits`. The sketch gained `ANGLE_MIN`/`ANGLE_MAX` (10/170)
> and a strict `parseAngle()` that replaces every `String::toInt()` coercion; a line is now applied
> whole or not at all. Suite green (1244 passed, was 1239).
>
> **Compiled and FLASHED on 2026-08-11.** The board is running this firmware now.
>
> **Correction — an earlier version of this banner said "there is no Arduino toolchain on the Jetson
> or the dev box, so the C++ is reviewed but unbuilt". That was wrong, twice.** The check was run on
> the Windows dev box rather than the robot, and it grepped for `gcc-avr`, which is a Debian *package*
> name and never a binary — the binary is `avr-gcc`. The Jetson has had a complete toolchain the whole
> time: `avr-gcc`, `avr-g++`, `avrdude 6.3`, `arduino-builder 1.3.25`, `arduino-core-avr 1.8.4`, and
> the Servo library in `~/Arduino/libraries`. Nothing was blocking this but a bad search.
>
> **Build.** Compiles clean under `-warnings all`, zero warnings, and comes out *smaller* than the
> firmware it replaces — `parseAngle()` costs less than the several `String::toInt()` instantiations
> it removes:
>
> | | flash | RAM |
> |---|---|---|
> | old (`main`) | 6262 bytes | 262 bytes |
> | new (R4) | **6122 bytes** | 262 bytes |
>
> **Board, finally identified.** It enumerates as a bare CH340 (`1a86:7523`) with no Arduino VID/PID,
> so the board type is not discoverable from USB and had to be probed. It is an **ATmega328P
> (signature `0x1e950f`) with an optiboot-class bootloader at 115200 baud** — STK500v1, hardware
> version 3, firmware 4.4. Not the 57600 Nano bootloader; 57600 and 19200 both fail to sync. Record
> this here, because the next person will have the same question and the USB descriptor will not
> answer it.
>
> **Flash verified twice**: avrdude's own post-write verify (6122 bytes), plus an independent
> readback-and-diff — **0 of 6122 bytes mismatched**. The pre-flash firmware was read off the chip
> first and kept at `~/firmware-backups/servo_serial-PRE-R4-20260811-083436.hex`
> (symlinked `latest-pre-r4.hex`), so the previous build can be restored without rebuilding it.

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
- [~] **Mostly done on hardware; one half needs eyes on the robot.** Flashed 2026-08-11 and exercised
      over the live serial link with `face_track.py` stopped:
      * boots and prints `READY`;
      * accepts every legal form — `pan,tilt`, `pan,tilt,jaw`, `J<angle>`, `G:<code>`;
      * survives every corrupt form — empty fields (`,90`, `90,`, `J`), letters for digits (`9O,90`,
        `1l0,90`, `Jab`), signed values (`-40,90`), a run-together line (`90,9012,90`), raw binary
        noise, and truncated numbers;
      * **does not wedge** — still answers a reset with `READY` after all of it, which is the failure
        a hand-rolled parser would actually produce.

      **What could not be checked from here: that the rejected lines produced no MOTION.** The link is
      fire-and-forget by design — no checksum, no echo, no ack — so the board emits nothing to
      distinguish "rejected the line" from "moved the servo", and `servo/servo.py` tracks its own
      `last_pan` rather than reading the board. Someone standing at the robot needs to watch the head
      while corrupt lines are sent. `/tmp/r4/probe.py` on the Jetson sends exactly that sequence.
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
