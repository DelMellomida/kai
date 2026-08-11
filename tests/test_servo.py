import re
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from servo.servo import ServoSerial, SEND_INTERVAL, SERVO_MIN, SERVO_MAX, PAN_DEADBAND


def make_servo(port: str = "/dev/ttyUSB0") -> ServoSerial:
    """ServoSerial with a mocked serial port — no hardware required."""
    mock_ser = MagicMock()
    with patch("servo.servo._ensure_usb"), \
         patch("servo.servo.serial.Serial", return_value=mock_ser), \
         patch("servo.servo.time.sleep"):
        s = ServoSerial(port)
    s._ser = mock_ser
    mock_ser.reset_mock()   # discard init calls
    s._last_send = 0.0      # allow immediate send
    return s


class TestServoSend(unittest.TestCase):
    def test_pan_tilt_only_format(self):
        s = make_servo()
        s.send(90, 45)
        s._ser.write.assert_called_once_with(b"90,45\n")

    def test_pan_tilt_jaw_format(self):
        s = make_servo()
        s.send(90, 45, 120)
        s._ser.write.assert_called_once_with(b"90,45,120\n")

    def test_rate_limited_returns_false(self):
        s = make_servo()
        s._last_send = time.monotonic()
        self.assertFalse(s.send(90 + PAN_DEADBAND + 1, 90))

    def test_first_send_returns_true(self):
        # __init__ centres at 90/90/90, so send(90, 90) is a real no-op inside PAN_DEADBAND and
        # returns False before the rate limiter is ever the reason. This test is about "the first
        # send past the rate limiter goes out", so the angle has to actually change.
        s = make_servo()
        self.assertTrue(s.send(90 + PAN_DEADBAND + 1, 90))

    def test_clamps_pan_above_max(self):
        s = make_servo()
        s.send(999, 90)
        s._ser.write.assert_called_once_with(f"{SERVO_MAX},90\n".encode())

    def test_clamps_pan_below_min(self):
        s = make_servo()
        s.send(-50, 90)
        s._ser.write.assert_called_once_with(f"{SERVO_MIN},90\n".encode())

    def test_clamps_tilt(self):
        s = make_servo()
        s.send(90, 200)
        s._ser.write.assert_called_once_with(f"90,{SERVO_MAX}\n".encode())

    def test_clamps_jaw(self):
        s = make_servo()
        s.send(90, 90, 250)
        s._ser.write.assert_called_once_with(f"90,90,{SERVO_MAX}\n".encode())

    def test_updates_last_pan_tilt(self):
        s = make_servo()
        s.send(100, 120)
        self.assertEqual(s.last_pan, 100)
        self.assertEqual(s.last_tilt, 120)

    def test_updates_last_jaw(self):
        s = make_servo()
        s.send(90, 90, 130)
        self.assertEqual(s.last_jaw, 130)


class TestServoJaw(unittest.TestCase):
    def test_jaw_only_format(self):
        s = make_servo()
        s._last_jaw = 90
        s.send_jaw(150)
        s._ser.write.assert_called_once_with(b"J150\n")

    def test_jaw_clamped_to_max(self):
        s = make_servo()
        s._last_jaw = 90
        s.send_jaw(999)
        s._ser.write.assert_called_once_with(f"J{SERVO_MAX}\n".encode())

    def test_pan_deadband_passes_a_two_degree_move(self):
        # The stair-step regression: pan used to share the jaw's 3 degree deadband, so slow tracking
        # (~1 degree per tick) was swallowed entirely until the error crossed 4 and the head hopped.
        s = make_servo()
        s._last_pan, s._last_tilt = 100, 90
        self.assertTrue(s.send(102, 90))
        s._ser.write.assert_called_once_with(b"102,90\n")

    def test_pan_deadband_still_swallows_rounding(self):
        # PAN_DEADBAND's remaining job. A 1 degree delta is int() noise, not motion.
        s = make_servo()
        s._last_pan, s._last_tilt = 100, 90
        self.assertFalse(s.send(101, 90))
        s._ser.write.assert_not_called()

    def test_a_converged_pan_still_quiesces(self):
        # The property the deadband protects: a still face must stop producing serial traffic, so the
        # firmware's idle detach can relax the servos.
        s = make_servo()
        s._last_pan, s._last_tilt = 120, 90
        for _ in range(5):
            s._last_send = 0.0
            self.assertFalse(s.send(120, 90))
        s._ser.write.assert_not_called()

    def test_jaw_keeps_the_coarser_deadband(self):
        # Pan got its own threshold precisely so the 20 Hz jaw channel's traffic was left alone.
        s = make_servo()
        s._last_pan, s._last_tilt, s._last_jaw = 90, 90, 100
        self.assertFalse(s.send(90, 90, 102))
        s._ser.write.assert_not_called()

    def test_jaw_deadband_skips_small_change(self):
        s = make_servo()
        s._last_jaw = 100
        self.assertFalse(s.send_jaw(102))   # within SEND_DEADBAND
        s._ser.write.assert_not_called()

    def test_jaw_rate_limited_returns_false(self):
        s = make_servo()
        s._last_jaw = 90
        s._last_jaw_send = time.monotonic()
        self.assertFalse(s.send_jaw(150))

    def test_jaw_channel_independent_of_pan_gate(self):
        # A recent pan/tilt send must NOT rate-limit the jaw channel (separate timers).
        s = make_servo()
        s._last_send = time.monotonic()   # pan/tilt gate closed
        s._last_jaw  = 90
        self.assertTrue(s.send_jaw(150))
        s._ser.write.assert_called_once_with(b"J150\n")


class TestServoGesture(unittest.TestCase):
    def test_nod_sends_G_N(self):
        s = make_servo()
        s.send_gesture("nod")
        s._ser.write.assert_called_once_with(b"G:N\n")

    def test_shake_sends_G_S(self):
        s = make_servo()
        s.send_gesture("shake")
        s._ser.write.assert_called_once_with(b"G:S\n")

    def test_mouth_open_sends_G_M(self):
        s = make_servo()
        s.send_gesture("mouth_open")
        s._ser.write.assert_called_once_with(b"G:M\n")

    def test_mouth_close_sends_G_C(self):
        s = make_servo()
        s.send_gesture("mouth_close")
        s._ser.write.assert_called_once_with(b"G:C\n")

    def test_approach_sends_G_A(self):
        s = make_servo()
        s.send_gesture("approach")
        s._ser.write.assert_called_once_with(b"G:A\n")

    def test_retreat_sends_G_R(self):
        s = make_servo()
        s.send_gesture("retreat")
        s._ser.write.assert_called_once_with(b"G:R\n")


class TestServoCenter(unittest.TestCase):
    def test_center_sends_90_90_90(self):
        s = make_servo()
        s.center()
        s._ser.write.assert_called_once_with(b"90,90,90\n")


class TestServoConcurrency(unittest.TestCase):
    """The servo control thread (pan/tilt) and the main loop (speaking jaw) now write the same
    serial handle. Verify the internal lock serializes writes: no exceptions, and every write is
    a single well-formed line (never interleaved/garbled)."""

    _LINE_RE = re.compile(rb'^(J\d+|G:[A-Z?]|\d+,\d+(?:,\d+)?)\n$')

    def test_concurrent_send_and_send_jaw(self):
        s = make_servo()
        writes = []
        wlock = threading.Lock()

        def record(data):
            with wlock:
                writes.append(data)
            return len(data)

        s._ser.write = record   # replace mock write with a thread-safe recorder
        stop = threading.Event()
        errors = []

        def pan_worker():
            try:
                i = 0
                while not stop.is_set():
                    s._last_send = 0.0                      # open the 10 Hz gate so sends go through
                    s.send(SERVO_MIN + i % 150, SERVO_MIN + (i * 3) % 150)
                    i += 1
            except Exception as e:      # noqa: BLE001 — capture any thread-safety failure
                errors.append(e)

        def jaw_worker():
            try:
                i = 0
                while not stop.is_set():
                    s._last_jaw_send = 0.0
                    s._last_jaw = -1                        # force past the deadband
                    s.send_jaw(SERVO_MIN + i % 150)
                    i += 1
            except Exception as e:      # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=pan_worker), threading.Thread(target=jaw_worker)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        stop.set()
        for t in threads:
            t.join(timeout=1.0)

        self.assertEqual(errors, [], f"threads raised: {errors}")
        self.assertGreater(len(writes), 0, "no writes recorded — test didn't exercise the path")
        for data in writes:
            self.assertRegex(data, self._LINE_RE)


class TestFirmwareAngleLimits(unittest.TestCase):
    """The firmware's copy of the travel limits, checked against config/servo.py.

    R4 asks the sketch to clamp to the same window as the host, "with a comment pointing at
    config/servo.py as the source of truth they must be kept in step with". A comment is not a
    mechanism — the two files are in different languages, one of them is not executed by anything
    in this repo, and the sketch only reaches the robot when a human remembers to flash it. This
    class is the mechanism.

    It reads the real .ino. It cannot prove the firmware BEHAVES correctly — only hardware can do
    that, and R4's on-hardware criterion is deferred for exactly that reason — but drift between
    the two constants is the failure that would otherwise go unnoticed for months, and that it can
    prove."""

    _INO = Path(__file__).parent.parent / "arduino" / "servo_serial" / "servo_serial.ino"

    def setUp(self):
        if not self._INO.is_file():
            self.skipTest(f"sketch not found at {self._INO}")
        self.source = self._INO.read_text(encoding="utf-8")
        self.code = self._strip_comments(self.source)

    @staticmethod
    def _strip_comments(source: str) -> str:
        """Source with // and /* */ comments removed.

        The banned-idiom checks below have to run against CODE. This file's comments explain at
        length why toInt() and constrain(..., 0, 180) are wrong, and a plain substring search over
        the whole file matches that explanation and fails on the very change that fixed it — which
        is what happened when this was first written. Naming the mistake is not committing it."""
        source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
        return re.sub(r"//[^\n]*", " ", source)

    def _const(self, name: str) -> int:
        m = re.search(rf"^const\s+int\s+{name}\s*=\s*(\d+)\s*;", self.code, re.MULTILINE)
        self.assertIsNotNone(m, f"{name} not found in {self._INO.name}")
        return int(m.group(1))

    def test_angle_min_matches_config(self):
        self.assertEqual(self._const("ANGLE_MIN"), SERVO_MIN)

    def test_angle_max_matches_config(self):
        self.assertEqual(self._const("ANGLE_MAX"), SERVO_MAX)

    def test_no_full_range_constrain_remains(self):
        # The defect itself: constrain(..., 0, 180) is the full mechanical range, which is the one
        # window the SG90 must not be driven to. Written as a search over the source rather than a
        # count, so it fails on a NEW one as well as on a surviving one.
        offenders = re.findall(r"constrain\s*\([^;]*?,\s*0\s*,\s*180\s*\)", self.code)
        self.assertEqual(offenders, [], f"full-range constrain() still in the sketch: {offenders}")

    def test_no_toint_coercion_remains(self):
        # String::toInt() returns 0 for anything it cannot parse, and 0 is a slam into the stop —
        # so the one input it cannot report is also the worst thing it can command.
        self.assertNotIn("toInt()", self.code,
                         f"toInt() still called in {self._INO.name}; use parseAngle() instead")

    def test_parser_digit_cap_admits_every_legal_angle(self):
        # parseAngle() rejects fields longer than 3 digits. That is only safe while every angle the
        # host can emit is at most 3 digits, which SERVO_MAX bounds. Pins the link between the two
        # so raising SERVO_MAX past 999 cannot silently make the firmware reject valid commands.
        self.assertGreaterEqual(SERVO_MIN, 0)
        self.assertLessEqual(SERVO_MAX, 999)
        for angle in (SERVO_MIN, SERVO_MAX, (SERVO_MIN + SERVO_MAX) // 2):
            self.assertLessEqual(len(str(angle)), 3)


if __name__ == '__main__':
    unittest.main()
