import unittest
from types import SimpleNamespace

from vision.gesture import (
    OscillationDetector, ProximityDetector, MouthDetector, GestureDetector,
)


def fp(**kwargs) -> SimpleNamespace:
    defaults = {"x": 50, "y": 50, "distance": 50, "mouth": 0,
                "yaw": 50, "pitch": 50, "roll": 0, "smile_kiss": 0,
                "left_eye": 50, "right_eye": 50, "face_visible": 1}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestOscillationDetector(unittest.TestCase):
    def _make(self, window=6, min_amp=5, min_rev=2, cooldown=1.0):
        return OscillationDetector(window, min_amp, min_rev, cooldown)

    def test_no_fire_without_enough_reversals(self):
        od = self._make(window=5, min_amp=5, min_rev=4)
        fired = any(od.update(v, 100.0) for v in [10, 20, 30, 40, 50])
        self.assertFalse(fired)

    def test_no_fire_when_amplitude_too_small(self):
        od = self._make(window=6, min_amp=30, min_rev=2)
        fired = any(od.update(v, 100.0) for v in [50, 51, 50, 51, 50, 51])
        self.assertFalse(fired)

    def test_fires_on_sufficient_oscillation(self):
        od = self._make(window=6, min_amp=5, min_rev=2)
        fired = any(od.update(v, 100.0) for v in [10, 50, 10, 50, 10, 50])
        self.assertTrue(fired)

    def test_respects_cooldown(self):
        od = self._make(window=6, min_amp=5, min_rev=2, cooldown=5.0)
        for v in [10, 50, 10, 50, 10, 50]:
            od.update(v, 100.0)
        fired_again = any(od.update(v, 100.0) for v in [10, 50, 10, 50, 10, 50])
        self.assertFalse(fired_again)

    def test_fires_again_after_cooldown(self):
        od = self._make(window=6, min_amp=5, min_rev=2, cooldown=1.0)
        for v in [10, 50, 10, 50, 10, 50]:
            od.update(v, 100.0)
        fired = any(od.update(v, 102.0) for v in [10, 50, 10, 50, 10, 50])
        self.assertTrue(fired)


class TestProximityDetector(unittest.TestCase):
    def test_approach_fires(self):
        pd = ProximityDetector(window=5, delta=10, cooldown=1.0, alpha=1.0)
        result = None
        for d in [20, 20, 20, 20, 45]:
            result = pd.update(d, 100.0)
        self.assertEqual(result, "approach")

    def test_retreat_fires(self):
        pd = ProximityDetector(window=5, delta=10, cooldown=1.0, alpha=1.0)
        result = None
        for d in [60, 60, 60, 60, 30]:
            result = pd.update(d, 100.0)
        self.assertEqual(result, "retreat")

    def test_blocked_within_cooldown(self):
        pd = ProximityDetector(window=5, delta=10, cooldown=5.0, alpha=1.0)
        for d in [20, 20, 20, 20, 45]:
            pd.update(d, 100.0)
        result = None
        for d in [20, 20, 20, 20, 45]:
            result = pd.update(d, 100.0)
        self.assertIsNone(result)


class TestMouthDetector(unittest.TestCase):
    def test_fires_mouth_open(self):
        md = MouthDetector(open_thr=40, close_thr=20, cooldown=0.0)
        self.assertEqual(md.update(50, 0.0), "mouth_open")

    def test_fires_mouth_close(self):
        md = MouthDetector(open_thr=40, close_thr=20, cooldown=0.0)
        md.update(50, 0.0)
        self.assertEqual(md.update(10, 1.0), "mouth_close")

    def test_no_flicker_hysteresis(self):
        md = MouthDetector(open_thr=40, close_thr=20, cooldown=0.0)
        md.update(50, 0.0)          # opens
        # value in hysteresis band — no close event
        self.assertIsNone(md.update(30, 1.0))

    def test_cooldown_prevents_immediate_reopen(self):
        md = MouthDetector(open_thr=40, close_thr=20, cooldown=5.0)
        md.update(50, 0.0)          # opens at t=0
        md._open = False            # manually reset so logic would allow re-open
        self.assertIsNone(md.update(50, 0.1))  # within cooldown

    def test_no_event_when_neutral(self):
        md = MouthDetector(open_thr=40, close_thr=20, cooldown=0.0)
        self.assertIsNone(md.update(30, 0.0))  # in hysteresis band, not open


class TestGestureDetector(unittest.TestCase):
    def test_mouth_open_fires_first_frame(self):
        gd = GestureDetector()
        result = gd.update(fp(mouth=50), 100.0)
        self.assertEqual(result, "mouth_open")

    def test_no_gesture_when_neutral(self):
        gd = GestureDetector()
        result = gd.update(fp(mouth=10, y=50, x=50, distance=50), 100.0)
        self.assertIsNone(result)

    def test_mouth_close_fires_after_open(self):
        gd = GestureDetector()
        gd.update(fp(mouth=50), 0.0)
        result = gd.update(fp(mouth=5), 2.0)
        self.assertEqual(result, "mouth_close")

    def test_default_windows_match_30fps(self):
        # Default construction must reproduce the original fixed frame-count windows.
        gd = GestureDetector()
        self.assertEqual(gd._nod._buf.maxlen, 20)
        self.assertEqual(gd._shake._buf.maxlen, 20)
        self.assertEqual(gd._prox._history.maxlen, 15)

    def test_windows_scale_with_inference_fps(self):
        # Halving the inference rate halves the frame-count windows, preserving wall-clock.
        gd = GestureDetector(inference_fps=15)
        self.assertEqual(gd._nod._buf.maxlen, 10)
        self.assertEqual(gd._shake._buf.maxlen, 10)
        self.assertEqual(gd._prox._history.maxlen, 8)

    def test_mouth_open_priority_over_nod(self):
        # Fill the nod buffer to make it ready to fire
        gd = GestureDetector()
        t = 100.0
        # Fill buffer alternating so nod detector is primed
        for v in [30, 70, 30, 70, 30, 70, 30, 70, 30, 70,
                  30, 70, 30, 70, 30, 70, 30, 70, 30, 70]:
            gd.update(fp(y=v, mouth=10), t)
        # Now both mouth open AND nod might fire — mouth_open wins
        result = gd.update(fp(y=30, mouth=50), t + 2.0)
        self.assertEqual(result, "mouth_open")


if __name__ == '__main__':
    unittest.main()
