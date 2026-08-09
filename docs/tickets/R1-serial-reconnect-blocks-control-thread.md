# R1 — Serial reconnect blocks the servo control thread for seconds

| | |
|---|---|
| **Tier** | 2 |
| **Severity** | High |
| **Effort** | Medium |
| **Confidence** | High |
| **Lens** | Robotics |

## Location

- `servo/servo.py` — `ServoSerial._write()`, `_reconnect()`, `_ensure_usb()`, `send()`, `send_jaw()`
- `app/control_loop.py` — `run()`, which calls `servo.send()` every tick at `CONTROL_FPS`
- `face_track.py` — `run()`'s main loop, which calls `servo.send_jaw()` for speech animation
- `config/servo.py` — `SEND_INTERVAL`, `JAW_SEND_INTERVAL`; `servo/servo.py` — `RECONNECT_INTERVAL`

## Problem

`send()` acquires `self._lock` and holds it for the entire call. When the USB link has dropped,
`_write()` fails, discards the stale handle and falls through to `_reconnect()` — still under that
lock, still on the control thread. `_reconnect()` opens the port and then `time.sleep(2)` to wait
for the Arduino's DTR-triggered reboot. If the device node is gone entirely it first calls
`_ensure_usb()`, which shells out to `sudo modprobe usbserial`, `sudo insmod …/ch341.ko`,
`time.sleep(1.0)`, and — if the node still hasn't appeared — `sudo bash …/fix_usb.sh` plus
`time.sleep(0.5)`.

Worst case that is roughly 3.5 s of blocking, including three `sudo` subprocess invocations, inside
a 15 Hz real-time loop's critical section. `RECONNECT_INTERVAL = 2.0` rate-limits how often this can
be attempted, but does nothing to shorten any single attempt.

`send_jaw()` is already defended — it uses a non-blocking `acquire()` and skips the frame if the
lock is held — so the jaw does not *block*, but it does go completely silent for the duration.

## Why it matters

`config/servo.py` and `config/tracking.py` both document that the CH340 flaps on and off the bus
under SG90 brownout on the shared power rail. This path therefore fires exactly when the robot is
already misbehaving, and it makes every symptom worse at once:

- the head freezes for up to ~3.5 s and the control loop's effective rate collapses (visible as a
  gap in `[control] N Hz`),
- every jaw frame in that window is dropped, so speech pantomime dies mid-sentence,
- the recovery blocks on `sudo` subprocesses from a real-time thread, which is the one place in the
  codebase where a loop thread can be held hostage by external process state.

## Acceptance criteria

- [ ] `ServoSerial.send()` and `send_jaw()` never block for longer than a single `serial.write()`
      on a healthy handle — no `time.sleep()`, no `subprocess.run()`, and no port-open call is
      reachable from either method.
- [ ] With the serial device removed mid-run (`sudo modprobe -r ch341` or physically unplugged),
      `[control] N Hz` stays within ~10% of `CONTROL_FPS` and does not gap.
- [ ] Jaw animation continues normally during a link outage — `send_jaw` returns `False` promptly
      rather than being starved by a held lock, and resumes on its own once the link is back.
- [ ] Reconnection still happens: replugging the adapter restores servo motion within roughly
      `RECONNECT_INTERVAL` + the Arduino's boot time, with `[servo] reconnected on /dev/ttyUSBn`
      logged, including when the node hops `ttyUSB0` ↔ `ttyUSB1`.
- [ ] The `ch341` driver reload path (`_ensure_usb`) is never invoked from a loop thread; it runs
      only on the reconnect worker (and at construction, which is startup and may block).
- [ ] Link state is observable: `/params` reports servo link up/down and the reconnect attempt
      count, so a flapping adapter is visible from the dashboard rather than only inferable from
      motionless servos. (`_servo_state` in `face_track.py` currently reports only the startup
      outcome and never updates.)
- [ ] `tests/test_servo.py` gains cases asserting that a write failure returns promptly and does not
      sleep — e.g. a fake `serial.Serial` that raises on `write()`, with the test failing if the call
      takes longer than a few milliseconds.

## Suggested approach

Turn the link into a small state machine owned by a dedicated worker, so the loop threads only ever
observe its state:

1. **`_write()` becomes non-blocking.** On a write failure it drops the handle, sets
   `self._link_down = True`, signals a `threading.Event` for the reconnect worker, and returns
   `False`. No sleeping, no reopening, no driver loading.
2. **A reconnect worker thread** (daemon, started lazily on first failure or at construction) waits
   on that event, then performs the existing `_present_ttyusb` → `_ensure_usb` → `serial.Serial` →
   `time.sleep(2)` → `reset_input_buffer()` sequence, applying `RECONNECT_INTERVAL` as its own
   pacing. On success it publishes the new handle and clears `_link_down`.
3. **Handle publication.** The worker must not swap the handle while a write is in flight. Keep
   `self._lock` for the handle swap and for the writes themselves — the point is that the *slow*
   work now happens outside it, so the lock is only ever held for microseconds.
4. **`close()`** must stop the worker and must not deadlock against an in-progress reconnect (bound
   the join, mirroring `MicStream.stop()`'s 2 s join).

Note the shape this mirrors: `MicStream`'s worker already does exactly this for the capture device
— detect the stall, back off on `MIC_REOPEN_BACKOFF_S`, reopen on its own thread, expose
`reopening` so consumers can tell recovery from failure. Reuse that structure, including a
`reconnecting` flag so `/params` can distinguish "link down" from "link down, recovery in progress".

Keep `RECONNECT_INTERVAL` and its rationale comment; it becomes the worker's pacing rather than a
guard on the loop thread.
