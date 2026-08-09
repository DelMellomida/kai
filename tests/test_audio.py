import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from ai import audio
from ai.audio import (
    CaptureBuffer, Decimator, FrameAssembler, HighPass, OpenWakeWordEngine, PorcupineEngine,
    RingPreroll, SpeechGate, WakeDetector, WhisperWakeEngine,
    design_highpass, design_lowpass, keyword_paths, normalize_for_asr, resolve_access_key, rms,
)
from config.voice import (
    ASR_NORMALIZE_MAX_GAIN, ASR_NORMALIZE_MIN_RMS, ASR_NORMALIZE_PEAK_CEILING,
    ASR_NORMALIZE_TARGET_RMS,
)
from config.wake import WAKE_AMBIENT_MAX_LIFT


def _noise(n, amp=3000, seed=0):
    return (np.random.default_rng(seed).standard_normal(n) * amp).astype(np.int16)


def _tone(freq, n, fs=48000, amp=8000):
    return (np.sin(2 * np.pi * freq * np.arange(n) / fs) * amp).astype(np.int16)


def _silence(n):
    return np.zeros(n, dtype=np.int16)


class TestRms(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(rms(np.zeros(0, dtype=np.int16)), 0.0)

    def test_silence_is_zero(self):
        self.assertEqual(rms(_silence(100)), 0.0)

    def test_constant_equals_magnitude(self):
        self.assertAlmostEqual(rms(np.full(64, -1000, dtype=np.int16)), 1000.0)

    def test_no_overflow_at_int16_extremes(self):
        # int16 arithmetic would wrap here; rms must promote before squaring.
        self.assertAlmostEqual(rms(np.full(16, -32768, dtype=np.int16)), 32768.0)


class TestDesignLowpass(unittest.TestCase):
    def test_unity_dc_gain(self):
        self.assertAlmostEqual(design_lowpass(97, 7200, 48000).sum(), 1.0)

    def test_forces_odd_tap_count_for_linear_phase(self):
        self.assertEqual(len(design_lowpass(96, 7200, 48000)), 97)

    def test_symmetric(self):
        h = design_lowpass(97, 7200, 48000)
        np.testing.assert_allclose(h, h[::-1], atol=1e-15)


class TestDecimatorContinuity(unittest.TestCase):
    """The bug class this guards: a stateless per-block resampler zero-pads each block's edges, so
    the stream picks up a filter discontinuity every 32 ms. Output length looks right and the wake
    word quietly gets worse."""

    def test_unequal_blocks_are_bit_identical_to_the_whole_stream(self):
        x = _noise(48000)
        whole = Decimator(48000, 16000).feed(x)

        d = Decimator(48000, 16000)
        parts, i = [], 0
        for n in (1536, 700, 1, 4096, 333, 1536, 9999, 2, 1535):
            parts.append(d.feed(x[i:i + n]))
            i += n
        split = np.concatenate(parts)

        m = min(len(whole), len(split))
        self.assertGreater(m, 1000)
        np.testing.assert_array_equal(whole[:m], split[:m])

    def test_block_length_not_a_multiple_of_the_ratio(self):
        # 100 samples per block is not divisible by 3: the decimation phase has to be carried, or
        # the picked samples drift off the grid.
        x = _noise(30000, seed=1)
        whole = Decimator(48000, 16000).feed(x)
        d = Decimator(48000, 16000)
        split = np.concatenate([d.feed(x[i:i + 100]) for i in range(0, len(x), 100)])
        m = min(len(whole), len(split))
        np.testing.assert_array_equal(whole[:m], split[:m])

    def test_single_sample_blocks(self):
        x = _noise(900, seed=2)
        whole = Decimator(48000, 16000).feed(x)
        d = Decimator(48000, 16000)
        split = np.concatenate([d.feed(x[i:i + 1]) for i in range(len(x))])
        np.testing.assert_array_equal(whole, split)

    def test_output_length_tracks_the_ratio(self):
        out = Decimator(48000, 16000).feed(_silence(1536))
        self.assertEqual(len(out), 512, "1536 in must give exactly one Porcupine frame")

    def test_empty_block_is_a_noop(self):
        d = Decimator(48000, 16000)
        self.assertEqual(len(d.feed(np.zeros(0, dtype=np.int16))), 0)
        np.testing.assert_array_equal(d.feed(_silence(1536)), _silence(512))

    def test_reset_clears_history(self):
        d = Decimator(48000, 16000)
        d.feed(_noise(4800, amp=20000))
        d.reset()
        np.testing.assert_array_equal(d.feed(_silence(1536)), _silence(512))

    def test_rejects_non_integer_ratio(self):
        with self.assertRaises(ValueError):
            Decimator(44100, 16000)

    def test_accepts_a_passthrough_ratio_of_one(self):
        d = Decimator(16000, 16000)
        self.assertEqual(d.ratio, 1)
        self.assertEqual(len(d.feed(_silence(320))), 320)


class TestDecimatorAntiAliasing(unittest.TestCase):
    def _out_rms(self, freq):
        out = Decimator(48000, 16000).feed(_tone(freq, 48000))
        return rms(out[500:])   # skip the filter's settling transient

    def test_speech_band_passes_intact(self):
        for freq in (440, 1000, 3000):
            self.assertGreater(self._out_rms(freq), 0.9 * 8000 / np.sqrt(2))

    def test_frequencies_that_would_alias_are_rejected(self):
        # Without the filter, 10 kHz folds to 6 kHz — right into the speech band, where it presents
        # as "the wake word model is bad" rather than as an aliasing problem.
        for freq in (10000, 14000, 20000):
            self.assertLess(self._out_rms(freq), 50.0)

    def test_gain_scales_output(self):
        quiet = Decimator(48000, 16000, gain=1.0).feed(_tone(1000, 48000))
        loud = Decimator(48000, 16000, gain=3.0).feed(_tone(1000, 48000))
        self.assertAlmostEqual(rms(loud[500:]) / rms(quiet[500:]), 3.0, places=1)

    def test_saturates_instead_of_wrapping(self):
        out = Decimator(48000, 16000, gain=10.0).feed(_tone(1000, 4800, amp=20000))
        self.assertLessEqual(int(out.max()), 32767)
        self.assertGreaterEqual(int(out.min()), -32768)


class TestHighPass(unittest.TestCase):
    """Removes the INMP441's DC offset and sub-100 Hz rumble from the audio Whisper transcribes.

    Nothing did this before: the anti-alias filter is low-pass only, and VAD_DC_BLOCK cleans the
    VAD's decision but not the stored audio. The offset on this mic is large enough that raw RMS
    reads ~2x the DC-blocked value, so it was comparable to the speech riding on it.
    """

    def _hp(self, cutoff=80, taps=255, rate=16000):
        return HighPass(cutoff_hz=cutoff, rate=rate, taps=taps)

    def _tone_blocks(self, freq, amp, fs=16000):
        """A block generator producing a continuous tone across calls (phase carried)."""
        phase = {"n": 0}

        def block(i, n):
            t = (np.arange(n) + phase["n"]) / fs
            phase["n"] += n
            return (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16)

        return block

    def _steady(self, hp, block_fn, blocks=12, n=512):
        """Feed several blocks and return the last, past the filter's ring-in."""
        out = None
        for i in range(blocks):
            out = hp.feed(block_fn(i, n))
        return out

    def test_dc_is_removed(self):
        # The headline case: a pure standing offset must come out as nothing.
        out = self._steady(self._hp(), lambda i, n: np.full(n, 3000, dtype=np.int16))
        self.assertLess(rms(out.astype(np.float64)), 30.0,
                        "a pure DC offset must not survive into the transcript")

    def test_speech_band_tone_passes_at_full_amplitude(self):
        amp = 6000.0
        out = self._steady(self._hp(), self._tone_blocks(400.0, amp))
        self.assertGreater(rms(out.astype(np.float64)), (amp / np.sqrt(2)) * 0.9)

    def test_rumble_is_attenuated(self):
        # 30 Hz chassis/fan rumble, well below the corner — must be cut hard.
        amp = 6000.0
        out = self._steady(self._hp(), self._tone_blocks(30.0, amp))
        self.assertLess(rms(out.astype(np.float64)), (amp / np.sqrt(2)) * 0.25)

    def test_offset_riding_on_speech_is_stripped_without_eating_the_speech(self):
        # The real signal shape on this mic: speech plus a large standing offset.
        fs, freq, amp, offset = 16000, 500.0, 4000.0, 5000.0
        phase = {"n": 0}

        def block(i, n):
            t = (np.arange(n) + phase["n"]) / fs
            phase["n"] += n
            return (np.sin(2 * np.pi * freq * t) * amp + offset).astype(np.int16)

        out = self._steady(self._hp(), block)
        self.assertLess(abs(float(np.mean(out))), 50.0, "the offset must be gone")
        self.assertGreater(rms(out.astype(np.float64)), (amp / np.sqrt(2)) * 0.9,
                           "and the speech must not be")

    def test_output_length_matches_input(self):
        # Overlap-save must be length-preserving, or every downstream frame count drifts.
        hp = self._hp()
        for n in (320, 512, 1536, 7):
            with self.subTest(n=n):
                self.assertEqual(hp.feed(np.zeros(n, dtype=np.int16)).size, n)

    def test_empty_block_is_safe(self):
        self.assertEqual(self._hp().feed(np.zeros(0, dtype=np.int16)).size, 0)

    def test_no_discontinuity_at_block_boundaries(self):
        """Overlap-save, for the same reason Decimator uses it: filtering each block independently
        zero-pads its edges and injects a step every 32 ms. A step is broadband, so it lands right
        in the speech band — the opposite of what this filter is for."""
        t = np.arange(4096) / 16000
        tone = (np.sin(2 * np.pi * 500.0 * t) * 6000.0).astype(np.int16)

        hp = self._hp()
        streamed = np.concatenate([hp.feed(tone[i:i + 512]) for i in range(0, tone.size, 512)])
        whole = self._hp().feed(tone)
        # Identical either way — that is what "exact across block boundaries" means.
        np.testing.assert_array_equal(streamed, whole)

    def test_reset_clears_the_tail(self):
        hp = self._hp()
        hp.feed(np.full(512, 8000, dtype=np.int16))
        hp.reset()
        np.testing.assert_array_equal(hp.feed(np.zeros(512, dtype=np.int16)),
                                      np.zeros(512, dtype=np.int16))

    def test_saturates_rather_than_wraps(self):
        # _to_int16 clips; a wrap would turn a loud transient into a full-scale sign flip.
        loud = np.full(512, 32767, dtype=np.int16)
        loud[::2] = -32768
        out = self._hp().feed(loud)
        self.assertGreaterEqual(int(out.min()), -32768)
        self.assertLessEqual(int(out.max()), 32767)


class TestDesignHighpass(unittest.TestCase):
    def test_dc_gain_is_exactly_zero(self):
        h = design_highpass(255, 80, 16000)
        self.assertAlmostEqual(float(h.sum()), 0.0, places=6,
                               msg="the sum of the taps IS the DC gain; it must be zero")

    def test_forces_odd_taps_for_linear_phase(self):
        self.assertEqual(design_highpass(254, 80, 16000).size, 255)

    def test_is_symmetric(self):
        h = design_highpass(255, 80, 16000)
        np.testing.assert_allclose(h, h[::-1], atol=1e-12)


class TestFrameAssembler(unittest.TestCase):
    def test_chunk_smaller_than_a_frame_yields_nothing_yet(self):
        fa = FrameAssembler(512)
        self.assertEqual(fa.push(_silence(100)), [])
        self.assertEqual(fa.pending, 100)

    def test_exact_frame(self):
        frames = FrameAssembler(512).push(_silence(512))
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(frames[0]), 512)

    def test_many_frames_from_one_push(self):
        fa = FrameAssembler(512)
        frames = fa.push(_silence(512 * 10 + 7))
        self.assertEqual(len(frames), 10)
        self.assertEqual(fa.pending, 7)

    def test_frames_emerge_across_pushes_without_losing_samples(self):
        fa = FrameAssembler(320)
        x = np.arange(320 * 5, dtype=np.int16)
        out = []
        for i in range(0, len(x), 97):
            out.extend(fa.push(x[i:i + 97]))
        self.assertEqual(len(out), 5)
        np.testing.assert_array_equal(np.concatenate(out), x)

    def test_reset_drops_the_partial_frame(self):
        fa = FrameAssembler(512)
        fa.push(_silence(300))
        fa.reset()
        self.assertEqual(fa.pending, 0)
        self.assertEqual(fa.push(_silence(300)), [])


class TestRingPreroll(unittest.TestCase):
    def test_holds_at_most_its_capacity(self):
        ring = RingPreroll(0.3, 16000)   # 4800 samples
        for _ in range(20):
            ring.push(_silence(1000))
        self.assertLessEqual(ring.samples, 4800 + 1000)
        self.assertLessEqual(len(ring.take()), 4800)

    def test_keeps_the_most_recent_audio(self):
        ring = RingPreroll(0.1, 16000)   # 1600 samples
        ring.push(np.full(1600, 111, dtype=np.int16))
        ring.push(np.full(1600, 222, dtype=np.int16))
        out = ring.take()
        self.assertEqual(len(out), 1600)
        self.assertTrue(np.all(out == 222), "the pre-roll must be the audio just before onset")

    def test_take_drains(self):
        ring = RingPreroll(0.3, 16000)
        ring.push(_silence(1000))
        self.assertEqual(len(ring.take()), 1000)
        self.assertEqual(len(ring.take()), 0)

    def test_zero_capacity_is_a_noop(self):
        ring = RingPreroll(0.0, 16000)
        ring.push(_silence(1000))
        self.assertEqual(len(ring.take()), 0)


class TestCaptureBuffer(unittest.TestCase):
    def test_accumulates_and_reports_seconds(self):
        buf = CaptureBuffer(16000, cap_s=20.0)
        buf.push(_silence(16000))
        self.assertEqual(buf.samples, 16000)
        self.assertAlmostEqual(buf.seconds, 1.0)
        self.assertFalse(buf.truncated)

    def test_hard_cap_drops_oldest_and_flags_truncation(self):
        # This is the bound that stops an always-open mic from exhausting the Jetson's 8 GB when the
        # session FSM wedges — so it is enforced here, not in the FSM.
        buf = CaptureBuffer(16000, cap_s=1.0)
        for _ in range(10):
            buf.push(_silence(8000))
        self.assertLessEqual(buf.samples, 16000)
        self.assertTrue(buf.truncated)

    def test_prepend_puts_preroll_in_front(self):
        buf = CaptureBuffer(16000, cap_s=20.0)
        buf.push(np.full(100, 222, dtype=np.int16))
        buf.prepend(np.full(50, 111, dtype=np.int16))
        out = buf.take()
        self.assertEqual(len(out), 150)
        self.assertTrue(np.all(out[:50] == 111))
        self.assertTrue(np.all(out[50:] == 222))

    def test_prepend_over_the_cap_trims_the_tail_keeping_the_onset(self):
        buf = CaptureBuffer(16000, cap_s=1.0)
        buf.push(np.full(16000, 222, dtype=np.int16))
        buf.prepend(np.full(4800, 111, dtype=np.int16))
        out = buf.take()
        self.assertTrue(np.all(out[:4800] == 111), "the speech onset must survive the trim")
        self.assertTrue(buf.truncated is False, "take() resets, so truncated resets with it")

    def test_take_drains_and_clears_truncation(self):
        buf = CaptureBuffer(16000, cap_s=1.0)
        for _ in range(5):
            buf.push(_silence(8000))
        buf.take()
        self.assertEqual(buf.samples, 0)
        self.assertFalse(buf.truncated)
        self.assertEqual(len(buf.take()), 0)

    def test_empty_take_is_a_mono_int16_array(self):
        out = CaptureBuffer(16000).take()
        self.assertEqual(out.dtype, np.int16)
        self.assertEqual(out.ndim, 1)


class TestSpeechGate(unittest.TestCase):
    """No webrtcvad on a dev box, so these exercise the RMS-floor path plus a fake VAD. The
    transitions are the same either way — that's the point of the AND."""

    def _gate(self, **kw):
        kw.setdefault("rms_floor", 1000.0)
        kw.setdefault("onset_frames", 3)
        kw.setdefault("hangover_s", 0.8)
        # Default the hold floor to the open floor so existing cases keep their single-threshold
        # behaviour; hysteresis is exercised explicitly by the tests that pass it.
        kw.setdefault("rms_floor_hold", kw["rms_floor"])
        return SpeechGate(rate=16000, frame_ms=20, **kw)

    def _feed(self, gate, block_fn, count, t0=100.0):
        """Feed `count` 20 ms frames one at a time, returning the events seen."""
        events = []
        for i in range(count):
            ev = gate.update(block_fn(i), t0 + (i + 1) * 0.02)
            if ev:
                events.append((ev, t0 + (i + 1) * 0.02))
        return events

    def test_rejects_invalid_frame_ms(self):
        with self.assertRaises(ValueError):
            SpeechGate(frame_ms=25)

    def test_silence_never_triggers(self):
        gate = self._gate()
        self.assertEqual(self._feed(gate, lambda i: _silence(320), 100), [])
        self.assertEqual(gate.state, SpeechGate.IDLE)

    def test_room_noise_below_the_floor_never_triggers(self):
        # Raw-device ambient measures ~900; the floor sits well above it.
        gate = self._gate(rms_floor=2500.0)
        self.assertEqual(self._feed(gate, lambda i: _noise(320, amp=900, seed=i), 200), [])

    def test_onset_requires_consecutive_frames(self):
        gate = self._gate(onset_frames=3)
        # loud, quiet, loud, quiet — never three in a row
        events = self._feed(gate, lambda i: _noise(320, amp=6000) if i % 2 == 0 else _silence(320), 20)
        self.assertEqual(events, [], "a single-frame blip (fan, amp click) must not open an utterance")

    def test_onset_fires_on_the_third_consecutive_frame(self):
        gate = self._gate(onset_frames=3)
        events = self._feed(gate, lambda i: _noise(320, amp=6000, seed=i), 3)
        self.assertEqual([e for e, _ in events], ["onset"])
        self.assertEqual(gate.state, SpeechGate.SPEECH)
        self.assertEqual(gate.onsets, 1)

    def test_onset_is_credited_to_where_speech_actually_began(self):
        gate = self._gate(onset_frames=3)
        self._feed(gate, lambda i: _noise(320, amp=6000, seed=i), 3, t0=100.0)
        # Frames end at 100.02/.04/.06; speech really started at the first of them.
        self.assertAlmostEqual(gate.speech_duration(100.06), 0.04, places=4)

    def test_hangover_ends_the_turn_after_trailing_silence(self):
        gate = self._gate(onset_frames=3, hangover_s=0.8)
        loud, silent = 3, 45   # 45 x 20 ms = 0.9 s > 0.8 s
        events = self._feed(gate, lambda i: _noise(320, amp=6000, seed=i) if i < loud else _silence(320),
                            loud + silent)
        self.assertEqual([e for e, _ in events], ["onset", "hangover"])
        self.assertEqual(gate.state, SpeechGate.IDLE)

    def test_hangover_does_not_fire_early(self):
        gate = self._gate(onset_frames=3, hangover_s=0.8)
        loud, silent = 3, 30   # 0.6 s of silence — still mid-sentence pause
        events = self._feed(gate, lambda i: _noise(320, amp=6000, seed=i) if i < loud else _silence(320),
                            loud + silent)
        self.assertEqual([e for e, _ in events], ["onset"])
        self.assertEqual(gate.state, SpeechGate.SPEECH)

    def test_speech_resumes_within_the_hangover_window(self):
        gate = self._gate(onset_frames=3, hangover_s=0.8)

        def block(i):
            if i < 3 or 20 <= i < 25:      # speak, pause 0.34 s, speak again
                return _noise(320, amp=6000, seed=i)
            return _silence(320)

        events = self._feed(gate, block, 30)
        self.assertEqual([e for e, _ in events], ["onset"], "a natural pause must not end the turn")

    def test_speech_duration_excludes_the_hangover_tail(self):
        gate = self._gate(onset_frames=1, hangover_s=0.8)
        self._feed(gate, lambda i: _noise(320, amp=6000, seed=i) if i < 5 else _silence(320), 45)
        # 5 frames of speech = 0.10 s; last speech frame ends at 100.10, first at 100.02.
        self.assertAlmostEqual(gate.speech_duration(101.0), 0.08, places=4)

    def test_short_blip_duration_is_below_min_utterance(self):
        # What lets the session discard a fan blip without paying for Whisper and the LLM.
        gate = self._gate(onset_frames=1, hangover_s=0.1)
        self._feed(gate, lambda i: _noise(320, amp=6000) if i == 0 else _silence(320), 10)
        self.assertLess(gate.speech_duration(101.0), 0.35)

    def test_partial_frames_are_buffered_not_dropped(self):
        gate = self._gate(onset_frames=3)
        # 100-sample pushes: no whole frame until the fourth push.
        events = []
        for i in range(12):
            ev = gate.update(_noise(100, amp=6000, seed=i), 100.0 + i * 0.00625)
            if ev:
                events.append(ev)
        self.assertIn("onset", events)

    def test_quiet_speech_holds_the_utterance_open(self):
        """The bug this pins: with one floor for both jobs, most syllables of real speech on this quiet
        mic fell below it, the hangover clock kept restarting, and every capture ended after ~0.7 s —
        cutting the speaker off mid-sentence."""
        gate = self._gate(rms_floor=2000.0, rms_floor_hold=400.0, onset_frames=2, hangover_s=0.8)
        # Open on two loud frames, then continue with audio that is well under the OPEN floor but
        # comfortably over the HOLD floor.
        events = self._feed(gate, lambda i: (_noise(320, amp=8000, seed=i) if i < 2
                                             else _noise(320, amp=1200, seed=i)), 30)
        self.assertEqual([e for e, _ in events], ["onset"],
                         "quiet-but-voiced audio must not end the turn")
        self.assertEqual(gate.state, SpeechGate.SPEECH)

    def test_hold_floor_still_ends_the_turn_on_real_silence(self):
        gate = self._gate(rms_floor=2000.0, rms_floor_hold=400.0, onset_frames=2, hangover_s=0.4)
        events = self._feed(gate, lambda i: (_noise(320, amp=8000, seed=i) if i < 2
                                             else _silence(320)), 30)
        self.assertEqual([e for e, _ in events], ["onset", "hangover"])

    def test_hold_floor_does_not_lower_the_bar_to_OPEN(self):
        # Audio between the hold and open floors must never start an utterance.
        gate = self._gate(rms_floor=2000.0, rms_floor_hold=400.0, onset_frames=2)
        self.assertEqual(self._feed(gate, lambda i: _noise(320, amp=1200, seed=i), 30), [])

    def test_hold_floor_defaults_to_config_and_is_clamped(self):
        from config.wake import VAD_RMS_FLOOR_HOLD

        self.assertEqual(SpeechGate(rate=16000, frame_ms=20).rms_floor_hold, VAD_RMS_FLOOR_HOLD)
        # A hold floor above the open floor would be nonsense; it gets clamped rather than obeyed.
        gate = SpeechGate(rate=16000, frame_ms=20, rms_floor=500.0, rms_floor_hold=9000.0)
        self.assertEqual(gate.rms_floor_hold, 500.0)

    def test_set_hangover_applies_live(self):
        # One gate serves both jobs; the session flips this at each capture's onset.
        gate = self._gate(hangover_s=1.5)
        gate.set_hangover(0.45)
        self.assertEqual(gate.hangover_s, 0.45)

    def test_set_rms_floor_applies_live_and_keeps_hysteresis(self):
        # The dashboard's mic-noise-floor slider. The hold floor must never end up above the open
        # floor, or an utterance could stay open on audio too quiet to have started it.
        from config.wake import VAD_RMS_FLOOR_HOLD

        gate = SpeechGate(rate=16000, frame_ms=20)
        gate.set_rms_floor(3000.0)
        self.assertEqual(gate.rms_floor, 3000.0)
        self.assertEqual(gate.rms_floor_hold, VAD_RMS_FLOOR_HOLD)

        gate.set_rms_floor(100.0)          # below the configured hold
        self.assertEqual(gate.rms_floor, 100.0)
        self.assertLessEqual(gate.rms_floor_hold, gate.rms_floor)


    # ── ambient adaptation ──────────────────────────────────────────────────
    # Behaviour lives in TestAmbientAdaptation below; these few stay here because they are
    # assertions about a plain SpeechGate that has never seen a full window.

    def test_unmeasured_room_means_no_lift(self):
        # A fresh gate must behave EXACTLY like the fixed-floor version until it has seen a window,
        # so nothing changes for anyone who never reaches one.
        gate = self._gate()
        self.assertEqual(gate.ambient, 0.0)
        self.assertEqual(gate.open_floor, gate.rms_floor)
        self.assertEqual(gate.hold_floor, gate.rms_floor_hold)

    def test_reset_clears_the_onset_run(self):
        gate = self._gate(onset_frames=3)
        self._feed(gate, lambda i: _noise(320, amp=6000, seed=i), 2)   # 2 of 3
        gate.reset()
        events = self._feed(gate, lambda i: _noise(320, amp=6000, seed=i), 2)
        self.assertEqual(events, [], "pre-mute residue must not count toward a new onset")

    def test_reset_returns_to_idle(self):
        gate = self._gate(onset_frames=1)
        self._feed(gate, lambda i: _noise(320, amp=6000, seed=i), 1)
        self.assertEqual(gate.state, SpeechGate.SPEECH)
        gate.reset()
        self.assertEqual(gate.state, SpeechGate.IDLE)
        self.assertEqual(gate.speech_duration(200.0), 0.0)

    def test_last_rms_is_exposed_for_tuning(self):
        gate = self._gate()
        gate.update(_noise(320, amp=6000, seed=3), 100.02)
        self.assertGreater(gate.last_rms, 1000.0)

    def test_dc_offset_does_not_inflate_the_level(self):
        # A MEMS mic's standing offset would otherwise clear the floor on its own.
        gate = self._gate(rms_floor=1000.0, dc_block=True)
        events = self._feed(gate, lambda i: np.full(320, 1500, dtype=np.int16), 30)
        self.assertEqual(events, [], "a pure DC offset is not speech")

    def test_floor_and_vad_are_anded(self):
        gate = self._gate(rms_floor=1000.0, onset_frames=1)
        gate._vad = MagicMock()
        gate._vad.is_speech.return_value = False
        self.assertEqual(self._feed(gate, lambda i: _noise(320, amp=6000, seed=i), 10), [],
                         "loud non-speech (fan, music) is rejected by the VAD half")

    def test_vad_is_not_consulted_below_the_floor(self):
        gate = self._gate(rms_floor=5000.0, onset_frames=1)
        gate._vad = MagicMock()
        gate._vad.is_speech.return_value = True
        self.assertEqual(self._feed(gate, lambda i: _silence(320), 10), [],
                         "the floor short-circuits, so quiet hiss can't be talked up by the VAD")
        gate._vad.is_speech.assert_not_called()

    def test_vad_error_does_not_make_kai_deaf(self):
        gate = self._gate(rms_floor=1000.0, onset_frames=1)
        gate._vad = MagicMock()
        gate._vad.is_speech.side_effect = RuntimeError("bad frame")
        events = self._feed(gate, lambda i: _noise(320, amp=6000, seed=i), 3)
        self.assertEqual([e for e, _ in events], ["onset"])


class TestAmbientAdaptation(unittest.TestCase):
    """The floors track the room instead of being pinned to the one they were measured in.

    Both configured floors came from 40 s in a quiet room. Carried into a noisy one,
    VAD_RMS_FLOOR_HOLD (the bar to KEEP an utterance open) sits under the noise, so the hangover
    clock never runs out and the scan utterance never closes — it hits the 6 s ceiling, is discarded
    as "too_long", and Whisper never runs at all. That is the failure this class pins.
    """

    WINDOW_FRAMES = 75          # WAKE_AMBIENT_WINDOW_S (1.5) / 20 ms

    def _gate(self, **kw):
        kw.setdefault("rms_floor", 650.0)
        kw.setdefault("rms_floor_hold", 250.0)
        kw.setdefault("onset_frames", 3)
        kw.setdefault("hangover_s", 0.5)
        return SpeechGate(rate=16000, frame_ms=20, **kw)

    def _feed(self, gate, block_fn, count, t0=100.0):
        events = []
        for i in range(count):
            ev = gate.update(block_fn(i), t0 + (i + 1) * 0.02)
            if ev:
                events.append(ev)
        return events

    def test_silence_measures_a_near_zero_room(self):
        gate = self._gate()
        self._feed(gate, lambda i: _silence(320), self.WINDOW_FRAMES + 5)
        self.assertLess(gate.ambient, 1.0)
        self.assertEqual(gate.open_floor, gate.rms_floor, "a quiet room must not lift anything")

    def test_a_noisy_room_is_measured_and_lifts_both_floors(self):
        gate = self._gate()
        self._feed(gate, lambda i: _noise(320, amp=400, seed=i), self.WINDOW_FRAMES + 5)
        self.assertGreater(gate.ambient, 300.0)
        self.assertLess(gate.ambient, 500.0)
        self.assertGreater(gate.hold_floor, 250.0, "the hold floor is the one that matters")
        self.assertGreater(gate.open_floor, 650.0)

    def test_the_multipliers_reproduce_the_measured_tuning(self):
        # config/wake.py claims adaptation is a no-op in the room the constants were measured in
        # (p50 = 124). If someone retunes a multiplier, this is the assertion that catches it.
        gate = self._gate()
        gate.ambient = 124.0
        self.assertAlmostEqual(gate.open_floor, 650.0, delta=10.0)
        self.assertAlmostEqual(gate.hold_floor, 250.0, delta=10.0)

    # ── the bug it exists to fix ────────────────────────────────────────────

    def test_an_utterance_closes_in_a_room_noisier_than_the_hold_floor(self):
        """The whole point. Noise at 400 sits above the configured hold floor of 250, so with fixed
        floors the utterance can never end — 6 s ceiling, discarded, Whisper never runs."""
        gate = self._gate()

        def block(i):
            if i < self.WINDOW_FRAMES + 5:
                return _noise(320, amp=400, seed=i)          # calibrate on the room
            if i < self.WINDOW_FRAMES + 8:
                return _noise(320, amp=6000, seed=i)         # somebody speaks
            return _noise(320, amp=400, seed=i)              # back to just the room

        events = self._feed(gate, block, self.WINDOW_FRAMES + 60)
        self.assertEqual(events, ["onset", "hangover"])
        self.assertEqual(gate.state, SpeechGate.IDLE)

    def test_without_adaptation_the_same_room_never_closes_the_utterance(self):
        """The contrast that proves the test above is measuring adaptation and not something else.
        This is the shipped behaviour before this change, and it is what "deaf in a noisy room"
        actually was."""
        gate = self._gate()

        def block(i):
            if i < self.WINDOW_FRAMES + 5:
                return _noise(320, amp=400, seed=i)
            if i < self.WINDOW_FRAMES + 8:
                return _noise(320, amp=6000, seed=i)
            return _noise(320, amp=400, seed=i)

        with patch("ai.audio.WAKE_AMBIENT_ADAPT", False):
            events = self._feed(gate, block, self.WINDOW_FRAMES + 60)
        self.assertEqual(events, ["onset"], "the fixed hold floor can never time out on this room")
        self.assertEqual(gate.state, SpeechGate.SPEECH)

    # ── the guards ──────────────────────────────────────────────────────────

    def test_the_lift_is_capped(self):
        # Deafness is worse than false onsets: a floor above the speaker's own voice makes the
        # feature do nothing and report no error.
        gate = self._gate()
        gate.ambient = 100_000.0
        self.assertEqual(gate.open_floor, 650.0 * WAKE_AMBIENT_MAX_LIFT)

    def test_hold_never_rises_above_open(self):
        # The hysteresis invariant has to survive adaptation too, at every ambient.
        gate = self._gate()
        for ambient in (0.0, 50.0, 124.0, 400.0, 1200.0, 100_000.0):
            with self.subTest(ambient=ambient):
                gate.ambient = ambient
                self.assertLessEqual(gate.hold_floor, gate.open_floor)

    def test_the_floors_are_never_lowered_below_the_configured_values(self):
        gate = self._gate()
        gate.ambient = 1.0
        self.assertEqual(gate.open_floor, 650.0)
        self.assertEqual(gate.hold_floor, 250.0)

    def test_adaptation_is_frozen_while_an_utterance_is_open(self):
        """Continuous speech has no true silence in it, so a minimum taken during a turn settles on
        the speaker's quietest syllable and lifts the hold floor out from under them — re-creating
        the exact bug the hold floor was added to fix."""
        gate = self._gate()
        self._feed(gate, lambda i: _silence(320), self.WINDOW_FRAMES + 5)
        quiet_room = gate.ambient

        # Open an utterance and keep talking for several windows.
        self._feed(gate, lambda i: _noise(320, amp=6000, seed=i), self.WINDOW_FRAMES * 3, t0=200.0)
        self.assertEqual(gate.state, SpeechGate.SPEECH)
        self.assertEqual(gate.ambient, quiet_room, "the speaker must not raise the room estimate")

    def test_reset_keeps_the_room_estimate(self):
        # reset() runs at every state change and every un-mute; ambient is a property of the ROOM
        # and must survive all of it, or it would be permanently re-learning from scratch.
        gate = self._gate()
        self._feed(gate, lambda i: _noise(320, amp=400, seed=i), self.WINDOW_FRAMES + 5)
        measured = gate.ambient
        self.assertGreater(measured, 0.0)
        gate.reset()
        self.assertEqual(gate.ambient, measured)

    def test_the_first_window_seeds_directly_instead_of_smoothing_up_from_zero(self):
        # Smoothing from 0.0 would spend ~30 s pretending a loud room is quiet — which is the state
        # the whole mechanism exists to escape.
        gate = self._gate()
        self._feed(gate, lambda i: _noise(320, amp=400, seed=i), self.WINDOW_FRAMES)
        self.assertGreater(gate.ambient, 300.0)

    def test_disabling_adaptation_pins_the_floors(self):
        gate = self._gate()
        gate.ambient = 5000.0
        with patch("ai.audio.WAKE_AMBIENT_ADAPT", False):
            self.assertEqual(gate.open_floor, 650.0)
            self.assertEqual(gate.hold_floor, 250.0)


class TestImportPvporcupine(unittest.TestCase):
    """pvporcupine resolves the CPU at IMPORT time and raises on anything outside a short table.
    On the Jetson Orin (Cortex-A78AE, 0xd42) that is a NotImplementedError — which must never reach
    the caller, because ai.audio is imported transitively by face_track at startup."""

    # Captured before any patching — calling the patched __import__ from inside a side_effect
    # would recurse forever.
    REAL_IMPORT = staticmethod(__import__)

    def test_clean_import_is_used_when_it_works(self):
        fake = MagicMock()
        with patch.dict("sys.modules", {"pvporcupine": fake}):
            mod, err, patched = audio._import_pvporcupine()
        self.assertIs(mod, fake)
        self.assertIsNone(err)
        self.assertFalse(patched, "no override should be attempted on a supported CPU")

    def test_unsupported_cpu_retries_with_the_override(self):
        calls = {"n": 0}
        fake = MagicMock()

        def fake_import(name, *a, **k):
            if name == "pvporcupine":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise NotImplementedError("Unsupported CPU: '0xd42'.")
                return fake
            return self.REAL_IMPORT(name, *a, **k)

        with patch("builtins.__import__", side_effect=fake_import):
            mod, err, patched = audio._import_pvporcupine()
        self.assertIs(mod, fake)
        self.assertIsNone(err)
        self.assertTrue(patched)
        self.assertEqual(calls["n"], 2, "must actually retry, not just report success")

    def test_override_substitutes_the_cpu_part_only_for_cpuinfo(self):
        import subprocess as sp

        seen = {}

        def fake_import(name, *a, **k):
            if name == "pvporcupine":
                if "cpuinfo" not in seen:
                    seen["cpuinfo"] = sp.check_output(["cat", "/proc/cpuinfo"])
                    raise NotImplementedError("Unsupported CPU: '0xd42'.")
                seen["cpuinfo"] = sp.check_output(["cat", "/proc/cpuinfo"])
                return MagicMock()
            return self.REAL_IMPORT(name, *a, **k)

        real = sp.check_output
        with patch("builtins.__import__", side_effect=fake_import):
            audio._import_pvporcupine()
        self.assertIn(audio.WAKE_CPU_PART_OVERRIDE, seen["cpuinfo"].decode())
        self.assertIs(sp.check_output, real, "the patch must be reversed no matter what")

    def test_other_import_errors_are_not_retried(self):
        calls = {"n": 0}

        def fake_import(name, *a, **k):
            if name == "pvporcupine":
                calls["n"] += 1
                raise ImportError("No module named 'pvporcupine'")
            return self.REAL_IMPORT(name, *a, **k)

        with patch("builtins.__import__", side_effect=fake_import):
            mod, err, patched = audio._import_pvporcupine()
        self.assertIsNone(mod)
        self.assertIn("No module named", err)
        self.assertEqual(calls["n"], 1, "a missing package is not a CPU problem")

    def test_override_can_be_disabled(self):
        def fake_import(name, *a, **k):
            if name == "pvporcupine":
                raise NotImplementedError("Unsupported CPU: '0xd42'.")
            return self.REAL_IMPORT(name, *a, **k)

        with patch("ai.audio.WAKE_CPU_PART_OVERRIDE", None), \
             patch("builtins.__import__", side_effect=fake_import):
            mod, err, patched = audio._import_pvporcupine()
        self.assertIsNone(mod)
        self.assertFalse(patched)

    def test_failure_of_both_attempts_is_reported_not_raised(self):
        def fake_import(name, *a, **k):
            if name == "pvporcupine":
                raise NotImplementedError("Unsupported CPU: '0xd42'.")
            return self.REAL_IMPORT(name, *a, **k)

        with patch("builtins.__import__", side_effect=fake_import):
            mod, err, patched = audio._import_pvporcupine()
        self.assertIsNone(mod)
        self.assertIn("override also failed", err)

    def test_engine_surfaces_the_cpu_hint(self):
        # The tier itself, not the chain: the chain's joined string would contain this substring even
        # if the porcupine tier had stopped producing it.
        eng = PorcupineEngine()
        with patch("ai.audio._WAKE_OK", False), \
             patch("ai.audio._WAKE_IMPORT_ERROR", "NotImplementedError: Unsupported CPU: '0xd42'."), \
             patch("builtins.print"):
            self.assertFalse(eng.open())
        self.assertIn("WAKE_CPU_PART_OVERRIDE", eng.unavailable)


class TestResolveAccessKey(unittest.TestCase):
    def test_environment_wins(self):
        with patch.dict("os.environ", {"PICOVOICE_ACCESS_KEY": "  from-env  "}):
            self.assertEqual(resolve_access_key(), "from-env")

    def test_falls_back_to_the_key_file(self):
        with patch.dict("os.environ", {"PICOVOICE_ACCESS_KEY": ""}), \
             patch("pathlib.Path.read_text", return_value="from-file\n"):
            self.assertEqual(resolve_access_key(), "from-file")

    def test_none_when_neither_exists(self):
        with patch.dict("os.environ", {"PICOVOICE_ACCESS_KEY": ""}), \
             patch("pathlib.Path.read_text", side_effect=OSError("missing")):
            self.assertIsNone(resolve_access_key())

    def test_blank_env_var_falls_through_to_the_file(self):
        with patch.dict("os.environ", {"PICOVOICE_ACCESS_KEY": "   "}), \
             patch("pathlib.Path.read_text", return_value="from-file"):
            self.assertEqual(resolve_access_key(), "from-file")

    def test_empty_key_file_is_none(self):
        with patch.dict("os.environ", {"PICOVOICE_ACCESS_KEY": ""}), \
             patch("pathlib.Path.read_text", return_value="\n"):
            self.assertIsNone(resolve_access_key())

    def test_unset_home_does_not_crash_startup(self):
        # @reboot cron can run with no HOME at all, and expanduser() raises RuntimeError there —
        # exactly the environment the key file is meant to serve.
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(resolve_access_key())


class TestKeywordPaths(unittest.TestCase):
    def test_relative_paths_resolve_against_the_project_root(self):
        for path in keyword_paths():
            self.assertTrue(path.is_absolute(), "must survive any cwd (autostart runs from cron)")
            self.assertEqual(path.suffix, ".ppn")


class TestPorcupineEngine(unittest.TestCase):
    """Tier 1. Every failure mode must leave the chain free to try the next tier, never raise."""

    def test_missing_package_disables_gracefully(self):
        det = PorcupineEngine()
        with patch("ai.audio._WAKE_OK", False), patch("builtins.print"):
            self.assertFalse(det.open())
        self.assertFalse(det.ready)
        self.assertIn("pvporcupine", det.unavailable)

    def test_missing_access_key_disables_gracefully(self):
        det = PorcupineEngine()
        with patch("ai.audio._WAKE_OK", True), \
             patch("ai.audio.resolve_access_key", return_value=None), \
             patch("builtins.print"):
            self.assertFalse(det.open())
        self.assertIn("access key", det.unavailable)

    def test_missing_keyword_file_disables_gracefully(self):
        det = PorcupineEngine()
        with patch("ai.audio._WAKE_OK", True), \
             patch("ai.audio.resolve_access_key", return_value="k"), \
             patch("ai.audio.keyword_paths", return_value=[Path("/nope/hey-kai.ppn")]), \
             patch("builtins.print"):
            self.assertFalse(det.open())
        self.assertIn("not found", det.unavailable)

    def test_wrong_platform_ppn_reports_the_likely_cause(self):
        det = PorcupineEngine()
        fake_pv = MagicMock()
        fake_pv.create.side_effect = RuntimeError("invalid keyword file")
        with patch("ai.audio._WAKE_OK", True), \
             patch("ai.audio.pvporcupine", fake_pv, create=True), \
             patch("ai.audio.resolve_access_key", return_value="k"), \
             patch("ai.audio.keyword_paths", return_value=[Path(__file__)]), \
             patch("builtins.print"):
            self.assertFalse(det.open())
        self.assertIn("platform", det.unavailable)

    def _opened(self, sensitivity=None):
        """An open engine over a fake pvporcupine, returning (engine, fake_module)."""
        eng = PorcupineEngine()
        if sensitivity is not None:
            eng.set_sensitivity(sensitivity)
        fake_pv = MagicMock()
        fake_pv.create.return_value = MagicMock(frame_length=512, sample_rate=16000)
        with patch("ai.audio._WAKE_OK", True), \
             patch("ai.audio.pvporcupine", fake_pv, create=True), \
             patch("ai.audio.resolve_access_key", return_value="k"), \
             patch("ai.audio.keyword_paths", return_value=[Path(__file__)]), \
             patch("builtins.print"):
            eng.open()
        return eng, fake_pv

    def test_open_passes_the_live_sensitivity_to_create(self):
        _, fake_pv = self._opened(sensitivity=0.85)
        self.assertEqual(fake_pv.create.call_args.kwargs["sensitivities"], [0.85])

    def test_set_sensitivity_on_an_open_engine_demands_a_reopen(self):
        # Porcupine bakes sensitivities into the native handle, so this tier cannot apply a change in
        # place. Saying so is what makes the dashboard slider actually work instead of appearing to.
        eng, _ = self._opened()
        self.assertTrue(eng.set_sensitivity(0.9))

    def test_set_sensitivity_before_open_needs_no_reopen(self):
        eng = PorcupineEngine()
        self.assertFalse(eng.set_sensitivity(0.9))
        self.assertEqual(eng.sensitivity, 0.9)

    def test_sample_rate_mismatch_is_refused(self):
        det = PorcupineEngine()
        handle = MagicMock(frame_length=512, sample_rate=8000)
        fake_pv = MagicMock()
        fake_pv.create.return_value = handle
        with patch("ai.audio._WAKE_OK", True), \
             patch("ai.audio.pvporcupine", fake_pv, create=True), \
             patch("ai.audio.resolve_access_key", return_value="k"), \
             patch("ai.audio.keyword_paths", return_value=[Path(__file__)]), \
             patch("builtins.print"):
            self.assertFalse(det.open())
        self.assertFalse(det.ready)
        handle.delete.assert_called_once()

    def _open_fake(self, det, handle):
        fake_pv = MagicMock()
        fake_pv.create.return_value = handle
        with patch("ai.audio._WAKE_OK", True), \
             patch("ai.audio.pvporcupine", fake_pv, create=True), \
             patch("ai.audio.resolve_access_key", return_value="k"), \
             patch("ai.audio.keyword_paths", return_value=[Path(__file__)]), \
             patch("builtins.print"):
            return det.open()

    def test_opens_and_adopts_the_handle_geometry(self):
        det = PorcupineEngine()
        handle = MagicMock(frame_length=512, sample_rate=16000)
        self.assertTrue(self._open_fake(det, handle))
        self.assertTrue(det.ready)
        self.assertEqual(det.frame_length, 512)
        self.assertIsNone(det.unavailable)

    def test_open_is_idempotent(self):
        det = PorcupineEngine()
        handle = MagicMock(frame_length=512, sample_rate=16000)
        self._open_fake(det, handle)
        self.assertTrue(det.open())

    def test_process_reports_detection(self):
        det = PorcupineEngine()
        handle = MagicMock(frame_length=512, sample_rate=16000)
        self._open_fake(det, handle)
        handle.process.return_value = 0
        self.assertTrue(det.process(_silence(512)))
        handle.process.return_value = -1
        self.assertFalse(det.process(_silence(512)))

    def test_process_before_open_is_false(self):
        self.assertFalse(PorcupineEngine().process(_silence(512)))

    def test_process_failure_disables_instead_of_raising(self):
        det = PorcupineEngine()
        handle = MagicMock(frame_length=512, sample_rate=16000)
        self._open_fake(det, handle)
        handle.process.side_effect = RuntimeError("native blew up")
        with patch("builtins.print"):
            self.assertFalse(det.process(_silence(512)))
        self.assertFalse(det.ready)

    def test_close_releases_native_memory(self):
        det = PorcupineEngine()
        handle = MagicMock(frame_length=512, sample_rate=16000)
        self._open_fake(det, handle)
        det.close()
        handle.delete.assert_called_once()
        self.assertFalse(det.ready)

    def test_close_is_safe_twice_and_when_delete_raises(self):
        det = PorcupineEngine()
        handle = MagicMock(frame_length=512, sample_rate=16000)
        handle.delete.side_effect = RuntimeError("already gone")
        self._open_fake(det, handle)
        det.close()
        det.close()


def _fake_oww_model(score=0.0, keys=("hey_kai",), predict_raises=None, init_raises=None):
    """A stand-in for openwakeword.model.Model."""
    model = MagicMock()
    if predict_raises is not None:
        model.predict.side_effect = predict_raises
    else:
        model.predict.return_value = {k: score for k in keys}
    factory = MagicMock(return_value=model)
    if init_raises is not None:
        factory.side_effect = init_raises
    return factory, model


class TestOpenWakeWordEngine(unittest.TestCase):
    """Tier 2. Patched the same way pvporcupine is — nothing here needs the package installed."""

    def _open(self, factory, model_exists=True, front_end_exists=True):
        eng = OpenWakeWordEngine()
        real_is_file = Path.is_file

        def is_file(self):
            name = self.name
            if name.endswith(".onnx") and "hey_kai" in name:
                return model_exists
            if name in ("melspectrogram.onnx", "embedding_model.onnx"):
                return front_end_exists
            return real_is_file(self)

        with patch("ai.audio._OWW_OK", True), \
             patch("ai.audio._OWWModel", factory, create=True), \
             patch("ai.audio.openwakeword", MagicMock(__file__=__file__), create=True), \
             patch.object(Path, "is_file", is_file), \
             patch("builtins.print"):
            return eng, eng.open()

    def test_missing_package_reports_the_install_command(self):
        eng = OpenWakeWordEngine()
        with patch("ai.audio._OWW_OK", False), patch("ai.audio._OWW_IMPORT_ERROR", None), \
             patch("builtins.print"):
            self.assertFalse(eng.open())
        self.assertIn("pip3 install", eng.unavailable)

    def test_import_failure_is_surfaced_verbatim(self):
        eng = OpenWakeWordEngine()
        with patch("ai.audio._OWW_OK", False), \
             patch("ai.audio._OWW_IMPORT_ERROR", "openwakeword unusable (X: no tflite)"), \
             patch("builtins.print"):
            self.assertFalse(eng.open())
        self.assertIn("no tflite", eng.unavailable)

    def test_missing_custom_model_points_at_the_training_recipe(self):
        factory, _ = _fake_oww_model()
        eng, ok = self._open(factory, model_exists=False)
        self.assertFalse(ok)
        self.assertIn("train it", eng.unavailable)
        factory.assert_not_called()

    def test_missing_front_end_names_the_download_command(self):
        # openWakeWord fetches these over the network on first use. Constructing anything before
        # checking would hang an offline boot for the HTTP timeout.
        factory, _ = _fake_oww_model()
        eng, ok = self._open(factory, front_end_exists=False)
        self.assertFalse(ok)
        self.assertIn("download_models()", eng.unavailable)
        factory.assert_not_called()

    def test_opens_and_reports_1280_frames(self):
        factory, _ = _fake_oww_model()
        eng, ok = self._open(factory)
        self.assertTrue(ok)
        self.assertTrue(eng.ready)
        self.assertEqual(eng.frame_length, 1280)
        self.assertEqual(eng.kind, "frame")

    def test_warm_up_probe_runs_once_on_a_zero_frame(self):
        factory, model = _fake_oww_model()
        self._open(factory)
        model.predict.assert_called_once()
        frame = model.predict.call_args[0][0]
        self.assertEqual(len(frame), 1280)

    def test_predict_failing_on_warm_up_refuses_the_tier(self):
        # Model() constructing while predict() throws on a mis-shaped .onnx is the realistic failure.
        # Accepting it would leave Kai deaf while reporting the tier as live.
        factory, _ = _fake_oww_model(predict_raises=RuntimeError("bad input shape"))
        eng, ok = self._open(factory)
        self.assertFalse(ok)
        self.assertFalse(eng.ready)
        self.assertIn("bad input shape", eng.unavailable)

    def test_unknown_kwargs_are_retried_with_the_minimal_signature(self):
        # openWakeWord's Model.__init__ kwargs have drifted across releases.
        calls = []
        model = MagicMock()
        model.predict.return_value = {"hey_kai": 0.0}

        def factory(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise TypeError("unexpected keyword argument 'vad_threshold'")
            return model

        eng, ok = self._open(factory)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        self.assertIn("vad_threshold", calls[0])
        self.assertNotIn("vad_threshold", calls[1])
        self.assertIn("wakeword_models", calls[1])

    def test_construction_failure_is_reported_not_raised(self):
        factory, _ = _fake_oww_model(init_raises=OSError("onnx session failed"))
        eng, ok = self._open(factory)
        self.assertFalse(ok)
        self.assertIn("onnx session failed", eng.unavailable)

    def test_score_at_and_below_threshold(self):
        from config.wake import WAKE_OWW_THRESHOLD

        factory, model = _fake_oww_model()
        eng, _ = self._open(factory)
        model.predict.return_value = {"hey_kai": WAKE_OWW_THRESHOLD - 0.01}
        self.assertFalse(eng.process(_silence(1280)))
        model.predict.return_value = {"hey_kai": WAKE_OWW_THRESHOLD}
        self.assertTrue(eng.process(_silence(1280)))
        self.assertAlmostEqual(eng.last_score, WAKE_OWW_THRESHOLD)

    def test_sensitivity_applies_per_frame_with_no_reopen(self):
        # This tier compares per frame, so a sensitivity change is free — no engine reload, nothing
        # touching native memory.
        factory, model = _fake_oww_model()
        eng, _ = self._open(factory)
        model.predict.return_value = {"hey_kai": 0.35}
        self.assertFalse(eng.process(_silence(1280)))
        self.assertFalse(eng.set_sensitivity(0.9), "no reopen should be needed for this tier")
        self.assertTrue(eng.process(_silence(1280)), "raising sensitivity must accept a lower score")

    def test_higher_sensitivity_means_more_detections(self):
        # The direction that matters: this tier's native knob is a THRESHOLD (higher = fewer hits),
        # ours is a SENSITIVITY (higher = more). If the inversion were ever dropped, the dashboard
        # slider would run backwards on whichever tier happened to win.
        factory, _ = _fake_oww_model()
        eng, _ = self._open(factory)
        eng.set_sensitivity(0.1)
        low = eng.threshold
        eng.set_sensitivity(0.9)
        self.assertLess(eng.threshold, low)

    def test_default_sensitivity_leaves_the_configured_threshold_untouched(self):
        # config/wake.py's WAKE_OWW_THRESHOLD must still set the resting point, or tuning it there
        # would silently stop working.
        from config.wake import WAKE_OWW_THRESHOLD, WAKE_SENSITIVITIES

        factory, _ = _fake_oww_model()
        eng, _ = self._open(factory)
        eng.set_sensitivity(WAKE_SENSITIVITIES[0])
        self.assertAlmostEqual(eng.threshold, WAKE_OWW_THRESHOLD)

    def test_last_score_is_published_for_tuning(self):
        factory, model = _fake_oww_model()
        eng, _ = self._open(factory)
        model.predict.return_value = {"hey_kai": 0.42}
        eng.process(_silence(1280))
        self.assertAlmostEqual(eng.last_score, 0.42)

    def test_multi_key_score_dict_uses_the_recorded_key(self):
        factory, model = _fake_oww_model(keys=("hey_kai", "alexa"))
        eng, _ = self._open(factory)
        # Two keys at warm-up -> falls back to the model stem, which is "hey_kai".
        model.predict.return_value = {"hey_kai": 0.9, "alexa": 0.0}
        self.assertTrue(eng.process(_silence(1280)))

    def test_predict_failure_at_runtime_disables_rather_than_raises(self):
        factory, model = _fake_oww_model()
        eng, _ = self._open(factory)
        model.predict.side_effect = RuntimeError("native blew up")
        with patch("builtins.print"):
            self.assertFalse(eng.process(_silence(1280)))
        self.assertFalse(eng.ready)

    def test_reset_clears_the_internal_buffer(self):
        factory, model = _fake_oww_model()
        eng, _ = self._open(factory)
        eng.reset()
        model.reset.assert_called_once()

    def test_reset_survives_a_model_without_reset(self):
        factory, model = _fake_oww_model()
        model.reset.side_effect = AttributeError("no reset")
        eng, _ = self._open(factory)
        eng.reset()   # must not propagate

    def test_process_before_open_is_false(self):
        self.assertFalse(OpenWakeWordEngine().process(_silence(1280)))


class TestWhisperWakeEngine(unittest.TestCase):
    """Tier 3 — utterance-kind, so it must never claim to spot frames."""

    def test_opens_when_faster_whisper_is_importable(self):
        with patch("importlib.util.find_spec", return_value=object()), patch("builtins.print"):
            eng = WhisperWakeEngine()
            self.assertTrue(eng.open())
        self.assertTrue(eng.ready)
        self.assertEqual(eng.kind, "utterance")

    def test_disabled_by_config(self):
        eng = WhisperWakeEngine()
        with patch("ai.audio.WAKE_WHISPER_ENABLED", False):
            self.assertFalse(eng.open())
        self.assertIn("WAKE_WHISPER_ENABLED", eng.unavailable)

    def test_missing_faster_whisper(self):
        eng = WhisperWakeEngine()
        with patch("importlib.util.find_spec", return_value=None):
            self.assertFalse(eng.open())
        self.assertIn("faster-whisper", eng.unavailable)

    def test_never_loads_a_model(self):
        # A second small/int8 copy would not fit the headroom; the session reuses the warm instance.
        with patch("importlib.util.find_spec", return_value=object()), patch("builtins.print"):
            eng = WhisperWakeEngine()
            eng.open()
        self.assertFalse(hasattr(eng, "_model"))

    def test_process_always_false(self):
        with patch("importlib.util.find_spec", return_value=object()), patch("builtins.print"):
            eng = WhisperWakeEngine()
            eng.open()
        self.assertFalse(eng.process(_silence(512)))

    def test_frame_length_is_never_zero(self):
        # FrameAssembler(0) divides by zero, and MicStream sizes itself from this.
        self.assertGreater(WhisperWakeEngine().frame_length, 0)


class _CountingEngine:
    """Records construction so the chain can be asserted to skip tiers entirely."""
    constructed: list = []

    def __init__(self, name, kind="frame", frame_length=512, ok=True, raises=None):
        self.name, self.kind, self.frame_length = name, kind, frame_length
        self.sample_rate, self.last_score = 16000, 0.0
        self.unavailable = None
        self._ok, self._raises = ok, raises
        self.closed = 0
        self.sensitivity = 0.5
        _CountingEngine.constructed.append(name)

    def set_sensitivity(self, value):
        """WakeDetector.open() pushes the chain's sensitivity into each tier before opening it, because
        Porcupine bakes the value into create(). Returns needs-reopen, like the real engines."""
        self.sensitivity = value
        return False

    def open(self):
        if self._raises:
            raise self._raises
        if not self._ok:
            self.unavailable = f"{self.name} said no"
            return False
        return True

    @property
    def ready(self):
        return self._ok and not self._raises

    def process(self, frame):
        return False

    def reset(self):
        pass

    def close(self):
        self.closed += 1


class TestWakeChain(unittest.TestCase):
    def setUp(self):
        _CountingEngine.constructed = []

    def _chain(self, spec, order=("porcupine", "openwakeword", "whisper"), force=None):
        """spec: {name: kwargs-for-_CountingEngine}"""
        factories = {n: (lambda n=n, kw=kw: _CountingEngine(n, **kw)) for n, kw in spec.items()}
        det = WakeDetector(order=order, force=force)
        det._FACTORIES = factories
        return det

    def test_first_tier_wins_and_later_tiers_are_never_constructed(self):
        det = self._chain({"porcupine": {}, "openwakeword": {}, "whisper": {}})
        with patch("builtins.print"):
            self.assertTrue(det.open())
        self.assertEqual(det.engine, "porcupine")
        self.assertEqual(_CountingEngine.constructed, ["porcupine"],
                         "a working first tier must not construct the others")

    def test_falls_through_to_the_second_tier(self):
        det = self._chain({"porcupine": {"ok": False}, "openwakeword": {"frame_length": 1280},
                           "whisper": {}})
        with patch("builtins.print"):
            self.assertTrue(det.open())
        self.assertEqual(det.engine, "openwakeword")
        self.assertEqual(det.frame_length, 1280, "the chain must adopt the WINNER's frame length")
        self.assertEqual(_CountingEngine.constructed, ["porcupine", "openwakeword"])

    def test_falls_through_to_the_utterance_tier(self):
        det = self._chain({"porcupine": {"ok": False}, "openwakeword": {"ok": False},
                           "whisper": {"kind": "utterance"}})
        with patch("builtins.print"):
            self.assertTrue(det.open())
        self.assertEqual(det.engine, "whisper")
        self.assertEqual(det.kind, "utterance")
        self.assertTrue(det.ready)
        self.assertFalse(det.frame_ready, "there are no frames to push for an utterance tier")

    def test_frame_ready_is_true_only_for_frame_tiers(self):
        det = self._chain({"porcupine": {}})
        with patch("builtins.print"):
            det.open()
        self.assertTrue(det.frame_ready)

    def test_every_tier_failing_reports_all_reasons(self):
        det = self._chain({"porcupine": {"ok": False}, "openwakeword": {"ok": False},
                           "whisper": {"ok": False}})
        with patch("builtins.print"):
            self.assertFalse(det.open())
        self.assertFalse(det.ready)
        for name in ("porcupine", "openwakeword", "whisper"):
            self.assertIn(name, det.unavailable)
        self.assertEqual(det.engine, "")

    def test_a_tier_that_raises_is_caught_and_recorded(self):
        # This is what took the robot down when pvporcupine began raising at import.
        det = self._chain({"porcupine": {"raises": NotImplementedError("Unsupported CPU")},
                           "openwakeword": {}})
        with patch("builtins.print"):
            self.assertTrue(det.open())
        self.assertEqual(det.engine, "openwakeword")
        self.assertIn("Unsupported CPU", det.tiers["porcupine"])

    def test_failed_tiers_are_closed_so_nothing_leaks(self):
        closed = []
        factories = {
            "porcupine": lambda: _record_close(_CountingEngine("porcupine", ok=False), closed),
            "openwakeword": lambda: _CountingEngine("openwakeword"),
        }
        det = WakeDetector(order=("porcupine", "openwakeword"))
        det._FACTORIES = factories
        with patch("builtins.print"):
            det.open()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].closed, 1)

    def test_close_closes_only_the_winner(self):
        det = self._chain({"porcupine": {}})
        with patch("builtins.print"):
            det.open()
        winner = det._engine
        det.close()
        self.assertEqual(winner.closed, 1)
        self.assertFalse(det.ready)

    def test_open_is_idempotent(self):
        det = self._chain({"porcupine": {}})
        with patch("builtins.print"):
            det.open()
            det.open()
        self.assertEqual(_CountingEngine.constructed, ["porcupine"])

    def test_force_bypasses_the_order(self):
        det = self._chain({"porcupine": {}, "whisper": {"kind": "utterance"}}, force="whisper")
        with patch("builtins.print"):
            self.assertTrue(det.open())
        self.assertEqual(det.engine, "whisper")
        self.assertEqual(_CountingEngine.constructed, ["whisper"])

    def test_unknown_engine_name_is_recorded_and_skipped(self):
        det = self._chain({"porcupine": {}}, order=("nope", "porcupine"))
        with patch("builtins.print"):
            self.assertTrue(det.open())
        self.assertIn("unknown engine name", det.tiers["nope"])
        self.assertEqual(det.engine, "porcupine")

    def test_sensitivity_is_pushed_into_each_tier_before_it_opens(self):
        # Porcupine bakes sensitivity into create(), so setting it after open() would be too late —
        # and open() builds a FRESH engine, which is exactly the reopen used to apply a change.
        det = self._chain({"porcupine": {}})
        det.set_sensitivity(0.9)
        with patch("builtins.print"):
            det.open()
        self.assertEqual(det._engine.sensitivity, 0.9)

    def test_sensitivity_survives_a_reopen(self):
        det = self._chain({"porcupine": {}})
        with patch("builtins.print"):
            det.open()
            det.set_sensitivity(0.2)
            det.close()
            det.open()
        self.assertEqual(det._engine.sensitivity, 0.2,
                         "a live change must not be lost by the reopen that applies it")

    def test_set_sensitivity_on_a_closed_chain_needs_no_reopen(self):
        det = self._chain({"porcupine": {}})
        self.assertFalse(det.set_sensitivity(0.7))
        self.assertEqual(det.sensitivity, 0.7)

    def test_sensitivity_is_clamped(self):
        det = self._chain({"porcupine": {}})
        det.set_sensitivity(5.0)
        self.assertEqual(det.sensitivity, 1.0)
        det.set_sensitivity(-1.0)
        self.assertEqual(det.sensitivity, 0.0)

    def test_delegates_process_and_reports_not_ready_before_open(self):
        det = self._chain({"porcupine": {}})
        self.assertFalse(det.process(_silence(512)))
        self.assertEqual(det.last_score, 0.0)
        self.assertEqual(det.kind, "")

    def test_real_default_order_is_the_configured_one(self):
        from config.wake import WAKE_ENGINE_ORDER

        self.assertEqual(WakeDetector()._order, tuple(WAKE_ENGINE_ORDER))


def _record_close(engine, sink):
    sink.append(engine)
    return engine


class TestPorcupineFrameGeometry(unittest.TestCase):
    def test_capture_blocksize_gives_whole_porcupine_frames(self):
        from config.voice import I2S_CAPTURE_RATE
        from config.wake import CAPTURE_BLOCKSIZE, WAKE_FRAME_LENGTH

        d = Decimator(I2S_CAPTURE_RATE, 16000)
        out = d.feed(_silence(CAPTURE_BLOCKSIZE))
        self.assertEqual(len(out) % WAKE_FRAME_LENGTH, 0,
                         "CAPTURE_BLOCKSIZE must decimate to a whole number of wake frames")
        self.assertGreater(len(out), 0)


class TestNormalizeForAsr(unittest.TestCase):
    """The level fix for a distant talker. Each test here is one of the ways it could make things
    worse instead of better — see the guards in normalize_for_asr's docstring."""

    @staticmethod
    def _at_rms(level, n=16000, seed=0):
        x = np.random.default_rng(seed).standard_normal(n).astype(np.float32)
        return (x * (level / float(np.sqrt(np.mean(x.astype(np.float64) ** 2))))).astype(np.float32)

    def test_quiet_audio_is_lifted_to_the_target(self):
        out = normalize_for_asr(self._at_rms(ASR_NORMALIZE_TARGET_RMS / 4))
        self.assertAlmostEqual(rms(out.samples * 32768) / 32768, ASR_NORMALIZE_TARGET_RMS, places=4)

    def test_reports_the_measured_input_level(self):
        out = normalize_for_asr(self._at_rms(0.01))
        self.assertAlmostEqual(out.rms, 0.01, places=5)

    def test_gain_is_capped(self):
        # Far quieter than max_gain can reach: the gain must stop at the ceiling, NOT reach the
        # target. A gain pinned here is the signal that the answer is the mic, not a constant.
        out = normalize_for_asr(self._at_rms(ASR_NORMALIZE_TARGET_RMS / (ASR_NORMALIZE_MAX_GAIN * 4)))
        self.assertAlmostEqual(out.gain, ASR_NORMALIZE_MAX_GAIN, places=6)

    def test_loud_audio_is_left_exactly_alone(self):
        loud = self._at_rms(ASR_NORMALIZE_TARGET_RMS * 3)
        out = normalize_for_asr(loud)
        self.assertEqual(out.gain, 1.0)
        # Bit-identical, not merely close: the close-mic case must not be perturbed at all.
        np.testing.assert_array_equal(out.samples, loud)

    def test_near_silence_is_not_amplified(self):
        # Amplifying this is how you manufacture the hallucinated-filler transcripts that
        # TRANSCRIPT_MAX_NO_SPEECH_PROB exists to catch.
        out = normalize_for_asr(self._at_rms(ASR_NORMALIZE_MIN_RMS / 2))
        self.assertEqual(out.gain, 1.0)

    def test_digital_silence_is_safe(self):
        out = normalize_for_asr(np.zeros(1024, dtype=np.float32))
        self.assertEqual(out.gain, 1.0)
        self.assertEqual(out.rms, 0.0)

    def test_empty_is_safe(self):
        self.assertEqual(normalize_for_asr(np.zeros(0, dtype=np.float32)).gain, 1.0)

    def test_a_transient_limits_the_gain_instead_of_clipping(self):
        # Quiet speech with one loud knock in it: RMS says "lift a lot", the peak says "you can't".
        quiet = self._at_rms(ASR_NORMALIZE_TARGET_RMS / 4)
        quiet[100] = 0.9
        out = normalize_for_asr(quiet)
        self.assertLessEqual(float(np.max(np.abs(out.samples))), ASR_NORMALIZE_PEAK_CEILING + 1e-6)

    def test_never_makes_a_buffer_hotter_than_the_ceiling(self):
        # The invariant across every input level: if this touched the audio at all, the result sits
        # under the peak ceiling — and it never raises the peak of something already above it.
        for level in (0.0001, 0.001, 0.005, 0.02, 0.06, 0.3):
            with self.subTest(level=level):
                src = self._at_rms(level, seed=1)
                out = normalize_for_asr(src)
                self.assertLessEqual(float(np.max(np.abs(out.samples))),
                                     max(float(np.max(np.abs(src))), ASR_NORMALIZE_PEAK_CEILING))

    def test_output_stays_float32(self):
        self.assertEqual(normalize_for_asr(self._at_rms(0.01)).samples.dtype, np.float32)


if __name__ == "__main__":
    unittest.main()
