"""Gesture-detector thresholds. Consumed by vision/gesture.py (the _window_frames() logic
that turns *_WINDOW_S into deque lengths lives there, not here)."""

# Windows are specified in SECONDS, not frames, so gesture behavior stays identical in
# wall-clock time regardless of the inference rate face_track.py runs update() at. The
# frame-count deque length is derived per-detector as round(seconds * inference_fps).
# These durations reproduce the original 30 fps constants (20 frames ≈ 0.66s, 15 ≈ 0.5s).
DEFAULT_INFERENCE_FPS = 30

NOD_WINDOW_S      = 0.66
NOD_MIN_AMP       = 15
NOD_MIN_REVERSALS = 2
NOD_COOLDOWN      = 1.0

SHAKE_WINDOW_S      = 0.66
SHAKE_MIN_AMP       = 15
SHAKE_MIN_REVERSALS = 2
SHAKE_COOLDOWN      = 1.0

PROX_WINDOW_S = 0.5
PROX_DELTA    = 12
PROX_COOLDOWN = 1.5

MOUTH_OPEN_THRESHOLD  = 40
MOUTH_CLOSE_THRESHOLD = 20
MOUTH_COOLDOWN        = 0.8
