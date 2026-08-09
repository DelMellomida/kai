"""The "thinking" pan sweep maths.

Everything under test is pure: the randomness lives in a SweepShape the caller draws, so these run
with no servo, no camera, no clock and no seeding — and now without importing MediaPipe either,
since the sweep lives in app/control_loop.py rather than in face_track.py.
"""

import math
import random
import unittest

from app import control_loop
from config.servo import SERVO_MAX, SERVO_MIN
from config.thinking import (
    THINKING_SWEEP_AMP_JITTER, THINKING_SWEEP_DEG, THINKING_SWEEP_PERIOD_JITTER,
    THINKING_SWEEP_PERIOD_S, THINKING_SWEEP_RETURN_DPS, THINKING_SWEEP_START_S,
    THINKING_SWEEP_WANDER_FRAC, THINKING_SWEEP_WANDER_RATIO,
)
from config.tracking import CONTROL_FPS, PAN_MAX_STEP, PAN_SCALE

offset = control_loop.thinking_offset
ease = control_loop.ease_toward
draw = control_loop.draw_sweep


def every_shape(n=200, seed=1234):
    """A sample across the whole random parameter space, plus the two corners the worst cases live in
    (biggest amplitude with shortest period, and the reverse)."""
    rng = random.Random(seed)
    shapes = [draw(rng) for _ in range(n)]
    amp_lo, amp_hi = THINKING_SWEEP_AMP_JITTER
    per_lo, per_hi = THINKING_SWEEP_PERIOD_JITTER
    for a, p in ((amp_hi, per_lo), (amp_lo, per_hi), (amp_hi, per_hi), (amp_lo, per_lo)):
        amp = THINKING_SWEEP_DEG * a
        period = THINKING_SWEEP_PERIOD_S * p
        shapes.append(control_loop.SweepShape(
            main_amp=amp * (1.0 - THINKING_SWEEP_WANDER_FRAC), main_period=period,
            wander_amp=amp * THINKING_SWEEP_WANDER_FRAC,
            wander_period=period * THINKING_SWEEP_WANDER_RATIO, direction=1.0))
    return shapes


def trace(shape, seconds, fps=CONTROL_FPS):
    n = int(seconds * fps) + 1
    return [offset(i / fps, shape) for i in range(n)]


class TestThinkingOffset(unittest.TestCase):
    def test_silent_before_the_dead_time(self):
        # The dead time is the whole reason an instant reply doesn't twitch the head.
        for shape in every_shape(20):
            self.assertEqual(offset(0.0, shape), 0.0)
            self.assertEqual(offset(THINKING_SWEEP_START_S - 0.001, shape), 0.0)

    def test_starts_from_zero_so_the_head_grows_into_it(self):
        # BOTH components must start at sin(0)=0, whatever was drawn: a non-zero first sample is a jump
        # away from the position the head was holding, which is exactly what the sweep must not do.
        for shape in every_shape():
            self.assertAlmostEqual(offset(THINKING_SWEEP_START_S, shape), 0.0, places=9)

    def test_never_exceeds_the_amplitude_for_any_draw(self):
        # THINKING_SWEEP_DEG is a hard bound on the SUM of the two sines, not on each one. This is what
        # the servo-limit and per-command-travel arguments in config/ rest on.
        for shape in every_shape():
            for value in trace(shape, 12.0):
                self.assertLessEqual(abs(value), THINKING_SWEEP_DEG + 1e-9)

    def test_goes_both_ways_within_a_window(self):
        # The point of the retune: at the old 3 s period a ~1.3 s thinking window only ever produced a
        # lean to one side. It now has to cross the middle and come out the other side.
        for shape in every_shape(60):
            values = trace(shape, THINKING_SWEEP_START_S + shape.main_period)
            self.assertGreater(max(values), 0.0)
            self.assertLess(min(values), 0.0)

    def test_gets_through_its_excursion_inside_a_measured_reply_window(self):
        # sess_last_llm_ms measured ~1050 ms on this robot, so a whole thinking window is only ~1.3 s.
        # Whatever was drawn, the head has to complete most of THAT DRAW's intended excursion inside
        # one -- the failure being guarded is the original bug, a sweep so slow and starting so late
        # that the turn was over before the head had gone anywhere. Measured against the drawn
        # amplitude, not the global maximum: a small draw travelling its whole range is correct
        # behaviour, and how big the smallest draw is allowed to be is a config question (below).
        for shape in every_shape(60):
            travel = max(trace(shape, 1.3)) - min(trace(shape, 1.3))
            self.assertGreater(travel, 0.9 * (shape.main_amp + shape.wander_amp),
                               "the window ends before the sweep has been anywhere")

    def test_even_the_smallest_draw_is_big_enough_to_see(self):
        # The other half of the above, and the one a future retune is most likely to break: shrinking
        # the amplitude or widening the jitter downward could leave the sweep technically working and
        # practically invisible, which is precisely how this was first reported. Pan movement below
        # about 8 degrees is hard to notice at conversational distance on this head.
        self.assertGreaterEqual(THINKING_SWEEP_DEG * THINKING_SWEEP_AMP_JITTER[0], 8.0)

    def test_direction_is_not_always_the_same(self):
        # A gesture that always goes the same way first reads as mechanical.
        firsts = {shape.direction for shape in every_shape(60)}
        self.assertEqual(firsts, {-1.0, 1.0})

    def test_no_two_draws_are_identical(self):
        shapes = every_shape(60)
        self.assertGreater(len({(s.main_amp, s.main_period) for s in shapes}), 50)

    def test_the_path_does_not_repeat_on_a_long_think(self):
        # Incommensurate periods: one main period later the offset must NOT be back where it was, which
        # is what a single sine would do.
        for shape in every_shape(40):
            a = offset(THINKING_SWEEP_START_S + 0.25 * shape.main_period, shape)
            b = offset(THINKING_SWEEP_START_S + 1.25 * shape.main_period, shape)
            self.assertGreater(abs(a - b), 1e-3, "the sweep is repeating like a plain sine")

    def test_wander_period_is_not_a_whole_ratio(self):
        # A whole-number ratio would make the sum periodic again, defeating the point.
        self.assertNotAlmostEqual(THINKING_SWEEP_WANDER_RATIO,
                                  round(THINKING_SWEEP_WANDER_RATIO), places=6)

    def test_stays_inside_the_servo_limits_at_the_edge_of_the_tracked_range(self):
        # PAN_SCALE maps a full nose sweep onto 90 +/- PAN_SCALE/2, the worst case the sweep rides on.
        # servo.send() clamps anyway, but a clamped sweep would flat-top and look wrong.
        self.assertLessEqual(90.0 + PAN_SCALE / 2.0 + THINKING_SWEEP_DEG, SERVO_MAX)
        self.assertGreaterEqual(90.0 - PAN_SCALE / 2.0 - THINKING_SWEEP_DEG, SERVO_MIN)


class TestEaseToward(unittest.TestCase):
    def test_snaps_when_already_within_a_step(self):
        self.assertEqual(ease(10.0, 10.5, 1.0), 10.5)
        self.assertEqual(ease(10.0, 10.0, 1.0), 10.0)

    def test_never_steps_more_than_the_cap(self):
        for start, target in ((0.0, 90.0), (0.0, -90.0), (5.0, -5.0)):
            self.assertLessEqual(abs(ease(start, target, 2.0) - start), 2.0 + 1e-9)

    def test_reaches_zero_within_the_advertised_time(self):
        # From a full-amplitude offset, "thinking over" must land back on 0 in DEG/RETURN_DPS seconds.
        # This is the bound that makes the return feel deliberate rather than a jerk or a drift.
        step = THINKING_SWEEP_RETURN_DPS / CONTROL_FPS
        ticks = math.ceil(THINKING_SWEEP_DEG / step)
        current = THINKING_SWEEP_DEG
        for _ in range(ticks):
            current = ease(current, 0.0, step)
        self.assertEqual(current, 0.0)

    def test_easing_clears_the_worst_case_slope_for_every_draw(self):
        # The config invariant in config/thinking.py: the per-tick cap must clear the sweep's own peak
        # slope, or the easing turns the sweep into a triangle and quietly caps the amplitude. Measured
        # numerically over the whole parameter space rather than trusting the closed form.
        worst = 0.0
        for shape in every_shape():
            values = trace(shape, 12.0, fps=200)
            worst = max(worst, max(abs(b - a) * 200 for a, b in zip(values, values[1:])))
        self.assertLess(worst, THINKING_SWEEP_RETURN_DPS,
                        "RETURN_DPS is below the sweep's own peak slope")

    def test_the_sweep_survives_the_easing_for_every_draw(self):
        # End to end at the real tick rate: the eased output must reproduce the raw amplitude, not a
        # clipped version of it.
        step = THINKING_SWEEP_RETURN_DPS / CONTROL_FPS
        for shape in every_shape(60):
            raw = trace(shape, THINKING_SWEEP_START_S + 2.0 * shape.main_period)
            eased, current = [], 0.0
            for target in raw:
                current = ease(current, target, step)
                eased.append(current)
            self.assertGreater(max(map(abs, eased)), max(map(abs, raw)) * 0.95)

    def test_per_command_travel_stays_under_the_slew_cap(self):
        # The sweep rides on top of the PD output, and PAN_MAX_STEP bounds SG90 current per command.
        # At the 10 Hz send rate the sweep's own contribution must stay well inside that.
        worst = 0.0
        for shape in every_shape():
            values = trace(shape, 12.0, fps=10)
            worst = max(worst, max(abs(b - a) for a, b in zip(values, values[1:])))
        self.assertLess(worst, PAN_MAX_STEP)


if __name__ == "__main__":
    unittest.main()
