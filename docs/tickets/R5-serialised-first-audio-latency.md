# R5 — First-audio latency is serialised end to end

| | |
|---|---|
| **Tier** | 3 |
| **Severity** | High |
| **Effort** | Large |
| **Confidence** | High |
| **Lens** | Robotics |

## Location

- `ai/llm.py` — `_ollama_request()`, `"stream": False`
- `ai/voice_assistant.py` — `_process()` (STT → RAG → LLM → `_speak`), `_call_ollama()`,
  `_speak()`'s `_worker()` (synthesise whole reply → measure → play)
- `ai/tts.py` — `synthesize()`, `play()`, `_RAW_WAV`/`_OUTPUT_WAV`
- `ai/speak_envelope.py` — `_speak_segments_for_duration()` (the jaw schedule, built from one
  known total duration)
- The whole latency-masking subsystem this exists to justify: `config/filler.py`, `ai/filler.py`,
  `ai/session.py`'s `_arm_filler` / `_tick_filler` / `_speak_filler` / `_prewarm_bank`

## Problem

A turn runs strictly in series, and nothing starts until the stage before it has fully finished:

| stage | measured / configured |
|---|---|
| STT (`base`, int8, beam 1, `vad_filter`) | ~1.3–2.4 s |
| RAG (embed + rank + format) | tens of ms to low hundreds |
| LLM (gemma2:2b, ~27 tok/s, `OLLAMA_NUM_PREDICT = 160`) | up to ~6 s |
| Piper synthesis of the **entire** reply (~0.42× realtime) | proportional to reply length |
| paplay start | small |

`sess_last_first_audio_ms` exists precisely because that sum is large. The LLM call is a blocking
non-streaming POST — `ai/session.py` even documents a consequence of that ("the Ollama call is a
blocking non-streaming POST and is not cancellable, so accepting [a barge-in wake] would mean either
queueing or abandoning a reply that arrives anyway"). Synthesis then waits for the last token before
producing the first sample.

## Why it matters

This is the dominant user-visible defect and the root cause of a large amount of the system's
complexity. The filler-bank subsystem — `config/filler.py`, `ai/filler.py`, roughly 250 lines of
session code, eight instance fields, a multi-minute startup prewarm, a length cap, three
"already used" sets, and a documented history of overlap and repeat bugs — exists to *mask* this
wait rather than shorten it. `FILLER_MAX_SILENCE_S` is a promise about how long a listener is left
in silence; streaming would attack the silence itself.

Secondary consequences: Kai is deaf while speaking (no echo cancellation, barge-in off), so a long
reply is a long deaf spell, which is why `TTS_MAX_SPOKEN_CHARS` and `OLLAMA_NUM_PREDICT` both exist
as caps. Shorter time-to-first-audio does not fix deafness, but it does make the whole turn shorter.

## Why it is Large

It touches the three most carefully-reasoned contracts in the codebase, each with recorded incident
history:

- the **epoch** mechanism (`_epoch_ok`, `bump_epoch`) that drops abandoned work,
- the **speech generation** mechanism (`_begin_speech`/`_end_speech`, `_speech_gen`, `_tts_active`)
  that decides who owns the speaker and who may clear the in-flight flag,
- the **jaw envelope**, which currently receives one known total duration.

None of them survive unchanged when a single reply becomes N sequentially-played fragments.

## Acceptance criteria

- [ ] Ollama is called with `"stream": True` and the response is consumed incrementally.
- [ ] Generated text is split into speakable fragments on sentence boundaries as it arrives, with a
      minimum fragment length so the first fragment is not a two-word stub, and a flush of whatever
      remains at end-of-stream.
- [ ] Synthesis of fragment *n* overlaps generation of fragment *n+1*; playback of fragment *n*
      overlaps synthesis of *n+1*. Fragments play in order, gapless enough that a listener hears one
      reply, not a list.
- [ ] `sess_last_first_audio_ms` drops substantially on a representative set of turns — record
      before/after medians for a short reply, a long reply and a RAG-heavy reply. (The review's
      estimate is 2–4×; the acceptance bar is the measurement being taken and recorded, not a
      specific ratio.)
- [ ] Ollama's own timing counters (`prompt_eval_*`, `eval_*`, `load_duration`) are still captured
      and logged from the streamed response — `_log_llm_timings` currently reads them from the final
      non-streamed body, and they arrive on the terminal chunk when streaming.
- [ ] **Epoch safety holds**: a session ending mid-reply stops the stream, cancels any in-flight
      synthesis, and plays no further fragments. No fragment from a stale epoch ever reaches the
      speaker. The existing "reply dropped after synthesis / before playback" log lines have
      equivalents at fragment granularity.
- [ ] **Speaker ownership holds**: `speech_in_flight()` is true continuously from before the first
      fragment's synthesis until the last fragment's playback ends — it must not flicker false
      between fragments, or `ai/session.py`'s `STATE_SPEAKING` drops to `COOLDOWN` mid-answer and the
      filler loop starts talking over it (the 2026-08-09 failure, recorded in `_speech_gen`'s comment).
- [ ] **The mute gate holds**: `_gate_until` covers the whole multi-fragment reply plus
      `TTS_TAIL_MUTE_S`, so Kai never hears itself. Verified over ≥20 hands-free turns with no
      self-triggered wake.
- [ ] The jaw animates continuously across the whole reply — no closed-mouth gap at fragment
      boundaries beyond the normal `SPEAK_GAP_S` between sentences.
- [ ] The dashboard still receives the complete reply text (`voice_response`) and posts exactly one
      chat bubble per turn — `voice_turn_id` increments once, not per fragment.
- [ ] `TTS_MAX_SPOKEN_CHARS` clamping and `ai/delivery.shape()` still apply. Note that both
      currently operate on the whole reply at once; decide and document whether shaping runs per
      fragment (cheaper, but the opener heuristic and the "words since last break" counter need the
      sentence context) or on a buffered whole.
- [ ] A failure mid-stream (Ollama drops the connection after 2 of 4 sentences) degrades honestly:
      what was generated is spoken, the error is logged, and the turn reports `error` rather than
      silently truncating.
- [ ] Once landed, the filler bank is re-evaluated against the new latency — `FILLER_DELAY_JITTER_S`
      and the opener/stall structure were sized for a 5–10 s wait, and a large fraction of the bank
      may become dead code. Removing it is out of scope for this ticket; measuring whether it should
      be is not.

## Suggested approach

Sequenced, because the prerequisites are real:

**Prerequisite — S4 step 1.** Per-utterance WAV paths. Streaming synthesises several fragments per
reply, which is impossible against the two fixed filenames `_RAW_WAV`/`_OUTPUT_WAV`. Do that first.

**Step 1 — streaming client.** Add `_ollama_stream(messages) -> Iterator[str]` alongside
`_ollama_request` in `ai/llm.py`, consuming Ollama's newline-delimited JSON chunks
(`message.content` per chunk, `done: true` on the terminal chunk carrying the timing fields). Keep
`_ollama_request` — `ensure_llm_warm()` and any non-interactive caller are happier with it, and it
is the rollback.

**Step 2 — a fragment pipeline.** A small bounded queue between three roles:

- *producer*: consumes the token stream, accumulates into a sentence buffer, emits a fragment when
  it sees a terminator and the buffer exceeds a minimum length;
- *synthesiser*: one worker, takes fragments in order, calls `tts.synthesize()`, pushes WAV paths;
- *player*: one worker, plays WAV paths in order, sets the jaw envelope per fragment from that
  fragment's measured duration.

One synthesiser and one player, both single-threaded, keeps ordering trivial and keeps CPU
contention with STT/MediaPipe bounded — parallel Piper runs are exactly what the 2026-08-07 incident
was.

**Step 3 — rework the ownership flags.** `_begin_speech()` claims the speaker once for the *whole
reply*, not per fragment, and `_end_speech(gen)` fires only when the player drains. That is what
keeps `speech_in_flight()` continuous. `_gate_until` extends as each fragment's duration becomes
known.

**Step 4 — the jaw.** `_speak_segments_for_duration()` already builds a schedule from a known
duration; call it per fragment as that fragment starts playing. The visual result is equivalent
because it is already per-sentence.

**Rollback.** Keep the whole path behind a `config/voice.py` flag (e.g. `LLM_STREAMING = True`) that
reverts to today's `_ollama_request` + whole-reply `synthesize()`, in the style of
`RAG_CONTEXT_PLACEMENT`'s documented REVERT. This is the largest behavioural change in the system
and it needs a one-line way back.
