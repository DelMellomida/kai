"""The dashboard's HTTP surface: one Flask app, every route, and the /params snapshot.

Kai has no screen, so this is the whole operator interface — the live video, the tuning knobs, the
chat transcript, and the three rungs of the recovery ladder (/audio/reresolve, /restart,
/system/reboot). It is also unauthenticated and bound to 0.0.0.0, which is why the destructive
routes are guarded the way they are.

Everything the routes act on arrives as a collaborator on Dashboard rather than being reached for
in a module global: the voice assistant, the conversation session, the camera supervisor and the
published state. That is what makes the routes testable — a fake session is enough to exercise
/audio/reresolve — and it is what stops this file growing a second opinion about robot state.

Flask is optional. On a dev box without it the import fails, create_app() returns None, and the
robot runs headless with the wake word unaffected; face_track.py checks FLASK_OK before serving.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import cv2

import settings
from ai import rag
from ai.wake_phrase import match_wake_phrase
from app import lifecycle
from config.tracking import REBOOT_ENABLED, UPLOAD_DIR

logging.getLogger('werkzeug').setLevel(logging.ERROR)
try:
    from flask import Flask, Response, request, send_from_directory
    FLASK_OK = True
except ImportError:
    FLASK_OK = False
    print("[face_track] WARNING: flask not installed — web dashboard disabled. "
          "Run: pip3 install flask")

_FRONTEND_DIR = str(Path(__file__).resolve().parent / "frontend")

# How often the SSE stream re-sends state, and how often the MJPEG generator looks for a new frame.
# Both are poll intervals rather than tunables: the publishers are gated by WEB_PUBLISH_INTERVAL,
# so these only decide how promptly a ready frame is picked up.
_PARAMS_POLL_S = 0.05
_VIDEO_POLL_S = 0.02
_JPEG_QUALITY = 75


class Dashboard:
    """Holds what the routes need and builds the Flask app around it."""

    def __init__(self, voice, session, camera, state) -> None:
        self.voice = voice
        self.session = session
        self.camera = camera
        self.state = state

    # ── things the routes and the tracking loop both need ───────────────────

    def params_snapshot(self) -> dict:
        """One full state snapshot for /params. Extracted from the SSE generator so it can be
        tested — the generator itself never terminates."""
        data = self.state.merged()
        data.update(self.voice.get_status())
        # Additive sess_* keys, plus the projected voice_status/voice_speaking for hands-free
        # states. Unknown keys are ignored by the frontend.
        data.update(self.session.get_status())
        # Whether the reboot control is configured at all. Published so the dashboard can leave the
        # button out entirely rather than show one that always answers 403 — an operator reaching
        # for a recovery control should not have to learn it was never switched on.
        data["reboot_enabled"] = REBOOT_ENABLED
        # Retrieval failures. RAG fails OPEN by design — a broken index answers exactly as if RAG
        # did not exist — which means a real regression there is otherwise invisible: it presents
        # only as answers getting vaguer. A counter is the cheapest way to make it visible over ssh.
        data.update(rag.status())
        return data

    def capture_start(self) -> dict:
        """Begin a push-to-talk recording, via the session when it owns the stream."""
        if self.session.owns_capture:
            return self.session.request_ptt_start()
        return self.voice.start_recording()

    def capture_stop(self) -> dict:
        if self.session.owns_capture:
            return self.session.request_ptt_stop()
        return self.voice.stop_recording()

    # ── the app ─────────────────────────────────────────────────────────────

    def create_app(self):
        """Build the Flask app, or None when flask is not installed."""
        if not FLASK_OK:
            return None

        app = Flask(__name__, static_folder=None)
        app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200 MB
        dash = self

        @app.route('/')
        def _index():
            return send_from_directory(_FRONTEND_DIR, 'dashboard.html')

        @app.route('/guide')
        def _guide():
            return send_from_directory(_FRONTEND_DIR, 'guide.html')

        @app.route('/video')
        def _video_feed():
            def _gen():
                dash.state.add_video_client()
                last_id = -1
                try:
                    while True:
                        frame, fid = dash.state.latest_frame()
                        # Encode on the Flask thread (off the tracking loop) and only when the
                        # frame is new — never re-encode one we already sent.
                        if frame is not None and fid != last_id:
                            last_id = fid
                            ok, jpg = cv2.imencode('.jpg', frame,
                                                   [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
                            if ok:
                                yield (b'--frame\r\n'
                                       b'Content-Type: image/jpeg\r\n\r\n' + jpg.tobytes() + b'\r\n')
                        time.sleep(_VIDEO_POLL_S)
                finally:
                    dash.state.remove_video_client()
            return Response(_gen(), mimetype='multipart/x-mixed-replace; boundary=frame')

        @app.route('/params')
        def _params_stream():
            def _gen():
                while True:
                    yield f'data: {json.dumps(dash.params_snapshot())}\n\n'
                    time.sleep(_PARAMS_POLL_S)
            return Response(_gen(), mimetype='text/event-stream',
                            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

        @app.route('/voice/start', methods=['POST'])
        def _voice_start():
            # Routed through the session, not straight to the assistant: after a hands-free turn the
            # assistant's status is DONE while the session is still SPEAKING, so its own guard would
            # accept this and open a SECOND stream on the raw hw device — which admits only one
            # opener.
            result = dash.capture_start()
            return result, (400 if 'error' in result else 200)

        @app.route('/voice/stop', methods=['POST'])
        def _voice_stop():
            result = dash.capture_stop()
            return result, (400 if 'error' in result else 200)

        @app.route('/voice/wake', methods=['POST'])
        def _voice_wake():
            # Fire the wake word by hand. Invaluable when the wake engine is misbehaving: it
            # separates "the session machine is broken" from "the engine isn't hearing me".
            #
            # With {"text": "..."} it runs the whisper tier's matcher over that text and takes the
            # one-breath path — which is how you exercise the matcher, say(), the epoch plumbing and
            # _on_turn_done over ssh with curl, with no mic, no whisper and no room noise.
            data = request.get_json(silent=True) or {}
            text = (data.get('text') or request.form.get('text') or '').strip()
            if text:
                match = match_wake_phrase(text)
                if match is None:
                    return {"error": f"no wake phrase in {text!r}"}, 400
                accepted = dash.session.on_wake(command=match.command)
            else:
                accepted = dash.session.on_wake()
            return ({"status": "ok"} if accepted
                    else {"error": f"wake rejected while {dash.session.state}"}), (
                200 if accepted else 400)

        @app.route('/session/end', methods=['POST'])
        def _session_end():
            return dash.session.end_session("manual"), 200

        @app.route('/voice/say', methods=['POST'])
        def _voice_say():
            # Mic-free jaw trigger: POST {"text": "...", "use_llm": true|false}. Animates the
            # mouth from model output (use_llm) or verbatim text — no audio capture, so it can't
            # hit the PulseAudio teardown crash the record path can.
            data = request.get_json(silent=True) or {}
            text = data.get('text') or request.form.get('text') or ''
            use_llm = data.get('use_llm', True)
            result = dash.voice.say(text, use_llm=bool(use_llm))
            return result, (400 if 'error' in result else 200)

        @app.route('/upload_video', methods=['POST'])
        def _upload_video():
            f = request.files.get('video')
            if not f:
                return {"error": "no file"}, 400
            ext  = os.path.splitext(f.filename or "")[1].lower() or ".mp4"
            path = os.path.join(UPLOAD_DIR, f"upload{ext}")
            f.save(path)
            try:
                from vision.camera import VideoFileCamera
                cam = VideoFileCamera(path)
            except Exception as exc:
                return {"error": str(exc)}, 422
            warnings = []
            if cam.width < 640 or cam.height < 480:
                warnings.append(f"Resolution {cam.width}×{cam.height} below recommended 640×480")
            if cam.fps < 15:
                warnings.append(f"FPS {cam.fps:.1f} below recommended 15")
            dash.camera.play_video(cam)
            return {"status": "ok", "fps": cam.fps, "width": cam.width,
                    "height": cam.height, "frame_count": cam.frame_count,
                    "warnings": warnings}

        @app.route('/stop_video', methods=['POST'])
        def _stop_video():
            dash.camera.stop_video()
            return {"status": "ok"}

        @app.route('/settings')
        def _settings_get():
            # The values are already on /params as set_*; this exists so the specs and the valid
            # ranges are discoverable with curl over ssh, the same reason /voice/wake accepts a text
            # payload.
            return {"values": settings.snapshot(), "defaults": settings.defaults(),
                    "specs": settings.describe(), "locked": dash.camera.settings_locked(),
                    "persist_error": settings.persist_error(),
                    "supervised": lifecycle.supervised()}

        @app.route('/settings', methods=['POST'])
        def _settings_post():
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict) or not data:
                return {"error": "expected a JSON object of setting -> value"}, 400
            locked = dash.camera.settings_locked()
            for name in data:
                if name in locked:
                    return {"error": f"{name}: {locked[name]}"}, 400
            try:
                # All-or-nothing: a batch that half-applied could leave two knobs disagreeing.
                applied = settings.set_many(data)
            except ValueError as exc:
                return {"error": str(exc)}, 400
            # A failed SAVE is not a failed request — the change is live, which is what was asked.
            # Reporting 400 here would make the dashboard snap the control back to a value no longer
            # in effect.
            return {"status": "ok", "values": applied, "persist_error": settings.persist_error()}

        @app.route('/settings/reset', methods=['POST'])
        def _settings_reset():
            return {"status": "ok", "values": settings.reset()}

        @app.route('/restart', methods=['POST'])
        def _restart():
            # The escape hatch for the failures a setting cannot reach: a wedged capture device, a
            # Porcupine engine that stopped hearing, an Ollama that came back after face_track gave
            # up. Everything those need is done at startup, so restarting the process is the fix,
            # and the alternative in the room is an ssh session or a power cycle.
            #
            # It exits rather than re-initialising in place: run() builds the camera, servo,
            # MediaPipe and session state as locals, and a second run() on top of a half-torn-down
            # first one is a much worse thing to get wrong than a 20 s outage. scripts/autostart.sh
            # brings us back.
            #
            # The reply is sent BEFORE anything is torn down (the shutdown fires on a short timer),
            # so the dashboard learns whether a supervisor will actually restart us — the one thing
            # the operator cannot see from the UI, and the difference between "back in 20s" and
            # "dead".
            supervised = lifecycle.supervised()
            lifecycle.schedule_restart()
            return {"status": "ok", "supervised": supervised,
                    "message": ("restarting — the dashboard will reconnect on its own" if supervised
                                else "shutting down; nothing is supervising this process, so it "
                                     "will NOT come back on its own")}

        @app.route('/audio/reresolve', methods=['POST'])
        def _audio_reresolve():
            # The cheap half of the restart button. A mic that failed at boot is usually fine a
            # minute later (the INMP441 read silent on one boot and timed out on the next, with
            # `arecord` finding real audio on both), but nothing short of restarting the process
            # used to take that second look — the watchdog only reopens a stream that died, and
            # skips STATE_DISABLED entirely, which is exactly the state a never-started mic leaves
            # behind.
            #
            # Seconds instead of the restart's ~20 s, and it keeps the camera, servos and
            # conversation history up. Reach for the restart only when this does not help.
            return dash.session.reresolve_mic()

        @app.route('/system/reboot', methods=['POST'])
        def _system_reboot():
            # The blunt instrument, for the residue a process restart genuinely cannot clear: a
            # wedged ALSA/kernel audio path, nvargus-daemon, GPU memory fragmentation. Try
            # /audio/reresolve, then /restart, before this — both are seconds and neither drops the
            # network.
            #
            # Guarded three ways, because unlike every other control on this dashboard a reboot is
            # not recoverable-in-place and the dashboard has NO authentication (Flask binds
            # 0.0.0.0):
            #   - an explicit {"confirm": "reboot"} body, so no stray or replayed POST can trigger it
            #   - REBOOT_ENABLED off by default, so it cannot fire on a robot nobody set it up on
            #   - a sudo probe first, so an unconfigured sudoers reports a clear error instead of a
            #     silent no-op that leaves the operator watching a robot that is never coming back
            if not REBOOT_ENABLED:
                return {"status": "error", "error":
                        "the reboot control is disabled — set REBOOT_ENABLED in config/tracking.py "
                        "and give this user a NOPASSWD sudoers line for exactly the reboot "
                        "command"}, 403
            body = request.get_json(silent=True) or {}
            if body.get("confirm") != "reboot":
                return {"status": "error", "error": "missing confirmation"}, 400
            ok, detail = lifecycle.reboot_now()
            if not ok:
                return {"status": "error", "error": detail}, 500
            return {"status": "ok", "message": "rebooting — Kai will be back in about 90 seconds"}

        @app.route('/camera/probe', methods=['POST'])
        def _camera_probe():
            # Wakes the supervisor so a camera just plugged in is picked up now rather than after
            # the backoff. The outcome arrives on /params as cam_source/cam_reason, like every other
            # async result in this app.
            dash.camera.probe_now()
            return {"status": "ok"}

        return app
