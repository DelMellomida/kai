# Filler responses — WIP notes

**Status: tests green, NOT verified on hardware. Do not treat the robot as good.**
Last touched 2026-08-09. Companion to `filler-responses.md`, which describes the design as
intended; this file describes where the work actually stands.

The feature is wired in and was deployed to the robot once. That run exposed two real bugs plus
a third reported by ear. Fixes for all three are written, and **the suite is now green** (1094
tests, ~3 s) — but **the robot has not been restarted onto them and `scripts/filler_check.py` has
still never run.** Nothing below has been heard out loud.

---

## What is done and believed good

- The 40 lines (20 openers, 20 stalls; 12 tl / 4 ceb / 4 en) in `config/filler.py`. Two Tagalog
  openers had their `...` replaced with a comma on 2026-08-09; see the tests section for why that
  was a bug and not a style call.
- Selection logic in `ai/filler.py` — pure, injected `Random`, tested.
- 8 new facts appended to `documents/kai_facts.txt`; index rebuilt (513 chunks).
- `scripts/rag_accuracy.py` at 29/32. The 3 misses are pre-existing, confirmed by re-running
  them with only the 8 new chunks dropped from the loaded index.
- `scripts/filler_check.py` — the on-robot synthesise-then-transcribe check. **Never run yet.**

---

## What the robot run found (2026-08-07, log `/tmp/face-servo.log`)

Restarted via `kill -9` on `face_track.py` so `autostart.sh`'s supervisor relaunched it
(SIGTERM would exit 0 and the supervisor would *stop* — see below). It came up on the new code
and immediately showed:

### Bug 1 — the 44-line prewarm burst ran through a live turn

17 filler lines failed to synthesise in a row, the reply's own synthesis was read while still
being written (`EOFError` in `wave.open`, `sox`: "RIFF header not found"), and the turn took
**24.6 s to first audio, 12.3 s of it synthesis**.

Cause: `tts` publishes ONE `_synth_proc` handle and `stop()` kills whatever is in it. Handing
all 44 lines to `prewarm_canned` in a single burst put a second Piper on the CPU beside the
reply's, and made `stop()` kill the wrong process.

**Fix written:** `_canned_lines()` is back to the four core lines only. The bank warms in
`session._prewarm_bank` — one line at a time, each gated on `_quiet_for_synth()`, with
`BANK_*` pacing constants in `config/filler.py`. `_warm_all` runs core first, then bank.

### Bug 2 — filler synthesised itself live on a cache miss

`_speak_filler` went through `_speak_canned`, which falls back to `_voice._speak()` →
`tts.synthesize()` → the **fixed shared** `_RAW_WAV` / `_OUTPUT_WAV`. During BUSY that is the
same pair of files the reply is about to write. Hence the corruption above.

Wrong even without the collision: filler exists to mask latency, and a live Piper run costs
0.5–1.5 s of CPU the reply is competing for — so it *lengthened* the wait it was covering.

**Fix written:** `_speak_filler` plays from cache or stays **silent**. Never synthesises.
Consequently `pick_opener` / `stall_queue` now take a `have` set and only choose warm keys; an
empty result returns `False` from `_tick_filler` and the old "Hmm" takes the turn.

### Bug 3 — fillers talking over each other (reported by ear)

The loop guarded on `tts.is_playing()`, which only goes true once a playback *process* exists.
`_speak_wav` hands off to a worker thread first, so at 20 Hz there were several ticks where a
line had been started but was not yet "playing" — and each of those ticks started another one.

**Fix written:** guard on `self._voice.speech_in_flight()`, which covers the whole span from
before synthesis to end of playback. That is the window in which nothing else may start.

---

## Changed 2026-08-09, after the tests went green

Four asks, of which two were already satisfied:

- **One "Hmm", not a 1.5 s one.** Already done — `THINKING_SOUND_TEXT = "Hmm."`,
  `THINKING_SOUND_TARGET_S = 0.0`, and `_played_thinking` latches it to once per turn. Only the
  comment above it needed fixing: it still told the next reader to use "repeated short units",
  which is exactly how the three-hum version would come back.
- **Long filler first, short after.** Already the shape — one opener, then stalls on a loop.
- **At least 1 s before any filler line.** New. `FILLER_MIN_GAP_S = 1.0`, and both jitter ranges
  moved to `(1.00, 1.60)` — the whole room left between the floor and the ceiling minus the
  playback reservation (1.65). Enforced in `session._filler_gap`, the one place both bounds are
  applied, so widening the jitter downward cannot quietly remove it. **The ceiling wins** if the
  two ever conflict. Side benefit: the opener delay no longer starts under `THINKING_SOUND_DELAY_S`
  (0.6), so on a turn where the bank has nothing the "Hmm" reliably gets its tick first.
- **No line repeats within one conversation.** New, and the substantive one. The shuffle only ever
  covered a single turn — the queue is rebuilt on every entry to BUSY and a fresh shuffle knows
  nothing about the last one's, so "Sandali ha" could open the stalls of three turns running while
  the opener guard excluded only the line immediately before. `_filler_used_openers` /
  `_filler_used_stalls` now carry it across turns, cleared in `_begin_session`.

  **Scope is one conversation** — wake, talk for as many turns as you like, nothing repeats; greet
  Kai again and the bank is whole. Deliberately not per-turn (that is what it already was, and what
  allowed the repeat) and not never-reset (a demo would exhaust 12 Tagalog stalls in an afternoon
  and spend the rest on the fallback). Note a repeated "hey Kai" that lands in LISTEN_WAIT does not
  reach `_begin_session`, so it continues the conversation and keeps its history — correct, but
  worth knowing if it ever looks like the reset is not firing.

  The rule is soft: an exhausted set starts a second lap rather than going quiet, because refusing
  to repeat would trade a small tell for the dead air the whole module exists to prevent. The
  *warm* filter is not relaxed that way — an uncached line plays nothing at all.

Suite after all four: **1108 tests, OK.**

## Also changed in the previous pass

- **The "Hmm" is one hum again.** `THINKING_SOUND_TEXT` = `"Hmm."` and
  `THINKING_SOUND_TARGET_S` = `0.0` (no stretch). The 1.5 s target is what had forced
  `"Hmm, hmm. Hmm."` — a single hum needed ~3x length-scale to reach it, which smears the hum.
  The bank covers the long wait now, so the stretch buys nothing.
- **A 10 s hard cap per line**, enforced at prewarm, not by counting words:
  `session._within_length_cap` measures the WAV Piper produced and refuses to cache anything
  over `FILLER_MAX_LINE_S` (10.0) — or `FILLER_MAX_STALL_S` (1.2) for stalls. Rejected lines are
  never cached and therefore never selected. Measured rather than counted because speaking rate
  is a live dashboard setting, so the same text crosses the cap at one rate and not another.

---

## The tests: green, 2026-08-09

`python3 -m unittest discover -s tests -t .` → **1094 run, 0 failures, ~3 s.**

The 13 failures are closed. Two of the three diagnoses in the previous version of this file were
wrong in a way worth recording, because both looked like "the spec and the lines disagree" and
both were mechanical bugs:

**The two line failures were one bug, not two, and needed no product decision.**
`test_openers_are_one_or_two_sentences` counts `[.!?]` *characters*, so a single `...` scores as
three sentence-enders. Both lines reported **5** sentences, not 3. Dropping the ellipsis fixed
that test and `test_no_ellipses_or_dashes` together, and both lines are still 2 sentences with
the joke intact — the ellipsis became a comma, so the beat before the punchline survives as a
Piper comma pause:

- *"...ang bilis ko mag-jowa, i mean, mag-process."*
- *"Hmm, isang segundo lang, tao rin ako, charot, robot ako!..."*

The ellipsis rule was never style policing — it is the espeak-ng hazard from
`config/thinking.py:72-86`, the same class of bug that turned `"Hmmmm..."` into "H-A-M-A-M-M".

**The nine `TestFiller` failures were not about the `tts.is_playing` patch target.** The real
cause was the fixture: `SessionCase.make` builds the warm cache from `_canned_lines()`, which the
prewarm split made core-only — so the bank was **cold in every test**, `pick_opener(have=...)`
returned `""`, and the whole class was silently exercising the "Hmm" fallback while claiming to
test the bank. `TestFiller.make` now warms the bank explicitly.

Two further seam problems fell out of that:

- `FakeVoice._speak_wav` latches `speaking = True` and nothing in a tick loop clears it, so a fake
  left to itself reports the opener playing forever and no stall can follow. `self.playing` now
  drives **both** `tts.is_playing` and `speech_in_flight`, so the default (False) means "playback
  ends immediately" — the worst case for the gap contract.
- One test deliberately makes them **disagree**
  (`test_the_overlap_guard_covers_the_gap_before_playback_starts`): nothing playing, speech in
  flight, nothing new may start. That is the regression guard for Bug 3.

Retargeted or added: the bank is warmed one line at a time (`test_the_bank_is_warmed_one_line_at_a_time`
— one synth call per line *is* the fix), core-before-bank ordering, the quiet gate, the length cap
including the tighter stall cap and the 0.0-measurement fallback, cache-miss silence, and
half-warm-bank selection.

### A real bug the tests found

`_tick_filler` latched `_filler_opened = True` **before** the empty-key check. The "Hmm" fallback
only fires on a tick where `_tick_filler` returns False *and* `elapsed >= THINKING_SOUND_DELAY_S`
(0.6) — but the filler delay draws from 0.45–0.90. So on any turn drawing under 0.6, the latch
spent the turn's only False tick too early, every later tick returned True from the stall branch,
and **the turn was silent end to end** — the exact dead air the bank exists to remove, on roughly
half the draws. Fixed by latching only once a line is actually chosen;
`test_a_cold_bank_hands_the_turn_back_to_the_hmm` walks three draws around the boundary.

### The suite is no longer slow

It was ~22 minutes with one test that looked like a hang. Neither was a wedge:
`session._prewarm_bank` paces itself with **real `time.sleep`** — `BANK_SYNTH_GAP_S` between
lines, plus up to `BANK_QUIET_WAIT_TRIES × BANK_QUIET_POLL_S` (10 s) waiting for quiet, across 40
lines × `BANK_PASSES`. Any test reaching a re-warm paid ~36 s; `test_rewarm_waits_for_a_reply_to_finish`
holds `voice.speaking = True`, so it never went quiet and paid ~20 minutes.
`SessionCase.setUp` now patches those three constants to 0/0/1. The pacing is about real Piper CPU
on the Jetson and buys nothing against a mocked synth, so no coverage is lost.

---

## Next steps, in order

1. ~~Fix the 13 tests, then re-run the suite.~~ **Done 2026-08-09 — suite green.**
2. **Restart the robot** and watch for
   `[session] filler bank: N/52 lines cached (pass 1, +N) [tl 12op/12st, ceb 4op/10st, en 4op/10st]`.
   The per-language pools in the brackets are the numbers that matter: a stall pool that arrives
   under ~8 will repeat inside one wait no matter what the total says.
   Confirm no `could not pre-synthesize` storm and no `EOFError` traceback.
3. **Run `python3 -m scripts.filler_check` on the robot.** Still never run. It flags lines whose
   audio does not match their text, and over-long stalls. Review flags by ear — a Bisaya line can
   be fine audio that Whisper simply cannot transcribe.
4. Listen to a slow turn end to end: one opener, then stalls, no overlap, nothing over 10 s, and
   the real answer cutting in cleanly.

## Open questions

- Bisaya has no Piper voice; it runs through the Tagalog one. Needs an ear check, and if it
  sounds wrong rather than accented, either rewrite those 4 lines or set `FILLER_CEB_SHARE = 0`.
- `FILLER_MAX_LINE_S = 10.0` is the user's stated ceiling. Several openers are two full
  sentences and may land near it. Rejections will name themselves in the log at prewarm; shorten
  whatever appears there.

---

## Restarting the robot (the part that is easy to get wrong)

`face_track.py` installs a SIGTERM handler that gives the **same orderly shutdown as Ctrl-C**,
i.e. exit code 0 — and `scripts/autostart.sh`'s supervisor reads exit 0 as "someone asked it to
stop" and **breaks the loop**. So a plain `pkill -f face_track.py` leaves Kai down.

```sh
ssh devconph@192.168.1.25
pgrep -af 'autostart.sh|face_track.py'   # supervisor present?
pkill -9 -f face_track.py                # rc 137 -> supervisor relaunches after 5s
tail -f /tmp/face-servo.log
```

Repo on the robot is `/home/devconph/Documents/kai`, which is the same filesystem as the
`\\192.168.1.25\Documents\kai` share — edits made over the share are already on the robot, but
`config/` is restart-only and the RAG index is loaded once at startup, so a restart is required
for either to take effect.
