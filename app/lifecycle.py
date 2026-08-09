"""Process lifecycle: the single-instance lock, signal handling, restart and reboot.

Everything here is about the PROCESS rather than the robot — starting exactly one of them, shutting
one down in an orderly way, and the two escape hatches the dashboard offers when a subsystem is
wedged past what any setting can reach. It owns no robot state at all, which is why it can be
imported and exercised with no camera, no mic and no serial port.

The exit codes are a contract with scripts/autostart.sh: EXIT_ALREADY_RUNNING is terminal (a
supervisor that retried it would spin forever against the healthy instance it is losing the lock
to) and EXIT_RESTART is relaunched immediately without counting as a failure.

Log lines keep the [face_track] prefix. They are what an operator greps for in
~/kai-logs/face-servo.log, and a rename here would be a silent break in a place nobody would think
to check.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

from config.tracking import REBOOT_COMMAND, REBOOT_TIMEOUT_S

LOCK_PATH = "/tmp/kai_face_track.lock"
_lock_fd = None   # module-level: the lock lives as long as the process holds this open

# Distinct exit code for "someone else is already running". scripts/autostart.sh treats it as
# terminal and does NOT restart — a supervisor that retried this would spin forever against the
# healthy instance it is losing the lock to.
EXIT_ALREADY_RUNNING = 3

# Distinct exit code for "the dashboard asked for a restart". scripts/autostart.sh relaunches
# immediately on this one and does NOT count it as a failure — an operator pressing the button is
# not a crash, and letting it feed the consecutive-fast-failure backoff would make the second
# restart in a minute take 10 s, the third 15 s, for no reason.
EXIT_RESTART = 7

# Long enough for Flask to have written the response before the process starts tearing itself down,
# short enough that nobody wonders whether the button worked. The reply is generated synchronously
# in the route; this only covers the socket write.
RESTART_DELAY_S = 0.4

# How long the orderly shutdown gets before arm_restart_deadline() takes the process down by force.
# Comfortably above a healthy teardown (measured ~1-2 s: the mic stop joins a 2 s thread, the camera
# and serial close immediately) and well inside the supervisor's restart window, so a robot that
# CAN exit cleanly always does. Only a wedged one ever reaches this.
RESTART_FORCE_AFTER_S = 12.0

restart_requested = threading.Event()


def supervised() -> bool:
    """Is something going to start us again after we exit?

    scripts/autostart.sh exports KAI_SUPERVISED=1 before its supervisor loop. Nothing else does, so
    a run started by hand (scripts/run.sh, or python3 face_track.py over ssh) reports False and the
    dashboard warns instead of quietly killing the robot on a click.

    The parent-process fallback is not belt-and-braces, it is the deploy path: a supervisor loop that
    was already running when this file was updated started from the OLD autostart.sh and therefore
    exports nothing, so the env var alone would call a perfectly supervised robot unsupervised until
    somebody rebooted it. Best-effort — no /proc (a Mac, the tests on Windows) just means we fall
    back to the honest "no".
    """
    if os.environ.get("KAI_SUPERVISED", "") not in ("", "0"):
        return True
    try:
        with open(f"/proc/{os.getppid()}/cmdline", "rb") as fh:
            return b"autostart.sh" in fh.read()
    except OSError:
        return False


def schedule_restart() -> None:
    """Tear ourselves down shortly after the caller's response has gone out.

    Its own function, not an inline Timer, so the route can be exercised in the tests without a
    live timer that would SIGTERM the test runner four tenths of a second later.
    """
    timer = threading.Timer(RESTART_DELAY_S, request_restart)
    timer.daemon = True
    timer.start()


def request_restart() -> None:
    """Exit the way SIGTERM already exits, then have main() report EXIT_RESTART.

    Reuses install_signal_handlers' path rather than inventing a second shutdown: the mic, the
    Porcupine native memory, the serial port and the camera all have to be released before the
    replacement process can open them (see scripts/autostart.sh's wait_for_capture_device), and
    run()'s `finally` is the only code that does that.
    """
    restart_requested.set()
    print("[face_track] restart requested from the dashboard", flush=True)
    try:
        import signal
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception as exc:
        # Never leave the flag set on a process that is going to keep running — the next orderly
        # Ctrl-C would then exit 7 and the supervisor would restart a robot somebody just stopped.
        restart_requested.clear()
        print(f"[face_track] ERROR: could not signal ourselves to restart ({exc})", flush=True)
        return
    arm_restart_deadline()


def reboot_now() -> tuple[bool, str]:
    """Ask the OS to reboot. Returns (ok, detail); never raises.

    Checks first, fires second. `sudo -n` fails immediately when no NOPASSWD rule matches, which
    turns the most likely misconfiguration into a message the operator can act on instead of a
    button that reports success and does nothing — the same failure mode that made the wedged
    restart so misleading. The command is fixed here rather than composed from a setting, so the
    sudoers rule can name exactly one binary and one argument.

    The reboot itself is scheduled on a short timer for the same reason /restart is: the HTTP
    response has to be written before the box starts going down, or the dashboard cannot tell
    "rebooting" from "network died".
    """
    # `sudo -l <command>` asks "may I run this?" and answers without running it. Deliberately not a
    # dry run of the command itself: a sudoers rule scoped to exactly `/usr/bin/systemctl reboot`
    # does NOT match `/usr/bin/systemctl reboot --help`, so probing with an extra argument would
    # report a correctly-configured robot as broken.
    try:
        probe = subprocess.run(["sudo", "-n", "-l", *REBOOT_COMMAND],
                               capture_output=True, text=True, timeout=REBOOT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not check sudo permission ({type(exc).__name__}: {exc})"
    if probe.returncode != 0:
        return False, ("this user may not run " + " ".join(REBOOT_COMMAND) + " without a password — "
                       "add a NOPASSWD sudoers line for exactly that command (see REBOOT_ENABLED "
                       "in config/tracking.py)")

    def _fire() -> None:
        try:
            subprocess.run(["sudo", "-n", *REBOOT_COMMAND], capture_output=True, text=True,
                           timeout=REBOOT_TIMEOUT_S)
        except Exception as exc:                        # pragma: no cover - the box is going down
            print(f"[face_track] ERROR: reboot command failed ({exc})", flush=True)

    print("[face_track] REBOOT requested from the dashboard", flush=True)
    timer = threading.Timer(RESTART_DELAY_S, _fire)
    timer.daemon = True
    timer.start()
    return True, ""


def arm_restart_deadline() -> None:
    """Force the exit if the orderly shutdown does not finish in time.

    The graceful path is the right one and stays the default: run()'s `finally` releases the mic,
    Porcupine's native memory, the serial port and the camera, and the replacement process needs all
    of them. But it only works if the threads it waits on are willing to stop, and the restart
    button exists precisely for robots where something is stuck.

    Observed 2026-08-09: face_track wedged inside mic resolution holding the raw I2S device, and a
    POST /restart replied `{"status":"ok"}` and then did nothing at all — same pid 40 minutes later,
    no log line past the request. From the dashboard that is indistinguishable from a restart that
    worked, which is the worst possible outcome for a recovery control: the operator believes the
    robot has been restarted and moves on. Only `kill -9` over ssh actually cleared it.

    So the graceful attempt gets a deadline, and past it we leave the hard way. os._exit skips
    atexit handlers and buffered output on purpose — everything worth flushing has already had
    RESTART_DELAY_S plus this window to do it, and the supervisor's wait_for_capture_device covers
    the devices the `finally` did not get to release. Exiting with the same EXIT_RESTART code means
    the supervisor treats it as a restart rather than a crash, so the robot still comes back.
    """
    def _force() -> None:
        print(f"[face_track] WARNING: orderly shutdown did not finish within "
              f"{RESTART_FORCE_AFTER_S:.0f}s — forcing the exit. Something was wedged; if this "
              f"repeats, the log above the restart request says what.", flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(EXIT_RESTART)

    timer = threading.Timer(RESTART_FORCE_AFTER_S, _force)
    timer.daemon = True
    timer.start()


def claim_single_instance(path: str = LOCK_PATH) -> bool:
    """Take an exclusive lock, or return False if another face_track already holds it.

    Two of these fight over hardware that admits exactly one owner — the raw I2S capture device,
    the CSI camera and the servo serial port — and the symptom is not a clean error but a robot
    that half-works: tracking fine, deaf, or with the jaw driven from two places. Cron only starts
    one at boot, but nothing stopped a manual start from landing on top of a running one (observed:
    a kill that matched the wrong PID, followed by a relaunch, left two processes competing).

    flock is released automatically by the kernel when the process dies, however it dies — which is
    what makes it safe here, where clean shutdown is not guaranteed. Best-effort: if fcntl is
    missing (non-Linux, e.g. running the tests on Windows) the guard is skipped rather than fatal."""
    global _lock_fd
    try:
        import fcntl
    except ImportError:
        return True
    try:
        _lock_fd = open(path, "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError as exc:            # unwritable path, full disk — do not refuse to boot over it
        print(f"[face_track] WARNING: instance lock unavailable ({exc}) — starting anyway",
              flush=True)
        return True
    _lock_fd.write(f"{os.getpid()}\n")
    _lock_fd.flush()
    return True


def install_signal_handlers() -> None:
    """Turn SIGTERM into the same orderly shutdown Ctrl-C already gets.

    Without this, the default SIGTERM disposition kills the interpreter outright and run()'s
    `finally` never executes — so the mic and Porcupine's native memory, the serial port and the
    camera are all left to the OS. Measured before this existed: 5 process starts in one log and
    not a single "[face_track] Stopped.", which is also why scripts/autostart.sh needs to wait for
    the capture device to be released before the next start can open it.

    Raising KeyboardInterrupt rather than setting a flag reuses the shutdown path that is already
    there and already tested, and it interrupts the main loop wherever it happens to be."""
    import signal

    def _on_term(signum, _frame):
        print(f"[face_track] received signal {signum} — shutting down", flush=True)
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _on_term)
        except (ValueError, OSError, AttributeError):
            pass          # not the main thread, or the platform lacks it — not worth failing over
