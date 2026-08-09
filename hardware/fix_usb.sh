#!/bin/bash
# Fix CH340 USB binding so /dev/ttyUSB0 appears
set -e

echo "=== CH340 USB Fix ==="

# 1. Load modules if not already loaded
echo "[1/3] Loading kernel modules..."
if ! lsmod | grep -q usbserial; then
    sudo modprobe usbserial
    echo "  usbserial loaded"
fi

KOPATH="/home/devconph/Documents/kai/hardware/ch341_build/ch341.ko"
if ! lsmod | grep -q ch341; then
    if [ -f "$KOPATH" ]; then
        sudo insmod "$KOPATH"
        echo "  ch341 loaded from $KOPATH"
    else
        echo "  ERROR: ch341.ko not found at $KOPATH"
        exit 1
    fi
fi

# 2. Find the CH340 USB interface and bind it
echo "[2/3] Binding ch341 driver to CH340 device..."
BOUND=0
for IF in /sys/bus/usb/devices/*:1.0; do
    [ -e "$IF" ] || continue
    VENDOR=$(cat "$IF/../idVendor" 2>/dev/null || true)
    PRODUCT=$(cat "$IF/../idProduct" 2>/dev/null || true)
    if [ "$VENDOR" = "1a86" ] && [ "$PRODUCT" = "7523" ]; then
        IFNAME=$(basename "$IF")
        echo "  Found CH340 interface: $IFNAME"
        # unbind from any existing driver
        if [ -e "$IF/driver" ]; then
            echo "$IFNAME" | sudo tee /sys/bus/usb/drivers/ch341/unbind > /dev/null 2>&1 || true
        fi
        echo "$IFNAME" | sudo tee /sys/bus/usb/drivers/ch341/bind > /dev/null 2>&1 && BOUND=1 || true
        break
    fi
done

if [ "$BOUND" = "0" ]; then
    echo "  CH340 device not found. Is the Arduino plugged in?"
    echo "  Try: lsusb | grep 1a86"
    exit 1
fi

# 3. Wait and verify
sleep 0.5
echo "[3/3] Checking /dev/ttyUSB0..."
if ls /dev/ttyUSB* 2>/dev/null | head -1; then
    echo ""
    echo "SUCCESS: Arduino is available"
else
    echo "  /dev/ttyUSB0 not created yet. Wait 2 seconds and try: ls /dev/ttyUSB*"
fi
