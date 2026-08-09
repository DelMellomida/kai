"""The filler bank — what Kai says while the real answer is still on its way.

`thinking.py` already covers the *short* pause with a wordless "Hmm, hmm. Hmm." That works when
the reply lands in ~1.3 s. It does not work when STT, RAG or Ollama runs long, or when the model
errors out entirely: the room gets dead air and Kai reads as broken. Worse, it is the same sound
every single turn, which an audience notices within three questions.

This module is the bank that replaces it for the long case. Two tiers, because one long line
cannot cover an arbitrarily long wait and repeating a long line is unbearable:

  FILLER_OPENERS  one per turn, spoken first. Long enough to buy several seconds, and it carries
                  a real fact about Kai so the wait teaches the audience something.
  FILLER_STALLS   1-4 words. After the opener, these loop as many times as needed until the real
                  answer lands. Short enough to be cut off mid-word without sounding wrong.

A turn therefore sounds like:

    [FILLER_MIN_GAP_S <= silence < FILLER_MAX_SILENCE_S]
        -> one opener -> stall, stall, stall ... -> the real answer

Every gap is clamped into that window from BOTH sides. The ceiling is the reason the module
exists; the floor is what keeps it from sounding like a queue draining rather than like thinking.

No line repeats inside one conversation, openers or stalls -- the session tracks what it has spent
and clears it in _begin_session, so a fresh greeting gets the whole bank back. That is a
preference, not a promise: a conversation that outlasts the bank starts a second lap rather than
going quiet.

Everything here is a plain string, pre-synthesised at startup like the other canned lines
(see ai/tts.py prewarm_canned), so nothing in this file costs latency at speak time.

WRITING RULES — these are not style preferences, they are what keeps the audio intelligible:

  * ASCII only. No em dashes, no smart quotes, no ellipses. Piper's phonemizer (espeak-ng) does
    unpredictable things with unusual punctuation, and the failure is inaudible from the string.
  * No long repeated-letter runs. espeak-ng reads them as initialisms and spells them out letter
    by letter -- "Hmmmm..." came back from Whisper as "H-A-M-A-M-M". See config/thinking.py:72-86.
  * Write brand names the way they should be SPOKEN: "NMBLR dot AI", not "NMBLR.AI".
  * Openers are 1-2 sentences, always shaped as: acknowledge the problem, then one Kai fact.
    KEEP THEM SHORT -- about 18 words for tl/ceb and 22 for en. This voice speaks Tagalog at
    roughly 0.45 s/word and English at 0.35, so the ORIGINAL bank averaged 27 words and every
    single opener measured over the 10 s cap on the robot; all 20 were rejected and the tier was
    dead. Two sentences is a shape, not a licence to run long.
  * Stalls carry no facts and no full sentences. Nothing over ~1.5 s spoken (see
    FILLER_MAX_STALL_S -- the cap is 1.8 but a WAV includes Piper's silence padding, so a 2-word
    line already measures ~1.2).

Every line must be synthesised through Piper and transcribed back with Whisper before shipping,
for the same reason config/thinking.py says so: a mangled line sounds fine as text.
"""

# ── The openers ───────────────────────────────────────────────────────────────
# Keyed by the language code Whisper detects for the utterance, so the filler answers in the
# language the person actually used (config/voice.py WHISPER_LANGUAGES bounds the set).
#
# "ceb" has no Piper voice of its own and there is no Cebuano anywhere else in the codebase.
# The Tagalog voice is close enough phonetically that Bisaya reads as accented rather than
# wrong; that is a judgement to confirm by ear on the robot, not from the text.
#
# The facts embedded here are the same ones in documents/kai_facts.txt, so a listener who asks
# a follow-up ("what is a Jetson?") gets a consistent answer out of RAG.
FILLER_OPENERS = {
    "tl": [
        "Teka, ang ingay dito parang palengke sa Divisoria! May NVIDIA Jetson Orin Nano ako sa loob.",
        "Hala, sabay-sabay kayo, na-shookt ang mikropono ko! Lahat ng iniisip ko dito lang mismo, walang cloud.",
        "Sandali, nagbu-buffer pa ang utak ko! Ampere architecture kasi ako na may Tensor Cores, mini supercomputer lang.",
        "Ulitin mo nga, may kuliglig yatang sumingit sa mic ko! Gawa ako ng DEVCON Jumpstart Cohort 4 interns.",
        "Loading pa po, wag mo akong pipindutin! CUDA at TensorRT ang gamit ko, kaya bilis mag-isip.",
        "Grabe ang echo dito, parang videoke room! Kahit walang WiFi gumagana pa rin ako, offline si ate.",
        "Nag-iisip pa ako, hindi ako nag-hang no! Camera, vision, at motor ko, lahat dito mismo naka-process.",
        "Sorry, hindi ko na-gets, may nag-videoke yata sa kabila! Hiram lang pala ako sa NMBLR dot AI Team.",
        "Wait lang, kino-compute ko pa, parang PLDT loading screen! Kaya kong makasabay kahit walang stable internet.",
        "Ang lakas ng background noise! Tech stack ko: Claude Code, Qwen, at NVIDIA, ang taba ng resume ko.",
        "Sandali lang, tao rin ako, charot, robot ako! Itong gumagalaw kong ulo, sinasalamin ka lang.",
        "May lag ang utak ko pero wala sa puso! Developer kit kasi ang loob ko, same sa drones.",
    ],
    # Base Bisaya/Cebuano, mixed with Tagalog/English
    "ceb": [
        "Kusog kaayo ang saba diri, murag tiangge sa Colon! Naa'y NVIDIA Jetson Orin Nano sa akong nawong.",
        "Huwat sa gamay, naghuna-huna pa ko! Wala koy kinahanglan nga internet, offline ra ko molihok bisan asa.",
        "Wa nako kadungog, murag naay nag-videoke sa luyo! Ang DEVCON Jumpstart Cohort 4 interns maoy naghimo nako.",
        "Sandali ra, gina-process pa nako, ayaw ko'g i-off! CUDA ug TensorRT akong gamit, mao paspas kaayo ko.",
    ],
    # Base English, mixed with Taglish flavor
    "en": [
        "Hold on, this room is louder than my cooling fan! Everything I think runs on an NVIDIA Jetson Orin Nano, no cloud.",
        "Sorry bes, didn't catch that, my ears are literally cardboard-grade! My camera, vision, and motors all run locally, ang galing 'di ba?",
        "Still processing, don't unplug me mid-thought, that's rude! Ampere architecture with Tensor Cores, a tiny supercomputer with stage fright.",
        "Buffering, my bad, blame the latency budget not the interns! I was built by the DEVCON Jumpstart Cohort 4 interns, shoutout!",
    ],
}

# ── The stalls ────────────────────────────────────────────────────────────────
# No facts, no sentences. These exist purely to fill the tail after the opener runs out, and
# they are deliberately the kind of thing a person says when they are stalling: half of them
# nudge the listener to try again, half just admit to still thinking.
#
# Same language keys as the openers, so a turn never switches language halfway through.
#
# NOT split 60/20/20 like the openers, and that is the point. An opener is drawn ONCE per turn, so
# its per-language count only has to outlast a conversation. Stalls loop until the answer lands --
# a 10 s wait spends 3-4 of them -- so what governs whether a line repeats is the size of ONE
# language's pool, not the bank total. At four lines, ceb and en lapped inside a single wait and
# the same stall was heard twice in one exchange on the robot (2026-08-09). Ten is roughly two
# laps of a long wait. The length cap can still shrink these below what is written here, which is
# why session._prewarm_bank now prints the surviving pool per language.
#
# The lines added to reach ten have NOT been through the synthesise-and-transcribe pass the
# docstring requires. The cap is the backstop, not the check: a line that mangles will still play.
FILLER_STALLS = {
    "tl": [
        "Try mo nga ulit.",
        "Isa pa nga.",
        "Paulit pls.",
        "Sandali ha.",
        "Konti na lang.",
        "Eto na, eto na.",
        "Teka, teka.",
        "Naglo-load pa.",
        "Ay, wait.",
        "Hala, sandali.",
        "Malapit na, promise.",
        "Char, nag-iisip pa.",
    ],
    "ceb": [
        "Huwat sa.",
        "Balik-balika nga.",
        "Hapit na.",
        "Ginaproseso pa.",
        "Huwat lang.",
        "Gamay na lang.",
        "Naghuna-huna pa.",
        "Hapit na gyud.",
        "Ay, huwat sa.",
        "Sige, huwat.",
    ],
    "en": [
        "One sec.",
        "Say that again.",
        "Still cooking.",
        "Almost, almost.",
        "Hang on.",
        "Wait lang.",
        "Still thinking.",
        "One moment.",
        "Almost there.",
        "Give me a sec.",
    ],
}

# Fallback language when Whisper's detection lands outside the keys above, or is unavailable
# (e.g. the filler fires before transcription has finished, which is the common case for the
# opener). Tagalog, because it is both the majority of the bank and the room's default.
FILLER_DEFAULT_LANG = "tl"

# How often a Tagalog-detected turn draws from the BISAYA bank instead.
#
# This knob exists because of a hard constraint upstream: config/voice.WHISPER_LANGUAGES is
# ("en", "tl"), so Whisper has no "ceb" label and detection can never select the Bisaya bank on
# its own. Without this the 14 Bisaya lines would be permanently dead weight -- synthesised at
# startup, never played.
#
# 0.25 rather than the opener bank's own 4-in-16 share, because this fires only on Tagalog turns.
# It is a deliberate product choice, not an inference about who is speaking: the room is Philippine,
# Bisaya reads as playful rather than wrong to a Tagalog listener, and a filler line is the
# lowest-stakes place in the whole system to mix them. Set to 0.0 to switch it off entirely.
# English turns are never affected.
FILLER_CEB_SHARE = 0.25

# ── Rollout ───────────────────────────────────────────────────────────────────
# Master switch. Off falls back to the single "Hmm, hmm. Hmm." in config/thinking.py, which is
# the behaviour before this bank existed -- so an unhappy room is one constant away from the
# old sound, without touching the session code.
FILLER_ENABLED = True

# Off leaves the bank cold, which means SILENT rather than slow: a filler line with no WAV is
# skipped, never synthesised on demand. That rule is not a nicety -- see session._speak_filler.
FILLER_PREWARM = True

# ── Bank prewarm pacing ───────────────────────────────────────────────────────
# These exist because of a measured failure, not a theory. The first version handed all 44 lines
# to prewarm_canned in one burst. tts publishes ONE _synth_proc handle and stop() kills whatever
# is in it, so that burst ran straight through a live turn: 17 filler lines failed in a row, a
# second Piper sat on the CPU beside the reply's own, the reply's WAV was read while still being
# written (EOFError out of wave.open), and the turn took 24.6 s to first audio -- 12.3 s of it
# synthesis. Robot log, 2026-08-07.
#
# So the bank now warms ONE line at a time, and only while nothing is speaking.

# How long to wait for quiet before giving up on a line this pass. 40 x 0.25 s = 10 s, which
# outlasts an ordinary turn but not a long conversation: a line skipped here is picked up by the
# next pass rather than blocking every line behind it.
BANK_QUIET_WAIT_TRIES = 40
BANK_QUIET_POLL_S = 0.25

# A breath between lines. Piper is the heaviest CPU consumer on this board after Ollama, and 40
# back-to-back runs with no gap starve the vision loop even when no turn is live.
BANK_SYNTH_GAP_S = 0.3

# Sweeps over the bank before giving up. Each pass skips what is already cached, so this is a
# retry budget for lines that kept losing the race, not three times the work.
BANK_PASSES = 3

# Immediate retries for a line whose synthesis STARTED and then died -- taken on the spot instead
# of waiting for the next pass.
#
# This exists because deferring to the next pass is not a neutral delay, and the bias it creates is
# measurable. Robot log, 2026-08-09: openers failed 11 times across 20 lines (55%), stalls 5 times
# across 32 (16%). Openers lose because they are the longest lines -- tts publishes ONE _synth_proc
# and stop() kills whatever is in it when a turn starts, so an 8 s opener is a far wider target
# than a 0.3 s stall. A killed line then sat out the rest of the pass, and a pass is minutes long
# (52 lines, each willing to wait BANK_QUIET_WAIT_TRIES x BANK_QUIET_POLL_S for quiet).
#
# The audible result was the tier inversion this constant fixes: pass 1 ending at "ceb 1op/10st,
# en 2op/10st", so for minutes after every restart a Bisaya or English turn had no opener to play
# and went straight from the "Hmm" to the short stalls. The long line the turn is supposed to open
# with simply was not on disk yet.
#
# Only a dead SYNTHESIS is retried. A line that never found a quiet window is not: the robot is
# busy right now, so re-waiting immediately is just as futile and would spend the budget where it
# cannot help. A line rejected by the length cap is not retried either -- that is deterministic and
# would fail identically every time.
BANK_LINE_RETRIES = 2

# ── The length cap ────────────────────────────────────────────────────────────
# HARD CEILING on how long any single filler line may take to say. A line that runs longer stops
# being filler and becomes a monologue the listener has to sit through, and — worse — it keeps
# talking well past the point the real answer arrived, so the answer either gets cut off or lands
# on top of it.
#
# ENFORCED AT PREWARM, not by counting words: session._prewarm_bank measures the WAV Piper
# actually produced and refuses to cache anything over this. A rejected line is simply never
# selected (the bank is chosen from warm keys only), so the cap holds even for a line someone
# adds later without measuring it. Speaking rate is a live dashboard setting, so the same text
# can cross this line at one rate and not another — which is exactly why it is measured per
# synthesis rather than decided once in a test.
FILLER_MAX_LINE_S = 10.0

# The same cap for stalls, which have a much tighter job: they must be interruptible, so being
# cut off mid-word reads as natural. Anything longer defeats the point of having two tiers.
#
# 1.8, not the 1.2 this started at, and the reason is a MEASUREMENT artifact rather than a change
# of mind about how long a stall should feel. wav_duration reads the file header, and Piper pads
# leading and trailing silence into every WAV it writes -- so "Paulit pls.", two words, measures
# 1.2 s. At 1.2 the cap sat at roughly the floor of what any 2-3 word line can physically produce,
# and it rejected 10 of the 20 stalls on the robot (2026-08-09) including several that sound
# perfectly snappy. 1.8 is still well inside "cut off mid-word reads as natural".
#
# The honest fix is to measure SPEECH rather than file length, which would let this go back under
# 1.2 -- but tts.wav_duration is also what sizes the jaw-sync window, so trimming silence there is
# a change with a second consumer and belongs in its own pass.
FILLER_MAX_STALL_S = 1.8


# ── Timing ────────────────────────────────────────────────────────────────────
# THE HARD CEILING, and the entire reason this module exists. No stretch of silence anywhere in
# a turn may exceed this: not before the opener, not between two stalls. Every jittered range
# below has an upper bound that still fits under it once playback start latency is added, and
# tests/test_filler.py asserts exactly that rather than trusting the numbers to stay consistent.
FILLER_MAX_SILENCE_S = 2.0

# THE FLOOR, and the counterweight to FILLER_MAX_SILENCE_S above. No filler line may begin less
# than this long after the previous one stopped -- not the opener after the turn starts, not a
# stall after the line before it.
#
# The ceiling alone is only half a contract. It bounds how long Kai can be quiet, and left to
# itself it pushes every gap toward zero: the safest way to never exceed 2 s of silence is to talk
# constantly. But filler that comes back the instant the last line ends does not sound like
# thinking, it sounds like a queue draining -- and it leaves no room for the real answer to land in
# a gap rather than on top of a line. A person stalling leaves a beat. This is that beat.
#
# ENFORCED IN _arm_filler AND _tick_filler, not merely by the ranges below, so widening the jitter
# downward cannot quietly remove it. The ceiling still wins if the two ever conflict (see
# _filler_gap): dead air is the failure this whole module exists to prevent, and a floor that could
# push a gap past the ceiling would be the floor breaking the ceiling's promise.
FILLER_MIN_GAP_S = 1.0

# How long into the turn to wait before the opener, so a genuinely fast reply is never
# interrupted by filler. DRAWN PER TURN, not fixed: a constant delay makes the opener land like
# a timer going off, and the ear picks that up immediately.
#
# Both bounds sit between FILLER_MIN_GAP_S and the ceiling minus the playback reservation (1.65),
# which is the whole room the two contracts leave. It used to start at 0.45, under
# thinking.py's THINKING_SOUND_DELAY_S; it no longer is, so on a turn where the bank has nothing
# the "Hmm" now reliably gets its tick first.
FILLER_DELAY_JITTER_S = (1.00, 1.60)

# Gap between consecutive stalls. Long enough that two stalls do not run together into a
# stutter, varied so a long wait does not turn into a metronome. The upper bound is what the
# ceiling test actually binds against, since this gap can recur many times in one turn; the lower
# bound is what the floor test binds against, for the same reason.
FILLER_STALL_GAP_JITTER_S = (1.00, 1.60)

# Playback start latency to budget for when clamping the drawn delay against the ceiling: the
# time between deciding to speak and the first audible sample from a pre-synthesised WAV. This
# is a reservation, not a measurement -- if a device turns out to be slower, raise this and the
# drawn delay shrinks to compensate, rather than the ceiling quietly breaking.
FILLER_PLAYBACK_START_BUDGET_S = 0.35
