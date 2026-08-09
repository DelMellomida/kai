# S4 — TTS is module-global state with fixed shared output paths

| | |
|---|---|
| **Tier** | 2 |
| **Severity** | Medium |
| **Effort** | Medium |
| **Confidence** | Medium-High |
| **Lens** | Software |

## Location

- `ai/tts.py` — module globals `_proc_lock`, `_synth_proc`, `_current_proc`, `_last_end`,
  `_profile_applied`; module constants `_RAW_WAV = /tmp/kai_tts_raw.wav`,
  `_OUTPUT_WAV = /tmp/kai_tts.wav`; `synthesize()`, `play()`, `stop()`, `is_playing()`, `quiet_since()`
- `ai/voice_assistant.py` — `_speak()`, `_begin_speech()`, `_end_speech()`, `speak_wav()`,
  `speak_text()`, `_speech_gen`, `_tts_active`
- `ai/session.py` — `_speak_filler()`, `_speak_canned()`, `_quiet_for_synth()`, `_warm_one()`,
  `_prewarm_bank()`, `_rewarm_when_quiet()`, `_speak_greeting()`

## Problem

`ai/tts.py` is a module, not an object: one synth handle, one playback handle, one "last ended"
timestamp, and two fixed output paths shared by every reply in the process. The docstring is honest
about it ("Not thread-managed beyond a single 'current playback' handle"), but the consequences have
propagated outward into every caller as workarounds rather than staying contained:

- `session._speak_filler()` must refuse live synthesis entirely, because a filler synthesising during
  `BUSY` writes the same `_RAW_WAV`/`_OUTPUT_WAV` the reply is about to — the documented 2026-08-07
  incident, where the reply thread died with `EOFError` inside `wave.open`, sox reported "RIFF header
  not found", and the turn took 24.6 s to first audio.
- `session._prewarm_bank()` needs `_quiet_for_synth()` polling and a three-outcome retry taxonomy
  (`cached`/`retry`/`skip`) purely because `stop()` kills whatever is in the single `_synth_proc`
  slot, including a background prewarm.
- `session._rewarm_when_quiet()` needs a 20 s quiet wait plus a verify-and-retry pass for the same
  reason.
- `voice_assistant._begin_speech()` needs a generation token because one `_tts_active` boolean is
  shared by four speech paths (reply, ack, canned, filler) whose workers finish out of order — the
  2026-08-09 incident where a 7.5 s filler opener's worker cleared the flag out from under a live
  reply and everything talked at once.

Each of those fixes is individually correct. Collectively they are a large mitigation surface
around a constraint that could be removed instead.

## Why it matters

The invariant "only one thing may synthesise or play at a time, and the previous output file is
invalid the moment the next synthesis starts" is enforced by convention at every call site. It works
today because each site remembers the rule and documents it. The next speech path added will not,
and the failure is not an exception — it is a corrupted WAV and a garbled or silent reply, which
presents as "the speaker is broken."

## Acceptance criteria

- [ ] Each synthesis writes to a path unique to that utterance (keyed on the speech generation
      counter), so two concurrent synths can never write the same file.
- [ ] Transient synthesis output is cleaned up after playback (or on the next claim), and a bounded
      number of stale files is enforced so `/tmp` cannot grow without limit across a long run.
- [ ] Cached/prewarmed lines (`synthesize_to`, `prewarm_canned`) continue to write stable,
      predictable paths under `ACK_WAV_DIR` — this ticket changes only the *transient* reply path.
- [ ] `session._speak_filler()` no longer needs to refuse live synthesis for collision reasons.
      (Whether it *should* synthesise live remains a separate product decision — the CPU-cost
      argument in its docstring still stands and should be preserved as the reason, with the
      corruption argument removed once it no longer applies.)
- [ ] `_prewarm_bank()` no longer requires `_quiet_for_synth()` polling to avoid corrupting a live
      reply. Pacing to avoid CPU contention may stay; the correctness gate goes.
- [ ] `stop()` still cancels both the synth and the playback of the **current** utterance, and does
      not cancel a background prewarm that belongs to a different generation.
- [ ] `is_playing()` / `quiet_since()` / the `TTS_TAIL_MUTE_S` mute gate behave identically — the
      self-hearing gate is built on them and must not regress. Verified by a hands-free session with
      no self-triggered wakes across at least 20 turns.
- [ ] All existing `tests/test_tts.py` cases pass, including the "newer handle" contract that
      depends on `play()` reading stderr after `wait()` rather than via `communicate()`.
- [ ] `tests/test_session.py`'s filler and prewarm cases pass, with any that assert the *workaround*
      behaviour rewritten to assert the underlying guarantee.

## Suggested approach

Two steps, and the first delivers most of the value:

**1. Per-utterance paths (small, high value).** Thread the speech generation token from
`VoiceAssistant._begin_speech()` into `tts.synthesize()`, and have it write
`{TTS_OUTPUT_DIR}/kai_tts_{gen}_raw.wav` → `{TTS_OUTPUT_DIR}/kai_tts_{gen}.wav`. Delete both once
playback finishes or the epoch goes stale. This alone removes the file-collision class outright,
which is the half that produced corrupted audio.

**2. Encapsulate the handles (medium).** Introduce a `Speaker` object owning `_synth_proc`,
`_current_proc`, `_last_end` and `_profile_applied`, constructed once by `VoiceAssistant` and passed
where needed. `ai/tts.py` keeps the pure functions (`clean_for_speech`, `clamp_for_speech`,
`wav_duration`, `synthesize_to`, `prewarm_canned`, the Piper/sox invocation) and loses the mutable
state. Module-level `stop()`/`is_playing()`/`quiet_since()` can remain as thin delegates to a
process-wide default instance during migration, so `ai/session.py`'s many call sites need not all
change at once.

Sequencing note: this ticket and **R5** (streaming synthesis) touch the same code, and R5 *requires*
per-utterance paths — it synthesises several fragments per reply, which is impossible against two
fixed filenames. Do step 1 of this ticket before starting R5, and treat step 2 as optional cleanup
that R5 will make easier either way.
