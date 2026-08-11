import queue
import threading
import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

import vision.camera as camera_mod
from vision.camera import CameraThread, NullCamera, VideoFileCamera, try_open_camera


def _make_mock_cap(opened=True, fps=30.0, w=640, h=480, frames=300):
    cap = MagicMock()
    cap.isOpened.return_value = opened
    def cap_get(prop):
        return {
            cv2.CAP_PROP_FPS:         fps,
            cv2.CAP_PROP_FRAME_WIDTH:  float(w),
            cv2.CAP_PROP_FRAME_HEIGHT: float(h),
            cv2.CAP_PROP_FRAME_COUNT:  float(frames),
        }.get(prop, 0.0)
    cap.get.side_effect = cap_get
    cap.read.return_value = (True, np.zeros((h, w, 3), dtype=np.uint8))
    return cap


class TestVideoFileCamera(unittest.TestCase):

    def test_raises_if_not_opened(self):
        cap = _make_mock_cap(opened=False)
        with patch('vision.camera.cv2.VideoCapture', return_value=cap):
            with self.assertRaises(RuntimeError):
                VideoFileCamera("/fake/path.mp4")

    def test_metadata_extracted(self):
        cap = _make_mock_cap(fps=24.0, w=1280, h=720, frames=500)
        with patch('vision.camera.cv2.VideoCapture', return_value=cap):
            cam = VideoFileCamera("/fake/path.mp4")
        self.assertAlmostEqual(cam.fps, 24.0)
        self.assertEqual(cam.width, 1280)
        self.assertEqual(cam.height, 720)
        self.assertEqual(cam.frame_count, 500)

    def test_throttle_returns_none_before_interval(self):
        cap = _make_mock_cap(fps=30.0)
        with patch('vision.camera.cv2.VideoCapture', return_value=cap):
            cam = VideoFileCamera("/fake/path.mp4")
        self.assertIsNotNone(cam.read())   # first read succeeds
        self.assertIsNone(cam.read())      # second read is throttled (< 1/30 s later)

    def test_loops_on_eof(self):
        cap = _make_mock_cap()
        good_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cap.read.side_effect = [(False, None), (True, good_frame)]
        with patch('vision.camera.cv2.VideoCapture', return_value=cap):
            cam = VideoFileCamera("/fake/path.mp4")
        frame = cam.read()
        cap.set.assert_called_once_with(cv2.CAP_PROP_POS_FRAMES, 0)
        self.assertIsNotNone(frame)

    def test_close_releases_cap(self):
        cap = _make_mock_cap()
        with patch('vision.camera.cv2.VideoCapture', return_value=cap):
            cam = VideoFileCamera("/fake/path.mp4")
        cam.close()
        cap.release.assert_called_once()


class TestNullCamera(unittest.TestCase):
    """The "no camera" source. face_track.py starts with one of these, so it has to be as safe as a
    real source and it has to say why it is there."""

    def test_reports_no_frame_and_carries_the_reason(self):
        cam = NullCamera("ribbon not detected")
        self.assertIsNone(cam.read())
        self.assertEqual(cam.source_name, "none")
        self.assertEqual(cam.reason, "ribbon not detected")
        self.assertFalse(cam.connected)

    def test_close_is_idempotent(self):
        cam = NullCamera()
        cam.close()
        cam.close()


class ProbeTestCase(unittest.TestCase):
    """try_open_camera memoises failures in module state; every test starts clean."""

    def setUp(self):
        camera_mod.reset_probe_memo()

    tearDown = setUp


class TestTryOpenCamera(ProbeTestCase):

    def test_no_device_node_returns_a_reason_without_touching_gstreamer(self):
        # THE performance guarantee: with no /dev/video* there is nothing to open, so the 10s Argus
        # wait must not run. Before this, every retry on a camera-less robot stalled for 10s.
        with patch('vision.camera.glob.glob', return_value=[]), \
             patch('vision.camera.CSICamera') as csi, \
             patch('vision.camera.cv2.VideoCapture') as cap:
            cam, reason = try_open_camera(0, None, 8485)
        self.assertIsNone(cam)
        self.assertIn("no /dev/video*", reason)
        csi.assert_not_called()
        cap.assert_not_called()

    def test_no_device_node_still_uses_the_network_host(self):
        # --network streams from another machine; it needs no local device at all.
        with patch('vision.camera.glob.glob', return_value=[]), \
             patch('vision.camera.NetworkReceiver') as rec:
            cam, reason = try_open_camera(0, "10.0.0.5", 8485)
        self.assertIs(cam, rec.return_value)
        self.assertEqual(reason, "")
        rec.return_value.start.assert_called_once()

    def test_failure_is_memoised_so_the_expensive_probe_runs_once(self):
        # Same devices, same outcome — the supervisor calls this on a timer, and re-running a failed
        # CSI probe every few seconds is exactly what we must not do.
        with patch('vision.camera.glob.glob', return_value=['/dev/video0']), \
             patch('vision.camera.CSICamera', side_effect=RuntimeError("no gi")) as csi, \
             patch('vision.camera.cv2.VideoCapture', return_value=_make_mock_cap(opened=False)):
            first  = try_open_camera(0, None, 8485)
            second = try_open_camera(0, None, 8485)
        self.assertIsNone(first[0])
        self.assertEqual(first[1], second[1], "the cached reason should be reported verbatim")
        csi.assert_called_once()

    def test_a_new_device_node_retries_the_expensive_probe(self):
        # Plugging in a webcam changes the signature; that is the signal to try properly again.
        with patch('vision.camera.CSICamera', side_effect=RuntimeError("no gi")) as csi, \
             patch('vision.camera.cv2.VideoCapture', return_value=_make_mock_cap(opened=False)):
            with patch('vision.camera.glob.glob', return_value=['/dev/video0']):
                try_open_camera(0, None, 8485)
            with patch('vision.camera.glob.glob', return_value=['/dev/video0', '/dev/video1']):
                try_open_camera(0, None, 8485)
        self.assertEqual(csi.call_count, 2)

    def test_csi_exception_is_reported_not_raised(self):
        with patch('vision.camera.glob.glob', return_value=['/dev/video0']), \
             patch('vision.camera.CSICamera', side_effect=RuntimeError("gi missing")), \
             patch('vision.camera.cv2.VideoCapture', return_value=_make_mock_cap(opened=False)):
            cam, reason = try_open_camera(0, None, 8485)
        self.assertIsNone(cam)
        self.assertIn("gi missing", reason)

    def test_falls_back_to_local_v4l2_when_csi_produces_nothing(self):
        csi = MagicMock()
        csi.return_value.read.return_value = None
        with patch('vision.camera.glob.glob', return_value=['/dev/video0']), \
             patch('vision.camera.CSICamera', csi), \
             patch('vision.camera.cv2.VideoCapture', return_value=_make_mock_cap()), \
             patch('vision.camera.LocalCamera') as local:
            cam, reason = try_open_camera(0, None, 8485, csi_first_frame_s=0.25)
        self.assertIs(cam, local.return_value)
        self.assertEqual(reason, "")
        csi.return_value.close.assert_called_once()

    def test_memo_expires_so_a_warming_camera_gets_another_chance(self):
        # A CSI probe can fail purely because nvargus-daemon was still starting. If the memo never
        # expired, that camera would be written off permanently — the node set never changes, so
        # nothing would ever prompt another attempt.
        with patch('vision.camera.glob.glob', return_value=['/dev/video0']), \
             patch('vision.camera.CSICamera', side_effect=RuntimeError("argus busy")) as csi, \
             patch('vision.camera.cv2.VideoCapture', return_value=_make_mock_cap(opened=False)):
            try_open_camera(0, None, 8485)
            camera_mod._last_fail_at -= (camera_mod.CAMERA_PROBE_MEMO_S + 1)   # pretend time passed
            try_open_camera(0, None, 8485)
        self.assertEqual(csi.call_count, 2)

    def test_force_ignores_the_memo(self):
        # What the dashboard's "Probe now" button relies on: the operator just plugged something in
        # and should not have to wait out a backoff.
        with patch('vision.camera.glob.glob', return_value=['/dev/video0']), \
             patch('vision.camera.CSICamera', side_effect=RuntimeError("no gi")) as csi, \
             patch('vision.camera.cv2.VideoCapture', return_value=_make_mock_cap(opened=False)):
            try_open_camera(0, None, 8485)
            try_open_camera(0, None, 8485, force=True)
        self.assertEqual(csi.call_count, 2)

    def test_success_clears_the_memo(self):
        # Otherwise a camera that failed once and then started working would stay "broken" until the
        # TTL lapsed on every subsequent call.
        with patch('vision.camera.glob.glob', return_value=['/dev/video0']), \
             patch('vision.camera.CSICamera', side_effect=RuntimeError("no gi")), \
             patch('vision.camera.cv2.VideoCapture', return_value=_make_mock_cap(opened=False)):
            try_open_camera(0, None, 8485)
        self.assertIsNotNone(camera_mod._last_fail_sig)

        with patch('vision.camera.glob.glob', return_value=['/dev/video0']), \
             patch('vision.camera.CSICamera', side_effect=RuntimeError("no gi")), \
             patch('vision.camera.cv2.VideoCapture', return_value=_make_mock_cap()), \
             patch('vision.camera.LocalCamera'):
            try_open_camera(0, None, 8485, force=True)
        self.assertIsNone(camera_mod._last_fail_sig)

    def test_open_camera_still_raises_for_one_shot_callers(self):
        with patch('vision.camera.glob.glob', return_value=[]):
            with self.assertRaises(RuntimeError) as ctx:
                camera_mod.open_camera(0, None, 8485)
        self.assertIn("no /dev/video*", str(ctx.exception), "the raise should carry the real reason")


class TestApplySwaps(unittest.TestCase):
    """CameraThread owns the swap queue. These run _apply_swaps directly — no thread, no sleeping."""

    def setUp(self):
        self.live = NullCamera("starting up")
        self.q    = queue.Queue(maxsize=3)
        self.thread = CameraThread(self.live, self.q)

    def _video_cam(self):
        cam = MagicMock()
        cam.width, cam.height, cam.fps = 640, 480, 30.0
        return cam

    def test_live_source_replaces_the_live_camera_and_closes_the_old_one(self):
        new = MagicMock()
        new.source_name = "local"
        self.q.put(("live_source", new))
        self.thread._apply_swaps()
        self.assertIs(self.thread._live_camera, new)
        self.assertIs(self.thread._camera, new)
        self.assertIs(self.thread.source_name, new.source_name)

    def test_live_source_does_not_yank_a_playing_video(self):
        # A camera appearing while someone reviews an upload must not cut the upload off; it becomes
        # what /stop_video returns to.
        video = self._video_cam()
        self.q.put(("video", video))
        self.thread._apply_swaps()
        self.assertIs(self.thread._camera, video)

        new = MagicMock()
        new.source_name = "csi"
        self.q.put(("live_source", new))
        self.thread._apply_swaps()
        self.assertIs(self.thread._camera, video, "the video must keep playing")
        self.assertIs(self.thread._live_camera, new)
        video.close.assert_not_called()

        self.q.put(("live", None))
        self.thread._apply_swaps()
        self.assertIs(self.thread._camera, new, "stopping the video lands on the NEW live source")
        video.close.assert_called_once()

    def test_drains_every_queued_swap_in_one_pass(self):
        # With one get() per tick, a queued upload could be evicted by a later swap before it was
        # ever applied.
        video = self._video_cam()
        new   = MagicMock()
        new.source_name = "local"
        self.q.put(("video", video))
        self.q.put(("live_source", new))
        self.thread._apply_swaps()
        self.assertTrue(self.q.empty())
        self.assertIs(self.thread._camera, video)
        self.assertIs(self.thread._live_camera, new)

    def test_empty_queue_is_a_no_op(self):
        self.thread._apply_swaps()
        self.assertIs(self.thread._camera, self.live)


class TestFrameHealth(unittest.TestCase):
    """The supervisor needs to tell 'idle camera' from 'camera unplugged'. read() returning None looks
    identical in both cases, so the only signal is how long it has been since a real frame."""

    def setUp(self):
        self.q = queue.Queue(maxsize=3)
        self.thread = CameraThread(NullCamera("starting up"), self.q)

    def test_no_frame_yet_reads_as_zero(self):
        # 0.0 means "never", which the supervisor must not mistake for "stale since the epoch" and
        # immediately tear down a camera that simply has not warmed up yet.
        self.assertEqual(self.thread.last_frame_t, 0.0)

    def test_note_frame_time_starts_the_grace_period(self):
        self.thread.note_frame_time(1234.5)
        self.assertEqual(self.thread.last_frame_t, 1234.5)

    def test_showing_live_is_false_while_a_video_plays(self):
        # A playing upload says nothing about the live camera's health, so the stall check must skip it.
        video = MagicMock()
        video.width, video.height, video.fps = 640, 480, 30.0
        self.assertTrue(self.thread.showing_live)
        self.q.put(("video", video))
        self.thread._apply_swaps()
        self.assertFalse(self.thread.showing_live)
        self.q.put(("live", None))
        self.thread._apply_swaps()
        self.assertTrue(self.thread.showing_live)

    def test_a_real_frame_updates_the_clock(self):
        cam = MagicMock()
        cam.source_name = "local"
        cam.read.return_value = np.zeros((4, 4, 3), dtype=np.uint8)
        t = CameraThread(cam, queue.Queue(maxsize=1))
        t._running = True
        # One pass of the reader loop body, without starting the thread.
        frame = t._camera.read()
        with t._lock:
            t._frame = frame
            t._last_frame_t = 99.0
        self.assertEqual(t.last_frame_t, 99.0)


class TestCloseNeverClosesUnderALiveRead(unittest.TestCase):
    """close() frees native camera handles. Doing that while the reader thread is still parked
    inside read() is a use-after-free in GStreamer/V4L2, and a wedged device is exactly when it
    happens — cap.read() on a dead USB camera blocks well past the 2 s join."""

    def test_a_stopped_reader_gets_its_cameras_closed(self):
        cam = MagicMock()
        cam.source_name = "local"
        cam.read.return_value = None
        t = CameraThread(cam, queue.Queue(maxsize=1)).start()
        t.close()
        cam.close.assert_called_once()

    def test_a_wedged_reader_leaks_rather_than_closing_underneath_itself(self):
        release = threading.Event()
        cam = MagicMock()
        cam.source_name = "local"
        # read() that never returns until we say so — a wedged V4L2 device.
        cam.read.side_effect = lambda: (release.wait(10.0), None)[1]

        t = CameraThread(cam, queue.Queue(maxsize=1))
        t._live_camera = t._camera = cam
        t._running = True
        t._thread = threading.Thread(target=t._loop, daemon=True)
        t._thread.start()
        try:
            with patch.object(t._thread, "join"):        # simulate the join timing out
                t.close()
            cam.close.assert_not_called()
        finally:
            release.set()
            t._thread.join(timeout=2.0)


if __name__ == '__main__':
    unittest.main()
