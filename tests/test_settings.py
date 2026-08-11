import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import settings
from config.servo import JAW_SEND_INTERVAL
from config.thinking import THINKING_SOUNDS, THINKING_SWEEP
from config.tracking import NO_FRAME_WAIT, WEB_PUBLISH_INTERVAL
from config.voice import (
    TTS_ENABLED, TTS_LENGTH_SCALE, TTS_NOISE_SCALE, TTS_NOISE_W, TTS_SENTENCE_SILENCE_S, TTS_VOLUME,
)
from config.wake import HANDS_FREE_ENABLED, VAD_RMS_FLOOR, WAKE_SENSITIVITIES


class SettingsTestCase(unittest.TestCase):
    """settings keeps module-level state and writes a file; every test gets a clean slate and its own
    temp dir, so nothing here ever touches the operator's real ~/.config/kai/settings.json."""

    def setUp(self):
        settings._reset_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "settings.json"
        self._patch = patch.object(settings, "PATH", str(self.path))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        settings._reset_for_tests()
        self._tmp.cleanup()

    def write(self, obj):
        """Put a raw overlay file in place, as a previous run (or a human editor) would have."""
        self.path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


class TestDefaults(SettingsTestCase):
    def test_defaults_come_from_config(self):
        # The guarantee that makes this safe to ship: delete settings.json and you are back to
        # exactly the committed config/*.py behaviour. If someone re-types a default as a literal in
        # settings.py, this is the test that catches the drift.
        self.assertEqual(settings.get("hands_free"), HANDS_FREE_ENABLED)
        self.assertEqual(settings.get("tts_enabled"), TTS_ENABLED)
        self.assertEqual(settings.get("tts_volume"), TTS_VOLUME)
        self.assertEqual(settings.get("tts_length_scale"), TTS_LENGTH_SCALE)
        self.assertEqual(settings.get("tts_sentence_silence"), TTS_SENTENCE_SILENCE_S)
        self.assertEqual(settings.get("tts_noise_scale"), TTS_NOISE_SCALE)
        self.assertEqual(settings.get("tts_noise_w"), TTS_NOISE_W)
        self.assertEqual(settings.get("vad_rms_floor"), VAD_RMS_FLOOR)
        self.assertEqual(settings.get("wake_sensitivity"), WAKE_SENSITIVITIES[0])
        self.assertEqual(settings.get("thinking_sweep"), THINKING_SWEEP)
        self.assertEqual(settings.get("thinking_sounds"), THINKING_SOUNDS)

    def test_camera_mode_defaults_to_auto(self):
        # "auto" is the whole point: use a camera whenever one is available.
        self.assertEqual(settings.get("camera_mode"), "auto")

    def test_snapshot_is_json_safe(self):
        # /params serialises this every 50 ms; a non-scalar here would break the whole SSE stream.
        json.dumps(settings.snapshot())

    def test_describe_is_json_safe_and_carries_bounds(self):
        rows = {row["name"]: row for row in settings.describe()}
        self.assertEqual(rows["tts_volume"]["min"], 0.0)
        self.assertEqual(rows["tts_volume"]["max"], 2.0)
        self.assertEqual(rows["camera_mode"]["choices"], ["auto", "off"])
        json.dumps(settings.describe())


class TestLoad(SettingsTestCase):
    def test_missing_file_leaves_defaults(self):
        settings.load()
        self.assertEqual(settings.snapshot(), settings.defaults())

    def test_overlay_is_applied(self):
        self.write({"tts_volume": 1.5, "hands_free": False})
        settings.load()
        self.assertEqual(settings.get("tts_volume"), 1.5)
        self.assertFalse(settings.get("hands_free"))

    def test_unknown_key_is_ignored_and_others_still_apply(self):
        # A knob removed in a later version must not cost the operator the rest of their file.
        self.write({"tts_volume": 1.5, "gone_in_v2": 42})
        settings.load()
        self.assertEqual(settings.get("tts_volume"), 1.5)

    def test_out_of_range_is_clamped_not_rejected(self):
        # From the FILE we clamp: rejecting would mean one bad hand-edit reverts everything.
        self.write({"tts_volume": 99.0, "vad_rms_floor": -5})
        settings.load()
        self.assertEqual(settings.get("tts_volume"), 2.0)
        self.assertEqual(settings.get("vad_rms_floor"), 50.0)

    def test_wrong_type_falls_back_to_default(self):
        self.write({"tts_volume": "loud", "hands_free": "nonsense"})
        settings.load()
        self.assertEqual(settings.get("tts_volume"), TTS_VOLUME)
        self.assertEqual(settings.get("hands_free"), HANDS_FREE_ENABLED)

    def test_string_booleans_are_accepted(self):
        self.write({"hands_free": "false", "tts_enabled": "true"})
        settings.load()
        self.assertFalse(settings.get("hands_free"))
        self.assertTrue(settings.get("tts_enabled"))

    def test_corrupt_json_falls_back_and_quarantines(self):
        self.write("{not json")
        settings.load()
        self.assertEqual(settings.snapshot(), settings.defaults())
        self.assertTrue(Path(str(self.path) + ".bad").exists(),
                        "the bad file must be kept for inspection, not silently deleted")
        self.assertFalse(self.path.exists())

    def test_non_dict_json_falls_back(self):
        self.write([1, 2, 3])
        settings.load()
        self.assertEqual(settings.snapshot(), settings.defaults())

    def test_unresolvable_home_does_not_raise(self):
        # The @reboot cron environment has no HOME; expanduser() raises RuntimeError there. Settings
        # must never be the reason the robot fails to start.
        with patch.object(Path, "expanduser", side_effect=RuntimeError("no home directory")):
            settings.load()
        self.assertEqual(settings.snapshot(), settings.defaults())

    def test_unreadable_file_does_not_raise(self):
        self.write({"tts_volume": 1.5})
        with patch.object(Path, "read_text", side_effect=OSError("EIO")):
            settings.load()
        self.assertEqual(settings.snapshot(), settings.defaults())

    def test_unreadable_file_is_not_quarantined(self):
        # A read error says nothing about the file's contents — this box has ext4 errors, so a
        # transient EIO is plausible. Moving a good file aside would lose the overlay for nothing.
        self.write({"tts_volume": 1.5})
        with patch.object(Path, "read_text", side_effect=OSError("EIO")):
            settings.load()
        self.assertTrue(self.path.exists(), "a transient read error must not destroy the overlay")
        self.assertFalse(Path(str(self.path) + ".bad").exists())

    def test_load_does_not_rewrite_the_file(self):
        # Rewriting on load would drop the unknown keys we deliberately preserve, and would churn the
        # SD card on every boot.
        self.write({"tts_volume": 1.5, "gone_in_v2": 42})
        settings.load()
        self.assertIn("gone_in_v2", json.loads(self.path.read_text()))


class TestSetMany(SettingsTestCase):
    def test_applies_and_returns_stored_values(self):
        out = settings.set_many({"tts_volume": 1.25, "hands_free": False})
        self.assertEqual(out, {"tts_volume": 1.25, "hands_free": False})
        self.assertEqual(settings.get("tts_volume"), 1.25)

    def test_unknown_key_raises(self):
        with self.assertRaises(ValueError) as ctx:
            settings.set_many({"nope": 1})
        self.assertIn("unknown setting", str(ctx.exception))

    def test_out_of_range_raises_from_a_route(self):
        # From a ROUTE we reject, so the dashboard can say which control was wrong instead of
        # silently storing something the user did not ask for.
        with self.assertRaises(ValueError) as ctx:
            settings.set_many({"tts_volume": 5.0})
        self.assertIn("outside", str(ctx.exception))

    def test_bad_choice_raises_and_names_the_options(self):
        with self.assertRaises(ValueError) as ctx:
            settings.set_many({"camera_mode": "on"})
        self.assertIn("auto", str(ctx.exception))

    def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            settings.set_many(["tts_volume", 1.0])

    def test_is_all_or_nothing(self):
        # A half-applied batch would leave two knobs disagreeing. Nothing may change, and nothing may
        # be written, if any key in the batch is bad.
        settings.load()
        with self.assertRaises(ValueError):
            settings.set_many({"hands_free": False, "tts_volume": 99.0})
        self.assertEqual(settings.get("hands_free"), HANDS_FREE_ENABLED)
        self.assertFalse(self.path.exists(), "a rejected batch must not touch the file")

    def test_nan_and_inf_are_rejected(self):
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                settings.set_many({"tts_volume": bad})

    def test_set_one_returns_the_stored_value(self):
        self.assertEqual(settings.set_one("tts_volume", 0.5), 0.5)


class TestPersistence(SettingsTestCase):
    def test_writes_only_non_default_values(self):
        # So that changing a config/*.py default later actually propagates, instead of being frozen
        # forever by an overlay that pinned every knob at its old default.
        settings.set_many({"tts_volume": 1.5})
        body = json.loads(self.path.read_text())
        self.assertEqual(body, {"tts_volume": 1.5})

    def test_value_returned_to_default_is_dropped_from_the_file(self):
        settings.set_many({"tts_volume": 1.5})
        settings.set_many({"tts_volume": TTS_VOLUME})
        self.assertEqual(json.loads(self.path.read_text()), {})

    def test_creates_the_parent_directory(self):
        nested = Path(self._tmp.name) / "deep" / "kai" / "settings.json"
        with patch.object(settings, "PATH", str(nested)):
            settings.set_many({"tts_volume": 1.5})
        self.assertTrue(nested.exists())

    def test_write_is_atomic(self):
        # A torn settings.json would be quarantined on the next boot, losing every knob. The temp
        # sibling + os.replace is what prevents that; assert the mechanism, not just the result.
        seen = {}
        real_replace = os.replace

        def spy(src, dst):
            seen["src"], seen["dst"] = str(src), str(dst)
            return real_replace(src, dst)

        with patch("settings.os.replace", side_effect=spy):
            settings.set_many({"tts_volume": 1.5})
        self.assertTrue(seen["src"].endswith(".tmp"))
        self.assertEqual(seen["dst"], str(self.path))

    def test_fsync_is_called(self):
        with patch("settings.os.fsync") as fsync:
            settings.set_many({"tts_volume": 1.5})
        fsync.assert_called()

    def test_write_failure_keeps_the_value_live_and_records_why(self):
        # The operator asked for this change; refusing it because the disk is full would be worse
        # than applying it and saying it will not survive a restart.
        with patch("settings.open", side_effect=OSError("ENOSPC")):
            settings.set_many({"tts_volume": 1.5})
        self.assertEqual(settings.get("tts_volume"), 1.5)
        self.assertIn("ENOSPC", settings.persist_error())

    def test_persist_error_clears_on_the_next_good_write(self):
        with patch("settings.open", side_effect=OSError("ENOSPC")):
            settings.set_many({"tts_volume": 1.5})
        settings.set_many({"tts_volume": 1.75})
        self.assertEqual(settings.persist_error(), "")

    def test_no_write_when_nothing_changed(self):
        settings.set_many({"tts_volume": TTS_VOLUME})
        self.assertFalse(self.path.exists())

    def test_survives_a_reload(self):
        settings.set_many({"tts_volume": 1.5, "camera_mode": "off"})
        settings._reset_for_tests()
        settings.load()
        self.assertEqual(settings.get("tts_volume"), 1.5)
        self.assertEqual(settings.get("camera_mode"), "off")


class TestReset(SettingsTestCase):
    def test_restores_defaults_and_removes_the_file(self):
        settings.set_many({"tts_volume": 1.5, "hands_free": False})
        out = settings.reset()
        self.assertEqual(out, settings.defaults())
        self.assertEqual(settings.snapshot(), settings.defaults())
        self.assertFalse(self.path.exists())

    def test_fires_callbacks_for_what_changed(self):
        seen = []
        settings.on_change("tts_volume", seen.append)
        settings.set_many({"tts_volume": 1.5})
        settings.reset()
        self.assertEqual(seen, [1.5, TTS_VOLUME])

    def test_is_safe_with_no_file(self):
        settings.reset()
        self.assertEqual(settings.snapshot(), settings.defaults())


class TestCallbacks(SettingsTestCase):
    def test_fires_on_change_with_the_new_value(self):
        seen = []
        settings.on_change("tts_volume", seen.append)
        settings.set_many({"tts_volume": 1.5})
        self.assertEqual(seen, [1.5])

    def test_does_not_fire_when_the_value_is_unchanged(self):
        # Otherwise re-POSTing the same value would reopen the wake engine or re-run Piper for
        # nothing — and the dashboard does re-POST.
        seen = []
        settings.on_change("tts_volume", seen.append)
        settings.set_many({"tts_volume": TTS_VOLUME})
        self.assertEqual(seen, [])

    def test_only_the_changed_key_fires(self):
        seen = []
        settings.on_change("hands_free", lambda v: seen.append(("hands_free", v)))
        settings.on_change("tts_volume", lambda v: seen.append(("tts_volume", v)))
        settings.set_many({"tts_volume": 1.5})
        self.assertEqual(seen, [("tts_volume", 1.5)])

    def test_a_raising_callback_does_not_abort_the_batch_or_the_persist(self):
        seen = []
        settings.on_change("tts_volume", lambda v: (_ for _ in ()).throw(RuntimeError("boom")))
        settings.on_change("tts_volume", seen.append)
        settings.set_many({"tts_volume": 1.5})
        self.assertEqual(seen, [1.5], "a later subscriber must still be notified")
        self.assertEqual(json.loads(self.path.read_text()), {"tts_volume": 1.5})

    def test_unknown_key_cannot_be_subscribed(self):
        with self.assertRaises(ValueError):
            settings.on_change("nope", lambda v: None)

    def test_debounce_collapses_a_burst_into_one_call(self):
        # A dragged slider POSTs repeatedly; re-synthesising the canned wake replies each time would
        # run Piper dozens of times.
        seen = []
        settings.on_change("tts_volume", seen.append, debounce=0.05)
        for v in (1.1, 1.2, 1.3, 1.4):
            settings.set_many({"tts_volume": v})
        settings._wait_for_debounced()
        self.assertEqual(seen, [1.4], "only the final value should reach an expensive subscriber")


class TestConcurrency(SettingsTestCase):
    def test_readers_never_see_a_torn_value(self):
        # Flask request threads write while the tracking loop, control loop and audio worker read.
        wanted = {0.5, 1.5}
        seen, stop = [], threading.Event()

        def reader():
            while not stop.is_set():
                seen.append(settings.get("tts_volume"))

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        try:
            for _ in range(50):
                for v in sorted(wanted):
                    settings.set_many({"tts_volume": v}, persist=False)
        finally:
            stop.set()
            for t in threads:
                t.join(2.0)

        self.assertTrue(seen)
        self.assertLessEqual(set(seen), wanted | {TTS_VOLUME},
                             "every observed value must be one that was actually written")


class TestIdleWaitBounds(unittest.TestCase):
    """`NO_FRAME_WAIT` against the two cadences it must not slow down (R2).

    With no camera, this value IS the main loop's period, and the loop is the only thing driving the
    jaw and `_publish_status` on that path. Set it above either obligation and the work does not stop
    — it just quietly happens less often, which no other test would notice and no one would see on a
    dashboard until a countdown looked wrong.

    The two constants live in different config modules, and those modules deliberately import
    nothing (they are flat constant files), so the relationship cannot be expressed as an assignment.
    This is where it gets expressed instead."""

    def test_does_not_slow_the_status_publisher(self):
        self.assertLessEqual(NO_FRAME_WAIT, WEB_PUBLISH_INTERVAL)

    def test_does_not_slow_the_jaw(self):
        # R2 suggested pinning the wait to JAW_SEND_INTERVAL (0.05). That is the looser of the two
        # and would have dropped /params from 25 Hz to 20 Hz — forbidden by the ticket's own third
        # acceptance criterion. This assertion is what makes choosing the wrong one fail loudly.
        self.assertLessEqual(NO_FRAME_WAIT, JAW_SEND_INTERVAL)

    def test_is_bounded_well_under_a_second(self):
        # Shutdown latency: the loop notices SIGTERM/KeyboardInterrupt one wait at a time.
        self.assertGreater(NO_FRAME_WAIT, 0.0)
        self.assertLess(NO_FRAME_WAIT, 0.5)


if __name__ == "__main__":
    unittest.main()
