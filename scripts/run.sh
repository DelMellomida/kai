#!/bin/bash
# Wrapper: loads CH340 USB driver if needed, then runs face_track.py
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"   # project root
MODULE_PATH="$ROOT/hardware/ch341_build/ch341.ko"
SCRIPT="$ROOT/face_track.py"

# Load ch341 if /dev/ttyUSB0 is missing
if [ ! -e /dev/ttyUSB0 ]; then
    echo "[run.sh] /dev/ttyUSB0 not found — loading ch341 driver..."
    sudo modprobe usbserial
    sudo insmod "$MODULE_PATH"
    sleep 0.5

    # Manual bind if still not showing
    if [ ! -e /dev/ttyUSB0 ]; then
        echo "[run.sh] Still missing — attempting manual bind..."
        sudo bash "$ROOT/hardware/fix_usb.sh"
        sleep 0.5
    fi

    if [ ! -e /dev/ttyUSB0 ]; then
        echo "[run.sh] ERROR: /dev/ttyUSB0 still not found. Check USB connection."
        exit 1
    fi

    echo "[run.sh] /dev/ttyUSB0 ready."
fi

python3 -u "$SCRIPT" "$@"
