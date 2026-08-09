#!/usr/bin/env python3
"""
Servo diagnostic / interactive tool — thin CLI wrapper around servo.ServoSerial.

Run: python3 servo_serial.py
     python3 servo_serial.py --port /dev/ttyACM0
     python3 servo_serial.py --sweep
"""

import sys
import time
import argparse

try:
    import serial.tools.list_ports
except ImportError:
    print("pyserial not installed. Run: pip3 install pyserial")
    sys.exit(1)

from servo.servo import ServoSerial


def detect_arduino() -> str | None:
    """Scan serial ports for likely Arduino devices."""
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        desc = (p.description or '').lower()
        mfr  = (p.manufacturer or '').lower()
        if any(x in desc or x in mfr for x in ('arduino', 'ch340', 'cp210', 'ftdi', 'acm', 'usb serial')):
            return p.device
    for p in ports:
        if 'ttyUSB' in p.device or 'ttyACM' in p.device:
            return p.device
    return None


def sweep(s: ServoSerial) -> None:
    """Sweep pan servo 0° → 180° → 0° in 10° steps."""
    print("\nSweeping 0° -> 180° -> 0°...")
    for angle in list(range(0, 181, 10)) + list(range(180, -1, -10)):
        s._force_send(angle, 90)
        print(f"  {angle:3}°")
        time.sleep(0.1)
    s._force_send(90, 90)
    print("Sweep done. Parked at 90°.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Servo diagnostic tool")
    parser.add_argument('--port',  default=None)
    parser.add_argument('--sweep', action='store_true', help='Run auto sweep then exit')
    args = parser.parse_args()

    port = args.port or detect_arduino() or "/dev/ttyUSB0"
    print(f"Connecting to Arduino on {port}...")
    try:
        s = ServoSerial(port)
    except Exception as e:
        print(f"Could not open {port}: {e}")
        sys.exit(1)

    if args.sweep:
        sweep(s)
        s.close()
        return

    # position test
    print("\nRunning position test: 0° -> 90° -> 180°")
    for angle in [0, 90, 180]:
        s._force_send(angle, 90)
        print(f"  -> {angle}°")
        time.sleep(1.0)
    s._force_send(90, 90)

    # interactive mode
    print("\nType an angle (0-180) or 'q' to quit:")
    while True:
        try:
            raw = input("  Angle: ").strip()
            if raw.lower() in ('q', 'quit', 'exit'):
                break
            angle = max(0, min(180, int(float(raw))))
            s._force_send(angle, 90)
            print(f"  -> {angle}°")
        except ValueError:
            print("  Enter a number or 'q'")
        except (EOFError, KeyboardInterrupt):
            break

    s._force_send(90, 90)
    s.close()
    print("Done.")


if __name__ == '__main__':
    main()
