"""Hands-free conversation: "Hey Kai" wake word, always-open capture, VAD turn-taking, sessions.
Consumed by ai/audio.py and ai/session.py. (SessionState strings live in ai/session.py — they are
projected onto the dashboard's voice_status wire contract, so they're structural, not tuning.)

Push-to-talk tuning stays in voice.py; this file only covers what makes Kai listen on his own.
"""

# ── Master switches ──────────────────────────────────────────────────────────────
# HANDS_FREE_ENABLED is the rollback lever: False gives exactly the old push-to-talk behaviour
# (no wake word, no VAD, no session timers) while still using the shared always-open stream.
# MIC_LEGACY_CAPTURE goes one step further and restores the original per-turn sd.InputStream.
HANDS_FREE_ENABLED = True
MIC_LEGACY_CAPTURE = False

# ── Wake word (Porcupine) ────────────────────────────────────────────────────────
# The access key is NEVER stored here — config/ is committed. ai/audio.py resolves it from the
# environment first, then the key file; with neither, hands-free stays off and push-to-talk is
# unaffected. The file is the primary source because scripts/autostart.sh runs under @reboot cron
# with no login shell, so an `export` would have to live in a tracked file.
WAKE_ACCESS_KEY_ENV  = "PICOVOICE_ACCESS_KEY"
WAKE_ACCESS_KEY_FILE = "~/.config/kai/porcupine.key"

# Custom "Hey Kai" keyword from console.picovoice.ai. The .ppn is PLATFORM-SPECIFIC: a Windows or
# macOS file raises PorcupineInvalidArgumentError on the Jetson — generate the ARM/Linux (aarch64)
# variant. Path is relative to the project root so it survives any cwd (cf. TTS_VOICE_MODEL).
WAKE_KEYWORD_PATHS = ("wake/hey-kai.ppn",)
WAKE_MODEL_PATH    = None          # None = Porcupine's bundled English model

# pvporcupine chooses its bundled library by matching /proc/cpuinfo's "CPU part" against a short
# hardcoded table, and RAISES at import time on anything else. The Jetson Orin Nano's Cortex-A78AE
# reports 0xd42, which is in NO published version (checked 2.1 through 4.0) — so without this the
# wake word can never load on this board. 0xd0b is Cortex-A76; that bundled build runs correctly on
# an A78 (same ARMv8.2-A baseline). Only used as a FALLBACK, after a normal import has failed
# specifically on CPU detection, so it is a no-op on hardware pvporcupine already knows.
# Set to None to disable the workaround entirely.
WAKE_CPU_PART_OVERRIDE = "0xd0b"
WAKE_SENSITIVITIES = (0.5,)        # 0..1 per keyword; higher = more detections AND more false accepts
# Porcupine's own frame size. Not a tunable — asserted against the live handle at startup, and
# CAPTURE_BLOCKSIZE below is chosen as an exact multiple of it.
WAKE_FRAME_LENGTH  = 512
# A drawn-out "Heeey Kaaai" can trip the detector twice. Same cooldown idiom as vision/gesture.py.
WAKE_REFRACTORY_S  = 1.5
# Acoustic barge-in: interrupting Kai mid-reply by voice. Off, and not merely untuned — there is no
# echo cancellation and the mic and speaker share the chassis, so an open mic during playback hears
# Kai far louder than the human. Pressing the dashboard mic button DOES interrupt (unambiguous
# intent). Flipping this on only ungates the wake word, never the VAD.
WAKE_ALLOW_BARGE_IN = False

# ── Wake engine fallback chain ───────────────────────────────────────────────────
# Tried in order at startup; the first tier that initializes wins and the rest are never even
# constructed. Kai must never be left without hands-free if ANY tier can run, so a tier failing is a
# logged reason (see sess_wake_tried on /params) — never an exception, never a hard stop.
#
# Why a chain at all: Porcupine has three independent ways to be unavailable — it needs a cloud
# account and key, it needs a .ppn built for this exact platform, and it does not list this CPU
# (see WAKE_CPU_PART_OVERRIDE above). Depending on all three holding forever is not a plan.
#   porcupine     frame-level, 512 samples. Best latency and accuracy. Most setup.
#   openwakeword  frame-level, 1280 samples. No account, no per-platform blob. Needs a trained model.
#   whisper       utterance-level. No setup at all, but decides only AFTER an utterance (see below).
WAKE_ENGINE_ORDER = ("porcupine", "openwakeword", "whisper")
WAKE_ENGINE_FORCE = None        # pin one tier by name, bypassing the order (debugging only)

# ── Tier 2: openWakeWord ─────────────────────────────────────────────────────────
# Custom-trained "hey kai" model — see wake/README.md for the training recipe. Relative to project root.
WAKE_OWW_MODEL_PATH   = "wake/hey_kai.onnx"
WAKE_OWW_THRESHOLD    = 0.5     # on the model's own 0..1 score; raise if it false-accepts
# openWakeWord's fixed frame: 80 ms @ 16 kHz. NOT a tunable — the model's input shape.
# Note CAPTURE_BLOCKSIZE (1536) is 3x512 but only 1.2x1280, so with this tier the frame assembler
# genuinely carries a remainder and wake latency quantizes to 80 ms. That is correct; do not "fix"
# CAPTURE_BLOCKSIZE, it is pinned by the 48 kHz -> 16 kHz integer decimation ratio.
WAKE_OWW_FRAME_LENGTH = 1280
# openWakeWord's shared front-end (melspectrogram + speech embedding) is DOWNLOADED on first use. A
# startup that reaches for the network is a startup that hangs, so ai/audio.py refuses this tier
# unless both files already exist on disk. None = look inside the installed package; set explicit
# paths if you pre-downloaded them elsewhere. See wake/README.md.
WAKE_OWW_MELSPEC_PATH   = None
WAKE_OWW_EMBEDDING_PATH = None
# Its optional backends are absent here on purpose: no tflite-runtime wheel for aarch64/py3.10, and
# speexdsp isn't installed. Forced off rather than probed. Its own VAD needs silero — also not shipped.
WAKE_OWW_FRAMEWORK      = "onnx"
WAKE_OWW_NOISE_SUPPRESS = False
WAKE_OWW_VAD_THRESHOLD  = 0.0   # 0 = off

# ── Tier 3: Whisper phrase spotting (utterance-level) ────────────────────────────
# The last resort: no extra model, no key, no download — it reuses the faster-whisper instance that
# is already resident and pre-warmed for turns. Fundamentally different from tiers 1-2: it can only
# decide AFTER a complete utterance, so it costs a whole STT run per nearby utterance and adds its
# check time before the ack. Everything below exists to bound that cost.
#
# It also transcribes speech nobody addressed to Kai (locally, nothing stored) in order to look for
# the phrase. Set False to prefer push-to-talk over that.
WAKE_WHISPER_ENABLED = True
# A SEPARATE, much smaller model for spotting only — turns keep WHISPER_MODEL ("small") for quality.
# This is the difference between the tier working and not. Measured on the robot 2026-07-29, int8 on
# 4 threads over a 1.6 s clip:
#     small  3172 ms      base  1033 ms      tiny  564 ms
# At "small" a real check took 7.4 s and hit WAKE_WHISPER_CHECK_MAX_S, so nothing ever matched.
# Decoding tweaks (greedy, fixed language, no VAD filter) made NO difference — model size is the
# only lever that matters here.
# The trade-off is honest: "tiny" is much weaker, especially on Tagalog. It only has to recognise two
# known words, and ai/wake_phrase.py matches fuzzily with aliases, so that is an acceptable job for
# it. ~75 MB resident. Set None to reuse the turn model (and expect a multi-second wake).
WAKE_WHISPER_SCAN_MODEL = "tiny"
# Force the SCAN to English. With language=None, "tiny" guesses wildly on 1-3 s clips — observed on
# the robot 2026-07-29 decoding "hey kai" as '嘿哀', 'Hẹc gai!', Norwegian and Spanish. The wake phrase
# is English either way, so pinning it removes a whole class of misses, and it keeps this
# latency-critical path to exactly one pass (WHISPER_LANGUAGES restriction can cost a second one).
#
# Set None to fall back to the restricted auto-detect in config/voice.py (WHISPER_LANGUAGES) instead.
# That is what to change if you want a Tagalog command spoken in ONE BREATH ("hey kai, anong oras
# na?") transcribed properly — at the cost of a slower and less reliable wake. The TWO-STEP path
# ("Hey Kai" ... wait for "Yes?" ... then speak) is already fully bilingual either way, because the
# command there is transcribed by the turn model, not by this one.
WAKE_WHISPER_SCAN_LANGUAGE = "en"
# Only utterances inside this band are transcribed at all. Below the floor is a blip. Above the
# ceiling is someone mid-conversation, not someone saying a wake phrase — discarded WITHOUT running
# Whisper, which is what stops a room full of talking from pegging a core.
#
# The floor is 0.15, NOT the 0.35 that MIN_UTTERANCE_S uses for turns. It was 0.35 (copied from that
# constant) and that silently ate the wake word once WAKE_PHRASE_SOLO_PREFIXES made a bare "hey"
# enough: "Hey Kai" is ~0.65 s of voiced audio and cleared it easily, but "hey" on its own is
# 0.25-0.35 s — sitting exactly ON the threshold, so a crisp one was discarded before Whisper ever
# ran. sess_scan_skip_short is the counter that shows it happening.
#
# The cost of lowering it is more Whisper runs on noise blips. That is bounded by
# WAKE_WHISPER_COOLDOWN_S below and is the cheap direction of the trade: a discarded wake is the
# feature not working.
WAKE_WHISPER_MIN_UTTERANCE_S = 0.15
WAKE_WHISPER_MAX_UTTERANCE_S = 6.0
# Minimum gap between checks, from the END of the previous one. Bounds the duty cycle.
WAKE_WHISPER_COOLDOWN_S      = 1.0
# Longer back-off after an utterance blew the ceiling: someone is talking continuously, so don't
# re-arm into the middle of their next sentence.
WAKE_WHISPER_LONG_COOLDOWN_S = 3.0
# More words than this cannot be a wake phrase plus a short command; skip matching entirely.
WAKE_WHISPER_MAX_WORDS       = 24
# Backstop on the check worker, well above a measured check (~0.4-1.0 s for a 1-2 s clip on this
# board) so it only fires when ctranslate2 is wedged in native code, where nothing raises.
WAKE_WHISPER_CHECK_MAX_S     = 8.0
# Logging non-matching transcripts means putting speech nobody addressed to Kai into
# /tmp/face-servo.log. Off by default, and truncated when on.
WAKE_WHISPER_LOG_TEXT        = False
WAKE_WHISPER_LOG_CHARS       = 60

# ── Wake phrase matching (Whisper tier only) ─────────────────────────────────────
# faster-whisper renders the same two words as "Hey, Kai.", "Hey Ky", "hey chi", "Hi Kai",
# "Hey. Kai." — and with WHISPER_LANGUAGE=None it sometimes decodes as Tagalog. Exact matching is
# useless, and `"kai" in text` is WORSE than useless: it fires on "kaya", "okay", "kayo", and on
# anyone mentioning Kai in the third person. So: match a PREFIX word followed by a NAME word, near
# the START of the utterance.
WAKE_PHRASE_PREFIXES = ("hey", "hi", "hoy", "oy", "okay", "ok", "ey")
# "kye" is deliberately NOT here: it scores 0.86 against "Kyle" and "Kaye", both common names, so
# including it made "hey Kyle can you help" wake the robot. Measured, not assumed.
#
# "guy" IS here because that is what the tiny scan model actually produces for "Kai" — observed
# repeatedly on the robot ('Hey guys!', 'Hey, Guy.', 'Hey guy'). One entry is enough: "guys" scores
# 0.86 against it while "gus" (0.67), "gail" (0.29), "greg", "girls" and "guess" all fall below the
# threshold. Listing "guys" and "gai" separately as well DID drag in "hey Gail" and "hey Gus" —
# measured, so don't re-add them.
# The cost is that a genuine "hey guys" said to the room wakes Kai: an ack and a listening window that
# self-ends. A false reject, by contrast, means the feature does nothing at all. The real fix is tier 2
# (openWakeWord), which spots the phrase acoustically and doesn't care how Whisper spells it.
WAKE_PHRASE_NAMES    = ("kai", "ky", "kay", "cai", "chi", "chai", "tsai", "kaii", "guy")
# Tagalog high-frequency ka- words that difflib scores dangerously close to "kai". Rejected
# outright, before any ratio is computed. Expect to add to this after real-world tuning.
WAKE_PHRASE_BLOCKLIST = ("kaya", "kayo", "kami", "kanya", "kailan", "kasi", "kahit", "kailangan")
# Words that must never be accepted in the PREFIX slot. "kay" is the Tagalog linker — "sabihin mo
# kay Kai" means "tell Kai", i.e. talking *about* Kai in the third person, and it scores 0.86 against
# "okay" so the ratio alone lets it through. Measured, not guessed.
WAKE_PHRASE_PREFIX_BLOCKLIST = ("kay", "kai", "ka", "ni", "si")
WAKE_PHRASE_PREFIX_RATIO = 0.75    # difflib ratio for the prefix token
WAKE_PHRASE_NAME_RATIO   = 0.72    # difflib ratio for the name token
WAKE_PHRASE_JOINED_RATIO = 0.82    # for one-token renderings: "heykai", "haykai"
# The phrase must begin within this many tokens of the start. This single constraint removes most
# false accepts: "sabihin mo kay Kai" and "...and then Kai said..." cannot fire.
WAKE_PHRASE_SCAN_TOKENS  = 3

# ── Bare-prefix wake ("hey" with no name) ────────────────────────────────────────
# The name slot is where this tier loses wakes. "tiny" renders "Kai" as guy / gai / chi / 嘿哀 /
# whatever it feels like, and WAKE_PHRASE_NAMES above is a running list of renderings we discovered
# by being ignored first. The prefix, by contrast, is one very common English word that "tiny" gets
# right — so accepting it ALONE trades false accepts for the false rejects that actually hurt.
#
# Set to () to restore strict two-word matching. Only the WHISPER tier honours this: tiers 1-2 spot
# "hey kai" acoustically from a trained blob and cannot be widened from config (see wake/README.md).
# That asymmetry is the reason to consider WAKE_ENGINE_FORCE = "whisper" while evaluating this —
# otherwise which phrase works depends on which tier won at startup.
#
# Deliberately NOT the full WAKE_PHRASE_PREFIXES list. "okay"/"ok"/"oy"/"ey" open ordinary sentences
# ("okay, so what happened was...") and would fire constantly; "hi" is how people greet each other in
# the room. "hey" alone is close enough to always-addressing-someone to be worth the trade.
WAKE_PHRASE_SOLO_PREFIXES = ("hey",)
# Matched EXACTLY, not by difflib ratio — unlike every other slot in this file. Measured: "they"
# scores 0.857 against "hey", which is the identical score to "heyy", a drawn-out real wake. At three
# characters difflib cannot separate them at ANY threshold, and with no second token there is nothing
# left to disconfirm a bad guess. So form C matches literally, after collapsing repeated letters
# ("heyy"/"heeey" -> "hey"), which is the only variation that actually needs absorbing here.
# Add renderings to the tuple above as you observe them; do not reach for a ratio.
# Bare "hey" must start the utterance. This is the whole safety argument: "hey" at token 0 is
# addressing someone, "hey" mid-sentence ("...and I was like hey, no") is conversation. Kept
# separate from WAKE_PHRASE_SCAN_TOKENS (3) because one token of evidence deserves a tighter window
# than two. Raising this above 1 gives up most of the guard — measure before you do.
WAKE_PHRASE_SOLO_SCAN_TOKENS = 1

# ── Wake acknowledgement ─────────────────────────────────────────────────────────
# Kai answers "Hey Kai" with a short canned line, then listens. Synthesized ONCE at startup into
# ACK_WAV_DIR: running Piper per wake would put 0.5-1.5 s of dead air between the wake word and the
# ack, which is most of what makes hands-free feel broken. Set ACK_PRESYNTH False to synthesize on
# demand instead (debugging only).
WAKE_ACK_TEXT  = "Yes?"
WAKE_ACK_MAX_S = 3.0               # ACK state gives up after this even if playback never reports done
ACK_WAV_DIR    = "/tmp/kai_ack"
ACK_PRESYNTH   = True

# The canned replies for a failed turn. NO_SPEECH_RESPONSE in voice_assistant.py stays as-is for the
# dashboard, but it is UI text — "(didn't catch that — try again)" read aloud by espeak-ng voices the
# parentheses. These are what actually get spoken, and they're cached alongside the ack.
CANNED_NO_SPEECH = "Sorry, I didn't catch that."
CANNED_ERROR     = "Sorry, something went wrong."

# ── Startup greeting ─────────────────────────────────────────────────────────────
# Said ONCE per process, shortly after the service comes up (ai/session._warm_all). Two jobs, and
# the second is the reason it earns its place: it tells whoever is standing there that this is Kai
# and how to address it — the wake phrase is not discoverable by looking at the robot — and it is
# an end-to-end proof that Piper, sox, the USB dongle and the amp are all working, at the moment
# where that is cheapest to notice. Before this, the first evidence that audio was alive at all was
# someone saying "Hey Kai" into a robot that then failed silently (see TTS_ASSERT_CARD_PROFILE in
# config/voice.py for how invisible that failure mode is).
#
# Spoken through speak_text(), NOT say(): no LLM call, no turn status, and so no chat bubble on the
# dashboard — this is Kai talking to the room, not a turn anybody took.
GREETING_ENABLED = True
# Keep it short and keep the wake phrase in it. It is synthesized live (once, so caching it would
# only mean re-synthesising it on every voice change for a line that will never be said again), and
# it plays while the filler bank is still cold — so a long greeting is dead air the robot cannot
# fill. Set GREETING_ENABLED False for a silent boot (a demo table, a quiet room).
#
# The middle clause is the one people at a table actually react to: a cardboard face does not look
# like it is doing anything locally, and "no cloud" is the whole point of the build. Phrased the same
# way as the loud-room filler in config/filler.py so the two never contradict each other. It costs
# roughly three extra seconds of boot audio — that is the trade, and it is paid once per process.
GREETING_TEXT = (
    "Hi, I'm Kai. Everything I think runs on an NVIDIA Jetson Orin Nano behind this face, "
    "no cloud. Say Hey Kai, and ask me anything about DEVCON."
)

# ── Always-open capture ──────────────────────────────────────────────────────────
# 1536 = 3 x WAKE_FRAME_LENGTH: at I2S_CAPTURE_RATE (48 kHz) each callback decimates to exactly one
# 512-sample Porcupine frame at 16 kHz, so there is no partial-frame buffering anywhere. 32 ms of
# latency. Raise to 3072 (2 frames/callback) if the audio thread ever starves the servo loop.
CAPTURE_BLOCKSIZE    = 1536
CAPTURE_QUEUE_BLOCKS = 32          # ~1 s; drop-oldest on overflow — the PortAudio callback never blocks
# Speech before the VAD confirms onset would otherwise be clipped off the front of the utterance.
PREROLL_S            = 0.30
# Hard ceiling on a single buffered utterance, enforced in the audio thread independently of the
# session FSM so a wedged FSM can't exhaust the Jetson's 8 GB. Drops oldest audio past this.
CAPTURE_HARD_CAP_S   = 20.0

# Anti-alias FIR for the 48 kHz -> 16 kHz decimation. 97 taps of windowed sinc, cutoff at 0.9x the
# 8 kHz output Nyquist. Plain decimation with no filter would fold fan noise and amp hiss straight
# into the speech band, which presents as "the wake word model is bad".
MIC_RESAMPLE_TAPS   = 97
MIC_RESAMPLE_CUTOFF = 7200         # Hz

# High-pass on the captured audio, applied AFTER decimation (so at 16 kHz, where a sharp low cut is
# affordable — at 48 kHz a 97-tap windowed sinc has a ~2 kHz transition and cannot place an 80 Hz
# corner at all).
#
# Why it exists: the anti-alias filter above is low-pass ONLY, so nothing ever removed the low end.
# The INMP441 carries a large standing DC offset — see MicStream._on_block, where the raw RMS
# measures roughly TWICE the DC-blocked value on this mic. That offset and the sub-100 Hz rumble it
# sits in (chassis fan, PAM8403 amp) went into Whisper untouched, and VAD_DC_BLOCK never helped
# because it only cleans the VAD's own decision, not the audio that gets transcribed.
#
# 80 Hz: below every speech fundamental worth keeping (adult male F0 starts ~85 Hz) and above where
# the rumble lives. ASR does not need F0 anyway — telephony starts at 300 Hz and is perfectly
# intelligible — so erring low here costs nothing and protects deep voices.
#
# 255 taps at 16 kHz is a ~100 Hz transition (Hamming, ~3.3*fs/N) and 8 ms of linear-phase latency.
# NOTE: removing the offset LOWERS every level this file tunes against — sess_rms, and with it
# sess_rms_ambient. The ambient adaptation absorbs the relative change automatically, but
# VAD_RMS_FLOOR is an absolute base and may now sit too high; measure sess_vad_onsets before and
# after. Set MIC_HIGHPASS_HZ = 0 to disable.
MIC_HIGHPASS_HZ   = 80
MIC_HIGHPASS_TAPS = 255
# The INMP441 on this build is QUIET. Measured 2026-07-29 on the robot: idle RMS 200-390, speech
# peaking at 1480 — only ~3.7x separation, and low enough that webrtcvad (tuned for telephony levels)
# and Whisper both do better with it lifted. Gain does not change the signal-to-noise ratio, so it
# does not by itself make the VAD more decisive — VAD_RMS_FLOOR below is scaled to match it.
# Headroom check: speech peak 1480 RMS x2 = 2960, instantaneous peaks well under int16 clipping.
MIC_INPUT_GAIN      = 2.0

# Watchdog. If pulseaudio re-grabs the suspended APE card (a settings panel, a stray pactl, a USB
# re-enumeration) the stream goes SILENT with no exception raised — without this Kai would go
# permanently deaf with no signal at all. On a stall we re-suspend pulse and reopen.
MIC_STALL_S         = 2.0
MIC_REOPEN_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)   # last value repeats
# Ceiling on the `amixer`/`pactl` helper calls used to set up and free the card. These now run on
# every mic open and every reopen, so an unresponsive pulseaudio must not be able to wedge the
# capture thread indefinitely.
MIXER_TIMEOUT_S     = 5.0
# Session start retries. The watchdog above only covers a stream that opened once and later went
# quiet. If the FIRST open loses the race for the single-opener raw device (a previous face_track
# still exiting, pulse re-grabbing the card), the session gives up permanently and Kai is deaf for
# the whole run — start() reports False before mutating any state, so retrying it is safe.
#
# Measured on the robot 2026-08-07, and the reason this is a BACKOFF rather than a flat interval:
# 5 attempts x 5 s covered only the first 25 s of startup, which is the single most contended stretch
# the Jetson ever has. That boot's log had `[llm] MODEL RELOADED: 26215ms` plus MediaPipe init and a
# HuggingFace fetch running inside exactly that window. Every attempt lost the I2S probe, Kai fell
# back to the 44.1 kHz USB dongle (which cannot be decimated to 16 kHz, so NO mic opened at all),
# and then gave up permanently — one bad 25 s at boot cost hands-free for the whole run, silently.
#
# So the schedule now reaches ~6 minutes, well past any plausible startup storm, and the early
# entries stay short so the normal transient case (a previous face_track still exiting) still
# recovers in seconds. Last value repeats, same idiom as MIC_REOPEN_BACKOFF_S above.
SESSION_START_ATTEMPTS = 14
SESSION_START_BACKOFF_S = (5.0, 5.0, 5.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0)
# Kept as the first-retry default so anything still reading it behaves as before.
SESSION_START_RETRY_S  = 5.0

# ── Turn end (webrtcvad) ─────────────────────────────────────────────────────────
# webrtcvad was tuned for telephony-level speech; the INMP441 is a quiet MEMS mic with a DC offset,
# so is_speech() alone false-triggers on fan and amp hiss. It is ANDed with an RMS floor and
# requires several consecutive speech frames before an utterance opens.
# 0..3, where 3 rejects the most. Measured on the robot 2026-07-29: at aggressiveness 2 webrtcvad
# calls 26% of frames speech vs 21% at 3, on audio that was ~25% speech — so 2 tracks reality here
# while 3 throws away real speech. The RMS floor below does the noise rejection; this half only needs
# to be roughly right.
VAD_AGGRESSIVENESS = 2
VAD_FRAME_MS       = 20            # webrtcvad accepts 10 / 20 / 30 only -> 320 samples @ 16 kHz
VAD_DC_BLOCK       = True          # one-pole DC blocker ahead of the VAD (MEMS mics carry offset)
# int16 RMS of a DC-BLOCKED 20 ms frame, after MIC_INPUT_GAIN. Compare against sess_rms on /params,
# which is measured the same way on purpose.
#
# Measured per-frame distribution on the robot, 2026-07-29, at gain 2.0 over 40 s of ~25% speech:
#   p50=124  p75=191  p90=505  p95=742  p99=956  max=1220
# So 350 sits ~2-3x above the ambient bulk while still passing the top ~12% of frames, which is where
# voiced speech lives on this very quiet mic.
#
# Do NOT raise this on a hunch. It started at 2500, taken from a stale comment claiming ~900 ambient,
# and that one number silently disabled the entire whisper wake tier: ZERO frames ever cleared it, so
# the VAD never opened an utterance, nothing was transcribed, and "Hey Kai" did nothing at all — with
# no error logged anywhere. sess_vad_onsets is the check: if it stays 0 while you talk, it's this.
#
# Separation between room and voice is only ~3-4x here, so occasional false onsets are expected and
# are CHEAP: anything under MIN_UTTERANCE_S is dropped without running Whisper, and a transcript with
# no wake phrase is discarded silently. A false REJECT, by contrast, means the feature does nothing.
#
# 350 was too permissive in practice: it passed ~12-15% of frames, so the VAD opened on room noise
# ~150 times in a minute, and "tiny" confidently hallucinated whole sentences out of that noise
# ("Open the door.", "I'm not saying that."). 650 passes ~5% — closer to p95 (742) — which cuts the
# false onsets without losing real speech, whose frames run well above it.
VAD_RMS_FLOOR      = 650.0
# Hysteresis: a high bar to OPEN an utterance, a much lower one to KEEP it open. Same idiom as
# MouthDetector's MOUTH_OPEN/CLOSE_THRESHOLD in config/gesture.py.
#
# Without this the two goals fight and neither wins: at 650 only ~5% of frames pass, so on this quiet
# mic most syllables of real speech fall BELOW the floor, the hangover clock keeps restarting, and
# every capture ended after ~0.7 s of speech — cutting people off mid-sentence. Dropping the floor to
# 350 instead let room noise open utterances ~150x/minute.
# So: open on a clear peak, then hold on anything plausibly voiced until the speaker is really done.
VAD_RMS_FLOOR_HOLD = 250.0

# ── Ambient adaptation ───────────────────────────────────────────────────────────
# Both floors above are one room's measurement — 40 s in a quiet room on 2026-07-29. Carried into a
# noisy one they fail, and they fail in the worst possible direction:
#
# VAD_RMS_FLOOR_HOLD (250) is the bar to KEEP an utterance open. Once ambient noise sits above it the
# hangover clock can never run out, so the scan utterance never closes, hits
# WAKE_WHISPER_MAX_UTTERANCE_S (6 s), is thrown away as "too_long", and arms the 3 s long cooldown.
# That is a 6-on/3-off cycle in which Whisper NEVER RUNS ONCE. Not "degraded in noise" — structurally
# deaf, with sess_wake_ok still reporting True. The signature on /params is sess_scan_skip_long
# climbing while sess_scan_checks stays flat.
#
# So the floors track the room instead of being pinned to it. The multipliers below are chosen to
# REPRODUCE the measured tuning at the measured ambient: that session's p50 was 124, and
# 124 x 5.2 = 650 (the open floor), 124 x 2.0 = 250 (the hold floor). In the room they were tuned in,
# adaptation is a no-op. Set WAKE_AMBIENT_ADAPT False to pin them again.
WAKE_AMBIENT_ADAPT = True
# Ambient is the QUIETEST frame in a sliding window, not an average — speech is loud and intermittent,
# so a minimum ignores it while an average would be dragged up by the very person trying to wake Kai.
# 1.5 s is long enough to contain a gap between syllables and short enough to follow a room.
WAKE_AMBIENT_WINDOW_S = 1.5
# Per-window smoothing toward the new minimum. 0.3 means a changed room is tracked over ~5 windows
# (~7 s) rather than lurching on one quiet moment.
WAKE_AMBIENT_SMOOTH   = 0.3
WAKE_AMBIENT_OPEN_MULT = 5.2       # -> VAD_RMS_FLOOR at the ambient it was measured at
WAKE_AMBIENT_HOLD_MULT = 2.0       # -> VAD_RMS_FLOOR_HOLD, same
# Ceiling on the lift, as a multiple of the configured floor. Deafness is strictly worse than false
# onsets — a false onset costs one discarded Whisper run, while a floor lifted above the speaker's
# own voice means the feature does nothing and reports no error. In a room that loud the answer is
# tier 2, not a higher floor.
WAKE_AMBIENT_MAX_LIFT = 4.0
VAD_ONSET_FRAMES   = 3             # consecutive speech frames to open an utterance (60 ms)
# Trailing silence that ends the turn. 1.5 s, not 0.8: people pause mid-sentence to think, and at
# 0.8 s Kai cut them off and answered half a question. This is the "keep listening until they've
# actually stopped" knob — raise it if Kai still interrupts, at the cost of a slower reply.
VAD_HANGOVER_S     = 1.5
# The same clock for a WAKE SCAN, which is a completely different job and was silently paying the
# turn's tuning. Nobody pauses mid-thought while saying "hey" — the reason VAD_HANGOVER_S is 1.5 does
# not exist here, and every millisecond of it was charged to the wake:
#
#   1.5 s hangover + ~0.75 s transcribe + 1.0 s WAKE_WHISPER_COOLDOWN_S = ~3.2 s DEAF after you speak
#
# Nothing is captured during the last two. So a missed wake, followed by the natural human response
# of immediately saying it again, put the retry inside the dead window — which is most of why this
# tier felt broken rather than merely slow. At 0.45 the wake is ~1 s faster and the dead window is
# ~1.8 s, short enough that a retry lands.
#
# Do not raise this to "be safe": it only has to cover the gap between syllables of one short phrase.
# It bounds latency on EVERY wake, and lowering it also shortens the clip Whisper has to decode.
WAKE_SCAN_HANGOVER_S = 0.45
# Shorter than this is discarded WITHOUT running Whisper or the LLM. That is what breaks the
# self-sustaining loop: hiss -> empty transcript -> Kai says "didn't catch that" -> amp hiss -> ...
MIN_UTTERANCE_S    = 0.35
MAX_UTTERANCE_S    = 15.0          # forced turn end; also caps Whisper latency per turn

# ── Debug utterance capture ──────────────────────────────────────────────────────
# Writes every harvested utterance to disk as a WAV, plus a JSONL line of the numbers that were
# live when it was captured and a second line with what Whisper made of it (see ai/audio_debug.py).
#
# This is the answer to a problem every constant above shares: they were all measured at a desk in
# a quiet room on one afternoon, and there is no way to re-measure them in a venue — you cannot
# stand at the back of a hall reading /params. Switch this on before taking Kai somewhere loud and
# the corpus collects itself, labelled, in the room where the failures actually happen.
#
# OFF by default, and not only for disk: it records speech nobody addressed to Kai (the wake scan
# transcribes every nearby utterance, so every nearby utterance is a candidate clip). That is a
# privacy decision, not a performance one. Turn it on deliberately, for a session, with the people
# in the room aware of it, and turn it off afterwards.
DEBUG_CAPTURE_ENABLED = False
DEBUG_CAPTURE_DIR     = "/tmp/kai_utterances"
# Two independent caps, both cumulative across restarts (the recorder adopts what is already in the
# directory at startup — see UtteranceRecorder._prepare). Recording stops at whichever is hit
# first; clearing the directory resumes it. 500 x ~3 s of 16 kHz mono is roughly 50 MB, so the
# file cap is the one that normally binds and the MB cap is the backstop against long captures.
DEBUG_CAPTURE_MAX_FILES = 500
DEBUG_CAPTURE_MAX_MB    = 200.0
# Which paths to record. "turn" is an utterance Kai was actually answering; "scan" is a candidate
# the whisper wake tier overheard while idle. Narrow this to ("turn",) to record far less and
# capture only speech that was addressed to Kai — the scan fires on room noise and passers-by.
DEBUG_CAPTURE_KINDS = ("turn", "scan")

# ── Session lifecycle ────────────────────────────────────────────────────────────
SESSION_TICK_HZ      = 20          # the one thread that owns every timer
SESSION_NO_SPEECH_S  = 25.0        # silence while waiting for the user -> end session
SESSION_NO_FACE_S    = 8.0         # continuous absence while waiting -> end session
# Older than this and presence is UNKNOWN, not ABSENT: the face feed stops entirely on a camera
# stall or with --no-camera, and a dead camera must never end sessions.
FACE_FEED_STALE_S    = 2.0
# Two failed turns in a row ends the session rather than looping "didn't catch that" forever.
SESSION_MAX_NO_SPEECH_STREAK = 2
SESSION_MAX_ERROR_STREAK     = 2
# Above OLLAMA_TIMEOUT_S (90) plus STT: a cold gemma2:2b load legitimately takes ~50 s, so this
# only fires if a turn worker is genuinely wedged in native code, where no exception is raised.
SESSION_BUSY_MAX_S   = 120.0
SESSION_SPEAK_GRACE_S       = 2.0   # allowed overrun past the WAV's own duration
SESSION_SPEAK_MAX_UNKNOWN_S = 20.0  # cap when the duration couldn't be read — a wedged paplay
                                    # would otherwise keep the mic muted, i.e. deafen Kai for good

# ── Self-hearing gate ────────────────────────────────────────────────────────────
# How long after Kai's audio ends the mic stays shut.
#
# Measured, not guessed. paplay returns once the WAV is in the PulseAudio sink buffer, and Pulse keeps
# playing from that buffer afterwards — so "playback finished" arrives before the speaker is silent.
# `pactl list sinks` reported a TWO SECOND buffer on this box, which is why 0.8 s let Kai transcribe
# his own reply ("count to three" came back as 'We did it, we did it, we did it.') and answer it,
# doubling every request.
#
# The proper fix was upstream: TTS_LATENCY_MSEC in config/voice.py now asks Pulse for a 200 ms buffer,
# so paplay's exit tracks the real end of the audio and this only has to cover that 200 ms plus slack.
# It was briefly 2.0 s to work around the big buffer, at the cost of Kai being deaf for two seconds
# after every reply — which is what made him feel unresponsive.
#
# If doubling ever returns, check TTS_LATENCY_MSEC and this value FIRST. The wake sensitivity and the
# RMS floor are not the cause, and lowering them only makes the echo easier to hear.
TTS_TAIL_MUTE_S = 0.5
# Kai cannot hear anything while speaking, so a runaway reply is a long stretch of deafness. This is
# the real cost of a longer answer and the reason this number is not simply large.
#
# 500 (was 400, briefly 700): persona.txt lets the question set the reply length, and caps it at four
# spoken sentences — ~450 characters — so 500 only truncates runaways.
#
# 700 was the first attempt and it was wrong twice over, both caught by speaking real questions at the
# robot. It was sized off an ESTIMATED 450-500 char ceiling while the persona at the time invited
# "four or five sentences", which measured 849 chars on a multi-part question — so the clamp silently
# ate ~150 chars of a legitimate answer, the worst failure this cap has, because the tail was
# generated and paid for and then never heard. And the replies it did pass were simply too long out
# loud: 134 words is a ~45 s monologue at someone standing in front of the robot. The persona now
# answers with two or three sentences and OFFERS the rest, which is what actually fixed it; this
# number just has to sit above that. Fixing the clamp alone would have kept the monologue.
#
# Kept BELOW what OLLAMA_NUM_PREDICT (160) can generate on purpose, so this clamp is the one that
# normally bites — it backs up to a sentence end, where num_predict would stop mid-word.
TTS_MAX_SPOKEN_CHARS = 500
