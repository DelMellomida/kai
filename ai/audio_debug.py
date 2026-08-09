"""Optional on-disk capture of the utterances Kai hears, for offline tuning.

Why this exists: every audio constant in config/wake.py is annotated with a room, a date and a
measured distribution, and every one of those measurements was taken by hand at a desk in a quiet
room. There is no way to re-take them in a venue — you cannot stand at the back of a hall with a
laptop reading /params — and there is no recording of what Kai actually heard when it failed. So
the tuning that matters most is the tuning we have the least evidence for.

Turning this on makes the robot collect that evidence itself: every harvested utterance is written
as a WAV, next to a JSONL line holding the numbers that were live when it was captured (RMS, the
adapted floors, which wake tier won, how it was classified) and a second line, added when the
transcript comes back, holding what Whisper made of it. That is a labelled corpus of exactly the
failures we care about, gathered in the room where they happen, at the cost of pressing one switch.

Everything here is best-effort and bounded. It runs on the session thread in the middle of a turn,
so a full disk, a read-only mount or a path that does not exist must cost a counter and nothing
else — never an exception, never a stalled turn. Disk use is capped in two independent ways
(DEBUG_CAPTURE_MAX_FILES and DEBUG_CAPTURE_MAX_MB) and the caps are cumulative across restarts,
because "bounded" has to mean bounded on a robot that gets rebooted a dozen times a day.

stdlib only — `wave` and `json`. Nothing here may add a dependency to the voice path.
"""

from __future__ import annotations

import json
import threading
import time
import wave
from pathlib import Path

import numpy as np


class UtteranceRecorder:
    """Writes captured utterances and their metadata to a directory. Disabled by default.

    One instance per session, shared by every path that harvests audio. Thread-safe: the turn path
    and the wake-scan path both call in, from different threads, and the index must not interleave.
    """

    def __init__(self, directory: str, enabled: bool = False, max_files: int = 0,
                 max_mb: float = 0.0, kinds: tuple[str, ...] = ()) -> None:
        self.enabled = bool(enabled)
        self.dir = Path(directory).expanduser()
        self.max_files = int(max_files)
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.kinds = tuple(kinds)

        self._lock = threading.Lock()
        self._index = 0
        self._bytes = 0
        self.written = 0
        self.skipped = 0
        self.error: str | None = None
        self._capped_logged = False

        if self.enabled:
            self._prepare()

    # ── setup ───────────────────────────────────────────────────────────────

    def _prepare(self) -> None:
        """Create the directory and adopt whatever is already in it.

        Adopting rather than starting from zero is what makes the caps hold across restarts: a
        fresh counter would overwrite `0001-turn.wav` on every boot and would believe the disk was
        empty each time, so the two limits below would never be reached and the "bounded" claim in
        the module docstring would be false.
        """
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            existing = sorted(self.dir.glob("*.wav"))
            self._bytes = sum(p.stat().st_size for p in existing)
            for path in existing:
                head = path.name.split("-", 1)[0]
                if head.isdigit():
                    self._index = max(self._index, int(head))
            print(f"[audio-debug] recording utterances to {self.dir} "
                  f"({len(existing)} already there, {self._bytes / 1e6:.1f} MB)", flush=True)
        except OSError as exc:
            self.enabled = False
            self.error = f"{type(exc).__name__}: {exc}"
            print(f"[audio-debug] disabled — cannot use {self.dir}: {self.error}", flush=True)

    # ── the two entry points ────────────────────────────────────────────────

    def record(self, audio: np.ndarray, rate: int, kind: str, **meta) -> str:
        """Write one utterance. Returns its clip id, or "" if nothing was written.

        The id is what annotate() takes; callers should hold on to it and pass it back when the
        transcript arrives, but treat "" as "recording is off" and skip the follow-up.
        """
        if not self.enabled or audio is None or getattr(audio, "size", 0) == 0:
            return ""
        if self.kinds and kind not in self.kinds:
            return ""
        with self._lock:
            if self._capped():
                self.skipped += 1
                return ""
            self._index += 1
            clip = f"{self._index:04d}-{kind}"
        # Outside the lock: a WAV write is milliseconds of I/O on a shared filesystem, and the two
        # callers are on the session's tick thread mid-turn. Only the counter allocation above needs
        # to be serialised — the filename it produced is already unique to this call.
        path = self.dir / f"{clip}.wav"
        try:
            pcm = np.asarray(audio, dtype=np.int16).reshape(-1)
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(int(rate))
                wav.writeframes(pcm.tobytes())
            size = path.stat().st_size
        except (OSError, ValueError) as exc:
            self._fail(f"write {path.name}: {type(exc).__name__}: {exc}")
            return ""
        with self._lock:
            self._bytes += size
            self.written += 1
        self._append({"clip": clip, "event": "captured", "t": time.time(),
                      "rate": int(rate), "samples": int(pcm.size),
                      "seconds": round(pcm.size / max(1, int(rate)), 3), **meta})
        return clip

    def annotate(self, clip: str, **fields) -> None:
        """Attach later-arriving facts (the transcript, the outcome) to an already-written clip.

        Appended as a second line rather than merged into the first: the audio is written on the
        capture path and the transcript arrives on a worker thread seconds later, and rewriting a
        file from two threads to save one line of JSON would be a bad trade.
        """
        if not self.enabled or not clip:
            return
        self._append({"clip": clip, "event": "result", "t": time.time(), **fields})

    # ── internals ───────────────────────────────────────────────────────────

    def _capped(self) -> bool:
        """True once either limit is reached. Logs the first time, then stays quiet."""
        hit = ((self.max_files and self.written >= self.max_files) or
               (self.max_bytes and self._bytes >= self.max_bytes))
        if hit and not self._capped_logged:
            self._capped_logged = True
            print(f"[audio-debug] cap reached ({self.written} files, "
                  f"{self._bytes / 1e6:.1f} MB) — no longer recording. Clear {self.dir} to resume.",
                  flush=True)
        return bool(hit)

    def _append(self, row: dict) -> None:
        """One JSON object per line in index.jsonl. Under the lock — two threads append here."""
        try:
            with self._lock:
                with (self.dir / "index.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, default=str) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            self._fail(f"index: {type(exc).__name__}: {exc}")

    def _fail(self, reason: str) -> None:
        """Record a failure without ever raising into the turn.

        Deliberately does NOT disable recording: a single transient write error (a momentarily full
        /tmp) should not silently switch the corpus off for the rest of the run. `error` and
        `skipped` are published on /params, which is where a persistent problem shows up.
        """
        self.error = reason
        self.skipped += 1
        print(f"[audio-debug] WARNING: {reason}", flush=True)

    def status(self) -> dict:
        """Counters for /params."""
        with self._lock:
            return {
                "enabled": self.enabled,
                "written": self.written,
                "skipped": self.skipped,
                "mb": round(self._bytes / 1e6, 1),
                "dir": str(self.dir) if self.enabled else "",
                "error": self.error or "",
            }
