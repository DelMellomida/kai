"""
Gesture detection layer for face_track.py.

Call GestureDetector.update(fp, t) once per frame.
Returns a gesture name string or None.
"""

from __future__ import annotations

from collections import deque

from vision.face_params import FaceParams

# Tunable thresholds live in config/gesture.py; re-imported so the names stay module-level
# (windows are in seconds and converted to frame counts by _window_frames below).
from config.gesture import (
    DEFAULT_INFERENCE_FPS,
    NOD_WINDOW_S, NOD_MIN_AMP, NOD_MIN_REVERSALS, NOD_COOLDOWN,
    SHAKE_WINDOW_S, SHAKE_MIN_AMP, SHAKE_MIN_REVERSALS, SHAKE_COOLDOWN,
    PROX_WINDOW_S, PROX_DELTA, PROX_COOLDOWN,
    MOUTH_OPEN_THRESHOLD, MOUTH_CLOSE_THRESHOLD, MOUTH_COOLDOWN,
)


def _window_frames(seconds: float, inference_fps: float) -> int:
    """Convert a window duration to a deque length, floored at 2 (need ≥2 samples for a diff)."""
    return max(2, round(seconds * inference_fps))


# ── Sub-detectors ─────────────────────────────────────────────────────────────

class OscillationDetector:
    """Detects deliberate back-and-forth movement along one axis."""

    def __init__(self, window: int, min_amp: float, min_reversals: int, cooldown: float):
        self._buf      = deque(maxlen=window)
        self._min_amp  = min_amp
        self._min_rev  = min_reversals
        self._cooldown = cooldown
        self._last_t   = -cooldown

    def update(self, value: float, t: float) -> bool:
        self._buf.append(value)
        if len(self._buf) < self._buf.maxlen:
            return False
        if t - self._last_t < self._cooldown:
            return False
        amplitude = max(self._buf) - min(self._buf)
        if amplitude < self._min_amp:
            return False
        vals   = list(self._buf)
        diffs  = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
        reversals = sum(
            1 for i in range(len(diffs) - 1)
            if diffs[i] * diffs[i + 1] < 0
        )
        if reversals >= self._min_rev:
            self._last_t = t
            return True
        return False


class ProximityDetector:
    """Detects sustained approach or retreat using EMA-smoothed distance."""

    def __init__(self, window: int, delta: float, cooldown: float, alpha: float = 0.15):
        self._alpha   = alpha
        self._ema     = None
        self._history = deque(maxlen=window)
        self._delta   = delta
        self._cooldown = cooldown
        self._last_t  = -cooldown

    def update(self, distance: float, t: float) -> str | None:
        if self._ema is None:
            self._ema = float(distance)
        else:
            self._ema = self._alpha * distance + (1 - self._alpha) * self._ema
        self._history.append(self._ema)
        if len(self._history) < self._history.maxlen:
            return None
        if t - self._last_t < self._cooldown:
            return None
        change = self._ema - self._history[0]
        if change > self._delta:
            self._last_t = t
            return "approach"
        if change < -self._delta:
            self._last_t = t
            return "retreat"
        return None


class MouthDetector:
    """Fires mouth_open / mouth_close on threshold crossings with hysteresis."""

    def __init__(self, open_thr: float, close_thr: float, cooldown: float):
        self._open_thr  = open_thr
        self._close_thr = close_thr
        self._cooldown  = cooldown
        self._open      = False
        self._last_t    = -cooldown

    def update(self, mouth: float, t: float) -> str | None:
        if t - self._last_t < self._cooldown:
            return None
        if not self._open and mouth >= self._open_thr:
            self._open   = True
            self._last_t = t
            return "mouth_open"
        if self._open and mouth <= self._close_thr:
            self._open   = False
            self._last_t = t
            return "mouth_close"
        return None


# ── Public API ────────────────────────────────────────────────────────────────

class GestureDetector:
    """
    Wraps all sub-detectors. Call update() once per frame.
    Returns the highest-priority gesture name fired this frame, or None.
    Priority: mouth_open > mouth_close > nod > shake > approach > retreat
    """

    def __init__(self, inference_fps: float = DEFAULT_INFERENCE_FPS) -> None:
        nod_w   = _window_frames(NOD_WINDOW_S,   inference_fps)
        shake_w = _window_frames(SHAKE_WINDOW_S, inference_fps)
        prox_w  = _window_frames(PROX_WINDOW_S,  inference_fps)
        self._nod   = OscillationDetector(nod_w,   NOD_MIN_AMP,   NOD_MIN_REVERSALS,   NOD_COOLDOWN)
        self._shake = OscillationDetector(shake_w, SHAKE_MIN_AMP, SHAKE_MIN_REVERSALS, SHAKE_COOLDOWN)
        self._prox  = ProximityDetector(prox_w, PROX_DELTA, PROX_COOLDOWN)
        self._mouth = MouthDetector(MOUTH_OPEN_THRESHOLD, MOUTH_CLOSE_THRESHOLD, MOUTH_COOLDOWN)

    def update(self, fp: FaceParams, t: float) -> str | None:
        mouth = self._mouth.update(fp.mouth, t)
        nod   = self._nod.update(fp.y, t)
        shake = self._shake.update(fp.x, t)
        prox  = self._prox.update(fp.distance, t)

        if mouth:  return mouth
        if nod:    return "nod"
        if shake:  return "shake"
        if prox:   return prox
        return None
