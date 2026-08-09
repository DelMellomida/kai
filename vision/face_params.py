"""
Face parameter computation — landmarks → FaceParams dataclass.
Mirrors the face-detection-movements LOFI format exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

# ── Landmark indices ──────────────────────────────────────────────────────────
NOSE_TIP        = 1
_CHIN           = 152
_LEFT_EYE_OUT   = 33
_RIGHT_EYE_OUT  = 263
_LEFT_MOUTH     = 61
_RIGHT_MOUTH    = 291
_UPPER_LIP      = 13
_LOWER_LIP      = 14
_LEFT_FACE      = 234
_RIGHT_FACE     = 454

_LEFT_EYE_PTS  = (33, 160, 158, 133, 153, 144)
_RIGHT_EYE_PTS = (362, 385, 387, 263, 373, 380)

_POSE_IDXS = (NOSE_TIP, _CHIN, _LEFT_EYE_OUT, _RIGHT_EYE_OUT, _LEFT_MOUTH, _RIGHT_MOUTH)
_MODEL_3D  = np.array([
    ( 0.0,    0.0,   0.0),
    ( 0.0,  -63.6, -12.5),
    (-43.3,  32.7, -26.0),
    ( 43.3,  32.7, -26.0),
    (-28.9, -28.9, -24.1),
    ( 28.9, -28.9, -24.1),
], dtype=np.float64)

# Pixel space used for solvePnP — must match face-detection-movements
_PARAM_W = 640
_PARAM_H = 480

# Resize input to this before running MediaPipe (4× fewer pixels). The tunable values live in
# config/camera.py; aliased to the historic PROCESS_W/PROCESS_H names face_track.py imports here.
from config.camera import PROCESS_WIDTH as PROCESS_W, PROCESS_HEIGHT as PROCESS_H


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class FaceParams:
    x:            int = 0
    y:            int = 0
    distance:     int = 0
    yaw:          int = 0
    pitch:        int = 0
    mouth:        int = 0
    left_eye:     int = 0
    right_eye:    int = 0
    roll:         int = 0
    smile_kiss:   int = 0
    face_visible: int = 0

    def clamp(self) -> FaceParams:
        def c(v, lo, hi): return max(lo, min(hi, int(v)))
        return FaceParams(
            x=c(self.x, 0, 99), y=c(self.y, 0, 99),
            distance=c(self.distance, 0, 99),
            yaw=c(self.yaw, 0, 99), pitch=c(self.pitch, 0, 99),
            mouth=c(self.mouth, 0, 99),
            left_eye=c(self.left_eye, 0, 99), right_eye=c(self.right_eye, 0, 99),
            roll=c(self.roll, 0, 9), smile_kiss=c(self.smile_kiss, 0, 9),
            face_visible=c(self.face_visible, 0, 1),
        )

    def to_lofi_string(self) -> str:
        p = self.clamp()
        return (f"{p.x:02d}{p.y:02d}{p.distance:02d}{p.yaw:02d}{p.pitch:02d}"
                f"{p.mouth:02d}{p.left_eye:02d}{p.right_eye:02d}"
                f"{p.roll}{p.smile_kiss}{p.face_visible}")

    @staticmethod
    def no_face() -> FaceParams:
        return FaceParams(face_visible=0)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _scale(value: float, in_min: float, in_max: float, out_min: int, out_max: int) -> int:
    if in_max <= in_min:
        return out_min
    t = max(0.0, min(1.0, (value - in_min) / (in_max - in_min)))
    return int(round(out_min + t * (out_max - out_min)))


def _lm_xy(lm, idx: int) -> tuple[float, float]:
    p = lm[idx]
    return p.x * _PARAM_W, p.y * _PARAM_H


def _dist(a: tuple, b: tuple) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _ear(lm, indices: tuple) -> float:
    pts = [_lm_xy(lm, i) for i in indices]
    vertical   = _dist(pts[1], pts[5]) + _dist(pts[2], pts[4])
    horizontal = _dist(pts[0], pts[3])
    return vertical / (2.0 * horizontal) if horizontal > 1e-6 else 0.0


def _head_pose(lm) -> tuple[float, float, float]:
    img_pts = np.array([_lm_xy(lm, i) for i in _POSE_IDXS], dtype=np.float64)
    fl  = float(_PARAM_W)
    cam = np.array([[fl, 0, _PARAM_W / 2], [0, fl, _PARAM_H / 2], [0, 0, 1]], dtype=np.float64)
    ok, rvec, _ = cv2.solvePnP(_MODEL_3D, img_pts, cam, np.zeros((4, 1)),
                                flags=cv2.SOLVEPNP_ITERATIVE)
    if not ok:
        return 0.0, 0.0, 0.0
    R, _ = cv2.Rodrigues(rvec)
    sy = math.sqrt(R[0, 0] ** 2 + R[1, 0] ** 2)
    if sy > 1e-6:
        pitch = math.atan2(-R[2, 0], sy)
        yaw   = math.atan2( R[1, 0], R[0, 0])
        roll  = math.atan2( R[2, 1], R[2, 2])
    else:
        pitch = math.atan2(-R[2, 0], sy)
        yaw   = math.atan2(-R[1, 2], R[1, 1])
        roll  = 0.0
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


def _smile_kiss(mouth_open: float, mouth_width: float, face_width: float) -> int:
    if face_width < 1e-6:
        return 0
    wr    = mouth_width / face_width
    smile = _scale(wr, 0.35, 0.55, 0, 9)
    if mouth_open > 0.12:
        smile = min(9, smile + 1)
    return smile


def compute_face_params(lm, compute_pose: bool = True) -> FaceParams:
    """Compute all FaceParams from a MediaPipe landmark list.

    compute_pose=False skips the per-frame solvePnP in _head_pose (yaw/pitch/roll left 0).
    Those three only feed logging + the dashboard display — classify_emotion and the
    gesture detector don't use them — so callers decimate the solve when nothing consumes it.
    """
    nose = lm[NOSE_TIP]
    x = _scale(nose.x, 0.0, 1.0, 0, 99)
    y = _scale(nose.y, 0.0, 1.0, 0, 99)

    # Face width in pixel space — larger = closer to camera
    fw       = _dist(_lm_xy(lm, _LEFT_FACE), _lm_xy(lm, _RIGHT_FACE))
    distance = _scale(fw, 40.0, 400.0, 0, 99)

    if compute_pose:
        yaw_d, pitch_d, roll_d = _head_pose(lm)
        yaw   = _scale(yaw_d,       -45.0, 45.0, 0, 99)
        pitch = _scale(pitch_d,     -35.0, 35.0, 0, 99)
        roll  = _scale(abs(roll_d),   0.0, 45.0, 0,  9)
    else:
        yaw = pitch = roll = 0

    upper = _lm_xy(lm, _UPPER_LIP)
    lower = _lm_xy(lm, _LOWER_LIP)
    lm_   = _lm_xy(lm, _LEFT_MOUTH)
    rm    = _lm_xy(lm, _RIGHT_MOUTH)
    mh    = _dist(upper, lower)
    mw    = _dist(lm_, rm)
    mo    = mh / max(mw, 1e-6)
    mouth = _scale(mo, 0.02, 0.45, 0, 99)

    left_eye  = _scale(_ear(lm, _LEFT_EYE_PTS),  0.12, 0.38, 0, 99)
    right_eye = _scale(_ear(lm, _RIGHT_EYE_PTS), 0.12, 0.38, 0, 99)

    smile_kiss = _smile_kiss(mo, mw, fw)

    return FaceParams(x=x, y=y, distance=distance, yaw=yaw, pitch=pitch,
                      mouth=mouth, left_eye=left_eye, right_eye=right_eye,
                      roll=roll, smile_kiss=smile_kiss, face_visible=1).clamp()


def classify_emotion(fp: FaceParams) -> str:
    """Classify emotional state from face parameters. Returns: happy, surprised, sleepy, or neutral."""
    p        = fp.clamp()
    eyes_avg = (p.left_eye + p.right_eye) // 2
    if p.mouth >= 55 and eyes_avg >= 60:
        return "surprised"
    if p.smile_kiss >= 5:
        return "happy"
    if eyes_avg <= 25:
        return "sleepy"
    return "neutral"
