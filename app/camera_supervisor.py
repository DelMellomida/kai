"""Keeps the live camera in sync with what is actually plugged in and what the operator asked for.

face_track.py starts with no camera at all and this supervisor probes for one on a timer, hot-swaps
it in when it appears, releases it when camera_mode goes to "off", and notices when a camera that
was working stops delivering frames. That is what makes a missing ribbon a state the dashboard can
explain rather than a startup crash.

It owns three things and nothing else: the swap queue that vision/camera.CameraThread drains, the
lock-guarded state dict the dashboard reads (same shape as vision/controller.TrackingTarget), and
the "probe now" event. It knows about vision/ and settings.py; it does not know about Flask,
MediaPipe or the servo, so it can be driven with a fake camera and a fake clock.

Two-phase construction is deliberate. The instance exists at import time because the dashboard
routes need something to talk to before run() has parsed the CLI, and its seeded "starting up"
state is what stops the frontend claiming a live feed in that window. configure() then supplies the
CLI facts once they are known.
"""

from __future__ import annotations

import queue
import threading
import time

import settings
from config.camera import (
    CAMERA_RETRY_INTERVAL_S, CAMERA_RETRY_MAX_S, CAMERA_STALL_S,
    CSI_FIRST_FRAME_S, CSI_FIRST_FRAME_RETRY_S,
)
from vision import presence
from vision.camera import NullCamera, device_signature, try_open_camera


class CameraSupervisor:
    """The live camera's owner. One per process; face_track.py holds it."""

    def __init__(self) -> None:
        # Thread-safe camera swap signal: ("video", VideoFileCamera), ("live", None), or
        # ("live_source", camera) when a real camera appears or is released.
        # Depth 3, not 1: an upload and a supervisor swap can be in flight together, and with depth 1
        # the drop-oldest replace in _replace_swap would silently evict one before CameraThread
        # applied it.
        self.swap_queue: queue.Queue = queue.Queue(maxsize=3)

        # Written by the supervisor thread, read by the tracking loop's status publisher.
        self._lock = threading.Lock()
        self._state = {"reason": "starting up", "mode": "auto", "locked": False,
                       "next_probe_at": 0.0}
        self._probe_now = threading.Event()   # set by POST /camera/probe to skip the backoff wait

        self._live = False        # is the live source a real camera (vs a NullCamera)?
        self._last_reason = None  # last reason we logged, so a stuck robot doesn't spam the log
        self._cam_thread = None   # the live CameraThread, so we can check frame staleness

        # CLI facts, supplied by configure(). Until then the supervisor is inert — it is constructed
        # at import time so the routes have something to talk to, and only run() knows the flags.
        self._index = 0
        self._network_host = None
        self._network_port = 0
        self._forced_off = False

    # ── setup ───────────────────────────────────────────────────────────────

    def configure(self, index: int, network_host: str | None, network_port: int,
                  forced_off: bool) -> None:
        """Take the CLI facts. Called once from run(), before the supervisor thread starts."""
        self._index = index
        self._network_host = network_host
        self._network_port = network_port
        self._forced_off = bool(forced_off)
        self.set_state(mode=self.effective_mode(), locked=self._forced_off)

    def attach_thread(self, cam_thread) -> None:
        """Hand over the CameraThread, so frame staleness can be checked against it."""
        self._cam_thread = cam_thread

    # ── state, read by the dashboard ────────────────────────────────────────

    def set_state(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def settings_locked(self) -> dict:
        """Settings a CLI flag has taken away for this run -> why.

        The dashboard disables those controls and shows the reason, rather than accepting a click
        that silently does nothing.
        """
        return {"camera_mode": "locked off by --no-camera"} if self.snapshot()["locked"] else {}

    def effective_mode(self) -> str:
        """"auto" or "off". --no-camera wins over the stored setting.

        Like --no-servo, --no-camera declares this machine's hardware situation for this run, so a
        remote browser must not be able to re-enable hardware the operator disabled at launch. The
        dashboard is told (cam_mode_locked) so it can disable the control and say why instead of
        accepting a click that does nothing. scripts/autostart.sh does not pass --no-camera, so in
        production the setting rules.
        """
        if self._forced_off:
            return "off"
        return settings.get("camera_mode")

    # ── controls the dashboard reaches ──────────────────────────────────────

    def probe_now(self) -> None:
        """Wake the supervisor so a camera just plugged in is picked up now rather than after the
        backoff. The outcome arrives on /params as cam_source/cam_reason."""
        self._probe_now.set()

    def probe_pending(self) -> bool:
        return self._probe_now.is_set()

    def play_video(self, cam) -> None:
        """Show an uploaded video instead of the live camera."""
        self._replace_swap(("video", cam))

    def stop_video(self) -> None:
        """Back to whatever the live source currently is."""
        self._replace_swap(("live", None))

    # ── the supervisor loop ─────────────────────────────────────────────────

    def run(self, stop_evt: threading.Event) -> None:
        """Runs for the whole process rather than exiting once a camera is found, because
        camera_mode can be flipped to "off" later, and a USB camera can be unplugged and replugged.
        Parked cost is one settings lookup per interval.

        Backoff applies only to *expensive* failures. When there is no device node at all,
        try_open_camera returns in microseconds, so those attempts stay at the base interval —
        there is nothing to spare the machine from.
        """
        interval = CAMERA_RETRY_INTERVAL_S
        first    = True
        while not stop_evt.is_set():
            mode   = self.effective_mode()
            locked = self._forced_off
            self.set_state(mode=mode, locked=locked)

            if mode == "off":
                if self._live:
                    self._release("camera off (settings)" if not locked
                                  else "locked off by --no-camera")
                elif locked:
                    self.set_state(reason="locked off by --no-camera", next_probe_at=0.0)
                else:
                    self.set_state(reason="camera off (settings)", next_probe_at=0.0)
                interval = CAMERA_RETRY_INTERVAL_S
            elif self._live:
                # Parked on a live camera — but verify it is still DELIVERING. A camera unplugged
                # mid-run (or a wedged CSI pipeline) just returns no frames forever, which is
                # indistinguishable from a healthy idle camera unless we time it. Without this the
                # dashboard goes on reporting cam_source="csi" at 0 fps, claiming a feed that no
                # longer exists.
                last = self._cam_thread.last_frame_t if self._cam_thread is not None else 0.0
                if (self._cam_thread is not None and self._cam_thread.showing_live and last
                        and (time.monotonic() - last) > CAMERA_STALL_S):
                    self._release(f"camera stopped delivering frames "
                                  f"({CAMERA_STALL_S:g}s with none) — looking for it again")
                    interval = CAMERA_RETRY_INTERVAL_S
            else:
                # A shorter Argus budget on retries than at startup: a node that just appeared is
                # warm, and we would rather come back around than block this thread for 10s.
                budget = CSI_FIRST_FRAME_S if first else CSI_FIRST_FRAME_RETRY_S
                cheap  = not device_signature()
                cam, reason = try_open_camera(self._index, self._network_host, self._network_port,
                                              csi_first_frame_s=budget,
                                              force=self._probe_now.is_set())
                first = False
                if cam is not None:
                    self._acquire(cam)
                    interval = CAMERA_RETRY_INTERVAL_S
                else:
                    self._report_failure(reason)
                    interval = (CAMERA_RETRY_INTERVAL_S if cheap
                                else min(interval * 2, CAMERA_RETRY_MAX_S))

            self._probe_now.clear()
            self.set_state(next_probe_at=time.monotonic() + interval)
            # Wake early for shutdown or for an explicit "Probe now".
            deadline = time.monotonic() + interval
            while not stop_evt.is_set() and not self._probe_now.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                stop_evt.wait(min(0.25, remaining))

    # ── internals ───────────────────────────────────────────────────────────

    def _acquire(self, cam) -> None:
        self._live = True
        self._last_reason = None
        self.set_state(reason="")
        if self._cam_thread is not None:
            # Start the staleness clock now, so a camera that takes a moment to produce its first
            # frame is not immediately judged dead by the stall check.
            self._cam_thread.note_frame_time(time.monotonic())
        self._replace_swap(("live_source", cam))
        presence.reset()   # a hot-swap means the old presence history describes a different camera
        print(f"[camera] live camera acquired: {cam.source_name}", flush=True)

    def _release(self, reason: str) -> None:
        self._live = False
        self._last_reason = reason
        self.set_state(reason=reason, next_probe_at=0.0)
        self._replace_swap(("live_source", NullCamera(reason)))
        presence.reset()
        print(f"[camera] released the camera — {reason}", flush=True)

    def _report_failure(self, reason: str) -> None:
        """Record why there is no camera, logging only when the reason CHANGES — this runs on a
        timer for the life of the process, and a fixed hardware fault would otherwise fill the
        log."""
        self.set_state(reason=reason)
        if reason != self._last_reason:
            print(f"[camera] no camera — {reason}", flush=True)
            self._last_reason = reason

    def _replace_swap(self, item: tuple) -> None:
        try:
            self.swap_queue.put_nowait(item)
        except queue.Full:
            try:
                self.swap_queue.get_nowait()
            except queue.Empty:
                pass
            self.swap_queue.put_nowait(item)
