# S7 — Flask dev server, unauthenticated, on 0.0.0.0

| | |
|---|---|
| **Tier** | 2 |
| **Severity** | Medium (High for venue deployment) |
| **Effort** | Medium |
| **Confidence** | High |
| **Lens** | Software |

## Location

- `face_track.py` — `_start_web_server()`: `_flask_app.run(host='0.0.0.0', port=WEB_PORT, debug=False, use_reloader=False, threaded=True)`
- `web/server.py` — `create_app()` and every route; `app.config['MAX_CONTENT_LENGTH'] = 200 MB`;
  `_upload_video()`, `_settings_post()`, `_voice_say()`, `_restart()`, `_audio_reresolve()`,
  `_system_reboot()`, `_video_feed()`, `_params_stream()`
- `config/tracking.py` — `WEB_PORT = 8081`, `UPLOAD_DIR = /tmp/face_servo_upload`, and the
  `REBOOT_ENABLED` note that already articulates the no-authentication problem

## Problem

The dashboard is served by Werkzeug's development server, in production, bound to all interfaces,
with no authentication of any kind. `web/server.py`'s own docstring says so ("It is also
unauthenticated and bound to 0.0.0.0, which is why the destructive routes are guarded the way they
are"), and `REBOOT_ENABLED` is defaulted off with a three-step opt-in for exactly this reason.

That reasoning was applied to `/system/reboot` and stopped there. The remaining exposure:

- **Control routes are unguarded.** `POST /restart` takes the robot off the air for ~20 s.
  `POST /settings` can disable hands-free, mute TTS, turn off face tracking, or drive
  `vad_rms_floor` to a value that deafens it. `POST /voice/say` makes the robot say arbitrary text
  aloud. `POST /audio/reresolve` reopens the capture device mid-conversation.
- **Upload.** `POST /upload_video` accepts 200 MB into `/tmp` with an attacker-controlled extension,
  writing to a fixed `upload{ext}` name. (Path traversal is **not** reachable — `os.path.splitext`
  cannot return a separator in the extension — so the realistic exposure is filling `/tmp` and
  leaving arbitrary-extension files behind, not arbitrary write.)
- **Unbounded streaming threads.** `/video` and `/params` are `while True` generators; each
  connected client holds a Werkzeug thread for as long as it stays connected, with no cap, no
  timeout and no concurrency limit. `/video` additionally does a full JPEG encode per client.

## Why it matters

On a home LAN this is a reasonable trade for a camera feed and a servo slider, and the code says as
much. At a venue — which is precisely where the robot is meant to be used, and the scenario the
`REBOOT_ENABLED` comment names — anyone on the same network can silence, blind, restart or puppet
the robot. The dev server compounds it: it is explicitly not intended for production and has no
protection against slow clients or connection floods.

## Acceptance criteria

- [ ] The dashboard is served by a production WSGI server (`waitress` is the lightest fit and is
      pure-Python; `gunicorn` is acceptable) with an explicit worker/thread cap, or the bind address
      is narrowed to a specific interface — ideally both.
- [ ] All state-changing routes (`POST /settings`, `/settings/reset`, `/restart`, `/audio/reresolve`,
      `/voice/say`, `/voice/start`, `/voice/stop`, `/voice/wake`, `/session/end`, `/upload_video`,
      `/stop_video`, `/camera/probe`, `/system/reboot`) require a shared secret — a header token or
      HTTP basic auth — read from a file outside the repo, in the same style as
      `WAKE_ACCESS_KEY_FILE`.
- [ ] Read-only routes (`/`, `/guide`, `/params`, `/video`, `GET /settings`) may stay open if that is
      the deliberate choice, but the decision is recorded in `config/tracking.py` next to
      `REBOOT_ENABLED` with the same explicitness.
- [ ] The frontend obtains and sends the token without the operator having to paste it on every
      action, and the `curl`-over-ssh workflows described throughout `web/server.py` still work
      with one extra header.
- [ ] Concurrent `/video` clients are capped (a small number, e.g. 2–3); requests past the cap get a
      clear 503 rather than silently multiplying JPEG encoding work.
- [ ] `/upload_video` applies `werkzeug.utils.secure_filename` and an extension allowlist
      (`.mp4`, `.mov`, `.avi`, `.mkv`), rejects anything else with 400, and the upload size cap is
      lowered to something a demo video actually needs.
- [ ] Missing/invalid credentials return 401 with a body that does not leak whether the route
      exists, and a rejected attempt is logged once (rate-limited) so probing is visible.
- [ ] `tests/test_web.py` covers: an unauthenticated POST to each guarded route is rejected; an
      authenticated one succeeds; the `/video` client cap; the upload extension allowlist.
- [ ] Headless operation is unaffected — the "flask not installed" path still runs the robot with
      the wake word working, exactly as `create_app()` returning `None` provides today.

## Suggested approach

Do it in three independent commits so each can be reverted alone:

1. **Server swap.** Replace `_flask_app.run(...)` with `waitress.serve(app, host=..., port=WEB_PORT,
   threads=N)`, guarded by an import check that falls back to the current `app.run` with a logged
   warning — the same optional-dependency shape `FLASK_OK` already uses. Add `waitress` to
   `requirements.txt` with a call-site comment.

2. **Auth.** A `before_request` hook on the app that checks `request.method` and the endpoint
   against an allowlist of open routes, and otherwise requires a token. Read the token once at
   `create_app()` time from `~/.config/kai/dashboard.token` (create-on-first-run with 0600, print
   the path — not the value — at startup). If no token file can be created, log loudly and fall back
   to open access rather than bricking the dashboard; that mirrors the "settings must never be a
   reason the robot fails to start" principle in `settings.py`, and the log line is what makes the
   degraded state visible.

3. **Limits.** A counter in `DashboardState` for video clients already exists
   (`add_video_client`/`remove_video_client`) — reuse it to reject past the cap. Tighten
   `MAX_CONTENT_LENGTH` and add the filename handling in `_upload_video`.

Note the interaction with **S2**: capping and caching the `/params` work is a performance fix;
this is an access fix. They touch the same routes but are independent and can land in either order.
