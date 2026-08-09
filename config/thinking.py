"""The "thinking" expression — what Kai does during the 2-3 s of STT + LLM.

Every tunable for the feature lives here, so the whole thing is easy to find and easy to delete.
Consumed by settings.py (the two defaults), face_track.py (the sweep) and ai/session.py (the sound).

Both halves are independent dashboard toggles, not config edits, because nobody knows yet how they
feel in the room — see the reverting notes at the bottom.
"""

# ── The gentle pan sweep ──────────────────────────────────────────────────────
# Default for the "Sweep while thinking" toggle. The live value is settings.get("thinking_sweep").
THINKING_SWEEP           = True

# Amplitude, ± degrees around the position the head was ALREADY holding (never around centre, so the
# person stays framed). This is a HARD bound on the whole sweep: the randomisation below only ever
# scales DOWN from it, and the two sine components split it rather than stacking, so their sum cannot
# exceed it. That is what keeps per-command travel low (the brownout note in config/servo.py) and keeps
# the head inside SERVO_MIN/MAX even at the edge of the tracked range — config/tracking.PAN_SCALE maps
# a full nose sweep onto 30..150°, so 150 + 12 is still under 170.
THINKING_SWEEP_DEG       = 12.0

# One full there-and-back cycle. TUNED TO THE MEASURED REPLY TIME, which is why this is not the 3.0 s
# a "pondering" gesture instinctively wants: sess_last_turn_ms on this robot is ~1050 ms, so a whole
# (that field was called sess_last_llm_ms when this was measured; it has always been the whole
# STT+RAG+LLM turn, and was renamed when sess_last_llm_ms became the LLM stage alone)
# thinking window is only about 1.3 s. At a 3 s period the head got through under a quarter of a cycle
# — one slow lean to one side and back, which reads as nothing happening at all. At 1.6 s it completes
# roughly three quarters of a cycle in the same window: out, back through the middle, and part-way to
# the other side. Raise this only if replies get slower.
THINKING_SWEEP_PERIOD_S  = 1.6

# Dead time before the head moves at all, so a genuinely instant reply never twitches it. Kept SHORT
# for the same reason as the period: at 0.4 s it was eating a third of a 1.3 s window before anything
# moved. 0.15 s is still longer than any reply that could return before the head reacted.
THINKING_SWEEP_START_S   = 0.15

# ── Randomisation ─────────────────────────────────────────────────────────────
# A fixed sine is recognisably mechanical: the same arc, the same direction, every single turn. These
# are drawn ONCE per thinking window — never mid-sweep, which would jerk — so no two turns look alike
# while each individual turn stays one smooth continuous motion.
#
# Amplitude only ever scales DOWN, so THINKING_SWEEP_DEG remains a hard ceiling.
THINKING_SWEEP_AMP_JITTER    = (0.70, 1.00)
THINKING_SWEEP_PERIOD_JITTER = (0.80, 1.25)

# A second, slower sine summed on top, so even a long think never traces the same path twice. Its
# period is deliberately incommensurate with the main one (not a whole-number ratio) — that is what
# stops the sum from being periodic. Both components start at sin(0) = 0, so the head still grows out
# of exactly where it was; a phase offset here would make it jump on the first tick.
THINKING_SWEEP_WANDER_FRAC  = 0.25   # share of the amplitude given to the wander; main gets the rest
THINKING_SWEEP_WANDER_RATIO = 2.7    # wander period = main period * this. >1 keeps it a slow drift,
                                     # so it adds shape without adding much slope.

# How fast the offset eases back to 0 when thinking ends, degrees/second. This is what stops the head
# jerking back when the reply lands mid-swing; it also ramps the sweep in.
# MUST STAY ABOVE the sweep's own worst-case peak slope, or the easing flattens the sine into a
# triangle and quietly caps the amplitude. With the values above that worst case is ~50°/s (highest
# when the amplitude draw is large and the period draw small), hence 60 rather than the 40 that a 3 s
# period needed. tests/test_thinking.py asserts this across the whole random parameter space.
THINKING_SWEEP_RETURN_DPS = 60.0

# ── The "hmm" ─────────────────────────────────────────────────────────────────
# Default for the "Think out loud" toggle. Live value is settings.get("thinking_sounds").
THINKING_SOUNDS          = True

# Wait this long into the turn before playing it, so quick replies stay silent. Longer than the
# sweep's dead time on purpose: a twitch is cheap to be wrong about, a noise is not.
THINKING_SOUND_DELAY_S   = 0.6

# Pre-synthesised at startup by ai/session._prewarm_canned (and re-synthesised when the TTS voice
# changes), so there is no Piper latency inside the pause it is meant to fill.
#
# ONE SHORT UNIT, never a long m-run. espeak-ng (Piper's phonemizer) reads a long run of m's as an
# initialism and spells it out letter by letter: an earlier value here, "Hmmmm...", came back as
# "H-A-M-A-M-M" when the synthesized audio was fed to Whisper. "Hmm" on its own is short enough to
# be phonemized as a hum.
#
# It was "Hmm, hmm. Hmm." for a while, and that repeat was never wanted for its own sake — it
# existed only to reach the old 1.5 s THINKING_SOUND_TARGET_S without a big length-scale stretch,
# because a single "Hmm" needed roughly 3x and that much stretch smears the hum into something that
# stops sounding like a voice. The filler bank covers a long wait now, so the target is 0 and the
# repeat has nothing left to buy. This sound has exactly one job — mark the very start of a think.
# Three hums read as Kai stalling; one reads as Kai starting.
#
# So: do NOT restore the repeat, and do NOT raise the target to make it longer. If a longer cover
# is wanted, that is the bank's job (config/filler.py), not this line's.
#
# Verified by synthesizing through Piper and transcribing the result with Whisper — the failure is
# inaudible from the string itself, so re-check that way if you change it.
THINKING_SOUND_TEXT      = "Hmm."

# How long the "Hmm" itself should last. tts.synthesize_to_duration measures what Piper produced and
# re-synthesizes at a corrected length scale until it lands here, so this stays true across voices.
# 0 keeps whatever length the voice naturally produces.
#
# NOW 0, i.e. no stretch at all. It was 1.5 s when this sound had to cover the whole pause on its
# own, and that target is exactly what forced "Hmm, hmm. Hmm." — a single "Hmm" needed roughly a
# 3x length-scale to reach 1.5 s, and that much stretch smears the hum into something that stops
# sounding like a voice. The filler bank covers the long wait now, so the stretch has nothing left
# to buy: one natural-length hum, and the bank takes it from there.
THINKING_SOUND_TARGET_S  = 0.0
