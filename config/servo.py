"""Servo serial + motion limits. Consumed by servo/servo.py and servo/servo_diag.py."""

SERVO_PORT    = "/dev/ttyUSB0"   # Arduino serial device
SERVO_BAUD    = 115200

# 10 Hz serial cap for pan/tilt (near SG90 mechanical limit). This gates the servo control
# thread (config/tracking.CONTROL_FPS): effective send rate ≈ min(CONTROL_FPS, 1/SEND_INTERVAL).
# BROWNOUT-SENSITIVE — lowering this (faster sends) raises average SG90 current on the shared
# rail and can re-trigger CH340 USB flapping. To actually raise the send rate, lower this AND
# CONTROL_FPS together, incrementally, watching `dmesg | grep "USB disconnect"`. The real fix
# for fast motion is a separate servo power supply (see docs plan Phase 6).
SEND_INTERVAL     = 0.10
JAW_SEND_INTERVAL = 0.05   # 20 Hz for the jaw-only channel — smoother speech, own gate
SERVO_MIN     = 10     # keep away from physical stop (Tower Pro SG90 overshoots at 0)
SERVO_MAX     = 170    # keep away from physical stop (Tower Pro SG90 overshoots at 180)
SEND_DEADBAND = 3      # degrees — the JAW channel's threshold. Real filtering is in face_track.py
                       # (the jaw EMA) and app/control_loop.py (the pan slew clamp).

# Pan/tilt gets its own, smaller threshold, because the deadband was never meant to filter MOTION and
# on pan it was doing exactly that. During slow tracking the PD asks for ~1 degree per tick; at 3 those
# were all discarded and _last_pan never advanced, so nothing moved until the accumulated error crossed
# 4 degrees and then the head hopped 4 degrees at once — smooth drift emitted as visible stair-steps.
# NOT a brownout risk, despite touching the servo rail: the PEAK send rate is capped by SEND_INTERVAL
# either way, so lowering this only fills in send windows that were being skipped mid-motion; it cannot
# make sends more frequent than 1/SEND_INTERVAL. Quiescence is unaffected too — a converged PD produces
# a 0 degree delta, and the control loop sends nothing at all while holding.
# 1 keeps the deadband's actual job (swallowing int() rounding). Raise it if the head ever buzzes.
PAN_DEADBAND  = 1
