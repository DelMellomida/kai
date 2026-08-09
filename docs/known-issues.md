# Known issues — deferred backlog

Opened 2026-08-07, from an end-to-end review of the conversation path (documents-first RAG, fuzzy
DEVCON matching) and of the process/setup structure around it.

Everything here was **measured on the robot**, not inferred — the numbers are recorded so the next
person doesn't have to re-derive them. Resolved in the same pass and NOT listed here: heading-only
chunks, duplicate chunks, index staleness, follow-up query expansion, `num_ctx`/history budget,
markdown reaching Piper, the "Hmmmm" spelled-out filler, process supervision, the SIGTERM handler,
and the single-instance lock.

Priority is about risk to a live demo, not effort.

**Status, 2026-08-07:** worked through in [resolution-plan.md](resolution-plan.md), which sorts
these by what a fix can break rather than by effort. **P1, P3, P4, P5, P6 and P7 are resolved and
live on the robot** — lock file captured, documents reindexed, face_track restarted, log cadence
verified in production, suite green at 863 passed / 0 failed. P2, P8 and P9 are deliberately NOT
applied: each changes the LLM prompt, the audio path or the boot chain, and none of them fixes a
failure the robot is currently exhibiting. P2's measurement was built and run instead. Per-issue
status is marked below.

---

## P1 — Dependencies are undocumented and unpinned

> **RESOLVED.** `requirements.txt` lists all 16 packages with their call sites and the
> do-not-reinstall warnings; README corrected; `scripts/freeze_requirements.sh` added and **run on
> the robot** — `requirements.lock.txt` is committed, 193 entries, 7 flagged `[JETSON-BUILT]`.
> The one that mattered: torch is `file:///home/devconph/torch-2.5.0a0+872d972e41.nv24.08…whl`,
> a local wheel path recorded nowhere else. Re-run the script after any `pip install`.

**Where:** `README.md` "Software Requirements" (~line 343); no `requirements.txt` anywhere.

README says:

```bash
pip3 install mediapipe opencv-python pyserial numpy
```

…followed by "No other dependencies." That is no longer true. Actual third-party imports across
the codebase:

`cv2, fastembed, faster_whisper, flask, gi, mediapipe, numpy, openwakeword, pvporcupine, pypdf,
requests, scipy, serial, sounddevice, webrtcvad` — **14**, plus `piper-tts` (invoked as
`python3 -m piper`) and the hand-built CUDA-enabled torch on this Jetson.

None are version-pinned. Rebuilding this environment from the README is not possible today, and the
undocumented parts are the awkward ones: the Porcupine CPU-table workaround (`config/wake.py`), the
ch341 kernel module for the Arduino, the I2S/PulseAudio routing in `scripts/autostart.sh`.

**Why it matters:** this is invisible until the SD card dies or the robot has to be rebuilt before
an event, and then it is archaeology under time pressure.

**Fix:** commit `pip freeze > requirements.txt` (note which entries are Jetson-specific wheels that
must NOT be reinstalled from PyPI — torch above all), and correct the README list. Cheap now.

---

## P2 — `SIMILARITY_THRESHOLD` never fires

> **OPEN, deliberately.** Not a number change, and changing what reaches the LLM on every turn to
> fix a currently-benign problem is the wrong trade before an event. The measurement was built
> instead — `python3 -m scripts.rag_eval` — and **run on the robot against the new 331-chunk
> index**: on-topic 0.606–0.771, off-topic 0.515–0.687, still **overlapping by 0.081**, 16/16
> queries carrying a block. Independently reproduced.
>
> The sweep is the part worth keeping: 0.70 is the first cutoff that rejects all eight distractors
> and it already costs two real answers; 0.80 rejects everything. No row separates them. Three
> ranked fixes in [resolution-plan.md](resolution-plan.md) § P2 — start with the header softening,
> which drops no chunks and so cannot cost an answer.
>
> (The 16 queries here are now recorded in `scripts/rag_eval.py`. The numbers differ slightly from
> the ranges above because the original queries were never written down verbatim; the next
> comparison will be exact.)

**Where:** `config/rag.py` (`SIMILARITY_THRESHOLD = 0.45`), applied in `ai/rag.py rank_chunks`.

Measured best-chunk scores over 16 queries against the real index:

| | range |
|---|---|
| on-topic (8 queries) | 0.572 – 0.843 |
| off-topic (8 queries: weather, "Kumusta ka na?", favourite colour, adobo, jokes…) | 0.541 – 0.686 |

The two ranges **overlap**, so no global cutoff separates them, and 0.45 sits below both. Every turn
retrieves 3 chunks and carries a documents block, including "what's the weather like today?".

Currently benign — gemma2:2b ignores the irrelevant block and answers in persona ("Ay, wala akong
alam 'diyan!"). But the gate is decorative, and `format_context()`'s "answer from them and nothing
else" instruction is being applied to unrelated text on every single turn.

**Fix:** not a number change — raising the threshold cuts real answers before it cuts noise. Options:
a cross-encoder rerank of the top-k, or a cheap relevance check before the block is attached. Verify
against the same 16-query split before/after.

---

## P3 — Canned-line re-warm only verifies `ack`

> **RESOLVED.** `_rewarm_when_quiet` now diffs the cache against `_canned_lines()`, the single
> source of truth `_prewarm_canned` also uses, so the two cannot drift. Missing keys are named in
> the log. Regression test: `test_rewarm_retries_when_any_line_is_missing_not_just_the_ack`.

**Where:** `ai/session.py:591`

```python
missing = "ack" not in self._canned
```

`_rewarm_when_quiet` exists to survive a turn starting mid-prewarm (tts has a single synth slot and
`stop()` cancels it), but it only retries when **ack** is the casualty. Observed: after a re-warm,
`[session] cached spoken lines: ack, error, no_speech` — `thinking` was silently missing and the
retry never fired, because `ack` was present.

`thinking` is the most likely one to be lost: it is the length-fitted line, so it costs several
Piper passes. `_speak_canned` falls back to live synthesis, so behaviour stays correct — it just
reintroduces the 0.5–1.5 s of dead air that prewarming exists to remove.

**Fix:** verify every key that was requested, not just `ack` — compare against the `lines` dict
passed to `_prewarm_canned`.

---

## P4 — Memory budget is thin and undocumented

> **RESOLVED.** Written up as README § Memory Budget, including the two operational rules
> (stop `face_track.py` before a model/context change; a runner crash self-heals, the turn does
> not). `config/voice.py` points at it from the `OLLAMA_NUM_CTX` line. No knob changed.

Measured with the robot, camera and Ollama all up (8 GB Jetson Orin Nano, shared CPU/GPU memory):

- total 7.6 GB, **~2.0–2.3 GB available** steady-state
- Ollama `gemma2:2b` 2.4 GB resident (`keep_alive=-1`, 100% GPU), face_track ~1.4 GB
- zram swap active, ~300 MB in use across 3 devices
- `OLLAMA_NUM_CTX=2048` costs ~35 MB over 1024 and is fine; **4096 hard-crashes the llama runner**
  (`llama runner process has terminated: signal arrived during cgo execution`)
- raising num_ctx forces a model reload, and that reload OOM'd once while the camera was up —
  it succeeded on retry with more free memory

Ollama is `Restart=always`, so a runner crash self-heals; the voice turn that triggered it still
fails. There is no written budget, so the ceiling gets discovered by crashing into it.

**Fix:** document the budget (a short section in `README.md` or a comment block in
`config/voice.py`), and make model/context changes with `face_track.py` stopped.

---

## P5 — The log is 58% noise

> **RESOLVED.** Both lines are now edge-triggered on face presence plus a 30 s heartbeat, via
> `NO_FACE_LOG_INTERVAL_S` / `CONTROL_LOG_INTERVAL_S` in `config/tracking.py`. `--lofi` keeps its
> 1 Hz cadence — it is a machine-readable stream, not a human log. Rollback is setting both
> intervals to `1.0`; no code change needed.
>
> **Confirmed in production:** 90 s of the running robot with no face present produced 29 log
> lines — 3 `NO FACE` and 3 `[control]`. The old code would have written ~135.

`[face_track] NO FACE — pan=90°` prints once per second forever: **5281 of 9070 lines** in a 1.5-hour
log. `/tmp` is disk-backed and cleared at boot, so growth is bounded by uptime and disk is not at
risk (54 GB free) — the cost is diagnostic. Every investigation starts by filtering this out, and a
real warning scrolls past between two thousand identical lines.

**Fix:** log face-presence **transitions** rather than state, or rate-limit to every N seconds.
`[control] N Hz face=False` has the same shape.

---

## P6 — A permanently failing test

> **RESOLVED — and the diagnosis above is wrong.** It is not a rate-limiter timing assertion (that
> would have been flaky; this failed deterministically). `__init__` centres at 90/90/90, so
> `send(90, 90)` is a real no-op inside `PAN_DEADBAND` and returns `False` at
> `servo/servo.py:163`, never reaching the rate limiter. The test predates the deadband. Fixed by
> sending an angle that changes; `test_rate_limited_returns_false` was passing for the same wrong
> reason and got the same fix. No production code touched. Suite: **862 passed, 0 failed**.

`tests/test_servo.py::TestServoSend::test_first_send_returns_true` fails in isolation and in the full
run (`858 passed, 1 failed`). It is unrelated to any recent change — a rate-limiter timing assertion.

**Why it matters:** a suite that is never green trains everyone to ignore the one number that should
mean something. Either fix it or mark it `skipUnless` with the reason.

---

## P7 — The documents disagree with each other

> **RESOLVED in the sources (reindex pending on the robot).** Chapters → **11**: the Showcase
> page's own body links 11 chapters (bacolod, bohol, bukidnon, cagayandeoro, davao, iligan,
> iloilo, laguna, legazpi, manila, pampanga), so its "10" lede was stale. The general pass also
> caught a second contradiction — "the last 15 years" in `..._Our_Programs_...md` against "17
> years" / "since 2009" / "17th Anniversary" everywhere else → **17**. A numeric sweep of
> `documents/` found no others.
>
> **Reindexed on the robot** (331 chunks, 38.2 s) and face_track restarted, so this is live:
> 56/56 chunks now say "11 active chapters", none say "10", and `retrieve_context()` returns 11.
> Old index kept at `documents/.rag_index.json.bak-20260807` — delete when satisfied.
> Correction to the above: the "15 years" line was never in the index either way, as that section
> does not survive chunking. Only the chapter count was ever reachable by Kai.

Chapter count, as indexed:

- `Nationwide_Chapters_Showcase_-_DEVCON_PH_2.md`: "**10** active chapters across Luzon, Visayas and Mindanao"
- `DEVCON-Philippines-Omnibus-July-2026`: "**11** active chapters nationwide"
- `DEVCON_Philippines_Our_Programs_and_Pioneering_work…md`: "**11** active chapters nationwide"

Kai answers with whichever chunk ranks higher, so the same question gets 10 or 11 depending on
phrasing. Retrieval cannot resolve a contradiction in the sources.

**Fix:** correct the source documents and re-run `python3 -m ai.index_documents`. Worth a general
pass — anything stated twice in `documents/` is a chance for Kai to contradict himself in public.

---

## P8 — The thinking hum is an open-mouthed "hum", not a closed one

> **OPEN, deliberately.** Cosmetic, and the fix means special-casing the audio path for one line.
> It is a judgement call for someone standing in the room, so it stays a decision.

`THINKING_SOUND_TEXT = "Hmm, hmm. Hmm."` now hums instead of spelling itself out (verified by
transcribing Piper's own output with Whisper: `'HUM HUM HUM'`, previously `'H.O.M.A.M.'`).

But espeak-ng renders `Hmm` as /hʌm/ — an open-mouthed "hum" — and no spelling produces a true
closed-mouth `mmm`. Text alone cannot fix this.

**Fix (if wanted):** record or generate a closed-mouth hum once and drop the WAV into
`/tmp/kai_ack`, bypassing Piper for this one line. Cosmetic; only worth it if the current one
sounds wrong in the room.

---

## P9 — Optional: systemd instead of cron + supervisor loop

> **OPEN, deliberately.** The highest blast radius of anything here — it is the one change that
> can leave the robot not starting at boot — and it buys diagnostic convenience, not reliability,
> over a setup that is already supervised and verified. A staged, reversible migration order (unit
> disabled first, two unattended reboots before the cron line is removed) is in
> [resolution-plan.md](resolution-plan.md) § P9 for whenever there is a week of slack.

`scripts/autostart.sh` now supervises `face_track.py` (restart on crash, backoff, no restart on
clean exit or on the instance-lock refusal), started from `@reboot` cron. That closes the gap.

A systemd **user** unit would be strictly better — `Restart=always`, proper dependency ordering on
`nvargus-daemon` and PulseAudio, `journalctl` instead of a flat file, and `systemctl status` for
whoever is standing next to the robot. `loginctl enable-linger devconph` is already applied, which
is the part that usually blocks this.

Not urgent: the current setup is supervised and verified (SIGKILL → restart in 5 s; SIGTERM → clean
`[face_track] Stopped.` and no restart).
