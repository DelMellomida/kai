# R9 — `PDAxis.update` truncates instead of rounding

> **Status: FIXED** — `fix/pd-axis-rounding`. `update()` now returns `int(round(self.current))`.
> Suite green (1176 passed, 2700 subtests). **One acceptance criterion is deferred**: the on-hardware
> left/right symmetry check needs the robot and has not been run — see the checklist below.

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
rounding. Every commanded angle is therefore biased downward by up to 1°.

> **Correction, made while fixing this.** The original write-up claimed the bias was *asymmetric
> about the 90° centre* — "it pulls angles above 90 down and angles below 90 up in magnitude terms".
> That is wrong. Servo angles are clamped to `0..180` and are therefore always positive, so
> truncation toward zero is simply `floor()` for every value in range: a **uniform** downward bias,
> not a left/right asymmetry. The defect and the fix are unchanged; only the characterisation was
> wrong, and the on-hardware check below is weaker evidence than first stated because there is no
> asymmetry to observe.

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

- [x] `PDAxis.update()` no longer truncates — it returns `int(round(self.current))`. The
      float-returning alternative was rejected: it changes a public return type with existing tests,
      and the extra precision buys nothing a 1°-resolution servo can act on.
- [x] `last_pan_cmd`, the `PAN_MAX_STEP` clamp and `pan_pd.reset(pan_out)` all operate on the same
      representation, with no second implicit truncation introduced anywhere in
      `app/control_loop.py`. (`app/control_loop.py` was not modified; its existing
      `int(round(pan_out + sweep_off))` at the send site now rounds an already-rounded value.)
- [x] The hold branch's anchor (`anchor = int(round(last_pan_cmd))`) and the sweep-settle comparison
      against `servo.last_pan` remain consistent — covered by the existing `tests/test_thinking.py`
      sweep cases, which pass unchanged.
- [x] `tests/test_controller.py` gains cases pinning the rounding behaviour, and the existing
      `PDAxis` derivative-priming tests still pass unchanged.
      **The criterion as originally written was wrong** — it asked for `90.5 → 91`, but Python's
      `round()` is half-to-even, so 90.5 → 90. Rather than hand-roll half-up rounding to satisfy a
      criterion written without checking, the tests pin `92.8 → 93` and `92.2 → 92` (the real
      defect, either side of it) plus a swept invariant: the command is never more than half a
      degree from `self.current`, for five gains × five targets. Half-to-even at an exact `.5` is
      immaterial at servo resolution and matches the `int(round(...))` already used at the send site.
- [ ] **DEFERRED — needs the robot.** On hardware: a slow left-to-right sweep and the mirrored
      right-to-left sweep reach symmetric extremes, and `[face_track] pan=N°` shows no systematic
      offset between the two directions. Note this check is weaker than it first appeared: since the
      bias was uniform rather than asymmetric (see the correction above), what it would actually show
      is a half-degree shift in both directions, which is at the edge of what is observable. The unit
      tests are the stronger evidence here.

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
