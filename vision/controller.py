"""
PD controller and EMA filter for servo axis tracking.
"""

from __future__ import annotations

import threading
import time

# Default PD gains — can be overridden per-axis in PDAxis constructor
Kp = 0.20
Kd = 0.25


class EMAFilter:
    """Exponential moving average — stateful single-value smoother."""

    def __init__(self, alpha: float) -> None:
        self._alpha = alpha
        self._value: float | None = None

    def update(self, raw: float) -> float:
        if self._value is None:
            self._value = raw
        else:
            self._value = self._alpha * raw + (1.0 - self._alpha) * self._value
        return self._value

    def reset(self) -> None:
        self._value = None


class PDAxis:
    """Single-axis PD controller. Smoothly drives current angle toward target.

    The derivative term is PRIMED after every reset: the first update() following one contributes no
    kd at all, because there is no previous error to difference against. This is not cosmetic. The
    control loop calls reset() on every held tick, and MediaPipe drops single frames at the
    MIN_FACE_AREA boundary, so without priming the first tick after any flicker computed
    `kd * (err - 0)` on top of `kp * err` — 0.45*err instead of 0.20*err at the default gains, which
    at a 20 degree error asks for 9 degrees and lands as a full PAN_MAX_STEP jump. That was a visible
    head twitch on every dropped detection. A missing derivative for one tick costs nothing by
    comparison: kd only damps overshoot, and one tick of pure P cannot overshoot.
    """

    def __init__(self, start: float = 90.0, kp: float = Kp, kd: float = Kd) -> None:
        self.current    = start
        self._kp        = kp
        self._kd        = kd
        self._prev_err  = 0.0
        self._primed    = False

    def update(self, target: float) -> int:
        err = target - self.current
        if not self._primed:
            self._prev_err = err     # first tick after a reset: differencing against 0 is a kick
            self._primed   = True
        correction   = self._kp * err + self._kd * (err - self._prev_err)
        self.current = max(0.0, min(180.0, self.current + correction))
        self._prev_err = err
        # ROUND, not int(). int() truncates toward zero and every servo angle is positive, so it was
        # a uniform downward bias of up to a degree — and not only on the wire: app/control_loop.py
        # stores this value as last_pan_cmd and uses it as the slew reference, the hold anchor, and
        # the value pan_pd.reset() re-syncs to. PAN_DEADBAND is 1 (see config/servo.py, which
        # lowered it precisely so ~1 degree tracking requests are not swallowed), so a sub-degree
        # bias sits exactly at the resolution where it stops being invisible.
        # round() is half-to-even at an exact .5; that is immaterial here and matches the
        # int(round(...)) already used at the send site in app/control_loop.py.
        return int(round(self.current))

    def reset(self, value: float = 90.0) -> None:
        self.current   = value
        self._prev_err = 0.0
        self._primed   = False


class TrackingTarget:
    """Thread-safe holder for the latest desired pan/tilt, shared between the inference
    thread (writer) and the servo control thread (reader). Decouples 'how often we see the
    face' from 'how often we command the servo': inference calls set() whenever it has a new
    target; the control loop calls snapshot() at a fixed rate and drives the PD toward it.
    The lock is held only for a scalar copy, so the control loop never meaningfully contends."""

    def __init__(self, pan: float = 90.0, tilt: float = 90.0) -> None:
        self._lock         = threading.Lock()
        self._pan          = pan
        self._tilt         = tilt
        self._face_present = False
        self._updated_at   = time.monotonic()

    def set(self, pan: float, tilt: float, face_present: bool) -> None:
        with self._lock:
            self._pan          = pan
            self._tilt         = tilt
            self._face_present = face_present
            self._updated_at   = time.monotonic()

    def snapshot(self) -> tuple[float, float, bool, float]:
        """Return (pan, tilt, face_present, updated_at) — a consistent copy under the lock."""
        with self._lock:
            return self._pan, self._tilt, self._face_present, self._updated_at
