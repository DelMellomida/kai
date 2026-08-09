import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from ai.audio_debug import UtteranceRecorder


def _speech(n=1600, seed=0):
    return (np.random.default_rng(seed).standard_normal(n) * 2000).astype(np.int16)


class _TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "clips"
        self.addCleanup(self._tmp.cleanup)

    def _rec(self, **kw):
        kw.setdefault("enabled", True)
        kw.setdefault("max_files", 100)
        kw.setdefault("max_mb", 10.0)
        return UtteranceRecorder(str(self.dir), **kw)

    def _index(self):
        path = self.dir / "index.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestDisabledByDefault(_TempDirCase):
    def test_disabled_writes_nothing_and_creates_no_directory(self):
        rec = UtteranceRecorder(str(self.dir), enabled=False)
        self.assertEqual(rec.record(_speech(), 16000, "turn"), "")
        self.assertFalse(self.dir.exists())

    def test_annotate_on_a_disabled_recorder_is_a_no_op(self):
        UtteranceRecorder(str(self.dir), enabled=False).annotate("0001-turn", outcome="done")
        self.assertFalse(self.dir.exists())


class TestRecording(_TempDirCase):
    def test_writes_a_readable_16k_mono_wav(self):
        rec = self._rec()
        pcm = _speech(1600)
        clip = rec.record(pcm, 16000, "turn")
        self.assertEqual(clip, "0001-turn")
        with wave.open(str(self.dir / "0001-turn.wav"), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), 16000)
            back = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        # The corpus is only useful if the WAV is the audio Kai actually had, sample for sample.
        np.testing.assert_array_equal(back, pcm)

    def test_metadata_goes_to_the_index(self):
        rec = self._rec()
        rec.record(_speech(), 16000, "turn", reason="hangover", rms=812.5)
        row = self._index()[0]
        self.assertEqual(row["clip"], "0001-turn")
        self.assertEqual(row["event"], "captured")
        self.assertEqual(row["reason"], "hangover")
        self.assertEqual(row["rms"], 812.5)
        self.assertAlmostEqual(row["seconds"], 0.1, places=3)

    def test_annotate_appends_a_second_line_for_the_same_clip(self):
        rec = self._rec()
        clip = rec.record(_speech(), 16000, "turn")
        rec.annotate(clip, outcome="done", text="what time is it")
        rows = self._index()
        self.assertEqual([r["event"] for r in rows], ["captured", "result"])
        self.assertEqual(rows[1]["clip"], clip)
        self.assertEqual(rows[1]["text"], "what time is it")

    def test_clip_ids_increment(self):
        rec = self._rec()
        self.assertEqual([rec.record(_speech(), 16000, "turn") for _ in range(3)],
                         ["0001-turn", "0002-turn", "0003-turn"])

    def test_empty_audio_is_not_recorded(self):
        rec = self._rec()
        self.assertEqual(rec.record(np.zeros(0, dtype=np.int16), 16000, "turn"), "")
        self.assertEqual(rec.written, 0)

    def test_kinds_filter_excludes_the_scan_path(self):
        rec = self._rec(kinds=("turn",))
        self.assertNotEqual(rec.record(_speech(), 16000, "turn"), "")
        self.assertEqual(rec.record(_speech(), 16000, "scan"), "")
        self.assertEqual(rec.written, 1)


class TestBounds(_TempDirCase):
    def test_file_cap_stops_recording(self):
        rec = self._rec(max_files=2)
        clips = [rec.record(_speech(), 16000, "turn") for _ in range(4)]
        self.assertEqual(clips, ["0001-turn", "0002-turn", "", ""])
        self.assertEqual(rec.written, 2)
        self.assertEqual(rec.skipped, 2)

    def test_size_cap_stops_recording(self):
        # 1600 samples = 3200 bytes + header, so one clip is well over a 0.000001 MB budget.
        rec = self._rec(max_mb=0.000001)
        self.assertNotEqual(rec.record(_speech(), 16000, "turn"), "")
        self.assertEqual(rec.record(_speech(), 16000, "turn"), "")

    def test_caps_survive_a_restart(self):
        """The reason _prepare adopts what is already there: a fresh counter on every boot would
        overwrite clip 0001 and believe the disk was empty, so neither cap would ever bind."""
        first = self._rec(max_files=3)
        first.record(_speech(), 16000, "turn")
        first.record(_speech(), 16000, "turn")

        second = self._rec(max_files=3)
        self.assertEqual(second.record(_speech(), 16000, "turn"), "0003-turn")
        self.assertEqual(len(list(self.dir.glob("*.wav"))), 3)


class TestFailuresAreNeverFatal(_TempDirCase):
    def test_an_unusable_directory_disables_rather_than_raises(self):
        with patch.object(Path, "mkdir", side_effect=OSError("read-only file system")):
            rec = self._rec()
        self.assertFalse(rec.enabled)
        self.assertIn("read-only", rec.error)
        self.assertEqual(rec.record(_speech(), 16000, "turn"), "")

    def test_a_write_error_is_counted_but_does_not_disable(self):
        """A momentarily full /tmp must not silently switch the corpus off for the rest of the run."""
        rec = self._rec()
        with patch("ai.audio_debug.wave.open", side_effect=OSError("no space left on device")):
            self.assertEqual(rec.record(_speech(), 16000, "turn"), "")
        self.assertTrue(rec.enabled)
        self.assertEqual(rec.skipped, 1)
        self.assertNotEqual(rec.record(_speech(), 16000, "turn"), "")

    def test_unserialisable_metadata_does_not_raise(self):
        rec = self._rec()
        clip = rec.record(_speech(), 16000, "turn", weird=object())
        # The WAV is what matters; a metadata value json cannot render must not lose it.
        self.assertNotEqual(clip, "")
        self.assertTrue((self.dir / f"{clip}.wav").is_file())


class TestStatus(_TempDirCase):
    def test_status_reports_the_counters(self):
        rec = self._rec()
        rec.record(_speech(), 16000, "turn")
        status = rec.status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["written"], 1)
        self.assertEqual(status["error"], "")

    def test_disabled_status_hides_the_path(self):
        self.assertEqual(UtteranceRecorder(str(self.dir), enabled=False).status()["dir"], "")


if __name__ == "__main__":
    unittest.main()
