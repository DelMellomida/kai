import unittest
from unittest.mock import MagicMock

from vision.face_params import (
    FaceParams, _scale, _dist, _ear, classify_emotion,
    _LEFT_EYE_PTS, _PARAM_W, _PARAM_H,
)


class TestScale(unittest.TestCase):
    def test_min_boundary(self):
        self.assertEqual(_scale(0.0, 0.0, 1.0, 0, 99), 0)

    def test_max_boundary(self):
        self.assertEqual(_scale(1.0, 0.0, 1.0, 0, 99), 99)

    def test_midpoint(self):
        self.assertEqual(_scale(0.5, 0.0, 1.0, 0, 100), 50)

    def test_clamp_below(self):
        self.assertEqual(_scale(-1.0, 0.0, 1.0, 0, 99), 0)

    def test_clamp_above(self):
        self.assertEqual(_scale(2.0, 0.0, 1.0, 0, 99), 99)

    def test_degenerate_range(self):
        self.assertEqual(_scale(5.0, 5.0, 5.0, 0, 99), 0)


class TestDist(unittest.TestCase):
    def test_horizontal(self):
        self.assertAlmostEqual(_dist((0.0, 0.0), (3.0, 0.0)), 3.0)

    def test_vertical(self):
        self.assertAlmostEqual(_dist((0.0, 0.0), (0.0, 4.0)), 4.0)

    def test_diagonal(self):
        self.assertAlmostEqual(_dist((0.0, 0.0), (3.0, 4.0)), 5.0)

    def test_same_point(self):
        self.assertAlmostEqual(_dist((5.0, 5.0), (5.0, 5.0)), 0.0)


class TestFaceParams(unittest.TestCase):
    def test_clamp_values_in_range(self):
        fp = FaceParams(x=50, y=30, distance=80, yaw=10, pitch=90,
                        mouth=99, left_eye=0, right_eye=99,
                        roll=5, smile_kiss=9, face_visible=1)
        c = fp.clamp()
        self.assertEqual(c.x, 50)
        self.assertEqual(c.roll, 5)
        self.assertEqual(c.smile_kiss, 9)
        self.assertEqual(c.face_visible, 1)

    def test_clamp_overflow(self):
        fp = FaceParams(x=200, y=-5, distance=999,
                        roll=20, smile_kiss=50, face_visible=5)
        c = fp.clamp()
        self.assertEqual(c.x, 99)
        self.assertEqual(c.y, 0)
        self.assertEqual(c.distance, 99)
        self.assertEqual(c.roll, 9)
        self.assertEqual(c.smile_kiss, 9)
        self.assertEqual(c.face_visible, 1)

    def test_no_face_zeros(self):
        fp = FaceParams.no_face()
        self.assertEqual(fp.face_visible, 0)
        self.assertEqual(fp.x, 0)
        self.assertEqual(fp.mouth, 0)

    def test_to_lofi_string_length(self):
        fp = FaceParams(x=12, y=34, distance=56, yaw=78, pitch=90,
                        mouth=11, left_eye=22, right_eye=33,
                        roll=4, smile_kiss=5, face_visible=1)
        self.assertEqual(len(fp.to_lofi_string()), 19)

    def test_to_lofi_string_content(self):
        fp = FaceParams(x=1, y=2, distance=3, yaw=4, pitch=5,
                        mouth=6, left_eye=7, right_eye=8,
                        roll=0, smile_kiss=1, face_visible=1)
        # 2+2+2+2+2+2+2+2+1+1+1 = 19 chars
        self.assertEqual(fp.to_lofi_string(), "0102030405060708011")


class TestEar(unittest.TestCase):
    def _make_lm(self, pts):
        """Create a mock landmark list from {idx: (x_px, y_px)}."""
        class Pt:
            def __init__(self, x_px, y_px):
                self.x = x_px / _PARAM_W
                self.y = y_px / _PARAM_H
        lm = [MagicMock()] * 500
        for idx, (x_px, y_px) in pts.items():
            lm[idx] = Pt(x_px, y_px)
        return lm

    def test_ear_returns_expected_ratio(self):
        # horizontal = dist(33→133) = 60px; vertical each side = 30px; EAR = 0.5
        lm = self._make_lm({
            33: (100, 200), 160: (115, 185), 158: (135, 185),
            133: (160, 200), 153: (135, 215), 144: (115, 215),
        })
        self.assertAlmostEqual(_ear(lm, _LEFT_EYE_PTS), 0.5, places=3)

    def test_closed_eye_lower_than_open(self):
        lm_open = self._make_lm({
            33: (100, 200), 160: (115, 170), 158: (135, 170),
            133: (160, 200), 153: (135, 230), 144: (115, 230),
        })
        lm_closed = self._make_lm({
            33: (100, 200), 160: (115, 198), 158: (135, 198),
            133: (160, 200), 153: (135, 202), 144: (115, 202),
        })
        self.assertGreater(
            _ear(lm_open, _LEFT_EYE_PTS),
            _ear(lm_closed, _LEFT_EYE_PTS),
        )


class TestClassifyEmotion(unittest.TestCase):
    def test_happy(self):
        fp = FaceParams(smile_kiss=6, mouth=10, left_eye=50, right_eye=50, face_visible=1)
        self.assertEqual(classify_emotion(fp), "happy")

    def test_surprised(self):
        fp = FaceParams(smile_kiss=3, mouth=70, left_eye=70, right_eye=70, face_visible=1)
        self.assertEqual(classify_emotion(fp), "surprised")

    def test_surprised_priority_over_happy(self):
        fp = FaceParams(smile_kiss=6, mouth=70, left_eye=70, right_eye=70, face_visible=1)
        self.assertEqual(classify_emotion(fp), "surprised")

    def test_sleepy(self):
        fp = FaceParams(smile_kiss=3, mouth=5, left_eye=20, right_eye=18, face_visible=1)
        self.assertEqual(classify_emotion(fp), "sleepy")

    def test_neutral(self):
        fp = FaceParams(smile_kiss=3, mouth=15, left_eye=50, right_eye=50, face_visible=1)
        self.assertEqual(classify_emotion(fp), "neutral")


if __name__ == '__main__':
    unittest.main()
