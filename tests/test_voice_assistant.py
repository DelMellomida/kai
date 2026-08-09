import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import requests

from ai import voice_assistant
from ai.tts import clean_for_speech
from ai.voice_assistant import (
    VoiceAssistant,
    MicChoice,
    WHISPER_BEAM_SIZE, WHISPER_INITIAL_PROMPT,
    apply_i2s_route,
    free_i2s_device,
    resume_pulse_source,
    build_chat_messages,
    latin_letter_ratio,
    load_persona,
    resolve_input_device,
    transcript_rejection,
    speaking_openness_at,
    _best_allowed_language,
    _candidate_input_devices,
    _capture_rates_for,
    _classify_device,
    _probe_is_live,
    _speak_segments,
    _speak_segments_for_duration,
    _split_sentences,
    _DEFAULT_PERSONA,
    MAX_HISTORY_TURNS,
    NO_SPEECH_RESPONSE,
    OLLAMA_MODEL,
    SPEAK_AMP,
    SPEAK_GAP_S,
    SPEAK_MAX_S,
    SPEAK_MIN_SENTENCE_S,
    SPEAK_SEC_PER_WORD,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_RECORDING,
)


def make_assistant() -> VoiceAssistant:
    return VoiceAssistant()


def make_segment(text: str):
    seg = MagicMock()
    seg.text = text
    return seg


class TestBuildChatMessages(unittest.TestCase):
    def test_system_prompt_first(self):
        msgs = build_chat_messages("sys", [], "hello")
        self.assertEqual(msgs[0], {"role": "system", "content": "sys"})

    def test_appends_user_turn_last(self):
        msgs = build_chat_messages("sys", [], "hello")
        self.assertEqual(msgs[-1], {"role": "user", "content": "hello"})

    def test_includes_history_in_order(self):
        history = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        msgs = build_chat_messages("sys", history, "c")
        self.assertEqual(msgs, [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ])

    def test_truncates_to_max_history_turns(self):
        history = []
        for i in range(MAX_HISTORY_TURNS + 5):
            history.append({"role": "user", "content": f"u{i}"})
            history.append({"role": "assistant", "content": f"a{i}"})
        msgs = build_chat_messages("sys", history, "new")
        # system + capped history + new user turn
        self.assertEqual(len(msgs), 1 + MAX_HISTORY_TURNS * 2 + 1)


class TestLoadPersona(unittest.TestCase):
    def test_reads_custom_content(self):
        mock_path = MagicMock()
        mock_path.read_text.return_value = "Custom persona text.\n"
        with patch("ai.voice_assistant.PERSONA_PATH", mock_path):
            self.assertEqual(load_persona(), "Custom persona text.")

    def test_missing_file_falls_back_to_default(self):
        mock_path = MagicMock()
        mock_path.read_text.side_effect = OSError("no such file")
        with patch("ai.voice_assistant.PERSONA_PATH", mock_path):
            self.assertEqual(load_persona(), _DEFAULT_PERSONA)

    def test_empty_file_falls_back_to_default(self):
        mock_path = MagicMock()
        mock_path.read_text.return_value = "   \n"
        with patch("ai.voice_assistant.PERSONA_PATH", mock_path):
            self.assertEqual(load_persona(), _DEFAULT_PERSONA)


class TestCandidateInputDevices(unittest.TestCase):
    def test_default_device_listed_first(self):
        devices = [
            {"name": "card0 (hw:0,0)", "max_input_channels": 2},
            {"name": "card1 (hw:1,0)", "max_input_channels": 2},
        ]
        with patch("ai.voice_assistant.sd.default") as mock_default:
            mock_default.device = [1, 1]
            candidates = _candidate_input_devices(devices)
        self.assertEqual(candidates[0], 1)

    def test_dedupes_duplicate_subdevices_of_same_card(self):
        devices = [
            {"name": "APE (hw:1,0)", "max_input_channels": 16},
            {"name": "APE (hw:1,1)", "max_input_channels": 16},
            {"name": "APE (hw:1,2)", "max_input_channels": 16},
            {"name": "USB Mic (hw:0,0)", "max_input_channels": 2},
        ]
        with patch("ai.voice_assistant.sd.default") as mock_default:
            mock_default.device = [-1, -1]
            candidates = _candidate_input_devices(devices)
        # only one representative for card 1, plus card 0 — not all 3 hw:1,* duplicates
        card_1_hits = [i for i in candidates if i in (0, 1, 2)]
        self.assertEqual(len(card_1_hits), 1)
        self.assertIn(3, candidates)

    def test_skips_output_only_devices(self):
        devices = [
            {"name": "HDMI out (hw:0,3)", "max_input_channels": 0},
            {"name": "Mic (hw:1,0)", "max_input_channels": 2},
        ]
        with patch("ai.voice_assistant.sd.default") as mock_default:
            mock_default.device = [-1, -1]
            candidates = _candidate_input_devices(devices)
        self.assertNotIn(0, candidates)
        self.assertIn(1, candidates)


class TestPulseSuspend(unittest.TestCase):
    def test_free_i2s_device_suspends_source(self):
        from ai.voice_assistant import I2S_PULSE_SOURCE
        with patch("ai.voice_assistant.I2S_SUSPEND_PULSE", True), \
             patch("ai.voice_assistant.PULSE_SUSPEND_ALL_SOURCES", False), \
             patch("ai.voice_assistant.subprocess.run") as mock_run:
            free_i2s_device()
        args, _ = mock_run.call_args
        self.assertEqual(args[0], ["pactl", "suspend-source", I2S_PULSE_SOURCE, "1"])

    def test_every_capture_source_is_released_not_just_i2s(self):
        # A source pulse holds makes that device's liveness probe time out, which reads as "not live" —
        # enough to skip the real mic and fall back to a 44.1 kHz pulse device that cannot be resampled
        # to 16 kHz. Reachable once pulseaudio started at boot and held the USB card.
        from ai.voice_assistant import I2S_PULSE_SOURCE
        listing = (f"0\t{I2S_PULSE_SOURCE}\tmodule-alsa-card.c\ts16le 2ch 44100Hz\tSUSPENDED\n"
                   "1\talsa_input.usb-C-Media_Audio-00.mono-fallback\tmodule-alsa-card.c\t"
                   "s16le 1ch 44100Hz\tIDLE\n"
                   "2\talsa_output.usb-C-Media_Audio-00.analog-stereo.monitor\tmodule-alsa-card.c\t"
                   "s16le 2ch 44100Hz\tIDLE\n")

        def run(cmd, **kw):
            out = MagicMock()
            out.stdout = listing if cmd[:3] == ["pactl", "list", "short"] else ""
            return out

        with patch("ai.voice_assistant.I2S_SUSPEND_PULSE", True), \
             patch("ai.voice_assistant.PULSE_SUSPEND_ALL_SOURCES", True), \
             patch("ai.voice_assistant.subprocess.run", side_effect=run) as mock_run:
            free_i2s_device()

        suspended = [c.args[0][2] for c in mock_run.call_args_list
                     if c.args[0][:2] == ["pactl", "suspend-source"]]
        self.assertIn(I2S_PULSE_SOURCE, suspended)
        self.assertIn("alsa_input.usb-C-Media_Audio-00.mono-fallback", suspended)
        self.assertEqual(len(suspended), 2, "the I2S source must not be suspended twice")
        self.assertFalse([s for s in suspended if s.endswith(".monitor")],
                         "monitors are output taps and hold no capture hardware")

    def test_missing_pactl_while_enumerating_does_not_raise(self):
        with patch("ai.voice_assistant.I2S_SUSPEND_PULSE", True), \
             patch("ai.voice_assistant.PULSE_SUSPEND_ALL_SOURCES", True), \
             patch("ai.voice_assistant.subprocess.run", side_effect=FileNotFoundError("no pactl")):
            free_i2s_device()   # must not raise

    def test_resume_pulse_source_unsuspends(self):
        from ai.voice_assistant import I2S_PULSE_SOURCE
        with patch("ai.voice_assistant.I2S_SUSPEND_PULSE", True), \
             patch("ai.voice_assistant.subprocess.run") as mock_run:
            resume_pulse_source()
        args, _ = mock_run.call_args
        self.assertEqual(args[0], ["pactl", "suspend-source", I2S_PULSE_SOURCE, "0"])

    def test_disabled_toggle_skips_pactl(self):
        with patch("ai.voice_assistant.I2S_SUSPEND_PULSE", False), \
             patch("ai.voice_assistant.subprocess.run") as mock_run:
            free_i2s_device()
            resume_pulse_source()
        mock_run.assert_not_called()

    def test_missing_pactl_does_not_raise(self):
        with patch("ai.voice_assistant.I2S_SUSPEND_PULSE", True), \
             patch("ai.voice_assistant.subprocess.run", side_effect=FileNotFoundError("no pactl")):
            free_i2s_device()      # must not raise
            resume_pulse_source()  # must not raise


class TestApplyI2SRoute(unittest.TestCase):
    def test_applies_every_control_when_amixer_succeeds(self):
        from ai.voice_assistant import I2S_ROUTE_CONTROLS
        with patch("ai.voice_assistant.I2S_APPLY_ROUTE_ON_STARTUP", True), \
             patch("ai.voice_assistant.subprocess.run") as mock_run:
            ok = apply_i2s_route()
        self.assertTrue(ok)
        self.assertEqual(mock_run.call_count, len(I2S_ROUTE_CONTROLS))
        # each invocation is a non-shell amixer cset on the configured card
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0][0], "amixer")
        self.assertTrue(kwargs.get("check"))

    def test_disabled_toggle_skips_amixer(self):
        with patch("ai.voice_assistant.I2S_APPLY_ROUTE_ON_STARTUP", False), \
             patch("ai.voice_assistant.subprocess.run") as mock_run:
            ok = apply_i2s_route()
        self.assertFalse(ok)
        mock_run.assert_not_called()

    def test_missing_amixer_returns_false_without_raising(self):
        with patch("ai.voice_assistant.I2S_APPLY_ROUTE_ON_STARTUP", True), \
             patch("ai.voice_assistant.subprocess.run", side_effect=FileNotFoundError("no amixer")):
            self.assertFalse(apply_i2s_route())   # must not raise

    def test_failed_control_stops_early(self):
        import subprocess as _sp
        with patch("ai.voice_assistant.I2S_APPLY_ROUTE_ON_STARTUP", True), \
             patch("ai.voice_assistant.subprocess.run",
                   side_effect=_sp.CalledProcessError(1, "amixer")) as mock_run:
            ok = apply_i2s_route()
        self.assertFalse(ok)
        self.assertEqual(mock_run.call_count, 1)   # bails after the first failure, no 9x spam

    def test_ensure_input_resolved_frees_card_before_probing(self):
        va = make_assistant()
        calls = []
        with patch("ai.voice_assistant.apply_i2s_route",
                   side_effect=lambda: calls.append("route")), \
             patch("ai.voice_assistant.free_i2s_device",
                   side_effect=lambda: calls.append("free")), \
             patch("ai.voice_assistant.resume_pulse_source",
                   side_effect=lambda: calls.append("resume")), \
             patch("ai.voice_assistant.resolve_input_device",
                   side_effect=lambda: (calls.append("resolve"),
                                        MicChoice(3, 48000, 2, 0, "int16", True))[1]):
            va.ensure_input_resolved()
        # route applied, card freed from pulse, THEN probed; i2s result => no resume
        self.assertEqual(calls, ["route", "free", "resolve"])

    def test_ensure_input_resolved_resumes_pulse_when_not_i2s(self):
        va = make_assistant()
        with patch("ai.voice_assistant.apply_i2s_route"), \
             patch("ai.voice_assistant.free_i2s_device"), \
             patch("ai.voice_assistant.resume_pulse_source") as mock_resume, \
             patch("ai.voice_assistant.resolve_input_device",
                   return_value=MicChoice(None, 16000, 1, 0, "int16", False)):
            va.ensure_input_resolved()
        mock_resume.assert_called_once()


class TestClassifyDevice(unittest.TestCase):
    def test_i2s_matches_ape_and_tegra_dlink(self):
        self.assertEqual(_classify_device("APE (hw:APE,0)"), "i2s")
        self.assertEqual(_classify_device("tegra-dlink-0 (hw:1,0)"), "i2s")

    def test_usb_match(self):
        self.assertEqual(_classify_device("Some USB Audio (hw:0,0)"), "usb")

    def test_case_insensitive(self):
        self.assertEqual(_classify_device("my usb mic"), "usb")
        self.assertEqual(_classify_device("Tegra-DLink capture"), "i2s")

    def test_other_when_no_hint_matches(self):
        self.assertEqual(_classify_device("Generic onboard analog"), "other")

    def test_i2s_wins_over_usb_when_both_present(self):
        # Contrived name containing both hints — I2S is checked first.
        self.assertEqual(_classify_device("APE USB bridge"), "i2s")


class TestResolveInputDevice(unittest.TestCase):
    def test_prefers_live_i2s_over_usb(self):
        devices = [
            {"name": "USB Mic (hw:0,0)", "max_input_channels": 2, "default_samplerate": 44100.0},
            # Real Jetson APE reports a misleading default_samplerate (44100) but the hw device is
            # locked to its 48 kHz route rate — resolution must ignore the advertised rate.
            {"name": "NVIDIA Jetson Orin Nano APE: - (hw:1,0)", "max_input_channels": 16, "default_samplerate": 44100.0},
        ]
        # Both live; the I2S device must be probed first and win.
        with patch("ai.voice_assistant.sd.query_devices", return_value=devices), \
             patch("ai.voice_assistant.sd.default") as mock_default, \
             patch("ai.voice_assistant._probe_is_live", return_value=True):
            mock_default.device = [-1, -1]
            choice = resolve_input_device()
        self.assertEqual(choice.device, 1)         # the APE/I2S device
        self.assertEqual(choice.rate, 48000)       # pinned to the I2S clock rate (pulse suspended)
        self.assertEqual(choice.channels, 2)       # captured stereo
        self.assertEqual(choice.take_channel, 0)   # left slot
        self.assertTrue(choice.is_i2s)

    def test_falls_back_to_usb_when_i2s_silent(self):
        devices = [
            {"name": "APE tegra-dlink-0 (hw:APE,0)", "max_input_channels": 16, "default_samplerate": 48000.0},
            {"name": "USB Mic (hw:0,0)", "max_input_channels": 2, "default_samplerate": 44100.0},
        ]
        # I2S (probed first) reads silent, USB is live.
        with patch("ai.voice_assistant.sd.query_devices", return_value=devices), \
             patch("ai.voice_assistant.sd.default") as mock_default, \
             patch("ai.voice_assistant._probe_is_live", side_effect=[False, True]):
            mock_default.device = [-1, -1]
            choice = resolve_input_device()
        self.assertEqual(choice.device, 1)         # the USB device
        # NOT 44100, which is what this device advertises. 44100 does not divide into SAMPLE_RATE,
        # so MicStream cannot build a decimator for it and the session dies on open — the whole
        # point of _capture_rates_for. Only divisible rates are ever offered.
        self.assertEqual(choice.rate % 16000, 0)
        self.assertEqual(choice.channels, 1)       # mono
        self.assertEqual(choice.take_channel, 0)
        self.assertFalse(choice.is_i2s)

    def test_usb_that_rejects_16k_is_opened_at_48k_not_its_advertised_44100(self):
        """The 2026-08-09 robot failure, end to end.

        The C-Media dongle advertises default_samplerate=44100 and its hw params are
        `S16_LE mono, RATE: [44100 48000]` — so 16 kHz cannot be opened at all and 44100 cannot be
        resampled. The old code took the advertised rate and handed back 44100, and MicStream.open()
        died on `decimation needs an integer ratio, got 44100 -> 16000`, which took hands-free AND
        push-to-talk down. 48000 was available the entire time.
        """
        devices = [
            {"name": "USB Audio Device: - (hw:0,0)", "max_input_channels": 1,
             "default_samplerate": 44100.0},
        ]

        def probe(device, rate, channels, take_channel, retries=0):
            return rate in (44100, 48000)      # exactly what this dongle supports

        with patch("ai.voice_assistant.sd.query_devices", return_value=devices), \
             patch("ai.voice_assistant.sd.default") as mock_default, \
             patch("ai.voice_assistant._probe_is_live", side_effect=probe):
            mock_default.device = [-1, -1]
            choice = resolve_input_device()
        self.assertEqual(choice.device, 0)
        self.assertEqual(choice.rate, 48000)       # the one rate that both opens and resamples
        self.assertFalse(choice.is_i2s)

    def test_device_that_opens_at_no_usable_rate_is_skipped_not_returned(self):
        """A 44.1-kHz-only device must be passed over, not handed back.

        Returning it is strictly worse than falling through: it looks like success and then fails
        at Decimator construction, where the only recovery is the session refusing to start.
        """
        devices = [
            {"name": "Fussy Mic (hw:1,0)", "max_input_channels": 1, "default_samplerate": 44100.0},
        ]
        with patch("ai.voice_assistant.sd.query_devices", return_value=devices), \
             patch("ai.voice_assistant.sd.default") as mock_default, \
             patch("ai.voice_assistant._probe_is_live", return_value=False):
            mock_default.device = [-1, -1]
            choice = resolve_input_device()
        self.assertIsNone(choice.device)
        self.assertEqual(choice.rate, 16000)


class TestCaptureRatesFor(unittest.TestCase):
    def test_i2s_is_pinned_to_the_route_rate_and_ignores_the_advertised_one(self):
        # The real APE device advertises 44100 while the route runs at 48 kHz. Trusting the
        # advertised rate here would garble speech even when it happened to be divisible.
        self.assertEqual(_capture_rates_for("i2s", 44100), (48000,))

    def test_every_offered_rate_divides_into_the_pipeline_rate(self):
        for kind in ("usb", "other"):
            for advertised in (0, 8000, 44100, 48000, 96000):
                for rate in _capture_rates_for(kind, advertised):
                    self.assertEqual(rate % 16000, 0,
                                     f"{rate} from kind={kind} advertised={advertised}")

    def test_indivisible_advertised_rate_is_dropped_entirely(self):
        self.assertNotIn(44100, _capture_rates_for("usb", 44100))

    def test_divisible_advertised_rate_leads_so_the_native_rate_is_tried_first(self):
        # Opening a device at its own rate avoids a driver-side resample, so prefer it — but only
        # because it passed the divisibility filter, never on the strength of being advertised.
        self.assertEqual(_capture_rates_for("usb", 48000)[0], 48000)
        self.assertEqual(_capture_rates_for("other", 32000)[0], 32000)

    def test_no_duplicate_rates_so_no_device_is_probed_twice_at_one_rate(self):
        for advertised in (16000, 32000, 44100, 48000):
            rates = _capture_rates_for("usb", advertised)
            self.assertEqual(len(rates), len(set(rates)))

    def test_default_is_last_resort(self):
        # No I2S/USB present: an 'other' device that's live is chosen, captured mono.
        devices = [
            {"name": "Generic onboard (hw:1,0)", "max_input_channels": 2, "default_samplerate": 48000.0},
        ]
        with patch("ai.voice_assistant.sd.query_devices", return_value=devices), \
             patch("ai.voice_assistant.sd.default") as mock_default, \
             patch("ai.voice_assistant._probe_is_live", return_value=True):
            mock_default.device = [-1, -1]
            choice = resolve_input_device()
        self.assertEqual(choice.device, 0)
        self.assertEqual(choice.channels, 1)

    def test_falls_back_when_nothing_is_live(self):
        devices = [
            {"name": "Silent onboard (hw:1,0)", "max_input_channels": 2, "default_samplerate": 48000.0},
        ]
        with patch("ai.voice_assistant.sd.query_devices", return_value=devices), \
             patch("ai.voice_assistant.sd.default") as mock_default, \
             patch("ai.voice_assistant._probe_is_live", return_value=False):
            mock_default.device = [-1, -1]
            choice = resolve_input_device()
        self.assertIsNone(choice.device)
        self.assertEqual(choice.rate, 16000)
        self.assertEqual(choice.channels, 1)

    def test_falls_back_when_query_raises(self):
        with patch("ai.voice_assistant.sd.query_devices", side_effect=OSError("no audio subsystem")):
            choice = resolve_input_device()
        self.assertIsNone(choice.device)
        self.assertEqual(choice.rate, 16000)


class TestProbeIsLive(unittest.TestCase):
    def test_returns_true_above_threshold(self):
        with patch("ai.voice_assistant.sd.rec", return_value=np.full((100, 1), 100, dtype="int16")), \
             patch("ai.voice_assistant.sd.wait"):
            self.assertTrue(_probe_is_live(0, 16000, 1, 0))

    def test_returns_false_on_silence(self):
        with patch("ai.voice_assistant.sd.rec", return_value=np.zeros((100, 1), dtype="int16")), \
             patch("ai.voice_assistant.sd.wait"):
            self.assertFalse(_probe_is_live(0, 16000, 1, 0))

    def test_returns_false_on_exception(self):
        with patch("ai.voice_assistant.sd.rec", side_effect=OSError("busy")):
            self.assertFalse(_probe_is_live(0, 16000, 1, 0))

    def test_stereo_measures_only_the_taken_channel(self):
        # INMP441 shape: left (col 0) loud, right (col 1) digital silence -> live on channel 0.
        rec = np.zeros((100, 2), dtype="int16")
        rec[:, 0] = 100
        with patch("ai.voice_assistant.sd.rec", return_value=rec), \
             patch("ai.voice_assistant.sd.wait"):
            self.assertTrue(_probe_is_live(0, 48000, 2, 0))

    def test_a_silent_first_read_is_retried_and_the_device_can_come_back(self):
        """The INMP441 warm-up: silent on the first capture after the route is applied, live after.

        Before the retry, that single early read condemned the preferred mic for the whole life of
        the process and Kai ran the entire session on the fallback USB mic.
        """
        silent = np.zeros((100, 2), dtype="int16")
        live = np.zeros((100, 2), dtype="int16")
        live[:, 0] = 100
        with patch("ai.voice_assistant.sd.rec", side_effect=[silent, silent, live]), \
             patch("ai.voice_assistant.sd.wait"), \
             patch("ai.voice_assistant.time.sleep"):
            self.assertTrue(_probe_is_live(5, 48000, 2, 0, retries=3))

    def test_retries_are_bounded_and_a_dead_device_still_reads_dead(self):
        with patch("ai.voice_assistant.sd.rec", return_value=np.zeros((100, 2), dtype="int16")) as rec, \
             patch("ai.voice_assistant.sd.wait"), \
             patch("ai.voice_assistant.time.sleep"):
            self.assertFalse(_probe_is_live(5, 48000, 2, 0, retries=3))
        self.assertEqual(rec.call_count, 4)      # the first read plus exactly three retries

    def test_a_device_that_refuses_to_open_is_not_retried(self):
        """An open failure is a definite answer. Retrying it burns LIVE_PROBE_TIMEOUT_S multiples on
        the session start path — which is the hang the timeout exists to prevent."""
        with patch("ai.voice_assistant.sd.rec", side_effect=OSError("busy")) as rec, \
             patch("ai.voice_assistant.time.sleep"):
            self.assertFalse(_probe_is_live(5, 48000, 2, 0, retries=3))
        self.assertEqual(rec.call_count, 1)

    def test_retries_default_to_off_so_other_devices_are_read_once(self):
        with patch("ai.voice_assistant.sd.rec", return_value=np.zeros((100, 1), dtype="int16")) as rec, \
             patch("ai.voice_assistant.sd.wait"):
            self.assertFalse(_probe_is_live(0, 48000, 1, 0))
        self.assertEqual(rec.call_count, 1)

    def test_only_the_i2s_device_is_retried_during_resolution(self):
        devices = [
            {"name": "APE tegra-dlink-0 (hw:APE,0)", "max_input_channels": 16,
             "default_samplerate": 44100.0},
            {"name": "USB Mic (hw:0,0)", "max_input_channels": 1, "default_samplerate": 44100.0},
        ]
        seen = []

        def probe(device, rate, channels, take_channel, retries=0):
            seen.append((device, retries))
            return device == 1 and rate == 48000

        with patch("ai.voice_assistant.sd.query_devices", return_value=devices), \
             patch("ai.voice_assistant.sd.default") as mock_default, \
             patch("ai.voice_assistant._probe_is_live", side_effect=probe):
            mock_default.device = [-1, -1]
            resolve_input_device()
        self.assertTrue(all(r > 0 for d, r in seen if d == 0), seen)   # I2S retried
        self.assertTrue(all(r == 0 for d, r in seen if d == 1), seen)  # USB read once per rate

    def test_stereo_silent_on_taken_channel_reads_dead(self):
        # Signal only in the untaken channel -> the taken (left) channel is silent -> not live.
        rec = np.zeros((100, 2), dtype="int16")
        rec[:, 1] = 100
        with patch("ai.voice_assistant.sd.rec", return_value=rec), \
             patch("ai.voice_assistant.sd.wait"):
            self.assertFalse(_probe_is_live(0, 48000, 2, 0))


class _Seg:
    """A faster-whisper segment, only the fields the sanity gate reads."""

    def __init__(self, text="hello", avg_logprob=-0.2, no_speech_prob=0.05):
        self.text = text
        self.avg_logprob = avg_logprob
        self.no_speech_prob = no_speech_prob


class TestLatinLetterRatio(unittest.TestCase):
    def test_plain_english(self):
        self.assertEqual(latin_letter_ratio("what time is it"), 1.0)

    def test_tagalog_with_accents_is_latin(self):
        # Tagalog's only extras are ñ and Spanish loanword accents — all Latin. This must never
        # be mistaken for a foreign script.
        for text in ("anong oras na", "magandáng umaga", "señor", "kumustá ka"):
            with self.subTest(text=text):
                self.assertEqual(latin_letter_ratio(text), 1.0)

    def test_cjk_is_not_latin(self):
        self.assertEqual(latin_letter_ratio("嘿哀"), 0.0)

    def test_punctuation_digits_and_emoji_are_ignored(self):
        # They are not alphabetic, so they can neither trip the check nor dilute a bad transcript
        # into passing it.
        self.assertEqual(latin_letter_ratio("set a timer for 5 minutes — ok? 🎉"), 1.0)
        self.assertEqual(latin_letter_ratio("123 !?, —"), 1.0)

    def test_no_letters_at_all_is_treated_as_fine(self):
        self.assertEqual(latin_letter_ratio(""), 1.0)
        self.assertEqual(latin_letter_ratio("..."), 1.0)

    def test_mixed_script_is_proportional(self):
        self.assertAlmostEqual(latin_letter_ratio("hey嘿"), 3 / 4)


class TestTranscriptRejection(unittest.TestCase):
    """The hole this closes: WHISPER_LANGUAGES restricts the detected-language LABEL only. A clip
    labelled 'en' is never re-transcribed, so a decode that emitted '嘿哀' — or invented a sentence out
    of fan noise — reached the LLM and was answered as though someone had asked it."""

    def test_good_transcript_is_kept(self):
        self.assertEqual(transcript_rejection("what time is it", [_Seg()]), "")

    def test_foreign_script_is_rejected(self):
        reason = transcript_rejection("嘿哀", [_Seg(text="嘿哀")])
        self.assertIn("Latin", reason)

    def test_the_measured_real_world_hallucination(self):
        # Observed on the robot: "hey kai" decoded as these. The first is caught by script; the
        # second is Latin and must fall to the confidence gate instead, not slip through.
        self.assertNotEqual(transcript_rejection("嘿哀", [_Seg(text="嘿哀")]), "")
        self.assertNotEqual(
            transcript_rejection("Hẹc gai!", [_Seg(text="Hẹc gai!", avg_logprob=-1.6)]), "")

    def test_low_confidence_is_rejected_even_in_english(self):
        reason = transcript_rejection("open the door", [_Seg(avg_logprob=-2.0)])
        self.assertIn("unintelligible", reason)

    def test_the_worst_segment_decides(self):
        # One confidently-wrong stretch is enough to make the transcript a different question than
        # the one that was asked, so the minimum governs — not the mean.
        segs = [_Seg(avg_logprob=-0.1), _Seg(avg_logprob=-3.0), _Seg(avg_logprob=-0.1)]
        self.assertNotEqual(transcript_rejection("a b c", segs), "")

    def test_words_decoded_out_of_silence_are_rejected(self):
        reason = transcript_rejection("Thank you.", [_Seg(text="Thank you.", no_speech_prob=0.95)])
        self.assertIn("no_speech_prob", reason)

    def test_empty_text_is_not_a_rejection(self):
        # Empty is handled upstream as "didn't catch that"; calling it a rejection would double-log.
        self.assertEqual(transcript_rejection("", [_Seg(avg_logprob=-9.0)]), "")

    def test_missing_fields_disable_only_that_gate(self):
        # A faster-whisper version that renames or omits a field must degrade to "gate off", never
        # to a TypeError that fails every turn.
        class Bare:
            text = "what time is it"

        self.assertEqual(transcript_rejection("what time is it", [Bare()]), "")

    def test_non_numeric_fields_are_ignored_rather_than_compared(self):
        self.assertEqual(
            transcript_rejection("hello", [_Seg(avg_logprob=MagicMock(),
                                                no_speech_prob=MagicMock())]), "")

    def test_numpy_floats_are_honoured(self):
        # np.float64 is a numbers.Real, so it must NOT be skipped as "not a number".
        segs = [_Seg(avg_logprob=np.float64(-2.5))]
        self.assertNotEqual(transcript_rejection("hello", segs), "")

    def test_script_guard_can_be_switched_off(self):
        with patch("ai.voice_assistant.TRANSCRIPT_SCRIPT_GUARD", False):
            self.assertEqual(transcript_rejection("嘿哀", [_Seg(text="嘿哀")]), "")

    def test_each_gate_can_be_disabled_with_none(self):
        with patch("ai.voice_assistant.TRANSCRIPT_MIN_AVG_LOGPROB", None):
            self.assertEqual(transcript_rejection("hello", [_Seg(avg_logprob=-9.0)]), "")
        with patch("ai.voice_assistant.TRANSCRIPT_MAX_NO_SPEECH_PROB", None):
            self.assertEqual(transcript_rejection("hello", [_Seg(no_speech_prob=0.99)]), "")


class TestProbeExplainsItself(unittest.TestCase):
    """A rejected probe must say WHY, and the two reasons must be distinguishable.

    Both used to return a bare False. "The device refused to open" and "the mic is silent" then
    looked identical from the log — just `i2s=False` with no reason — and on 2026-08-07 that turned a
    startup race into a hardware investigation. They need different fixes (check what is holding the
    card vs. check the wiring), so the log has to say which one happened.
    """

    def test_open_failure_reports_the_exception(self):
        with patch("ai.voice_assistant.sd.rec", side_effect=OSError("Device unavailable")), \
             patch("builtins.print") as out:
            self.assertFalse(_probe_is_live(5, 48000, 2, 0))
        logged = " ".join(str(c) for c in out.call_args_list)
        self.assertIn("rejected the probe", logged)
        self.assertIn("Device unavailable", logged, "the real reason must survive to the log")
        self.assertIn("OSError", logged)

    def test_silence_is_reported_as_silence_not_as_an_error(self):
        with patch("ai.voice_assistant.sd.rec", return_value=np.zeros((100, 1), dtype="int16")), \
             patch("ai.voice_assistant.sd.wait"), patch("builtins.print") as out:
            self.assertFalse(_probe_is_live(5, 48000, 1, 0))
        logged = " ".join(str(c) for c in out.call_args_list)
        self.assertIn("read as silent", logged)
        self.assertNotIn("rejected the probe", logged, "silence is not an open failure")

    def test_a_live_device_stays_quiet(self):
        # One line per REJECTED candidate; the success path must not add noise to every startup.
        with patch("ai.voice_assistant.sd.rec",
                   return_value=np.full((100, 1), 100, dtype="int16")), \
             patch("ai.voice_assistant.sd.wait"), patch("builtins.print") as out:
            self.assertTrue(_probe_is_live(5, 48000, 1, 0))
        self.assertEqual(out.call_args_list, [])


class TestStateMachine(unittest.TestCase):
    def setUp(self):
        # ensure_input_resolved()/start_recording() shell out to `amixer` and `pactl`; stub them so
        # no test reconfigures real audio on the Jetson (or spawns a missing binary on a dev box).
        for name in ("apply_i2s_route", "free_i2s_device", "resume_pulse_source"):
            p = patch(f"ai.voice_assistant.{name}")
            setattr(self, f"mock_{name}", p.start())
            self.addCleanup(p.stop)
        self.mock_apply_i2s_route.return_value = False

    def test_starts_idle(self):
        va = make_assistant()
        self.assertEqual(va.get_status()["voice_status"], STATUS_IDLE)

    def test_start_recording_opens_stream(self):
        va = make_assistant()
        with patch("ai.voice_assistant.resolve_input_device",
                   return_value=MicChoice(None, 16000, 1, 0, "int16")), \
             patch("ai.voice_assistant.sd.InputStream") as mock_stream_cls:
            mock_stream = MagicMock()
            mock_stream_cls.return_value = mock_stream
            result = va.start_recording()
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(va.get_status()["voice_status"], STATUS_RECORDING)
        mock_stream.start.assert_called_once()

    def test_start_recording_opens_i2s_stereo_and_frees_pulse(self):
        va = make_assistant()
        with patch("ai.voice_assistant.resolve_input_device",
                   return_value=MicChoice(3, 48000, 2, 0, "int16", True)), \
             patch("ai.voice_assistant.sd.InputStream") as mock_stream_cls:
            mock_stream_cls.return_value = MagicMock()
            va.start_recording()
        _, kwargs = mock_stream_cls.call_args
        self.assertEqual(kwargs["channels"], 2)
        self.assertEqual(kwargs["samplerate"], 48000)
        self.assertEqual(kwargs["device"], 3)
        # I2S capture must (re)free the card from pulse before opening the stream
        self.mock_free_i2s_device.assert_called()

    def test_start_recording_non_i2s_hands_pulse_back(self):
        va = make_assistant()
        with patch("ai.voice_assistant.resolve_input_device",
                   return_value=MicChoice(None, 16000, 1, 0, "int16", False)), \
             patch("ai.voice_assistant.sd.InputStream") as mock_stream_cls:
            mock_stream_cls.return_value = MagicMock()
            va.start_recording()
        # non-I2S resolution resumes pulse (so the pulse-backed fallback isn't left muted)
        self.mock_resume_pulse_source.assert_called()

    def test_start_recording_rejected_while_recording(self):
        va = make_assistant()
        with patch("ai.voice_assistant.resolve_input_device",
                   return_value=MicChoice(None, 16000, 1, 0, "int16")), \
             patch("ai.voice_assistant.sd.InputStream"):
            va.start_recording()
            result = va.start_recording()
        self.assertIn("error", result)

    def test_stop_recording_rejected_while_idle(self):
        va = make_assistant()
        result = va.stop_recording()
        self.assertIn("error", result)

    def test_stop_recording_transitions_and_spawns_worker(self):
        va = make_assistant()
        with patch("ai.voice_assistant.resolve_input_device",
                   return_value=MicChoice(None, 16000, 1, 0, "int16")), \
             patch("ai.voice_assistant.sd.InputStream"):
            va.start_recording()
        with patch("threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            result = va.stop_recording()
        self.assertEqual(result, {"status": "ok"})
        mock_thread.start.assert_called_once()

    def test_start_recording_mic_failure_sets_error(self):
        va = make_assistant()
        with patch("ai.voice_assistant.resolve_input_device",
                   return_value=MicChoice(None, 16000, 1, 0, "int16")), \
             patch("ai.voice_assistant.sd.InputStream", side_effect=OSError("no device")):
            result = va.start_recording()
        self.assertIn("error", result)
        self.assertEqual(va.get_status()["voice_status"], STATUS_ERROR)

    def test_device_resolution_only_probed_once(self):
        va = make_assistant()
        with patch("ai.voice_assistant.resolve_input_device",
                   return_value=MicChoice(2, 44100, 2, 0, "int16")) as mock_resolve:
            va.ensure_input_resolved()
            va.ensure_input_resolved()
        mock_resolve.assert_called_once()
        self.assertEqual(va._capture_device, 2)
        self.assertEqual(va._capture_rate, 44100)
        self.assertEqual(va._capture_channels, 2)
        self.assertEqual(va._capture_channel, 0)


class TestOnAudioChunk(unittest.TestCase):
    def test_mono_chunk_stored_as_is(self):
        va = make_assistant()  # defaults: 1 channel, take channel 0
        indata = np.arange(10, dtype="int16").reshape(10, 1)
        va._on_audio_chunk(indata, 10, None, None)
        stored = va._audio_chunks[0]
        self.assertEqual(stored.shape, (10, 1))
        np.testing.assert_array_equal(stored[:, 0], np.arange(10))

    def test_stereo_chunk_keeps_only_taken_channel(self):
        va = make_assistant()
        va._capture_channels = 2
        va._capture_channel  = 0
        indata = np.zeros((10, 2), dtype="int16")
        indata[:, 0] = np.arange(10)     # left = real audio
        indata[:, 1] = 999               # right = digital silence stand-in
        va._on_audio_chunk(indata, 10, None, None)
        stored = va._audio_chunks[0]
        self.assertEqual(stored.shape, (10, 1))   # stays mono downstream
        np.testing.assert_array_equal(stored[:, 0], np.arange(10))

    def test_chunk_is_a_copy(self):
        # PortAudio reuses indata's buffer after the callback returns — the stored chunk must not
        # alias it.
        va = make_assistant()
        indata = np.ones((5, 1), dtype="int16")
        va._on_audio_chunk(indata, 5, None, None)
        indata[:] = 0
        self.assertTrue((va._audio_chunks[0] == 1).all())


class TestProcess(unittest.TestCase):
    def setUp(self):
        # _process -> _speak; with a real voice model present (on the Jetson) _speak would spawn a
        # worker that shells out to Piper/paplay. Disable TTS so these tests exercise the synchronous
        # text-timed pantomime fallback deterministically and never touch audio.
        p = patch("ai.voice_assistant.tts.enabled", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_empty_transcript_short_circuits_before_ollama(self):
        va = make_assistant()
        va._whisper_model = MagicMock()
        va._whisper_model.transcribe.return_value = ([], None)
        with patch.object(va, "_call_ollama") as mock_call:
            va._process(np.zeros((10, 1), dtype="int16"))
        mock_call.assert_not_called()
        status = va.get_status()
        self.assertEqual(status["voice_status"], STATUS_DONE)
        self.assertEqual(status["voice_response"], NO_SPEECH_RESPONSE)

    def test_transcript_concatenation(self):
        va = make_assistant()
        va._whisper_model = MagicMock()
        va._whisper_model.transcribe.return_value = (
            [make_segment("hello "), make_segment("world")], None
        )
        with patch.object(va, "_call_ollama", return_value="hi there"):
            va._process(np.ones((10, 1), dtype="int16"))
        status = va.get_status()
        self.assertEqual(status["voice_transcript"], "hello world")
        self.assertEqual(status["voice_response"], "hi there")
        self.assertEqual(status["voice_status"], STATUS_DONE)

    def test_history_updated_after_success(self):
        va = make_assistant()
        va._whisper_model = MagicMock()
        va._whisper_model.transcribe.return_value = ([make_segment("hi")], None)
        with patch.object(va, "_call_ollama", return_value="hello back"):
            va._process(np.ones((10, 1), dtype="int16"))
        self.assertEqual(va._history[-2], {"role": "user", "content": "hi"})
        self.assertEqual(va._history[-1], {"role": "assistant", "content": "hello back"})

    def test_exception_sets_error_status(self):
        va = make_assistant()
        va._whisper_model = MagicMock()
        va._whisper_model.transcribe.side_effect = RuntimeError("boom")
        va._process(np.ones((10, 1), dtype="int16"))
        status = va.get_status()
        self.assertEqual(status["voice_status"], STATUS_ERROR)
        self.assertIn("boom", status["voice_error"])

    def test_resamples_when_capture_rate_differs_from_whisper_rate(self):
        va = make_assistant()
        va._capture_rate = 44100
        va._whisper_model = MagicMock()
        va._whisper_model.transcribe.return_value = ([make_segment("hi")], None)
        audio = np.ones((44100, 1), dtype="int16")  # 1s at the mic's native rate
        va._transcribe(audio)
        call_args = va._whisper_model.transcribe.call_args
        samples = call_args[0][0]
        # resampled from 44100 to 16000 samples/sec — length should track the target rate
        self.assertAlmostEqual(len(samples), 16000, delta=100)
        self.assertTrue(call_args[1]["vad_filter"])


class TestCallOllama(unittest.TestCase):
    def test_posts_expected_payload(self):
        va = make_assistant()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "reply"}}
        with patch("ai.voice_assistant.rag.retrieve_context", return_value=""), \
             patch("ai.voice_assistant.requests.post", return_value=mock_resp) as mock_post:
            result = va._call_ollama("hello")
        self.assertEqual(result, "reply")
        _, kwargs = mock_post.call_args
        # Assert against the config value, not a hardcoded name — this drifted once already when the
        # model was switched from gemma3:4b to fit the camera in 8 GB.
        self.assertEqual(kwargs["json"]["model"], OLLAMA_MODEL)
        self.assertEqual(kwargs["json"]["stream"], False)
        self.assertIn("num_ctx", kwargs["json"]["options"])

    def test_context_goes_in_the_user_turn_leaving_the_prefix_stable(self):
        """The RAG context must NOT sit in the system message. It changes every turn, and Ollama
        reuses its KV cache only for the longest common PREFIX — so context at the front
        re-evaluates the persona and all of history on every turn. In the user turn it leaves that
        prefix byte-identical. See RAG_CONTEXT_PLACEMENT in config/voice.py."""
        va = make_assistant()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "reply"}}
        with patch("ai.voice_assistant.load_persona", return_value="PERSONA_TEXT"), \
             patch("ai.voice_assistant.rag.retrieve_context", return_value="CONTEXT_TEXT"), \
             patch("ai.voice_assistant.requests.post", return_value=mock_resp) as mock_post:
            va._call_ollama("hello")
        messages = mock_post.call_args[1]["json"]["messages"]
        system_msg, user_msg = messages[0], messages[-1]
        self.assertEqual(system_msg["role"], "system")
        self.assertEqual(system_msg["content"], "PERSONA_TEXT")   # the persona ALONE — no context
        self.assertNotIn("CONTEXT_TEXT", system_msg["content"])
        self.assertEqual(user_msg["role"], "user")
        self.assertIn("CONTEXT_TEXT", user_msg["content"])
        self.assertIn("hello", user_msg["content"])

    def test_system_placement_restores_the_old_shape(self):
        """The documented revert (RAG_CONTEXT_PLACEMENT = "system") must put the context back in the
        system prompt exactly as before, so a quality regression can be undone with one config line."""
        va = make_assistant()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "reply"}}
        with patch("ai.voice_assistant.RAG_CONTEXT_PLACEMENT", "system"), \
             patch("ai.voice_assistant.load_persona", return_value="PERSONA_TEXT"), \
             patch("ai.voice_assistant.rag.retrieve_context", return_value="CONTEXT_TEXT"), \
             patch("ai.voice_assistant.requests.post", return_value=mock_resp) as mock_post:
            va._call_ollama("hello")
        messages = mock_post.call_args[1]["json"]["messages"]
        self.assertIn("PERSONA_TEXT", messages[0]["content"])
        self.assertIn("CONTEXT_TEXT", messages[0]["content"])
        self.assertEqual(messages[-1]["content"], "hello")

    def test_history_stores_the_raw_transcript_not_the_injected_context(self):
        """The prefix-stability win depends on this: if the context leaked into stored history, the
        prefix would differ next turn and the KV cache would be invalidated anyway."""
        va = make_assistant()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "reply"}}
        with patch("ai.voice_assistant.rag.retrieve_context", return_value="CONTEXT_TEXT"), \
             patch("ai.voice_assistant.requests.post", return_value=mock_resp), \
             patch.object(va, "_transcribe", return_value="hello"), \
             patch.object(va, "_speak"):
            va._process(np.zeros(16000, dtype=np.int16), rate=16000)
        self.assertEqual(va._history[0], {"role": "user", "content": "hello"})

    def test_records_stage_timings(self):
        va = make_assistant()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "message": {"content": "reply"},
            "prompt_eval_count": 120, "prompt_eval_duration": 200_000_000,   # 200 ms
            "eval_count": 40, "eval_duration": 2_000_000_000,                # 2 s -> 20 tok/s
        }
        with patch("ai.voice_assistant.rag.retrieve_context", return_value=""), \
             patch("ai.voice_assistant.requests.post", return_value=mock_resp):
            va._call_ollama("hello")
        t = va.stage_timings()
        self.assertEqual(t["llm_prompt_ms"], 200)
        self.assertEqual(t["llm_gen_ms"], 2000)
        self.assertEqual(t["llm_tok_s"], 20.0)
        self.assertGreaterEqual(t["llm_ms"], 0)

    def test_missing_timing_fields_are_tolerated(self):
        """A response with no timing fields at all (an old Ollama, or a stub) must not raise —
        this sits on the hot path and may never cost a reply."""
        va = make_assistant()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "reply"}}
        with patch("ai.voice_assistant.rag.retrieve_context", return_value=""), \
             patch("ai.voice_assistant.requests.post", return_value=mock_resp):
            self.assertEqual(va._call_ollama("hello"), "reply")
        self.assertEqual(va.stage_timings()["llm_tok_s"], 0.0)

    def test_uses_only_persona_when_no_context(self):
        va = make_assistant()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "reply"}}
        with patch("ai.voice_assistant.load_persona", return_value="PERSONA_TEXT"), \
             patch("ai.voice_assistant.rag.retrieve_context", return_value=""), \
             patch("ai.voice_assistant.requests.post", return_value=mock_resp) as mock_post:
            va._call_ollama("hello")
        messages = mock_post.call_args[1]["json"]["messages"]
        self.assertEqual(messages[0]["content"], "PERSONA_TEXT")
        self.assertEqual(messages[-1]["content"], "hello")   # no context, so the turn is untouched

    def test_keep_alive_and_timeout_present(self):
        va = make_assistant()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "reply"}}
        with patch("ai.voice_assistant.rag.retrieve_context", return_value=""), \
             patch("ai.voice_assistant.requests.post", return_value=mock_resp) as mock_post:
            va._call_ollama("hello")
        _, kwargs = mock_post.call_args
        self.assertIn("timeout", kwargs)
        # keep_alive must be a JSON number (e.g. -1) — Ollama rejects the string "-1" as an
        # invalid Go duration with a 400, which silently broke every real request before.
        self.assertIsInstance(kwargs["json"]["keep_alive"], (int, float))

    def test_connection_error_raises_runtime_error(self):
        va = make_assistant()
        with patch("ai.voice_assistant.rag.retrieve_context", return_value=""), \
             patch("ai.voice_assistant.requests.post", side_effect=requests.exceptions.ConnectionError()):
            with self.assertRaises(RuntimeError):
                va._call_ollama("hello")


class TestEnsureLlmWarm(unittest.TestCase):
    def test_swallows_connection_error(self):
        va = make_assistant()
        with patch("ai.voice_assistant.requests.post", side_effect=requests.exceptions.ConnectionError()):
            va.ensure_llm_warm()  # must not raise

    def test_posts_a_trivial_request(self):
        va = make_assistant()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "hi"}}
        # log_model_placement patched out: it is diagnostics-only, and unpatched it would reach for a
        # real Ollama on localhost from the test suite.
        with patch("ai.voice_assistant.log_model_placement"), \
             patch("ai.voice_assistant.requests.post", return_value=mock_resp) as mock_post:
            va.ensure_llm_warm()
        mock_post.assert_called_once()

    def test_logs_model_placement_after_a_successful_warm(self):
        """The placement probe is the whole reason warming happens at startup — Ollama pins the
        CPU/GPU choice at load time, and a partial offload is otherwise invisible."""
        va = make_assistant()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "hi"}}
        with patch("ai.voice_assistant.log_model_placement") as mock_ps, \
             patch("ai.voice_assistant.requests.post", return_value=mock_resp):
            va.ensure_llm_warm()
        mock_ps.assert_called_once()

    def test_skips_placement_probe_when_warm_up_failed(self):
        va = make_assistant()
        with patch("ai.voice_assistant.log_model_placement") as mock_ps, \
             patch("ai.voice_assistant.requests.post",
                   side_effect=requests.exceptions.ConnectionError()):
            va.ensure_llm_warm()
        mock_ps.assert_not_called()


class TestLogModelPlacement(unittest.TestCase):
    def _resp(self, payload):
        r = MagicMock()
        r.json.return_value = payload
        return r

    def test_reports_a_full_gpu_offload(self):
        payload = {"models": [{"name": f"{OLLAMA_MODEL}", "size": 2000, "size_vram": 2000}]}
        with patch("ai.voice_assistant.requests.get", return_value=self._resp(payload)):
            out = voice_assistant.log_model_placement()
        self.assertEqual(out["gpu_pct"], 100.0)

    def test_reports_a_partial_offload(self):
        """The ~2x slowdown this whole probe exists to make visible."""
        payload = {"models": [{"name": f"{OLLAMA_MODEL}", "size": 2000, "size_vram": 900}]}
        with patch("ai.voice_assistant.requests.get", return_value=self._resp(payload)):
            out = voice_assistant.log_model_placement()
        self.assertEqual(out["gpu_pct"], 45.0)

    def test_never_raises_when_ollama_is_unreachable(self):
        with patch("ai.voice_assistant.requests.get",
                   side_effect=requests.exceptions.ConnectionError()):
            self.assertEqual(voice_assistant.log_model_placement(), {})

    def test_handles_model_not_loaded(self):
        with patch("ai.voice_assistant.requests.get", return_value=self._resp({"models": []})):
            self.assertEqual(voice_assistant.log_model_placement(), {})


class TestSplitSentences(unittest.TestCase):
    def test_splits_on_terminal_punctuation(self):
        self.assertEqual(_split_sentences("Hi there. How are you?"), ["Hi there.", "How are you?"])

    def test_no_punctuation_is_one_sentence(self):
        self.assertEqual(_split_sentences("didn't catch that"), ["didn't catch that"])

    def test_empty_returns_empty(self):
        self.assertEqual(_split_sentences("   "), [])


class TestSpeakSegments(unittest.TestCase):
    def test_one_segment_per_sentence(self):
        _, segs = _speak_segments("First one. Second one here.", 0.0)
        self.assertEqual(len(segs), 2)

    def test_short_sentence_hits_min_floor(self):
        _, segs = _speak_segments("Hi.", 0.0)
        self.assertAlmostEqual(segs[0][1] - segs[0][0], SPEAK_MIN_SENTENCE_S)

    def test_gap_between_sentences(self):
        _, segs = _speak_segments("One two three. Four five six.", 0.0)
        gap = segs[1][0] - segs[0][1]
        self.assertAlmostEqual(gap, SPEAK_GAP_S)

    def test_longer_sentence_stays_open_longer(self):
        _, short = _speak_segments("Hi there now.", 0.0)
        _, long  = _speak_segments("Hi there now this is a much longer sentence indeed.", 0.0)
        self.assertLess(short[0][1] - short[0][0], long[0][1] - long[0][0])

    def test_runaway_reply_capped_at_max(self):
        text = ". ".join(["word " * 5 for _ in range(100)])
        _, segs = _speak_segments(text, 0.0)
        self.assertLessEqual(segs[-1][1], SPEAK_MAX_S)

    def test_empty_text_still_produces_a_segment(self):
        _, segs = _speak_segments("", 0.0)
        self.assertEqual(len(segs), 1)


class TestSpeakingOpennessAt(unittest.TestCase):
    def test_none_when_no_schedule(self):
        self.assertIsNone(speaking_openness_at(5.0, None, ()))
        self.assertIsNone(speaking_openness_at(5.0, 0.0, ()))

    def test_none_before_start(self):
        _, segs = _speak_segments("Hello there friend.", 10.0)
        self.assertIsNone(speaking_openness_at(9.0, 10.0, segs))

    def test_none_after_last_sentence(self):
        _, segs = _speak_segments("Hello there friend.", 0.0)
        end = segs[-1][1]
        self.assertIsNone(speaking_openness_at(end, 0.0, segs))
        self.assertIsNone(speaking_openness_at(end + 1.0, 0.0, segs))

    def test_openness_within_bounds(self):
        _, segs = _speak_segments("Hello there friend. Nice to meet you.", 0.0)
        t = 0.0
        while t < segs[-1][1]:
            o = speaking_openness_at(t, 0.0, segs)
            self.assertIsNotNone(o)
            self.assertGreaterEqual(o, 0.0)
            self.assertLessEqual(o, SPEAK_AMP + 1e-9)
            t += 0.02

    def test_opens_at_start_holds_then_closes(self):
        # A single long sentence: closed-ish at the very edges, wide open in the middle.
        _, segs = _speak_segments("This is one nice long spoken sentence for testing.", 0.0)
        s0, s1 = segs[0]
        mid   = speaking_openness_at((s0 + s1) / 2.0, 0.0, segs)
        start = speaking_openness_at(s0 + 0.001, 0.0, segs)
        end   = speaking_openness_at(s1 - 0.001, 0.0, segs)
        self.assertAlmostEqual(mid, SPEAK_AMP)   # held fully open through the middle
        self.assertLess(start, mid * 0.5)         # ramps open from ~closed
        self.assertLess(end, mid * 0.5)           # ramps closed at the end

    def test_mouth_closed_between_sentences(self):
        _, segs = _speak_segments("First sentence here. Second sentence here.", 0.0)
        gap_t = (segs[0][1] + segs[1][0]) / 2.0   # midpoint of the between-sentence pause
        self.assertEqual(speaking_openness_at(gap_t, 0.0, segs), 0.0)


class TestSpeakingIntegration(unittest.TestCase):
    def setUp(self):
        # Same as TestProcess: keep the jaw pantomime synchronous and audio-free by disabling TTS.
        p = patch("ai.voice_assistant.tts.enabled", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_reply_opens_speaking_window(self):
        va = make_assistant()
        self.assertIsNone(va.speaking_openness())
        with patch("ai.voice_assistant.rag.retrieve_context", return_value=""):
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"message": {"content": "hello there friend"}}
            with patch("ai.voice_assistant.requests.post", return_value=mock_resp), \
                 patch.object(va, "_transcribe", return_value="hi"):
                va._process(np.zeros((10, 1), dtype="int16"))
        self.assertEqual(va.get_status()["voice_status"], STATUS_DONE)
        self.assertTrue(va.get_status()["voice_speaking"])
        self.assertIsNotNone(va.speaking_openness())

    def test_no_speech_still_animates(self):
        va = make_assistant()
        with patch.object(va, "_transcribe", return_value="   "):
            va._process(np.zeros((10, 1), dtype="int16"))
        self.assertEqual(va.get_status()["voice_response"], NO_SPEECH_RESPONSE)
        self.assertTrue(va.get_status()["voice_speaking"])


class TestSpeakSegmentsForDuration(unittest.TestCase):
    def test_fills_requested_duration_exactly(self):
        _, segs = _speak_segments_for_duration("One two. Three four.", 0.0, 5.0)
        self.assertAlmostEqual(segs[-1][1], 5.0, places=6)

    def test_one_segment_per_sentence(self):
        _, segs = _speak_segments_for_duration("A here. B here. C here.", 0.0, 3.0)
        self.assertEqual(len(segs), 3)

    def test_longer_sentence_gets_more_time(self):
        _, segs = _speak_segments_for_duration("Hi. This one is quite a bit longer indeed.", 0.0, 6.0)
        self.assertLess(segs[0][1] - segs[0][0], segs[1][1] - segs[1][0])

    def test_gap_between_sentences_and_still_fills_duration(self):
        _, segs = _speak_segments_for_duration("One two three. Four five six.", 0.0, 4.0)
        self.assertAlmostEqual(segs[1][0] - segs[0][1], SPEAK_GAP_S, places=6)
        self.assertAlmostEqual(segs[-1][1], 4.0, places=6)

    def test_zero_duration_is_empty(self):
        _, segs = _speak_segments_for_duration("Hello.", 0.0, 0.0)
        self.assertEqual(segs, ())

    def test_negative_duration_is_empty(self):
        _, segs = _speak_segments_for_duration("Hello.", 0.0, -2.0)
        self.assertEqual(segs, ())

    def test_empty_text_single_segment_filling_duration(self):
        _, segs = _speak_segments_for_duration("   ", 0.0, 2.0)
        self.assertEqual(len(segs), 1)
        self.assertAlmostEqual(segs[-1][1], 2.0, places=6)

    def test_start_time_is_passed_through(self):
        start, _ = _speak_segments_for_duration("Hello there.", 12.5, 2.0)
        self.assertEqual(start, 12.5)


class _InlineThread:
    """threading.Thread stand-in that runs the target synchronously on .start(), so _speak's worker
    body is exercised deterministically in-process — no real thread, no shelling out to Piper/paplay
    (the tts.* calls are themselves mocked)."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


class TestSpeak(unittest.TestCase):
    def test_disabled_uses_text_timed_pantomime_without_synth(self):
        va = make_assistant()
        with patch("ai.voice_assistant.tts.enabled", return_value=False), \
             patch("ai.voice_assistant.tts.synthesize") as mock_synth:
            va._speak("Hello there. Nice to meet you.")
        mock_synth.assert_not_called()
        self.assertIsNotNone(va._speak_start)
        self.assertEqual(len(va._speak_segments), 2)   # one window per sentence

    def test_enabled_scales_window_to_audio_duration_and_plays(self):
        va = make_assistant()
        with patch("ai.voice_assistant.tts.enabled", return_value=True), \
             patch("ai.voice_assistant.tts.stop"), \
             patch("ai.voice_assistant.tts.synthesize", return_value="/tmp/kai_tts.wav") as mock_synth, \
             patch("ai.voice_assistant.tts.wav_duration", return_value=4.0), \
             patch("ai.voice_assistant.tts.play") as mock_play, \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va._speak("One two three. Four five six.")
        mock_synth.assert_called_once()
        mock_play.assert_called_once()
        self.assertIsNotNone(va._speak_start)
        self.assertAlmostEqual(va._speak_segments[-1][1], 4.0, places=6)  # window == real audio length

    def test_enabled_synth_failure_falls_back_to_pantomime(self):
        va = make_assistant()
        with patch("ai.voice_assistant.tts.enabled", return_value=True), \
             patch("ai.voice_assistant.tts.stop"), \
             patch("ai.voice_assistant.tts.synthesize", return_value=None), \
             patch("ai.voice_assistant.tts.wav_duration") as mock_dur, \
             patch("ai.voice_assistant.tts.play") as mock_play, \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va._speak("Hello there.")
        mock_play.assert_not_called()   # nothing to play
        mock_dur.assert_not_called()
        self.assertIsNotNone(va._speak_start)          # pantomime window still set so the jaw moves
        self.assertEqual(len(va._speak_segments), 1)

    def test_synthesizes_emoji_stripped_text(self):
        # The UI keeps emoji; the spoken text must not (real clean_for_speech runs — not mocked).
        va = make_assistant()
        with patch("ai.voice_assistant.tts.enabled", return_value=True), \
             patch("ai.voice_assistant.tts.stop"), \
             patch("ai.voice_assistant.tts.synthesize", return_value="/tmp/kai_tts.wav") as mock_synth, \
             patch("ai.voice_assistant.tts.wav_duration", return_value=2.0), \
             patch("ai.voice_assistant.tts.play"), \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va._speak("Hello there! 😀🎉")
        mock_synth.assert_called_once_with("Hello there!")


class TestCleanForSpeech(unittest.TestCase):
    def test_strips_emoji_keeps_words(self):
        self.assertEqual(clean_for_speech("Hello there! 😀"), "Hello there!")

    def test_strips_symbols_dingbats_and_flags(self):
        self.assertEqual(clean_for_speech("Nice ✨🎉 job 🇵🇭"), "Nice job")

    def test_collapses_whitespace_left_behind(self):
        self.assertEqual(clean_for_speech("Yes 👍 sir"), "Yes sir")

    def test_keeps_normal_punctuation_and_ellipsis(self):
        # … (U+2026) and — (U+2014) live in General Punctuation and must be preserved.
        self.assertEqual(clean_for_speech("Wait… really? Yes—now!"), "Wait… really? Yes—now!")

    def test_emoji_only_becomes_empty(self):
        self.assertEqual(clean_for_speech("😀🎉👍"), "")

    def test_empty_and_none_safe(self):
        self.assertEqual(clean_for_speech(""), "")
        self.assertEqual(clean_for_speech(None), "")


class TestTranscribeAsync(unittest.TestCase):
    """STT only — the wake-phrase scan must not be able to touch turn state."""

    def _run(self, **transcribe_kw):
        va = make_assistant()
        seen = []
        with patch.object(va, "_transcribe", **transcribe_kw), \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va.transcribe_async(np.ones(1000, dtype="int16"), 16000,
                                on_done=lambda tok, text, err: seen.append((tok, text, err)),
                                token=7, log_language=False)
        return va, seen

    def test_hands_back_the_transcript(self):
        _, seen = self._run(return_value="hey kai what time is it")
        self.assertEqual(seen, [(7, "hey kai what time is it", "")])

    def test_never_touches_turn_state(self):
        # Writing _status here would post a chat bubble for a sentence nobody addressed to Kai.
        va, _ = self._run(return_value="some overheard sentence")
        status = va.get_status()
        self.assertEqual(status["voice_status"], STATUS_IDLE)
        self.assertEqual(status["voice_transcript"], "")
        self.assertEqual(status["voice_response"], "")

    def test_errors_come_back_as_an_error_not_an_exception(self):
        _, seen = self._run(side_effect=RuntimeError("ctranslate2 exploded"))
        self.assertEqual(seen[0][0], 7)
        self.assertEqual(seen[0][1], "")
        self.assertIn("exploded", seen[0][2])

    def test_forwards_rate_and_suppresses_the_language_log(self):
        va = make_assistant()
        with patch.object(va, "_transcribe", return_value="") as mock_tr, \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va.transcribe_async(np.ones(10, dtype="int16"), 48000, on_done=lambda *a: None)
        self.assertEqual(mock_tr.call_args.kwargs["rate"], 48000)
        self.assertTrue(mock_tr.call_args.kwargs["log_language"])

    def test_broken_callback_does_not_kill_the_thread(self):
        va = make_assistant()
        with patch.object(va, "_transcribe", return_value="hi"), \
             patch("ai.voice_assistant.threading.Thread", _InlineThread), \
             patch("builtins.print") as mock_print:
            va.transcribe_async(np.ones(10, dtype="int16"), 16000,
                                on_done=lambda *a: (_ for _ in ()).throw(ValueError("bad")))
        mock_print.assert_called_once()

    def test_language_log_is_suppressed_when_asked(self):
        va = make_assistant()
        va._whisper_model = MagicMock()
        va._whisper_model.transcribe.return_value = ([make_segment("hi")], MagicMock(language="en",
                                                                                    language_probability=0.9))
        with patch("builtins.print") as mock_print:
            va._transcribe(np.ones((10, 1), dtype="int16"), rate=16000, log_language=False)
        mock_print.assert_not_called()


class TestSayEpochAndCallback(unittest.TestCase):
    """say() is the one-breath turn path: the transcript already exists, so re-transcribing the same
    audio through process_utterance would double the latency of the fastest interaction."""

    def setUp(self):
        p = patch("ai.voice_assistant.tts.enabled", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_reports_done(self):
        va = make_assistant()
        seen = []
        with patch.object(va, "_call_ollama", return_value="a reply"), \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va.say("what time is it", epoch=va.epoch, on_done=lambda ep, o: seen.append(o))
        self.assertEqual(seen, ["done"])
        self.assertEqual(va.get_status()["voice_response"], "a reply")

    def test_reports_error(self):
        va = make_assistant()
        seen = []
        with patch.object(va, "_call_ollama", side_effect=RuntimeError("ollama down")), \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va.say("hello", epoch=va.epoch, on_done=lambda ep, o: seen.append(o))
        self.assertEqual(seen, ["error"])

    def test_rejects_a_stale_epoch_up_front(self):
        va = make_assistant()
        stale = va.epoch
        va.bump_epoch()
        result = va.say("hello", epoch=stale)
        self.assertEqual(result, {"error": "stale"})

    def test_stale_after_ollama_writes_nothing(self):
        va = make_assistant()
        seen = []

        def ollama(text):
            va.bump_epoch()
            return "a reply"

        with patch.object(va, "_call_ollama", side_effect=ollama), \
             patch.object(va, "_speak") as mock_speak, \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va.say("hello", epoch=va.epoch, on_done=lambda ep, o: seen.append(o))
        self.assertEqual(seen, ["stale"])
        self.assertEqual(va._history, [])
        mock_speak.assert_not_called()
        self.assertEqual(va.get_status()["voice_response"], "")

    def test_busy_is_reported_so_the_caller_can_fall_back(self):
        # Ignoring this return leaves the session in BUSY for SESSION_BUSY_MAX_S — 120 s of a robot
        # that looks hung.
        va = make_assistant()
        va._status = "thinking"
        self.assertIn("error", va.say("hello", epoch=va.epoch))

    def test_unversioned_say_behaves_exactly_as_before(self):
        va = make_assistant()
        va.bump_epoch()
        with patch.object(va, "_call_ollama", return_value="reply"), \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            self.assertEqual(va.say("hello"), {"status": "ok"})
        self.assertEqual(va.get_status()["voice_response"], "reply")

    def test_broken_callback_does_not_lose_the_reply(self):
        va = make_assistant()
        with patch.object(va, "_call_ollama", return_value="reply"), \
             patch("ai.voice_assistant.threading.Thread", _InlineThread), \
             patch("builtins.print"):
            va.say("hello", epoch=va.epoch,
                   on_done=lambda ep, o: (_ for _ in ()).throw(ValueError("bad")))
        self.assertEqual(va.get_status()["voice_response"], "reply")


def _info(language, probs=None, prob=0.9):
    """A stand-in for faster-whisper's TranscriptionInfo."""
    info = MagicMock()
    info.language = language
    info.language_probability = prob
    info.all_language_probs = probs
    return info


class TestBestAllowedLanguage(unittest.TestCase):
    def test_picks_the_highest_scoring_allowed_language(self):
        info = _info("cy", [("cy", 0.5), ("tl", 0.3), ("en", 0.1), ("nn", 0.05)])
        self.assertEqual(_best_allowed_language(info, ("en", "tl")), "tl")

    def test_ignores_disallowed_languages_however_confident(self):
        info = _info("nn", [("nn", 0.99), ("en", 0.001)])
        self.assertEqual(_best_allowed_language(info, ("en", "tl")), "en")

    def test_falls_back_to_the_first_allowed_when_probs_are_missing(self):
        # Older faster-whisper builds don't populate all_language_probs; guess nothing.
        self.assertEqual(_best_allowed_language(_info("cy", None), ("en", "tl")), "en")
        self.assertEqual(_best_allowed_language(_info("cy", []), ("tl", "en")), "tl")


class TestLanguageRestriction(unittest.TestCase):
    """Whisper picks from all 99 languages otherwise, and on short audio it is confidently wrong —
    measured on the robot: en 0.34, cy (Welsh) 0.22, nn (Norwegian Nynorsk) 0.21."""

    def _va(self, results):
        """results: list of (text, info) returned by successive transcribe() calls."""
        va = make_assistant()
        va._capture_rate = 16000
        va._whisper_model = MagicMock()
        calls = []

        beams = []
        prompts = []

        def transcribe(samples, language=None, vad_filter=True, beam_size=None,
                       initial_prompt=None):
            calls.append(language)
            beams.append(beam_size)
            prompts.append(initial_prompt)
            text, info = results[min(len(calls) - 1, len(results) - 1)]
            return ([make_segment(text)], info)

        va._whisper_model.transcribe.side_effect = transcribe
        va._recorded_beam_sizes = beams   # so the beam-width wiring can be asserted on
        va._recorded_prompts = prompts    # ditto for the decoder-bias wiring
        return va, calls

    def test_allowed_language_uses_the_first_pass_only(self):
        va, calls = self._va([("magandang umaga", _info("tl", [("tl", 0.8), ("en", 0.1)]))])
        with patch("builtins.print"):
            text = va._transcribe(np.ones((16000, 1), dtype="int16"), rate=16000)
        self.assertEqual(text, "magandang umaga")
        self.assertEqual(calls, [None], "an allowed detection must not cost a second pass")

    def test_english_also_uses_one_pass(self):
        va, calls = self._va([("what time is it", _info("en", [("en", 0.9)]))])
        with patch("builtins.print"):
            va._transcribe(np.ones((16000, 1), dtype="int16"), rate=16000)
        self.assertEqual(calls, [None])

    def test_configured_beam_size_reaches_both_passes(self):
        """Beam width is a turn-latency knob (greedy measured ~10% faster per turn), so it must not
        silently fall back to faster-whisper's default of 5 — least of all on the second pass."""
        va, calls = self._va([
            ("Rwy'n hoffi coffi", _info("cy", [("cy", 0.5), ("tl", 0.3), ("en", 0.1)])),
            ("gusto ko ng kape", _info("tl", [("tl", 0.9)])),
        ])
        with patch("builtins.print"):
            va._transcribe(np.ones((16000, 1), dtype="int16"), rate=16000)
        self.assertEqual(calls, [None, "tl"], "expected the two-pass path for this fixture")
        self.assertEqual(va._recorded_beam_sizes, [WHISPER_BEAM_SIZE, WHISPER_BEAM_SIZE])

    def test_decoder_bias_reaches_both_passes(self):
        """WHISPER_INITIAL_PROMPT is how "DEVCON" gets spelled correctly in the first place — a
        second pass that drops it hands the fuzzy matcher a mishearing to repair for nothing."""
        va, calls = self._va([
            ("Rwy'n hoffi coffi", _info("cy", [("cy", 0.5), ("tl", 0.3), ("en", 0.1)])),
            ("gusto ko ng kape", _info("tl", [("tl", 0.9)])),
        ])
        with patch("builtins.print"):
            va._transcribe(np.ones((16000, 1), dtype="int16"), rate=16000)
        self.assertEqual(va._recorded_prompts, [WHISPER_INITIAL_PROMPT, WHISPER_INITIAL_PROMPT])

    def test_the_scan_path_is_never_biased(self):
        # The wake tier runs on a weak model over overheard room noise, where priming it with
        # DEVCON vocabulary shows up as invented DEVCON talk. It only needs "hey kai".
        va, calls = self._va([("hey kai", _info("en", [("en", 0.9)]))])
        va._scan_model = va._whisper_model
        with patch("builtins.print"):
            va._transcribe(np.ones((16000, 1), dtype="int16"), rate=16000, scan=True)
        self.assertEqual(va._recorded_prompts, [None])

    def test_disallowed_language_is_retranscribed_as_the_best_allowed(self):
        va, calls = self._va([
            ("Rwy'n hoffi coffi", _info("cy", [("cy", 0.5), ("tl", 0.3), ("en", 0.1)])),
            ("gusto ko ng kape", _info("tl", [("tl", 0.9)])),
        ])
        with patch("builtins.print"):
            text = va._transcribe(np.ones((16000, 1), dtype="int16"), rate=16000)
        self.assertEqual(calls, [None, "tl"], "must redo the pass forced to the best allowed language")
        self.assertEqual(text, "gusto ko ng kape", "the Welsh transcript must not be kept")

    def test_norwegian_falls_back_to_english_when_that_scores_higher(self):
        va, calls = self._va([
            ("Han er ikke en annen", _info("nn", [("nn", 0.6), ("en", 0.2), ("tl", 0.01)])),
            ("I don't know what it is", _info("en", [("en", 0.9)])),
        ])
        with patch("builtins.print"):
            text = va._transcribe(np.ones((16000, 1), dtype="int16"), rate=16000)
        self.assertEqual(calls, [None, "en"])
        self.assertEqual(text, "I don't know what it is")

    def test_an_explicitly_forced_language_is_never_second_guessed(self):
        va, calls = self._va([("hello", _info("cy", [("cy", 0.9)]))])
        with patch("ai.voice_assistant.WHISPER_LANGUAGE", "en"), patch("builtins.print"):
            va._transcribe(np.ones((16000, 1), dtype="int16"), rate=16000)
        self.assertEqual(calls, ["en"])

    def test_the_scan_path_is_pinned_and_not_second_guessed(self):
        # The scan only has to spot two English words, and it is latency-critical.
        va, calls = self._va([("hey guys", _info("cy", [("cy", 0.9)]))])
        va._scan_model = va._whisper_model
        with patch("ai.voice_assistant.WAKE_WHISPER_SCAN_LANGUAGE", "en"), patch("builtins.print"):
            va._transcribe(np.ones((16000, 1), dtype="int16"), rate=16000, scan=True)
        self.assertEqual(calls, ["en"])

    def test_empty_allow_list_restores_full_auto_detect(self):
        va, calls = self._va([("Rwy'n hoffi coffi", _info("cy", [("cy", 0.9)]))])
        with patch("ai.voice_assistant.WHISPER_LANGUAGES", ()), patch("builtins.print"):
            text = va._transcribe(np.ones((16000, 1), dtype="int16"), rate=16000)
        self.assertEqual(calls, [None])
        self.assertEqual(text, "Rwy'n hoffi coffi")

    def test_missing_probs_still_produces_a_usable_transcript(self):
        va, calls = self._va([
            ("garble", _info("cy", None)),
            ("what time is it", _info("en", None)),
        ])
        with patch("builtins.print"):
            text = va._transcribe(np.ones((16000, 1), dtype="int16"), rate=16000)
        self.assertEqual(calls, [None, "en"])
        self.assertEqual(text, "what time is it")


class TestTurnId(unittest.TestCase):
    """The dashboard appends a chat bubble when voice_turn_id changes. It must move exactly once per
    completed turn and never otherwise — inferring the same thing from voice_status reaching "done"
    caused three separate duplicate-bubble races."""

    def setUp(self):
        p = patch("ai.voice_assistant.tts.enabled", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_starts_at_zero(self):
        self.assertEqual(make_assistant().get_status()["voice_turn_id"], 0)

    def test_increments_once_per_successful_turn(self):
        va = make_assistant()
        with patch.object(va, "_transcribe", return_value="hi"), \
             patch.object(va, "_call_ollama", return_value="hello"):
            va._process(np.ones((10, 1), dtype="int16"))
        self.assertEqual(va.get_status()["voice_turn_id"], 1)
        with patch.object(va, "_transcribe", return_value="again"), \
             patch.object(va, "_call_ollama", return_value="sure"):
            va._process(np.ones((10, 1), dtype="int16"))
        self.assertEqual(va.get_status()["voice_turn_id"], 2)

    def test_increments_for_the_no_speech_reply(self):
        # That reply is shown to the user, so it is a turn as far as the chat log is concerned.
        va = make_assistant()
        with patch.object(va, "_transcribe", return_value="   "):
            va._process(np.ones((10, 1), dtype="int16"))
        self.assertEqual(va.get_status()["voice_turn_id"], 1)

    def test_increments_for_a_say_turn(self):
        va = make_assistant()
        with patch.object(va, "_call_ollama", return_value="reply"), \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va.say("hello")
        self.assertEqual(va.get_status()["voice_turn_id"], 1)

    def test_does_not_increment_on_error(self):
        va = make_assistant()
        with patch.object(va, "_transcribe", side_effect=RuntimeError("whisper died")):
            va._process(np.ones((10, 1), dtype="int16"))
        self.assertEqual(va.get_status()["voice_turn_id"], 0)

    def test_does_not_increment_for_a_stale_turn(self):
        va = make_assistant()
        epoch = va.epoch

        def ollama(text):
            va.bump_epoch()
            return "a reply"

        with patch.object(va, "_transcribe", return_value="hi"), \
             patch.object(va, "_call_ollama", side_effect=ollama):
            va._process(np.ones((10, 1), dtype="int16"), epoch=epoch)
        self.assertEqual(va.get_status()["voice_turn_id"], 0)

    def test_is_unaffected_by_status_churn(self):
        # The whole point: clearing/reprojecting status must never look like a new turn.
        va = make_assistant()
        with patch.object(va, "_transcribe", return_value="hi"), \
             patch.object(va, "_call_ollama", return_value="hello"):
            va._process(np.ones((10, 1), dtype="int16"))
        before = va.get_status()["voice_turn_id"]
        for _ in range(5):
            va.clear_turn_status()
            va._status = STATUS_DONE
            va.clear_turn_status()
        self.assertEqual(va.get_status()["voice_turn_id"], before)


class TestClearTurnStatus(unittest.TestCase):
    """The dashboard posts a chat bubble on the transition INTO "done". A stale "done" surfacing when
    a hands-free session ends therefore duplicates the last question and answer."""

    def test_retires_done_to_idle(self):
        va = make_assistant()
        va._status = STATUS_DONE
        va.clear_turn_status()
        self.assertEqual(va.get_status()["voice_status"], STATUS_IDLE)

    def test_retires_error_too(self):
        va = make_assistant()
        va._status = STATUS_ERROR
        va.clear_turn_status()
        self.assertEqual(va.get_status()["voice_status"], STATUS_IDLE)

    def test_keeps_the_transcript_and_response_for_display(self):
        va = make_assistant()
        va._status, va._transcript, va._response = STATUS_DONE, "what time is it", "It's 3pm"
        va.clear_turn_status()
        status = va.get_status()
        self.assertEqual(status["voice_transcript"], "what time is it")
        self.assertEqual(status["voice_response"], "It's 3pm")

    def test_does_not_disturb_an_in_flight_turn(self):
        va = make_assistant()
        for live in ("recording", "transcribing", "thinking"):
            va._status = live
            va.clear_turn_status()
            self.assertEqual(va.get_status()["voice_status"], live)


class _FakeMic:
    """Stand-in for the shared always-open stream owned by ai/session.py."""

    def __init__(self, audio=None, rate=16000, arm_ok=True):
        self.audio = np.zeros((0, 1), dtype="int16") if audio is None else audio
        self.rate = rate
        self.arm_ok = arm_ok
        self.armed = 0
        self.harvested = 0
        self.last_preroll = None

    def arm_utterance(self, preroll=False):
        self.last_preroll = preroll
        if self.arm_ok:
            self.armed += 1
        return self.arm_ok

    def harvest_utterance(self):
        self.harvested += 1
        return self.audio, self.rate


class TestSharedMicCapture(unittest.TestCase):
    """With a session owning the stream, VoiceAssistant must never open one of its own — the raw I2S
    hw device admits a single opener, and a second one fails as intermittent silent capture."""

    def test_start_recording_arms_the_shared_mic_and_opens_no_stream(self):
        va = make_assistant()
        mic = _FakeMic()
        va.attach_mic(mic)
        with patch("ai.voice_assistant.sd.InputStream") as mock_stream_cls, \
             patch("ai.voice_assistant.tts.stop"):
            result = va.start_recording()
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(mic.armed, 1)
        self.assertTrue(mic.last_preroll)
        mock_stream_cls.assert_not_called()
        self.assertEqual(va.get_status()["voice_status"], STATUS_RECORDING)

    def test_start_recording_skips_device_resolution_entirely(self):
        va = make_assistant()
        va.attach_mic(_FakeMic())
        with patch("ai.voice_assistant.resolve_input_device") as mock_resolve, \
             patch("ai.voice_assistant.tts.stop"):
            va.start_recording()
        mock_resolve.assert_not_called()

    def test_arm_failure_reports_the_same_error_shape_as_before(self):
        va = make_assistant()
        va.attach_mic(_FakeMic(arm_ok=False))
        with patch("ai.voice_assistant.tts.stop"):
            result = va.start_recording()
        self.assertIn("error", result)
        self.assertEqual(va.get_status()["voice_status"], STATUS_ERROR)

    def test_stop_recording_harvests_and_passes_the_rate_through(self):
        va = make_assistant()
        mic = _FakeMic(audio=np.ones(1000, dtype="int16"), rate=16000)
        va.attach_mic(mic)
        with patch("ai.voice_assistant.tts.stop"):
            va.start_recording()
        with patch("ai.voice_assistant.threading.Thread") as mock_thread_cls:
            mock_thread_cls.return_value = MagicMock()
            va.stop_recording()
        self.assertEqual(mic.harvested, 1)
        self.assertEqual(mock_thread_cls.call_args.kwargs["kwargs"]["rate"], 16000)

    def test_stop_recording_never_touches_the_shared_stream(self):
        va = make_assistant()
        va.attach_mic(_FakeMic())
        sentinel = MagicMock()
        va._stream = sentinel   # a session's stream must survive a push-to-talk stop
        with patch("ai.voice_assistant.tts.stop"):
            va.start_recording()
        with patch("ai.voice_assistant.threading.Thread", return_value=MagicMock()):
            va.stop_recording()
        sentinel.close.assert_not_called()
        sentinel.stop.assert_not_called()


class TestLegacyCaptureIsBounded(unittest.TestCase):
    def test_buffer_stops_growing_past_the_hard_cap(self):
        # Nothing auto-stops a push-to-talk recording, so a stuck button used to grow this forever.
        from config.wake import CAPTURE_HARD_CAP_S

        va = make_assistant()
        va._capture_rate = 16000
        chunk = np.zeros((16000, 1), dtype="int16")   # 1 s each
        for _ in range(int(CAPTURE_HARD_CAP_S) + 30):
            va._on_audio_chunk(chunk, len(chunk), None, None)
        self.assertLessEqual(va._audio_samples, int(CAPTURE_HARD_CAP_S * 16000) + len(chunk))

    def test_keeps_the_most_recent_audio(self):
        va = make_assistant()
        va._capture_rate = 100   # tiny cap so the trim is easy to observe
        for value in range(60):
            va._on_audio_chunk(np.full((100, 1), value, dtype="int16"), 100, None, None)
        newest = va._audio_chunks[-1]
        self.assertEqual(int(newest[0, 0]), 59)


class TestEpoch(unittest.TestCase):
    def test_starts_at_zero_and_increments(self):
        va = make_assistant()
        self.assertEqual(va.epoch, 0)
        self.assertEqual(va.bump_epoch(), 1)
        self.assertEqual(va.epoch, 1)

    def test_none_is_always_current(self):
        va = make_assistant()
        va.bump_epoch()
        self.assertTrue(va._epoch_ok(None), "legacy unversioned callers must keep working")

    def test_stale_epoch_is_detected(self):
        va = make_assistant()
        stale = va.epoch
        va.bump_epoch()
        self.assertFalse(va._epoch_ok(stale))

    def test_reset_history_invalidates_in_flight_turns(self):
        # Otherwise a reply already inside _call_ollama appends itself after the clear, handing the
        # next person one turn of the last person's conversation.
        va = make_assistant()
        va._history = [{"role": "user", "content": "secret"}]
        epoch = va.epoch
        va.reset_history()
        self.assertEqual(va._history, [])
        self.assertFalse(va._epoch_ok(epoch))


class TestProcessEpochGuards(unittest.TestCase):
    def setUp(self):
        p = patch("ai.voice_assistant.tts.enabled", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def _assistant(self, transcript="hi"):
        va = make_assistant()
        va._whisper_model = MagicMock()
        va._whisper_model.transcribe.return_value = ([make_segment(transcript)], None)
        return va

    def test_stale_after_transcription_writes_nothing(self):
        va = self._assistant()
        epoch = va.epoch

        def transcribe(audio, rate=None):
            va.bump_epoch()   # a session reset lands while Whisper is running
            return "hello"

        with patch.object(va, "_transcribe", side_effect=transcribe), \
             patch.object(va, "_call_ollama") as mock_ollama:
            va._process(np.ones((10, 1), dtype="int16"), epoch=epoch)
        mock_ollama.assert_not_called()
        self.assertEqual(va.get_status()["voice_transcript"], "")

    def test_stale_after_ollama_does_not_append_history(self):
        va = self._assistant()
        epoch = va.epoch

        def ollama(text):
            va.bump_epoch()   # ~50 s cold load is plenty of time for the session to end
            return "a reply"

        with patch.object(va, "_transcribe", return_value="hello"), \
             patch.object(va, "_call_ollama", side_effect=ollama), \
             patch.object(va, "_speak") as mock_speak:
            va._process(np.ones((10, 1), dtype="int16"), epoch=epoch)
        self.assertEqual(va._history, [])
        mock_speak.assert_not_called()
        self.assertEqual(va.get_status()["voice_response"], "")

    def test_current_epoch_completes_normally(self):
        va = self._assistant()
        with patch.object(va, "_transcribe", return_value="hello"), \
             patch.object(va, "_call_ollama", return_value="a reply"):
            va._process(np.ones((10, 1), dtype="int16"), epoch=va.epoch)
        self.assertEqual(va.get_status()["voice_response"], "a reply")
        self.assertEqual(len(va._history), 2)

    def test_rate_is_forwarded_to_transcribe(self):
        va = self._assistant()
        with patch.object(va, "_transcribe", return_value="") as mock_tr:
            va._process(np.ones((10, 1), dtype="int16"), rate=48000)
        self.assertEqual(mock_tr.call_args.kwargs["rate"], 48000)


class TestOnDoneCallback(unittest.TestCase):
    def setUp(self):
        p = patch("ai.voice_assistant.tts.enabled", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def _run_turn(self, **transcribe_kw):
        # NB: not named _outcome — unittest.TestCase already owns that attribute.
        va = make_assistant()
        seen = []
        with patch.object(va, "_transcribe", **transcribe_kw), \
             patch.object(va, "_call_ollama", return_value="r"):
            va._process(np.ones((10, 1), dtype="int16"), epoch=va.epoch,
                        on_done=lambda ep, outcome: seen.append(outcome))
        return va, seen

    def test_reports_done_on_a_real_reply(self):
        _, seen = self._run_turn(return_value="hello")
        self.assertEqual(seen, ["done"])

    def test_reports_empty_on_no_transcript(self):
        _, seen = self._run_turn(return_value="   ")
        self.assertEqual(seen, ["empty"])

    def test_reports_error_on_failure(self):
        _, seen = self._run_turn(side_effect=RuntimeError("whisper died"))
        self.assertEqual(seen, ["error"])

    def test_reports_stale_when_the_session_moved_on(self):
        va = make_assistant()
        seen = []
        epoch = va.epoch
        with patch.object(va, "_transcribe", side_effect=lambda a, rate=None: va.bump_epoch() and "hi"):
            va._process(np.ones((10, 1), dtype="int16"), epoch=epoch,
                        on_done=lambda ep, outcome: seen.append(outcome))
        self.assertEqual(seen, ["stale"])

    def test_empty_transcript_is_not_spoken_twice(self):
        # The session speaks its own cached "didn't catch that", so _process must not also say it.
        va = make_assistant()
        with patch.object(va, "_transcribe", return_value=""), \
             patch.object(va, "_speak") as mock_speak:
            va._process(np.ones((10, 1), dtype="int16"), on_done=lambda ep, o: None)
        mock_speak.assert_not_called()

    def test_empty_transcript_is_still_spoken_without_a_session(self):
        va = make_assistant()
        with patch.object(va, "_transcribe", return_value=""), \
             patch.object(va, "_speak") as mock_speak:
            va._process(np.ones((10, 1), dtype="int16"))
        mock_speak.assert_called_once()

    def test_broken_callback_does_not_kill_the_turn_thread(self):
        va = make_assistant()
        with patch.object(va, "_transcribe", return_value="hello"), \
             patch.object(va, "_call_ollama", return_value="r"), \
             patch("builtins.print"):
            va._process(np.ones((10, 1), dtype="int16"),
                        on_done=lambda ep, o: (_ for _ in ()).throw(ValueError("bad")))
        self.assertEqual(va.get_status()["voice_response"], "r")


class TestProcessUtterance(unittest.TestCase):
    def test_spawns_a_worker_with_the_epoch_and_rate(self):
        va = make_assistant()
        with patch("ai.voice_assistant.threading.Thread") as mock_thread_cls:
            mock_thread_cls.return_value = MagicMock()
            result = va.process_utterance(np.ones(100, dtype="int16"), rate=16000, epoch=7)
        self.assertEqual(result, {"status": "ok"})
        kwargs = mock_thread_cls.call_args.kwargs["kwargs"]
        self.assertEqual(kwargs["rate"], 16000)
        self.assertEqual(kwargs["epoch"], 7)

    def test_rejected_while_thinking(self):
        va = make_assistant()
        va._status = "thinking"
        result = va.process_utterance(np.ones(100, dtype="int16"), rate=16000)
        self.assertIn("error", result)

    def test_clears_the_previous_turns_text(self):
        va = make_assistant()
        va._transcript, va._response, va._error = "old", "old", "old"
        with patch("ai.voice_assistant.threading.Thread", return_value=MagicMock()):
            va.process_utterance(np.ones(100, dtype="int16"), rate=16000)
        status = va.get_status()
        self.assertEqual(status["voice_transcript"], "")
        self.assertEqual(status["voice_response"], "")
        self.assertEqual(status["voice_error"], "")


class TestMicMuted(unittest.TestCase):
    """The self-hearing gate. With the mic always open, this is the only thing between Kai and
    answering his own reply."""

    def setUp(self):
        for name, kw in (("is_playing", {"return_value": False}),
                         ("quiet_since", {"return_value": float("inf")})):
            p = patch(f"ai.voice_assistant.tts.{name}", **kw)
            setattr(self, f"mock_{name}", p.start())
            self.addCleanup(p.stop)

    def test_open_when_nothing_is_happening(self):
        self.assertFalse(make_assistant().mic_muted(now=100.0))

    def test_muted_while_playback_runs(self):
        self.mock_is_playing.return_value = True
        self.assertTrue(make_assistant().mic_muted(now=100.0))

    def test_muted_during_the_settle_tail(self):
        from config.wake import TTS_TAIL_MUTE_S

        self.mock_quiet_since.return_value = TTS_TAIL_MUTE_S / 2
        self.assertTrue(make_assistant().mic_muted(now=100.0),
                        "paplay exits before the amp goes quiet — the tail covers that gap")

    def test_open_once_the_tail_expires(self):
        from config.wake import TTS_TAIL_MUTE_S

        self.mock_quiet_since.return_value = TTS_TAIL_MUTE_S + 0.01
        self.assertFalse(make_assistant().mic_muted(now=100.0))

    def test_muted_during_synthesis_before_any_audio_exists(self):
        # The window voice_speaking cannot see: the jaw envelope is only set after synth returns.
        va = make_assistant()
        va._tts_active = True
        self.assertTrue(va.mic_muted(now=100.0))

    def test_muted_until_the_computed_audio_end(self):
        va = make_assistant()
        va._gate_until = 150.0
        self.assertTrue(va.mic_muted(now=149.9))
        self.assertFalse(va.mic_muted(now=150.1))

    def test_speak_gates_before_the_worker_runs(self):
        va = make_assistant()
        with patch("ai.voice_assistant.tts.enabled", return_value=True), \
             patch("ai.voice_assistant.threading.Thread") as mock_thread_cls:
            mock_thread_cls.return_value = MagicMock()   # worker never runs
            va._speak("Hello there.")
        self.assertTrue(va._tts_active, "the gate must close before Piper is even started")

    def test_speak_sets_the_gate_from_the_wav_duration(self):
        from config.wake import TTS_TAIL_MUTE_S

        va = make_assistant()
        with patch("ai.voice_assistant.tts.enabled", return_value=True), \
             patch("ai.voice_assistant.tts.stop"), \
             patch("ai.voice_assistant.tts.synthesize", return_value="/tmp/kai_tts.wav"), \
             patch("ai.voice_assistant.tts.wav_duration", return_value=4.0), \
             patch("ai.voice_assistant.tts.play"), \
             patch("ai.voice_assistant.time.monotonic", return_value=100.0), \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va._speak("One two three.")
        self.assertAlmostEqual(va._gate_until, 100.0 + 4.0 + TTS_TAIL_MUTE_S)

    def test_gate_is_released_even_when_the_worker_bails_out(self):
        va = make_assistant()
        with patch("ai.voice_assistant.tts.enabled", return_value=True), \
             patch("ai.voice_assistant.tts.stop"), \
             patch("ai.voice_assistant.tts.synthesize", side_effect=RuntimeError("piper died")), \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            with self.assertRaises(RuntimeError):
                va._speak("Hello.")
        self.assertFalse(va._tts_active, "a stuck gate would deafen Kai permanently")


class TestSpeakStaleEpoch(unittest.TestCase):
    def test_abandoned_reply_is_never_played(self):
        # The bug this closes: synthesis takes 0.5-1.5 s, and tts.stop() alone only killed playback,
        # so a worker cancelled mid-Piper still spoke into whatever session came next.
        va = make_assistant()
        epoch = va.epoch

        def synth(text):
            va.bump_epoch()
            return "/tmp/kai_tts.wav"

        with patch("ai.voice_assistant.tts.enabled", return_value=True), \
             patch("ai.voice_assistant.tts.stop"), \
             patch("ai.voice_assistant.tts.synthesize", side_effect=synth), \
             patch("ai.voice_assistant.tts.play") as mock_play, \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va._speak("A reply nobody is waiting for.", epoch=epoch)
        mock_play.assert_not_called()
        self.assertIsNone(va._speak_start, "and the jaw must not animate either")

    def test_current_epoch_still_plays(self):
        va = make_assistant()
        with patch("ai.voice_assistant.tts.enabled", return_value=True), \
             patch("ai.voice_assistant.tts.stop"), \
             patch("ai.voice_assistant.tts.synthesize", return_value="/tmp/kai_tts.wav"), \
             patch("ai.voice_assistant.tts.wav_duration", return_value=2.0), \
             patch("ai.voice_assistant.tts.play") as mock_play, \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va._speak("A wanted reply.", epoch=va.epoch)
        mock_play.assert_called_once()

    def test_unversioned_speak_always_plays(self):
        va = make_assistant()
        va.bump_epoch()
        with patch("ai.voice_assistant.tts.enabled", return_value=True), \
             patch("ai.voice_assistant.tts.stop"), \
             patch("ai.voice_assistant.tts.synthesize", return_value="/tmp/kai_tts.wav"), \
             patch("ai.voice_assistant.tts.wav_duration", return_value=2.0), \
             patch("ai.voice_assistant.tts.play") as mock_play, \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va._speak("Verbatim say() path.")
        mock_play.assert_called_once()


class TestSpeechOwnership(unittest.TestCase):
    """One speaker, one _tts_active boolean, and workers that finish out of order.

    Heard on the robot 2026-08-09 as "all the fillers are being spoken at once". A 7.5 s filler
    opener was still playing when the 2.8 s reply started; the opener's worker then cleared
    _tts_active out from under the reply. speech_in_flight() went False mid-answer, which dropped
    SPEAKING straight to COOLDOWN and told the filler loop nothing was playing, so it started more
    lines on top of the answer.
    """

    def test_a_late_worker_does_not_report_silence_for_a_newer_line(self):
        va = make_assistant()
        with patch("ai.voice_assistant.tts.stop"):
            first = va._begin_speech()
            second = va._begin_speech()
            va._end_speech(first)                  # the older line finishes last
            self.assertTrue(va.speech_in_flight(),
                            "an older line clearing the flag makes a live answer look finished")
            va._end_speech(second)
            self.assertFalse(va.speech_in_flight())

    def test_starting_a_line_cuts_whatever_is_still_playing(self):
        # tts.play() starts a SECOND paplay rather than replacing the first, so without this a
        # filler that outlives its turn keeps talking straight over the answer it was covering.
        va = make_assistant()
        with patch("ai.voice_assistant.tts.stop") as mock_stop:
            va._begin_speech()
        mock_stop.assert_called_once()

    def test_the_reply_cuts_a_filler_that_is_still_going(self):
        # The end-to-end shape, through the two public speech paths rather than the helpers.
        va = make_assistant()
        with patch("ai.voice_assistant.tts.wav_duration", return_value=7.5), \
             patch("ai.voice_assistant.tts.play"), \
             patch("ai.voice_assistant.tts.stop") as mock_stop, \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va._speak_wav("/tmp/kai_canned_filler_op_tl_0.wav", "opener", epoch=va.epoch)
            mock_stop.reset_mock()
            va._speak_wav("/tmp/kai_canned_ack.wav", "Yes?", epoch=va.epoch)
        mock_stop.assert_called_once()


class TestSpeakWav(unittest.TestCase):
    """The cached wake acknowledgement."""

    def test_plays_the_cached_file_and_syncs_the_jaw(self):
        va = make_assistant()
        with patch("ai.voice_assistant.tts.wav_duration", return_value=0.6), \
             patch("ai.voice_assistant.tts.play") as mock_play, \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va._speak_wav("/tmp/kai_ack/kai_canned_ack.wav", "Yes?", epoch=va.epoch)
        mock_play.assert_called_once_with("/tmp/kai_ack/kai_canned_ack.wav")
        self.assertIsNotNone(va._speak_start)
        self.assertAlmostEqual(va._speak_segments[-1][1], 0.6, places=6)

    def test_does_not_synthesize_anything(self):
        va = make_assistant()
        with patch("ai.voice_assistant.tts.synthesize") as mock_synth, \
             patch("ai.voice_assistant.tts.wav_duration", return_value=0.6), \
             patch("ai.voice_assistant.tts.play"), \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va._speak_wav("/tmp/x.wav", "Yes?")
        mock_synth.assert_not_called()

    def test_leaves_status_and_response_untouched(self):
        # Routing the ack through say() would post a "Kai: Yes?" chat bubble on every single wake.
        va = make_assistant()
        with patch("ai.voice_assistant.tts.wav_duration", return_value=0.6), \
             patch("ai.voice_assistant.tts.play"), \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va._speak_wav("/tmp/x.wav", "Yes?")
        status = va.get_status()
        self.assertEqual(status["voice_status"], STATUS_IDLE)
        self.assertEqual(status["voice_response"], "")

    def test_stale_epoch_is_silent(self):
        va = make_assistant()
        epoch = va.epoch
        va.bump_epoch()
        with patch("ai.voice_assistant.tts.wav_duration", return_value=0.6), \
             patch("ai.voice_assistant.tts.play") as mock_play, \
             patch("ai.voice_assistant.threading.Thread", _InlineThread):
            va._speak_wav("/tmp/x.wav", "Yes?", epoch=epoch)
        mock_play.assert_not_called()


if __name__ == '__main__':
    unittest.main()
