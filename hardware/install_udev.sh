#!/bin/bash
# Install udev rule so CH340 Arduino auto-appears as /dev/ttyUSB0 on every plug
set -e

echo "=== Installing CH340 udev rule ==="

# Write the bind helper script
sudo tee /usr/local/bin/ch341-bind.sh > /dev/null << 'SCRIPT'
#!/bin/bash
# Called by udev when CH340 device appears
DEV="$1"
sleep 1

# Load modules if needed
modprobe usbserial 2>/dev/null || true
KOPATH="/home/devconph/Documents/kai/hardware/ch341_build/ch341.ko"
if ! lsmod | grep -q ch341; then
    insmod "$KOPATH" 2>/dev/null || true
fi
sleep 0.5

# Bind interface 1.0 of the device
echo "${DEV}:1.0" > /sys/bus/usb/drivers/ch341/bind 2>/dev/null || true
SCRIPT

sudo chmod +x /usr/local/bin/ch341-bind.sh

# Write the udev rule
sudo tee /etc/udev/rules.d/99-ch341-arduino.rules > /dev/null << 'RULE'
ACTION=="add", SUBSYSTEM=="usb", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", RUN+="/usr/local/bin/ch341-bind.sh %k"
RULE

# Reload udev
sudo udevadm control --reload-rules
sudo udevadm trigger

echo "Done. Unplug and replug the Arduino — it should auto-appear as /dev/ttyUSB0."
