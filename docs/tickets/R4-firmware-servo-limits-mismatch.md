# R4 — Firmware clamps to 0–180 while the host clamps to 10–170

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

- [ ] Firmware constrains pan and jaw to the same window as the host (10–170), with the limits
      named as constants at the top of the sketch and a comment pointing at `config/servo.py` as
      the source of truth they must be kept in step with.
- [ ] A line containing any character outside `[0-9,\n]` (for the numeric forms) is rejected
      outright rather than coerced through `toInt()` — no servo write happens for a malformed line.
- [ ] An empty numeric field (e.g. `",90\n"`, `"J\n"`) is rejected rather than treated as 0.
- [ ] `G:` gesture lines and the `J` prefix continue to parse exactly as before.
- [ ] Verified on hardware: sending deliberately corrupted lines over `servo/servo_serial.py`'s
      interactive mode (or a raw `screen`/`python -m serial.tools.miniterm` session) produces no
      motion, and the head never travels past the mechanical limits.
- [ ] The host-side clamps in `servo/servo.py` are left in place — this is defence in depth, not a
      relocation. `tests/test_servo.py`'s existing `SERVO_MIN`/`SERVO_MAX` assertions still pass.

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
