# Engineering tickets — codebase review, 2026-08-10

Every finding from the two-lens codebase review (robotics engineering + software engineering),
converted into an implementation-ready ticket. One file per finding; nothing merged, nothing
dropped.

Tickets are grouped by **Tier**, which ranks them by severity × inverse effort — high-impact,
low-effort work first. Within a tier they appear in the review's own priority order. Each ticket
carries the review's Severity / Effort / Confidence / Lens verbatim.

**Nothing here has been implemented.** These are specifications, not change records.

**ID prefixes:** `R` = robotics engineering lens, `S` = software engineering lens. `S11` was a
grouped "minor correctness and hygiene" finding and is split into `S11a`–`S11d` for tracking; the
four share no code and can land in any order.

---

## Tier 1 — high impact, small effort

Do these first. All eight are small, self-contained, and individually revertible.

| ID | Ticket | Summary |
|---|---|---|
| **R7** | [TTS subprocesses outlive the process](R7-tts-subprocesses-outlive-process.md) | `run()`'s `finally` never calls `tts.stop()`, so `paplay`/Piper survive shutdown and the restarted process talks over them. |
| **S8** | [`app/camera_supervisor.py` has no tests](S8-camera-supervisor-untested.md) | The only substantial untested module — and the one deciding whether the robot believes it has a camera. |
| **R4** | [Firmware clamps to 0–180 while the host clamps to 10–170](R4-firmware-servo-limits-mismatch.md) | A corrupted serial line drives the pan servo into its mechanical stop; `toInt()` turns garbage into 0°. |
| **S2** | [`/params` rebuilds the whole snapshot at 20 Hz, per client](S2-params-sse-snapshot-per-client.md) | Per-tab work on the same `RLock` the 30 Hz audio worker needs; the data can't change faster than the 25 Hz publishers. |
| **R2** | [200 Hz idle spin in the main loop](R2-idle-spin-loop-gil-contention.md) | The no-frame path burns the GIL 200×/s — the documented bottleneck for the control thread's cadence. |
| **S1** | [The session RLock is held across disk I/O on two paths](S1-session-lock-held-across-disk-io.md) | `RLock` re-entrancy means `tick()`'s timeout paths run the WAV write inside the critical section the code believes it left. |
| **S9** | [Fail-open blanket excepts swallow bugs silently](S9-blanket-excepts-swallow-bugs.md) | `retrieve_context()` returns `""` for a `TypeError` — indistinguishable from "no relevant documents", which is the dangerous state. |
| **R9** | [`PDAxis.update` truncates instead of rounding](R9-pdaxis-truncates-instead-of-rounding.md) | `int()` biases every commanded angle toward zero, feeding back into `last_pan_cmd` at the 1° deadband resolution. |

## Tier 2 — high impact, medium effort

| ID | Ticket | Summary |
|---|---|---|
| **R1** | [Serial reconnect blocks the servo control thread for seconds](R1-serial-reconnect-blocks-control-thread.md) | Up to ~3.5 s of `sleep` + `sudo` under the serial lock, on the 15 Hz control thread — firing exactly when the robot is already misbehaving. |
| **R3** | [Firmware serial read can block the loop for a full second](R3-firmware-blocking-serial-read.md) | `readStringUntil`'s 1000 ms timeout re-creates the freeze-then-lurch the non-blocking LED ack was built to remove. |
| **R6** | [Startup thundering herd](R6-startup-warm-thundering-herd.md) | Six concurrent warm-ups defeat the reason `ensure_llm_warm` runs at startup: Ollama pins its GPU/CPU split from free memory at that instant. |
| **S3** | [RAG retrieval is a Python loop over float64 vectors, from a JSON index](S3-rag-retrieval-python-loop-float64.md) | Per-chunk `cosine_similarity` with repeated norms, doubled memory, and embeddings parsed from decimal text at every startup. |
| **S4** | [TTS is module-global state with fixed shared output paths](S4-tts-global-state-shared-wav-paths.md) | Two filenames shared by every reply; the mitigation surface across three modules now exceeds the fix. Prerequisite for R5. |
| **S7** | [Flask dev server, unauthenticated, on 0.0.0.0](S7-unauthenticated-dev-server-dashboard.md) | Anyone on the venue network can silence, blind, restart or puppet the robot; unbounded streaming threads on a dev server. |
| **R8** | [No liveness watchdog on the inference loop itself](R8-no-main-loop-watchdog.md) | Every other subsystem is watched. A wedged MediaPipe leaves a convincingly-alive, half-working robot with nothing reporting it. |

## Tier 3 — high impact, large effort

Plan these; don't squeeze them in. **R5** and **S6** are related — R5 removes much of the reason S6's
complexity exists, so sequencing matters.

| ID | Ticket | Summary |
|---|---|---|
| **R5** | [First-audio latency is serialised end to end](R5-serialised-first-audio-latency.md) | Non-streaming LLM + whole-reply synthesis; the filler bank exists to mask this rather than shorten it. Largest user-visible win available. |
| **S6** | [`ConversationSession` is a god object](S6-conversation-session-god-object.md) | 1587 lines: FSM, timers, wake tier, filler policy, warm lifecycle, status projection, mic recovery. The recorded bugs cluster in the extractable parts. |
| **S5** | [`face_track.py` constructs the whole robot at import time](S5-face-track-import-time-construction.md) | Import has filesystem side effects and builds most of the object graph before the CLI is parsed; forces two-phase construction outward. |

## Tier 4 — cleanup

| ID | Ticket | Summary |
|---|---|---|
| **R10** | [The tilt axis is plumbed everywhere but has no hardware](R10-tilt-axis-plumbed-without-hardware.md) | CLI flag, EMA, PD target, wire format, dashboard field and tests for an axis the firmware never attaches. Decide: wire it or collapse it. |
| **S11a** | [`has_video_client()` reads a shared counter without the lock](S11a-has-video-client-unlocked-read.md) | Benign under CPython, but it breaks the one-lock-guards-all contract the class is built on. |
| **S11b** | [`_publish_web`'s `fps` can never report the real rate](S11b-publish-web-fps-mislabelled.md) | Derived from the publish interval, so it pins at 25 and relates to neither the camera's 30 nor inference's 15. |
| **S11c** | [Dead and stray code](S11c-dead-and-stray-code.md) | Unused `open_camera()`, an ambiguous `autostart.sh.new` beside the live boot script, and a diagnostic reaching into `_force_send()`. |
| **S11d** | [`load_persona()` re-reads `persona.txt` on every LLM call](S11d-persona-reread-per-call.md) | Live reload is the intended feature; the silent mid-conversation drift and KV-prefix invalidation are not documented. |
| **S10** | [Dependency and environment fragility](S10-dependency-environment-fragility.md) | The lock file records the rebuild but the rebuild has never been executed; plus a process-global `subprocess` patch to satisfy pvporcupine's CPU allow-list. |

---

## Cross-ticket dependencies

Worth reading before scheduling — several of these are cheaper or safer in a particular order.

- **S4 → R5.** Streaming synthesis produces several WAVs per reply, which is impossible against the
  two fixed filenames. Do S4 step 1 (per-utterance paths) first.
- **R3 → R4.** R3 replaces the Arduino `String` parser with a `char` buffer; R4's field validation is
  much easier to write against that buffer. If both are scheduled, do R3 first and fold R4 in.
- **R5 → S6.** R5 may make a large fraction of the filler bank dead code. Refactoring code that is
  about to shrink is wasted motion.
- **S5 → S6.** Removing `face_track`'s import-time construction makes S6's test setup simpler —
  session tests stop needing to import `face_track` at all.
- **S8 → R8.** S8 builds the camera supervisor's test harness; R8 puts the loop watchdog on that same
  supervisor thread and can reuse it.
- **S4 → S6.** If S4 lands first, `VoiceWarmer`'s `_quiet_for_synth` gate reduces from a correctness
  requirement to CPU pacing.
- **S10 ↔ S11c.** The rebuild rehearsal is the moment the ambiguous `autostart.sh.new` becomes a real
  trap. Resolve S11c before the rehearsal, or resolve it during.

## Review context

Health ratings from the review, for reference when judging whether a change helped:

| Dimension | Rating |
|---|---|
| Efficiency | 6 / 10 |
| Performance | 5 / 10 |
| Stability | 8 / 10 |

Two framing notes carried over from the review's conclusion:

1. Most of what these tickets describe is **policy**, not defect — blocking calls on real-time
   paths, serialised latency, shared globals. The defect density is genuinely low, and the
   comment-as-measurement discipline throughout the codebase is why.
2. **R5 and S6 are the same story told twice.** A large fraction of the session's complexity exists
   to paper over the latency R5 would remove. Fixing the latency first makes the refactor smaller.
