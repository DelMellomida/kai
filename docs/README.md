# Documentation

[`README.md`](../README.md) at the repo root is the overview and the entry point. Everything below
is the detail it links out to.

Four kinds of document live here, and the distinction is what keeps this directory navigable:

| Kind | Answers | Where |
|---|---|---|
| **Reference** | "How do I do X / what does this knob do?" | `docs/*.md` |
| **Plan** | "What are we doing next, and why that?" | `docs/plan/` |
| **Ticket** | "What exactly is wrong, and how do I know it's fixed?" | `docs/tickets/` |
| **R&D** | "Why is it built this way?" | `docs/rnd/` |

A document that starts answering two of those questions is a document that should be split.

---

## Reference

| Document | Read it when |
|---|---|
| [hardware.md](hardware.md) | Building or rewiring the robot. Bill of materials, both wiring diagrams, the 3.3V constraint. |
| [setup.md](setup.md) | Bringing up a Jetson from scratch. Software requirements, the CH340 kernel module, the Arduino sketch, first run. |
| [operating.md](operating.md) | Running a live robot. Every CLI flag, console output, the dashboard's live settings, the recovery ladder, restart-only constants. |
| [architecture.md](architecture.md) | Working out where something lives. Data flow, thread layout, file-by-file reference. |
| [memory-budget.md](memory-budget.md) | **Before** changing the LLM, `OLLAMA_NUM_CTX`, the Whisper model or the TTS engine. 8 GB shared CPU/GPU — what is resident and what is left. Cited from `config/voice.py`, `requirements.txt` and `scripts/tts_bench.py`. |
| [faq.md](faq.md) | Someone asks why the Arduino exists, why serial and not PWM, or why not the Jetson's 5V pins. |

Tunable constants are **not** documented here. They live in [`config/`](../config/), one file per
subsystem, each annotated with the measurement that set it — see [config/README.md](../config/README.md).
That is deliberate: a constant and the reason for its value should not be able to drift apart.

## Plans — [plan/](plan/)

Design and implementation plans, filed by whether work remains against them.

- `plan/completed/` — concluded; nothing pending. (A plan abandoned at its own abort criteria counts.)
- `plan/wip/` — unapplied steps, or applied steps not yet verified on hardware.

[plan/README.md](plan/README.md) lists what is outstanding in each, specifically — "step 2 is not
started" rather than "in progress".

## Tickets — [tickets/](tickets/)

24 implementation-ready tickets from the 2026-08-10 two-lens codebase review, each with location,
problem, impact, acceptance criteria and a suggested approach. Grouped into four tiers by severity
× inverse effort. **None are implemented.** [tickets/README.md](tickets/README.md) is the index.

Tickets differ from plans by being discrete and checkable. A plan describes an arc; a ticket
describes one unit of work and states how you know it is done.

## R&D — [rnd/](rnd/)

The original research write-ups, kept because they explain constraints that are still load-bearing.

| Document | What's in it |
|---|---|
| [rnd/challenges.md](rnd/challenges.md) | Seven hardware and platform problems and how each was solved — the 3.3V GPIO limit, the missing ch341 module, compiling Arduino sketches without the IDE, MediaPipe on aarch64, and the mic that was rejected on arithmetic. |
| [rnd/findings.md](rnd/findings.md) | The short version: what was learned, one paragraph each. |

## Conventions

- **Prose explains *why*; code comments explain *why here*.** If a fact only matters at one call
  site, it belongs in the comment at that call site, not in a document that will drift from it.
- **Measurements carry their date, their box and their method.** A number with none of those is an
  opinion. This is why the changelog entries read the way they do.
- **A moved document keeps its inbound references.** Several files here are cited by path from
  code comments; `grep -rn "docs/" --include="*.py" --include="*.sh"` before moving anything.
- **Nothing here is generated.** There is no build step and no doc tooling to keep working.
