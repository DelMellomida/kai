# Natural audio — the offline levers

Goal: Kai stops sounding AI-generated. **Fully offline** — the cloud option was put on the table on
2026-08-10 and declined, so nothing here reaches the network. Written 2026-08-10; nothing in it is
implemented.

**Everything here is additive and gated.** Kai keeps today's audio until a gate is passed by ear, the
revert is one config line at every point, and two of the four steps may honestly end in "no
difference, put it back".

This is the second pass at this complaint. The first one —
[`completed/expressive-voice-plan.md`](../completed/expressive-voice-plan.md) — ran to its own abort
criteria and shipped `ai/delivery.py` instead of a new voice. **Read its table before starting**, and
read § What this plan cannot do before promising anyone the result.

---

## What is already closed. Do not retest any of this

Measured on the robot 2026-08-09, all of it in the completed plan:

| hypothesis | test | result |
|---|---|---|
| Wrong voice | 29 candidates, 7 engine families | all rejected by ear the same way |
| The voice is flat in pitch | p10–p90 semitone range | current voice **11.0 st**; human conversational is 6–12 |
| The sox compand flattens it | dynamics before/after | **0.4 dB**, not the ~5 dB first claimed |
| The text is too written | disfluent/paused variants | range unchanged, 9.0 → 9.0 st |
| A livelier speaker exists | 280 of libritts_r's 904 speakers | none beat the current voice |
| The GPU will fix it | VoxCPM-0.5B | **OOMs on load**, twice, with 2.4–3.0 GB free |

The structural finding: every TTS model small enough to run beside Ollama here was trained on
read-aloud audiobook corpora, so it narrates. That is not fixable from this side of the model.

Already shipped against it: `ai/delivery.py` — a semicolon breath before a clause-initial
conjunction, a CRC-gated discourse-marker opener on 35% of replies, ±6% per-reply tempo jitter.
Verified on the robot the same evening.

---

## The gap this plan lives in — three findings from reading the code, 2026-08-10

**1. Piper's own prosody parameters have never been passed.** Piper is a VITS model and generates
prosody from noise. Three inference parameters control it; [`ai/tts.py:219-229`](../../../ai/tts.py#L219-L229)
passes exactly one:

| parameter | what it controls | driven today? |
|---|---|---|
| `length_scale` | speaking rate | yes — dashboard slider + `delivery.length_scale` jitter |
| `noise_scale` | acoustic sampling: timbre and pitch contour within a line | **no** |
| `noise_w` | phoneme *duration* variation — the rhythm | **no** |

Unpassed means each voice runs at whatever its `.onnx.json` shipped. Read off the files in
`voices/`, which are checked in:

| voice | `noise_scale` | `noise_w` |
|---|---|---|
| **en_US-hfc_female-medium** (shipping) | 0.667 | 0.8 |
| en_GB-jenny_dioco-medium | 0.667 | 0.8 |
| en_US-lessac-medium | 0.667 | 0.8 |
| en_US-libritts_r-medium | 0.333 | 0.333 |
| en_US-ljspeech-high | 0.667 | 0.333 |

So the shipping voice sits at the stock VITS defaults, and the "flat, uniform, no ups and downs"
complaint has never been tested against the two parameters that literally control variation. The
complaint's own words point at `noise_w`: the thing that survived every timbre measurement above was
*rhythm*.

**This also invalidates a comparison in the previous plan.** hfc_female was A/B'd against
libritts_r, and those two voices ship at 0.667/0.8 versus 0.333/0.333 — less than half the
variation. Some unknown share of that verdict was these parameters, not the speaker.

**2. The output chain is loudness-only.** `TTS_POST_EFFECTS` is `compand … gain -n -1`
([`config/voice.py`](../../../config/voice.py)) — no EQ, no room. Piper writes 22050 Hz mono; sox
duplicates it to stereo, compresses and peak-normalises to −1 dB, and that is the whole chain.
Bone-dry, dead-centre, flat-EQ speech is itself a synthetic cue, independently of the model, and this
is the cheapest place in the system to change it.

**3. Tempo varies per reply, not per phrase.** [`ai/delivery.py:171`](../../../ai/delivery.py#L171)
picks one `--length-scale` for a whole turn. Real speech varies phrase to phrase; a four-sentence
reply currently comes out at one metronomic rate.

And two items the previous plan explicitly listed as safe-to-ship, which never shipped: the
disfluency transform it prototyped, and `en_US-libritts_r-medium` speaker **188**.

---

## Step 0 — access and a baseline (0.5 h) — **Gate 0**

Nothing else in this plan is judgeable without this step.

**Access.** Key auth is refused (`Permission denied (publickey,password)`) and Windows has no
`sshpass`/`plink`, so this needs the `devconph@192.168.1.25` password through **paramiko**, with
scripts base64'd across — see the SSH notes in `docs/operating.md` and the established idiom.
`POST /voice/say` on port 8081 covers *listening* to a change without a shell; it does not cover
measuring one.

**Baseline, kept as files so later steps compare against audio and not memory.** Four reference
lines, each captured **raw pre-sox and post-sox** into `/tmp/kai_audio_baseline/`:

1. a short English reply (one sentence)
2. a long English reply (four sentences — the shape the complaint is loudest about)
3. a Tagalog reply (the voice is `en_US`; this is the worst case and the one an event hears most)
4. one filler opener from `config/filler.py` (cached audio, a different code path)

Then, per file: `scripts/tts_pitch.py` for median f0 and p10–p90 semitone range, and the longest
interior silence **on the raw file only** — the completed plan records that sox lifts the noise floor,
so an absolute −45 dBFS gate finds zero gaps even in unshaped audio.

Also record `[turn]` first-audio latency for ten real turns from `/tmp/face-servo.log`, so a
synthesis-cost regression in step 1 shows up as a number rather than a feeling.

**Gate 0:** the baseline reproduces twice with the same numbers. If it does not, the measurement rig
is the bug and everything downstream is noise.

---

## Step 1 — pass the prosody parameters (1.5 h) — **Gate A**

The cheapest untried lever in the system: one CLI argument, no extra synthesis, no new process.

**The flag name must be discovered, not assumed.** Piper has spelled the duration knob `--noise_w`,
`--noise-w` and `--noise-w-scale` across versions. `requirements.lock.txt` pins `piper-tts==1.4.2`,
but a wrong flag here does not degrade the audio — Piper exits non-zero, `_run_piper` returns False,
and **every reply is silent**. So read the spelling off `python3 -m piper --help` once per process,
cache it, pass only what is advertised, and log once what was found. An unknown flag is simply not
passed, which is today's behaviour exactly.

**The rest of the work:**

- `TTS_NOISE_SCALE = 0.667`, `TTS_NOISE_W = 0.8` in `config/voice.py` — the shipping voice's *own*
  values, so adding the plumbing is byte-identical to today until somebody moves a slider.
- Two live knobs in `settings.py` (`tts_noise_scale`, `tts_noise_w`), bounded **0.0–1.5**. Live because
  this is a change only an ear can judge, and it has to be flippable mid-conversation.
- **Two lines in `face_track.py`**, mirroring [`face_track.py:284`](../../../face_track.py#L284):
  `settings.on_change("tts_noise_w", lambda v: _session.reprewarm_canned(), debounce=1.5)`. Without
  them the cached lines keep the old prosody while fresh replies get the new one — see step 4.
- Tests: flag resolution against fake `--help` text (all three spellings, and none), the args
  actually reaching Popen, and the no-flags-found path leaving the command line untouched.

**What to listen to, in this order.** `noise_w` alone first — 0.8 / 0.9 / 1.0 / 1.1 — on reference
line 2, because rhythm is what the complaint describes. Then the best of those crossed with
`noise_scale` 0.667 / 0.8. Eight cells, one long line each; a grid script writes them labelled and
runs `tts_pitch.py` over each.

**Gate A:** at least one cell is meaningfully less machine-like by ear on the long line **and** keeps
every word intelligible, **and** the numbers move in the direction the ear reports (semitone range up,
per-word duration spread up). Impression plus measurement, because this project has a documented
history of changes that sounded different and measured identical.

Failing that: both constants go back to the voice's defaults and step 2 begins. The plumbing stays —
it costs nothing at defaults, and it permanently closes "we never tried the obvious parameters".

**Known risks.** Past roughly 1.0–1.2 either parameter stops sounding expressive and starts sounding
unstable — slurred consonants, wandering pitch, the occasional mispronounced word. Synthesis cost
*should* be unchanged (same graph, different noise vector); measure it against the Step 0 latency
figures rather than assuming, since a filler bank of 52 lines re-warms through this path.

---

### Gate A result — measured on the robot 2026-08-10, the same day

**Steps 0 and 1's measurement half are done. The prosody knobs failed; a third parameter nobody knew
about passed.** Flags confirmed against the installed `piper-tts==1.4.2`: `--noise-scale`,
`--noise-w-scale` (with `--noise-w` as an accepted alias, so the three-spellings risk is moot on this
version), and — not previously known to exist — **`--sentence-silence`** and `--no-normalize`.

Baseline, four reference lines, sox'd: short_en **7.7 st**, long_en **9.3**, tagalog **10.0**,
filler **9.9**, all 197–216 Hz.

Then four repeats per config of the long line, because one sample per cell cannot tell an effect from
a seed draw — VITS samples fresh noise every run:

| config | intonation range, 4 runs | mean | duration mean |
|---|---|---|---|
| today (no flags) | 9.4 / 9.6 / 9.5 / 9.8 | **9.58 st** | 16.16 s |
| `noise_scale 0.8, noise_w 1.1` | 9.0 / 9.4 / 9.4 / 9.9 | **9.43 st** | 16.80 s |
| `sentence-silence 0.4` | 9.3 / 9.0 / 9.3 / 9.0 | **9.15 st** | 17.10 s |

**Within-config spread is ±0.4–0.9 st, so the knobs move pitch range by less than the noise floor,
and the "livelier" setting measures marginally *lower* than today.** An eight-cell sweep
(`noise_w` 0.8→1.2 × `noise_scale` 0.667/0.8) stayed inside 8.8–10.0 st, i.e. inside that same band.
Add this to the completed plan's table of killed hypotheses: *the VITS noise parameters do not
increase intonation range.* What they do change, outside the noise, is **timing** — +0.64 s on
identical text, ~4%. Synthesis cost is unchanged (3.4–3.7 s per cell, every cell), so the plumbing is
still free; it just does not buy what it was expected to buy.

**The finding worth having: Kai has never paused between sentences.** `--sentence-silence` was never
passed and **its default is 0** — the no-flag file and an explicit `0.0` have identical gap profiles.
Measured longest interior silences on the raw pre-sox long line:

| setting | the three sentence boundaries |
|---|---|
| **today / 0.0** | 0.20, 0.17, 0.14 s |
| 0.3 | **0.49, 0.47, 0.45** s |
| 0.5 | 0.80, 0.74, 0.67 s |

So today's longest inter-sentence pause is ~0.2 s, which is just a phrase-final decay, against
0.4–0.7 s for conversational speech. Kai runs four sentences together without ever taking a breath —
a mechanical cue with a mechanism behind it, unlike everything else measured in either plan. Note
`ai/delivery.py` already buys a 0.156 s breath *inside* long sentences via `DELIVERY_PAUSE`, while the
one place a person pauses longest got nothing.

**Safe for the cached bank**, checked before recommending it: `--sentence-silence` does **not** pad the
tail (trailing silence stays 0.13–0.18 s at every value), so a one-sentence filler stall is unchanged
(1.11 → 1.14 s, inside noise) and `FILLER_MAX_STALL_S` is untouched. Two-sentence lines grow by ~0.31 s
per interior boundary, which the openers have room for under `FILLER_MAX_LINE_S = 10.0`. No trailing
silence also means no risk of re-creating the stranded-jaw bug fixed on 2026-08-10.

**SHIPPED 2026-08-10, verified on the robot.** `TTS_SENTENCE_SILENCE_S = 0.35` plus the two noise
parameters plumbed at the voice's own values, three dashboard sliders, and `reprewarm_canned()` wired
to all three. The live `_speak` path measures **0.59 / 0.56 / 0.54 s** at its sentence boundaries
(was 0.20 / 0.17 / 0.14), the filler bank came back **52/52 with identical per-language pools**, and
the suite is 1237 passed / 2680 subtests. See the CHANGELOG entry of the same date. **What remains
against step 1 is one judgement only an ear can make:** whether 0.35 is the right value in the room,
and whether the two noise knobs are worth moving despite measuring nothing.

**Original recommendation, as written before shipping:** ship `TTS_SENTENCE_SILENCE_S` at **0.3–0.4** as the primary
change and keep the two noise knobs as ear-only options defaulted to the voice's own values — the
measurement says they do nothing, so they need an ear to justify shipping at all, and the honest
default is off. The uniformity of the resulting gaps (0.45/0.47/0.49 — near-identical at every
boundary) is itself a residual tell, and the only fix for that is per-sentence synthesis, which is
step 3 / R5.

---

### Tagalog: closed 2026-08-11, and the answer is that offline Filipino TTS does not exist here

Opened because Tagalog is the room's default language and the voice is `en_US`, so Tagalog has always
been pronounced with English phonetics. Three findings, the last decisive:

- **There is no Filipino voice in Piper at all.** The catalogue is **173 voices across 54 language
  codes** and contains no `tl`, `fil` or `ceb`. So a native voice is not a download away — it is a
  training project. **Correct a stale comment while you are here:** `config/filler.py` says the Bisaya
  lines run through "the Tagalog voice", implying one exists. None does; every Tagalog and Bisaya line
  Kai speaks comes out of an American English model.
- **The phonemizer can be swapped without changing the voice, and it does not fix the problem.** Piper
  takes `-c`, and the espeak language lives in the voice config, so Kai's own acoustic model can be
  driven with Indonesian, Malay or Spanish phoneme rules. Verified working, and **voice identity
  survives** — 200–206 Hz and 7.4–8.2 st across all four variants, against Kai's 206 Hz / 8.3 st.
- **A dedicated `id_ID` voice fails the she/they constraint anyway**: 254.9 Hz and a **3.4 st** range,
  i.e. higher-pitched and, by `tts_pitch.py`'s own scale, monotone. `es_ES-sharvard` speaker 1 matches
  Kai's pitch closely (204 Hz) but reads Tagalog no better.

**Whisper readback could not rank any of it** — across two runs the winner on *"Lahat"* flipped between
the English and Malay phonemizers, i.e. the differences sit inside Whisper's own Tagalog error rate.
Do not re-derive a verdict from transcripts; it is not a sharp enough instrument for this question.

**A native Tagalog speaker judged the A/B and said both sounded bad.** That is the finding. Phoneme
rules cannot rescue an audiobook-trained English acoustic model reading Tagalog — the same structural
wall as the completed plan, in a second place. The three candidate voices were deleted again (189 MB);
the rendered comparison is in `voice-audition/tagalog-ab/` until someone clears it.

**What is left for Tagalog, honestly:** pre-rendering the FIXED Tagalog lines (the 52-line filler bank,
the greeting, the ack) once with a good Filipino voice and committing the WAVs — runtime stays fully
offline and it fixes the most-heard Tagalog, but not dynamic replies; or a `fil-PH` cloud voice, which
is the only thing that fixes dynamic Tagalog and costs the offline claim; or accepting it. Nothing in
the offline toolbox moves this further.

---

## Step 2 — the output chain — **SHIPPED 2026-08-11, Gate B outstanding**

`TTS_POST_HIGHPASS = ["highpass", "90"]` before the compand and
`TTS_POST_ROOM = ["reverb", "18", "50", "28", "100", "0", "-4"]` after it, each its own constant so
loudness, EQ and space revert independently; order in `ai/tts._sox_chain()` and pinned by tests.
Measured: duration unchanged under every chain (the tail decays into Piper's existing padding rather
than extending the file), trailing silence shrinks 0.13 → 0.10 s, Whisper transcribes all chains
identically, and a live reply's sub-90 Hz share fell 0.55% → 0.18%. A stronger room
(`30 50 45 100 0 -2`) was rendered alongside; both are in `voice-audition/room-ab/`.

**Gate B is still open and can only be closed in the venue**, not at a desk: reverb trades against
intelligibility in ambient noise, which is the trade the wake path already lost once. If Kai is harder
to understand at an event, `TTS_POST_ROOM = []` keeps the EQ and drops the space.

### Original plan for this step

## Step 2 — the output chain (1 h) — **Gate B**

Two additions, each behind its own constant so loudness and naturalness revert independently:

- **A high-pass around 90 Hz.** The PAM8403 into a small driver reproduces nothing down there; the
  energy only eats headroom that the compand then reacts to. Also the cheapest way to stop the
  chassis buzzing on plosives.
- **A very short room** — sox `reverb` at low wetness, small room scale. Bone-dry mono is a synthetic
  cue in itself, and this is the one item in the whole plan that changes the *space* rather than the
  voice. Free: sox is already in the chain.

Optionally a gentle cut in the 3–5 kHz region if the voice is harsh on this speaker; decide by ear,
not preemptively.

**Gate B: intelligibility in the venue, not at a desk.** Reverb trades directly against the thing
this robot needs most in a noisy room, and this is exactly the trade the wake-word work already lost
once to ambient noise. Any loss of word clarity → wetness to 0, keep the EQ. Also re-check loudness:
inserting a filter before `gain -n -1` changes the peak the normalise works from, so "it got quieter"
is a predictable outcome to look for rather than a surprise.

---

## Step 3 — pacing per phrase, and why it is not this plan's job

The finding is real: one `--length-scale` per turn is a metronome across four sentences. The fix is
to synthesize **per sentence**, which also cuts time-to-first-audio because sentence 1 can play while
sentence 2 synthesizes.

That is already scoped elsewhere — `latency-plan.md` step 2 and ticket
[R5](../../tickets/R5-serialised-first-audio-latency.md) — and it touches the speak path, the jaw
schedule and the interrupt seams. **Do it there, not here.** Noted in this plan only so the next
person does not rediscover it as an audio idea and implement a second, competing version.

The cheap approximation available today is punctuation-driven, which is what `DELIVERY_PAUSE` already
does.

---

## Step 4 — the two unshipped leftovers (1 h)

- **The disfluency transform** the previous plan prototyped. Its seam moved: the old
  `ai/voice_assistant.py:1174` is now `ai/delivery.shape()`, which is the correct home and already
  has 32 tests around it. Measured to do nothing for pitch range; kept because it is cheap, testable,
  and composes with everything above.
- **`en_US-libritts_r-medium` speaker 188** — 10.5 st, faster than the current voice. A/B it **with
  `noise_scale`/`noise_w` set equal on both voices**, which the previous comparison could not do
  because the knobs were not reachable. This is the one place step 1 unlocks new information about an
  already-closed question.

---

## Step 5 — verify, and the cache trap (1 h) — **Gate C**

**Every canned line is pre-synthesised**: the 52-line filler bank, the wake acknowledgement, the
startup greeting. Two consequences that are easy to miss:

1. A prosody change makes the cache disagree with live replies until `reprewarm_canned()` runs —
   hence the `on_change` wiring in step 1. During an A/B, expect a mismatch until the 1.5 s debounce
   fires and the re-warm finishes; that is the tool working, not a bug.
2. **`FILLER_MAX_LINE_S` (10.0) and `FILLER_MAX_STALL_S` (1.8) were derived from Piper's WAV padding
   at today's parameters.** `noise_w` changes phoneme durations, so the caps will reject a *different*
   set of lines — `config/filler.py` records that 10 of 20 stalls were rejected at 1.2 s for exactly
   this class of reason. Read the per-language pool line in the log after the restart
   (`[session] filler bank: N/52 … [tl 12op/12st, ceb …]`), and **run
   `python3 -m scripts.filler_check`** — outstanding since 2026-08-07 and never once run.

Also: Whisper transcribe-back on every changed canned line (the `"Hmmmm..."` → `"H-A-M-A-M-M"` rule
in `config/thinking.py`), and the full suite.

No soak is required. Nothing here adds a process, a thread or a resident model — which is precisely
what makes this plan cheap compared to the last one.

**Gate C:** suite green, filler pools no smaller than today's, no canned line mistranscribed, no
first-audio regression against Step 0.

---

## Abort criteria — decide these now

- Gate 0 fails: the baseline is not reproducible → stop, fix the rig.
- Gate A fails on all eight cells → constants back to voice defaults, keep the plumbing, continue at
  step 2.
- Gate B costs word clarity in the room → wetness 0.
- Any first-audio regression above ~0.3 s → revert the step that caused it. Latency is the complaint
  this robot has actually lost demos to.
- Filler pools shrink, or `filler_check` flags lines that pass today → revert step 1's values and
  re-derive the caps before trying again.

## Revert table

| change | revert |
|---|---|
| prosody values | `TTS_NOISE_SCALE = 0.667`, `TTS_NOISE_W = 0.8`, restart |
| prosody plumbing entirely | delete the two `on_change` lines; the probe passes nothing at defaults |
| sox EQ / room | drop the added constant from `TTS_POST_EFFECTS` |
| disfluencies | its own constant, or `delivery_shaping` off from the dashboard |
| voice swap | `TTS_VOICE_MODEL` back one line, then `reprewarm_canned()` |

## What ships even if every gate fails

- The prosody plumbing at defaults: no audio change, and the "we never tried the obvious parameters"
  gap is closed permanently rather than rediscovered a third time.
- A reproducible baseline — four reference lines, raw and processed, with f0, semitone range and pause
  numbers — which no previous pass left behind.
- The `filler_check` run that has been outstanding since 2026-08-07.

## What this plan cannot do

State it once, plainly, so it is on the record and not rediscovered at an event: **these are
refinements to a voice that already resisted a full pass.** The measured ceiling is real — an
audiobook-trained 20M-parameter model narrates, and no parameter here retrains it. Expect "less
obviously synthetic", not "sounds like a person".

The move that clears that bar is the one declined on 2026-08-10: cloud TTS measured **272 ms** round
trip from this Jetson and has native `fil-PH` voices, which would also close the Tagalog-in-an-en_US-
accent gap that no offline step above touches. If the bar is "nobody can tell", that decision is the
lever, not this document.

## Timeline

| | | cumulative |
|---|---|---|
| Step 0 | access, baseline, **Gate 0** | 0.5 h |
| Step 1 | prosody knobs + grid, **Gate A** | 2 h |
| — | **decision point: did anything actually change?** | |
| Step 2 | sox EQ + room, **Gate B** | 3 h |
| Step 4 | disfluencies, voice re-A/B | 4 h |
| Step 5 | verify, filler caps, suite, **Gate C** | 5 h |

The decision point after step 1 is the important one: two hours in, the cheapest lever has either
produced an audible difference or it has not, and that answer decides whether steps 2 and 4 are worth
the rest of the day.
