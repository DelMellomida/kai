"""The Flask layer: the settings routes and the /params contract.

face_track.py imports cleanly with no hardware — no camera, no serial, no mic; VoiceAssistant and
ConversationSession construct lazily — so the app can be exercised through Flask's test client without
extracting the routes into a separate module. The import does cost ~13 s (MediaPipe, onnxruntime).
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import face_track
import settings
from app import lifecycle


class _FakeCamThread:
    """CameraThread's whole surface as far as _publish_status is concerned."""

    def __init__(self, source_name):
        self.source_name = source_name


class _FakeServo:
    last_pan = last_tilt = last_jaw = 90


class WebCase(unittest.TestCase):
    """Both face_track and settings hold module-level state; every test starts from a clean slate and
    writes any settings file into its own temp dir."""

    def setUp(self):
        settings._reset_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = patch.object(settings, "PATH",
                                   str(Path(self._tmp.name) / "settings.json"))
        self._patch.start()
        self.client = face_track._flask_app.test_client()
        face_track._camera.set_state(reason="starting up", mode="auto", locked=False,
                                     next_probe_at=0.0)

    def tearDown(self):
        self._patch.stop()
        settings._reset_for_tests()
        self._tmp.cleanup()


class TestGetSettings(WebCase):
    def test_returns_values_defaults_and_specs(self):
        res = self.client.get('/settings')
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["values"], settings.snapshot())
        self.assertEqual(body["defaults"], settings.defaults())
        self.assertTrue(any(s["name"] == "tts_volume" for s in body["specs"]))
        self.assertEqual(body["persist_error"], "")

    def test_is_json_safe(self):
        json.dumps(self.client.get('/settings').get_json())


class TestPostSettings(WebCase):
    def test_applies_and_echoes_the_stored_values(self):
        res = self.client.post('/settings', json={"tts_volume": 1.25, "camera_mode": "off"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["values"], {"tts_volume": 1.25, "camera_mode": "off"})
        self.assertEqual(settings.get("tts_volume"), 1.25)

    def test_unknown_setting_is_rejected(self):
        res = self.client.post('/settings', json={"nope": 1})
        self.assertEqual(res.status_code, 400)
        self.assertIn("unknown setting", res.get_json()["error"])

    def test_out_of_range_is_rejected_and_nothing_is_applied(self):
        res = self.client.post('/settings', json={"jaw_enabled": False, "tts_volume": 99.0})
        self.assertEqual(res.status_code, 400)
        self.assertTrue(settings.get("jaw_enabled"),
                        "a rejected batch must leave every knob in the batch untouched")

    def test_bad_choice_names_the_options(self):
        res = self.client.post('/settings', json={"camera_mode": "on"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("auto", res.get_json()["error"])

    def test_non_object_body_is_rejected(self):
        self.assertEqual(self.client.post('/settings', json=["tts_volume", 1]).status_code, 400)
        self.assertEqual(self.client.post('/settings', json={}).status_code, 400)

    def test_camera_mode_locked_by_cli_is_refused_with_the_reason(self):
        # --no-camera declares this machine's hardware situation for the run; a browser must not be
        # able to re-enable hardware the operator disabled at launch.
        face_track._camera.set_state(locked=True)
        res = self.client.post('/settings', json={"camera_mode": "auto"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("--no-camera", res.get_json()["error"])
        self.assertIn("camera_mode", self.client.get('/settings').get_json()["locked"])

    def test_other_settings_still_work_while_camera_mode_is_locked(self):
        face_track._camera.set_state(locked=True)
        self.assertEqual(self.client.post('/settings', json={"tts_volume": 0.5}).status_code, 200)

    def test_a_save_failure_is_still_a_success_with_a_warning(self):
        # The change IS live, which is what the user asked for. A 400 would make the UI snap the
        # control back to a value that is no longer in effect.
        with patch("settings.open", side_effect=OSError("ENOSPC")):
            res = self.client.post('/settings', json={"tts_volume": 1.5})
        self.assertEqual(res.status_code, 200)
        self.assertIn("ENOSPC", res.get_json()["persist_error"])
        self.assertEqual(settings.get("tts_volume"), 1.5)


class TestResetSettings(WebCase):
    def test_restores_defaults(self):
        self.client.post('/settings', json={"tts_volume": 1.9})
        res = self.client.post('/settings/reset')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["values"], settings.defaults())
        self.assertEqual(settings.snapshot(), settings.defaults())


class TestCameraProbe(WebCase):
    def test_wakes_the_supervisor(self):
        cam = face_track._camera
        cam._probe_now.clear()
        self.assertEqual(self.client.post('/camera/probe').status_code, 200)
        self.assertTrue(cam.probe_pending())
        cam._probe_now.clear()


class TestRestart(WebCase):
    """POST /restart. lifecycle.schedule_restart is patched in every test here: unpatched it
    SIGTERMs the process running the tests four tenths of a second later."""

    def test_answers_before_it_tears_anything_down(self):
        with patch.object(lifecycle, "schedule_restart") as sched:
            res = self.client.post('/restart')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "ok")
        self.assertEqual(sched.call_count, 1)

    def test_reports_whether_anything_will_restart_us(self):
        with patch.object(lifecycle, "schedule_restart"):
            with patch.dict(lifecycle.os.environ, {"KAI_SUPERVISED": "1"}):
                self.assertTrue(self.client.post('/restart').get_json()["supervised"])
            with patch.dict(lifecycle.os.environ, {"KAI_SUPERVISED": "0"}):
                body = self.client.post('/restart').get_json()
        # Unsupervised, this is a shutdown, not a restart — the dashboard has to be able to say so.
        self.assertFalse(body["supervised"])
        self.assertIn("NOT come back", body["message"])

    def test_get_is_not_a_restart(self):
        # A crawler, a prefetch or a pasted URL must not be able to stop the robot.
        with patch.object(lifecycle, "schedule_restart") as sched:
            self.assertEqual(self.client.get('/restart').status_code, 405)
        sched.assert_not_called()

    def test_the_shutdown_signals_this_process_and_flags_the_exit_code(self):
        # arm_restart_deadline is patched for the same reason schedule_restart is: unpatched it
        # leaves a live timer that calls os._exit on the test runner twelve seconds later.
        lifecycle.restart_requested.clear()
        with patch.object(lifecycle.os, "kill") as kill, \
             patch.object(lifecycle, "arm_restart_deadline"):
            lifecycle.request_restart()
        self.assertTrue(lifecycle.restart_requested.is_set())
        self.assertEqual(kill.call_args[0][0], lifecycle.os.getpid())
        lifecycle.restart_requested.clear()

    def test_a_failed_signal_does_not_leave_the_exit_code_armed(self):
        # Otherwise the next ordinary Ctrl-C would exit 7 and the supervisor would restart a robot
        # somebody had just stopped on purpose.
        lifecycle.restart_requested.clear()
        with patch.object(lifecycle.os, "kill", side_effect=OSError("EPERM")), \
             patch.object(lifecycle, "arm_restart_deadline") as armed:
            lifecycle.request_restart()
        self.assertFalse(lifecycle.restart_requested.is_set())
        # And no force-exit timer either: the process is going to keep running, so a deadline that
        # fired would take down a robot that was never actually restarting.
        armed.assert_not_called()

    def test_a_wedged_shutdown_is_forced_out_rather_than_hanging_forever(self):
        """The 2026-08-09 failure: POST /restart answered ok and the process never exited.

        It was wedged in mic resolution, so run()'s `finally` never completed and the graceful path
        never finished. From the dashboard that is indistinguishable from a restart that worked.
        """
        with patch.object(lifecycle.os, "kill"), \
             patch.object(lifecycle.threading, "Timer") as timer:
            lifecycle.request_restart()
        lifecycle.restart_requested.clear()
        delay, fn = timer.call_args[0][0], timer.call_args[0][1]
        self.assertEqual(delay, lifecycle.RESTART_FORCE_AFTER_S)
        with patch.object(lifecycle.os, "_exit") as hard_exit:
            fn()
        # The same exit code as the graceful path, so the supervisor still treats it as a restart
        # and brings Kai back rather than reading it as a crash.
        hard_exit.assert_called_once_with(lifecycle.EXIT_RESTART)

    def test_supervised_reads_the_env_var(self):
        with patch.dict(lifecycle.os.environ, {}, clear=True):
            with patch.object(lifecycle, "open", side_effect=OSError, create=True):
                self.assertFalse(lifecycle.supervised())
        with patch.dict(lifecycle.os.environ, {"KAI_SUPERVISED": "1"}):
            self.assertTrue(lifecycle.supervised())

    def test_supervised_falls_back_to_the_parent_process(self):
        # A supervisor loop that predates this file's update exports nothing, and calling that robot
        # unsupervised would put a red "it will NOT come back" warning on a robot that will.
        from unittest.mock import mock_open
        with patch.dict(lifecycle.os.environ, {}, clear=True):
            with patch.object(lifecycle, "open",
                              mock_open(read_data=b"bash\0/home/x/scripts/autostart.sh\0"),
                              create=True):
                self.assertTrue(lifecycle.supervised())

    def test_settings_get_carries_the_supervised_flag(self):
        # The Settings panel seeds from here, so an unsupervised robot can warn before the click.
        self.assertIn("supervised", self.client.get('/settings').get_json())


class TestParamsSnapshot(WebCase):
    """The /params generator never terminates, so the snapshot is the testable seam."""

    def test_is_json_safe(self):
        json.dumps(face_track._params_snapshot())

    def test_reports_the_camera_even_with_no_frames(self):
        # THE regression test. _publish_web only runs when a frame arrives, so on a robot with no camera
        # nothing published camera state at all and the dashboard fell back to showing "live" — claiming
        # a camera that does not exist. _publish_status runs on every loop iteration instead.
        with face_track._web_lock:
            face_track._web_params = {}
        face_track._camera.set_state(reason="no /dev/video* device and no --network host")
        face_track._publish_status(_FakeCamThread("none"), _FakeServo(), 0.0)

        snap = face_track._params_snapshot()
        self.assertEqual(snap["cam_source"], "none")
        self.assertIn("/dev/video*", snap["cam_reason"])
        self.assertEqual(snap["pan"], 90, "servo angles are frame-independent and must keep flowing")
        self.assertNotIn("face_visible", snap, "no frame means no face data, not stale face data")

    def test_a_live_camera_reports_no_reason(self):
        face_track._camera.set_state(reason="")
        face_track._publish_status(_FakeCamThread("csi"), _FakeServo(), 0.0)
        snap = face_track._params_snapshot()
        self.assertEqual(snap["cam_source"], "csi")
        self.assertEqual(snap["cam_reason"], "")

    def test_stale_frame_data_is_dropped(self):
        # Otherwise the dashboard would keep showing the last face and fps it ever saw after the camera
        # went away. Cleared, the frontend's `?? 0` fallbacks read as "no face, 0 fps".
        with face_track._web_lock:
            face_track._web_params = {"face_visible": 1, "fps": 25}
            face_track._web_frame_t = 0.0        # no frame has ever been published
        face_track._publish_status(_FakeCamThread("none"), _FakeServo(), 0.0)
        self.assertNotIn("face_visible", face_track._params_snapshot())

    def test_frame_data_overlays_status_without_colliding(self):
        # The two halves must not fight: no key is written by both publishers.
        with face_track._web_lock:
            face_track._web_status = {"cam_source": "none", "pan": 90}
            face_track._web_params = {"face_visible": 1, "fps": 25}
        snap = face_track._params_snapshot()
        self.assertEqual(snap["cam_source"], "none")
        self.assertEqual(snap["pan"], 90)
        self.assertEqual(snap["fps"], 25)

    def test_carries_the_settings_echo_namespaced(self):
        # set_* prefixed so a knob can never collide with telemetry — `jaw` is already a servo angle.
        settings.set_many({"tts_volume": 1.1})
        with face_track._web_lock:
            face_track._web_status = {f"set_{k}": v for k, v in settings.snapshot().items()}
        snap = face_track._params_snapshot()
        self.assertEqual(snap["set_tts_volume"], 1.1)
        self.assertNotIn("tts_volume", snap)

    def test_publishes_whether_the_reboot_control_is_configured(self):
        # The dashboard leaves the button out entirely when this is False, so it has to be on every
        # snapshot rather than only when enabled.
        self.assertIn("reboot_enabled", face_track._params_snapshot())


class TestAudioReresolve(WebCase):
    """POST /audio/reresolve — the cheap rung of the recovery ladder."""

    def test_delegates_to_the_session_and_returns_what_it_reports(self):
        payload = {"status": "ok", "ok": True, "restarted_session": False,
                   "device": 5, "rate": 48000, "is_i2s": True, "live": True, "error": ""}
        with patch.object(face_track._session, "reresolve_mic", return_value=payload) as rr:
            body = self.client.post('/audio/reresolve').get_json()
        rr.assert_called_once_with()
        self.assertEqual(body, payload)

    def test_a_failure_is_reported_with_a_reason_not_swallowed(self):
        # "It didn't work" is useless on a robot that is deaf. The reason is the whole value of the
        # reply — it is what tells the operator whether to escalate to a restart.
        payload = {"status": "error", "ok": False, "restarted_session": False,
                   "device": None, "rate": 16000, "is_i2s": False, "live": False,
                   "error": "cannot resample 44100 Hz"}
        with patch.object(face_track._session, "reresolve_mic", return_value=payload):
            body = self.client.post('/audio/reresolve').get_json()
        self.assertFalse(body["ok"])
        self.assertIn("44100", body["error"])

    def test_get_does_not_touch_the_microphone(self):
        with patch.object(face_track._session, "reresolve_mic") as rr:
            self.assertEqual(self.client.get('/audio/reresolve').status_code, 405)
        rr.assert_not_called()


class TestSystemReboot(WebCase):
    """POST /system/reboot. lifecycle.reboot_now is patched throughout — unpatched it would reboot
    the machine running the tests, which is a uniquely bad property for a test suite to have."""

    def test_disabled_by_default_and_says_how_to_enable_it(self):
        with patch.object(face_track, "REBOOT_ENABLED", False), \
             patch.object(lifecycle, "reboot_now") as rb:
            res = self.client.post('/system/reboot', json={"confirm": "reboot"})
        self.assertEqual(res.status_code, 403)
        self.assertIn("REBOOT_ENABLED", res.get_json()["error"])
        rb.assert_not_called()          # the sudo path is never even reached

    def test_without_the_confirmation_token_nothing_happens(self):
        # The dashboard's two taps protect against a slip. This protects against everything else
        # that can POST to an endpoint on a service with no authentication at all.
        with patch.object(face_track, "REBOOT_ENABLED", True), \
             patch.object(lifecycle, "reboot_now") as rb:
            for body in ({}, {"confirm": "yes"}, {"confirm": ""}):
                res = self.client.post('/system/reboot', json=body)
                self.assertEqual(res.status_code, 400, body)
            self.assertEqual(self.client.post('/system/reboot').status_code, 400)   # no body at all
        rb.assert_not_called()

    def test_reboots_when_enabled_and_confirmed(self):
        with patch.object(face_track, "REBOOT_ENABLED", True), \
             patch.object(lifecycle, "reboot_now", return_value=(True, "")) as rb:
            res = self.client.post('/system/reboot', json={"confirm": "reboot"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "ok")
        rb.assert_called_once_with()

    def test_a_sudo_misconfiguration_is_surfaced_not_reported_as_success(self):
        # The failure being guarded against: a button that answers "ok" and does nothing, leaving
        # the operator watching a robot that is never coming back. That is exactly what made the
        # wedged restart so misleading, and it must not be rebuilt here.
        with patch.object(face_track, "REBOOT_ENABLED", True), \
             patch.object(lifecycle, "reboot_now",
                          return_value=(False, "this user may not run /usr/bin/systemctl reboot")):
            res = self.client.post('/system/reboot', json={"confirm": "reboot"})
        self.assertEqual(res.status_code, 500)
        self.assertIn("may not run", res.get_json()["error"])

    def test_get_is_not_a_reboot(self):
        with patch.object(face_track, "REBOOT_ENABLED", True), \
             patch.object(lifecycle, "reboot_now") as rb:
            self.assertEqual(self.client.get('/system/reboot').status_code, 405)
        rb.assert_not_called()

    def test_the_permission_probe_never_runs_the_reboot_command_itself(self):
        """`sudo -l <cmd>` asks, it does not do.

        A rule scoped to exactly `/usr/bin/systemctl reboot` does not match that command plus
        `--help`, so probing by dry-running would report a correctly-configured robot as broken.
        """
        with patch.object(lifecycle.subprocess, "run") as run:
            run.return_value = type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            ok, detail = lifecycle.reboot_now()
        self.assertFalse(ok)
        self.assertIn("-l", run.call_args[0][0])
        self.assertIn("sudoers", detail)


if __name__ == "__main__":
    unittest.main()
