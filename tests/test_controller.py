import threading
import time
import unittest

from config.servo import PAN_DEADBAND, SEND_DEADBAND
from config.tracking import (
    CONTROL_STALE_TIMEOUT, EMA_RESET_FRAMES, INFERENCE_FPS, PAN_MAX_STEP, SERVO_ABSENCE_FRAMES,
)
from vision.controller import EMAFilter, PDAxis, Kp, Kd, TrackingTarget


class TestEMAFilter(unittest.TestCase):
    def test_cold_start_returns_first_value(self):
        ema = EMAFilter(alpha=0.3)
        self.assertEqual(ema.update(42.0), 42.0)

    def test_converges_toward_target(self):
        ema = EMAFilter(alpha=0.3)
        ema.update(0.0)
        val = 0.0
        for _ in range(20):
            val = ema.update(100.0)
        self.assertGreater(val, 90.0)

    def test_reset_clears_state(self):
        ema = EMAFilter(alpha=0.3)
        ema.update(100.0)
        ema.reset()
        self.assertEqual(ema.update(0.0), 0.0)

    def test_alpha_zero_holds_previous(self):
        ema = EMAFilter(alpha=0.0)
        ema.update(10.0)
        self.assertAlmostEqual(ema.update(100.0), 10.0)

    def test_alpha_one_tracks_immediately(self):
        ema = EMAFilter(alpha=1.0)
        ema.update(10.0)
        self.assertAlmostEqual(ema.update(50.0), 50.0)


class TestPDAxis(unittest.TestCase):
    def test_moves_toward_target(self):
        pd = PDAxis(start=90.0)
        result = pd.update(100.0)
        self.assertGreater(result, 90)
        self.assertLess(result, 100)

    def test_returns_int(self):
        pd = PDAxis(start=90.0)
        self.assertIsInstance(pd.update(100.0), int)

    def test_clamps_to_zero(self):
        pd = PDAxis(start=5.0)
        for _ in range(50):
            pd.update(0.0)
        self.assertGreaterEqual(pd.current, 0.0)

    def test_clamps_to_180(self):
        pd = PDAxis(start=175.0)
        for _ in range(50):
            pd.update(180.0)
        self.assertLessEqual(pd.current, 180.0)

    def test_reset_changes_current(self):
        pd = PDAxis(start=90.0)
        pd.update(180.0)
        pd.reset(45.0)
        self.assertAlmostEqual(pd.current, 45.0)

    def test_reset_clears_derivative(self):
        pd = PDAxis(start=90.0)
        pd.update(180.0)
        pd.reset(90.0)
        result = pd.update(90.0)
        self.assertAlmostEqual(result, 90, delta=1)

    def test_first_step_after_reset_has_no_derivative_kick(self):
        # THE twitch regression. The control loop resets the PD on every held tick, and MediaPipe drops
        # single frames, so this path runs constantly. Before priming, the first tick differenced
        # against prev_err=0 and asked for (kp+kd)*err — more than double the intended step, which the
        # loop then clipped to a full PAN_MAX_STEP jump.
        pd = PDAxis(start=90.0, kp=0.20, kd=0.25)
        pd.update(180.0)          # wind up a prev_err
        pd.reset(90.0)
        self.assertEqual(pd.update(110.0), 94)              # 90 + kp*20, no kd term
        self.assertLessEqual(pd.current - 90.0, 0.20 * 20.0 + 1e-9,
                             "the first step must not exceed kp * err")

    def test_derivative_resumes_on_the_second_step(self):
        # Priming skips kd for ONE tick, not forever: the damping that stops overshoot has to come back.
        pd = PDAxis(start=90.0, kp=0.20, kd=0.25)
        pd.reset(90.0)
        pd.update(110.0)                                    # err 20 -> current 94
        before = pd.current
        pd.update(110.0)                                    # err 16, prev_err 20 -> kd damps
        self.assertLess(pd.current - before, 0.20 * 16.0,
                        "kd should be subtracting again as the error shrinks")

    def test_a_fresh_axis_is_also_primed(self):
        # Same hazard at startup as after a reset — __init__ must not leave prev_err=0 live either.
        pd = PDAxis(start=90.0, kp=0.20, kd=0.25)
        self.assertEqual(pd.update(110.0), 94)

    def test_repeated_resets_while_holding_do_not_accumulate(self):
        # The hold branch calls reset() every tick for as long as the face is gone.
        pd = PDAxis(start=90.0, kp=0.20, kd=0.25)
        for _ in range(50):
            pd.reset(120.0)
        self.assertEqual(pd.update(140.0), 124)             # 120 + kp*20

    def test_command_rounds_up_when_nearer_the_higher_degree(self):
        # R9. int() truncates toward zero and every servo angle is positive, so truncation was a
        # uniform downward bias of up to a degree. It did not stop at the wire either:
        # app/control_loop.py stores this return value as last_pan_cmd and uses it as the slew
        # reference, the hold anchor, and the value reset() re-syncs the PD to.
        pd = PDAxis(start=90.0, kp=0.28, kd=0.0)
        self.assertEqual(pd.update(100.0), 93)              # 92.8 -> 93; truncation gave 92
        self.assertAlmostEqual(pd.current, 92.8, places=6,
                               msg="internal state must stay float — only the command is rounded")

    def test_command_rounds_down_when_nearer_the_lower_degree(self):
        pd = PDAxis(start=90.0, kp=0.22, kd=0.0)
        self.assertEqual(pd.update(100.0), 92)              # 92.2 -> 92

    def test_command_is_always_the_nearest_degree_to_the_internal_state(self):
        # The invariant, swept rather than spot-checked: whatever the gains, the commanded angle is
        # never more than half a degree from where the controller actually thinks it is.
        for kp in (0.05, 0.13, 0.20, 0.37, 0.64):
            pd = PDAxis(start=90.0, kp=kp, kd=0.0)
            for target in (0.0, 45.0, 91.0, 137.0, 180.0):
                with self.subTest(kp=kp, target=target):
                    cmd = pd.update(target)
                    self.assertLessEqual(abs(cmd - pd.current), 0.5)

    def test_custom_kp_higher_gives_bigger_step(self):
        pd_high = PDAxis(start=90.0, kp=0.5, kd=0.0)
        pd_low  = PDAxis(start=90.0, kp=0.1, kd=0.0)
        self.assertGreater(pd_high.update(100.0), pd_low.update(100.0))

    def test_defaults_use_module_kp_kd(self):
        pd = PDAxis(start=90.0)
        self.assertEqual(pd._kp, Kp)
        self.assertEqual(pd._kd, Kd)


class TestControlCadenceInvariants(unittest.TestCase):
    """Relationships between config constants that the control loop's correctness rests on. Cheap to
    assert, and each one is something a plausible retune would silently break."""

    def test_absence_grace_is_shorter_than_the_stale_timeout(self):
        # The servo target's no-face grace must expire well before the control loop's stale path takes
        # over, or a genuine absence would be reported as "inference died" instead.
        self.assertLess(SERVO_ABSENCE_FRAMES, CONTROL_STALE_TIMEOUT * INFERENCE_FPS)

    def test_absence_grace_is_shorter_than_the_ema_reset(self):
        # The grace hides flicker; EMA_RESET_FRAMES handles real absence. Ordering them the other way
        # round would reset the smoother before the servo had even been told the face was gone.
        self.assertLess(SERVO_ABSENCE_FRAMES, EMA_RESET_FRAMES)

    def test_pan_deadband_is_below_the_per_tick_pd_step(self):
        # The stair-step regression: at slow tracking speeds the PD asks for roughly kp * err per tick,
        # and if the deadband swallows that the head stops moving until the error accumulates past it.
        self.assertLess(PAN_DEADBAND, PAN_MAX_STEP)
        self.assertLessEqual(PAN_DEADBAND, SEND_DEADBAND,
                             "pan must not be coarser than the jaw channel")


class TestTrackingTarget(unittest.TestCase):
    def test_defaults_to_center_no_face(self):
        t = TrackingTarget()
        pan, tilt, face, _ts = t.snapshot()
        self.assertEqual((pan, tilt), (90.0, 90.0))
        self.assertFalse(face)

    def test_set_updates_snapshot(self):
        t = TrackingTarget()
        t.set(120.0, 80.0, True)
        pan, tilt, face, _ts = t.snapshot()
        self.assertEqual((pan, tilt, face), (120.0, 80.0, True))

    def test_updated_at_advances_on_set(self):
        t = TrackingTarget()
        _, _, _, ts0 = t.snapshot()
        time.sleep(0.01)
        t.set(100.0, 90.0, True)
        _, _, _, ts1 = t.snapshot()
        self.assertGreater(ts1, ts0)

    def test_concurrent_set_snapshot_never_tears(self):
        """A snapshot must always return a self-consistent, well-typed tuple even while
        another thread hammers set() — verifies the lock (no torn reads / exceptions)."""
        t = TrackingTarget()
        stop = threading.Event()
        errors = []

        def writer():
            i = 0
            while not stop.is_set():
                t.set(float(i % 180), float((i * 2) % 180), bool(i % 2))
                i += 1

        w = threading.Thread(target=writer)
        w.start()
        try:
            for _ in range(20000):
                pan, tilt, face, ts = t.snapshot()
                if not (0.0 <= pan <= 179.0 and 0.0 <= tilt <= 179.0 and isinstance(face, bool)):
                    errors.append((pan, tilt, face))
                    break
        finally:
            stop.set()
            w.join(timeout=1.0)
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
