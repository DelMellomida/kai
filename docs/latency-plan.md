# Reply-latency plan — "Gemma feels slow"

Goal: cut the time between the end of an utterance and the **first sound out of the speaker**,
without changing what Kai says or how the working pipeline is shaped. Every step below is additive
and individually revertible; nothing here removes a working path.

---

## 0. What the turn actually costs today

The turn is strictly serial, and two of its stages are whole-reply blocking:

```
utterance ends
  └─ Whisper base transcribe                     ai/voice_assistant.py::_transcribe
  └─ RAG: embed query + cosine over index        ai/rag.py::retrieve_context
  └─ Ollama POST, stream=False  ← WAITS FOR THE ENTIRE REPLY
  └─ Piper synth of the ENTIRE reply (~0.42x realtime)
  └─ sox post-process of the whole WAV
  └─ paplay ──────────────────────────────────── first sound
```

So time-to-first-audio = `STT + RAG + full generation + full synthesis + sox`. On a 3-sentence
reply the last two stages alone are most of the wait, and **none of it overlaps**.

### The measurement problem comes first

There is currently no way to tell which stage is slow:

- `ai/session.py:919` — `_timings["llm_ms"] = now - self._turn_started`. That is STT **plus** RAG
  **plus** the LLM, labelled "llm".
- `ai/session.py:494` — `_timings = {"stt_ms": 0, ...}`. `stt_ms` is never assigned anywhere.
  `sess_last_stt_ms` on the dashboard has always been 0.
- `ai/voice_assistant.py:1087` — the Ollama response's `prompt_eval_count`,
  `prompt_eval_duration`, `eval_count`, `eval_duration` and `load_duration` are discarded. Those
  five numbers distinguish "prompt eval is slow" from "token generation is slow" from "the model
  got placed on CPU again" — the three causes with completely different fixes.

**Do step 1 before any tuning.** Otherwise every change below is guesswork.

---

## Step 1 — Instrument the turn (no behaviour change) — **DONE**

Zero risk, and it decides the priority of everything after it.

1. In `_ollama_request`, return the parsed JSON (or a small dataclass) instead of the bare
   `Response`, and log one line per turn:
   ```
   [llm] prompt 612 tok in 480ms (1275 tok/s) | gen 44 tok in 1630ms (27 tok/s) | load 0ms
   ```
   All derived from fields Ollama already sends. `load_duration > 0` means the model was
   re-loaded — that alone is ~48 s and explains any "it hung" report.
2. Split `session._timings` into `stt_ms`, `rag_ms`, `llm_ms`, `tts_synth_ms`, and
   `first_audio_ms` (utterance-end → paplay start). Assign `stt_ms` — it is currently dead.
3. Surface the new keys next to the existing ones in the status dict (`ai/session.py:1321`).

**Revert:** delete the log line and the extra dict keys. Nothing else reads them.

**Decision rule after one session of real turns:**
- generation `tok/s` well under ~27 → the model is CPU-split → go to **step 5**
- `prompt_eval_duration` large / prompt token count high → go to **steps 3 and 4**
- both fine but it still *feels* slow → the wait is synthesis, not Gemma → **step 2** is the whole fix

---

## Step 2 — Stream generation and pipeline TTS per sentence  ← biggest win

Today Piper cannot start until the last token is generated, and paplay cannot start until Piper
has finished the whole reply. Both waits are avoidable.

Change `ai/voice_assistant.py::_ollama_request` to `"stream": True`, accumulate the token deltas,
and emit each **complete sentence** to a speak queue as it closes. Piper synthesizes sentence *N+1*
while paplay is still playing sentence *N*.

Time-to-first-audio goes from `(whole generation + whole synthesis)` to
`(first sentence generated + first sentence synthesized)` — for a 3-sentence reply that is the
majority of the current wait, and it costs nothing in quality because the text is identical.

This is the most invasive change in the document, so it needs care in five places:

- **Jaw sync.** `_speak_segments_for_duration` currently takes one WAV duration for the whole
  reply. It needs to be applied per chunk as each WAV is measured. The per-sentence structure it
  already builds makes this a natural fit rather than a rewrite.
- **Self-hearing gate.** `tts.quiet_since()` / `_last_end` and `session._gate_until` must reflect
  the end of the *queue*, not the end of the first chunk — otherwise the mic reopens mid-reply and
  Kai answers himself. This is the failure mode to test hardest.
- **`tts.stop()`.** Must drain the pending queue as well as killing the in-flight synth and
  playback, or an abandoned turn keeps talking into the next session.
- **Fixed WAV paths.** `_RAW_WAV` / `_OUTPUT_WAV` are shared constants; chunked synthesis needs
  per-chunk paths (`kai_tts_00.wav`, …) or chunk N+1 overwrites chunk N mid-playback.
- **Epoch checks.** Currently checked twice in `_speak`'s worker; they need to be checked per
  chunk instead.
- **`clamp_for_speech` / `TTS_MAX_SPOKEN_CHARS`.** Becomes a running character budget across
  chunks rather than one clamp on a finished string.

**Guard it behind a flag.** Add `OLLAMA_STREAM = False` and `TTS_STREAM_SENTENCES = False` to
`config/voice.py`, keep the existing whole-reply path intact underneath, and flip the flags on
only after the self-hearing gate has been verified in the room. Reverting is one line, and the old
path is still the one the tests exercise.

**Bonus:** streaming also makes the `THINKING_SOUND_DELAY_S = 0.6` "Hmm" filler mostly unnecessary
— real speech arrives before the filler would.

---

## Step 3 — Stop invalidating Ollama's prompt cache every turn — **DONE**

`ai/voice_assistant.py:1084`:

```python
system_prompt = f"{persona}\n\n{context}" if context else persona
messages = build_chat_messages(system_prompt, history, text)
```

The RAG context sits **inside the system message**, i.e. at the very front of the prompt — and it
changes on every turn. llama.cpp reuses the KV cache only for the longest common *prefix*, so a
changed system message forces a full re-evaluation of the persona **and all 6 turns of history**,
every single turn. With `TOP_K = 3` × `CHUNK_SIZE_CHARS = 800` that is a substantial prompt-eval
bill paid repeatedly for text that did not change.

**Fix:** move the retrieved context out of the system message and prepend it to the *final user
message content* instead. The prefix (persona + history) then stays byte-identical between turns
and is cached; only the new context + question is evaluated.

```python
system_prompt = persona
user_content  = f"{context}\n\n{text}" if context else text
messages = build_chat_messages(system_prompt, history, user_content)
```

Two things make this safe here:

- `self._history` stores the **raw** transcript for user turns (`ai/voice_assistant.py:996`), so
  the injected context never pollutes the stored history — the prefix genuinely stays stable.
- Gemma2's chat template alternates user/assistant strictly, so an extra mid-conversation system
  message is not representable anyway. Prepending to the user turn is the correct shape.

**Risk to watch:** `ai/rag.py::format_context`'s header exists to make retrieved text
*authoritative* — that was tuned with the block in the system position. Moving it to the user turn
could weaken that. Verify with the known DEVCON questions before keeping it; if answers start
drifting back to pretraining, revert this step and take the prompt-eval cost.

**Revert:** two lines.

---

## Step 4 — Shrink what has to be evaluated

Only worth doing after step 1 shows prompt eval is actually significant.

- **`TOP_K = 3` → `2`** (`config/rag.py:170`). Cuts the per-turn context roughly by a third. The
  fallback layers in `retrieve_context` are unchanged, so nothing loses its safety net.
- **Cap chunk text at injection time.** `CHUNK_SIZE_CHARS = 800` is right for *retrieval* quality
  but the whole 800 chars go into the prompt. Trimming each injected chunk to the sentences around
  the match keeps recall and cuts tokens. More work than the `TOP_K` knob — do it only if that one
  is not enough.
- **`MAX_HISTORY_TURNS = 6`.** Leave it. It was raised from 3 for a real reason (Kai forgot the
  opening question and answered confidently anyway), and with step 3 in place the history is
  cached and nearly free. Do not undo a correctness fix for latency.
- **Persona:** `persona.txt` asks for 1–3 sentences, measured replies run 36–52 tokens against
  `OLLAMA_NUM_PREDICT = 96`. Tightening to "1–2 sentences" saves generation time *and* synthesis
  time on every turn. Cheapest possible change, but it is a quality trade — a dashboard-visible
  A/B, not a silent edit.

---

## Step 5 — Make the CPU/GPU split visible and less likely — **part 1 DONE**

`config/voice.py` already documents the failure: Ollama picks CPU-vs-GPU placement at **load
time**, `OLLAMA_KEEP_ALIVE = -1` pins that choice for the process lifetime, and GPU fragmentation
after hours of uptime silently produces a 45/55 split that is roughly half speed. That is very
likely what "sometimes it takes way too long" actually is — and today it is invisible.

1. **Log the placement at startup and on every prewarm.** Poll `GET /api/ps` after
   `prewarm_ollama` and log `size_vram` vs `size`. A partial offload becomes one obvious warning
   line instead of a mystery. Pure diagnostics, zero risk.
2. **Enable flash attention + a quantized KV cache** on the Ollama service:
   ```
   OLLAMA_FLASH_ATTENTION=1
   OLLAMA_KV_CACHE_TYPE=q8_0
   ```
   `q8_0` roughly halves KV-cache memory, which is exactly the headroom that decides whether the
   model fits the iGPU alongside the camera. Environment-only change to the systemd unit —
   revertible without touching the repo. Re-measure `tok/s` from step 1 either side of it.
3. **If step 1 shows a partial offload is recurring**, the documented escape hatch is already
   written down: free GPU headroom by dropping the desktop GUI
   (`systemctl set-default multi-user.target`), or restart Ollama to defragment before an event.

---

## Explicitly not doing

- **Swapping the model** (e.g. `qwen2.5:1.5b`, `llama3.2:1b`). `gemma2:2b` was chosen deliberately
  to fit the camera in 8 GB, and the persona and RAG headers are tuned against it. Revisit only if
  steps 1–5 leave generation itself as the bottleneck.
- **Lowering `OLLAMA_NUM_CTX` back to 1024.** Measured: a 6-turn session peaked at 959 of 1024
  tokens, and Ollama silently drops history to fit. That is a correctness regression for ~35 MB.
- **Touching Whisper further.** `base` + `beam_size=1` + capped threads is already the tuned
  result of a measured pass; the remaining STT time is not where the win is.

---

## Order of work

| # | Change | Risk | Revert | Status |
|---|--------|------|--------|--------|
| 1 | Split timings + log Ollama's own counters | none | delete lines | **done** |
| 3 | Move RAG context into the user turn | low (verify answer quality) | `RAG_CONTEXT_PLACEMENT = "system"` | **done** |
| 5a | `/api/ps` placement logging at warm-up | none | delete call | **done** |
| 5b | flash-attn + `q8_0` KV on the Ollama service | low | env vars | **not started** (on-device) |
| 2 | Stream generation → per-sentence TTS pipeline | **medium** | config flags | **not started** |
| 4 | `TOP_K` 3→2, persona tightening | low (quality trade) | config edits | **not started** |

Steps 1, 3 and 5a are cheap and independent — they landed together. Step 2 is the largest win and
the largest change; it should land on its own, behind its flags, with the self-hearing gate tested
deliberately. Step 4 is last because it trades answer quality for time and should only be spent if
the measurements say it is needed.

---

## What landed, and what to do with it

`ai/voice_assistant.py`
- `_ollama_request` returns the parsed body instead of the `Response`, so the timing fields are
  read rather than discarded.
- `_log_llm_timings` prints one `[llm]` line per turn (prompt tok + ms + tok/s, generation tok + ms
  + tok/s) and a separate `[llm] MODEL RELOADED` line when `load_duration` fires. Tolerant of a
  response with no timing fields at all — it must never be able to cost a reply.
- `log_model_placement()` reads `/api/ps` after warm-up and says whether the model is fully on the
  GPU or only partly, with the fix (restart/reboot to defragment) in the warning itself.
- `_stage_ms` + `stage_timings()` hold the per-stage split; `_process` measures STT and threads
  `turn_t0` into `_speak` so `first_audio_ms` covers the whole pipeline. One `[turn]` line per reply.
- `_call_ollama` puts the RAG context in the user turn (step 3).

`ai/session.py` — `sess_last_stt_ms` is populated for the first time (it was hardcoded 0);
`sess_last_llm_ms` now means the LLM alone; the old combined number moved to `sess_last_turn_ms`;
`sess_last_rag_ms`, `sess_last_llm_prompt_ms`, `sess_last_llm_gen_ms`, `sess_last_llm_tok_s`,
`sess_last_tts_synth_ms` and `sess_last_first_audio_ms` are new.

`config/voice.py` — `OLLAMA_PS_TIMEOUT_S`, `OLLAMA_LOG_TIMINGS`, `RAG_CONTEXT_PLACEMENT`.

### Next action: read the log for a handful of real turns

```
[turn] 4820ms to first audio = stt 2380 + rag 140 + llm 1180 (prompt 410, gen 760) + synth 1120
```

Then follow the decision rule at the top of step 1. Two specific things to confirm now that they
are finally measurable:

1. **`prompt` should drop sharply from turn 2 onward.** That is step 3 working — turn 1 has no
   cached prefix, but every turn after it should re-evaluate only the context and question. If it
   does not fall, the prefix is still being invalidated and something else is perturbing it.
2. **Verify answer quality before trusting step 3.** Ask the known DEVCON questions. If retrieved
   facts stop being treated as authoritative, set `RAG_CONTEXT_PLACEMENT = "system"` and take the
   prompt-eval cost back — the tests cover both placements, so the revert is one line and stays
   green.
