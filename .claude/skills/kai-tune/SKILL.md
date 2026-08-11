---
name: kai-tune
description: Change a tunable constant in config/ correctly — find which file owns it, decide whether it is live-settable or restart-only, respect the measurement recorded against it, and document the new value. Use when asked to adjust fps, gains, thresholds, timeouts, servo limits, wake sensitivity, RAG top-k, or any hand-tuned number.
---

# Changing a Kai constant

Every tunable lives in `config/`, one file per subsystem, and **each value carries the measurement
that set it in a comment above it.** That comment is the reason the codebase is trustworthy. A new
value without a new measurement is a guess wearing the same clothes.

## 1. Find the owner

| File | Tunes |
|---|---|
| `servo.py` | serial port/baud, send rates, angle limits, deadband |
| `tracking.py` | inference fps, EMA/PD gains, jaw range, slew cap, web port |
| `camera.py` | capture + processing resolution, network/stream ports |
| `voice.py` | Whisper + Ollama models, context size, jaw envelope |
| `wake.py` | wake engine chain, always-open capture, VAD turn-end, session timeouts |
| `rag.py` | embedding model, chunking, top-k, thresholds, failsafe chain |
| `gesture.py` | nod / shake / proximity / mouth thresholds |
| `thinking.py` | thinking sweep amplitude/period, the "hmm" delay |
| `filler.py` | the filler bank and the 2 s dead-air ceiling |

`config/README.md` is the index. `settings.py` is the **live overlay** on top of these — never edit
it to change a default.

## 2. Live knob or restart-only?

Eleven knobs are settable from the dashboard's ⚙ Settings tab and stored in
`~/.config/kai/settings.json`: camera, hands-free, wake sensitivity, mic noise floor, speak replies,
volume, speaking rate, follow faces, move the jaw, think out loud, sweep while thinking.

- **Tuning on the robot right now?** Use the dashboard — no edit, no restart. Then, if the value
  should become the default, edit `config/` too. Only knobs differing from their default are stored,
  so editing a default still propagates.
- **Everything else is restart-only by design.** There is deliberately no half-applied "restart
  required" state in the dashboard. Editing the file and restarting is the whole procedure.

## 3. Respect what the comment says

Read it before changing the value. Several encode hard limits that will bite:

- **`INFERENCE_FPS = 15` — GIL ceiling, measured 2026-07-09.** Raising it does not help latency: at
  22 fps the pure-Python control thread collapsed from ~14 Hz to 6–10 Hz and actuation got visibly
  jerkier, with CPU, memory and thermals all in headroom. The bottleneck is the GIL.
- **`CONTROL_FPS = 15` and `SEND_INTERVAL` — current-sensitive.** Effective send rate ≈
  `min(CONTROL_FPS, 1/SEND_INTERVAL)`. Raising it raises average SG90 current on the shared rail →
  brownout → CH340 USB flapping. Only raise after confirming no `dmesg` USB disconnects.
- **`PAN_MAX_STEP = 8`** is a current bound as well as a smoothness one.
- **`SERVO_ABSENCE_FRAMES` must stay well under `CONTROL_STALE_TIMEOUT × INFERENCE_FPS`**, or a real
  absence gets reported by the stale path instead.
- **Model or `OLLAMA_NUM_CTX` changes: read `docs/memory-budget.md` first.** 8 GB is shared between
  CPU and GPU, and Ollama pins its split from free memory at load time.
- **`TTS_TAIL_MUTE_S`** is the first knob to raise if Kai ever answers itself — `paplay` exits before
  the amp actually goes quiet.

If a change contradicts a recorded measurement, take a new measurement. Say what you measured, how,
and on which date — then replace the comment with it. Do not delete the old reasoning silently.

## 4. Finish the job

- Update the comment above the value with the *why* and the measurement.
- Update `config/README.md` if you added, removed, or promoted a knob to the dashboard.
- Run the suite — several tests assert on these constants: `python -m pytest -q`.
- Restart-only? Say so, and deploy with the `kai-deploy` skill.
- Behaviour changed observably? Add a `CHANGELOG.md` entry with the measurement.
