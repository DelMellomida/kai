"""CameraSupervisor — the module that decides whether the robot believes it has a camera.

S8. The module docstring has advertised testability since it was written ("it knows about vision/
and settings.py; it does not know about Flask, MediaPipe or the servo, so it can be driven with a
fake camera and a fake clock") and nothing took it up, so several behaviours that exist *because of
specific past bugs* had nothing stopping a refactor from undoing them:

  * a depth-1 swap queue silently evicting a swap CameraThread had not applied yet
  * a stalled CSI pipeline reported as a live feed at 0 fps
  * a backoff that punished failures which cost nothing

Every test here drives `_step()` directly against a fake clock and a fake `try_open_camera`. No
camera, no GStreamer, no OpenCV device access, and nothing sleeps.
"""

import queue
import threading
import unittest
from unittest.mock import patch

from app import camera_supervisor as cs
from app.camera_supervisor import CameraSupervisor
from config.camera import (
    CAMERA_RETRY_INTERVAL_S, CAMERA_RETRY_MAX_S, CAMERA_STALL_S,
    CSI_FIRST_FRAME_S, CSI_FIRST_FRAME_RETRY_S,
)


class _FakeClock:
    """Stands in for the module's `time`. Only monotonic() is ever called from _step()."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeCamera:
    def __init__(self, source_name: str = "usb0") -> None:
        self.source_name = source_name


class _FakeCamThread:
    """The slice of CameraThread the supervisor actually touches."""

    def __init__(self, last_frame_t: float = 0.0, showing_live: bool = True) -> None:
        self.last_frame_t = last_frame_t
        self.showing_live = showing_live
        self.noted: list[float] = []

    def note_frame_time(self, now: float) -> None:
        self.noted.append(now)
        self.last_frame_t = now


class SupervisorCase(unittest.TestCase):
    """Builds a configured supervisor with every outside edge faked.

    `presence.reset` is patched rather than left alone because it is module-global state shared with
    the rest of the suite, and because "did the hot-swap reset presence?" is one of the criteria —
    it needs to be observable, not merely harmless.
    """

    def setUp(self):
        self.clock = _FakeClock()
        self.sup = CameraSupervisor()
        self.cam_thread = _FakeCamThread()
        self.sup.attach_thread(self.cam_thread)

        self.open_result = (None, "no camera found")
        self.signature = ()

        patches = [
            patch.object(cs, "time", self.clock),
            patch.object(cs, "try_open_camera", self._try_open),
            patch.object(cs, "device_signature", lambda: self.signature),
            patch.object(cs.presence, "reset"),
            patch("builtins.print"),          # the module logs to stdout; not the subject here
        ]
        self.mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.presence_reset = self.mocks[3]

        self.open_calls: list[dict] = []
        self.configure()

    def configure(self, forced_off: bool = False, mode: str = "auto"):
        self._mode = mode
        patcher = patch.object(cs.settings, "get", lambda key: self._mode if key == "camera_mode" else None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sup.configure(index=0, network_host=None, network_port=0, forced_off=forced_off)

    def _try_open(self, index, host, port, csi_first_frame_s=None, force=False):
        self.open_calls.append({"index": index, "csi_first_frame_s": csi_first_frame_s,
                                "force": force})
        return self.open_result

    def drain(self) -> list[tuple]:
        items = []
        while True:
            try:
                items.append(self.sup.swap_queue.get_nowait())
            except queue.Empty:
                return items


class TestSuccessfulProbe(SupervisorCase):
    def test_a_camera_that_appears_is_acquired_and_swapped_in(self):
        cam = _FakeCamera("usb0")
        self.open_result = (cam, "")
        self.sup._step()

        self.assertEqual(self.drain(), [("live_source", cam)])
        self.assertTrue(self.sup._live)
        self.assertEqual(self.sup.snapshot()["reason"], "")
        self.presence_reset.assert_called_once_with()

    def test_acquiring_starts_the_staleness_clock(self):
        # Without this a camera that takes a moment to produce its first frame is judged dead by the
        # stall check on the very next pass.
        self.open_result = (_FakeCamera(), "")
        self.sup._step()
        self.assertEqual(self.cam_thread.noted, [self.clock.now])

    def test_a_successful_probe_resets_the_backoff(self):
        self.signature = ("/dev/video0",)          # expensive failures, so the backoff climbs
        self.sup._step()
        self.sup._step()
        self.assertGreater(self.sup._interval, CAMERA_RETRY_INTERVAL_S)

        self.open_result = (_FakeCamera(), "")
        self.assertEqual(self.sup._step(), CAMERA_RETRY_INTERVAL_S)


class TestCameraOff(SupervisorCase):
    def test_mode_off_releases_a_live_camera_and_enqueues_a_null(self):
        self.open_result = (_FakeCamera(), "")
        self.sup._step()
        self.drain()

        self._mode = "off"
        self.sup._step()

        swaps = self.drain()
        self.assertEqual(len(swaps), 1)
        kind, cam = swaps[0]
        self.assertEqual(kind, "live_source")
        self.assertIsInstance(cam, cs.NullCamera)
        self.assertFalse(self.sup._live)
        self.assertEqual(self.sup.snapshot()["reason"], "camera off (settings)")

    def test_mode_off_with_no_live_camera_just_reports(self):
        self._mode = "off"
        self.sup._step()
        self.assertEqual(self.drain(), [], "nothing to release, so nothing to enqueue")
        self.assertEqual(self.sup.snapshot()["reason"], "camera off (settings)")

    def test_off_never_probes(self):
        self._mode = "off"
        self.sup._step()
        self.assertEqual(self.open_calls, [])


class TestForcedOff(SupervisorCase):
    """--no-camera declares this machine's hardware for the run; a browser must not undo it."""

    def setUp(self):
        super().setUp()
        self.configure(forced_off=True, mode="auto")

    def test_cli_flag_beats_the_stored_setting(self):
        self.assertEqual(self.sup.effective_mode(), "off")
        self.sup._step()
        self.assertEqual(self.open_calls, [])

    def test_reports_locked_with_the_reason_the_dashboard_shows(self):
        self.sup._step()
        snap = self.sup.snapshot()
        self.assertTrue(snap["locked"])
        self.assertEqual(snap["mode"], "off")
        self.assertEqual(snap["reason"], "locked off by --no-camera")

    def test_settings_locked_explains_the_disabled_control(self):
        self.sup._step()
        self.assertEqual(self.sup.settings_locked(),
                         {"camera_mode": "locked off by --no-camera"})

    def test_a_live_camera_is_released_with_the_locked_reason(self):
        # Reachable when --no-camera is set while a camera is already held: the release reason must
        # be the locked one, not "camera off (settings)", or the dashboard blames the wrong thing.
        self.sup._live = True
        self.sup._step()
        self.assertEqual(self.sup.snapshot()["reason"], "locked off by --no-camera")


class TestStallDetection(SupervisorCase):
    """A camera that stops delivering is gone. Without this the dashboard reports a live feed at
    0 fps — a stalled CSI pipeline is indistinguishable from a healthy idle one unless it is timed."""

    def _go_live(self):
        self.open_result = (_FakeCamera(), "")
        self.sup._step()
        self.drain()
        self.presence_reset.reset_mock()

    def test_a_stalled_live_camera_is_released(self):
        self._go_live()
        self.clock.advance(CAMERA_STALL_S + 1)
        self.sup._step()

        self.assertFalse(self.sup._live)
        self.assertIn("stopped delivering frames", self.sup.snapshot()["reason"])
        self.presence_reset.assert_called_once_with()

    def test_a_camera_still_inside_the_window_is_left_alone(self):
        self._go_live()
        self.clock.advance(CAMERA_STALL_S - 1)
        self.sup._step()
        self.assertTrue(self.sup._live)
        self.assertEqual(self.drain(), [])

    def test_an_uploaded_video_suspends_the_check(self):
        # showing_live is False while a video plays, and the live camera's health says nothing then.
        # Releasing here would tear down a working camera because a user opened a video file.
        self._go_live()
        self.cam_thread.showing_live = False
        self.clock.advance(CAMERA_STALL_S * 10)
        self.sup._step()
        self.assertTrue(self.sup._live)

    def test_a_camera_that_has_never_delivered_is_not_judged(self):
        # last_frame_t == 0.0 means "no frame yet", not "a frame at time zero". The `and last` guard
        # is what stops a camera being released before it has had a chance to produce anything.
        self._go_live()
        self.cam_thread.last_frame_t = 0.0
        self.clock.advance(CAMERA_STALL_S * 10)
        self.sup._step()
        self.assertTrue(self.sup._live)

    def test_no_cam_thread_attached_is_not_a_stall(self):
        sup = CameraSupervisor()
        sup.configure(index=0, network_host=None, network_port=0, forced_off=False)
        sup._live = True
        sup._step()
        self.assertTrue(sup._live)


class TestBackoff(SupervisorCase):
    """Backoff exists to spare the machine expensive probes. A failure that costs nothing to
    discover has nothing to be spared from, so it must not push the retry out."""

    def test_expensive_failures_double_up_to_the_ceiling(self):
        self.signature = ("/dev/video0",)
        seen = [self.sup._step() for _ in range(12)]
        self.assertEqual(seen[0], CAMERA_RETRY_INTERVAL_S * 2)
        self.assertEqual(seen[1], CAMERA_RETRY_INTERVAL_S * 4)
        self.assertEqual(seen[-1], CAMERA_RETRY_MAX_S)
        self.assertTrue(all(a <= b for a, b in zip(seen, seen[1:])), "backoff must be monotonic")

    def test_cheap_failures_stay_at_the_base_interval(self):
        # No device node at all: try_open_camera returns in microseconds.
        self.signature = ()
        for _ in range(5):
            self.assertEqual(self.sup._step(), CAMERA_RETRY_INTERVAL_S)

    def test_a_cheap_failure_after_expensive_ones_returns_to_base(self):
        self.signature = ("/dev/video0",)
        self.sup._step()
        self.sup._step()
        self.signature = ()
        self.assertEqual(self.sup._step(), CAMERA_RETRY_INTERVAL_S)

    def test_next_probe_at_is_published_for_the_countdown(self):
        interval = self.sup._step()
        self.assertAlmostEqual(self.sup.snapshot()["next_probe_at"], self.clock.now + interval)


class TestArgusBudget(SupervisorCase):
    def test_startup_gets_the_long_budget_and_retries_the_short_one(self):
        # A node that just appeared is warm; blocking the supervisor thread for 10 s on every retry
        # is worse than coming back around.
        self.sup._step()
        self.sup._step()
        self.assertEqual([c["csi_first_frame_s"] for c in self.open_calls],
                         [CSI_FIRST_FRAME_S, CSI_FIRST_FRAME_RETRY_S])


class TestProbeNow(SupervisorCase):
    def test_probe_now_forces_the_open_and_is_consumed(self):
        self.sup.probe_now()
        self.assertTrue(self.sup.probe_pending())
        self.sup._step()
        self.assertTrue(self.open_calls[0]["force"])
        self.assertFalse(self.sup.probe_pending(), "the request must not survive the pass")

    def test_probe_now_short_circuits_the_wait(self):
        # run()'s wait loop, driven directly: an idle supervisor parked on a 60 s backoff must leave
        # the moment someone presses Probe now, or the button appears dead.
        stop = threading.Event()
        self.sup._interval = CAMERA_RETRY_MAX_S
        self.sup.probe_now()
        with patch.object(stop, "wait") as waited:
            deadline = self.clock.now + CAMERA_RETRY_MAX_S
            while not stop.is_set() and not self.sup._probe_now.is_set():
                remaining = deadline - self.clock.monotonic()
                if remaining <= 0:
                    break
                stop.wait(min(0.25, remaining))
        waited.assert_not_called()


class TestReportFailure(SupervisorCase):
    """A fixed hardware fault runs this on a timer for the life of the process."""

    def test_logs_only_when_the_reason_changes(self):
        with patch("builtins.print") as printed:
            self.sup._report_failure("no /dev/video*")
            self.sup._report_failure("no /dev/video*")
            self.sup._report_failure("no /dev/video*")
            self.assertEqual(printed.call_count, 1)
            self.sup._report_failure("csi pipeline timed out")
            self.assertEqual(printed.call_count, 2)

    def test_the_reason_is_published_every_time_regardless(self):
        # Only the LOG is rate-limited. The dashboard must always see current state.
        self.sup._report_failure("no /dev/video*")
        self.sup.set_state(reason="something else")
        self.sup._report_failure("no /dev/video*")
        self.assertEqual(self.sup.snapshot()["reason"], "no /dev/video*")

    def test_acquiring_clears_the_dedup_so_the_next_failure_logs(self):
        self.sup._report_failure("no /dev/video*")
        self.open_result = (_FakeCamera(), "")
        self.sup._step()
        with patch("builtins.print") as printed:
            self.sup._report_failure("no /dev/video*")
            printed.assert_called_once()


class TestReplaceSwap(SupervisorCase):
    """Depth 3, drop-oldest. Depth 1 once silently evicted a swap CameraThread had not applied."""

    def test_items_queue_up_to_capacity(self):
        for i in range(3):
            self.sup._replace_swap(("live_source", i))
        self.assertEqual(self.drain(), [("live_source", 0), ("live_source", 1), ("live_source", 2)])

    def test_a_full_queue_drops_the_oldest_and_keeps_the_newest(self):
        for i in range(5):
            self.sup._replace_swap(("live_source", i))
        self.assertEqual(self.drain(), [("live_source", 2), ("live_source", 3), ("live_source", 4)])

    def test_video_and_live_controls_go_through_the_same_path(self):
        cam = _FakeCamera("video_file")
        self.sup.play_video(cam)
        self.sup.stop_video()
        self.assertEqual(self.drain(), [("video", cam), ("live", None)])

    def test_an_upload_and_a_supervisor_swap_can_coexist(self):
        # The bug depth 3 exists for: with depth 1 the acquire would evict the upload before
        # CameraThread ever saw it.
        video = _FakeCamera("video_file")
        self.sup.play_video(video)
        self.open_result = (_FakeCamera("usb0"), "")
        self.sup._step()
        kinds = [kind for kind, _ in self.drain()]
        self.assertEqual(kinds, ["video", "live_source"])


class TestSnapshotIsolation(SupervisorCase):
    def test_snapshot_returns_a_copy(self):
        snap = self.sup.snapshot()
        snap["reason"] = "mutated by a caller"
        self.assertNotEqual(self.sup.snapshot()["reason"], "mutated by a caller")

    def test_state_is_seeded_before_configure(self):
        # The instance exists at import time so the dashboard routes have something to talk to;
        # the seeded state is what stops the frontend claiming a live feed in that window.
        fresh = CameraSupervisor()
        self.assertEqual(fresh.snapshot()["reason"], "starting up")
        self.assertFalse(fresh.snapshot()["locked"])


if __name__ == "__main__":
    unittest.main()
