# Face-Tracking Servo — Jetson Orin Nano R&D

Real-time face tracking using SG90 servo(s) on the **NVIDIA Jetson Orin Nano**. The servo follows the face position using a **PD controller** for smooth, jitter-free motion. All intelligence lives in Python on the Jetson; an Arduino Uno acts purely as a voltage-level bridge to drive the servo at 5V.

**Current implementation (v2):** PD controller · auto-sleep on no face · confidence gate · optional pan-tilt (2 servos)

---

## TL;DR
*Last updated: 2026-06-17*

**R&D Timeline**
- **2026-06-15** — First hardware test. Ran into issues early: missing wires and servo condition not checked beforehand. Session postponed to the following day.
- **2026-06-16** — Main R&D day. Got everything working — built the full face tracking pipeline, solved all hardware and software challenges, completed most of the development.
- **2026-06-17** — Post-development. Consolidated findings, wrote the README and documentation, created the TL;DR, implemented Y-axis tilt (code complete, untested — only 1 working servo available), added LOFI-compatible face parameter capture (yaw, pitch, roll, mouth, eyes, smile, distance — same algorithm as face-detection-movements), auto USB driver loading on startup (no more manual `modprobe` step), and cleaned up the project structure.

**What it does**
- Detects a face via MediaPipe on a Jetson Orin Nano and rotates SG90 servo(s) to track it in real-time at **35–45fps** (with full LOFI param capture)
- Pan-only (1 servo) or pan+tilt (2 servos) with `--tilt` flag — Y-axis is fully implemented in code but **not yet tested** (only 1 working servo available during R&D)
- Camera can be a local USB webcam or a laptop streaming over TCP

**Why Arduino is needed**
- Jetson GPIO (the physical pins you connect wires to) is capped at 3.3V hard limit — think of it as the Jetson only being able to "speak" at a volume of 3.3, but the servo only reliably "hears" at 5.0
- Arduino (USB-powered) outputs 5V and handles PWM timing — Jetson just sends `"pan,tilt\n"` over serial
- Alternatives exist (BSS138 level shifter) but require the Jetson to generate PWM itself

**How Jetson talks to Arduino**
- Jetson sends a plain text number over USB cable: `"90,90\n"` (pan angle, tilt angle)
- Arduino reads it, calls `servo.write(angle)`, and the servo moves — that's the entire protocol
- USB here is just a serial cable — 115200 baud, no special drivers needed on the Arduino side
- Latency is under 5ms; the Arduino responds with `"OK:90,90"` to confirm

**How it works (pipeline)**
- Frame → resize to 320×240 → MediaPipe FaceMesh → nose tip (landmark #1) → confidence gate (bbox ≥ 4%) → PD controller → serial to Arduino → servo

**PD controller (v2, replaces v1 EMA + dead zone)**
- `correction = Kp × error + Kd × d_error` — P chases the target, D damps jitter
- No dead zone needed — D term naturally cancels micro-oscillations
- Auto-sleeps at 90° after 3s of no face, resets on re-detection

**Hardware needed**
- Jetson Orin Nano + Arduino Uno (CH340 clone works) + 1–2× SG90 servo + USB cable + jumper wires
- Orange wire → Pin 9 (pan) / Pin 10 (tilt), Red → 5V, Brown → GND

**Setup steps**
1. Load CH340 driver: `sudo modprobe usbserial && sudo insmod ch341.ko` (not in Jetson kernel by default — must compile manually)
2. Upload Arduino sketch: `bash build_servo_serial.sh`
3. Verify servo: `python3 servo_serial.py --sweep`
4. Run tracking: `python3 -u face_track.py --network <ip> --no-display --flip`

**Post-development additions (2026-06-17)**
- LOFI face parameter capture — yaw, pitch, roll, mouth, eyes, smile/kiss, distance; same algorithm as face-detection-movements; use `--lofi` flag for 19-digit output string
- Auto USB driver loading — `face_track.py` now loads `ch341.ko` automatically if `/dev/ttyUSB0` is missing; no manual `modprobe` step needed
- All files consolidated into the `face-servo` directory

**Key challenges encountered**
- 3.3V GPIO → Arduino serial bridge solved it
- `ch341` missing from Jetson kernel → compiled from Linux 5.15 source
- `brltty` daemon stealing the CH340 device → `sudo apt remove brltty`
- Arduino IDE not available on Jetson → custom `avr-gcc` bash build script
- First SG90 was internally faulty → replaced; lesson: test with standalone sweep first
- Servo jitter → PD controller (Kd dampens noise)
- Startup centering silently skipped by dead zone → bypassed with direct serial write in `__init__`

**Tuning knobs**
- `Kp` — higher = faster tracking, may overshoot
- `Kd` — higher = less jitter, more damping
- `ANGLE_SCALE` — higher = bigger servo movement per head movement (default 300)
- `SEND_INTERVAL` — serial rate cap (default 10Hz)

**Voice assistant (2026-07-06)**
- Kai can now hear and reply: push-to-talk button on the web dashboard records from the
  Jetson's mic, transcribes locally with `faster-whisper`, and sends the text to a local
  Ollama model (`gemma3:4b`) for a reply — shown as text on the dashboard (`voice_assistant.py`).
- **Text-only for now** — speaker/TTS output is a planned next phase.
  *(Superseded 2026-07-28: Piper TTS shipped, and push-to-talk is no longer the only way in —
  see the hands-free entry above. `gemma3:4b` below is now `gemma2:2b`; check `config/voice.py`.)*
- One-time setup: `ollama pull gemma3:4b` (Ollama must already be installed and running).
  `faster-whisper`, `sounddevice`, and `requests` are already part of the environment — no
  new `pip install` needed.
- New endpoints: `POST /voice/start`, `POST /voice/stop`. Live status/transcript/response
  ride the existing `/params` SSE stream (`voice_status`, `voice_transcript`, `voice_response`,
  `voice_error` fields).
- Sanity-check the mic before use: `python3 -c "import sounddevice as sd; print(sd.query_devices())"`.

**RAG + editable persona (2026-07-06)**
- Kai can now answer from your own documents and its personality is editable without touching code:
  - Drop `.txt`/`.md`/`.pdf` files into `documents/`, then run `python3 -m ai.index_documents`
    from the project root (not `python3 ai/index_documents.py` — the imports are absolute)
    whenever files are added or changed — rebuilds `documents/.rag_index.json` from scratch
    each run (`rag.py`, `index_documents.py`).
  - Edit `persona.txt` at the project root to change Kai's personality — takes effect on the
    very next voice turn, no restart needed (`voice_assistant.load_persona()`).
  - Retrieval is fail-open: no index, nothing relevant enough (`SIMILARITY_THRESHOLD = 0.5`),
    or any failure anywhere just means Kai answers without extra context — unrelated
    questions behave exactly as before this feature existed.
  - Web search was discussed and intentionally left out of this round.
- **Important hardware finding:** embeddings do **not** run through Ollama. Measured directly
  on this Jetson: `gemma3:4b` and *any* Ollama-served embedding model (tried `nomic-embed-text`
  at 595MB resident, then even `all-minilm` at 76MB) cannot both stay loaded — loading either
  one always evicts the other, even with `keep_alive: -1` on both, because there's only
  ~200-300MB genuinely free once gemma3:4b is resident. That would have turned every
  RAG-enabled voice turn into a double reload (~7-13s embedding reload + ~48-51s gemma3:4b
  reload ≈ 60+ seconds), regressing the exact "thinking takes a long time" problem fixed
  earlier this session. Instead, `rag.py` embeds with **fastembed** (`BAAI/bge-small-en-v1.5`,
  ~90MB, ONNX/CPU via the already-installed `onnxruntime` — zero torch dependency, so it can't
  disturb the Jetson's custom CUDA-enabled torch build) running in-process in `face_track.py`,
  entirely decoupled from Ollama's GPU memory management. Query embedding takes ~0.03s once
  warm; `OLLAMA_NUM_CTX` stayed at `1024` with no need to bump it — verified via `ollama ps`
  that `gemma3:4b` remains 100% GPU-resident even with retrieved context injected (chunks are
  kept small on purpose, `CHUNK_SIZE_CHARS = 800`, specifically to fit this budget).
- One-time setup: `pip3 install pypdf` (only needed for indexing `.pdf` files — `fastembed` was
  installed as part of this feature and downloads its embedding model from Hugging Face on
  first use, cached afterward like `faster-whisper`'s model).
- New pre-warm threads at startup (same pattern as the voice assistant's): `rag.load_index()`
  and `rag.ensure_model_loaded()`, alongside the existing three.

**Wake-word fallback chain (2026-07-28)**
- Hands-free no longer depends on one vendor. Kai tries **three** wake engines in order and keeps the
  first that initializes: **Porcupine → openWakeWord → Whisper phrase spotting**. A tier failing is a
  logged reason, never a dead robot, and `sess_wake_engine` on `/params` says which one is live.
- Why: Porcupine has three independent ways to be unavailable — a cloud account and key, a
  `.ppn` compiled per-platform, and a CPU allow-list that **does not include this board** (the Orin's
  Cortex-A78AE reports `0xd42`, absent from every published version 2.1→4.0). Depending on all three
  holding forever was not a plan.
- **Tier 3 needs no setup at all.** It reuses the already-resident faster-whisper, so Kai always has
  *some* hands-free path. It also fixes the limitation noted in the entry below: because the
  transcript already contains what followed the wake words, *"Hey Kai, what time is it?"* is answered
  **in one breath** — no ack, no second turn. The trade-offs are ~0.4-1.0 s before the ack and the
  fact that it transcribes nearby speech locally to look for the phrase; both are bounded and
  documented in `wake/README.md`.
- New: `ai/wake_phrase.py` (pure-stdlib fuzzy matcher), `WakeEngine`/`PorcupineEngine`/
  `OpenWakeWordEngine`/`WhisperWakeEngine` and the `WakeDetector` chain in `ai/audio.py`, two scan
  states in the session FSM, `transcribe_async()` and `say(epoch=, on_done=)`.
- **Fixed a latent bug that would have made openWakeWord silently never fire:** `MicStream` sized its
  wake frame assembler in `__init__`, *before* `wake.open()` knows which tier won and therefore what
  frame size it wants. Porcupine is 512 in both places so it worked by luck; openWakeWord wants 1280
  and would have been fed 512-sample frames forever, with scores pinned near zero and
  `sess_wake_ok` still reporting `True`. `sess_wake_frame` on `/params` is the proof it took effect.
- Matching Whisper transcripts turned out to be the subtle part. `"kai" in text` is worse than
  useless — an adversarial sweep found it firing on `"Okay Google"`, `"okay okay i get it"`,
  `"hi hi hi"`, `"hey Kyle"`, `"hey Kayla"` and `"sabihin mo kay Kai"` (talking *about* Kai). All
  measured, all fixed, all pinned as tests. `"hey Kaye"` still matches and that is deliberate — it is
  a near-homophone the acoustic tiers can't separate either.
- Also added `WHISPER_CPU_THREADS = 4`: ctranslate2 defaults to *every* core, and STT now runs per
  nearby utterance rather than per button press. Left uncapped it starves the servo control loop and
  shows up as jittery face tracking rather than as anything audio-shaped — watch `[control] N Hz`.
- Setup for tiers 2 and 3, plus the "hey kai" training recipe: **`wake/README.md`**.

**Hands-free conversation — "Hey Kai" (2026-07-28)**
- Kai no longer needs a button. Say **"Hey Kai"**, he answers *"Yes?"*, and then you just talk:
  silence ends your turn, and the conversation ends itself when you stop talking or walk away.
  The dashboard mic button and spacebar still work exactly as before, as a fallback.
- **The camera does not wake Kai** — deliberately. Entry is the wake word only, so it works in the
  dark, from another room, and with nobody in frame. Vision is used only to help decide when a
  conversation is *over*.
- New pieces: `config/wake.py` (all tunables), `ai/audio.py` (resampler, framing, pre-roll, wake
  and VAD wrappers), `ai/session.py` (the one open stream + the state machine),
  `vision/presence.py` (a three-valued presence sink face_track feeds at `INFERENCE_FPS`).
- **Enable it with `--wake`** (already added to `scripts/autostart.sh`). Without the flag, or
  without a Porcupine key/`.ppn`, hands-free is simply off and push-to-talk is untouched.
- One-time setup:
  ```bash
  pip3 install pvporcupine
  pip3 install webrtcvad || pip3 install webrtcvad-wheels   # no aarch64 wheel; the fallback is prebuilt
  mkdir -p ~/.config/kai && printf '%s' 'YOUR_KEY' > ~/.config/kai/porcupine.key
  chmod 600 ~/.config/kai/porcupine.key
  ```
  Generate a custom "Hey Kai" keyword at `console.picovoice.ai` into `wake/hey-kai.ppn`. **The
  `.ppn` is platform-specific** — pick the ARM/Linux (aarch64) target, or Porcupine raises
  `PorcupineInvalidArgumentError` on the Jetson. The access key is never stored in `config/`.
- **Tune it on-device with `scripts/wake_test.py`** — it prints rolling RMS, VAD decisions and wake
  hits and nothing else, so setting `VAD_RMS_FLOOR` takes minutes instead of an afternoon of
  reading `/tmp/face-servo.log`. It cannot run at the same time as `face_track.py`: the raw I2S hw
  device admits exactly **one** opener, which is the single fact that shaped this whole design —
  hence one always-open stream fanned out to Porcupine, the VAD and the utterance buffer, rather
  than a self-contained wake module opening its own mic.
- **The mic is now open all the time, so Kai can hear himself.** A gate drops audio blocks before
  any DSP whenever Kai's own audio could be in the air, sized from the WAV's duration plus
  `TTS_TAIL_MUTE_S` — *not* from `paplay` exiting, which happens once the file is in the
  PulseAudio sink buffer, several hundred ms before the amp is actually quiet. That gap is exactly
  how a robot ends up answering itself; raise the tail first if it ever does.
- Voice barge-in is **not** supported (no echo cancellation, mic and speaker share the chassis), so
  the wake word is ignored while Kai is thinking or speaking. Pressing the dashboard mic button
  *does* interrupt him — a button press is unambiguous intent.
- New endpoints: `POST /voice/wake` (fire the wake word by hand — invaluable for telling "the
  session machine is broken" apart from "Porcupine isn't hearing me") and `POST /session/end`.
  ~40 additive `sess_*` fields ride the existing `/params` SSE stream, and session state is
  projected onto the `voice_status`/`voice_speaking` fields the dashboard already reads — so the
  existing UI shows hands-free state with **zero** frontend changes.
- Also fixed along the way, all pre-existing: `ai/tts.py` could not cancel an in-flight Piper
  synth (only playback), so an abandoned reply still got spoken; `reset_history()` was dead code
  and racy against an in-flight Ollama call, which could re-append one turn *after* the clear; and
  a push-to-talk recording had no maximum length, so a stuck button grew the buffer until the
  process died. A turn `epoch` now versions every async result and is dropped on mismatch.
- **Supersedes** the "text-only for now" note in the 2026-07-06 voice-assistant entry below and
  the onboard-audio "Next Steps" section: Piper TTS, the I2S mic and the speaker all shipped, and
  the jaw is synced to real audio duration rather than a text-timed estimate. The live model is
  `gemma2:2b` (switched from `gemma3:4b` to fit the camera in 8 GB), so read `config/voice.py`
  rather than the model names in the older entries.

**Documents first, and "DEVCON" by ear (2026-08-06)**
- **Retrieved documents now outrank the model's own knowledge.** The context block used to be
  introduced as *"Reference information (use only if relevant…)"*, which reads as optional — the
  model answered DEVCON questions out of its vague pretraining with the real text sitting right
  there in the prompt. `rag.format_context()` now presents the chunks as Kai's own documents, tells
  the model to answer from them and nothing else (names, numbers, dates as written, no guessing, say
  "not sure" when they don't cover it) — **and in the same breath re-asserts her voice**, because a
  bare "answer from the documents" makes a small model recite chunk prose and drop the persona.
  `persona.txt` carries the precedence rule too, so it survives even if the context block is
  trimmed. The "ignore if unrelated" escape stays: it is the defensive half of
  `SIMILARITY_THRESHOLD`.
- **Fuzzy "DEVCON" matching on the query (`ai/query_alias.py`).** Every chunk in `documents/` spells
  the brand `DEVCON`; Whisper spells it `defcon`, `dev com`, `debcon`, `Devon`, `de con`, `dev khan`.
  `bge-small` embeds those as *different words*, so the single most on-topic question Kai can be
  asked drifted below threshold and retrieved **nothing**. The query is now folded onto the
  canonical spelling before embedding — retrieval only, never the transcript on `/params` and never
  the turn handed to the LLM, since a wrong guess must not put words in the speaker's mouth. Pure
  stdlib `difflib`, reusing `wake_phrase.py`'s offset-carrying tokenizer.
  - Threshold `DEVCON_MATCH_RATIO = 0.80` was measured, not guessed: plausible mishearings land at
    0.833+ while the nearest real words (`recon`, `devotion`, `beckon`, `second`, `beacon`,
    `device`) top out at 0.727. `deacon` is the one word inside that gap and is blocklisted.
  - Split renderings are joined ("dev con" → `DEVCON`) **only when both halves are too short to
    stand alone**. Without that guard `isdevcon` scores 0.857 and *"what is DEVCON Philippines?"*
    came out as *"what DEVCON Philippines?"* — the guard also protects the trailing side, so
    "devcon po" keeps its `po` and "devcon ph" stays intact.
- Knobs in `config/rag.py`; `tests/test_query_alias.py` covers the renderings, the distractors and
  both swallowing bugs.
- **Watch the context budget.** `OLLAMA_NUM_CTX` is 1024 and `TOP_K = 3`. Indexed chunks average
  230 chars (median 140), so a normal retrieval is ~175 tokens and fits fine — but three worst-case
  800-char chunks are ~600 tokens, and with the persona, the new header, three history pairs and
  `OLLAMA_NUM_PREDICT = 96` that overflows, and what gets dropped is the *front* of the prompt, i.e.
  the persona. If Kai ever sounds flat and generic on a long-document question, set `TOP_K = 2`
  before touching `OLLAMA_NUM_CTX` (raising the context window is what breaks GPU residency).

**A DEVCON question can no longer come back empty (2026-08-07)**
- Fuzzy matching widened the *entrance* to retrieval. Nothing guaranteed an *exit*: when nothing
  cleared `SIMILARITY_THRESHOLD`, `retrieve_context()` returned `""` — and `""` is the dangerous
  state, the one where the model answers about DEVCON out of pretraining. Eight layers now sit
  around it, four preventive and four as the floor. **Requires a reindex** (`python3 -m
  ai.index_documents`) — the index format gained a `title` per chunk and an `entities` list; an
  older index still loads, it just runs without those two layers.
- **Before the text exists.** `WHISPER_INITIAL_PROMPT` seeds the turn decoder with the vocabulary
  it is about to need, so the mishearing often never happens. This is the only layer that reaches
  multi-word names — *"geeks on the beach"* is ordinary English, so no matcher can safely flag it,
  but a primed decoder writes *"Geeks on a Beach"* in the first place. Not applied to the wake
  scan: a weak model primed with DEVCON vocabulary invents DEVCON talk out of room noise.
- **A second matcher, OR'd with the ratio.** `difflib` compares characters *in order*, so a vowel
  shift (`devcon` → `davcan` → `duvcun`) costs it real score and costs the spoken word nothing.
  `_skeleton()` drops vowels and folds consonants onto sound-alike classes, reducing all of them
  to one key. Checked against all 2111 words in `documents/`: **zero** new false positives.
- **An entity gazetteer, derived not hand-listed.** A mangled program name carries no DEVCON token
  at all, so nothing above fires on it. `build_gazetteer()` harvests names from the documents' own
  headings at index time — 14 from 82 headings — and both filters are load-bearing: rarity alone
  let *"When in doubt"* and *"Color palette (official, exact)"* through, since a style guide's
  section headings are rare words too. A name-shape test (`_is_name_shaped`) is what separates a
  label from an instruction. Derived, because a hand-written list goes stale *silently* on the
  next content drop — and silent staleness is the exact failure this layer exists to prevent.
- **Titles in every chunk's embedding.** Breadcrumbs only reach chunks under a heading; a
  mid-document paragraph, a `.pdf`, or a line of `kai_facts.txt` can carry no mention of DEVCON at
  all — and those are the chunks a *perfectly transcribed* "what does DEVCON do?" scores worst
  against. The filename is the one piece of provenance every chunk has. Embedded, not stored in
  `text`: retrieval needs it, the prompt does not. **This shifts every score slightly — re-check
  `SIMILARITY_THRESHOLD` after the reindex.**
- **Then the floor, in order: lexical → sticky → lowered threshold → primer → notice.**
  - `lexical_rank()` is IDF-weighted token overlap, and it fails in the opposite direction to a
    dense embedder — blind to paraphrase, exact on the rare literal token (a chapter city, a
    surname, a year) that bge-small smooths away. IDF weighting alone proved too loose (a word in
    12 of 14 chunks still has positive IDF, and a query of merely common words scored a perfect
    1.0), so `LEXICAL_MAX_DF_RATIO` hard-gates which words may carry weight at all.
  - `STICKY_TURNS` covers the follow-up that carries neither pronoun nor brand — *"how many
    chapters?"*, *"when did it start?"* — which `ANAPHORA_WORDS` cannot see. It only ever runs as
    a **retry after normal retrieval already came back empty**, so a genuine topic change
    retrieves its own answer and never reaches it. It deliberately does not renew on a successful
    retry: doing so made the topic permanent, since every later turn kept the flag alive by its
    own retry. Cleared with the conversation by `reset_history()`.
  - `FALLBACK_THRESHOLD = 0.32` (`top_k=1`), then the pinned `documents/devcon_primer.txt`, then
    `NO_CONTEXT_NOTICE` — an explicit *"you don't have this, don't guess, and don't confuse
    DEVCON Philippines with the other conference"*, which beats the silence that invites a
    confident hallucination.
- **The primer needed capping (`PRIMER_MAX_IN_RANKING = 1`), measured after the reindex.** It is
  indexed like any other document, and one dense on-brand fact per line turns out to be the ideal
  shape for `bge-small`: primer lines took **all three** `TOP_K` slots on *"what is DEVCON?"*,
  *"when did DEVCON start?"* and *"who founded DEVCON?"*, crowding out the documents that answer
  the specific question. Barring it from ranking overcorrected in the other direction — *"who
  founded DEVCON?"* then returned a `kai_facts` line about who assembled the *robot*, and *"when
  did DEVCON start?"* returned anniversary boilerplate with no date in it. One slot is the
  measured middle: the accurate summary stays, `TOP_K - 1` slots are always left for whatever is
  specific. The last-resort injection is uncapped — when it fires there is nothing left to crowd.
- **The gate is the whole design.** Those last three fire only when the turn is *provably* about
  DEVCON — the brand however it was spelled, or a gazetteer name. An unrelated question still
  falls through to `""` exactly as before. `tests/test_rag.py` asserts both halves against the
  same index and the same scores, with only the flag differing.
- **`SIMILARITY_THRESHOLD` was re-checked after the reindex, and the finding is not the one that
  was expected.** The reindex itself is a wash — 21 real queries moved by ±0.05, on-topic mean
  **+0.003**, off-topic mean **−0.017**, so separation is marginally *better* and 0.45 needs no
  adjustment for it. But 0.45 is not separating anything on this corpus, and was not before this
  change either. Measured top scores: on-topic DEVCON **0.68–0.81**, Kai facts **0.61–0.76**,
  and *off-topic* — "tell me a joke", "what time is it?", "what is the capital of Japan?" —
  **0.51–0.64**. `bge-small` compresses everything into a narrow high band and 0.45 sits under
  all of it, so **every turn retrieves something**.
  - No single threshold fixes it: off-topic tops out at 0.637 ("tell me a joke") while the
    lowest legitimate hit is 0.613 ("when is your birthday?", already carrying its `SOURCE_BOOST`).
    They overlap. Raising the bar to 0.66 gives perfect off-topic rejection and keeps every DEVCON
    question — and loses the birthday, which is the exact case `SOURCE_BOOST` was added for.
  - The 236-chunk omnibus style guide (**71% of the index**) was the obvious suspect and is not
    the cause: excluding it only drops the off-topic ceiling from 0.637 to 0.614, still above
    that 0.613 floor. It is worth splitting anyway — it is a *writing* guide, not knowledge Kai
    should answer visitors from.
  - **Consequence for the layers above:** while nothing ever returns empty, the floor never gets
    reached. Layers 3–7 (lexical, sticky, lowered threshold, primer injection, notice) are
    insurance that stays dormant on today's corpus and index; they earn their keep when the index
    is missing, broken, or mid-rebuild, and the moment the threshold is raised to something that
    actually rejects. Actively working today: the decoder bias, the skeleton matcher, the
    gazetteer's query expansion, the per-chunk titles, and the primer as a ranked document.

**Bare "hey" wakes Kai on the Whisper tier (2026-08-07)**
- The wake phrase on tier 3 is now **"hey"** — the name is optional. `"Hey Kai"` still works and still
  wins when the name is recognized; `"hey"` alone is a third accepted form in `ai/wake_phrase.py`.
- Why: the NAME slot is where this tier loses real wakes. The `tiny` scan model renders "Kai" as
  *guy*, *gai*, *chi*, *嘿哀* — `WAKE_PHRASE_NAMES` is a list we only ever extend **after** being
  ignored. The prefix is one common English word the model gets right. A false reject means the
  feature does nothing; a false accept costs an ack and a listening window that self-ends.
- **Deliberately narrow.** Bare-prefix matching requires `"hey"` at **token 0** — a mid-sentence
  "...and I was like hey, no" cannot fire — and only `"hey"`, never the rest of
  `WAKE_PHRASE_PREFIXES`. `"okay"`/`"ok"`/`"oy"`/`"ey"` open ordinary sentences and `"hi"` is how
  people greet each other in the room; all stay two-word-only.
- **Exact match, not a ratio** — the one place in this file that abandons fuzzy matching, and it was
  measured: `"they"` scores **0.857** against `"hey"`, the *identical* score to `"heyy"`, a genuine
  drawn-out wake. No threshold separates them at three characters, and with no second token there is
  nothing left to disconfirm a bad guess. Repeated letters are collapsed (`"heeeyyy"` → `"hey"`)
  instead. Do not reintroduce a ratio here.
- **Accepted cost, stated plainly:** greeting any person by name now wakes Kai — `"hey Chris"`,
  `"hey guys"`, `"hey everyone"`. That is indistinguishable from a wake at token 0 and no matcher
  tuning fixes it. The real fix is tier 2 (openWakeWord), which spots the phrase acoustically.
- **This only affects tier 3.** Porcupine (`wake/hey-kai.ppn`) and openWakeWord (`wake/hey_kai.onnx`)
  are trained blobs that still require the full "Hey Kai" and cannot be widened from config — so
  which phrase works depends on which tier won at startup. Consider pinning
  `WAKE_ENGINE_FORCE = "whisper"` while evaluating this.
- Rollback is one line: `WAKE_PHRASE_SOLO_PREFIXES = ()` in `config/wake.py` restores strict
  two-word matching. Tests pin both configurations.

**Why the wake word barely worked — three bugs, none of them in the matcher (2026-08-07)**
- Shortening the phrase made it *worse*, and chasing that turned up two more problems upstream. The
  matcher was fine; everything below it was tuned for a different job.
- **The scan path was silently eating short wake phrases.** `WAKE_WHISPER_MIN_UTTERANCE_S` was 0.35,
  copied from the turn path's `MIN_UTTERANCE_S`. *"Hey Kai"* is ~0.65 s of voiced audio and cleared
  it easily; *"hey"* alone is 0.25–0.35 s — sitting exactly **on** the threshold, so a crisp one was
  discarded before Whisper ever ran. Now 0.15. `sess_scan_skip_short` is the counter that shows it.
- **~3.2 s of deafness after every wake attempt.** One `SpeechGate` served both paths, so a wake scan
  inherited `VAD_HANGOVER_S = 1.5` — a value that exists so a speaker pausing mid-sentence isn't cut
  off, which cannot happen while saying one word. Charged to every wake: 1.5 s hangover + ~0.75 s
  transcribe + 1.0 s cooldown, and **nothing is captured during the last two**. So a missed wake
  followed by the natural response — saying it again straight away — put the retry inside the dead
  window. That is most of why this felt broken rather than merely slow. New `WAKE_SCAN_HANGOVER_S =
  0.45` via `SpeechGate.set_hangover()`, set at each capture's onset: ~1 s faster per wake, dead
  window down to ~1.8 s, and a shorter clip for Whisper to decode.
- **In a noisy room the tier was structurally deaf — Whisper never ran once.** `VAD_RMS_FLOOR_HOLD`
  (250) is the bar to *keep* an utterance open. Once ambient noise sits above it the hangover clock
  can never run out, so the scan utterance never closes, hits `WAKE_WHISPER_MAX_UTTERANCE_S` (6 s),
  is thrown away as `too_long`, and arms the 3 s long cooldown. A 6-on/3-off cycle with **zero**
  transcriptions, and `sess_wake_ok` still reporting `True`. Signature on `/params`:
  `sess_scan_skip_long` climbing while `sess_scan_checks` stays flat.
  - Both floors were one room's measurement — 40 s in a quiet room, p95 and p50 of *that* room. They
    now track ambient instead of being pinned to it: `ambient` is the **quietest frame in a sliding
    1.5 s window** (a minimum, not an average — speech is loud and intermittent, so an average would
    be dragged up by the very person trying to wake Kai), and the floors are lifted to clear it.
  - **Adaptation is a no-op in the room the constants came from**, by construction: that session's
    p50 was 124, and 124 × 5.2 = 650 (the open floor), 124 × 2.0 = 250 (the hold floor). The
    multipliers were chosen to reproduce the measured tuning, and a test asserts it.
  - Frozen while an utterance is open. Continuous speech contains no true silence, so a minimum taken
    mid-turn settles on the speaker's quietest syllable and lifts the hold floor out from under
    them — re-creating the exact bug the hold floor was added to fix.
  - The lift is capped at 4× the configured floor. Deafness is strictly worse than false onsets: a
    false onset costs one discarded Whisper run, while a floor above the speaker's own voice makes
    the feature do nothing and report no error. In a room that loud the answer is tier 2.
- New on `/params`: `sess_rms_ambient`, `sess_rms_floor_live`, `sess_rms_hold_live`.
  **`sess_rms_ambient` is the number to look at when the wake word works at your desk and not in the
  venue.** `WAKE_AMBIENT_ADAPT = False` pins the old behaviour.
- Not done, and worth doing next: `WAKE_WHISPER_SCAN_MODEL` is still `tiny`, chosen when the matcher
  needed two words including a name `tiny` mangles constantly. Form C needs one common English word,
  so `base` (+470 ms measured) is probably now the better trade — and the hangover fix hands back
  more than it costs. Test it on the robot before switching.

---

## Table of Contents

1. [What This Does](#what-this-does)
2. [Architecture](#architecture)
3. [Hardware Requirements](#hardware-requirements)
4. [Software Requirements](#software-requirements)
5. [Memory Budget](#memory-budget)
6. [Wiring](#wiring)
7. [Setup Guide](#setup-guide)
8. [Running the System](#running-the-system)
9. [Configuration & Tuning](#configuration--tuning)
10. [Improvement Roadmap](#improvement-roadmap)
11. [Challenges & How They Were Overcome](#challenges--how-they-were-overcome)
12. [R&D Findings](#rd-findings)
13. [FAQ](#faq)
14. [File Reference](#file-reference)

---

## What This Does

The system detects a human face in a camera feed and rotates servo motor(s) to track it. A **PD controller** smoothly drives the servo toward the target without jitter or dead-zone snapping. When no face is detected for 3 seconds the servo auto-centers and stops sending commands.

**v1 scope:** X-axis pan only, EMA + dead zone smoothing.
**v2 scope (current):** Pan + optional tilt, PD controller, auto-sleep, confidence gate.

---

## Architecture

```
┌─────────────────────────────────────┐
│         Jetson Orin Nano            │
│                                     │
│  Camera (USB or TCP network)        │
│       │                             │
│       ▼                             │
│  OpenCV → resize 320×240            │
│       │                             │
│       ▼                             │
│  MediaPipe FaceMesh                 │
│  → nose tip landmark (index 1)      │
│  → nose.x (0.0=left  1.0=right)     │
│  → nose.y (0.0=top   1.0=bottom)    │
│       │                             │
│       ▼                             │
│  Confidence gate (bbox area ≥ 4%)   │
│       │                             │
│       ▼                             │
│  PD controller (Kp=0.25, Kd=0.10)  │
│  → target = nose.x × 180           │
│  → correction = Kp×err + Kd×d_err  │
│  → current_angle += correction      │
│       │                             │
│       ▼                             │
│  Auto-sleep after 3s no face        │
│       │                             │
│       ▼                             │
│  python serial → /dev/ttyUSB0       │
│  "pan,tilt\n"                       │
└──────────────┬──────────────────────┘
               │ USB cable
               ▼
┌──────────────────────────┐
│      Arduino Uno          │
│  (servo_serial.ino)       │
│                           │
│  parse "pan,tilt\n"       │
│  pan_servo.write(pan)     │
│  tilt_servo.write(tilt)   │
│  Pin 9 → pan  (5V PWM)    │
│  Pin 10 → tilt (5V PWM)   │
└──────────┬────────────────┘
           │
           ▼
     SG90 Servo(s)
```

**Why Arduino?**
The Jetson Orin Nano's GPIO is capped at **3.3V** on all pins — no exceptions, no workarounds in hardware. The SG90 servo requires a **~5V signal** to respond. The Arduino, powered by USB, outputs 5V PWM on its digital pins. The Arduino is completely passive — it just converts the serial angle command into a 5V PWM pulse. All tracking logic stays on the Jetson.

**PD controller:** Instead of mapping nose position directly to an angle (v1), the PD controller computes how far the face is from where the servo is pointing and applies a proportional+derivative correction each frame. The P term chases the target; the D term damps oscillation. This replaces both EMA smoothing and the dead zone.

**Camera modes:**
- **Local USB**: plug a webcam directly into the Jetson
- **Network TCP**: run `laptop_camera.py` on a laptop; Jetson receives JPEG frames over TCP port 8485

---

## Hardware Requirements

| Component | Notes |
|-----------|-------|
| NVIDIA Jetson Orin Nano | Any variant; tested on 8GB Super |
| Arduino Uno | CH340 clone works; needs ch341 kernel module on Jetson |
| SG90 9g Micro Servo × 1 | Pan axis. Orange = signal, Red = VCC, Brown = GND |
| SG90 9g Micro Servo × 1 *(optional)* | Tilt axis — only needed for `--tilt` mode |
| Pan-tilt bracket *(optional)* | Mounts both servos at 90° for 2D tracking |
| USB-A to USB-B cable | Arduino to Jetson |
| Jumper wires (female-female) | 3 wires per servo to Arduino |
| USB webcam (optional) | If not using laptop camera |
| Laptop on same network (optional) | Alternative camera source |

> **Servo quality matters.** A faulty servo (internally broken) can appear to respond but won't move or will behave erratically. If the servo buzzes but doesn't rotate during a standalone sweep test, replace it before debugging software.

---

## Software Requirements

### On the Jetson

The full list lives in **[`requirements.txt`](requirements.txt)** — 16 third-party packages, one
line each, annotated with which file imports it and which ones cannot come from PyPI. Read its
header before installing anything: **do not `pip3 install -r` it on the live robot.** The Jetson's
torch is hand-built with CUDA, and a PyPI reinstall silently replaces it with a CPU-only wheel.

The face-tracking half alone needs:

```bash
pip3 install mediapipe opencv-python pyserial numpy

# Kernel module for Arduino CH340 USB chip
# (ch341 is NOT included in Jetson's tegra kernel — see Challenges)
# Pre-built: servo-test/ch341_build/ch341.ko
```

The voice half adds `sounddevice openwakeword pvporcupine webrtcvad-wheels faster-whisper scipy
requests piper-tts fastembed pypdf flask`, plus Ollama, plus the I2S/PulseAudio routing in
[`scripts/autostart.sh`](scripts/autostart.sh) — again, see `requirements.txt` for the order and
the traps.

To record the exact versions this robot is running (read-only, safe on a live robot):

```bash
./scripts/freeze_requirements.sh    # → requirements.lock.txt
```

That lock file is what an SD-card rebuild restores from. `requirements.txt` says *what*;
the lock file says *which version*.

### On the Laptop (network camera mode only)

```bash
pip install opencv-python
```

`laptop_camera.py` is self-contained and needs nothing else.

---

## Memory Budget

8 GB Jetson Orin Nano with **shared CPU/GPU memory** — the GPU allocation comes out of the same
7.6 GB the OS and every process are using. This is the tightest constraint in the system and it is
the one that gets discovered by crashing into it, so: measured with robot, camera and Ollama all up.

| | |
|---|---|
| Total | 7.6 GB |
| Available, steady state | **~2.0–2.3 GB** |
| Ollama `gemma2:2b` | 2.4 GB resident (`keep_alive=-1`, 100% GPU) |
| `face_track.py` | ~1.4 GB |
| zram swap | active, ~300 MB used across 3 devices |

**`OLLAMA_NUM_CTX`** — 2048 costs ~35 MB over 1024 and is fine. **4096 hard-crashes the llama
runner** (`llama runner process has terminated: signal arrived during cgo execution`). Do not raise
it past 2048 on this hardware.

Two rules follow from the numbers:

1. **Stop `face_track.py` before changing the model or the context size.** Raising `num_ctx` forces
   a model reload, and that reload OOM'd once with the camera up — it succeeded on retry with more
   free memory. There is no headroom to absorb both at once.
2. **A runner crash self-heals, the voice turn does not.** Ollama runs `Restart=always`, so the
   service comes back on its own; the turn that triggered the crash is still lost, in front of
   whoever was talking to the robot.

---

## Wiring

### Single servo (pan only)

| Servo wire | Arduino pin |
|------------|-------------|
| **Orange** (signal) | **Pin 9** |
| **Red** (power) | **5V** pin |
| **Brown** (ground) | **GND** pin |

### Two servos (pan + tilt, `--tilt` mode)

| Servo wire | Pan servo | Tilt servo |
|------------|-----------|------------|
| **Orange** (signal) | **Pin 9** | **Pin 10** |
| **Red** (power) | **5V** pin | **5V** pin (shared rail) |
| **Brown** (ground) | **GND** pin | **GND** pin (shared) |

The Arduino is powered entirely by its USB connection to the Jetson. Its 5V pin outputs USB power directly to the servo(s).

```
Arduino board
┌────────────────────────────────┐
│  Pin 9  ──── Pan Orange        │
│  Pin 10 ──── Tilt Orange       │
│  5V     ──── Pan Red + Tilt Red│
│  GND    ──── Pan Brown + Tilt  │
│  USB ←── Jetson USB port       │
└────────────────────────────────┘
```

> **Do not connect the servo to the Jetson 40-pin header for signal.** 3.3V is insufficient. Power (5V from Pin 2) is fine for the Red wire IF you also share ground through the Arduino, but using Arduino 5V pin is simpler and safer.

---

## Setup Guide

### Step 1 — Load the CH340 USB driver

The Jetson's stock kernel does not include `ch341` (the driver for the Arduino's CH340 USB chip).

```bash
# One-time: load usbserial first, then the custom ch341
sudo modprobe usbserial
sudo insmod /home/devconph/Documents/face-servo/ch341_build/ch341.ko

# Verify
ls /dev/ttyUSB0   # should appear
```

If `/dev/ttyUSB0` doesn't appear after insmod, bind manually:

```bash
bash /home/devconph/Documents/face-servo/fix_usb.sh
```

For persistent auto-binding on every plug (run once):

```bash
sudo bash /home/devconph/Documents/face-servo/install_udev.sh
```

### Step 2 — Upload the Arduino sketch

The sketch receives `"pan,tilt\n"` commands over serial and drives the servo(s) at 5V. Compile and upload from the Jetson (no Arduino IDE needed):

```bash
bash /home/devconph/Documents/face-servo/ch341_build/build_servo_serial.sh
```

This uses `avr-gcc` from `/usr/share/arduino/hardware/tools/avr/bin/` and uploads via `avrdude`.

> **Note:** The v2 sketch accepts both `"90\n"` (single value, backward compat) and `"90,45\n"` (pan,tilt). Both `pan_servo` and `tilt_servo` are always attached; if only one physical servo is wired, the other just receives the 90° default and ignores it.

### Step 3 — Verify the servo works

```bash
python3 /home/devconph/Documents/face-servo/servo_serial.py --sweep
```

The servo should physically sweep 0° → 180° → 0° and park at 90°. If it doesn't move, check wiring (Orange on Pin 9, Red on 5V, Brown on GND).

### Step 4 — Run face tracking

**With local USB webcam:**
```bash
python3 -u /home/devconph/Documents/face-servo/face_track.py --no-display
```

**With laptop as camera (same network):**

On the laptop:
```bash
python laptop_camera.py
# Prints your laptop IP address
```

On the Jetson:
```bash
python3 -u /home/devconph/Documents/face-servo/face_track.py \
  --network <laptop-ip> \
  --no-display \
  --flip
```

> Add `--flip` if the servo tracks in the wrong direction.

---

## Running the System

### face_track.py — main script

```
usage: face_track.py [-h] [--camera CAMERA] [--network HOST]
                     [--network-port PORT] [--port PORT]
                     [--flip] [--flip-y] [--tilt] [--no-display]

  --camera N        Local USB camera index (default: 0)
  --network HOST    Laptop TCP camera IP (used when no local cam found)
  --network-port N  TCP port (default: 8485)
  --port PATH       Arduino serial port (default: /dev/ttyUSB0)
  --flip            Invert pan direction (X-axis)
  --flip-y          Invert tilt direction (Y-axis)
  --tilt            Enable Y-axis tilt servo on Arduino Pin 10
  --no-display      Headless — no OpenCV window (required over SSH)
```

**Example — pan only (most common):**
```bash
python3 -u face_track.py --network 192.168.1.x --no-display --flip
```

**Example — pan + tilt (two servos):**
```bash
python3 -u face_track.py --network 192.168.1.x --no-display --flip --tilt
```

### laptop_camera.py — camera server (run on laptop)

```
usage: laptop_camera.py [--port PORT] [--camera CAMERA]

  --port N      TCP port to serve on (default: 8485)
  --camera N    Webcam index (default: 0)
```

### Console output

```
[face_track] No local camera — using network 192.168.1.181:8485
[face_track] Connected to network camera at 192.168.1.181:8485
[face_track] flip=True  tilt=False  Kp=0.25  Kd=0.1
[face_track] Servo centered at 90°
[face_track] x=0.56 pan=82° sent | 28fps
[face_track] x=0.54 pan=81° hold | 28fps
[face_track] NO FACE (0s) — pan=81°
[face_track] No face for 3s — sleeping at 90°
```

- `x` — raw nose.x from MediaPipe (0=left, 1=right)
- `pan` / `tilt` — current angle from PD controller
- `sent/hold` — whether a serial command was dispatched this tick (20Hz cap)
- `fps` — MediaPipe processing throughput
- sleeping — servo has returned to 90° and stops sending until face reappears

---

## Configuration & Tuning

### Live settings (⚙ Settings tab)

Nine knobs are adjustable while Kai is running, from the dashboard's **⚙ Settings** tab on
`http://<jetson>:8081` — camera mode, hands-free wake word, wake sensitivity, mic noise floor, speak
replies, volume, speaking rate, follow faces, move the jaw. Every one applies the instant you change
it — none of them needs a restart.

They persist in `~/.config/kai/settings.json` (an overlay on the `config/*.py` defaults — never
committed). Delete that file, or press **Restore defaults**, to get exactly the committed behaviour
back. Also reachable over ssh:

```bash
curl localhost:8081/settings                      # values, defaults, valid ranges
curl -X POST localhost:8081/settings \
     -H 'content-type: application/json' \
     -d '{"tts_volume": 1.4, "vad_rms_floor": 800}'
curl -X POST localhost:8081/settings/reset
curl -X POST localhost:8081/camera/probe          # look for a camera right now
```

**Restart Kai** — at the bottom of the same tab, below the knobs, is the one control that is not a
setting. It stops `face_track.py` the same way `SIGTERM` does (mic, Porcupine, serial port and camera
all released properly) and exits `7`, which `scripts/autostart.sh` treats as "relaunch now, and don't
count it as a crash". Kai is back in roughly half a minute and the dashboard reconnects on its own.

It exists for the failures no knob above can reach — a capture device that has wedged, a wake engine
that stopped hearing, an Ollama that came back after Kai gave up on it — and it takes two taps, so a
stray touch on a kiosk cannot stop the robot mid-demo.

```bash
curl -X POST localhost:8081/restart               # {"status":"ok","supervised":true,...}
```

`supervised` in that reply is the honest part: it is true only when `KAI_SUPERVISED=1` is set, which
`scripts/autostart.sh` exports and nothing else does. Started by hand (`scripts/run.sh`, or
`python3 face_track.py` over ssh) it reads false, the button says so in red, and the click is a
shutdown rather than a restart — start it again from the Jetson.

`--no-camera` and `--no-hands-free` still win over the stored setting: they declare what this machine's
hardware situation is for the run, and the dashboard shows the control disabled with the reason.

### The recovery ladder — cheapest first

Three controls, stacked in the order to try them. **The order on screen is the order to use.** Each
one costs more than the one above it, and reaching for the bottom of the ladder first is how a
five-second problem becomes ninety seconds of dead robot.

| | Control | Cost | Use it for |
|---|---|---|---|
| 1 | 🎙 Find the microphone again | ~2 s, nothing interrupted | Kai cannot hear; `sess_mic_live` is false |
| 2 | ⟳ Restart Kai | ~30 s, conversation lost | Anything a setting cannot reach |
| 3 | ⏻ Reboot the Jetson | ~90 s, whole board down | Wedged kernel audio, nvargus, GPU fragmentation |

**1. Find the microphone again** (`POST /audio/reresolve`) re-runs the whole discovery path — I2S
route, pulse release, device probe — and reopens the stream, without touching the camera, the
servos or the conversation. It exists because both mics fail in ways a *later* look fixes, and
nothing short of a restart used to take that later look: across three boots the INMP441 read silent
on one, timed out on the next and worked on a third, with `arecord` finding real audio every time.

The existing watchdog cannot cover this. It reopens a stream that *died*, and is gated on
`state != disabled` — but a mic that never came up leaves the session in exactly `disabled`, so the
one case needing a second look was the one case it skipped.

```bash
curl -X POST localhost:8081/audio/reresolve
# {"ok":true,"device":5,"rate":48000,"is_i2s":true,"live":true,"restarted_session":false,...}
```

The reply says *which* mic it landed on, because "it worked" is not the whole answer — Kai on the
fallback dongle when it should be on the I2S mic is a different situation with a different next step.

**3. Reboot the Jetson** (`POST /system/reboot`) is **off by default** and needs two deliberate
steps to switch on — see `REBOOT_ENABLED` in `config/tracking.py`, which explains why. In short:
this dashboard has **no authentication at all** (Flask binds `0.0.0.0`), so unlike every other
control here, enabling it hands an unauthenticated LAN endpoint the ability to take Kai off the air.
It needs `REBOOT_ENABLED = True` *and* a sudoers line scoped to exactly `/usr/bin/systemctl reboot`
— never `NOPASSWD: ALL`. Consider running the rootfs `e2fsck` first; making reboots one click away
on a filesystem that already has known ext4 errors is how a demo robot becomes an unbootable one.

Requests carry a `{"confirm": "reboot"}` body, and the endpoint checks `sudo -l` *before* claiming
success — a misconfigured sudoers gets an error you can act on rather than a button that reports
"ok" and does nothing.

**Restarts now have a deadline.** The graceful shutdown is still the default, but if it has not
finished within `_RESTART_FORCE_AFTER_S` the process exits by force with the same code, so the
supervisor still brings Kai back. This is not hypothetical: on 2026-08-09 a `POST /restart` replied
`{"status":"ok"}` and the process never exited — it was wedged inside mic resolution, so the
teardown it depends on never completed. Same pid forty minutes later, nothing in the log. From the
dashboard that is indistinguishable from a restart that worked, which is the worst way for a
recovery control to fail.

### Starting without a camera

Kai starts and stays up with no camera attached — the dashboard, voice assistant, wake word and servos
do not need one, and the ⚙ Settings tab shows **NO CAMERA** with the actual reason (e.g. `no
/dev/video* device`). A background supervisor keeps checking, so **plugging a camera in later brings it
up live, with no restart**. Set Camera to `off` to stop using one deliberately.

The same applies to the Arduino: if the serial port is missing, Kai runs without servos rather than
failing to start. Under the cron `@reboot` launcher there is no supervisor to retry, so a startup crash
would mean a dead robot until the next reboot.

**CSI is probed only once, at boot.** If that probe fails — a cable reconnected while the board was
off, or a camera plugged in after boot — the sensor stays invisible for the whole boot, because
creating `/dev/video0` needs a driver bind and that needs root. A `@reboot` helper in **root's**
crontab retries the bind when no capture device appeared:

```bash
sudo bash scripts/camera_bind_retry.sh --install   # once; @reboot in root's crontab
sudo bash scripts/camera_bind_retry.sh --now       # or run it by hand any time
tail -f /tmp/camera-bind.log
```

When it succeeds, `face_track.py`'s supervisor picks the camera up within ~5 s — no restart. It is
deliberately separate from `autostart.sh`, which runs unprivileged: the robot process never needs root.
USB webcams are unaffected by any of this; they genuinely hot-plug.

**Diagnosing a camera that will not come up:**

```bash
sudo bash scripts/camera_diag.sh
```

Reports in plain language whether a sensor actually *answers* on the CSI bus, and ends with a verdict
that separates a software mismatch from a cable/module fault. Worth knowing why it exists: a CSI sensor
is only powered for ~10 ms while the driver probes it, so an ordinary `i2cdetect` sweep (50–100 ms)
misses the window and shows an empty bus whether or not a camera is attached. This probes single
addresses inside that window while cycling the rail, and checks the addresses used by other common
modules so "it is not actually an IMX219" surfaces instead of hiding. Run it after every hardware swap.

### Restart-only constants

See `config/README.md` for the full split. All the older tuning constants:

```python
Kp            = 0.25   # proportional gain: higher = faster tracking but may overshoot
Kd            = 0.10   # derivative gain: higher = more damping, less jitter
SEND_INTERVAL = 0.05   # serial rate cap (20Hz); lower = more responsive
SLEEP_AFTER   = 3.0    # seconds with no face before centering servo
MIN_FACE_AREA = 0.04   # bbox area threshold; raise to ignore distant faces
PROCESS_W     = 320    # MediaPipe input width (lower = faster)
PROCESS_H     = 240    # MediaPipe input height
```

| Goal | Change |
|------|--------|
| Less jitter / more natural | Decrease `EMA_ALPHA` (try 0.20) |
| Faster input response | Increase `EMA_ALPHA` (try 0.50) |
| Faster catch-up | Increase `Kp` (try 0.30–0.40) |
| Dampen overshoot | Increase `Kd` relative to `Kp` |
| Bigger servo movements | Increase `ANGLE_SCALE` (try 320–360) |
| Less CPU load | Decrease `PROCESS_W/H` (try 160×120) |
| Ignore far-away faces | Increase `MIN_FACE_AREA` (try 0.08) |
| Shorter sleep timeout | Decrease `SLEEP_AFTER` (try 1.5) |

---

## Improvement Roadmap

The system evolved through three deliberate phases from v1 (EMA + dead zone) to v2 (PD controller, auto-sleep, pan-tilt). This section documents each phase, what it changed, and why.

### Phase 1 — PD Controller (replaces EMA + dead zone)

**v1 problem:** EMA smoothing delays the signal uniformly regardless of how fast the face moves. The dead zone eliminates jitter but causes the servo to snap abruptly when the threshold is crossed.

**v2 solution:** PD (Proportional-Derivative) controller.

```
target_angle  = nose.x × 180
error         = target_angle − current_angle
correction    = Kp × error + Kd × (error − prev_error)
current_angle = clamp(current_angle + correction, 0, 180)
```

- **P term** (Kp): correction proportional to how far off the servo is — larger offset → faster catch-up
- **D term** (Kd): correction proportional to rate of change — oscillation produces alternating ±d_error → D term cancels it naturally, no dead zone needed

The D term replaces the dead zone: instead of gating commands, it dampens micro-oscillations so small noise produces near-zero net correction, while real motion produces consistent d_error that reinforces the P term.

### Phase 2 — Auto-Sleep + Confidence Gate

**Problem:** When no face is visible the servo holds its last position indefinitely and keeps receiving stale commands. Distant or partially-visible faces produce noisy landmark estimates.

**Auto-sleep:** `SLEEP_AFTER = 3.0s`. No face detected for 3 seconds → servo returns to 90° and stops sending serial. PD state resets. On face reappearance, tracking resumes cleanly from center.

**Confidence gate:** `MIN_FACE_AREA = 0.04`. MediaPipe can detect faces at any distance but landmark accuracy degrades with distance. Bounding box area (normalized, 0–1) is a simple proxy for face confidence. Faces smaller than 4% of frame area are skipped.

### Phase 3 — Pan-Tilt (Y-axis)

**Hardware:** Second SG90 on Arduino Pin 10. Pan-tilt bracket mounts both servos at 90° to each other.

**Serial protocol change:** `"90\n"` → `"pan,tilt\n"`. Arduino sketch handles both formats (backward compatible).

**PD controller:** Independent `PDAxis` instance for tilt. Tracks `nose.y` (0.0=top, 1.0=bottom). `--flip-y` flag inverts tilt direction. If `--tilt` is not set, tilt defaults to 90° and Arduino tilt servo stays centered.

**Enable:**
```bash
python3 -u face_track.py --network <ip> --no-display --flip --tilt
```

### Next Steps (Planned)

> **DONE (2026-07-28).** The section below is kept for the reasoning; both halves shipped. The
> INMP441 I²S MEMS mic and the USB-dongle → PAM8403 → speaker output are wired and configured, the
> jaw is synced to real WAV duration instead of a time-estimated envelope, and Kai is hands-free
> via the "Hey Kai" wake word. See the hands-free changelog entry near the top.

**Onboard audio — embedded mic + speaker (cleaner enclosure).** Replace the external USB
mic with an **embedded microphone**, and add an **on-board 3W–5W speaker**. The goal is a
clean, integrated look for Kai — no dongles or cables hanging off the enclosure.

- **Mic:** swap the USB capture device for a built-in mic (e.g. I²S MEMS or an analog
  electret into the audio input). `resolve_input_device()` in `ai/voice_assistant.py`
  already probes candidates for live signal, so the pipeline should adopt the new input
  with little/no code change — just re-verify with
  `python3 -c "import sounddevice as sd; print(sd.query_devices())"`.
- **Speaker:** add a 3W–5W speaker so Kai can actually talk out loud. This unblocks the
  TTS/speaker output phase noted above (currently text-only) — the jaw "speaking" pantomime
  in `voice_assistant.py` can then be synced to real audio playback instead of a
  time-estimated envelope.
- **Why:** removes external USB peripherals for a tidier, self-contained build and gives Kai
  a real voice.

---

## Challenges & How They Were Overcome

### 1. Jetson GPIO is capped at 3.3V — servo needs 5V

**Problem:** All 40 GPIO pins on the Jetson Orin Nano output 3.3V logic. The SG90 servo tested required ~5V signal to respond. At 3.3V, the servo electronics marginally detected the signal but the motor lacked torque to actually move.

**Attempted first:** Direct 3.3V PWM from Jetson Pin 33. Verified with sysfs (`/sys/class/pwm/pwmchip2/pwm0`) that the signal was correct (1500µs duty, 50Hz). Confirmed signal was present via Arduino acting as a reader — it read ~535µs instead of the expected 1500µs. This short reading caused the servo to drive to minimum position and buzz.

**Root cause of the 535µs reading:** The Arduino's digital HIGH threshold is ~3.0V. The 3.3V signal was only marginally above threshold, causing early false-low edge detection in `pulseIn()`.

**Final solution:** Arduino as transparent serial bridge. Jetson sends angle values (`"90\n"`) over USB serial. Arduino receives and calls `sg90.write(angle)` — native 5V PWM. No signal level translation needed.

---

### 2. Arduino not detected on Jetson (`/dev/ttyUSB0` missing)

**Problem:** The CH340 USB-serial chip on the Arduino clone uses the `ch341` kernel module, which is **not compiled into NVIDIA's Jetson tegra kernel** (5.15.148-tegra).

**Attempted first:** `sudo modprobe ch341` → "Module not found". The module simply doesn't exist in the Jetson kernel image.

**Also blocking:** `brltty` (Braille accessibility daemon) had a udev rule (`/etc/udev/rules.d/85-brltty.rules`) that claimed the CH340 USB device before `ch341` could bind it.

**Solution:**
1. Remove brltty: `sudo apt remove -y brltty`
2. Download `ch341.c` from the matching Linux 5.15 kernel source
3. Build `ch341.ko` against Jetson kernel headers at `/usr/src/linux-headers-5.15.148-tegra-ubuntu22.04_aarch64/`
4. Load: `sudo modprobe usbserial && sudo insmod ch341.ko`
5. Manual bind if needed: `echo "1-2.x:1.0" | sudo tee /sys/bus/usb/drivers/ch341/bind`
6. Persistent auto-bind via udev rule in `install_udev.sh`

---

### 3. Compiling Arduino sketches without Arduino IDE on Jetson

**Problem:** The Jetson has `avr-gcc` and `avrdude` installed (from the `arduino` package) but the Arduino IDE is unavailable or impractical. Building `.ino` files manually has many pitfalls.

**Issues encountered during manual build:**
- `avr-gcc-ar`: LTO (Link Time Optimization) plugin error → removed `-flto` entirely
- `Arduino.h` not found in sketch → prepend `#include <Arduino.h>` before compilation
- `.S` assembly files (`wiring_pulse.S`) not compiled → added `.S` compilation loop
- Duplicate symbol `wiring_pulse.o` from both `.c` and `.S` → renamed: `core_*.o` for C/C++, `core_*_asm.o` for `.S`
- `pulseIn` undefined reference → linking `core.a` archive had resolution issues → switched to linking all `.o` files directly

**Solution:** `ch341_build/build_servo_serial.sh` — a self-contained build script that compiles Arduino core + Servo library + sketch and uploads via avrdude, all from the Jetson CLI.

---

### 4. Faulty servo

**Problem:** The first SG90 servo showed puzzling behavior — it would buzz and resist movement but not rotate. This consumed significant debugging time suspecting wiring, voltage, or code issues.

**Diagnosis process:**
- Direct Arduino 5V sweep → servo moved ✓ (hardware path works)
- Arduino serial → servo still not moving ✗
- Manual push: servo felt stiff and buzzed when forced → motor was energized
- The servo was holding a position but completely ignoring angle changes

**Root cause:** The servo was internally faulty. The control circuit was partially working (could hold/power the motor) but the potentiometer or gearing was damaged — it could not actually drive rotation.

**Key lesson:** Test the physical servo with a simple standalone sweep sketch (`servo_standalone.ino`) before debugging any software. If it doesn't move during standalone sweep, it's hardware.

---

### 5. MediaPipe on Jetson ARM64 (aarch64)

**Problem:** Standard `mediapipe` pip package does not always install cleanly on Jetson's ARM64 architecture.

**Finding:** `mediapipe 0.10.18` installs and runs correctly on the Jetson Orin Nano via pip. The `XNNPACK` delegate is used automatically (CPU-optimized). Processing 320×240 frames achieves **35–45fps** in practice (with full LOFI face param capture including solvePnP head pose; ~40–50fps without).

**Optimization:** `refine_landmarks=False` skips iris and detailed mesh refinement — not needed for nose tip tracking — and meaningfully reduces per-frame CPU time.

---

### 6. Servo jitter from face detection noise

**Problem:** MediaPipe's nose tip landmark fluctuates by ~±2° even when the face is perfectly still (natural detection noise). With a small dead zone (1°) and high EMA alpha (0.7), every frame produced a new servo command, causing continuous micro-twitching.

**Solution:** Tuned two parameters together:
- `EMA_ALPHA = 0.3` — heavy low-pass filter; requires several frames of consistent signal to change the output
- `DEAD_ZONE_DEG = 8` — only dispatch a serial command when the smoothed angle changes by more than 8°

This eliminates jitter when the face is stationary while still tracking deliberate head movements.

### 7. "The mic is not being detected" — a working mic, rejected on arithmetic

**Problem:** After a boot on 2026-08-09 Kai was deaf. `sess_state` sat on `disabled`, `sess_mic_live` was `false`, `sess_wake_tried` was empty, and push-to-talk answered *"didn't catch that"*. The log:

```
[mic] device 5 read as silent (rms=0.0 <= 5.0)
[mic] resolved device=0 rate=44100 ch=1 i2s=False — opening stream…
[mic] ERROR: cannot resample 44100 Hz: decimation needs an integer ratio, got 44100 -> 16000
[face_track] WARNING: shared capture unavailable after 14 attempts — falling back to per-turn
```

Both microphones were fine the entire time. `arecord` on the raw I2S device returned a strong signal, and the USB dongle probed live on the first try.

**Two faults, and the second is what made it total:**

1. The INMP441 read as exact digital silence on the boot probe. It is a warm-up race, not a fault — the same device on the same route reads rms 124–435 when probed a second later, and `arecord` never saw silence at all. But one bad 0.3 s read condemned the preferred mic for the whole life of the process.
2. The USB fallback was **unusable by construction**. `resolve_input_device()` returned the rate ALSA advertises (44100), and `MicStream` resamples with an integer-ratio decimator, so `Decimator(44100 → 16000)` raised, `MicStream.open()` returned `False`, and `ConversationSession.start()` returned `False`. No capture stream at all — which is why a *silent* mic presented as *no* mic.

The advertised rate was never a capability. `arecord -D hw:0,0 --dump-hw-params` reports `S16_LE mono, RATE: [44100 48000]`: 16 kHz cannot be opened on that dongle and 44100 cannot be resampled. **48000 was available and usable the whole time**, and nothing in the advertised rate said so.

**Solution:**
- `FALLBACK_CAPTURE_RATES` (`config/voice.py`) — non-I2S devices are only ever offered rates that divide into `SAMPLE_RATE`, and the liveness probe, which opens the device for real, decides which one the hardware accepts. A device that opens at none of them is skipped rather than returned; returning it is the failure above.
- `I2S_PROBE_SILENT_RETRIES` — a *silent* I2S read is retried a few times before the mic is written off. Only silence is retried; a device that refuses to open or hangs has given a definite answer, and re-asking would multiply `LIVE_PROBE_TIMEOUT_S` on the session start path.

**The lesson worth keeping:** `default_samplerate` is a hint, not a capability, and the only honest test of a capture device is opening it. The rate the driver advertised was the one rate the pipeline could not use.

---

## R&D Findings

### Jetson GPIO voltage is a hard constraint
There is no software workaround for the 3.3V GPIO limit. PWM duty cycle, frequency, and signal shape are all correct at 3.3V — the issue is purely voltage level. Any servo requiring 5V signal needs a level shifter or a microcontroller bridge.

### pulseIn() on Arduino is unreliable with 3.3V input
Even with a correct 1500µs PWM signal from the Jetson, Arduino's `pulseIn()` consistently read ~535µs due to marginal voltage detection. The signal physically crossed the digital threshold but noise caused early false-low detection. This approach was abandoned in favour of serial communication.

### Serial communication is more reliable than PWM passthrough
Sending integer angle values over USB serial (115200 baud) is deterministic, noise-immune, and removes all analog signal issues. The Arduino parses `"90\n"` and calls `sg90.write(90)`. Latency is under 5ms.

### MediaPipe FaceMesh is viable on Jetson Orin Nano at 320×240
At full 640×480 resolution, MediaPipe struggles to maintain real-time throughput on CPU. Resizing to 320×240 before processing gives ~25–30fps with acceptable landmark accuracy. Nose tip (landmark #1) is stable enough for servo tracking.

### EMA + dead zone vs PD controller
v1 used EMA (low-pass filter) + dead zone (output gate). The combination works but has two failure modes: EMA adds lag uniformly regardless of motion speed, and the dead zone causes abrupt snapping when the threshold is crossed. v2 replaces both with a PD controller: the D term naturally dampens noise without gating (no snapping), and the P term scales correction to actual error (no uniform lag). The PD approach is more principled and produces smoother, more responsive motion.

### The ch341 module must be manually compiled for Jetson
NVIDIA does not include `ch341` in the tegra kernel. The module can be compiled from Linux 5.15 kernel source against Jetson headers. `usbserial` must be loaded first as a dependency. Without a udev rule, the module loads but doesn't auto-bind to the device on hot-plug.

### Servo startup calibration must bypass smoothing logic
On startup, `send(90)` hit the dead zone check (`|90 - 90| = 0 < 8`) and silently skipped. The servo never actually centered. Fix: write directly to serial (`self._ser.write(b"90\n")`) during `__init__`, bypassing all filtering.

---

## FAQ

**Q: Why not just use the Jetson's 5V pins to power the servo signal directly?**
The Jetson 40-pin header's 5V pins (Pin 2, Pin 4) are power rails only — they cannot be used as GPIO signal outputs. GPIO pins are separate and are all 3.3V.

**Q: Why do we need the Arduino? Can't the Jetson drive the servo directly?**

> **TL;DR:** The Jetson outputs 3.3V max. The SG90 is unreliable below ~4.8V power and the signal detection is marginal at 3.3V. The Arduino outputs 5V and handles all PWM timing — the Jetson just sends a number over USB.

The Jetson Orin Nano's 40-pin GPIO outputs **3.3V maximum** on every pin — this is a hard silicon limit confirmed by NVIDIA's official documentation. The SG90 servo's rated operating voltage is **4.8V–6.0V** (per datasheet). At 3.3V two problems occur:

1. **Marginal signal detection** — the ATmega328 on the Arduino has a digital HIGH threshold of **3.0V** (= 0.6 × VCC at 5V per datasheet). A 3.3V Jetson signal only gives 0.3V of noise margin, which caused false-low edge detection during R&D. A correct 1500µs PWM signal from the Jetson was read as ~535µs by `pulseIn()`.
2. **Unreliable across servo units** — some SG90s accept a 3.3V signal when powered at 5V; others don't. The behavior is unit-dependent and not guaranteed by the datasheet. The unit tested in this R&D did not respond reliably.

Alternatives if you want to avoid the Arduino:

| Option | Notes |
|--------|-------|
| **Arduino Uno (current)** | USB-powered, 5V GPIO, `Servo` library handles PWM timing. Jetson just sends an angle over serial — no PWM code needed. |
| **Logic level shifter (BSS138)** | Converts 3.3V → 5V signal. Cheap (~$1), no extra microcontroller. But Jetson still needs to generate PWM via `lgpio`/sysfs — more complex, CPU-dependent. |
| **5V-tolerant servo** | Some servos work with 3.3V signal if powered at 5V. Unit-dependent — not guaranteed for SG90. |
| **Raspberry Pi** | GPIO is also 3.3V — same problem. |
| **ESP32** | GPIO is also 3.3V — same problem. |

The Arduino was chosen because it was already available and eliminates PWM complexity entirely.

**Q: Why does face_track.py not import from face-detection-movements?**
`face-detection-movements` has Bluetooth (BLE) dependencies (`bleak`, `dbus-fast`, `bluez-peripheral`) that are complex to install and not needed here. `face_track.py` is self-contained — it reimplements only the camera and network receiver code it needs (~60 lines).

**Q: The servo moves but in the wrong direction. How do I fix it?**
Add `--flip` for pan, `--flip-y` for tilt. These invert the nose coordinate before mapping to angle.

**Q: What happens when no face is detected?**
After 3 seconds of no face, the servo centers at 90° and stops sending commands (auto-sleep). When a face reappears, the PD controller resets and tracking resumes from center.

**Q: How do I enable Y-axis (tilt) tracking?**
Wire a second SG90 to Arduino Pin 10 (signal), 5V (power), GND. Re-upload the sketch (`build_servo_serial.sh`). Add `--tilt` to the `face_track.py` command. Use `--flip-y` if tilt direction is inverted.

**Q: The Arduino isn't detected after replug.**
Run `bash fix_usb.sh`. For permanent auto-binding: `sudo bash install_udev.sh` (run once).

**Q: How do I verify the Arduino is receiving commands?**
```bash
python3 /home/devconph/Documents/face-servo/servo_serial.py --sweep
```
Look for `OK:0` through `OK:180` in the output.

**Q: Can the laptop camera server handle multiple Jetson clients?**
No — `laptop_camera.py` accepts one connection at a time. After disconnect it waits for the next client.

---

## File Reference

```
face-servo/
├── face_track.py          Main face tracking script (Jetson)
├── voice_assistant.py     One turn: mic -> Whisper STT -> Ollama LLM -> Piper TTS + jaw
├── session.py             Hands-free: the one open mic stream + the conversation state machine
├── audio.py               Resampler, framing, pre-roll, capture buffer, the wake engine chain, VAD
├── wake_phrase.py         Fuzzy "hey kai" / bare "hey" matching over a transcript (pure stdlib)
├── tts.py                 Piper synthesis and playback (cancellable, with cached canned lines)
├── presence.py            Three-valued "is anybody there", written by face_track's inference loop
├── scripts/wake_test.py   On-device mic/VAD/wake-word diagnostic — run this before tuning anything
├── rag.py                 Document retrieval (chunking, embeddings, ranking, failsafe chain)
├── query_alias.py         Fuzzy "DEVCON" + gazetteer matching on the RAG query (pure stdlib)
├── index_documents.py     Run manually to (re)build the RAG index from documents/
├── persona.txt            Kai's editable personality — edit freely, no restart needed
├── documents/             Drop .txt/.md/.pdf files here, then run index_documents.py
│   ├── devcon_primer.txt  Pinned last-resort facts — injected when retrieval finds nothing
│   └── .rag_index.json    Generated by index_documents.py — do not hand-edit
├── laptop_camera.py       TCP camera server (laptop)
├── servo_serial.py        Manual servo control / sweep test
├── servo_diag.py          Slow diagnostic sweep (position verification)
├── fix_usb.sh             One-shot CH340 USB bind
├── install_udev.sh        Persistent udev auto-bind rule
├── README.md              This file
├── arduino/
│   ├── servo_serial/
│   │   └── servo_serial.ino     Serial-controlled servo (active sketch)
│   └── servo_standalone/
│       └── servo_standalone.ino Standalone sweep (hardware test)
└── ch341_build/
    ├── ch341.c                  CH340 kernel module source (Linux 5.15)
    ├── ch341.ko                 Compiled kernel module
    ├── Makefile                 Kernel module build
    ├── build_servo_serial.sh    Compile + upload servo_serial.ino
    └── build_standalone.sh      Compile + upload servo_standalone.ino
```
