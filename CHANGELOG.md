# Changelog

Notable changes to Kai, newest first.

Entries dated **2026-08-07 and earlier** were reconstructed from the running log that used to live
in `README.md`'s TL;DR section — they are reproduced verbatim, so their wording, tense and any
claims they make are as originally written. Anything they say about the code reflects the state on
that date, not today's.

The style is deliberate: each entry says what changed, and — where it was hard-won — *why*, with the
measurement that justified it. That is the same convention the code comments follow, and it is the
reason this file is worth keeping by hand rather than generating from commit subjects.

Conventions:

- **Newest first.** Multiple entries on one date stay in the order they were originally written.
- One heading per change, not per commit. A change that took six commits gets one entry.
- Behaviour, measurements and reverts belong here. Refactors with no observable effect do not,
  unless they change how the thing is operated or debugged.

---

## 2026-08-11 — One corrupted byte on the servo wire could slam the head into its stop

Suite green: 1244 passed (was 1239). Implements
[R4](docs/tickets/R4-firmware-servo-limits-mismatch.md). **Firmware — inert until the Arduino is
flashed, and not compiled here: no Arduino toolchain was available on either box.**

- **The limit that protects the SG90 existed only on the host.** `config/servo.py`'s `SERVO_MIN`/
  `SERVO_MAX` (10/170) are applied in `servo/servo.py`'s `send()` and `send_jaw()`, but the sketch
  constrained to the full `0..180` in all three of its `constrain()` calls. The host's clamp is
  applied and then *destroyed in transit* if the line corrupts, and the link is fire-and-forget by
  design — no checksum, no echo, no ack — so nothing downstream can tell a mangled line from a real
  one. `ANGLE_MIN`/`ANGLE_MAX` now live in the sketch, which is the copy that survives the wire.
- **`String::toInt()` returns 0 for anything it cannot parse, and 0 is the worst value on this
  wire.** Not an inert default — a hard slam to the end of travel. So the one input the old parser
  could not report was also the most damaging thing it could command, and the CH340 is documented as
  flapping under servo brownout, which makes corruption *correlated* with the condition that makes a
  stall worse: the stall current from a slam to 0 lands on the same rail whose sag caused the flap.
  `parseAngle()` replaces it — rejects an empty field, any non-digit, and anything over 3 digits.
- **A line is now applied whole or not at all.** Every field is parsed before any servo is written,
  so a good pan with a corrupt jaw moves nothing. The tilt field is validated and then thrown away:
  there is no tilt hardware (R10), but garbage in tilt means the *line* is corrupt, and the pan
  field sitting beside it has no better claim to being intact.
- **The "keep these in step with config/servo.py" comment is backed by a test.** R4 asked only for
  the comment. `tests/test_servo.py::TestFirmwareAngleLimits` reads the real `.ino` and fails if the
  constants drift, if a full-range `constrain(..., 0, 180)` reappears, or if `toInt()` comes back.
  It strips C++ comments before searching — the first version matched this sketch's own explanation
  of why `toInt()` is wrong and failed on the change that fixed it.

What is still true: the host-side clamps are untouched, so this is defence in depth rather than a
relocation, and `G:` gesture lines and the `J` fast channel dispatch exactly as before.

**Compiled and flashed the same day, after an earlier note here wrongly said there was no Arduino
toolchain.** That check had been run on the Windows dev box rather than the robot, and it searched
for `gcc-avr` — a Debian package name, never a binary; the binary is `avr-gcc`. The Jetson has had
`avr-gcc`, `avrdude 6.3`, `arduino-builder` and `arduino-core-avr` all along. The new firmware is
**6122 bytes against the old 6262** — `parseAngle()` costs less than the `String::toInt()`
instantiations it removes — with zero warnings under `-warnings all` and RAM unchanged at 262 bytes.

The board had to be probed, because it enumerates as a bare CH340 (`1a86:7523`) with no Arduino
VID/PID: **ATmega328P, signature `0x1e950f`, optiboot at 115200** (57600 and 19200 do not sync).
Flash verified twice — avrdude's own verify plus an independent readback diff, 0 of 6122 bytes
mismatched — and the previous firmware was read off the chip beforehand and kept at
`~/firmware-backups/servo_serial-PRE-R4-20260811-083436.hex`.

On the live link the new firmware boots to `READY`, accepts every legal form, survives empty fields,
letters for digits, signed values, run-together lines, raw binary noise and truncated numbers, and
still answers a reset with `READY` afterwards — so the parser does not wedge, which is the failure
mode a hand-rolled one would actually have. **What could not be checked remotely is that the rejected
lines produced no motion**: the link is fire-and-forget with no echo, so the board says nothing that
distinguishes "rejected" from "moved". That half needs someone watching the head.

## 2026-08-10 — Kai forgot your name six questions after you gave it

Suite green: 1239 passed, 2705 subtests (was 1190, 2700). Implements
[S12](docs/tickets/S12-no-identity-within-a-session.md); deployed and exercised on the robot.

Two things the live run turned up that the tests could not, both recorded in the ticket: Whisper
mishears names (`[identity] talking to 'Jandal'` was a correct extraction of a misheard "Jhondel",
which nothing in this design defends against), and the model uses the name more often than
`IDENTITY_PROMPT` asks it to.

- **A name offered in speech was an ordinary history turn, so the rolling cap evicted it.** The
  prompt is `system + capped history + user turn` (`ai/llm.build_chat_messages`) with no slot for a
  fact about the *speaker*, so "I'm Jhondel" landed in `_history` and was dropped by
  `MAX_HISTORY_TURNS = 6` exchanges later — unrecoverable, because nothing had extracted it. Kai now
  pins the first name it is offered on `VoiceAssistant._person_name` and appends `IDENTITY_PROMPT` to
  the system prompt, so it outlives the window entirely. Published as `sess_person` on `/params`.
- **Extraction is a regex, not a second LLM call.** `ai/identity.py` is pure stdlib, in the shape of
  `ai/wake_phrase.py` and for the same reason — the bugs here are all false accepts and casing, so it
  must be testable with plain strings. Asking Ollama to pull the name out would put another
  round-trip on the path [R5](docs/tickets/R5-serialised-first-audio-latency.md) exists to shorten.
- **The anchors are two-tier, because the risk is not symmetric.** A missed name costs nothing; a
  wrong name is said out loud to somebody standing in front of the robot. Strong anchors ("my name
  is X", "call me X", "ako si X" — `si` is a personal-name marker, introducing a name is its
  grammatical job) are taken as-is. The weak tier ("I'm X") is accepted only with a capitalised
  first letter *and* a miss on `IDENTITY_STOPWORDS`, which is what separates "I'm Jhondel" from
  "I'm fine", "I'm from Cebu" and "I'm a developer".
- **The name is session-scoped, on the seam that already existed.** `reset_history()` clears it
  alongside the rolling history and the sticky RAG topic, because all three answer the same question
  — what may the next person inherit? Nothing. It survives a "hey Kai" landing in `LISTEN_WAIT`,
  matching the history rule there, and `note_identity()` is epoch-guarded so a session that ended
  while STT was still running cannot hand its name to whoever is next.
- **Both conversational paths capture, which took a live run to find.** `note_identity()` was hooked
  only into `_process()`, the mic-turn path. The **one-breath** turn — "Hey Kai, my name is Jhondel"
  said without pausing — runs through `say()` instead, because the whisper wake tier already holds
  the transcript. On the robot, `sess_person` stayed `''` while Kai cheerfully replied "Hi Jhondel!":
  the model read the name out of the user turn, which looks identical from outside and pins nothing.
  So the same sentence captured or did not purely on whether the speaker drew breath. Now hooked in
  both, gated on `use_llm` so the verbatim `/voice/say` route cannot make Kai think it is talking to
  itself.
- **The prompt placement is the opposite call to the RAG context, deliberately — and the reasoning
  behind it is currently unmeasurable.** `RAG_CONTEXT_PLACEMENT = "user"` keeps per-turn-varying text
  out of the cached prefix; this string does not vary once learned, so the system position should
  cost one invalidation and nothing after. On the robot it cannot be confirmed: every `[llm] turn:`
  line is preceded by `MODEL RELOADED: ~200-360ms — placement was re-decided`, so **no KV prefix
  survives between turns at all** and there is nothing to invalidate. What was measured is that the
  injection costs nothing detectable — 258-304 ms prompt eval with a name pinned, inside the
  215-465 ms spread without one. Noted at the constant in `config/voice.py`, to be re-measured if the
  per-turn reload is ever fixed. That reload also makes `RAG_CONTEXT_PLACEMENT`'s optimisation inert.
- **`ai/persona.txt` now says to use the conversation it already has.** One line, no code: refer back
  to what was said earlier, notice a repeated question, pick up a dropped thread — and never claim to
  remember an earlier *visit*, which would be a lie the history cannot support. `load_persona()`
  re-reads the file on every call, so this is revertible on the live robot without a restart.

## 2026-08-10 — Long replies were being cut off mid-sentence, with the jaw still moving

Two independent bugs behind one symptom. Suite green: 1180 passed, 2675 subtests (was 1173).

- **`STATE_SPEAKING` guillotined every reply at 20 s.** `_enter_speaking` armed
  `SESSION_SPEAK_MAX_UNKNOWN_S` unconditionally, and that was the only deadline a *healthy* reply
  ever got — `SESSION_SPEAK_GRACE_S`, commented "allowed overrun past the WAV's own duration",
  reached only the canned branch. The clock also starts before Piper does, because `on_done` fires
  from the turn worker the moment the reply text exists. Meanwhile `TTS_MAX_SPOKEN_CHARS` (500)
  allows ~90 words — about 31 s at `SPEAK_SEC_PER_WORD`. So any answer past ~18 s was cut mid-word
  by the guard against a *wedged* paplay. `VoiceAssistant.audio_ends_at()` now publishes the WAV's
  measured end and `session._speaking_deadline()` prefers it, falling back to the 20 s cap only
  when no length is known — a pantomime, an unreadable header, or the synthesis window. The
  backstop is intact: a wedged paplay either publishes no end time or overruns the one it did.
- **Cut audio left the jaw miming on.** The jaw is a `(start, segments)` schedule that
  `face_track.py` reads every frame; `tts.stop()` only kills the subprocess, and the sole reset in
  the class was `start_recording()`'s, covering push-to-talk alone. So a filler cut mid-word by an
  arriving reply went on mouthing the rest of its sentence in silence — up to `FILLER_MAX_LINE_S`,
  plus the 0.5–1.5 s Piper run before the reply had a window of its own. `_begin_speech()` now
  retires the outgoing schedule at the one seam every speech path already passes through, and
  `stop_speech()` pairs the two for the four sites in `ai/session.py` that cut audio directly
  (ack timeout, speak timeout, push-to-talk interrupt, session end).

Clearing the stale end time in `_begin_speech` is load-bearing for the first fix: `_enter_speaking`
arms from `on_done`, which fires after `_speak()` has claimed the speaker but before its worker
knows a duration. A left-over end time from the previous line would cut the new one instantly.

## 2026-08-10 — Documentation restructure

Docs only — no code behaviour changed. The full test suite is green before and after
(1173 passed, 2675 subtests).

- **`README.md` is now an overview only.** It had grown to 1207 lines and carried the running
  update log, the full setup guide, the tuning reference, the R&D write-ups and the FAQ inline.
  Every section moved to `docs/` verbatim; the README keeps the orientation and links out.
- **This changelog was created**, from the dated entries that used to live in the README's TL;DR.
  They are reproduced verbatim and re-ordered newest-first.
- **`docs/plan/` gained `completed/` and `wip/`.** The six planning documents were filed by
  whether work remains against them; only `expressive-voice-plan.md` is finished (concluded at its
  own abort gate). See `docs/plan/README.md` for what is outstanding in each.
- **`docs/tickets/` added** — 24 implementation-ready tickets from a two-lens (robotics +
  software) codebase review, grouped into four tiers by severity x inverse effort. Nothing in them
  is implemented. See `docs/tickets/README.md`.
- Comment references to the moved documents were updated in `ai/delivery.py`, `config/voice.py`,
  `config/rag.py`, `scripts/rag_eval.py`, `scripts/tts_setup_models.sh`,
  `scripts/tts_setup_kokoro.sh` and `tests/test_voice_assistant.py`.

## 2026-08-07 — A DEVCON question can no longer come back empty
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


## 2026-08-07 — Bare "hey" wakes Kai on the Whisper tier
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


## 2026-08-07 — Why the wake word barely worked — three bugs, none of them in the matcher
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


## 2026-08-06 — Documents first, and "DEVCON" by ear
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


## 2026-07-28 — Wake-word fallback chain
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


## 2026-07-28 — Hands-free conversation — "Hey Kai"
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


## 2026-07-06 — Voice assistant
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


## 2026-07-06 — RAG + editable persona
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


## 2026-06-17 — Post-development additions
- LOFI face parameter capture — yaw, pitch, roll, mouth, eyes, smile/kiss, distance; same algorithm as face-detection-movements; use `--lofi` flag for 19-digit output string
- Auto USB driver loading — `face_track.py` now loads `ch341.ko` automatically if `/dev/ttyUSB0` is missing; no manual `modprobe` step needed
- All files consolidated into the `face-servo` directory


## 2026-06-15 → 2026-06-17 — Initial R&D

- **2026-06-15** — First hardware test. Ran into issues early: missing wires and servo condition not checked beforehand. Session postponed to the following day.
- **2026-06-16** — Main R&D day. Got everything working — built the full face tracking pipeline, solved all hardware and software challenges, completed most of the development.
- **2026-06-17** — Post-development. Consolidated findings, wrote the README and documentation, created the TL;DR, implemented Y-axis tilt (code complete, untested — only 1 working servo available), added LOFI-compatible face parameter capture (yaw, pitch, roll, mouth, eyes, smile, distance — same algorithm as face-detection-movements), auto USB driver loading on startup (no more manual `modprobe` step), and cleaned up the project structure.

