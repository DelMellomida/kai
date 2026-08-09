# S5 — `face_track.py` constructs the whole robot at import time

| | |
|---|---|
| **Tier** | 3 |
| **Severity** | Medium |
| **Effort** | Medium |
| **Confidence** | High |
| **Lens** | Software |

## Location

- `face_track.py` — module scope: `os.makedirs(UPLOAD_DIR, exist_ok=True)`, `_camera = CameraSupervisor()`,
  `_voice = VoiceAssistant()`, `_session = ConversationSession(_voice, presence=presence.snapshot)`,
  `_web = DashboardState()`, `_dashboard = Dashboard(...)`, `_flask_app = _dashboard.create_app()`,
  `_servo_state = {...}`
- `face_track.py` — the helpers that reach for those globals: `_publish_web`, `_publish_status`,
  `_register_settings_callbacks`, `_start_web_server`, `_open_servo`
- `app/camera_supervisor.py` — the two-phase `__init__` / `configure()` split this forces
- `tests/test_face_params.py` — imports `face_track`

## Problem

Importing `face_track` has side effects. It creates a directory on disk (`UPLOAD_DIR`), and it
builds most of the object graph before `run()` has parsed a single CLI argument — including
`ConversationSession`, which in turn constructs a `MicStream`, a `WakeDetector`, a `SpeechGate` and
an `UtteranceRecorder` (the last of which will `mkdir` and scan a directory when debug capture is
enabled).

The module comments defend each decision individually and the reasoning is sound in each case: the
voice objects sit at module scope so "no flask installed → no wake word" cannot happen; the
dashboard routes need something to talk to before `run()` exists. But the aggregate is that
startup ordering is a property of *import order* rather than of code, and the two-phase construction
in `CameraSupervisor` exists solely to bridge the gap ("The instance exists at import time because
the dashboard routes need something to talk to before `run()` has parsed the CLI").

## Why it matters

- **It constrains testing.** Any test that imports `face_track` pays the whole construction and the
  `mkdir`. `tests/test_face_params.py` does exactly that.
- **It pushes complexity outward.** `CameraSupervisor`'s `configure()` split, and the seeded
  "starting up" state in `DashboardState`, are both workarounds for objects existing before their
  configuration does.
- **It makes ordering fragile.** The careful startup sequence documented at the top of `run()`
  ("everything camera-independent comes up FIRST … Nothing between here and the supervisor below can
  raise on absent hardware") only governs the second half of construction. The first half already
  happened, in import order, before any of that reasoning applies.

## Acceptance criteria

- [ ] Importing `face_track` has no filesystem side effects and constructs no hardware-facing
      objects — verified by a test that imports the module and asserts `UPLOAD_DIR` was not created.
- [ ] A `build(args) -> <context>` function (or a small `Robot`/`App` dataclass) constructs the
      supervisor, assistant, session, dashboard state, Flask app and servo, and is called from
      `run()` after `settings.load()`.
- [ ] `_publish_web`, `_publish_status`, `_register_settings_callbacks`, `_start_web_server` and
      `_open_servo` take their collaborators as parameters instead of reading module globals.
- [ ] The startup ordering guarantees `run()` documents still hold and are still expressed in one
      readable place: camera-independent subsystems first, camera last, nothing that can raise on
      absent hardware before the supervisor starts.
- [ ] The "no flask → wake word still works" property is preserved and covered by a test — the
      voice objects must be built regardless of `FLASK_OK`, which is the reason they were at module
      scope in the first place.
- [ ] `CameraSupervisor`'s two-phase construction can be collapsed (constructor takes the CLI facts
      directly) **or** an explicit comment records why it is being kept — either is acceptable, but
      the decision is made rather than inherited.
- [ ] `DashboardState`'s seeded `"starting up"` status is re-examined for the same reason and either
      kept with a stated reason or removed if the app is now built after configuration.
- [ ] `main()`'s single-instance lock and signal handlers still run *before* any construction, so a
      second instance still refuses to start without having touched hardware.
- [ ] Full suite green, and a real robot run verified: dashboard reachable, wake word live, camera
      hot-swap working, `/restart` still exits cleanly.

## Suggested approach

The mechanical part is straightforward; the care is in preserving the documented invariants.

1. **Move construction into `build(args)`.** Return a small frozen dataclass (`camera`, `voice`,
   `session`, `web`, `dashboard`, `flask_app`, `servo`, `servo_state`) rather than a tuple —
   the helpers need named access and a tuple of seven will not survive its first edit.
2. **Thread it through the helpers.** These are all small; the change is signature-only.
3. **`os.makedirs(UPLOAD_DIR)` moves into `build()`** (or into `_upload_video`'s first use, which is
   arguably better — the directory is only needed if someone uploads).
4. **Keep `main()`'s ordering.** `claim_single_instance()` → `install_signal_handlers()` →
   `run(args)` → `build(args)` inside `run()`. The lock must still be the first thing that happens.
5. **Preserve every explanatory comment**, re-sited. The comments at module scope explaining *why*
   the voice objects are not behind a flask check, and why the camera is last, are load-bearing
   documentation — they should move with the code, not be lost in the diff.

Sequencing note: this overlaps **S6** (splitting `ConversationSession`) only lightly — S6 changes
what the session is made of, this changes when it is made. They can land in either order, but doing
this one first makes S6's test setup simpler, because tests can build a session without importing
`face_track` at all.
