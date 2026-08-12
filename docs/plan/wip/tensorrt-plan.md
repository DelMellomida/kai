# TensorRT optimisation plan — what it can buy, and what it can cost

**Status:** PLANNING ONLY — no code written, nothing measured on the robot for this document.
**Scope:** decide whether any of Kai's inference stages should move to TensorRT on the Jetson, and
if so, in what order and behind what gates.

The short version: **one stage is worth it (Whisper), one is a trap that looks like a win
(MediaPipe), and the rest are not worth what they cost.** The cost is not compute — it is iGPU
memory, and it is paid by the LLM.

---

## 0. What is already true on this box

TensorRT is not an install project here. It is already present:

```
tensorrt==10.3.0            # [JETSON-BUILT] do not reinstall from PyPI
tensorrt_dispatch==10.3.0
tensorrt_lean==10.3.0
torch @ file:///home/devconph/torch-2.5.0a0+872d972e41.nv24.08...  # CUDA, hand-built
```

(`requirements.lock.txt`. See `docs/setup.md` and the Jetson install traps — none of these four may
ever be reinstalled from PyPI.)

What is *not* present: a GPU `onnxruntime`. The lock pins `onnxruntime==1.23.2`, the CPU build, so
there is **no TensorRT execution provider available today**. Any ORT-based route (Piper,
fastembed, openWakeWord) needs an `onnxruntime-gpu` built for this JetPack first, which is exactly
the class of install that has silently broken this box before.

And the fact that decides everything below:

> **`face_track.py` has no CUDA context today.** MediaPipe is tflite/CPU. Whisper is
> `WHISPER_DEVICE = "cpu"`, int8. fastembed, Piper and openWakeWord are CPU onnxruntime. The only
> process on the iGPU is Ollama, holding 2.4 GB pinned with `keep_alive = -1` against
> **~2.0–2.3 GB free** (`docs/memory-budget.md`).

---

## 1. The way this project fails

Not "TensorRT turned out to be slow". This:

1. An engine is loaded in the robot process. That creates a CUDA context — on this platform the
   context, not the weights, is usually the larger allocation.
2. STT gets ~1.5 s faster. Everyone is pleased.
3. Some hours later Ollama reloads — a service restart, a `num_ctx` change, an OOM retry — and with
   the iGPU now more fragmented it lands on a 45/55 CPU/GPU split, at roughly half speed.
4. Generation slows by more than STT gained, and it does it in front of whoever is talking to the
   robot.

Both halves of that are already recorded, from opposite directions:

- **The context is the binding term.** `docs/plan/completed/expressive-voice-plan.md` — VoxCPM-0.5B
  OOM'd on load, twice, with 2.4–3.0 GB free; shrinking its KV cache 4× changed nothing. Weights +
  CUDA context, not the cache.
- **The split is real and silent.** `latency-plan.md` step 5 — Ollama picks placement at load time,
  `OLLAMA_KEEP_ALIVE = -1` pins that choice for the process lifetime, and fragmentation after hours
  of uptime produces the half-speed split with no error anywhere.
- **There is no headroom to absorb two allocations at once.** `docs/memory-budget.md` rule 1: a
  model reload OOM'd once with the camera up and succeeded on retry with more free memory.

**Therefore every step in this plan is gated on Ollama's placement, not on a stopwatch.** The
instrument already exists — `log_model_placement()` reads `/api/ps` and reports `size_vram` vs
`size` (latency-plan step 5a, shipped). No TensorRT change may be accepted on a latency number
alone; it must be accepted on a latency number *and* a full-GPU placement that survives an
Ollama restart and an hour of uptime.

---

## 2. Candidates

| Stage | Today | Verdict |
|---|---|---|
| **Whisper STT** (turn + wake scan) | `base` / `tiny`, int8, CPU, 4 of 6 threads | **The only one worth doing** |
| MediaPipe FaceMesh | tflite CPU, 15 fps | No — right instinct, wrong tool (§2.2) |
| Piper TTS | ORT CPU, RTF 0.100 | No — latency-plan step 2 deletes this wait for free |
| bge-small embeddings | fastembed CPU, ~140 ms/turn | No — not worth a CUDA context |
| openWakeWord | ~1 MB ONNX, always-on | No |
| `gemma2:2b` → TensorRT-LLM | llama.cpp CUDA via Ollama | **Explicitly no** (§2.3) |

### 2.1 Whisper — yes, and the reason is Tagalog, not speed

STT is the first stage of a strictly serial turn and it blocks on the whole utterance. But the
argument for spending a CUDA context on it is not the current number — it is the number Kai
already had to give up. From `config/voice.py`, measured on this box, 2.94 s utterance,
int8/cpu/4-threads, `vad_filter` on, 3 runs each:

```
small + auto-detect   7.81 s  (2.66x realtime)   <- was the default, ~50% of the whole turn budget
base  + auto-detect   2.38 s  (0.81x realtime)   <- 5.4 s faster, shipping today
```

`bilingual-plan.md` phase 1 calls for `small` because **`base`'s Tagalog accuracy is poor**. That
bump was applied and then walked back to `base` on latency grounds, and `config/voice.py` still
carries the revert note: *"The cost is accuracy, and it lands hardest on Tagalog."* So the
bilingual work is currently blocked on a stage that cannot afford to get slower.

**That is the actual thesis of this document: GPU/TensorRT Whisper is what buys `small` back.** If
a TRT `small` lands anywhere near today's 2.38 s, Kai gets Tagalog accuracy for free. If it does
not, this project has no other justification strong enough to spend the memory.

Two second-order wins, both already documented as live problems:

- **Four CPU cores come back.** `WHISPER_CPU_THREADS = 4` of 6 exists because ctranslate2 defaults
  to every core and *"showing up as jittery face tracking rather than as anything audio-shaped."*
  Moving STT off the CPU removes that contention entirely.
- **There are two Whisper consumers, not one.** `WAKE_WHISPER_SCAN_MODEL = "tiny"` runs per nearby
  utterance, not per turn — far more often than the turn transcribe. Both models share one CUDA
  context, so the second one is nearly free once the first is paid for. `config/wake.py` records
  that `small` was tried at this tier and *"a real check took 7.4 s and hit
  `WAKE_WHISPER_CHECK_MAX_S`, so nothing ever matched"* — the wake tier has its own latency
  ceiling and would benefit from the same headroom.

### 2.2 MediaPipe — no, and this is the one worth arguing about

The instinct is right; the tool is wrong. `config/tracking.py` records that `INFERENCE_FPS = 15`
is a **GIL ceiling, not a compute ceiling**:

> at 22 fps it collapsed to 6-10 Hz (jerkier actuation). CPU/mem/thermals had headroom — the
> bottleneck is the GIL, not resources. […] Getting BOTH faster perception and smooth control
> would need inference off the GIL (subprocess / native) — future work.

TensorRT does release the GIL inside `execute_async`, so on paper it qualifies as that "native"
route. It does not survive contact with what FaceMesh actually is: BlazeFace detection → anchor
decode → NMS → ROI crop and rotate → landmark net → inter-frame tracking state. Port it and the
Python/numpy glue you write to replace the pipeline holds the GIL exactly like the code you
removed. You would take on two ONNX conversions, a CUDA context, and the risk in §1, to *maybe*
not fix the thing.

The cheaper route named in that same comment — run inference in a subprocess — addresses the
actual bottleneck, costs no iGPU memory, and cannot push Ollama onto the CPU.

There is also a hard compatibility constraint on any FaceMesh replacement: `vision/face_params.py`
(emotion, LOFI params), `vision/gesture.py` (nod / shake / approach / mouth) and the jaw channel
all consume the full 468-landmark set. A replacement must be landmark-compatible, not merely "a
face detector that runs faster".

### 2.3 gemma2:2b → TensorRT-LLM — no

- Orin Nano 8 GB is not a TRT-LLM target NVIDIA builds for; AGX Orin is the supported Jetson tier.
- At batch-1 on an iGPU, the gain over llama.cpp CUDA for a 2B model is modest.
- It would discard `keep_alive = -1`, the `/api/ps` placement logging, the prompt-cache fix from
  latency-plan step 3, and every knob in `config/voice.py`.

Revisit only if steps 1–5 of `latency-plan.md` leave generation itself as the measured bottleneck,
which they currently do not.

---

## 3. Steps

### Step 0 — read the turn log. Nothing else may start before this.

`latency-plan.md` step 1 shipped the per-stage split, and its own "next action" — *read the log for
a handful of real turns* — has never been done. The `[turn] 4820ms = stt 2380 + rag 140 + …` line
in that document is an **illustrative format, not a measurement**.

Ten real turns, on the robot, and read the `[turn]` and `[llm]` lines.

**Abort gate:** if STT is not a large share of time-to-first-audio in practice, **stop here.** This
whole plan is then not worth a CUDA context, and latency-plan step 2 (streaming TTS) is the entire
remaining win.

**Cost:** one session. **Revert:** nothing to revert.

### Step 1 — bench four backends before porting anything

New `scripts/stt_bench.py`, modelled on `scripts/tts_bench.py` (same shape: engine families on the
x-axis, real audio, a table at the end).

The corpus already exists and is the right one: `ai/audio_debug.py` keeps an on-disk record of what
Kai actually heard — real mic, real room, real accents — which is worth more than any public test
set for this question.

Four backends over identical WAVs:

| # | Backend | Why it is in the list |
|---|---|---|
| A | `base` / CPU / int8 | today's baseline |
| B | `small` / CPU / int8 | what bilingual wants, at 7.81 s — the number to beat |
| C | `small` / **CUDA** via ctranslate2 (`WHISPER_DEVICE = "cuda"`) | **a one-line config change**, no engines, no build |
| D | `small` / **whisper-trt** | the actual subject of this document |

Report per backend: **WER against the corpus, wall-clock latency, process RSS, and Ollama's
`/api/ps` placement after 30 minutes of coexistence.**

**The point of including C is that it may end the project.** If ctranslate2 on CUDA is fast enough,
TensorRT buys a marginal delta for an engine-build pipeline, a hardware-locked artifact and a
JetPack-upgrade liability. **D must beat C by a margin worth that, or C ships and D is dropped.**

**Cost:** a script and a session. **Revert:** delete the script; no runtime code touched.

### Step 2 — a backend seam, with a CPU fallback

Only if step 1 says C or D wins.

Add to `config/voice.py`:

```python
STT_BACKEND = "faster-whisper"   # | "faster-whisper-cuda" | "whisper-trt"
STT_ENGINE_DIR = None            # whisper-trt only; None = derive from the model name
```

Resolve it in `_ensure_whisper` (`ai/voice_assistant.py:180`), which is already lazy and
constructed through injected config — so the suite (1113 tests, ~12 s, no hardware) keeps running
untouched on a machine with no GPU and no engines.

**Fall back to CPU on any engine-load failure**, in the style of the RAG failsafe chain. A missing
or stale engine file must never be able to cost a reply — it should cost a warning line and a
slower turn. This is the same discipline as `_log_llm_timings`, which is explicitly written so that
it *"must never be able to cost a reply."*

Apply the same seam to the wake tier (`WAKE_WHISPER_SCAN_MODEL`), or state in the config why it
stays on CPU.

**Revert:** one config line back to `"faster-whisper"`.

### Step 3 — engines are build artifacts, never repo files

A TensorRT engine is locked to the GPU **and** the TRT version. It must be built on this Jetson and
rebuilt after any JetPack or TensorRT change. So:

- `scripts/build_stt_engine.sh` — build on-device, print the versions it built against.
- Output to a gitignored path; **nothing generated goes in the repo.**
- A section in `docs/setup.md` covering the rebuild trigger, because a stale engine after an
  upgrade will present as a load failure at the worst possible moment.
- **Build with `face_track.py` stopped.** `docs/memory-budget.md` rule 1 exists for exactly this:
  engine building is memory-hungry and would fight Ollama for the headroom that already OOM'd once
  with the camera up.

---

## 4. Two correctness risks that are not latency risks

**Language handling must be verified before anything is built.** `WHISPER_LANGUAGES = ("en", "tl")`
with `WHISPER_LANGUAGE = None`, and `config/voice.py` records why that machinery exists — a 2 s clip
scored `en 0.34, cy (Welsh) 0.22, nn (Norwegian Nynorsk) 0.21`, *"which is why replies came back in
Spanish and Norwegian."* Several whisper-TRT builds ship English-only or drop language detection
entirely. If the chosen engine cannot detect language, it **kills the bilingual plan on that
backend** — check this first, before any engine build.

**A different decoder mishears differently.** `DEVCON_MATCH_RATIO = 0.80` and the skeleton classes
in `config/rag.py` were measured over ~60 real renderings from *this* decoder at int8, and they sit
in a narrow gap: plausible mishearings land at 0.833+, the nearest real words top out at 0.727. A
fp16 TRT decoder will produce a different distribution of mishearings, and `WHISPER_INITIAL_PROMPT`
(decoder priming) may not be supported by every backend at all — which is the *only* mechanism that
reaches multi-word names like "Geeks on a Beach". Re-run `scripts/rag_accuracy.py` after any
backend change and confirm the gap still exists.

---

## 5. Where this sits in the queue

**Behind `latency-plan.md` step 2** (stream generation → per-sentence TTS). That step is the larger
win, it is already specified in detail, it costs zero iGPU memory, and it is the same work as
ticket `R5`. Nothing here should displace it.

**And behind step 5b**, which is two environment variables on the Ollama unit:

```
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0
```

`q8_0` roughly halves KV-cache memory. That is **free headroom, and it is the same headroom this
plan wants to spend** — it may be the difference between an STT engine fitting and not fitting. It
is an environment-only change, revertible without touching the repo. Do it first and re-measure.

Read this plan as a prerequisite for `bilingual-plan.md` phase 1's model bump, not as a
latency project standing on its own.

---

## Order of work

| # | Step | Risk | Revert | Status |
|---|------|------|--------|--------|
| — | latency-plan 5b: flash-attn + `q8_0` KV | low | env vars | prerequisite, not started |
| — | latency-plan 2: streaming TTS | medium | config flags | prerequisite, not started |
| 0 | Read `[turn]` / `[llm]` lines for 10 real turns | none | n/a | **not started — blocks everything** |
| 1 | `scripts/stt_bench.py`, four backends, WER + RSS + `/api/ps` | none | delete script | not started |
| 2 | `STT_BACKEND` seam + CPU fallback | low | one config line | not started |
| 3 | On-device engine build script + `docs/setup.md` | low | delete artifacts | not started |
| — | MediaPipe → TensorRT | — | — | **rejected, see §2.2** |
| — | gemma2:2b → TensorRT-LLM | — | — | **rejected, see §2.3** |

## Acceptance criteria

A TensorRT backend ships only when all four hold:

1. WER on the `audio_debug` corpus is **no worse than today's `base`/CPU**, and better on Tagalog.
2. Latency at `small` is **at or below today's 2.38 s**.
3. `log_model_placement()` reports Ollama **fully on the GPU** — after an Ollama restart, and again
   after an hour of uptime with the camera running.
4. Pulling the engine file makes Kai **slower, not broken.**

Criterion 3 is the one this plan is really about. The others are the easy part.
