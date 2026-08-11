# Engineering tickets — codebase review, 2026-08-10

Every finding from the two-lens codebase review (robotics engineering + software engineering),
converted into an implementation-ready ticket. One file per finding; nothing merged, nothing
dropped.

Tickets are grouped by **Tier**, which ranks them by severity × inverse effort — high-impact,
low-effort work first. Within a tier they appear in the review's own priority order. Each ticket
carries the review's Severity / Effort / Confidence / Lens verbatim.

**Five of these have been implemented** — R9, R7, S9 and S1 on 2026-08-09 (PRs #5–#8), and S12 on
2026-08-10 (PR #9). Everything else is still a specification, not a change record. A landed ticket
keeps its file rather than being deleted, so the spec and what actually shipped stay side by side.

How a landed ticket records itself, both forms being in use:

- a `> **Status: FIXED**` banner at the top of the file, naming the branch and the one-line change,
  with the acceptance criteria checked off in place — R9, R7, S9, S1;
- a `## Resolution` section at the bottom — S12.

The banner is the better of the two and is what new work should use: it is the first thing a reader
sees, and it puts the outcome next to the spec instead of a scroll away. Either way, **check the
acceptance criteria boxes** — S12's are still all unchecked despite the ticket having landed, which
is why the table below cites its Resolution section rather than its checklist.

> **This index went stale once, and it is worth knowing how.** Between 2026-08-09 and 2026-08-11 it
> still read "one of these has been implemented — S12" while four Tier 1 tickets had already merged.
> Each of the four PRs updated *its own ticket file* correctly and none of them updated this table,
> so every individual ticket was honest and the summary was not. Local `main` was also 13 commits
> behind `origin/main`, so `git log` on a fresh checkout agreed with the wrong version. If you are
> about to pick up a ticket, `git fetch` and check the ticket's own file before trusting this page.

**ID prefixes:** `R` = robotics engineering lens, `S` = software engineering lens. `S11` was a
grouped "minor correctness and hygiene" finding and is split into `S11a`–`S11d` for tracking; the
four share no code and can land in any order.

**Not everything here came from the review.** `S12`–`S14` are feature specifications raised on
2026-08-10 from a separate question — what would make Kai's *conversation* land, given the camera is
not part of the answer. They are written to the same format and carry the same contract, but their
Severity reads as "enhancement, not a defect": nothing about the current behaviour is broken, it is
absent. Do not read them as review findings.

---

## Tier 1 — high impact, small effort

Do these first. All nine are small, self-contained, and individually revertible. **Five have landed;
four remain open.**

| ID | Ticket | Summary |
|---|---|---|
| **S12** ✅ | [Kai never learns who it is talking to](S12-no-identity-within-a-session.md) | **Landed 2026-08-10**, PR #9. A name offered in speech survived only `MAX_HISTORY_TURNS = 6` and was then evicted; nothing pinned it. See its `## Resolution`; its checklist was never ticked. |
| **R9** ✅ | [`PDAxis.update` truncates instead of rounding](R9-pdaxis-truncates-instead-of-rounding.md) | **Landed 2026-08-09**, PR #5, `fix/pd-axis-rounding`. Returns `int(round(self.current))`. The write-up's "asymmetric about 90°" claim was corrected while fixing it — angles are clamped positive, so truncation was a *uniform* downward bias. One on-hardware check deferred. |
| **R7** ✅ | [TTS subprocesses outlive the process](R7-tts-subprocesses-outlive-process.md) | **Landed 2026-08-09**, PR #6, `fix/tts-outlives-shutdown`. `tts.stop()` is now the first statement of `run()`'s `finally`. **Two on-hardware checks deferred.** |
| **S9** ✅ | [Fail-open blanket excepts swallow bugs silently](S9-blanket-excepts-swallow-bugs.md) | **Landed 2026-08-09**, PR #7, `fix/rag-silent-failures`. Rate-limited `_note_error()` in `ai/rag.py`; the fail-open return values are bit-identical. All criteria met. |
| **S1** ✅ | [The session RLock is held across disk I/O on two paths](S1-session-lock-held-across-disk-io.md) | **Landed 2026-08-09**, PR #8, `fix/session-lock-disk-io`. `tick()` hands both capture timeouts back to the caller to finish off the lock. All criteria met. |
| **S8** | [`app/camera_supervisor.py` has no tests](S8-camera-supervisor-untested.md) | The only substantial untested module — and the one deciding whether the robot believes it has a camera. |
| **R4** | [Firmware clamps to 0–180 while the host clamps to 10–170](R4-firmware-servo-limits-mismatch.md) | A corrupted serial line drives the pan servo into its mechanical stop; `toInt()` turns garbage into 0°. |
| **S2** | [`/params` rebuilds the whole snapshot at 20 Hz, per client](S2-params-sse-snapshot-per-client.md) | Per-tab work on the same `RLock` the 30 Hz audio worker needs; the data can't change faster than the 25 Hz publishers. |
| **R2** | [200 Hz idle spin in the main loop](R2-idle-spin-loop-gil-contention.md) | The no-frame path burns the GIL 200×/s — the documented bottleneck for the control thread's cadence. |

**Three acceptance criteria across the landed five are deferred, all needing the robot**, and none
of them has been run:

- **R7** — after `SIGTERM` during a spoken reply, no `paplay` or `piper` survives; and a dashboard
  `POST /restart` mid-reply produces silence rather than two voices. These are the two that matter:
  R7's whole subject is a failure that only exists across a real process death.
- **R9** — the mirrored left/right sweep. Weak evidence by the ticket's own admission, since the
  corrected characterisation means there is no asymmetry to see, only a half-degree shift.

The unit suites are green for all three; what is missing is the on-hardware confirmation. Anyone with
the robot in front of them can close these in about ten minutes — see [operating.md](../operating.md).

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
| **S13** | [A conversation is forgotten the instant the session ends](S13-no-continuity-across-the-wake-gap.md) | 25 s of thinking discards the sticky RAG topic, so the same follow-up that resolved a moment ago degrades to "I'm not sure". |
| **S14** | [Kai only ever speaks when spoken to, and its sessions die silently](S14-kai-has-no-conversational-initiative.md) | The persona offers "gusto mo marinig?" but the state machine cannot act on an unanswered question; `no_speech` ends the conversation without a word. |

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
- **S13 → S14.** A sign-off has to know whether the conversation can be resumed — "balik ka ha" is
  right when S13 has landed and misleading when it has not.
- **S12 → S14.** A nudge and a sign-off both read far better with a name in them, and S12 is the
  cheaper half. If both are scheduled, do S12 first and write the banks with the name slot in mind.
- **R5 ‖ S12–S14.** Independent. R5 shortens the wait before Kai speaks; these three change what it
  says and when. Neither blocks the other, and S12 is small enough to land while R5 is still being
  planned.
- **S6 → S13, S14.** Both add state and a deadline to `ConversationSession`, which S6 already calls
  a god object at 1587 lines. Landing them first makes S6 bigger; landing S6 first is a large
  prerequisite for two medium tickets. Prefer taking them first and folding the new state into S6's
  extraction plan rather than growing the class twice.
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
