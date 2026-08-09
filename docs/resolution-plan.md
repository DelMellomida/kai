# Resolution plan for docs/known-issues.md

Opened 2026-08-07, against the backlog in [known-issues.md](known-issues.md).

The governing constraint is **do not damage the working robot**. Kai currently runs: hands-free
wake, documents-first RAG, face tracking, jaw pantomime, dashboard, supervised autostart. Every
item below is therefore sorted by *what it can break*, not by how hard it is — and the ones that
can break something are deliberately left un-applied, with the measurement that would justify
them built and ready instead.

Three tiers:

| Tier | Meaning | What was done |
|---|---|---|
| **A** | Cannot change robot behaviour: docs, tests, and a read-only script | **Applied** |
| **B** | Changes behaviour, but only log output or a retry path — bounded and reversible by one config line | **Applied**, each behind a named knob |
| **C** | Changes what reaches the LLM, the audio, or the process supervisor | **Not applied.** Designed below, gated on a measurement |

Nothing in tier A or B touches the servo control path, the wake tiers, the Ollama call, retrieval
results, or the autostart chain. The full suite is green after the changes: **862 passed, 0
failed** (was 858 passed, 1 failed).

---

## Applied

### P1 — Dependencies undocumented and unpinned → tier A

Two files, because the problem is two problems.

- **[`requirements.txt`](../requirements.txt)** — all 16 third-party packages, one per line,
  annotated with which source file imports it. Verified by grepping every `import` in the tree,
  not from memory. It opens with a loud warning **not** to `pip3 install -r` it on the live robot:
  the Jetson's torch is hand-built with CUDA and a PyPI reinstall would silently swap in a
  CPU-only wheel. It also records the three things pip cannot install — the ch341 kernel module,
  the I2S/PulseAudio routing, and PyGObject/GStreamer for `import gi`.
- **[`scripts/freeze_requirements.sh`](../scripts/freeze_requirements.sh)** — run **on the robot**
  to produce `requirements.lock.txt` from `pip freeze`, with Jetson-built wheels (torch,
  torchvision, onnxruntime-gpu, tensorrt, nvidia-*) flagged `[JETSON-BUILT] do not reinstall`.
  Read-only: installs nothing, upgrades nothing, safe mid-demo.

The README's "No other dependencies" line is corrected and now points at both.

**Remaining action, on the robot:** `./scripts/freeze_requirements.sh` and commit the lock file.
It is the only thing that can actually reproduce this environment, and it can only be generated
there. Until it exists, P1 is documented but not solved.

### P3 — Canned-line re-warm only verified `ack` → tier B

`_rewarm_when_quiet` now compares what it got against the full set it asked for, instead of
checking one key:

```python
missing = sorted(set(self._canned_lines()) - set(self._canned))
```

The line set moved into `_canned_lines()` so the prewarm and the verification cannot drift apart —
that drift is exactly what let `thinking` go missing while `ack` was present and the retry never
fired. A one-line log names what is being retried. Covered by a new regression test
(`test_rewarm_retries_when_any_line_is_missing_not_just_the_ack`).

**Risk:** at most one extra Piper pass on a re-warm that lost a line — the path that was supposed
to run all along. Behaviour was already correct via live-synthesis fallback; this removes the
0.5–1.5 s of dead air it costs.

### P4 — Memory budget thin and undocumented → tier A

New **README § Memory Budget**: the measured 7.6 GB total / ~2.0–2.3 GB available, what Ollama and
face_track hold, zram, and the hard finding that `OLLAMA_NUM_CTX=4096` kills the llama runner.
Plus the two operational rules the numbers imply — stop `face_track.py` before a model or context
change, and a runner crash self-heals but the voice turn does not. `config/voice.py` points at it
from the `OLLAMA_NUM_CTX` line, which is where someone stands when they are about to break it.

Documentation only. No knob changed.

### P5 — The log is 58% noise → tier B

Both offenders are now **edge-triggered plus a slow heartbeat**, because the information in a
repeated line is the change:

- `[face_track] NO FACE` prints on the transition into absence, then at most every
  `NO_FACE_LOG_INTERVAL_S` (30 s).
- `[control] N Hz face=…` prints whenever `face_present` flips, else every
  `CONTROL_LOG_INTERVAL_S` (30 s), and the rate is averaged over that same window.

Both knobs live in `config/tracking.py` with the measurement in the comment. **`--lofi` is
deliberately untouched at 1 Hz** — it is a machine-readable stream for tooling, not a human log,
and re-timing it could break a consumer.

**Rollback:** set both intervals to `1.0`. That restores the old cadence exactly, no code change.

Projected effect on the 1.5-hour log that motivated this: 5281 NO FACE lines → roughly 180 plus
one per real transition.

### P6 — A permanently failing test → tier A

**The recorded diagnosis was wrong**, and that matters, because a rate-limiter timing assertion
would have been flaky and this is deterministic. `ServoSerial.__init__` centres at 90/90/90, so
`send(90, 90)` is a genuine no-op inside `PAN_DEADBAND` and correctly returns `False` — `send()`
returns at the deadband check on line 163, never reaching the rate limiter. The test asserted
behaviour the deadband made impossible; it predates the deadband.

Fixed by sending an angle that actually changes (`90 + PAN_DEADBAND + 1`).
`test_rate_limited_returns_false` got the same treatment — it was passing for the wrong reason,
returning `False` at the deadband rather than at the rate limiter it claims to test.

**No production code changed.** The suite is green, so the number means something again.

### P7 — The documents disagree → tier A (needs one command on the robot)

Two contradictions found and resolved from the sources' own evidence, not by picking a side:

| Claim | Was | Now | Evidence |
|---|---|---|---|
| Active chapters | 10 (Showcase) vs 11 (two others) | **11** | The Showcase page's own body links 11 chapters: bacolod, bohol, bukidnon, cagayandeoro, davao, iligan, iloilo, laguna, legazpi, manila, pampanga. Its lede was stale. |
| Years active | 15 (Our Programs) vs 17 (everywhere) | **17** | "since 2009" + "17th Anniversary" in the omnibus and the primer. |

A full numeric sweep of `documents/` for years, anniversaries, chapter counts and founding dates
found no other disagreement.

**Remaining action, on the robot:** `python3 -m ai.index_documents`. The changes are in the source
markdown; `documents/.rag_index.json` still contains the old text until it is rebuilt, so **Kai
still answers "10" until you run it.** Do it with `face_track.py` stopped — see § Memory Budget.

---

## Not applied — and why

### P2 — `SIMILARITY_THRESHOLD` never fires → tier C

This is the one that looks like a config change and is not. The measured ranges overlap —
on-topic 0.572–0.843, off-topic 0.541–0.686 — so **every** cutoff that rejects the worst noise
also rejects real questions. Raising the number trades answers for silence and would look fine
in a two-question spot check.

The gate is currently decorative but the failure mode is benign: gemma2:2b ignores the irrelevant
block and stays in persona. Changing what reaches the LLM on every single turn, before a demo,
to fix a cosmetic problem, is a bad trade. So the measurement was built instead of the fix:

**[`scripts/rag_eval.py`](../scripts/rag_eval.py)** re-runs the same 16-query split, prints the
best score and whether a documents block was carried for each, reports whether the two sets are
separable at all, and sweeps candidate cutoffs showing what each would cost in on-topic recall.
Read-only against the existing index — no settings, no servos, no Ollama, no audio.

```bash
python3 -m scripts.rag_eval            # baseline
python3 -m scripts.rag_eval --context  # plus the block each query actually gets
```

Run it first to re-establish the baseline on the current index. Then, in order of increasing risk:

1. **Soften the header, keep the chunks** *(lowest risk, no recall change)*. The concrete harm P2
   names is that `format_context()`'s "answer from them and nothing else" is applied to unrelated
   text on every turn. `retrieve_context` already computes a `brand` flag — hard evidence the turn
   is about DEVCON, from the brand matcher or the gazetteer. When that flag is false and the best
   score is in the ambiguous band, emit a weaker header ("background notes; ignore them entirely
   if they don't answer the question") instead of the authoritative one. **No chunk is ever
   dropped, so no answer can be lost** — the only thing that changes is how hard the prompt leans
   on them. Verify: on-topic block count unchanged in the eval, `--context` shows the softer
   header only on off-topic queries.
2. **A relevance gate on unflagged turns** *(medium risk)*. Require, for turns with no brand or
   gazetteer evidence, that the query share at least one low-DF token with the top chunk — the
   `_INDEX_DF` / `_INDEX_IDF` tables `lexical_rank` already builds, so no new dependency and no
   new index. This *can* drop context; the eval's on-topic column is the acceptance test and it
   must not fall.
3. **Cross-encoder rerank of the top-k** *(highest risk)*. Genuinely the right answer for
   separability, and the wrong one for this hardware: § Memory Budget leaves ~2.0–2.3 GB and a
   second model in the turn loop competes with the thing the budget is already tight for. Only
   worth revisiting if 1 and 2 measurably fail.

Do not do any of these in the week before an event.

### P8 — Open-mouthed hum → tier C, cosmetic

Text cannot fix it: espeak-ng renders every spelling of a closed hum as /hʌm/. The fix is to drop
a recorded WAV into `/tmp/kai_ack` and bypass Piper for that one line — which means special-casing
the audio path for one sound. `known-issues.md` already says it is only worth doing "if the current
one sounds wrong in the room". That is a judgement call for someone standing in the room, so it
stays a decision, not a change.

### P9 — systemd instead of cron + supervisor loop → tier C, highest blast radius

A systemd user unit is strictly better: `Restart=always`, real dependency ordering on
`nvargus-daemon` and PulseAudio, `journalctl`, and `systemctl status` for whoever is next to the
robot. `loginctl enable-linger devconph` is already applied, which is normally the blocker.

It is also the single change that can leave the robot **not starting at boot**, and the current
setup is supervised and verified (SIGKILL → restart in 5 s; SIGTERM → clean stop, no restart). The
gap it closes is diagnostic convenience, not reliability.

If it is done, it must be done in this order, with an event at least a week away:

1. Write the unit **disabled**; start it by hand with the cron job still in place but its command
   commented out. The single-instance lock in `scripts/autostart.sh` prevents a double start if
   both fire, which is the failure this ordering is avoiding.
2. Verify by hand: `systemctl --user status`, kill -9 → restart, kill -TERM → clean stop.
3. **Reboot twice** and confirm the robot comes up unattended both times.
4. Only then remove the cron line — and keep it in the commit message so it can be pasted back.

Do not skip step 3. Boot-time ordering against `nvargus-daemon` and PulseAudio is precisely what
a hand-started test does not exercise.

---

## Verification summary

| | Where | Status |
|---|---|---|
| Full test suite | this checkout | ✅ **863 passed, 0 failed** |
| Lock file for P1 | robot | ✅ `requirements.lock.txt`, 193 entries, 7 flagged Jetson-built |
| Reindex after P7 | robot | ✅ 331 chunks; 56/56 say 11 chapters, 0 say 10 |
| Log cadence for P5 | robot | ✅ 29 lines in 90 s (3 NO FACE + 3 control) vs ~135 before |
| RAG baseline for P2 | robot | ✅ recorded below — overlap reproduced, 0.081 |

All executed on the robot 2026-08-07 12:01–12:10 over SSH.

### P1 — what the lock file caught

193 packages. The entry that justifies the whole exercise:

```
torch @ file:///home/devconph/torch-2.5.0a0%2B872d972e41.nv24.08.17622132-cp310-cp310-linux_aarch64.whl    # [JETSON-BUILT] do not reinstall from PyPI
```

A local wheel path, not a PyPI version. Nothing outside this file records it. Six others are
flagged alongside it: tensorrt ×3, torchvision, jetson-stats.

### P2 — measured baseline (331-chunk index, threshold 0.45)

| | range | carried a documents block |
|---|---|---|
| on-topic | 0.606 – 0.771 | 8/8 |
| off-topic | 0.515 – 0.687 | 8/8 |

**Overlapping by 0.081.** Independently reproduces the original finding. Note the scores differ
slightly from the 0.572–0.843 / 0.541–0.686 recorded in known-issues.md — the original 16 queries
were never written down verbatim, so `scripts/rag_eval.py`'s split is a reconstruction. The
conclusion is unchanged, and from now on the split *is* recorded, so the next comparison is exact.

What the sweep adds — the cost of every cutoff someone might reach for:

| cutoff | on-topic kept | off-topic rejected |
|---|---|---|
| 0.45 *(current)* | 8/8 | 0/8 |
| 0.60 | 8/8 | 3/8 |
| 0.65 | 6/8 | 7/8 |
| 0.70 | 6/8 | 8/8 |
| 0.80 | 0/8 | 8/8 |

There is no row that rejects noise without losing answers. 0.70 is the first cutoff that clears
all eight distractors and it has already cost two real questions. This table is the argument
against treating P2 as a number.

### P7 — reindex

`python3 -m ai.index_documents` → 331 chunks from 7 files in 38.2 s, gazetteer 14 names.
Was: 53 chunks saying "10 active chapters", 3 saying "11". Now: **56 saying "11", none saying
"10"**, and `retrieve_context("how many chapters does DEVCON have?")` returns 11.

The previous index was copied to `documents/.rag_index.json.bak-20260807` first — rollback is one
`mv`. Delete it once you are satisfied.

**One honest correction:** the "15 years" line was never in the index, before or after — that
section of `..._Our_Programs_...md` does not survive chunking. Fixing it was right for document
consistency, but it was not a live contradiction; only the chapter count was.

### The restart, and what it cost

`rag.load_index()` runs once at startup ([face_track.py:509](../face_track.py#L509)) with no
reload path, so the reindex needed a process restart. `kill -9` → supervisor saw rc=137 after
7896 s and restarted after the 5 s floor, exactly as designed.

**It was not free.** SIGKILL does not release the I2S capture device, so the new process hit
`[mic] no audio for 2.1s — reopening` twice and logged 43 `session end: mic_lost after 0 turn(s)`
while the stream came back. It self-healed in about a minute — 0 mic_lost and 0 reopens in a 90 s
window afterwards — and no wake tier changed (whisper, as on all four previous startups; Porcupine
has never had a key on this robot).

**Next time, prefer a clean restart:** `SIGTERM` the face_track PID, wait for
`[face_track] Stopped.`, then `bash scripts/autostart.sh` — the supervisor deliberately does not
restart on a clean exit, so it must be started by hand. That releases the mic properly and skips
the mic_lost burst entirely. `kill -9` is the right tool only when the process is wedged.
