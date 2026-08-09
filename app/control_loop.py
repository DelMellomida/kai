"""The servo control thread — decoupled from MediaPipe inference.

Perception and actuation run at different rates on purpose. The inference loop publishes a target
whenever it has one; this thread drives the pan PD controller toward the LATEST target at a fixed
CONTROL_FPS, so the head glides continuously between inference ticks instead of stepping on them.
See the GIL note in config/tracking.py for why raising the inference rate is not the alternative.

It also owns the "thinking" pan sweep: while the assistant is working out a reply the head drifts
gently, riding on top of whatever angle it would otherwise hold. The sweep MATHS is pure and lives
here as three small functions with the randomness injected, so the whole parameter space can be
swept in a test with no hardware and no clock (tests/test_thinking.py).

Dependencies are the servo, a TrackingTarget and the voice assistant's status — passed in, not
imported from face_track. Nothing here touches Flask, the camera or the conversation session.
"""

from __future__ import annotations

import math
import random
import threading
import time
from typing import NamedTuple

import settings
from ai.voice_assistant import STATUS_THINKING, STATUS_TRANSCRIBING
from config.servo import PAN_DEADBAND   # so the loop knows when a send would be a no-op
from config.thinking import (
    THINKING_SWEEP_AMP_JITTER, THINKING_SWEEP_DEG, THINKING_SWEEP_PERIOD_JITTER,
    THINKING_SWEEP_PERIOD_S, THINKING_SWEEP_RETURN_DPS, THINKING_SWEEP_START_S,
    THINKING_SWEEP_WANDER_FRAC, THINKING_SWEEP_WANDER_RATIO,
)
from config.tracking import (
    CONTROL_FPS, CONTROL_LOG_INTERVAL_S, CONTROL_STALE_TIMEOUT, PAN_KD, PAN_KP, PAN_MAX_STEP,
)
from vision.controller import PDAxis, TrackingTarget

CONTROL_INTERVAL = 1.0 / CONTROL_FPS     # servo control-loop period (decoupled from inference)

# The two statuses that mean "Kai has stopped listening and is working out a reply". Read off the
# assistant, not the session's projected voice_status: the projection exists for the dashboard and
# reports "recording" while a session listens, which is not the window we want.
_THINKING_STATUSES = (STATUS_TRANSCRIBING, STATUS_THINKING)


class SweepShape(NamedTuple):
    """One thinking window's randomly drawn sweep. Immutable and drawn once per window, so the motion
    within a window is smooth and deterministic while no two windows look alike."""
    main_amp:      float   # degrees
    main_period:   float   # seconds
    wander_amp:    float   # degrees
    wander_period: float   # seconds
    direction:     float   # +1 or -1: which way the head goes first


def draw_sweep(rng: random.Random) -> SweepShape:
    """Draw the shape for one thinking window.

    Amplitude and period are jittered and the starting direction is a coin flip, so the gesture is not
    the same arc every turn. main_amp + wander_amp == amp by construction, which is what keeps
    THINKING_SWEEP_DEG a hard bound on the sum rather than on each component separately."""
    amp    = THINKING_SWEEP_DEG * rng.uniform(*THINKING_SWEEP_AMP_JITTER)
    period = THINKING_SWEEP_PERIOD_S * rng.uniform(*THINKING_SWEEP_PERIOD_JITTER)
    return SweepShape(
        main_amp      = amp * (1.0 - THINKING_SWEEP_WANDER_FRAC),
        main_period   = period,
        wander_amp    = amp * THINKING_SWEEP_WANDER_FRAC,
        wander_period = period * THINKING_SWEEP_WANDER_RATIO,
        direction     = rng.choice((-1.0, 1.0)),
    )


def thinking_offset(elapsed: float, shape: SweepShape) -> float:
    """Pan offset in degrees for a head that is thinking, `elapsed` seconds in. 0 before the dead time.

    Two sines at incommensurate periods, so the path never repeats even on a long think. Pure — the
    randomness is all in `shape`, drawn by the caller — so the maths stays testable with no hardware
    and no seeding. Both components start at sin(0) = 0, so the sweep grows out of wherever the head
    already was instead of stepping to one side of it."""
    if elapsed < THINKING_SWEEP_START_S:
        return 0.0
    t = elapsed - THINKING_SWEEP_START_S
    main   = shape.main_amp   * math.sin(2.0 * math.pi * t / shape.main_period)
    wander = shape.wander_amp * math.sin(2.0 * math.pi * t / shape.wander_period)
    return shape.direction * (main + wander)


def ease_toward(current: float, target: float, max_step: float) -> float:
    """Move `current` toward `target` by at most `max_step`. Also pure.

    This is what stops the head jerking back to the tracked angle when a reply lands mid-swing: the
    offset walks home at THINKING_SWEEP_RETURN_DPS instead of vanishing in one tick."""
    delta = target - current
    if abs(delta) <= max_step:
        return target
    return current + (max_step if delta > 0 else -max_step)


def run(servo, target: TrackingTarget, voice, stop_evt: threading.Event) -> None:
    """Drive the servos until `stop_evt` is set. The body of the control thread.

    Runs at a fixed CONTROL_FPS and drives the pan PD controller toward the LATEST target set
    by the inference thread, so the head glides smoothly *between* inference ticks instead of
    stepping only on them. Each command stays bounded by PAN_MAX_STEP, so the per-command
    current draw is unchanged (only the cadence is fixed and inference-independent — see the
    brownout note in config/servo.py). Owns pan_pd + last_pan_cmd.

    On no-face / stale target it HOLDS: sends nothing (so the firmware idle-detach still relaxes
    the servos) and keeps the PD synced to the held position so re-acquire doesn't jump.

    While the assistant is thinking it adds a slow ±THINKING_SWEEP_DEG pan offset on top of whatever
    it would otherwise be doing (config/thinking.py). The offset RIDES ON the tracked or held angle
    rather than replacing it, so the person stays framed; last_pan_cmd and the PD stay anchored to the
    un-swept position, so re-acquire still glides from where the head really was."""
    pan_pd       = PDAxis(start=90, kp=PAN_KP, kd=PAN_KD)
    last_pan_cmd = 90.0
    next_tick    = time.monotonic()
    ticks        = 0
    last_rate_t  = next_tick
    logged_face  = None      # face_present as of the last [control] line; None = nothing logged yet
    sweep_off      = 0.0     # live thinking offset in degrees; eased, never snapped
    thinking_since = None    # monotonic time thinking began; None means "not thinking"
    sweep_settled  = True    # has the head been returned to the anchor since the last sweep?
    sweep_shape    = None    # this window's randomly drawn arc; redrawn on each entry into thinking
    sweep_rng      = random.Random()
    sweep_max_step = THINKING_SWEEP_RETURN_DPS * CONTROL_INTERVAL
    while not stop_evt.is_set():
        pan_t, tilt_t, face_present, updated_at = target.snapshot()
        now   = time.monotonic()
        stale = (now - updated_at) > CONTROL_STALE_TIMEOUT

        # Thinking is tracked LOCALLY off the assistant's own status: one dict copy under an
        # uncontended lock at CONTROL_FPS, and no new shared mutable or lock to reason about. (The
        # heavier session.get_status() is deliberately not used here.)
        if voice.get_status()["voice_status"] in _THINKING_STATUSES:
            if thinking_since is None:
                thinking_since = now
                # Draw the arc ONCE, here, on entry. Drawing per tick would resample the amplitude and
                # period every 67 ms and turn a smooth sweep into noise.
                sweep_shape = draw_sweep(sweep_rng)
        else:
            thinking_since = None
        # Gated on BOTH toggles: "Follow faces" off has to mean the head does not move at all, sweep
        # included. Don't drop the servo_tracking half in a refactor.
        want_off = 0.0
        if (thinking_since is not None
                and settings.get("thinking_sweep") and settings.get("servo_tracking")):
            want_off = thinking_offset(now - thinking_since, sweep_shape)
        sweep_off = ease_toward(sweep_off, want_off, sweep_max_step)

        # "Follow faces" off routes into the existing HOLD branch rather than a new code path: nothing
        # is sent, so the firmware's idle-detach relaxes the servos, and the PD stays synced so
        # switching it back on glides instead of snapping.
        if not face_present or stale or not settings.get("servo_tracking"):
            pan_pd.reset(last_pan_cmd)     # hold: resync PD so re-acquire glides, no jump
            # A held head still sweeps while thinking — the offset is what moves, around the held
            # position. Once it has eased back to 0 we go quiet again, so idle-detach still fires.
            anchor = int(round(last_pan_cmd))
            if round(sweep_off) != 0:
                servo.send(int(round(anchor + sweep_off)), servo.last_tilt)
                sweep_settled = False
            elif not sweep_settled:
                # One explicit command AT the anchor before going quiet. Necessary because send() is
                # gated to 10 Hz while this loop runs at 15: the easing's last steps toward 0 can be
                # dropped, and simply falling silent then leaves the head parked a few degrees off the
                # anchor — physically out of sync with last_pan_cmd, which is the desync that costs a
                # jump on the next re-acquire. Retried until send() reports it landed.
                if abs(anchor - servo.last_pan) <= PAN_DEADBAND or servo.send(anchor, servo.last_tilt):
                    sweep_settled = True
        else:
            pan_out = pan_pd.update(pan_t)
            if abs(pan_out - last_pan_cmd) > PAN_MAX_STEP:   # bound per-command travel (current safety)
                pan_out = last_pan_cmd + (PAN_MAX_STEP if pan_out > last_pan_cmd else -PAN_MAX_STEP)
                pan_pd.reset(pan_out)
            last_pan_cmd = pan_out
            # servo.send() clamps to SERVO_MIN/MAX, so the offset can never drive into a stop.
            servo.send(int(round(pan_out + sweep_off)), int(round(tilt_t)))
            # Tracking sends every tick, so it needs no anchor-return of its own — it just has to leave
            # the flag honest for whenever this drops into the hold branch.
            sweep_settled = round(sweep_off) == 0

        # Lightweight observability: effective control rate (shows decoupling working). Edge-
        # triggered on face presence, plus a slow heartbeat — see CONTROL_LOG_INTERVAL_S for why.
        # Reuses `now` from the top of the tick — a send takes well under the window's precision.
        ticks += 1
        elapsed = now - last_rate_t
        if face_present != logged_face or elapsed >= CONTROL_LOG_INTERVAL_S:
            # flush=True: stdout is block-buffered to the log file, so force this out for tuning
            rate = ticks / elapsed if elapsed > 0 else 0.0
            print(f"[control] {rate:.1f} Hz  face={face_present}", flush=True)
            ticks = 0
            last_rate_t = now
            logged_face = face_present

        next_tick += CONTROL_INTERVAL
        delay = next_tick - time.monotonic()
        if delay > 0:
            stop_evt.wait(delay)           # sleep but wake immediately on shutdown
        else:
            next_tick = time.monotonic()   # fell behind — resync, don't spiral
