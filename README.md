# Kai — a face-tracking, talking companion robot

Kai runs on an **NVIDIA Jetson Orin Nano**. It watches the room through a camera, turns its head to
follow whoever is in front of it, listens for "Hey Kai", answers out loud from its own documents,
and mimes the reply with a jaw servo. Everything runs **on the device** — no cloud, no API keys for
anything on the conversation path.

An Arduino Uno sits between the Jetson and the servos purely as a 5V bridge: the Jetson's GPIO is
capped at 3.3V, which SG90 servos do not reliably read. All the intelligence is Python on the
Jetson; the Arduino receives `"pan,tilt\n"` over USB serial and writes the angle.

**This file is an overview.** Detail lives in [`docs/`](docs/) — see the map below.

---

## What it does

| | |
|---|---|
| **Sees** | MediaPipe FaceMesh at 320×240, decimated to 15 fps, on a CSI or USB camera |
| **Follows** | A PD controller on its own thread at 15 Hz, slew-clamped, so the head glides between inference ticks instead of stepping on them |
| **Listens** | One always-open 16 kHz capture stream, a tiered wake word ("Hey Kai"), VAD turn-taking |
| **Understands** | faster-whisper (`base`, int8, CPU) → retrieval over `documents/` → Ollama `gemma2:2b` |
| **Speaks** | Piper TTS through a USB DAC and amp, with the jaw servo synced to the real audio length |
| **Is operated by** | An unauthenticated web dashboard on port 8081 — live video, tuning knobs, chat transcript, and a three-rung recovery ladder |

It is built to degrade rather than fail. No camera, no servo, no microphone, no Flask, no wake-word
engine — each of those is a state the robot reports and keeps running through, not a startup crash.

## Quick start

Assumes the Jetson is already set up — if it is not, start at [docs/setup.md](docs/setup.md).

```bash
python3 face_track.py --network 192.168.1.x --no-display --flip --wake
```

Then open `http://<jetson-ip>:8081` for the dashboard.

| Flag | Meaning |
|---|---|
| `--wake` | Hands-free: always-open mic, "Hey Kai", VAD turn-taking |
| `--flip` / `--flip-y` | Invert pan / tilt direction |
| `--tilt` / `--jaw` | Enable the tilt (pin 10) / jaw (pin 6) servo |
| `--no-camera` / `--no-servo` | Run without that hardware, deliberately |
| `--no-display` | Headless — required under the `@reboot` autostart |

Full flag reference and console output: [docs/operating.md](docs/operating.md).

## Documentation map

**Getting it running**

| Document | What's in it |
|---|---|
| [docs/hardware.md](docs/hardware.md) | Bill of materials, wiring diagrams for pan-only and pan+tilt, the 3.3V warning |
| [docs/setup.md](docs/setup.md) | Software requirements, the CH340 kernel module, uploading the Arduino sketch, first run |
| [docs/operating.md](docs/operating.md) | Running the system, every CLI flag, the dashboard's live settings, the recovery ladder, restart-only constants |

**Understanding it**

| Document | What's in it |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Data flow, thread layout, and a file-by-file reference |
| [docs/memory-budget.md](docs/memory-budget.md) | What is resident in 8 GB of shared CPU/GPU memory and what is left. **Read before changing the model or `OLLAMA_NUM_CTX`** |
| [config/README.md](config/README.md) | Every hand-tuned constant, one file per subsystem, each annotated with the measurement behind it |
| [docs/faq.md](docs/faq.md) | Why Arduino, why serial and not PWM, why not the Jetson's 5V pins |

**History and direction**

| Document | What's in it |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | What changed and when, newest first |
| [docs/plan/](docs/plan/) | Design and implementation plans, split into `completed/` and `wip/` |
| [docs/tickets/](docs/tickets/) | Engineering tickets from the 2026-08-10 codebase review, tiered by impact vs effort |
| [docs/rnd/](docs/rnd/) | The original R&D write-ups: hardware challenges and what they taught |

**Subsystem notes**

| Document | What's in it |
|---|---|
| [wake/README.md](wake/README.md) | Training and installing the custom "hey kai" wake model |

## Repository layout

```
face_track.py     entry point: CLI, the inference loop, startup ordering
settings.py       the live, dashboard-settable overlay on top of config/
app/              process concerns: control loop, camera supervisor, lifecycle
ai/               capture, wake, STT, RAG, LLM, TTS, the conversation state machine
vision/           camera sources, face params, PD controller, gestures, presence
servo/            the Arduino serial link
web/              the dashboard: Flask routes and the published state
config/           every tunable constant, one file per subsystem
arduino/          firmware
documents/        the corpus Kai answers from (+ the built index)
tests/            1173 tests, ~33 s, no hardware required
```

## Status

Kai is an R&D build that runs live at events. It is not a product and has no authentication —
the dashboard binds `0.0.0.0` and anyone on the network can reach every control on it. That is a
deliberate trade for a home LAN, and a real exposure at a venue; see
[docs/tickets/S7-unauthenticated-dev-server-dashboard.md](docs/tickets/S7-unauthenticated-dev-server-dashboard.md).

Known gaps, measured rather than guessed, are tracked in
[docs/plan/wip/known-issues.md](docs/plan/wip/known-issues.md) and
[docs/tickets/](docs/tickets/).

## Tests

```bash
python -m pytest -q
```

No hardware, no network and no models required — the audio, vision and serial layers are all
driven through fakes and injected clocks.
