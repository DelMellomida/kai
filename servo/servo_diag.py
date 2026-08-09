#!/usr/bin/env python3
"""Slow, obvious servo movement test — 0° for 3s, 180° for 3s, repeat 3x."""

import time

from servo.servo import ServoSerial
from config.servo import SERVO_PORT as PORT


def main() -> None:
    print(f"Opening {PORT}...")
    s = ServoSerial(PORT)

    print("\n>>> CENTER first (90°) — servo should go to neutral <<<")
    s._force_send(90, 90)
    time.sleep(3)

    for i in range(3):
        print(f"\n--- Pass {i + 1}/3 ---")
        print(">>> MINIMUM (0°) — horn should rotate to one end <<<")
        s._force_send(0, 90)
        time.sleep(3)
        print(">>> MAXIMUM (180°) — horn should rotate to other end <<<")
        s._force_send(180, 90)
        time.sleep(3)

    print("\n>>> Parking at CENTER (90°) <<<")
    s._force_send(90, 90)
    time.sleep(1)
    s.close()
    print("Done.")


if __name__ == '__main__':
    main()
