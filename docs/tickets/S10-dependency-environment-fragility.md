# S10 — Dependency and environment fragility

| | |
|---|---|
| **Tier** | 4 |
| **Severity** | Medium |
| **Effort** | Medium |
| **Confidence** | Medium |
| **Lens** | Software |

## Location

- `requirements.txt` — unpinned by design, with the do-not-install warning
- `requirements.lock.txt` — 193 entries, 7 flagged `[JETSON-BUILT]`, including
  `torch @ file:///home/devconph/torch-2.5.0a0+…nv24.08…whl`
- `scripts/freeze_requirements.sh`
- `ai/audio.py` — `_import_pvporcupine()`, which monkeypatches `subprocess.check_output` globally
  during import
- `config/wake.py` — `WAKE_CPU_PART_OVERRIDE`, `WAKE_ENGINE_ORDER`
- `docs/plan/wip/known-issues.md` — P1, marked RESOLVED (this ticket is what P1 did *not* cover)

## Problem

Two distinct fragilities, both currently latent.

**1. The environment cannot be reproduced from PyPI.** `requirements.txt` says so explicitly and is
right to: `faster-whisper` pulls in torch, and on this Jetson torch must remain a hand-built CUDA
wheel recorded only as a local file path in `requirements.lock.txt`. Reinstalling from PyPI
silently substitutes a CPU-only wheel. The lock file and the freeze script are good work — what has
never been done is proving the recorded facts are sufficient to rebuild anything.

**2. `_import_pvporcupine` patches a process-global.** To work around pvporcupine raising at import
on the Orin's Cortex-A78AE (`CPU part 0xd42`, absent from every published version 2.1–4.0), the
function replaces `subprocess.check_output` with a shim that fakes `/proc/cpuinfo`, deletes the
half-initialised `pvporcupine` modules from `sys.modules`, retries the import, and restores the
original in a `finally`.

It is careful, it is well documented, and it is safe *today* — import happens once, single-threaded,
at module load. But it is a global patch to satisfy a vendor's hardcoded allow-list, and it depends
on pvporcupine continuing to detect the CPU by shelling out to `cat /proc/cpuinfo`. If upstream
changes that to read the file directly, the override silently stops working and tier 1 falls back to
tier 2 with a message that reads like a missing key.

## Why it matters

The SD card already mounts with known ext4 errors and wants an `e2fsck` (recorded separately). A
rebuild under time pressure before an event is a live risk, and P1 in `docs/plan/wip/known-issues.md` was
closed on *documentation* being written, not on that documentation being exercised. Untested
recovery procedures are the ones that fail.

The pvporcupine patch is lower stakes — the wake chain degrades to openWakeWord, which is the whole
point of the tiered design — but a silent demotion of the best tier is exactly the kind of change
that shows up later as "the wake word got worse" with no log line anyone connects to it.

## Acceptance criteria

**Rebuild rehearsal**
- [ ] The documented rebuild is performed at least once against a spare SD card or a clean image —
      not read, executed.
- [ ] Every step that `requirements.lock.txt` does not cover is captured in a written runbook:
      the ch341 kernel module, the I2S/PulseAudio routing, the Ollama install + `gemma2:2b` pull +
      `keep_alive`, `loginctl enable-linger`, the Porcupine key file, the openWakeWord front-end
      download, the Piper voice models, and the `@reboot` cron entry.
- [ ] The runbook states, per step, how to verify it worked (the command to run and the expected
      output), not just what to type.
- [ ] The rebuilt card boots to a working robot: dashboard reachable, camera live, wake word
      responding, a spoken reply audible, `[llm] … fully on GPU` in the log.
- [ ] Elapsed time for the rebuild is recorded, so the pre-event risk is a known quantity.
- [ ] The local torch wheel is archived somewhere that is not the SD card being rebuilt, and its
      location is named in the runbook.

**pvporcupine workaround**
- [ ] The override's success or failure is visible at startup — it already prints when it patches;
      confirm the *failure* case is equally loud, and that `sess_wake_tried` distinguishes
      "CPU table" from "no key" from "bad .ppn".
- [ ] A written fallback position: if the override breaks, is Porcupine demoted in
      `WAKE_ENGINE_ORDER`, or is the patch updated? Record the decision and the criteria rather than
      discovering it during an incident.
- [ ] The patch's assumption (that detection goes through `subprocess.check_output` on
      `/proc/cpuinfo`) is stated as a comment at the call site so the next reader knows what would
      invalidate it. (It is currently implied by the code; make it explicit.)

## Suggested approach

**Rehearsal.** Write the runbook as `docs/rebuild.md` *while* doing the rebuild, not before — the
gaps only appear when the steps are actually run. Structure it in the order things must happen
(OS image → system packages → kernel module → audio routing → Python env from the lock file →
Ollama → models/voices/keys → cron), with a verification command after each. The existing
"Rebuilding from scratch" notes at the bottom of `requirements.txt` are the seed; move and expand
them rather than duplicating.

**Re-freeze first.** Run `scripts/freeze_requirements.sh` before starting, so the rehearsal tests
the current lock file and not a stale one.

**pvporcupine.** No code change is required — the workaround is correct. What is missing is the
decision record. Add it as a short section in `wake/README.md` (which already documents the tier
setup) covering: what the override does, what would break it, how to tell from the log that it
broke, and that the answer is to demote the tier rather than to chase the vendor. Note there that
openWakeWord needs no account and no per-platform binary, which is what makes the demotion cheap.

Deliberately out of scope: pinning `requirements.txt`. It is unpinned on purpose — it documents what
the code imports, and `requirements.lock.txt` is the restore target. Changing that would undo P1's
reasoning.
