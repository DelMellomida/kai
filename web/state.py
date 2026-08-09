"""What the dashboard is currently being told, behind one lock.

Producers are the tracking loop (frames + face data) and the status publisher (camera, servo,
settings); consumers are the /video generator and the /params SSE stream, both on Flask request
threads. This class is the seam between them, and it deliberately knows nothing about cameras,
servos or MediaPipe — callers hand it finished dicts. That is what lets the whole publish/merge
contract be exercised without any of that machinery.

The published state is split in two by whether it needs a FRAME:

  params — face data + fps. Written only when a frame arrives, so it is empty with no camera.
  status — camera/servo/settings state. Written every loop iteration, frame or not, because "there
           is no camera, and here is why" is exactly what the dashboard must show when no frames
           exist. Publishing this only alongside a frame is what used to make the UI claim LIVE on
           a robot with no camera at all.

No key is written by both, so the merge order in merged() cannot matter.
"""

from __future__ import annotations

import threading

import numpy as np

# After this long with no frame, stop reporting the last frame's face data. The frontend's `?? 0`
# fallbacks then read as "no face, 0 fps" rather than freezing on whatever it last saw.
FRAME_STALE_S = 1.0


class DashboardState:
    """One process-wide holder for everything /params and /video serve."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # The latest *un-encoded* frame. The /video generator encodes it on demand — only while a
        # client is connected — so the headless autostart case does no JPEG work at all. The id
        # lets that generator skip re-encoding a frame it has already sent.
        self._raw_frame: np.ndarray | None = None
        self._frame_id = 0
        self._frame_t = 0.0     # monotonic time of the last published frame; 0.0 = none yet
        self._params: dict = {}
        # Seeded, not empty: the dashboard is served as soon as Flask starts, which is before the
        # tracking loop's first publish_status. An empty status there would let the frontend fall
        # back to "live" and claim a camera we do not have.
        self._status: dict = {"cam_source": "none", "cam_reason": "starting up",
                              "cam_mode": "auto", "cam_mode_locked": False, "cam_retry_in_s": 0.0}
        self._video_clients = 0

    # ── producers (tracking loop) ───────────────────────────────────────────

    def publish_frame(self, frame, params: dict, now: float) -> None:
        """Store the newest frame and the face data that came with it."""
        with self._lock:
            self._raw_frame = frame
            self._frame_id += 1
            self._params = params
            self._frame_t = now

    def publish_status(self, status: dict, now: float) -> None:
        """Store the frame-independent half, and expire face data once frames stop.

        The expiry lives here rather than at the call site because it is a property of the two
        halves' relationship — status arriving without a frame for FRAME_STALE_S is precisely the
        evidence that the params half is stale.
        """
        with self._lock:
            self._status = status
            if self._params and (not self._frame_t or now - self._frame_t > FRAME_STALE_S):
                self._params = {}

    # ── consumers (Flask threads) ───────────────────────────────────────────

    def merged(self) -> dict:
        """Status overlaid with face data, as one dict. The base of every /params snapshot."""
        with self._lock:
            data = dict(self._status)
            data.update(self._params)
            return data

    def latest_frame(self) -> tuple[np.ndarray | None, int]:
        """The newest frame and its id. The id is what lets a stream skip an unchanged frame."""
        with self._lock:
            return self._raw_frame, self._frame_id

    def has_video_client(self) -> bool:
        """True while anyone is watching /video.

        The tracking loop reads this to decide whether the per-frame solvePnP is worth running:
        yaw/pitch/roll only feed the log line and this stream.
        """
        return self._video_clients > 0

    def add_video_client(self) -> None:
        with self._lock:
            self._video_clients += 1

    def remove_video_client(self) -> None:
        with self._lock:
            self._video_clients -= 1
