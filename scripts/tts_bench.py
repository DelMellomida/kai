#!/usr/bin/env python3
"""A/B Kai's voice candidates on the robot: how natural, how fast, how much RAM.

Run this ON THE JETSON. It answers the two questions that decide a voice swap and cannot be
answered anywhere else:

  1. Does it fit?    Peak RSS of the synth process, measured — not the vendor's claim. The budget is
                     ~2.0-2.3 GB free with the camera and Ollama up (docs/memory-budget.md), and the
                     synth spike lands on top of whatever else the turn is doing.
  2. Is it fast?     Real-time factor (synth seconds per audio second) AND absolute latency. RTF is
                     the number people quote; the one Kai actually feels is "seconds of dead air
                     before the reply starts", which includes process startup — so both are printed.

Naturalness it cannot answer. That is what --play is for: it renders each candidate through the
SAME sox chain and the SAME PulseAudio sink the real replies use, because a voice judged on
headphones is not the voice that comes out of a PAM8403 and a 3 W speaker.

    python3 -m scripts.tts_bench                    # measure every candidate, write WAVs
    python3 -m scripts.tts_bench --play             # ...and play each one through Kai's speaker
    python3 -m scripts.tts_bench --sids 0,4,9,20    # sweep Kokoro speaker ids to pick a voice
    python3 -m scripts.tts_bench --only kokoro      # just one family
    python3 -m scripts.tts_bench --threads 1,2,4    # find the core count that pays

Kokoro candidates need scripts/tts_setup_kokoro.sh first; without it they are skipped with a note
rather than failing the run, so the Piper-only comparison still works on a bare checkout.

This competes with the voice pipeline for CPU — do not run it mid-demo. Nothing here writes to
config/voice.py: picking a winner is still a deliberate one-line edit.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.voice import (                                          # noqa: E402
    TTS_SINK, TTS_XDG_RUNTIME, TTS_LATENCY_MSEC,
    TTS_POST_SOX, TTS_POST_CHANNELS, TTS_POST_EFFECTS,
)

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path("/tmp/kai_tts_bench")

# Three lengths, because RTF is not flat in length and the short one is where process startup
# dominates. Text shaped like what Kai actually says: a wake ack, a one-line answer, a RAG answer.
LINES = {
    "short":  "Yes?",
    "medium": "DEVCON Philippines is a community of technologists, and I help out at their events.",
    "long":   ("Geeks on a Beach is DEVCON's flagship conference — it brings together developers, "
               "designers and founders from across the region for a few days of talks and "
               "workshops, usually somewhere with a coastline. The Jumpstart internship program "
               "is the one aimed at students."),
}

# Repeats per candidate per line. The first run of any engine pays a cold page-cache cost for the
# model file, so the reported figures are over runs after the first.
WARMUP_RUNS = 1
TIMED_RUNS = 3

KOKORO_DIR = ROOT / "voices" / "kokoro"
SHERPA_BIN = ROOT / "vendor" / "sherpa-onnx" / "bin" / "sherpa-onnx-offline-tts"
SHERPA_LIB = ROOT / "vendor" / "sherpa-onnx" / "lib"


@dataclass
class Candidate:
    name: str
    argv: list[str]                     # {out} placeholder is substituted per run
    stdin_text: bool = False            # Piper takes text on stdin; sherpa takes it as an argument
    env: dict[str, str] = field(default_factory=dict)
    note: str = ""


def piper_candidates(voices: list[str], scales: list[float] | None = None,
                     sids: list[int] | None = None) -> list[Candidate]:
    """One candidate per (voice, speaker, length-scale).

    `scales` drives the speaking-rate A/B: Piper's --length-scale is >1 slower, <1 faster, and it is
    the same knob the dashboard exposes as tts_length_scale — so a winner here transfers to
    config/voice.py unchanged.

    `sids` matters more than it looks: en_US-libritts_r-medium is a **904-speaker** model, not one
    voice. Benching only speaker 0 measured one arbitrary person out of nine hundred. Single-speaker
    models (num_speakers=1 in the .onnx.json) ignore -s, so passing it is harmless there."""
    out = []
    for v in voices:
        model = ROOT / "voices" / f"{v}.onnx"
        if not model.is_file():
            continue
        n_spk = 1
        try:
            import json
            n_spk = json.loads((model.with_suffix(".onnx.json")).read_text(
                encoding="utf-8")).get("num_speakers", 1)
        except (OSError, ValueError):
            pass
        # A single-speaker model gets exactly one candidate however many sids were asked for —
        # otherwise a --piper-sids sweep silently renders the same audio N times.
        want = [None] if n_spk <= 1 else (sids or [0])
        for sid in want:
            for scale in (scales or [1.0]):
                # Both the speaker and the scale go in the NAME, not just the argv — otherwise a
                # sweep writes every variant to one filename and the last one silently wins.
                sid_part = "" if sid is None else f"/s{sid}"
                scale_part = "" if scale == 1.0 and not scales else f"/ls{scale:g}"
                out.append(Candidate(
                    name=f"piper/{v}{sid_part}{scale_part}",
                    argv=["python3", "-m", "piper", "-m", str(model),
                          *([] if sid is None else ["-s", str(sid)]),
                          "--length-scale", str(scale), "-f", "{out}"],
                    stdin_text=True,
                ))
    return out


def kokoro_candidates(sids: list[int], threads: int, model_dir: Path) -> list[Candidate]:
    # int8 builds name the weights model.int8.onnx, fp32 ones model.onnx — glob so --kokoro-dir can
    # point at either and the quantization A/B needs no code change.
    models = sorted(model_dir.glob("model*.onnx"))
    if not SHERPA_BIN.is_file() or not models:
        return []
    model = models[0]
    extra = []
    if (model_dir / "dict").is_dir():
        extra.append(f"--kokoro-dict-dir={model_dir / 'dict'}")
    lex = sorted(model_dir.glob("lexicon*.txt"))
    if lex:
        extra.append("--kokoro-lexicon=" + ",".join(str(p) for p in lex))
    return [
        Candidate(
            name=f"kokoro/sid{sid}/t{threads}",
            argv=[
                str(SHERPA_BIN),
                f"--kokoro-model={model}",
                f"--kokoro-voices={model_dir / 'voices.bin'}",
                f"--kokoro-tokens={model_dir / 'tokens.txt'}",
                f"--kokoro-data-dir={model_dir / 'espeak-ng-data'}",
                *extra,
                f"--num-threads={threads}",
                f"--sid={sid}",
                "--output-filename={out}",
                "{text}",
            ],
            env={"LD_LIBRARY_PATH": f"{SHERPA_LIB}:{os.environ.get('LD_LIBRARY_PATH', '')}"},
        )
        for sid in sids
    ]


def _sherpa_env() -> dict[str, str]:
    """The one thing every sherpa-onnx candidate needs: the bundled shared libs on the loader path,
    MERGED over whatever is already there rather than replacing it."""
    return {"LD_LIBRARY_PATH": f"{SHERPA_LIB}:{os.environ.get('LD_LIBRARY_PATH', '')}"}


def _first(model_dir: Path, *patterns: str) -> Path | None:
    """First file in `model_dir` matching any of `patterns`, in the order given.

    Every sherpa model family ships its weights under a slightly different name depending on the
    quantization of the build (`model.onnx` vs `model.int8.onnx` vs `model.fp16.onnx`), so the
    builders glob rather than assume — the same reason kokoro_candidates does."""
    for pat in patterns:
        hits = sorted(model_dir.glob(pat))
        if hits:
            return hits[0]
    return None


def matcha_candidates(model_dir: Path, threads: int, scale: float = 1.0) -> list[Candidate]:
    """Matcha-TTS: a flow-matching acoustic model plus a SEPARATE neural vocoder.

    The vocoder is the part that trips people up — it ships in its own release
    (`vocoder-models`, e.g. vocos-22khz-univ.onnx), not inside the model tarball, and sherpa aborts
    without it. Looked up in the model dir first, then voices/vocoders/, so either layout works.

    This is the candidate most likely to answer the actual complaint: a different architecture from
    VITS, and the one of the bunch with a reputation for prosody rather than raw speed."""
    acoustic = _first(model_dir, "model-steps-*.onnx", "model*.onnx")
    tokens = model_dir / "tokens.txt"
    if not (SHERPA_BIN.is_file() and acoustic and tokens.is_file()):
        return []
    vocoder = _first(model_dir, "vocos*.onnx", "hifigan*.onnx") or \
        _first(ROOT / "voices" / "vocoders", "vocos*.onnx", "hifigan*.onnx")
    if vocoder is None:
        print(f"note: {model_dir.name} has no vocoder — fetch vocos-22khz-univ.onnx "
              f"into voices/vocoders/ (see scripts/tts_setup_models.sh)")
        return []
    extra = []
    if (model_dir / "espeak-ng-data").is_dir():
        extra.append(f"--matcha-data-dir={model_dir / 'espeak-ng-data'}")
    lex = sorted(model_dir.glob("lexicon*.txt"))
    if lex and not extra:          # --matcha-data-dir wins outright: sherpa ignores the lexicon then
        extra.append("--matcha-lexicon=" + ",".join(str(p) for p in lex))
    return [Candidate(
        name=f"matcha/{model_dir.name}/t{threads}/ls{scale:g}",
        argv=[
            str(SHERPA_BIN),
            f"--matcha-acoustic-model={acoustic}",
            f"--matcha-vocoder={vocoder}",
            f"--matcha-tokens={tokens}",
            *extra,
            f"--matcha-length-scale={scale}",
            f"--num-threads={threads}",
            "--output-filename={out}",
            "{text}",
        ],
        env=_sherpa_env(),
        note=f"vocoder {vocoder.name}",
    )]


def kitten_candidates(model_dir: Path, threads: int, sids: list[int],
                      scale: float = 1.0) -> list[Candidate]:
    """KittenTTS: the smallest of the families sherpa supports (~15M in the nano build).

    Here to establish the speed ceiling — if even this is slow on the Jetson, no offline neural
    engine is going to be fast enough and the answer is a Piper voice plus the persistent worker."""
    model = _first(model_dir, "model*.onnx")
    tokens = model_dir / "tokens.txt"
    voices = model_dir / "voices.bin"
    if not (SHERPA_BIN.is_file() and model and tokens.is_file()):
        return []
    extra = [f"--kitten-voices={voices}"] if voices.is_file() else []
    if (model_dir / "espeak-ng-data").is_dir():
        extra.append(f"--kitten-data-dir={model_dir / 'espeak-ng-data'}")
    return [
        Candidate(
            name=f"kitten/{model_dir.name}/sid{sid}/t{threads}",
            argv=[
                str(SHERPA_BIN),
                f"--kitten-model={model}",
                f"--kitten-tokens={tokens}",
                *extra,
                f"--kitten-length-scale={scale}",
                f"--num-threads={threads}",
                f"--sid={sid}",
                "--output-filename={out}",
                "{text}",
            ],
            env=_sherpa_env(),
        )
        for sid in sids
    ]


def supertonic_candidates(model_dir: Path, threads: int, sids: list[int]) -> list[Candidate]:
    """Supertonic: four separate ONNX graphs (duration predictor, text encoder, vector estimator,
    vocoder) plus a JSON config, a unicode indexer and a voice-style blob.

    Every one of those paths is mandatory, so this returns nothing rather than half a command line
    if the tarball did not unpack cleanly — sherpa's failure for a missing path is an abort, not a
    warning."""
    need = {
        "duration-predictor": _first(model_dir, "duration_predictor*.onnx"),
        "text-encoder":       _first(model_dir, "text_encoder*.onnx"),
        "vector-estimator":   _first(model_dir, "vector_estimator*.onnx"),
        "vocoder":            _first(model_dir, "vocoder*.onnx"),
    }
    tts_json = model_dir / "tts.json"
    indexer = model_dir / "unicode_indexer.bin"
    voice = model_dir / "voice.bin"
    if not (SHERPA_BIN.is_file() and all(need.values())
            and tts_json.is_file() and indexer.is_file() and voice.is_file()):
        return []
    return [
        Candidate(
            name=f"supertonic/{model_dir.name}/sid{sid}/t{threads}",
            argv=[
                str(SHERPA_BIN),
                *(f"--supertonic-{k}={v}" for k, v in need.items()),
                f"--supertonic-tts-json={tts_json}",
                f"--supertonic-unicode-indexer={indexer}",
                f"--supertonic-voice-style={voice}",
                f"--num-threads={threads}",
                f"--sid={sid}",
                "--output-filename={out}",
                "{text}",
            ],
            env=_sherpa_env(),
        )
        for sid in sids
    ]


def pocket_candidates(model_dir: Path, threads: int,
                      refs: list[Path] | None = None) -> list[Candidate]:
    """Pocket-TTS: voice CLONING from a reference clip, via --reference-audio.

    This is the only offline family here that can escape the read-aloud ceiling. Every other model
    in this script was trained on audiobook corpora — LJSpeech, LibriTTS, Lessac, HFC are all one
    person reading a book — so they sound like narration no matter whose voice does the narrating.
    A clone inherits the reference speaker's *delivery* as well as their timbre, so a conversational
    reference gives conversational output. That is the whole reason this is worth measuring.

    Unlike ZipVoice, Pocket needs only the audio, not a transcript of it."""
    need = {
        "lm-flow":           _first(model_dir, "lm_flow*.onnx"),
        "lm-main":           _first(model_dir, "lm_main*.onnx"),
        "encoder":           _first(model_dir, "encoder*.onnx"),
        "decoder":           _first(model_dir, "decoder*.onnx"),
        "text-conditioner":  _first(model_dir, "text_conditioner*.onnx"),
    }
    vocab = model_dir / "vocab.json"
    scores = model_dir / "token_scores.json"
    if not (SHERPA_BIN.is_file() and all(need.values())
            and vocab.is_file() and scores.is_file()):
        return []
    refs = refs or sorted((model_dir / "test_wavs").glob("*.wav"))
    return [
        Candidate(
            name=f"pocket/{ref.stem}/t{threads}",
            argv=[
                str(SHERPA_BIN),
                *(f"--pocket-{k}={v}" for k, v in need.items()),
                f"--pocket-vocab-json={vocab}",
                f"--pocket-token-scores-json={scores}",
                f"--reference-audio={ref}",
                f"--num-threads={threads}",
                "--output-filename={out}",
                "{text}",
            ],
            env=_sherpa_env(),
            note=f"cloned from {ref.name}",
        )
        for ref in refs
    ]


def _sample_peak_rss(pid: int, stop: threading.Event, result: dict) -> None:
    """Poll /proc/<pid>/status for VmHWM until the process exits.

    VmHWM is the KERNEL's own high-water mark, not an instantaneous reading — so this is a true
    peak as long as we manage one read before exit, rather than a sampled approximation that can
    miss a spike between polls. A synth that dies faster than the first poll reports 0, which is
    why the short line is run several times."""
    path = Path(f"/proc/{pid}/status")
    peak = 0
    while not stop.is_set():
        try:
            for line in path.read_text().splitlines():
                if line.startswith("VmHWM:"):
                    peak = max(peak, int(line.split()[1]))   # kB
                    break
        except (OSError, ValueError, IndexError):
            break        # process gone; keep the last good read
        time.sleep(0.01)
    result["peak_kb"] = peak


def run_once(cand: Candidate, text: str, out: Path) -> tuple[float, int] | None:
    """Synthesize `text` to `out`. Returns (wall_seconds, peak_rss_kB), or None on failure.

    Wall time is measured around the WHOLE subprocess — spawn, interpreter start, model load,
    inference, WAV write — because that is the dead air a person hears, and it is exactly where the
    Python-in-the-loop engines lose to the C++ one."""
    argv = [a.replace("{out}", str(out)).replace("{text}", text) for a in cand.argv]
    env = {**os.environ, **cand.env}
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv, env=env,
            stdin=subprocess.PIPE if cand.stdin_text else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except OSError as exc:
        print(f"    ! could not run {cand.name}: {exc}")
        return None

    stop = threading.Event()
    rss: dict = {"peak_kb": 0}
    sampler = threading.Thread(target=_sample_peak_rss, args=(proc.pid, stop, rss), daemon=True)
    sampler.start()

    _, stderr = proc.communicate(input=text.encode("utf-8") if cand.stdin_text else None)
    elapsed = time.monotonic() - t0
    stop.set()
    sampler.join(timeout=1.0)

    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        err = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        print(f"    ! {cand.name} failed: {err[-1] if err else f'exit {proc.returncode}'}")
        return None
    return elapsed, rss["peak_kb"]


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        return w.getnframes() / rate if rate else 0.0


def post_process(src: Path, dst: Path) -> Path:
    """Apply the SAME sox chain ai/tts.py ships, so --play is judging shipping audio.

    Skipping this would flatter the quieter engine and mislead on the loudness the compressor
    actually delivers. Falls back to the raw WAV, exactly as ai/tts.py does."""
    cmd = [TTS_POST_SOX, str(src), "-c", str(TTS_POST_CHANNELS), str(dst), *TTS_POST_EFFECTS]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return src
    return dst if proc.returncode == 0 and dst.is_file() and dst.stat().st_size else src


def play(path: Path) -> None:
    cmd = ["paplay", f"--device={TTS_SINK}"]
    if TTS_LATENCY_MSEC:
        cmd.append(f"--latency-msec={int(TTS_LATENCY_MSEC)}")
    cmd.append(str(path))
    subprocess.run(cmd, env={**os.environ, "XDG_RUNTIME_DIR": TTS_XDG_RUNTIME},
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def bench(cand: Candidate, keys: list[str], do_play: bool,
          out_dir: Path = None, runs_each: int = TIMED_RUNS) -> None:
    out_dir = out_dir or OUT_DIR
    print(f"\n{cand.name}", flush=True)
    if cand.note:
        print(f"  ({cand.note})", flush=True)
    for key in keys:
        text = LINES[key]
        raw = out_dir / f"{cand.name.replace('/', '_')}_{key}_raw.wav"
        final = out_dir / f"{cand.name.replace('/', '_')}_{key}.wav"

        # runs_each=1 with no warm-up is the AUDITION mode: the timings are then cold-cache and not
        # comparable, but the WAVs are identical and it renders a wide sweep in a fraction of the
        # time. Naturalness is judged by ear, so paying 4x for tighter numbers buys nothing there.
        for _ in range(WARMUP_RUNS if runs_each > 1 else 0):
            if run_once(cand, text, raw) is None:
                return
        runs = [run_once(cand, text, raw) for _ in range(runs_each)]
        if any(r is None for r in runs):
            return

        times = sorted(r[0] for r in runs)
        peak_kb = max(r[1] for r in runs)
        median = times[len(times) // 2]
        audio_s = wav_duration(raw)
        rtf = median / audio_s if audio_s else float("nan")

        played = post_process(raw, final)
        # flush: a long sweep is usually watched over SSH, where stdout is a pipe and Python would
        # otherwise hold every line until the run ends — which is the whole run, for `lessac-high`.
        print(f"  {key:<6} audio {audio_s:5.2f}s | synth {median:5.2f}s "
              f"(min {times[0]:.2f}) | RTF {rtf:5.2f} | peak RSS {peak_kb / 1024:6.0f} MB",
              flush=True)
        if do_play:
            play(played)
            time.sleep(0.3)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--play", action="store_true",
                    help="play each result through Kai's speaker (the only naturalness test)")
    ap.add_argument("--only", default="", help="substring filter on candidate name, e.g. kokoro")
    ap.add_argument("--sids", default="0",
                    help="comma-separated Kokoro speaker ids to compare (see the model's voice list)")
    ap.add_argument("--threads", default="2",
                    help="comma-separated --num-threads values for Kokoro. The tracking loop and "
                         "Whisper already contend for the 6 cores, so more is not free")
    ap.add_argument("--lines", default="short,medium,long",
                    help=f"which lines to synthesize: {','.join(LINES)}")
    ap.add_argument("--kokoro-dir", default=str(KOKORO_DIR),
                    help="alternative Kokoro model dir (e.g. the fp32 build, to A/B quantization)")
    ap.add_argument("--length-scales", default="",
                    help="comma-separated Piper --length-scale values to sweep (e.g. "
                         "0.95,1.00,1.05,1.10). Empty = 1.0 only. >1 is slower")
    ap.add_argument("--out-dir", default="",
                    help="where to write the WAVs. Point this INSIDE the repo to audition from "
                         "Windows — /tmp is not on the network share, the project directory is")
    ap.add_argument("--runs", type=int, default=TIMED_RUNS,
                    help="timed runs per line. 1 = audition mode (no warm-up, cold timings, "
                         "same audio, ~4x faster to render a wide sweep)")
    ap.add_argument("--voices", default="",
                    help="comma-separated Piper voice stems to include (default: the three "
                         "benchmarked ones). Use 'all' for every .onnx in voices/")
    ap.add_argument("--piper-sids", default="0",
                    help="comma-separated Piper speaker ids, or 'N-M' for a range. Only meaningful "
                         "for multi-speaker models — en_US-libritts_r-medium has 904")
    args = ap.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = [k.strip() for k in args.lines.split(",") if k.strip() in LINES]
    sids = [int(s) for s in args.sids.split(",") if s.strip()]
    scales = [float(s) for s in args.length_scales.split(",") if s.strip()] or None

    if args.voices == "all":
        voices = sorted(p.stem for p in (ROOT / "voices").glob("*.onnx"))
    elif args.voices:
        voices = [v.strip() for v in args.voices.split(",") if v.strip()]
    else:
        voices = [
            "en_US-hfc_female-medium",  # what ships today — the baseline every number is read against
            "en_US-libritts_r-medium",  # most expressive of the mediums, already on disk
            "en_US-lessac-high",        # the quality ceiling Piper can reach here, and the slowest
        ]
    if "-" in args.piper_sids:
        lo, hi = args.piper_sids.split("-", 1)
        piper_sids = list(range(int(lo), int(hi) + 1))
    else:
        piper_sids = [int(s) for s in args.piper_sids.split(",") if s.strip()]
    cands = piper_candidates(voices, scales, piper_sids)
    # Every other family runs through the SAME sherpa-onnx binary — swapping engines is a model
    # download and a different set of flags, not new infrastructure. Each builder returns [] when
    # its model is not on disk, so a partial install just narrows the comparison.
    for t in (int(x) for x in args.threads.split(",") if x.strip()):
        cands += kokoro_candidates(sids, t, Path(args.kokoro_dir))
        for d in sorted((ROOT / "voices").glob("matcha-*")):
            cands += matcha_candidates(d, t)
        for d in sorted((ROOT / "voices").glob("kitten-*")):
            cands += kitten_candidates(d, t, sids)
        for d in sorted((ROOT / "voices").glob("*supertonic*")):
            cands += supertonic_candidates(d, t, sids)
        for d in sorted((ROOT / "voices").glob("*pocket*")):
            cands += pocket_candidates(d, t)

    if not any("/" in c.name and not c.name.startswith("piper") for c in cands):
        print("note: no sherpa-onnx candidates — run scripts/tts_setup_models.sh to include them\n")
    if args.only:
        cands = [c for c in cands if args.only in c.name]
    if not cands:
        print("no candidates matched", file=sys.stderr)
        return 1

    print(f"writing WAVs to {out_dir}  ({args.runs} timed run(s) each"
          f"{f', after {WARMUP_RUNS} warmup' if args.runs > 1 else ', audition mode — cold timings'})")
    print("RTF = synth seconds per audio second; under 1.0 is faster than real time.")
    print("peak RSS is the synth process only — it lands on top of Ollama's 2.4 GB and "
          "face_track's ~1.4 GB.")
    for c in cands:
        bench(c, keys, args.play, out_dir=out_dir, runs_each=args.runs)

    print(f"\nListen again without re-measuring:  paplay --device={TTS_SINK} {out_dir}/<file>.wav")
    return 0


if __name__ == "__main__":
    sys.exit(main())
