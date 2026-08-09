"""
Camera sources — CSI (Jetson), local webcam, TCP network receiver, video file, or none at all.

NullCamera and try_open_camera exist because a missing camera must not be able to stop Kai from
starting: the dashboard, voice assistant, wake word and servos are all camera-independent, and
face_track.py's loop already tolerates an endless run of no-frame ticks. So opening a camera is a
probe that returns a value (like servo/servo_serial.py's detect_arduino) rather than something that
raises, and "no camera" is a source in its own right that reports why.
"""

from __future__ import annotations

import glob
import socket
import struct
import threading
import time
from queue import Empty, Queue

import cv2
import numpy as np

# Tunable capture settings live in config/camera.py; re-imported so existing
# `from vision.camera import NETWORK_PORT` call sites keep working.
from config.camera import (
    NETWORK_PORT, CSI_WIDTH, CSI_HEIGHT, CSI_FRAMERATE, CSI_PULL_TIMEOUT_MS,
    LOCAL_WIDTH, LOCAL_HEIGHT, CSI_FIRST_FRAME_S, CAMERA_REQUIRE_DEVICE_NODE,
    CAMERA_PROBE_MEMO_S,
)

FRAME_HEADER = 4   # struct ">L" — wire format, not a tunable


class NullCamera:
    """The "no camera" source: always reports no frame, and carries the reason why.

    Drop-in for every other source here (read/close/source_name), so face_track.py can start with one
    of these and have a real camera swapped in underneath later. `reason` is published to the
    dashboard as cam_reason — the difference between "NO CAMERA (ribbon not detected)" and a UI that
    just shows a frozen LIVE badge.
    """

    source_name = "none"
    connected   = False        # honest: nothing is attached. Unlike the other sources, this one's
                               # connected flag actually reaches the operator.

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def read(self) -> np.ndarray | None:
        time.sleep(0.1)        # avoid busy-spinning the reader thread at 100% CPU
        return None

    def close(self) -> None:
        pass


class CSICamera:
    """NVIDIA CSI camera via GStreamer appsink — Jetson Orin Nano."""

    source_name = "csi"
    connected   = True

    def __init__(self, sensor_id: int = 0, width: int = CSI_WIDTH, height: int = CSI_HEIGHT,
                 framerate: int = CSI_FRAMERATE) -> None:
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        Gst.init(None)
        self._Gst    = Gst
        self._width  = width
        self._height = height

        pipeline_str = (
            f'nvarguscamerasrc sensor-id={sensor_id} ! '
            f'video/x-raw(memory:NVMM),width={width},height={height},'
            f'framerate={framerate}/1 ! '
            f'nvvidconv flip-method=0 ! '
            f'video/x-raw,format=BGRx ! '
            f'videoconvert ! '
            f'video/x-raw,format=BGR ! '
            f'appsink name=sink max-buffers=1 drop=true sync=false'
        )
        self._pipeline = Gst.parse_launch(pipeline_str)
        self._sink     = self._pipeline.get_by_name('sink')
        self._pipeline.set_state(Gst.State.PLAYING)
        self._pipeline.get_state(5 * Gst.SECOND)

    def read(self) -> np.ndarray | None:
        Gst    = self._Gst
        # CSI_PULL_TIMEOUT_MS (not a full second): with max-buffers=1 drop=true the sink always
        # holds the freshest frame, so a long block buys nothing — a miss just returns None and
        # the caller (or CameraThread) retries. Keeps a sensor stall from freezing the loop.
        sample = self._sink.emit('try-pull-sample', CSI_PULL_TIMEOUT_MS * Gst.MSECOND)
        if sample is None:
            return None
        buf      = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        arr = np.frombuffer(info.data, dtype=np.uint8).reshape(
            (self._height, self._width, 3)
        ).copy()
        buf.unmap(info)
        return arr

    def close(self) -> None:
        self._pipeline.set_state(self._Gst.State.NULL)


class VideoFileCamera:
    """Reads frames from a video file, throttled to the video's native FPS. Loops."""

    source_name = "video_file"
    connected   = True

    def __init__(self, path: str) -> None:
        self._cap = cv2.VideoCapture(path)
        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open video: {path}")
        self.fps         = self._cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.width       = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height      = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.frame_count = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self._interval   = 1.0 / self.fps
        self._next_t     = 0.0

    def read(self) -> np.ndarray | None:
        now = time.monotonic()
        if now < self._next_t:
            return None
        ok, frame = self._cap.read()
        if not ok:                   # EOF — loop back to start
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
            if not ok:
                return None
        self._next_t = now + self._interval
        return frame

    def close(self) -> None:
        self._cap.release()


class LocalCamera:
    """OpenCV webcam reader."""

    source_name = "local"
    connected   = True

    def __init__(self, index: int = 0, width: int = LOCAL_WIDTH, height: int = LOCAL_HEIGHT) -> None:
        self._cap = cv2.VideoCapture(index)
        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open camera index {index}")
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    def read(self) -> np.ndarray | None:
        ok, frame = self._cap.read()
        return frame if ok else None

    def close(self) -> None:
        self._cap.release()


class NetworkReceiver:
    """TCP client — length-prefixed JPEG frames from laptop_camera.py."""

    def __init__(self, host: str, port: int = NETWORK_PORT) -> None:
        self._host    = host
        self._port    = port
        self._queue: Queue[np.ndarray] = Queue(maxsize=1)
        self._running = False
        self._thread: threading.Thread | None = None
        self.connected   = False
        self.source_name = f"network:{host}:{port}"

    def start(self) -> None:
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while self._running:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.connect((self._host, self._port))
                self.connected = True
                print(f"[camera] Connected to {self._host}:{self._port}")
                self._read_frames(sock)
            except OSError as exc:
                self.connected = False
                print(f"[camera] Network error: {exc}")
            finally:
                sock.close()
                self.connected = False
            if self._running:
                print("[camera] Retrying in 2s...")
                threading.Event().wait(2.0)

    def _read_frames(self, sock: socket.socket) -> None:
        buf = b""
        while self._running:
            try:
                while len(buf) < FRAME_HEADER:
                    chunk = sock.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                msg_len = struct.unpack(">L", buf[:FRAME_HEADER])[0]
                while len(buf) < FRAME_HEADER + msg_len:
                    chunk = sock.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                jpg   = buf[FRAME_HEADER:FRAME_HEADER + msg_len]
                buf   = buf[FRAME_HEADER + msg_len:]
                frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    if self._queue.full():
                        try:
                            self._queue.get_nowait()
                        except Empty:
                            pass
                    self._queue.put(frame)
            except OSError:
                return

    def read(self) -> np.ndarray | None:
        try:
            return self._queue.get(timeout=0.01)
        except Empty:
            return None

    def close(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)


class CameraThread:
    """Owns the active camera and reads the newest frame into a single slot on its own
    thread, so the tracking loop never blocks on a slow or stalled camera read. Also
    drains the camera-swap queue (live <-> uploaded video) off the tracking loop.

    Drop-old semantics: there is no frame queue, only the most recent frame. latest()
    consumes it (returns it once, then None until a fresh frame arrives) so the loop
    coasts the jaw/servo on no-new-frame ticks instead of re-processing a stale frame.
    """

    def __init__(self, camera, swap_queue: "Queue") -> None:
        self._live_camera = camera
        self._camera      = camera
        self._swap_queue  = swap_queue
        self._lock        = threading.Lock()
        self._frame: np.ndarray | None = None
        self._running     = False
        self._thread: threading.Thread | None = None
        # When the active source last produced a frame. The supervisor uses this to notice a camera
        # that has stopped delivering (unplugged mid-run, CSI pipeline wedged) — read() returning None
        # forever is indistinguishable from a healthy idle camera without it.
        self._last_frame_t = 0.0

    @property
    def source_name(self) -> str:
        return self._camera.source_name

    @property
    def last_frame_t(self) -> float:
        """time.monotonic() of the last frame from the ACTIVE source; 0.0 if none yet."""
        with self._lock:
            return self._last_frame_t

    @property
    def showing_live(self) -> bool:
        """False while an uploaded video is playing — the live camera's health says nothing then."""
        return self._camera is self._live_camera

    def note_frame_time(self, now: float) -> None:
        """Reset the staleness clock. Called when a source is swapped in, so a fresh camera gets its
        grace period rather than being judged on the previous source's last frame."""
        with self._lock:
            self._last_frame_t = now

    def start(self) -> "CameraThread":
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _apply_swaps(self) -> None:
        """Drain every pending swap, not just one.

        Draining matters because the queue is shallow with drop-oldest replace: an /upload_video and a
        supervisor "a real camera appeared" swap can be in flight together, and handling one per tick
        used to let the newer one evict the older before it was ever applied.
        """
        while True:
            try:
                kind, new_cam = self._swap_queue.get_nowait()
            except Empty:
                return

            if kind == "video":
                if self._camera is not self._live_camera:
                    self._camera.close()
                self._camera = new_cam
                print(f"[camera] Video loaded: {new_cam.width}×{new_cam.height} @ {new_cam.fps:.0f}fps")

            elif kind == "live_source":
                # Replace what "live" MEANS, without yanking a playing upload: a camera that appears
                # while a video is being reviewed should become the source /stop_video returns to.
                showing_live = self._camera is self._live_camera
                old, self._live_camera = self._live_camera, new_cam
                if showing_live:
                    self._camera = new_cam      # reassign before closing, so no read hits a closed cam
                old.close()
                print(f"[camera] live source is now {new_cam.source_name}", flush=True)

            else:                               # "live" — back to the live source from a video
                if self._camera is not self._live_camera:
                    self._camera.close()
                self._camera = self._live_camera
                print("[camera] Returned to live camera")

    def _loop(self) -> None:
        while self._running:
            self._apply_swaps()
            frame = self._camera.read()
            if frame is None:
                # Some sources (VideoFileCamera throttling to native fps, _NullCamera)
                # return None immediately — sleep briefly so we don't busy-spin a core.
                time.sleep(0.003)
                continue
            with self._lock:
                self._frame = frame
                self._last_frame_t = time.monotonic()

    def latest(self) -> np.ndarray | None:
        with self._lock:
            frame       = self._frame
            self._frame = None
        return frame

    def close(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._camera is not self._live_camera:
            self._camera.close()
        self._live_camera.close()


DEVICE_GLOB = "/dev/video*"

_probe_lock       = threading.Lock()
_last_fail_sig: tuple[str, ...] | None = None
_last_fail_reason = ""
_last_fail_at     = 0.0


def device_signature() -> tuple[str, ...]:
    """The set of capture device nodes present right now. Cheap: one glob, no device opened.

    This is the gate that makes repeated probing free. A CSI sensor whose i2c probe failed registers
    no node at all, so on a robot with a broken ribbon this returns () in microseconds instead of
    stalling 10s in Argus every retry. It doubles as a change detector: when the tuple changes,
    something was plugged in and an expensive probe is worth doing again.
    """
    try:
        return tuple(sorted(glob.glob(DEVICE_GLOB)))
    except OSError:
        return ()


def reset_probe_memo() -> None:
    """Forget the cached failure so the next try_open_camera really probes. Tests, and anywhere an
    operator explicitly asks to retry."""
    global _last_fail_sig, _last_fail_reason, _last_fail_at
    with _probe_lock:
        _last_fail_sig, _last_fail_reason, _last_fail_at = None, "", 0.0


def try_open_camera(index: int = 0, network_host: str | None = None,
                    network_port: int = NETWORK_PORT,
                    csi_first_frame_s: float = CSI_FIRST_FRAME_S,
                    force: bool = False,
                    ) -> tuple[object | None, str]:
    """Open the best available camera, or return (None, reason). Never raises.

    Order is unchanged from the original open_camera: CSI, then local V4L2, then the network receiver
    if a host was given. What is new is that every failure path yields a human-readable reason (it
    ends up on the dashboard as cam_reason), and that two short-circuits keep this cheap enough to
    call on a timer — see device_signature() and the failure memo below.

    force=True ignores the memo and probes for real; that is what the dashboard's "Probe now" does.
    """
    sig = device_signature()

    def fail(reason: str, *, expensive: bool) -> tuple[None, str]:
        # Only remember failures that COST something. A cheap-gate miss is not worth memoising, and
        # memoising it would suppress the real probe when a node finally appears.
        if expensive:
            global _last_fail_sig, _last_fail_reason, _last_fail_at
            with _probe_lock:
                _last_fail_sig, _last_fail_reason = sig, reason
                _last_fail_at = time.monotonic()
        return None, reason

    if CAMERA_REQUIRE_DEVICE_NODE and not sig:
        if network_host:
            return _open_network(network_host, network_port)
        return fail("no /dev/video* device and no --network host", expensive=False)

    if not force:
        with _probe_lock:
            fresh = (time.monotonic() - _last_fail_at) < CAMERA_PROBE_MEMO_S
            if _last_fail_sig is not None and _last_fail_sig == sig and fresh:
                # Same devices as the last real attempt, which failed recently. Nothing has changed,
                # so skip GStreamer and OpenCV. The memo EXPIRES on purpose — a CSI probe can fail
                # only because Argus was still starting, and that camera deserves another try even
                # though no node came or went.
                return None, _last_fail_reason

    reasons = []

    try:
        cam   = CSICamera(sensor_id=index)
        frame = None
        for _ in range(max(1, int(csi_first_frame_s / 0.25))):
            frame = cam.read()
            if frame is not None:
                break
            time.sleep(0.25)
        if frame is not None:
            print(f"[camera] CSI camera (sensor {index})", flush=True)
            reset_probe_memo()
            return cam, ""
        cam.close()
        reasons.append(f"CSI opened but produced no frame in {csi_first_frame_s:g}s")
    except Exception as exc:
        reasons.append(f"CSI unavailable ({exc})")

    try:
        cap = cv2.VideoCapture(index)
        ok  = cap.isOpened()
        if ok:
            ret, _ = cap.read()
            ok = ret
        cap.release()
        if ok:
            local = LocalCamera(index)     # can still raise if the device vanished mid-probe
            print(f"[camera] Local camera (index {index})", flush=True)
            reset_probe_memo()
            return local, ""
        reasons.append(f"no V4L2 capture at index {index}")
    except Exception as exc:
        reasons.append(f"V4L2 index {index} unavailable ({exc})")

    if network_host:
        return _open_network(network_host, network_port)

    return fail("; ".join(reasons) or "no camera found", expensive=True)


def _open_network(host: str, port: int) -> tuple[object, str]:
    """The network receiver always "succeeds" — start() spawns a thread that retries the connection
    forever, so being unreachable is a state it reports, not a failure to open."""
    print(f"[camera] streaming from {host}:{port}", flush=True)
    rec = NetworkReceiver(host, port)
    rec.start()
    reset_probe_memo()
    return rec, ""


def open_camera(index: int, network_host: str | None,
                network_port: int) -> CSICamera | LocalCamera | NetworkReceiver:
    """Raising wrapper around try_open_camera, kept for callers that want a camera or nothing.

    face_track.py no longer uses this — it must not die when there is no camera. Retained because a
    hard failure is still the right behaviour for a one-shot script.
    """
    cam, reason = try_open_camera(index, network_host, network_port)
    if cam is None:
        raise RuntimeError(f"No camera found ({reason}). Check connections or use --network to "
                           f"stream from another device.")
    return cam
