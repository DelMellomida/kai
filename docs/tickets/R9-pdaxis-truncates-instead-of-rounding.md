# R9 — `PDAxis.update` truncates instead of rounding

| | |
|---|---|
| **Tier** | 1 |
| **Severity** | Low |
| **Effort** | Small |
| **Confidence** | Medium |
| **Lens** | Robotics |

## Location

- `vision/controller.py` — `PDAxis.update()`, `return int(self.current)`
- `app/control_loop.py` — `run()`: `pan_out = pan_pd.update(pan_t)`, the `PAN_MAX_STEP` clamp,
  `last_pan_cmd = pan_out`, `servo.send(int(round(pan_out + sweep_off)), ...)`
- `config/servo.py` — `PAN_DEADBAND = 1`

## Problem

`PDAxis.update()` returns `int(self.current)`, and `int()` truncates toward zero rather than
rounding. Every commanded angle is therefore biased downward by up to 1° — and because the bias is
toward zero, it is asymmetric about the 90° centre: it pulls angles above 90 down and angles below
90 up in magnitude terms, which is a systematic left/right asymmetry rather than a uniform offset.

The control loop then stores that truncated integer in `last_pan_cmd`, which is used as (a) the
slew-clamp reference, (b) the hold-branch anchor, and (c) the value `pan_pd.reset()` is re-synced
to when the clamp fires. So the truncation is not merely a display detail on the way to the wire —
it feeds back into the controller's own state.

`self.current` itself stays float inside `PDAxis`, so the error does not accumulate without bound;
it is a bounded sub-degree bias, not drift.

## Why it matters

`PAN_DEADBAND` is 1°, which is exactly the resolution at which a sub-degree bias stops being
invisible: `config/servo.py` documents that the PD asks for ~1° per tick during slow tracking, and
that the deadband was lowered to 1 specifically so those requests are not swallowed. A consistent
downward truncation interacts directly with that threshold — some 1° requests round to 0° of
commanded change and are dropped.

Practical effect: slightly different tracking behaviour on either side of centre, and a hold anchor
up to a degree away from where the controller believes the head is, which is the same class of
desync the sweep-settle logic in `control_loop.run()` exists to prevent.

## Acceptance criteria

- [ ] `PDAxis.update()` no longer truncates — either it rounds (`int(round(self.current))`) or it
      returns the float and rounding happens once, at the single send site.
- [ ] `last_pan_cmd`, the `PAN_MAX_STEP` clamp and `pan_pd.reset(pan_out)` all operate on the same
      representation, with no second implicit truncation introduced anywhere in
      `app/control_loop.py`.
- [ ] The hold branch's anchor (`anchor = int(round(last_pan_cmd))`) and the sweep-settle comparison
      against `servo.last_pan` remain consistent — a settled sweep still reaches the
      `abs(anchor - servo.last_pan) <= PAN_DEADBAND` exit and sets `sweep_settled`.
- [ ] `tests/test_controller.py` gains a case pinning the rounding behaviour at a half-degree
      boundary (e.g. a target that drives `current` to 90.5 produces 91, not 90), and the existing
      `PDAxis` derivative-priming tests still pass unchanged.
- [ ] On hardware: a slow left-to-right sweep and the mirrored right-to-left sweep reach
      symmetric extremes, and `[face_track] pan=N°` shows no systematic offset between the two
      directions.

## Suggested approach

Preferred: change `PDAxis.update()` to `return int(round(self.current))`. It is the smallest change,
keeps the existing `-> int` contract that `tests/test_controller.py` and any other caller rely on,
and fixes the feedback into `last_pan_cmd` at the source.

The alternative — returning `float` and rounding only in `servo.send(int(round(...)))` — is
arguably more correct (it keeps sub-degree precision in `last_pan_cmd` and the slew clamp) but it
changes the public return type of a class with existing tests, and the extra precision buys nothing
the servo can act on at 1° resolution. Take it only if the slew clamp is being reworked anyway.

Do not change `PAN_DEADBAND` as part of this. `config/servo.py` documents its value and the
stair-stepping incident that set it; if rounding changes the feel of slow tracking, that is a
separate hardware-tuning decision with its own before/after measurement.
