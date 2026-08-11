---
name: kai-deploy
description: Deploy an edit to the live robot and verify it came up — restart face_track.py on the Jetson the supported way and confirm the change is actually running. Use when asked to deploy, restart Kai, push a change to the robot, or check whether an edit took effect on hardware.
---

# Deploying to Kai

The working directory **is** the Jetson's `/home/devconph/Documents/kai`, over SMB. There is no scp
step — **every write to the share is already a deploy to disk.** What has not happened is a restart:
`face_track.py` holds the old code in memory.

Confirm with the user before restarting. It is a live robot; it may be mid-demo.

## Restart

```bash
curl -X POST http://192.168.1.25:8081/restart
```

Exit code 7, which `scripts/autostart.sh`'s supervisor relaunches immediately. Back in ~25–30 s.

**Never `kill -TERM`.** SIGTERM is an *orderly* shutdown that exits 0, and the supervisor reads 0 as
"someone asked it to stop" — it breaks the loop and Kai stays down. This has been verified the hard
way. Same for exit 3 (another instance holds the lock).

From Windows, drive HTTP with Python `urllib` and an **empty ProxyHandler** —
`Invoke-WebRequest`/`Invoke-RestMethod` hang past 120 s on proxy auto-detect:

```python
import urllib.request
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
op.open(urllib.request.Request("http://192.168.1.25:8081/restart", method="POST"), timeout=30)
```

If it is already **down**, the dashboard is gone too — relaunch the supervisor detached over SSH:

```bash
setsid nohup bash ~/Documents/kai/scripts/autostart.sh >> /tmp/face-servo.log 2>&1 < /dev/null &
```

SSH from Windows needs **paramiko** with `password=` (no `sshpass`/`plink`, and stdin is the null
device). Base64 the script and `base64 -d | bash` on the far side — quoting does not survive
otherwise. Set `MSYS_NO_PATHCONV=1`.

## Verify — always, and before declaring success

1. **It came back:** `tail -60 /tmp/face-servo.log` for the `[autostart] restart requested` line
   followed by a clean start. Not syslog, not in the repo.
2. **It is healthy:** read one `data:` line from `GET /params` (SSE — read one line and close) and
   check camera, mic and session state.
3. **The change is actually running.** A restart proves nothing about your edit. Exercise it:
   - `POST /voice/say` `{"text": "...", "use_llm": false}` — speaks a verbatim line through the real
     `_speak` path. The way to hear a TTS or delivery change. Safe with `--wake` on; `_speak` gates
     the mic so Kai will not answer itself.
   - `POST /voice/wake` `{"text": "Hey Kai, <question>"}` — drives a **whole turn**: session,
     `STATE_BUSY → STATE_SPEAKING → COOLDOWN`, filler, the lot, with no mic and no room noise. The
     only remote way to exercise anything the session owns. `/voice/say` bypasses the state machine
     entirely and cannot test it.
   - `[turn]` log lines break latency down. ~6.4–9.6 s to first audio is the current baseline, not a
     regression.

## Before you deploy

- Run the suite: `python -m pytest -q` (baseline 1185 passed, 2675 subtests, ~51 s).
- `git status` — the working tree usually carries unrelated in-flight work, and a **concurrent
  session may be editing the same share**. Check mtimes before assuming a diff is yours.
- A `config/` change is restart-only by design. One of the eleven dashboard-settable knobs applies
  live instead — check `config/README.md` before restarting for something that did not need it.

## Not a deploy path

`POST /system/reboot` reboots the Jetson. Ask first, every time. Over SSH a reboot returns a **bogus
non-zero exit code** as the channel dies — confirm with `uptime -s` or
`/proc/sys/kernel/random/boot_id` rather than rebooting twice. Note that a fresh boot also breaks two
session tests for ~17 minutes (uptime-coupled fake clock).
