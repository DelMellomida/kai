# R6 — Startup thundering herd

| | |
|---|---|
| **Tier** | 2 |
| **Severity** | Medium |
| **Effort** | Small |
| **Confidence** | Medium |
| **Lens** | Robotics |

## Location

- `face_track.py` — `_start_web_server()`: six concurrent `threading.Thread(...)` warm-ups
  (`_voice.ensure_model_loaded`, `_voice.ensure_scan_model_loaded`, `_voice.ensure_input_resolved`,
  `_voice.ensure_llm_warm`, `rag.load_index`, `rag.ensure_model_loaded`)
- `face_track.py` — `run()`: the camera supervisor thread and the session-start thread, started
  alongside
- `ai/session.py` — `_warm_all()` → `_prewarm_canned()` / `_speak_greeting()` / `_prewarm_bank()`
- `config/voice.py` — `WHISPER_CPU_THREADS = 4`, `OLLAMA_NUM_CTX`, `OLLAMA_KEEP_ALIVE = -1`, and the
  memory-budget notes

## Problem

Startup fires everything at once. Within a second or two of `run()` the process has in flight: two
`ctranslate2` model loads (the `base` turn model and the `tiny` scan model, 4 threads each), a
`fastembed` ONNX embedder, a JSON index parse, an Ollama warm-up request that loads a ~2.4 GB model,
a mic probe that can block for `LIVE_PROBE_TIMEOUT_S` per candidate device, a camera probe that can
sit in Argus for `CSI_FIRST_FRAME_S` (10 s), MediaPipe's FaceMesh init, and — once the session
starts — the Piper prewarm of the canned lines followed by the multi-minute filler bank.

That is six cores and 8 GB of *shared* CPU/GPU memory absorbing every heavy initialisation in the
system simultaneously.

## Why it matters

The consequential part is not startup time, it is Ollama's placement decision. `config/voice.py` and
`ai/llm.py` both document it: Ollama chooses the CPU/GPU split **at load time from whatever memory
is free at that moment**, and `OLLAMA_KEEP_ALIVE = -1` then pins that choice for the life of the
service. `log_model_placement()` exists solely to report the outcome, and the failure it reports —
"only N% on GPU … generation will run roughly half speed" — is unrecoverable without restarting
Ollama.

`ensure_llm_warm()` was placed at startup specifically to make that decision "while memory is still
fresh". Launching it in parallel with two Whisper loads and an ONNX embedder defeats the reason it
is there. Secondary effects: a longer window in which a wake word arrives before `ready` is true
(counted as `sess_wake_rejected_not_ready`), and more contention for the GIL and the CPU during the
period when MediaPipe and the control loop are also coming up.

## Acceptance criteria

- [ ] The memory-heavy warms run in a defined sequence on a single thread, not concurrently:
      Ollama first (its placement is irreversible), then the Whisper turn model, then the Whisper
      scan model, then the RAG embedder + index.
- [ ] `ensure_input_resolved` (or the session-start path that replaces it) remains genuinely
      parallel — it is I/O- and device-contention-bound, not memory-bound, and `face_track.py`
      already documents why it must not be serialised against the session's capture open.
- [ ] The camera supervisor thread and the session-start thread keep their current independence.
- [ ] `[llm] … fully on GPU` is observed on a cold boot across at least three consecutive reboots
      with the camera up; a partial offload is treated as a failure of this ticket.
- [ ] Time from process start to `sess_ready == True` does not regress by more than a few seconds
      versus today (sequencing trades a little wall-clock for a reliable placement — quantify the
      before/after and record it in the code comment).
- [ ] Each stage logs its start and completion with elapsed milliseconds, so the sequence is
      auditable from `~/kai-logs/face-servo.log` and a regression is attributable to a stage.
- [ ] A failure in any stage does not prevent later stages from running — the existing best-effort
      semantics of each `ensure_*` are preserved.

## Suggested approach

Replace the six `threading.Thread(...)` calls in `_start_web_server()` with a single
`kai-warmup` thread running an ordered list of `(name, callable)` pairs, each wrapped so an
exception is logged and does not abort the rest:

```
# sketch
_WARM_SEQUENCE = (
    ("llm",        _voice.ensure_llm_warm),      # first: placement is decided here and pinned
    ("whisper",    _voice.ensure_model_loaded),
    ("whisper-scan", _voice.ensure_scan_model_loaded),
    ("rag-index",  rag.load_index),
    ("rag-model",  rag.ensure_model_loaded),
)
```

Keep `ensure_input_resolved` on its own thread exactly as now, gated by the existing `resolve_mic`
flag — the comment there explains that probing the raw I2S device while `MicStream` is opening it
deadlocks both, and nothing about this change alters that.

Two judgement calls worth recording in the comment when implementing:

- **Ollama first vs. Whisper first.** Ollama's placement is the only irreversible decision in the
  list, which is why it leads. The cost is that `stt_ready` (and therefore `sess_ready`) arrives
  later, so early wakes are rejected for longer. If that proves worse in practice, the alternative
  is Whisper-turn first and Ollama second — but measure the placement outcome before making that
  trade, because a CPU-placed model costs every turn for the whole run.
- **The Piper bank.** `_prewarm_bank` is already paced and quiet-gated, and it starts only after the
  session comes up, so it is not part of this sequence. Leave it alone.
