"""
Arduino serial communication — authoritative ServoSerial implementation.
"""

from __future__ import annotations

import glob
import os
import subprocess
import threading
import time

import serial

# Tunable knobs live in config/servo.py; re-imported here so existing
# `from servo.servo import SEND_INTERVAL, ...` call sites and tests keep working.
from config.servo import (
    SEND_INTERVAL, JAW_SEND_INTERVAL, SERVO_MIN, SERVO_MAX, SEND_DEADBAND, PAN_DEADBAND,
    SERVO_PORT, SERVO_BAUD,
)

# Min seconds between reconnect attempts after a USB drop. The CH340 adapter can flap
# on/off the bus (bad cable / servo-current brown-out); without a floor here a dropped
# link would make every 20 Hz jaw write spin on reopen. Each attempt also blocks ~2 s
# waiting for the Arduino to reboot (opening the port toggles DTR → the board resets).
RECONNECT_INTERVAL = 2.0


def _ensure_usb(port: str) -> None:
    if os.path.exists(port):
        return
    print(f"[servo] {port} not found — loading ch341 driver...")
    # servo.py lives in servo/; the CH340 driver + fix script live in ../hardware/
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ko   = os.path.join(root, "hardware", "ch341_build", "ch341.ko")
    subprocess.run(["sudo", "modprobe", "usbserial"], check=False)
    subprocess.run(["sudo", "insmod", ko], check=False)
    time.sleep(1.0)
    if not os.path.exists(port):
        fix = os.path.join(root, "hardware", "fix_usb.sh")
        subprocess.run(["sudo", "bash", fix], check=False)
        time.sleep(0.5)
    if not os.path.exists(port):
        raise RuntimeError(f"{port} still not found after loading ch341. Check USB connection.")
    print(f"[servo] {port} ready.")


def _present_ttyusb(preferred: str) -> str | None:
    """The serial node that exists right now: `preferred` if present, else the first
    /dev/ttyUSB*. The CH340 re-enumerates between ttyUSB0/ttyUSB1 when the USB link flaps,
    so the node that reappears after a drop is often not the one we started on."""
    if os.path.exists(preferred):
        return preferred
    nodes = sorted(glob.glob("/dev/ttyUSB*"))
    return nodes[0] if nodes else None


class ServoSerial:
    """Sends pan,tilt[,jaw] commands to Arduino over serial."""

    _GESTURE_CODES = {
        "nod": "N", "shake": "S",
        "approach": "A", "retreat": "R",
        "mouth_open": "M", "mouth_close": "C",
    }

    def __init__(self, port: str = SERVO_PORT, baud: int = SERVO_BAUD) -> None:
        self._preferred_port = port
        self._baud           = baud
        self._last_reconnect = 0.0
        # Serializes serial writes: the servo control thread (pan/tilt) and the main loop
        # (speaking jaw / gestures) now both write this one handle. Public methods take it;
        # private _write/_reconnect/_force_send run only while it's already held.
        self._lock           = threading.Lock()
        _ensure_usb(port)
        self._ser = serial.Serial(port, baud, timeout=0.1)
        time.sleep(2)
        self._ser.reset_input_buffer()
        self._last_pan      = -1
        self._last_tilt     = -1
        self._last_jaw      = -1
        self._last_send     = 0.0
        self._last_jaw_send = 0.0
        self._force_send(90, 90, 90)
        print("[servo] Centered at 90°")

    # ── Link resilience ──────────────────────────────────────────────────────
    def _reconnect(self) -> bool:
        """Best-effort reopen after a USB drop. Re-resolves the ttyUSB node (it may have
        hopped ttyUSB0↔ttyUSB1) and reopens the port. Never raises — returns success."""
        port = _present_ttyusb(self._preferred_port)
        if port is None:
            # node gone entirely — try to (re)load the driver, then look again
            try:
                _ensure_usb(self._preferred_port)
            except Exception:
                pass
            port = _present_ttyusb(self._preferred_port)
        if port is None:
            return False
        try:
            self._ser = serial.Serial(port, self._baud, timeout=0.1)
            time.sleep(2)   # opening toggles DTR → Arduino resets; wait for it to boot
            self._ser.reset_input_buffer()
            print(f"[servo] reconnected on {port}")
            return True
        except (OSError, serial.SerialException) as exc:
            print(f"[servo] reconnect failed: {exc}")
            self._ser = None
            return False

    def _write(self, data: bytes) -> bool:
        """Write bytes to the Arduino, transparently reconnecting if the USB link dropped.
        Returns True if written, False if the link is currently down. Never raises — a
        flapping/absent CH340 adapter must not crash the tracking loop, it just makes writes
        no-ops (rate-limited reopen attempts) until the link comes back."""
        ser = self._ser
        if ser is not None:
            try:
                ser.write(data)
                return True
            except (OSError, serial.SerialException):
                self._ser = None   # stale handle — fall through to reconnect
        now = time.monotonic()
        if now - self._last_reconnect < RECONNECT_INTERVAL:
            return False           # don't spin while the adapter is continuously flapping
        self._last_reconnect = now
        if self._reconnect():
            try:
                self._ser.write(data)
                return True
            except (OSError, serial.SerialException):
                self._ser = None
        return False

    def _force_send(self, pan: int, tilt: int, jaw: int = 90) -> None:
        if not self._write(f"{pan},{tilt},{jaw}\n".encode()):
            return
        try:
            self._ser.reset_input_buffer()
        except (OSError, serial.SerialException):
            pass
        self._last_pan  = pan
        self._last_tilt = tilt
        self._last_jaw  = jaw
        self._last_send = time.monotonic()

    def send(self, pan: int, tilt: int, jaw: int | None = None) -> bool:
        """Send servo angles. Returns False if skipped, True if sent. Thread-safe."""
        with self._lock:
            now = time.monotonic()
            if now - self._last_send < SEND_INTERVAL:
                return False
            pan  = max(SERVO_MIN, min(SERVO_MAX, pan))
            tilt = max(SERVO_MIN, min(SERVO_MAX, tilt))
            jaw_c = max(SERVO_MIN, min(SERVO_MAX, jaw)) if jaw is not None else self._last_jaw
            # per-axis: snap to last value if change is within deadband. Pan/tilt use the smaller
            # PAN_DEADBAND so slow tracking is not emitted as stair-steps (see config/servo.py); the
            # jaw keeps SEND_DEADBAND, since its 20 Hz channel is where the serial traffic actually is.
            send_pan  = pan  if abs(pan  - self._last_pan)  > PAN_DEADBAND else self._last_pan
            send_tilt = tilt if abs(tilt - self._last_tilt) > PAN_DEADBAND else self._last_tilt
            send_jaw  = jaw_c if abs(jaw_c - self._last_jaw) > SEND_DEADBAND else self._last_jaw
            if send_pan == self._last_pan and send_tilt == self._last_tilt and send_jaw == self._last_jaw:
                return False
            # Fire-and-forget: firmware no longer echoes per-command, so there's nothing to
            # flush and reset_input_buffer()'s per-write ioctl is gone. A 2-field pan/tilt line
            # leaves the jaw servo untouched on the firmware side (only 3-field / 'J' write jaw).
            if jaw is not None:
                ok = self._write(f"{send_pan},{send_tilt},{send_jaw}\n".encode())
            else:
                ok = self._write(f"{send_pan},{send_tilt}\n".encode())
            if not ok:
                return False   # link down — leave last_* unchanged so we retry next tick
            if jaw is not None:
                self._last_jaw = send_jaw
            self._last_pan  = send_pan
            self._last_tilt = send_tilt
            self._last_send = now
            return True

    def send_jaw(self, angle: int) -> bool:
        """Jaw-only fast channel for speech animation. Bypasses send()'s 10 Hz pan/tilt gate
        so the mouth tracks speaking openness smoothly, via the firmware's 'J<angle>' command
        (writes only the jaw servo, leaves pan/tilt untouched). Returns False if skipped.

        Uses a NON-BLOCKING lock acquire: this runs on the main loop, so if the serial lock is
        held (e.g. a ~2 s USB reconnect on the control thread) we skip this jaw frame rather
        than stall the whole loop. Jaw is high-rate and self-correcting, so a dropped frame is
        invisible."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            now = time.monotonic()
            if now - self._last_jaw_send < JAW_SEND_INTERVAL:
                return False
            jaw_c = max(SERVO_MIN, min(SERVO_MAX, angle))
            if abs(jaw_c - self._last_jaw) <= SEND_DEADBAND:
                return False
            if not self._write(f"J{jaw_c}\n".encode()):
                return False   # link down — don't advance last_jaw so the next tick retries
            self._last_jaw      = jaw_c
            self._last_jaw_send = now
            return True
        finally:
            self._lock.release()

    def send_gesture(self, name: str) -> bool:
        """Fire a one-off gesture (nod, shake, …). Returns False if skipped.

        NON-BLOCKING lock acquire, for the same reason send_jaw uses one: this is called from the
        main tracking loop, and the control thread holds this lock for the whole of a USB reconnect
        — _reconnect() sleeps 2 s waiting for the Arduino to reboot, and _ensure_usb() can add a
        modprobe plus another 1.5 s on top of that. Waiting here would freeze face tracking, the
        speaking jaw and the dashboard status publisher for seconds every time a flaky CH340 flaps,
        which is precisely the stall send_jaw was made non-blocking to avoid.

        A gesture is a cosmetic social cue with no state behind it, so dropping one during a
        reconnect is strictly better than stalling the loop that drives everything else. The
        detector will raise the next one.
        """
        if not self._lock.acquire(blocking=False):
            return False
        try:
            code = self._GESTURE_CODES.get(name, "?")
            return self._write(f"G:{code}\n".encode())
        finally:
            self._lock.release()

    def center(self) -> None:
        with self._lock:
            self._force_send(90, 90, 90)

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except (OSError, serial.SerialException):
                    pass
                self._ser = None

    @property
    def last_pan(self) -> int:
        return self._last_pan

    @property
    def last_tilt(self) -> int:
        return self._last_tilt

    @property
    def last_jaw(self) -> int:
        return self._last_jaw
