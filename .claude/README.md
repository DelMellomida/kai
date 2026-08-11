# .claude/ — agents and skills for working on Kai

Project-local Claude Code configuration. These encode the conventions that are already true of this
repo — the cadence contract, the comment-as-measurement discipline, the ticket format, the supported
deploy path — so they do not have to be rediscovered each session.

## Agents (`agents/`)

Delegate to these; each returns findings or a completed change, not a file dump.

| Agent | Use it for |
|---|---|
| `realtime-auditor` | Before merging anything touching `face_track.py`, `app/`, `servo/`, `ai/mic_stream.py`, `ai/audio.py`, `ai/session.py`, `web/state.py`. Hunts blocking calls, lock scope, GIL pressure, cadence drift. Read-only. |
| `ticket-implementer` | Implementing one `docs/tickets/` ID end to end — spec, dependencies, code, tests, ticket resolution, CHANGELOG. |
| `test-author` | Writing or repairing tests in the house style: `unittest`, fakes, injected clocks, no hardware. Knows the baseline and the two named flakes. |
| `jetson-diagnostician` | Diagnosing the live robot: `/params`, `/tmp/face-servo.log`, mic, camera, servos, Ollama, `dmesg`. Read-only by default. |
| `docs-curator` | Checking `README`, `docs/`, `config/` comments and `CHANGELOG` against the code after a change. Edits docs only. |

## Skills (`skills/`)

| Skill | Use it for |
|---|---|
| `kai-deploy` | Restarting the robot the supported way (`POST /restart`, **never** `kill -TERM`) and verifying the change is really running. |
| `kai-test` | Running the suite and interpreting the result against the baseline and the two known flakes. |
| `kai-ticket` | Writing a new `docs/tickets/` entry in the house format, indexing it, or recording one as done. |
| `kai-tune` | Changing a `config/` constant — live knob vs restart-only, and the measurements that bound several of them. |
| `kai-changelog` | Adding a `CHANGELOG.md` entry in the house style. |

## Facts they depend on

These go stale. Correct them here and in the file that carries them when they move.

- Suite baseline: **1185 passed, 2675 subtests, ~51 s** on Windows (2026-08-10).
- The working directory **is** the Jetson's `/home/devconph/Documents/kai` over SMB — every write is
  a deploy to disk; only the restart is missing.
- Dashboard: `http://192.168.1.25:8081`. Live log: `/tmp/face-servo.log`.
- Restart with `POST /restart` (exit 7, supervisor relaunches). `kill -TERM` exits 0 and leaves Kai
  **down**.

`settings.local.json` and `scheduled_tasks.lock` are git-ignored; everything else here is checked in
deliberately.
