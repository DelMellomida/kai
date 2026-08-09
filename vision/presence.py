"""Is anybody in front of Kai? A one-slot, thread-safe handoff from the inference loop to whoever
needs to know — currently ai/session.py, which uses it to decide when a conversation is over.

Same shape as vision/controller.py's TrackingTarget: face_track.py's inference thread writes, another
thread reads, the lock is held only for a scalar copy. Kept as a standalone module (rather than a
field on TrackingTarget) so ai/ can consume it without importing the tracking/servo machinery, and
so it can be injected as a plain callable in tests.

Presence is deliberately THREE-valued. "No face" and "no idea" are different facts: face_track.py
stops calling mark() entirely when the camera returns no frame (or with --no-camera), and a consumer
that read that silence as absence would end every conversation whenever the camera hiccuped. Hence
is_fresh — after FACE_FEED_STALE_S with no update, presence is unknown and callers should fail open.
"""

from __future__ import annotations

import threading
import time

from config.wake import FACE_FEED_STALE_S

_lock = threading.Lock()
_visible = False
_last_seen = 0.0      # last mark(True); 0.0 = no face seen since start
_last_update = 0.0    # last mark() of either kind = proof the producer is alive


def mark(visible: bool, now: float | None = None) -> None:
    """Record one inference result. Called from face_track.py's inference branch at INFERENCE_FPS —
    cheap enough to call unconditionally every tick."""
    now = time.monotonic() if now is None else now
    with _lock:
        global _visible, _last_seen, _last_update
        _visible = visible
        _last_update = now
        if visible:
            _last_seen = now


def snapshot(now: float | None = None) -> tuple[bool, float, bool]:
    """Return (visible, seconds_since_last_seen, is_fresh).

    `is_fresh` False means the producer has gone quiet (camera stall, --no-camera, not started yet)
    so presence is UNKNOWN — treat `visible` as meaningless rather than as "absent".
    `seconds_since_last_seen` is inf when no face has been seen at all."""
    now = time.monotonic() if now is None else now
    with _lock:
        visible, last_seen, last_update = _visible, _last_seen, _last_update
    is_fresh = last_update > 0.0 and (now - last_update) <= FACE_FEED_STALE_S
    since = float("inf") if last_seen <= 0.0 else max(0.0, now - last_seen)
    return visible, since, is_fresh


def reset() -> None:
    """Forget everything — for tests, and for a clean slate across a camera hot-swap."""
    with _lock:
        global _visible, _last_seen, _last_update
        _visible = False
        _last_seen = 0.0
        _last_update = 0.0
