#!/usr/bin/env python3
"""Does every filler line actually SOUND like the words it is written as?

Run this on the robot after adding or editing a line in config/filler.py. It is the only check
that catches the failure class that file's docstring warns about, because the failure is
inaudible from the string: espeak-ng (Piper's phonemizer) reads some inputs as initialisms and
spells them out letter by letter. "Hmmmm..." came back from Whisper as "H-A-M-A-M-M" — the text
looked fine, the audio was gibberish, and nothing but synthesis-then-transcription would have
found it. tests/test_filler.py catches the patterns we know about; this catches the rest.

    python3 -m scripts.filler_check              # every line
    python3 -m scripts.filler_check --lang ceb   # one language
    python3 -m scripts.filler_check --keep out/  # keep the WAVs to listen to yourself

For each line: synthesize with Piper, measure the WAV, transcribe with Whisper, and compare the
transcript to the original as a bag of words. A low overlap means the audio does not say what
the text says. The comparison is deliberately loose — Whisper will punctuate differently, and it
is transcribing Tagalog and Bisaya through a model that mostly saw English, so exact-match would
flag every single line. What it reliably separates is "roughly the same words" from "letters
being spelled out", which is the failure being hunted.

REVIEW THE FLAGS BY EAR before rewriting anything. A Bisaya line scoring low may be perfectly
intelligible audio that Whisper simply cannot transcribe — that is a limit of the checker, not
a defect in the line. --keep exists for exactly that call.

Needs Piper and the Whisper model, so this is a ROBOT script: it will not run on a dev box
without the voices. It costs one Piper run and one Whisper pass per line (~40 of each), so
budget a couple of minutes, and do not run it during a demo — it competes with the voice
pipeline for CPU.
"""

import argparse
import re
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import filler, tts                                          # noqa: E402
from ai import voice_assistant as va                                # noqa: E402

# Below this share of the written words surviving into the transcript, the line is flagged. Set
# by measurement, not taste: the good English lines land at 0.85-1.0, the good Tagalog ones at
# 0.55-0.85 (Whisper mis-hears particles like "nga"/"po" but keeps the content words), and the
# known-bad "Hmmmm..." case scores 0.0. Anything in between deserves a human ear.
MIN_WORD_OVERLAP = 0.5

# A stall that runs long stops being interruptible, which is the only thing they are for.
MAX_STALL_S = 1.2


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w}


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """A Piper WAV as the int16 mono array _transcribe wants, plus its rate. Piper writes mono
    16-bit, so this does not need to handle the general case — but it takes the rate from the
    header rather than assuming, because _transcribe resamples on it and a wrong rate would
    transcribe a chipmunk."""
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="int16"), rate


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lang", help="only this language bank (tl, ceb, en)")
    ap.add_argument("--keep", metavar="DIR", help="write the WAVs here instead of a temp dir")
    args = ap.parse_args()

    lines = {k: v for k, v in filler.canned_lines().items()
             if not args.lang or f"_{args.lang}_" in k}
    if not lines:
        print(f"no lines for --lang {args.lang!r}", file=sys.stderr)
        return 1
    if not tts.enabled():
        print("TTS is disabled — nothing to synthesize", file=sys.stderr)
        return 1

    out_dir = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="kai_filler_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    assistant = va.VoiceAssistant()
    print(f"{len(lines)} line(s) -> {out_dir}\n")

    flagged: list[tuple[str, str, str, float]] = []
    for key, text in lines.items():
        wav = tts.synthesize_to(text, out_dir / f"{key}.wav")
        if wav is None:
            print(f"  SYNTH-FAIL  {key}")
            flagged.append((key, text, "<synthesis failed>", 0.0))
            continue
        secs = tts.wav_duration(wav)
        audio, rate = read_wav(wav)
        # log_language=False: this loop would otherwise print a detected-language line per line and
        # bury the table. scan=False so it uses the same turn model a real reply is transcribed by
        # — checking against the fast spotting model would pass lines that the real path garbles.
        heard = assistant._transcribe(audio, rate=rate, log_language=False)
        want = words(text)
        overlap = len(want & words(heard)) / len(want) if want else 0.0

        notes = []
        if overlap < MIN_WORD_OVERLAP:
            notes.append("GARBLED?")
        if key.startswith(filler.STALL_PREFIX) and secs > MAX_STALL_S:
            notes.append(f"LONG {secs:.2f}s")
        tag = " ".join(notes) or "ok"
        print(f"  {tag:<14} {key:<20} {overlap:.2f}  {secs:4.2f}s  {heard[:60]!r}")
        if notes:
            flagged.append((key, text, heard, overlap))

    print(f"\n{len(lines) - len(flagged)}/{len(lines)} clean")
    if flagged:
        print("\nReview these by ear before rewriting — a low score can be Whisper's limit, not "
              "the line's:")
        for key, text, heard, overlap in flagged:
            print(f"\n  {key}  (overlap {overlap:.2f})")
            print(f"    wrote: {text}")
            print(f"    heard: {heard}")
        print(f"\n  WAVs: {out_dir}")
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
