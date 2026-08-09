# Filler responses

## The problem

Kai's real answer takes a variable amount of time: STT, then RAG, then Ollama. When it runs
long — or errors out entirely — the room gets dead air and the robot reads as broken. A person
waiting on a robot reads two seconds of silence as a crash, not as thinking.

The only cover used to be `THINKING_SOUND_TEXT` (`config/thinking.py`), played after
`THINKING_SOUND_DELAY_S = 0.6` s. That is sized to the measured ~1.3 s turn and does its job
there, but it has two limits: it cannot cover a long wait, and it is the same sound every single
turn, which an audience notices inside three questions. It is now a single unstretched `"Hmm."` —
one hum that marks the start of a think — and it survives only as the fallback for when this bank
is off or cold. (It was `"Hmm, hmm. Hmm."` for a while; that repeat existed purely to reach a
1.5 s target without a length-scale stretch that smeared the hum, and the bank makes the target
unnecessary. Don't restore it.)

**The rule this feature is accountable to: no more than 2 seconds of silence, anywhere in a
turn** — not before Kai starts speaking, not between two things Kai says — **and no less than
1 second before any filler line.** The second half is not a nicety; see the floor below.

## The shape of a turn

```
[1 s <= silence < 2 s] -> one OPENER -> stall -> stall -> stall ... -> the real answer
```

Two tiers, because one long line cannot cover an arbitrarily long wait, and repeating a long
line is unbearable:

| Tier | Count | Length | Played |
|---|---|---|---|
| **Openers** (`FILLER_OPENERS`) | 20 (tl 12 / ceb 4 / en 4) | 1–2 sentences | exactly one per turn, first |
| **Stalls** (`FILLER_STALLS`) | 32 (tl 12 / ceb 10 / en 10) | 1–4 words | on a loop after the opener, as many times as needed |

The stalls do not follow the openers' 60/20/20 split, and that asymmetry is deliberate. An opener
is drawn once per turn, so its per-language count only has to outlast a *conversation*. Stalls
loop until the answer lands — a ~10 s wait spends three or four — so what governs whether a line
repeats is the size of **one language's pool**, not the bank total. At four lines, `ceb` and `en`
lapped inside a single wait.

Openers are long enough to buy several seconds and to carry a real fact about Kai, so the wait
teaches the audience something instead of just filling air. Stalls carry nothing — they exist
to fill the tail, and they are short so any one of them can be cut off mid-word the moment the
real answer arrives without sounding wrong.

Language split, both tiers: **12 Tagalog / 4 Bisaya / 4 English**. Keyed by the language code
Whisper detects, so a turn never switches language halfway through. `ceb` has no Piper voice of
its own; the Tagalog voice is close enough phonetically that Bisaya reads as accented rather
than wrong — a judgement to confirm by ear on the robot, not from the text.

## Tone

Comedic meme register, not customer service. "Nagbu-buffer pa ang utak ko, wag mo akong
i-refresh" rather than "please wait while I process your request". The opener structure is
fixed — *acknowledge the likely problem, then one Kai fact* — but the acknowledgement is where
the joke lives: the noisy room is a palengke, the echo is a videoke room, the wait is a PLDT
loading screen.

## Randomisation

Nothing about a turn should be predictable. The same opener, the same pause, or the same stall
order twice in a row is exactly what makes canned audio read as canned.

- **Opener** — drawn uniformly from the warm lines for the turn's language, excluding any already
  spoken in this conversation, and excluding the previous turn's line even after that history is
  cleared.
- **Stalls** — the language's list is *shuffled once per turn* and consumed in that order,
  refilling when exhausted, and never re-opening with the line that just played. A plain per-gap
  random draw repeats far more often than it feels like it should; shuffling is what makes a long
  wait cycle through varied phrases.
- **Timing** — the pre-opener delay and every inter-stall gap are drawn fresh from
  `FILLER_DELAY_JITTER_S` and `FILLER_STALL_GAP_JITTER_S`. A constant delay makes the opener
  land like a timer going off, and the ear picks that up immediately. Draws happen once per
  gap, never mid-playback.

This mirrors how `config/thinking.py:37-52` jitters the pan sweep, and for the same reason:
a fixed value is recognisably mechanical.

## Nothing repeats inside one conversation

The shuffle only ever covered *one turn*. The queue is rebuilt on every entry to BUSY, and a
fresh shuffle knows nothing about the last one's — so "Sandali ha" could comfortably open the
stalls of three turns running, and the opener guard only ever excluded the line immediately
before it. A 12-line bank sounded roughly half its size.

`_filler_used_openers` / `_filler_used_stalls` carry the promise across turns, and
`_begin_session` clears them, so **the span is one conversation**: wake, talk for as many turns
as you like, and no line comes back; greet Kai again and the whole bank is available.

The scope is deliberately neither narrower nor wider. Per-turn is what it already was, and is
what allowed the repeat. Never-resetting would exhaust the 12 Tagalog stalls in one long demo
and spend the rest of it on the fallback path. A conversation is the span over which one
listener would actually notice.

The rule is a **preference, not a promise**: a conversation can outlast the bank, and refusing
to repeat once everything has been heard would trade a small tell for the exact dead air the
module exists to prevent. An exhausted set quietly starts a second lap. The *warm* filter is not
relaxed that way — an uncached line plays nothing at all, so selecting one would put a gap
exactly where the ceiling forbids it.

### What the second lap costs, and the three guards on it

Heard on the robot, 2026-08-09: **the same stall twice inside one exchange**. Not a selection bug
on its own — three things had to line up, and each is now guarded separately.

1. **The lap boundary was unguarded.** `pick_opener` has always excluded the previous line;
   `stall_queue` had no equivalent, and the session pops from the *end* of the shuffled list. So
   when a lap ended, the shuffle was free to put the line still ringing in the room next in the
   queue's mouth — a 1-in-N back-to-back repeat at every boundary. `stall_queue(avoid=…)` now
   rotates that key to the front instead. Rotated, not dropped: dropping would cost the lap a
   line to protect the one gap it is least likely to be heard in.
2. **One `used` set collapsed too early.** It is conversation-scoped, so from turn two of a
   conversation that had been through the bank it was permanently full — every turn started on
   the fallback lap with no preference left at all. The soft filter is now a **ladder**:
   conversation-spent first, then `_filler_turn_stalls` (cleared by `_arm_filler`), then the full
   bank. The weaker promise that survives an exhausted conversation is *nothing twice inside one
   wait*, which is where the ear actually catches it.
3. **The pools were too small to lap slowly.** `ceb` and `en` held four stalls each against a
   wait that spends three or four. Both are now ten. No amount of selection logic fixes a pool
   that a single wait can drain — the guards above only choose *which* line repeats.

The length cap makes this worse invisibly: `_within_length_cap` drops any stall over
`FILLER_MAX_STALL_S` at prewarm, so a written pool of ten can reach the robot as a pool of six.
`_prewarm_bank` therefore prints the surviving pool **per language and per tier**, not just the
bank total — `filler bank: 41/52 lines cached (pass 1, +41) [tl 12op/12st, ceb 4op/7st, en 4op/6st]`.
The total was never the number that governs repeats.

## The ceiling and the floor are both enforced

`FILLER_MAX_SILENCE_S = 2.0` is the hard upper bound. Both jitter ranges have upper bounds that
fit under it *after* adding `FILLER_PLAYBACK_START_BUDGET_S` — the reservation for the gap
between deciding to speak and the first audible sample. That gap is where a ceiling like this
normally leaks, so it is budgeted explicitly rather than hoped away.

`FILLER_MIN_GAP_S = 1.0` is the lower bound, and it exists because the ceiling alone is only
half a contract. Left to itself the ceiling pushes every gap toward zero — the safest way to
never be quiet too long is to never be quiet — and filler that comes back the instant the last
line stops sounds like a queue draining rather than like thinking, with no room for the real
answer to land in a gap instead of on top of a line. A person stalling leaves a beat.

Both are applied in one place, `session._filler_gap`, so the opener delay and the stall gap
cannot drift apart. **The ceiling wins if they ever conflict**: dead air is the failure this
module was built for, and a floor raised past the ceiling would be one config edit silently
breaking the promise the ceiling exists to keep.

`tests/test_filler.py` asserts the worst-case draw plus the playback reservation stays under the
ceiling and that neither range starts below the floor; `TestFiller` walks a real turn at 20 Hz
and measures the gaps from both sides. A later tuning edit cannot quietly break either number.

## Writing rules

These are not style preferences. They are what keeps the audio intelligible, and every one of
them has a failure that is **inaudible from the string itself**:

- **ASCII only.** No em dashes, no smart quotes, no ellipses — Piper's phonemizer (espeak-ng)
  does unpredictable things with unusual punctuation.
- **No long repeated-letter runs.** espeak-ng reads them as initialisms and spells them out:
  `"Hmmmm..."` came back from Whisper as "H-A-M-A-M-M" (`config/thinking.py:72-86`).
- **Write brand names the way they should be spoken.** "NMBLR dot AI", not "NMBLR.AI".
- **Openers are 1–2 sentences**, always *problem → fact*. **Stalls carry no facts**, nothing
  over ~1.2 s spoken.

`tests/test_filler.py` mechanically enforces what it can: ASCII, no `...` or `--`, no
three-in-a-row letter runs, no dotted brand names, sentence counts, stall word counts.

**Every line must still be synthesised through Piper and transcribed back with Whisper before
shipping.** That is the only check that catches a line that reads fine and sounds wrong. Re-run
it whenever a line changes.

## Where things live

| File | Holds |
|---|---|
| `config/filler.py` | the 52 lines, the language keys, the timing constants, the switches |
| `ai/filler.py` | the choosing — pure, no audio, no clock, injected `Random` |
| `ai/session.py` | `_arm_filler` / `_tick_filler`, and the bank in `_canned_lines` |
| `tests/test_filler.py` | bank shape, speakability, the silence ceiling, selection |
| `tests/test_session.py::TestFiller` | the playback loop against a fake clock |
| `scripts/filler_check.py` | the on-robot synthesise-then-transcribe check |
| `documents/kai_facts.txt` | the facts the openers cite, so follow-up questions are answerable |

The filler lines deliberately do **not** live in `documents/` — that directory is the RAG
corpus, and indexing conversational filler would pollute retrieval.

The facts embedded in the openers are the same ones in `documents/kai_facts.txt`, so a listener
who hears "there's an NVIDIA Jetson Orin Nano behind my face" and asks a follow-up gets a
consistent answer out of RAG rather than a contradiction. If an opener's fact changes, change
the corresponding line in `kai_facts.txt` and rebuild the index:

```sh
python3 -m ai.index_documents          # from the repo root, as a module
python3 scripts/rag_accuracy.py        # the new facts compete with the DEVCON corpus
```

## How it runs

**No contract was broken to wire this in.** `tts.prewarm_canned` takes `{key: text}` and writes
one WAV per key; `ai/filler.canned_lines()` simply presents the whole bank in that shape, with
keys like `filler_op_tl_3` and `filler_st_ceb_1`. `session._canned_lines()` merges it after the
four core lines, and everything downstream — prewarming, the re-warm-on-voice-change retry, the
live-synthesis fallback on a cache miss — works unchanged.

Ordering matters and is deliberate: the four core lines come **first**. `prewarm_canned`
synthesises in iteration order, TTS has a single synth slot, and a turn arriving mid-prewarm
cancels the rest. Core-first means a cancellation costs filler variety rather than `"Yes?"`.

The loop lives in `session._tick_filler`, called from the BUSY branch of the 20 Hz tick:

```
elapsed >= drawn delay   -> opener, once, language latched here
playback ends            -> draw a gap, arm the next stall
now >= armed time        -> one stall, disarm, wait for playback to end again
```

It is written against playback state rather than a precomputed schedule, because the contract is
about *silence* — the gap between one line ending and the next beginning. A schedule would drift
the moment a line synthesised long or came back at a different length in a different voice;
reading playback state measures the gap where it actually is.

Specifically `voice.speech_in_flight()`, **not** `tts.is_playing()`. `is_playing()` only goes true
once a playback *process* exists, and `_speak_wav` hands off to a worker thread first — at 20 Hz
that leaves several ticks where a line has been started but is not yet "playing", and every one of
them cheerfully started another. That was heard on the robot as fillers talking over each other.
`speech_in_flight()` covers the whole span from before synthesis to end of playback, which is
exactly the window in which nothing else may start.

Nothing in the loop ends the turn. The real reply arriving calls `tts.stop()` and leaves BUSY,
stranding whatever is playing mid-word. That is the intent — stalls are short precisely so being
cut off reads as natural.

**Language** comes from `voice.last_language()`, which reports the *previous* utterance's Whisper
label. The opener fires ~1-1.6 s into a turn, often before STT has finished, so a fresh label
frequently does not exist yet; in a conversation the previous turn's is almost always right, and
being one turn stale is far cheaper than blocking on STT. It is latched at the opener and reused
for every stall, so a turn cannot switch language halfway through.

### Cost, and the switches

Prewarming is 56 Piper runs instead of 4, but **not in one burst and not all at startup**. The
four core lines go first and fast; the 52 bank lines then warm one at a time on the same thread,
each gated on nothing else speaking, over minutes rather than seconds. The burst version is what
put a second Piper on the CPU beside a live reply, made `tts.stop()` kill the wrong process, and
cost a turn 24.6 s to first audio — see `docs/filler-responses-wip.md`.

So the bank is *not* fully warm for the first minutes after boot, and that is expected. **A line
with no WAV is silent, never synthesised on demand**: selection only ever draws from warm keys, and
if none are warm the turn falls back to the "Hmm". Live synthesis here would write the same fixed
`_RAW_WAV` / `_OUTPUT_WAV` the reply is about to, and would spend 0.5-1.5 s of the CPU the reply is
competing for — lengthening the wait it was covering. The cost is paid again on a dashboard voice
or volume change.

| Switch | Effect |
|---|---|
| **Think out loud** (dashboard) | gates the filler *and* the "hmm" — to a listener this is the thinking sound, just a talking one |
| `FILLER_ENABLED` | off falls back to the single "Hmm.", i.e. the behaviour before the bank existed |
| `FILLER_PREWARM` | off leaves the bank cold, which means **silent**, not synthesised on demand |
| `FILLER_CEB_SHARE` | 0.0 switches the Bisaya bank off entirely |
| `FILLER_MIN_GAP_S` / `FILLER_MAX_SILENCE_S` | the floor and ceiling every gap is clamped into |

### Why Bisaya needs its own route

`config/voice.WHISPER_LANGUAGES` is `("en", "tl")`. There is no `ceb` label, so detection can
**never** select the Bisaya bank on its own — those 8 lines would be synthesised at startup and
never played. So a Tagalog turn draws from the Bisaya bank `FILLER_CEB_SHARE` of the time
(`ai/filler.pick_lang`). That is a product choice, not an inference about who is speaking: the
room is Philippine, Bisaya reads as playful rather than wrong to a Tagalog listener, and a
filler line is the lowest-stakes place in the system to mix them. English turns are unaffected.

## Testing

`tests/test_filler.py` covers the bank and the pure selection logic — counts, ASCII, letter
runs, key uniqueness, no back-to-back opener repeat, the shuffled stall queue, and that Bisaya
is reachable while Tagalog stays the majority. `tests/test_session.py::TestFiller` covers the
loop against a fake clock: the head-start silence, exactly one opener per turn, stalls
continuing through a long wait, never starting a line over one already playing, the language
latch, per-turn re-arming, and — the one that matters — **no gap exceeding the ceiling**, walked
at the real tick rate with playback ending instantly, which is the worst case.

`TestThinkingSound` now runs with `FILLER_ENABLED` patched off: it describes the fallback path,
which is what the "hmm" became.

None of that hears anything. Run `python3 -m scripts.filler_check` **on the robot** to
synthesise every line through Piper, transcribe it back with Whisper, and flag lines whose audio
does not match their text or stalls that run past ~1.2 s. Review flags by ear before rewriting —
a Bisaya line can be perfectly intelligible audio that Whisper simply cannot transcribe.
