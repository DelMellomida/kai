# R11 — The control thread's loop body has no test

| | |
|---|---|
| **Tier** | 2 |
| **Severity** | Medium |
| **Effort** | Medium |
| **Confidence** | High |
| **Lens** | Robotics |

## Location

- `app/control_loop.py` — `run()`, the whole `while not stop_evt.is_set()` body
- `tests/test_thinking.py` — covers `draw_sweep`, `thinking_offset`, `ease_toward` and the
  amplitude/slew arithmetic, and nothing that calls `run()`
- `vision/controller.py` — `PDAxis.update`/`reset`, `TrackingTarget.snapshot` — the collaborators
  the loop sequences
- `config/tracking.py` — `CONTROL_FPS`, `CONTROL_STALE_TIMEOUT`, `PAN_MAX_STEP`
- `config/servo.py` — `PAN_DEADBAND`, `SEND_INTERVAL`

## Problem

`app/control_loop.py` was split out of `face_track.py` explicitly so it could be tested — its
docstring says "the sweep MATHS is pure and lives here as three small functions with the randomness
injected, so the whole parameter space can be swept in a test with no hardware and no clock", and
`tests/test_thinking.py` does exactly that, thoroughly.

`run()` itself is not covered. It takes `servo`, `target`, `voice` and `stop_evt` as arguments — all
four are already injectable, and `face_track.py` passes a `_NullServo` on `--no-servo` — so the
absence is not structural. What is missing is a harness that drives a few iterations with a fake
clock.

The untested part is not the arithmetic. It is the sequencing, which carries three behaviours each
added to fix an observed fault:

- **The hold branch.** No face, or a stale target, or `servo_tracking` off: send nothing, `reset()`
  the PD to `last_pan_cmd` so re-acquire glides. `PDAxis`'s docstring records what the reset is for
  ("a visible head twitch on every dropped detection").
- **The anchor return.** `sweep_settled` exists because `send()` is gated to 10 Hz while the loop
  runs at 15, so the easing's last steps toward zero can be dropped and leave the head physically a
  few degrees off `last_pan_cmd`. The retry condition —
  `if abs(anchor - servo.last_pan) <= PAN_DEADBAND or servo.send(anchor, servo.last_tilt)` — encodes
  both the deadband escape and the retry-until-it-lands rule in one line.
- **The slew clamp feeding back into the PD.** On clamping, `pan_pd.reset(pan_out)` re-anchors the
  controller to the angle actually commanded rather than the one it wanted.

## Why it matters

This is the servo control law. Everything a person sees the robot's head do goes through these
branches, and the sim/hardware gap makes them expensive to check any other way: the anchor-return
bug's symptom was "the head is parked a few degrees off and jumps on the next re-acquire", which is
measurable on the robot only by watching it, and only sometimes.

The immediate consequence is on the tickets already queued against this file. **R9** changes
`PDAxis.update`'s rounding, which is precisely the behaviour a harness here would pin — landing R9
first means writing any regression test against the new behaviour, never against the old, so nothing
proves the change did what it says. **R10** proposes either wiring or collapsing the tilt axis, which
means editing `servo.send(int(round(pan_out + sweep_off)), int(round(tilt_t)))` with no test standing
behind it. **R1** rewrites the link layer underneath this loop.

**S8** calls `app/camera_supervisor.py` "the only substantial untested module". That was true of the
supervisor as a unit; it is not true of the tree — this loop body is the other one, and it is closer
to the hardware.

## Acceptance criteria

- [ ] `tests/test_control_loop.py` exists and drives `run()` for a bounded number of iterations with
      no hardware, no sleeping and no real clock — a fake servo recording `send()` calls, a real
      `TrackingTarget`, a stub voice returning a `voice_status`, and a `stop_evt` that trips after N
      ticks.
- [ ] The clock is injected or patched, not slept through. `run()` currently calls `time.monotonic()`
      and `stop_evt.wait()` directly; patching `control_loop.time` is acceptable and is the smaller
      change, but whichever is chosen, the test must not take real seconds. The suite's ~26 s total
      is a property worth keeping.
- [ ] Covered, each as its own case, each named for the behaviour rather than the branch:
      - a tracked face produces a `send()` per tick with `|Δ| ≤ PAN_MAX_STEP`;
      - a clamped step re-anchors the PD, so the next tick's correction is computed from the
        commanded angle, not the wanted one;
      - no face → nothing is sent, and the PD is resynced to `last_pan_cmd`;
      - a target older than `CONTROL_STALE_TIMEOUT` holds, even with `face_present` true;
      - `servo_tracking` off holds, and switching it back on does not jump;
      - a thinking window with `servo.send()` returning `False` (the 10 Hz gate) still reaches the
        anchor: `sweep_settled` stays False and the anchor send is retried until it lands;
      - the anchor send is skipped when the head is already within `PAN_DEADBAND`, so a settled loop
        goes quiet and the firmware's idle-detach can fire;
      - both `thinking_sweep` and `servo_tracking` gate the sweep — the comment in `run()` warns
        specifically against dropping the `servo_tracking` half in a refactor, so pin it.
- [ ] `settings.get` is exercised through the real `settings` module with `_reset_for_tests()`, not
      stubbed — the gates are read live at 15 Hz and that is the contract worth testing.
- [ ] The `[control] N Hz` line's edge-triggering on face presence is covered, since
      `CONTROL_LOG_INTERVAL_S` was set from a measurement about log volume and a regression there is
      otherwise invisible.
- [ ] No production code changes beyond what injecting the clock requires. This is a test ticket; if
      `run()` needs restructuring to be testable, that is a finding to raise, not to do here.

## Suggested approach

The pattern already exists in `tests/test_session.py`: a fake collaborator, an injected `now`, and
the loop driven a tick at a time. `ConversationSession.tick(now)` is separate from `_tick_loop()`
for exactly this reason, and it is the shape to copy.

Two options for the clock, in order of preference:

1. **Extract the body.** A `_tick(state, now)` taking and returning the small carried state
   (`last_pan_cmd`, `sweep_off`, `thinking_since`, `sweep_settled`, `sweep_shape`) with `run()`
   reduced to the timing wrapper. That mirrors `tick()`/`_tick_loop()` in the session, makes the
   test read like the session's, and needs no patching. It is a real refactor, so it is a judgement
   call whether it belongs in a test ticket — if it is taken, keep it mechanical and land it
   separately from the tests.
2. **Patch `control_loop.time`.** Smaller and entirely local to the test file. A monotonic stub
   advancing by `CONTROL_INTERVAL` per call, plus a `stop_evt` whose `wait()` returns immediately and
   whose `is_set()` returns True after N ticks.

Take (2) first — it buys the coverage now and prices in nothing. Take (1) only if **R1** or **R5**
end up restructuring this file anyway.

Sequence this **before R9 and R10**, both of which change behaviour this harness would pin.
