"""Camera capture + processing resolution. Consumed by vision/camera.py and
vision/face_params.py (PROCESS_*). (FRAME_HEADER is a wire-format constant and stays in
vision/camera.py. vision/laptop_camera.py runs standalone on the laptop and keeps its own
copy of these — no repo import there.)"""

NETWORK_PORT = 8485   # TCP port the Jetson listens on for laptop_camera.py frames

# CSI camera (Jetson Orin Nano, nvarguscamerasrc)
CSI_WIDTH     = 640
CSI_HEIGHT    = 480
CSI_FRAMERATE = 30
CSI_PULL_TIMEOUT_MS = 150   # try-pull-sample timeout — with max-buffers=1 drop=true a longer
                            # block buys nothing; a miss just returns None and the caller retries

# Local (OpenCV) webcam
LOCAL_WIDTH  = 640
LOCAL_HEIGHT = 480

# ── Availability probing ──────────────────────────────────────────────────────
# face_track.py starts with no camera and a background supervisor keeps probing, so a camera plugged
# in later is picked up live. These control that probe; they are read at each attempt, but they are
# startup/cadence tuning rather than operator knobs, so they are NOT in the dashboard settings.

CSI_FIRST_FRAME_S       = 10.0  # how long to wait for Argus's first frame at startup. nvarguscamerasrc
                                # reports PLAYING before its capture session really produces frames;
                                # 2s was too tight and caused spurious fallback-then-crash.
CSI_FIRST_FRAME_RETRY_S = 3.0   # shorter budget on a background re-probe: a device node that just
                                # appeared is warm, and the supervisor can afford to try again.

CAMERA_REQUIRE_DEVICE_NODE = True   # skip the (slow) CSI/V4L2 probe entirely when no /dev/video*
                                    # exists. A CSI sensor that fails its i2c probe registers no node,
                                    # so this turns a 10s stall into a microsecond check. Set False if
                                    # your capture source somehow works without a node.

CAMERA_RETRY_INTERVAL_S = 5.0   # supervisor: base delay between probes
CAMERA_RETRY_MAX_S      = 60.0  # ...doubling to this after each *expensive* failure, so a robot with
                                # no camera at all is not re-probing hard forever

CAMERA_STALL_S = 10.0   # a live camera delivering no frames for this long is treated as gone, so the
                        # dashboard stops claiming a feed it no longer has and the supervisor resumes
                        # probing. Comfortably above the CSI first-frame budget so a slow Argus start
                        # is never mistaken for a dead camera.

CAMERA_PROBE_MEMO_S = 60.0  # how long a failed expensive probe suppresses another one for the SAME
                            # set of device nodes. Must not be forever: a CSI probe can fail merely
                            # because nvargus-daemon was still starting, and that camera has to get
                            # another chance without the node set changing. "Probe now" ignores this.

# Resize input to this before running MediaPipe (4× fewer pixels than 640×480). Smaller =
# faster but coarser landmarks. Keep the 4:3 ratio to match the solvePnP pixel space.
PROCESS_WIDTH  = 320
PROCESS_HEIGHT = 240
