"""Face-tracking loop knobs — servo mapping, smoothing, jaw, inference cadence, web feed.

Consumed by face_track.py (INFERENCE_INTERVAL is derived there from INFERENCE_FPS),
app/control_loop.py (CONTROL_INTERVAL likewise from CONTROL_FPS), app/camera_supervisor.py, and
web/server.py (WEB_PORT, UPLOAD_DIR, REBOOT_ENABLED)."""

# Run MediaPipe at this rate. Perception cadence only — it updates the servo *target*; a
# dedicated control thread (CONTROL_FPS) drives the servo independently.
# GIL CEILING (measured 2026-07-09): raising this does NOT help latency in practice — MediaPipe's
# Python-side work (resize/cvtColor/gating) holds the GIL and starves the pure-Python control
# thread. At 15 fps the control thread runs ~14 Hz; at 22 fps it collapsed to ~6-10 Hz (jerkier
# actuation). CPU/mem/thermals had headroom — the bottleneck is the GIL, not resources. Keep
# perception moderate to protect the control cadence. Getting BOTH faster perception and smooth
# control would need inference off the GIL (subprocess / native) — future work.
INFERENCE_FPS      = 15

# NO_FRAME_SLEEP (0.005) used to live here. The main loop polled CameraThread.latest() and slept
# 5 ms when it came back empty, so with --no-camera, an unprobed camera, or simply between frames at
# 30 fps, it iterated ~200 times a second — doing real Python work each time (a settings read under
# an RLock, a speaking_openness() call taking the assistant lock, a time comparison) before deciding
# there was nothing to do. All of it holding the GIL, which the note above measures as the resource
# this box actually runs out of, and worst in the degraded states where the servo and voice paths
# are the only things still working and most need the headroom.
#
# The poll is gone. CameraThread signals a stored frame with a threading.Event and the loop waits on
# it, so it wakes immediately on a frame and otherwise at NO_FRAME_WAIT (defined below, next to the
# publish interval that bounds it).

# Servo control loop (vision + face_track _control_loop). Decoupled from INFERENCE_FPS: the
# control thread runs the pan PD + slew clamp at this rate toward the latest target, so the
# head glides continuously between inference ticks instead of stepping on them.
# CURRENT-SENSITIVE: the effective servo send rate ≈ min(CONTROL_FPS, 1/SEND_INTERVAL); raising
# it increases average SG90 current on the shared rail (brownout → CH340 USB flapping). Kept at
# 15 to match today's profile (PD integrated ~15 Hz, sends gated to 10 Hz by SEND_INTERVAL).
# Raise toward 25-30 only after confirming no `dmesg` USB disconnects (see config/servo.py).
CONTROL_FPS          = 15
CONTROL_STALE_TIMEOUT = 1.0   # s; if the "present" target isn't refreshed within this (dead/hung
                              # inference thread), the control thread holds instead of chasing a frozen target

EMA_ALPHA   = 0.50
PAN_SCALE   = 120   # servo degrees swept across full nose X range
TILT_SCALE  = 90    # servo degrees swept across full nose Y range
MIN_FACE_AREA    = 0.04
JAW_CLOSED       = 90    # mouth shut (rest) — low angle. Higher angle opens the jaw.
JAW_OPEN         = 170   # mouth open — clamped to SERVO_MAX (SG90 overshoots at 180).
JAW_EMA_ALPHA    = 0.15   # heavier smoothing than pan/tilt to reduce jaw jitter
JAW_DEADBAND     = 4      # degrees — ignore jaw changes smaller than this
PAN_MAX_STEP     = 8      # degrees/command — hard slew cap on top of the PD controller,
                          # bounds SG90 current spikes/brownout on the shared power rail
EMA_RESET_FRAMES = 20    # no-face frames before EMA resets (prevents jump on brief flicker)

# Consecutive no-face inference frames before the SERVO TARGET is told the face is gone. MediaPipe
# misses single frames at the MIN_FACE_AREA boundary, and one miss used to flip the control loop from
# its track branch into its hold branch and back — which resets the PD, and cost a visible jump every
# time (see vision/controller.PDAxis's derivative priming, which fixes the other half of this).
# Holding the last target for a few frames instead makes a one-frame flicker invisible to the servo.
# Deliberately ONLY the servo target: vision/presence.mark() is still told the truth on every frame,
# because the session-end rules need real absence, not a smoothed version of it.
# Must stay well under CONTROL_STALE_TIMEOUT * INFERENCE_FPS (15 frames), or a genuine absence would
# be reported by the stale path instead of this one.
SERVO_ABSENCE_FRAMES = 3
WEB_PUBLISH_INTERVAL = 0.04   # cap shared-frame refresh to 25 fps

# How long the main loop blocks waiting for a frame before going round anyway (see NO_FRAME_SLEEP's
# obituary above). It is an upper bound on idleness, not a cadence: a real frame wakes the loop
# immediately, so with a live camera this value is never reached.
#
# It must be no LARGER than the shortest thing the loop still has to do on a frameless tick, or that
# work simply happens less often. There are two such obligations and they do not agree:
#
#   jaw animation      1 / JAW_SEND_INTERVAL   = 20 Hz   (config/servo.py)
#   _publish_status    1 / WEB_PUBLISH_INTERVAL = 25 Hz
#
# so it is pinned to the tighter of the two. R2 proposed JAW_SEND_INTERVAL (0.05) — that would have
# quietly dropped /params and the cam_retry_in_s countdown from 25 Hz to 20 Hz, which the ticket's
# own third acceptance criterion forbids. Written as an alias rather than a literal 0.04 so the two
# cannot drift apart; tests/test_settings.py pins it against JAW_SEND_INTERVAL, which lives in
# another config module and so cannot be referenced here (these files deliberately import nothing).
#
# 200 Hz -> 25 Hz on the idle path. Shutdown is unaffected: the wait is bounded well under a second,
# so SIGTERM/KeyboardInterrupt is still noticed within one tick.
NO_FRAME_WAIT = WEB_PUBLISH_INTERVAL

# Pan PD-controller gains (vision/controller.py PDAxis). Kd damps overshoot; tune on hardware.
PAN_KP = 0.20
PAN_KD = 0.25

# MediaPipe FaceMesh confidence gates.
FACE_MIN_DETECTION_CONF = 0.5
FACE_MIN_TRACKING_CONF  = 0.5

WEB_PORT   = 8081
UPLOAD_DIR = "/tmp/face_servo_upload"

# ── Reboot control (web/server.py POST /system/reboot) ────────────────────────
# OFF by default, and deliberately so. Every other control on the dashboard is recoverable in
# place; a reboot is not, and the dashboard has **no authentication at all** — Flask binds
# 0.0.0.0, so anyone who can reach the robot's port can press anything on it. On a home LAN that
# is a reasonable trade for a camera feed and a servo slider. A control that takes Kai off the air
# for ~90 seconds is a different proposition at a venue, which is exactly where it would be used.
#
# So switching this on is a deliberate act with two steps, not one, and the second is the one that
# actually grants the power:
#
#   1. REBOOT_ENABLED = True here
#   2. a NOPASSWD sudoers line for EXACTLY this command (visudo):
#        devconph ALL=(root) NOPASSWD: /usr/bin/systemctl reboot
#      Never `NOPASSWD: ALL` — that hands the whole box to an unauthenticated HTTP endpoint.
#
# Before turning it on, consider fixing the rootfs first: this SD card mounts with known ext4
# errors and wants an e2fsck, and making reboots one click away on a filesystem that is already
# unhappy is how a demo robot becomes an unbootable one.
#
# Reach for it last. /audio/reresolve recovers a mic in seconds and /restart takes ~20 s; the only
# things that genuinely need a reboot are wedged kernel-side audio, nvargus-daemon, and GPU memory
# fragmentation.
REBOOT_ENABLED = False
REBOOT_COMMAND = ("/usr/bin/systemctl", "reboot")   # must match the sudoers line exactly
REBOOT_TIMEOUT_S = 5.0

# ── Log cadence (face_track.py) ───────────────────────────────────────────────
# Both of these lines used to print on a fixed short timer regardless of whether anything had
# changed: "[face_track] NO FACE" once a second forever and "[control] N Hz" every 2 s. Measured on
# a 1.5-hour log, NO FACE alone was 5281 of 9070 lines — 58% of the file. Disk was never the issue
# (/tmp is cleared at boot, 54 GB free); the cost is diagnostic, because a real warning scrolls past
# between two thousand identical lines.
#
# The information in a repeated line is the CHANGE, so both are now edge-triggered — printed
# whenever face presence flips — plus a slow heartbeat, so a quiet log still proves the loop is
# alive and still reports the control rate. Raise these to quieten further; drop them to ~1.0 to
# get the old once-per-second behaviour back for a debugging session. Transitions always print,
# whatever the heartbeat is set to.
NO_FACE_LOG_INTERVAL_S = 30.0   # while a face stays absent, repeat "NO FACE" at most this often
CONTROL_LOG_INTERVAL_S = 30.0   # control-rate heartbeat; also the window the rate is averaged over
# --lofi is a machine-readable stream for tooling, not a human log: it keeps its 1 Hz cadence.
