# Expressive voice — GPU plan

Goal: a voice with **actual expression** — pitch that moves with meaning, not a flat narration
contour. Fully offline. Without destabilising a robot that has a demo the next day.

**Everything here is additive and gated.** Kai keeps its current voice until the last step, the
rollback is one config line at every point, and there are four hard go/no-go gates where the honest
answer may be "abort, keep Piper". Read § Abort criteria before starting.

---

## Why this, and why nothing cheaper

Measured on the robot, 2026-08-09. 29 voices across 7 engine families were rendered and rejected by
ear as "too AI / flat / no expression". The measurements explain why, and rule out the cheap fixes:

| hypothesis | test | result |
|---|---|---|
| Wrong voice | 29 candidates, 7 families | all rejected the same way |
| Voice is too flat in pitch | p10–p90 semitone range | current voice **11.0 st**, near the top; human conversational is 6–12 |
| The sox compand flattens it | dynamics before/after | removes **0.4 dB**, not the ~5 dB claimed earlier. Not the cause |
| Text is too written | disfluency/pause variants | range unchanged (9.0 → 9.0 st); helps a little by ear, does not fix tone |
| A livelier speaker exists | 280 of 904 LibriTTS speakers swept | none beat the current voice's range |

The cause is structural, not a bad pick: **every engine small enough to run on this CPU was trained
on read-aloud audiobook corpora** — LJSpeech, LibriTTS, Lessac, HFC are all one person reading a
book. They reproduce narration faithfully. Expression is not something they lost; it is something
they never learned.

And the fix cannot be a smaller model:

| engine | params | long-line synth (14.4 s of audio) |
|---|---|---|
| piper (current) | ~20M | 3.26 s |
| kitten-nano | ~15M | 7.96 s |
| supertonic-3 | 66M | 9.56 s |
| kokoro fp32 | 82M | 16.09 s |
| **expressive models** | **150M – 1.6B** | **CPU: not viable** |

Kokoro at 82M is already 0.86× realtime on CPU. Expressive models start ~150M and cluster at 0.5B.
**The GPU is the only offline path**, which is what this plan is.

### What the GPU has to spare

Measured with everything running:

```
RAM 4909/7620MB   available 2563MB   swap 488/3810MB
ollama gemma2:2b  size_vram 2370573312 (2.37GB, 100% VRAM)  ctx 2048
GR3D_FREQ 0%      GPU idle between turns
lfb 3x2MB         <-- largest free block is 2MB: the memory is FRAGMENTED
```

A 0.5B model at Q4/fp16 is ~350–700 MB of weights; with KV cache and a CUDA context, ~1–1.5 GB.
**That fits in 2.5 GB — but only just, and only if fragmentation is dealt with.** `lfb 3x2MB` is
the exact condition `config/voice.py:265-268` blames for the `cudaMalloc: out of memory` 500s, and
its documented remedy is a reboot. **Reboot before attempting this.**

### The one piece of luck

`torch 2.5.0a0+872d972e41.nv24.08` with `cuda.is_available() == True` is **already installed** —
NVIDIA's JetPack build. The single biggest install risk (building or fetching a CUDA torch for
aarch64) is already paid. This plan must therefore **never install, upgrade or shadow torch**;
`requirements.lock.txt:1-8` warns that JETSON-BUILT packages reinstalled from PyPI silently become
CPU-only builds, which would break faster-whisper and MediaPipe too.

---

## Architecture: isolation is the stability story

The single most important decision. **The expressive model runs in its own process, in its own
venv, behind a localhost HTTP API — exactly like Ollama.** `face_track.py` never imports torch.

```
face_track.py ──► ai/tts.py ──HTTP──► kai-voice service (own venv, own process)
                     │                    │  torch + ChatTTS on GPU
                     │                    └─ writes WAV to /tmp, returns the path
                     └──fallback──► python3 -m piper   (unchanged, always present)
```

Four reasons this shape and not an in-process import:

1. **The main process stays untouched.** A torch/CUDA import into `face_track.py` puts a second
   CUDA context inside the process running the vision loop and the servo control loop. A crash or
   OOM there takes down face tracking, the mic and the servos — not just the voice.
2. **Failure is already handled.** `ai/tts.py` degrades to the silent jaw pantomime on any
   failure, and `enabled()` is checked per reply. A dead service is an existing, tested code path.
3. **It can be killed independently.** `systemctl stop kai-voice` frees the GPU instantly without
   restarting Kai — which is what you want ten minutes before a demo.
4. **The venv can use `--system-site-packages`**, reusing the JetPack torch while installing only
   pure-Python deps into an isolated tree. Nothing can shadow torch.

### Engine selection, matching house idiom

`config/wake.py:49-61` already establishes the pattern — an ordered chain, first tier that works
wins, a failing tier is a logged reason and never an exception:

```python
TTS_ENGINE_ORDER = ("voxcpm", "piper")   # first that answers wins
TTS_ENGINE_FORCE = None                  # pin one, bypassing the order (debugging)
```

**`REVERT: TTS_ENGINE_ORDER = ("piper",)`** — one line, no restart of anything but Kai.

### Hard guards (all mandatory, all learned from measurements here)

| guard | why | value |
|---|---|---|
| Request timeout | Autoregressive models have unbounded latency | 1.5× the Piper time for the same text, then fall back |
| **Max output duration** | Measured: Pocket-TTS produced **39.5 s of audio for a 4 s sentence** — a real runaway, and Kai is deaf while speaking (barge-in off) | reject > 20 s, fall back |
| Circuit breaker | A wedged service must not cost every reply a timeout | 3 consecutive failures → latch to piper for the process lifetime, one WARNING |
| VRAM pre-check | Loading must not evict Ollama to CPU | abort load if free < 1.8 GB |
| Health check on start | Fail fast and visibly, not per-reply | one synth at service start |

**The fallback must be silent and fast.** A reply that takes 400 ms longer because the expressive
engine timed out is fine. A reply that never happens is not.

---

## The model: VoxCPM-0.5B (Apache-2.0)

Licences checked on the Hugging Face API, 2026-08-09 — not assumed:

| model | licence | params | verdict |
|---|---|---|---|
| **openbmb/VoxCPM-0.5B** | **apache-2.0** | 0.5B | **primary target** |
| FunAudioLLM/CosyVoice2-0.5B | apache-2.0 | 0.5B | alternate, same size class |
| 2Noise/ChatTTS | **cc-by-nc-4.0** | 0.5B | **excluded — non-commercial** |
| nari-labs/Dia-1.6B | apache-2.0 | 1.6B | too big for the free VRAM |
| canopylabs/orpheus-3b | apache-2.0 | 3B | ~8 GB at runtime; impossible here |

ChatTTS is the one whose training data most directly matches the problem — its own description is
"a generative speech model for daily dialogue" rather than audiobook narration. It is excluded
anyway: **CC BY-NC is non-commercial, and Kai represents DEVCON publicly.** Not worth the argument.

Be clear about what that costs in confidence: **VoxCPM and CosyVoice2 are chosen on licence and
size, not on evidence that they sound conversational.** Both are 0.5B — ~6× Kokoro, the class where
expression starts being modelled at all — but whether they actually fix the flat tone is exactly
what Gate C exists to answer, by ear, before any integration work happens. If VoxCPM disappoints,
try CosyVoice2 before concluding the approach failed; they are interchangeable in this plan.

---

## Step 0 — Before anything (30 min, no installs)

1. **Licence: already settled** — VoxCPM-0.5B is Apache-2.0 (verified above). No further check
   needed. If you substitute a different model, re-verify; do not assume from a blog post.
2. **Reboot the Jetson.** `lfb 3x2MB` says the GPU memory is fragmented; a reboot defragments it
   and is the documented remedy for the `cudaMalloc` OOM in `config/voice.py:265-268`. Do not skip
   this — it is the difference between a clean measurement and chasing a phantom.
3. **Baseline, so regressions are attributable.** Record: `python3 -m unittest discover -s tests -t .`
   (expect 1094 pass, ~3 s), `free -m`, `curl localhost:11434/api/ps`, and the `[turn]` line from
   two real voice turns — that log already breaks out `stt + rag + llm + synth` to first audio
   (`ai/voice_assistant.py:1224-1227`).

---

## Step 1 — Isolated venv (45 min) — **Gate A**

```bash
python3 -m venv --system-site-packages ~/kai-voice-venv
~/kai-voice-venv/bin/pip install --no-deps <model package>   # --no-deps is not optional
~/kai-voice-venv/bin/pip install <its pure-python deps, one at a time>
```

`--system-site-packages` so the JetPack torch is visible; `--no-deps` so pip cannot decide to
"helpfully" fetch a CPU torch wheel over it.

**Gate A — verify the environment is intact before going further:**

```bash
~/kai-voice-venv/bin/python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
python3 -c "import torch; print(torch.__version__, torch.cuda.is_available())"   # SYSTEM python
python3 -c "import faster_whisper, mediapipe; print('ok')"
```

All three must still report the `nv24.08` torch and `True`. **If the system python's torch changed,
stop and restore it — that breaks Whisper and MediaPipe, i.e. all of Kai, and it is far more
expensive than a flat voice.**

---

## Step 2 — Measure before integrating (1 h) — **Gates B and C**

Standalone script, no Kai involvement. Synthesize the same three lines
`scripts/tts_bench.py` uses (0.84 s / 4.55 s / 14.4 s of audio) and record synth time, peak RSS,
and VRAM delta.

**Gate B — speed.** Long line must synthesize in **< 14 s** (RTF < 1.0). Under 7 s is comfortable.
Over 14 s means Kai falls further behind the longer it talks — abort.

**Gate C — Ollama is unharmed.** With the model loaded, run two real voice turns and compare the
`[turn]` log against the Step 0 baseline. Check `curl localhost:11434/api/ps` still shows
`size_vram` equal to the full model size — **if Ollama has been partially evicted to CPU, generation
roughly halves in speed and that costs more than the voice gains. Abort.**

Then **listen**. Nothing below is worth doing if it does not clearly beat what is in
`voice-audition/` — that is the entire point of this exercise, and 29 previous candidates failed it.

---

## Step 3 — The service (2 h)

`services/kai_voice.py`, run by the venv python, `POST /synth {"text": ..., "out": ...}` →
`{"ok": true, "wav": "...", "ms": 1234, "audio_s": 4.2}`.

- Model loaded **once** at start, never per request — the same reason `OLLAMA_KEEP_ALIVE = -1` exists.
- Single-threaded request handling. One synth at a time is already the invariant `ai/tts.py`'s
  `_synth_proc` slot enforces, and `ai/session.py:782-791` depends on it.
- WAV written to a file, path returned — **never audio over the wire**. Keeps `_post_process()`,
  `wav_duration()` and the `dst.is_file()/st_size` validation working untouched.
- `/health` returns model state and free VRAM.
- systemd unit `kai-voice.service`: `Restart=on-failure`, `RestartSec=5`, **`MemoryMax=2G`** so a
  leak cannot take the board down with it. Deliberately **not** `Restart=always` — a model that
  cannot load should stay down and let Kai fall back, not restart-loop against the GPU.

`ai/tts.py` gains one adapter, `_run_voxcpm(text, dst, length_scale)`, with the same
`(text, dst, scale) -> bool` signature as `_run_piper`, dispatched through `TTS_ENGINE_ORDER`. The
public API — `synthesize`, `synthesize_to`, `prewarm_canned`, `play`, `stop`, `is_playing`,
`quiet_since` — does not change, so `ai/session.py`, `ai/voice_assistant.py` and
`scripts/filler_check.py` are untouched.

**Tests:** keep `_run_piper` as the name and move today's body to `_run_piper_cli`, so
`TestSynthesizeTo` keeps `patch("ai.tts._run_piper")` verbatim and the 47 tests in `TestStop`,
`TestPlay`, `TestPrewarmCanned`, `TestOutputCardProfile` need no edits. Add: service-down falls back
to piper; timeout falls back; over-length output rejected; circuit breaker latches after 3;
`TTS_ENGINE_ORDER = ("piper",)` never contacts the service.

---

## Step 4 — Soak before believing it (1 h) — **Gate D**

The step most likely to be skipped and most likely to save the demo.

1. **30 minutes of continuous conversation.** Sample every 30 s: `free -m`,
   `ps -o rss= -p $(pgrep -f kai_voice)`, `curl localhost:11434/api/ps`. Watch for RSS climbing
   (leak) and for Ollama's `size_vram` dropping (eviction).
2. **Kill the service mid-reply** (`systemctl stop kai-voice`). Expected: that reply falls back to
   Piper or degrades to the silent pantomime; the next reply works; no traceback; Kai never hangs.
3. **`kill -STOP` the service mid-reply.** Expected: the timeout fires, one WARNING, fallback. This
   is the case a plain crash-handler misses.
4. **`python3 -m scripts.filler_check`.** Re-measures the whole filler bank against
   `FILLER_MAX_LINE_S` / `FILLER_MAX_STALL_S`. `config/filler.py:198-208` records that 10 of 20
   stalls were rejected at 1.2 s purely because of Piper's WAV padding — **a different engine has a
   different padding profile**, so this cap must be re-derived, not assumed.
5. **Full suite**: `python3 -m unittest discover -s tests -t .` — 1094, still ~3 s.
6. **Cold boot.** Reboot and confirm Kai comes up talking, with the service either up or cleanly
   absent. The `@reboot` autostart path is not the same as a hand-started one.

**Gate D:** any leak, any Ollama eviction, any hang, or any reply lost → **ship with
`TTS_ENGINE_ORDER = ("piper",)`** and keep the service for after the demo.

---

## Abort criteria — decide these now, not at 2am

Abort and revert to Piper if **any** of these is true:

- System python's torch or CUDA availability changed (Gate A)
- Long line ≥ 14 s synthesis (Gate B)
- Ollama drops out of full VRAM residency, or turn latency regresses vs the Step 0 baseline (Gate C)
- It does not clearly sound better than `voice-audition/` by ear
- Any leak, hang, lost reply, or failed cold boot in the soak (Gate D)
- **You are within 3 hours of the demo and Gate D has not passed**

Revert is always: `TTS_ENGINE_ORDER = ("piper",)` in `config/voice.py`, restart `face_track.py`.
The service can stay installed and stopped; it costs nothing while it is not running.

---

## Timeline against one day

| | | cumulative |
|---|---|---|
| Step 0 | licence, reboot, baseline | 0.5 h |
| Step 1 | venv + install, **Gate A** | 1.25 h |
| Step 2 | measure + listen, **Gates B, C** | 2.25 h |
| — | **decision point: is it clearly better?** | |
| Step 3 | service + adapter + tests | 4.25 h |
| Step 4 | soak, **Gate D** | 5.25 h |
| buffer | | 6 h |

The decision point after Step 2 is deliberate: **~2 hours in you know whether this is worth the
other 4**, and abandoning there costs only a venv you can delete.

---

## What ships if this is abandoned

Not nothing. Independent of the GPU work, already measured and safe:

- `en_US-libritts_r-medium` speaker **188** — 10.5 st range, faster than the current voice, and one
  of 904 speakers now reachable via the `-s` flag support added to `scripts/tts_bench.py`.
- The **speech-path disfluency transform** prototyped in this session (hesitations and pauses
  injected in code at `ai/voice_assistant.py:1174`, where spoken text already diverges from
  displayed text). It does not fix flat tone, but it is cheap, testable, and composes with any
  engine chosen later.

Both are config-and-small-code changes with one-line reverts, and neither depends on any of the
above.
