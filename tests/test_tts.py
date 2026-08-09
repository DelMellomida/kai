import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ai import tts


def _fake_proc(returncode=0, stderr=b"", alive=False):
    """A stand-in for a Popen: communicate() returns immediately, poll() reports alive/dead."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.returncode = returncode
    proc.communicate.return_value = (b"", stderr)
    proc.poll.return_value = None if alive else returncode
    return proc


class _ResetProcState(unittest.TestCase):
    """tts holds module-level subprocess handles; every test starts from a clean slate."""

    def setUp(self):
        tts._synth_proc = None
        tts._current_proc = None
        tts._last_end = 0.0
        # Pretend the output card profile has already been asserted. These tests patch Popen but not
        # subprocess.run, so a False here would let play()'s lazy assert shell out to a real pactl.
        # TestOutputCardProfile clears it deliberately, with subprocess.run patched.
        tts._profile_applied = True

    tearDown = setUp


class TestCleanForSpeech(unittest.TestCase):
    def test_strips_emoji(self):
        self.assertEqual(tts.clean_for_speech("hello 😀 there"), "hello there")

    def test_keeps_smart_punctuation(self):
        # General Punctuation is deliberately excluded from the strip ranges
        self.assertEqual(tts.clean_for_speech("it's — really"), "it's — really")

    def test_emoji_only_returns_empty(self):
        self.assertEqual(tts.clean_for_speech("😀🚀"), "")

    def test_none_returns_empty(self):
        self.assertEqual(tts.clean_for_speech(None), "")

    def test_strips_markdown_emphasis(self):
        # Piper voices these as words: "**world**" measured 1.90s longer than "world".
        self.assertEqual(tts.clean_for_speech("hello **bold** and *italic*"),
                         "hello bold and italic")

    def test_strips_backticks(self):
        self.assertEqual(tts.clean_for_speech("say `hello` now"), "say hello now")

    def test_strips_wrapped_aside(self):
        # The shape gemma actually produced on a live turn.
        self.assertEqual(tts.clean_for_speech("* I don't have the weather. *"),
                         "I don't have the weather.")

    def test_strips_leading_list_and_heading_markers(self):
        self.assertEqual(tts.clean_for_speech("- one\n- two"), "one two")
        self.assertEqual(tts.clean_for_speech("## Heading"), "Heading")

    def test_keeps_hyphenated_words_and_mid_sentence_dashes(self):
        # Only line-leading markers go — a hyphen inside a word is speakable text.
        self.assertEqual(tts.clean_for_speech("push-to-talk still works"),
                         "push-to-talk still works")
        self.assertEqual(tts.clean_for_speech("wait - what?"), "wait - what?")

    def test_keeps_underscores(self):
        # Measured silent through Piper, so there is nothing to buy by removing them.
        self.assertEqual(tts.clean_for_speech("file_name here"), "file_name here")

    def test_markdown_only_returns_empty(self):
        self.assertEqual(tts.clean_for_speech("**"), "")


class TestClampForSpeech(unittest.TestCase):
    """Kai can't hear while talking, so an over-long reply is an over-long deaf spell."""

    def test_short_text_untouched(self):
        self.assertEqual(tts.clamp_for_speech("Hello there.", 400), "Hello there.")

    def test_zero_max_disables_clamping(self):
        long = "x" * 1000
        self.assertEqual(tts.clamp_for_speech(long, 0), long)

    def test_prefers_a_sentence_boundary(self):
        text = "First sentence here. Second sentence here. Third one here."
        out = tts.clamp_for_speech(text, 45)
        self.assertEqual(out, "First sentence here. Second sentence here.")

    def test_falls_back_to_a_word_boundary(self):
        # One long sentence: no usable break, so don't cut mid-word.
        text = "alpha bravo charlie delta echo foxtrot golf hotel india"
        out = tts.clamp_for_speech(text, 20)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 21)
        self.assertNotIn("brav…", out)

    def test_ignores_a_boundary_that_would_lose_most_of_the_reply(self):
        text = "Hi. " + "a long continuation without any sentence break at all here"
        out = tts.clamp_for_speech(text, 40)
        self.assertNotEqual(out, "Hi.", "a 4-char sentence must not swallow a 40-char budget")

    def test_never_exceeds_the_budget_by_more_than_the_ellipsis(self):
        for n in (5, 10, 50, 200):
            self.assertLessEqual(len(tts.clamp_for_speech("word " * 200, n)), n + 1)

    def test_none_is_empty(self):
        self.assertEqual(tts.clamp_for_speech(None, 400), "")


class TestLiveSettings(_ResetProcState):
    """TTS reads its three dashboard knobs at the point of use, so a change applies to the very next
    thing Kai says — no restart, no engine reload."""

    def test_enabled_follows_the_setting(self):
        with patch("ai.tts.voice_model_path", return_value=Path(__file__)):
            with patch("ai.tts.settings.get", return_value=False):
                self.assertFalse(tts.enabled())
            with patch("ai.tts.settings.get", return_value=True):
                self.assertTrue(tts.enabled())

    def test_piper_gets_the_live_rate(self):
        proc = _fake_proc()
        live = {"tts_volume": 1.75, "tts_length_scale": 0.8, "tts_enabled": True}
        with patch("ai.tts.voice_model_path", return_value=Path(__file__)), \
             patch("ai.tts.settings.get", side_effect=lambda name: live[name]), \
             patch("subprocess.Popen", return_value=proc) as popen, \
             patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.stat", return_value=MagicMock(st_size=1024)):
            tts._run_piper("hi", Path("/tmp/x.wav"))
        argv = popen.call_args[0][0]
        self.assertEqual(argv[argv.index("--length-scale") + 1], "0.8")

    def test_synthesis_does_not_apply_volume(self):
        # Piper's --volume is normalised straight back out by TTS_POST_EFFECTS (`gain -n -1`), and
        # values above ~1.2 clip the raw audio. Volume belongs at playback — see play().
        proc = _fake_proc()
        live = {"tts_volume": 1.75, "tts_length_scale": 1.0, "tts_enabled": True}
        with patch("ai.tts.voice_model_path", return_value=Path(__file__)), \
             patch("ai.tts.settings.get", side_effect=lambda name: live[name]), \
             patch("subprocess.Popen", return_value=proc) as popen, \
             patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.stat", return_value=MagicMock(st_size=1024)):
            tts._run_piper("hi", Path("/tmp/x.wav"))
        self.assertNotIn("--volume", popen.call_args[0][0])

    @staticmethod
    def _paplay_argv(popen):
        """The paplay invocations only.

        Deliberately not popen.call_args (the LAST call): the suite runs daemon threads that also
        shell out (session ack prewarm, TTS workers), so under load one of those can land inside this
        patch and make the last call somebody else's.
        """
        return [c.args[0] for c in popen.call_args_list
                if c.args and c.args[0] and c.args[0][0] == "paplay"]

    def test_playback_applies_the_live_volume_to_the_sink_input(self):
        proc = _fake_proc()
        proc.returncode = 0
        with patch("ai.tts.settings.get", return_value=1.5), \
             patch("subprocess.Popen", return_value=proc) as popen:
            tts.play(Path("/tmp/x.wav"))
        calls = self._paplay_argv(popen)
        self.assertTrue(calls, "play() must invoke paplay")
        # PA_VOLUME_NORM is 65536, so 1.5 -> 98304.
        self.assertIn("--volume=98304", calls[0])

    def test_playback_volume_is_never_negative(self):
        proc = _fake_proc()
        proc.returncode = 0
        with patch("ai.tts.settings.get", return_value=-3.0), \
             patch("subprocess.Popen", return_value=proc) as popen:
            tts.play(Path("/tmp/x.wav"))
        calls = self._paplay_argv(popen)
        self.assertTrue(calls, "play() must invoke paplay")
        self.assertIn("--volume=0", calls[0])


class TestOutputCardProfile(_ResetProcState):
    """PulseAudio flips this dongle to its digital (S/PDIF) profile on its own, which deletes
    TTS_SINK and makes every reply inaudible without raising anything. apply_output_profile() is the
    counter-measure, so it has to be both effective and incapable of costing us a reply itself."""

    @staticmethod
    def _pactl_argv(run):
        return [c.args[0] for c in run.call_args_list
                if c.args and c.args[0] and c.args[0][0] == "pactl"]

    def test_asserts_the_configured_card_and_profile(self):
        with patch("ai.tts.subprocess.run") as run:
            self.assertTrue(tts.apply_output_profile())
        argv = self._pactl_argv(run)
        self.assertEqual(argv, [["pactl", "set-card-profile", tts.TTS_CARD, tts.TTS_CARD_PROFILE]])
        # pactl needs XDG_RUNTIME_DIR to find the Pulse socket under the @reboot cron autostart.
        self.assertEqual(run.call_args.kwargs["env"]["XDG_RUNTIME_DIR"], tts.TTS_XDG_RUNTIME)
        self.assertIsNotNone(run.call_args.kwargs["timeout"], "must not be able to wedge the worker")

    def test_profile_keeps_the_input_half(self):
        # This one card carries the dongle's mic too: the bare "output:analog-stereo" profile fixes
        # playback by dropping that capture source. Guard against someone "simplifying" the string.
        self.assertIn("+input:", tts.TTS_CARD_PROFILE)
        self.assertTrue(tts.TTS_CARD_PROFILE.startswith("output:analog-stereo"))

    def test_disabled_toggle_skips_pactl(self):
        with patch("ai.tts.TTS_ASSERT_CARD_PROFILE", False), \
             patch("ai.tts.subprocess.run") as run:
            self.assertFalse(tts.apply_output_profile())
        run.assert_not_called()

    def test_missing_pactl_returns_false_without_raising(self):
        # Runs on a daemon speak worker with no error handling above it — must never escape.
        with patch("ai.tts.subprocess.run", side_effect=FileNotFoundError("no pactl")), \
             patch("builtins.print") as mock_print:
            self.assertFalse(tts.apply_output_profile())
        mock_print.assert_called_once()

    def test_pactl_failure_returns_false_without_raising(self):
        for exc in (subprocess.CalledProcessError(1, "pactl"),
                    subprocess.TimeoutExpired("pactl", 5.0)):
            with self.subTest(exc=type(exc).__name__):
                with patch("ai.tts.subprocess.run", side_effect=exc), \
                     patch("builtins.print"):
                    self.assertFalse(tts.apply_output_profile())

    def test_play_asserts_the_profile_once_per_process_not_per_reply(self):
        tts._profile_applied = False
        proc = _fake_proc(returncode=0)
        with patch("ai.tts.settings.get", return_value=1.0), \
             patch("ai.tts.subprocess.run") as run, \
             patch("subprocess.Popen", return_value=proc):
            tts.play(Path("/tmp/x.wav"))
            tts.play(Path("/tmp/x.wav"))
        self.assertEqual(len(self._pactl_argv(run)), 1,
                         "the profile assert is a per-process cost, not a per-reply one")

    def test_a_failed_playback_reasserts_the_profile_before_retrying(self):
        # The whole point of the retry: an identical retry fails identically when the cause is the
        # card having flipped and taken TTS_SINK with it.
        proc = _fake_proc(returncode=1)
        # `stderr` is a Popen *instance* attribute, so spec=Popen doesn't expose it — assign it.
        proc.stderr = MagicMock()
        proc.stderr.read.return_value = b"Failure: No such entity"
        with patch("ai.tts.settings.get", return_value=1.0), \
             patch("ai.tts.subprocess.run") as run, \
             patch("ai.tts.time.sleep"), \
             patch("subprocess.Popen", return_value=proc), \
             patch("builtins.print"):
            tts.play(Path("/tmp/x.wav"))
        self.assertEqual(len(self._pactl_argv(run)), 1,
                         "retry must re-assert the profile, not just wait and hope")

    def test_successful_playback_does_not_reassert(self):
        proc = _fake_proc(returncode=0)
        with patch("ai.tts.settings.get", return_value=1.0), \
             patch("ai.tts.subprocess.run") as run, \
             patch("subprocess.Popen", return_value=proc):
            tts.play(Path("/tmp/x.wav"))
        self.assertEqual(self._pactl_argv(run), [], "nothing to fix on the happy path")


class TestRunPiper(_ResetProcState):
    def test_publishes_handle_so_stop_can_cancel_it(self):
        seen = []
        proc = _fake_proc()
        proc.communicate.side_effect = lambda **kw: (seen.append(tts._synth_proc), (b"", b""))[1]
        with patch("ai.tts.voice_model_path", return_value=Path(__file__)), \
             patch("subprocess.Popen", return_value=proc), \
             patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.stat", return_value=MagicMock(st_size=1024)):
            self.assertTrue(tts._run_piper("hi", Path("/tmp/x.wav")))
        self.assertIs(seen[0], proc, "synth handle must be visible to stop() while Piper runs")
        self.assertIsNone(tts._synth_proc, "handle must be cleared once the synth finishes")

    def test_communicate_failure_is_contained(self):
        # This runs on a daemon speak worker with no error handling above it, so an escaping
        # exception would kill the jaw animation silently.
        proc = _fake_proc()
        proc.communicate.side_effect = OSError("broken pipe")
        with patch("ai.tts.voice_model_path", return_value=Path(__file__)), \
             patch("subprocess.Popen", return_value=proc), \
             patch("builtins.print") as mock_print:
            self.assertFalse(tts._run_piper("hi", Path("/tmp/x.wav")))
        mock_print.assert_called_once()
        self.assertIsNone(tts._synth_proc, "handle must be cleared even on failure")

    def test_negative_returncode_is_silent_cancellation(self):
        # stop() terminates the synth -> negative returncode. That is a newer turn taking over,
        # not a failure, so it must not be logged as one.
        proc = _fake_proc(returncode=-15)
        with patch("ai.tts.voice_model_path", return_value=Path(__file__)), \
             patch("subprocess.Popen", return_value=proc), \
             patch("builtins.print") as mock_print:
            self.assertFalse(tts._run_piper("hi", Path("/tmp/x.wav")))
        mock_print.assert_not_called()

    def test_nonzero_returncode_logs_and_fails(self):
        proc = _fake_proc(returncode=1, stderr=b"piper: bad model")
        with patch("ai.tts.voice_model_path", return_value=Path(__file__)), \
             patch("subprocess.Popen", return_value=proc), \
             patch("builtins.print") as mock_print:
            self.assertFalse(tts._run_piper("hi", Path("/tmp/x.wav")))
        mock_print.assert_called_once()

    def test_missing_voice_model_fails_before_spawning(self):
        with patch("ai.tts.voice_model_path", return_value=Path("/nope/missing.onnx")), \
             patch("subprocess.Popen") as mock_popen, \
             patch("builtins.print"):
            self.assertFalse(tts._run_piper("hi", Path("/tmp/x.wav")))
        mock_popen.assert_not_called()


class TestSynthesizeTo(_ResetProcState):
    def test_returns_none_for_unspeakable_text(self):
        with patch("ai.tts._run_piper") as mock_run:
            self.assertIsNone(tts.synthesize_to("😀", Path("/tmp/kai_ack/a.wav")))
        mock_run.assert_not_called()

    def test_writes_to_its_own_path_not_the_shared_output(self):
        dest = Path("/tmp/kai_ack/ack.wav")
        with patch("ai.tts._run_piper", return_value=True) as mock_run, \
             patch("ai.tts._post_process", return_value=dest), \
             patch("pathlib.Path.mkdir"):
            self.assertEqual(tts.synthesize_to("Yes?", dest), dest)
        raw_used = mock_run.call_args[0][1]
        self.assertEqual(raw_used, Path("/tmp/kai_ack/ack_raw.wav"))
        # A cached line sharing _OUTPUT_WAV would be overwritten by the next reply.
        self.assertNotEqual(dest, tts._OUTPUT_WAV)
        self.assertNotEqual(raw_used, tts._RAW_WAV)

    def test_falls_back_to_raw_file_moved_onto_dest(self):
        dest = Path("/tmp/kai_ack/ack.wav")
        raw = Path("/tmp/kai_ack/ack_raw.wav")
        with patch("ai.tts._run_piper", return_value=True), \
             patch("ai.tts._post_process", return_value=raw), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.replace") as mock_replace:
            self.assertEqual(tts.synthesize_to("Yes?", dest), dest)
        mock_replace.assert_called_once_with(dest)

    def test_synth_failure_returns_none(self):
        with patch("ai.tts._run_piper", return_value=False), patch("pathlib.Path.mkdir"):
            self.assertIsNone(tts.synthesize_to("Yes?", Path("/tmp/kai_ack/ack.wav")))


class TestPrewarmCanned(_ResetProcState):
    def test_returns_empty_when_tts_disabled(self):
        with patch("ai.tts.enabled", return_value=False), \
             patch("ai.tts.synthesize_to") as mock_synth:
            self.assertEqual(tts.prewarm_canned({"ack": "Yes?"}, "/tmp/kai_ack"), {})
        mock_synth.assert_not_called()

    def test_maps_keys_to_distinct_paths(self):
        with patch("ai.tts.enabled", return_value=True), \
             patch("ai.tts.synthesize_to", side_effect=lambda t, d: d):
            out = tts.prewarm_canned({"ack": "Yes?", "err": "Oops."}, "/tmp/kai_ack")
        self.assertEqual(set(out), {"ack", "err"})
        self.assertEqual(len(set(out.values())), 2, "each canned line needs its own file")

    def test_one_failure_omits_only_that_key(self):
        def synth(text, dest):
            return None if "err" in str(dest) else dest

        with patch("ai.tts.enabled", return_value=True), \
             patch("ai.tts.synthesize_to", side_effect=synth), \
             patch("builtins.print"):
            out = tts.prewarm_canned({"ack": "Yes?", "err": "Oops."}, "/tmp/kai_ack")
        self.assertEqual(set(out), {"ack"})


class TestIsPlaying(_ResetProcState):
    def test_false_when_nothing_started(self):
        self.assertFalse(tts.is_playing())

    def test_true_while_process_alive(self):
        tts._current_proc = _fake_proc(alive=True)
        self.assertTrue(tts.is_playing())

    def test_false_once_process_exits(self):
        tts._current_proc = _fake_proc(alive=False)
        self.assertFalse(tts.is_playing())


class TestQuietSince(_ResetProcState):
    def test_infinite_before_anything_plays(self):
        self.assertEqual(tts.quiet_since(now=100.0), float("inf"))

    def test_zero_while_playing(self):
        tts._current_proc = _fake_proc(alive=True)
        tts._last_end = 50.0
        self.assertEqual(tts.quiet_since(now=100.0), 0.0)

    def test_elapsed_since_last_end(self):
        tts._last_end = 90.0
        self.assertAlmostEqual(tts.quiet_since(now=100.5), 10.5)

    def test_never_negative_on_clock_skew(self):
        tts._last_end = 200.0
        self.assertEqual(tts.quiet_since(now=100.0), 0.0)


class TestPlay(_ResetProcState):
    def test_stamps_last_end_so_the_quiet_tail_can_start(self):
        proc = _fake_proc()
        with patch("subprocess.Popen", return_value=proc), \
             patch("ai.tts.time.monotonic", return_value=123.0):
            tts.play(Path("/tmp/x.wav"))
        proc.wait.assert_called_once()
        self.assertEqual(tts._last_end, 123.0)
        self.assertIsNone(tts._current_proc)

    def test_spawn_failure_only_logs(self):
        with patch("subprocess.Popen", side_effect=OSError("no paplay")), \
             patch("builtins.print") as mock_print:
            tts.play(Path("/tmp/x.wav"))
        mock_print.assert_called_once()
        self.assertEqual(tts._last_end, 0.0)

    def test_does_not_clear_a_newer_playback_handle(self):
        # stop() + a new play() can land while this one is still in proc.wait().
        old, new = _fake_proc(), _fake_proc(alive=True)

        def wait():
            tts._current_proc = new

        old.wait.side_effect = wait
        with patch("subprocess.Popen", return_value=old):
            tts.play(Path("/tmp/x.wav"))
        self.assertIs(tts._current_proc, new)


class TestStop(_ResetProcState):
    def test_noop_when_nothing_running(self):
        tts.stop()   # must not raise
        self.assertIsNone(tts._current_proc)

    def test_terminates_playback_and_clears_handle(self):
        proc = _fake_proc(alive=True)
        tts._current_proc = proc
        tts.stop()
        proc.terminate.assert_called_once()
        self.assertIsNone(tts._current_proc)

    def test_terminates_an_in_flight_synth(self):
        # The bug this guards: a worker cancelled mid-Piper used to finish synthesizing and then
        # play its reply into whatever session came next.
        synth = _fake_proc(alive=True)
        tts._synth_proc = synth
        tts.stop()
        synth.terminate.assert_called_once()
        self.assertIsNone(tts._synth_proc)

    def test_terminates_both_stages_at_once(self):
        synth, play = _fake_proc(alive=True), _fake_proc(alive=True)
        tts._synth_proc, tts._current_proc = synth, play
        tts.stop()
        synth.terminate.assert_called_once()
        play.terminate.assert_called_once()

    def test_starts_the_quiet_tail_from_the_cut(self):
        tts._current_proc = _fake_proc(alive=True)
        with patch("ai.tts.time.monotonic", return_value=77.0):
            tts.stop()
        self.assertEqual(tts._last_end, 77.0)

    def test_cancelling_a_synth_alone_does_not_stamp_last_end(self):
        # No audio was in the air, so there is no quiet tail to wait out.
        tts._synth_proc = _fake_proc(alive=True)
        tts.stop()
        self.assertEqual(tts._last_end, 0.0)

    def test_skips_terminate_on_already_dead_process(self):
        proc = _fake_proc(alive=False)
        tts._current_proc = proc
        tts.stop()
        proc.terminate.assert_not_called()

    def test_survives_terminate_raising(self):
        proc = _fake_proc(alive=True)
        proc.terminate.side_effect = OSError("gone")
        tts._current_proc = proc
        tts.stop()   # must not propagate


if __name__ == "__main__":
    unittest.main()
