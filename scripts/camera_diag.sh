#!/bin/bash
# Is a camera actually reachable? Run this after every hardware change.
#
#   sudo bash scripts/camera_diag.sh
#
# Answers one question in plain language: does a sensor ANSWER on the CSI bus? Everything else about a
# camera failure follows from that, and it is the one thing you cannot tell by looking at the ribbon.
#
# Why this exists: a CSI sensor that fails to answer produces `-121` (EREMOTEIO, "no acknowledgement")
# in dmesg and nothing else -- no /dev/video0, no driver binding, and an ordinary `i2cdetect` sweep
# shows an empty bus whether or not a camera is attached. That is because the sensor's power rail is
# only energised for ~10 ms while the driver probes it, and a full sweep takes 50-100 ms, so it misses
# the window. This script probes single addresses (about 1 ms each) in a tight loop while forcing the
# driver to re-bind, so it lands INSIDE the power window -- and it checks the addresses used by other
# common modules too, so "it is not actually an IMX219" shows up instead of hiding.
#
# Companion to servo/servo_diag.py. Read-only apart from driver bind/unbind, which the kernel does
# every boot anyway.

set -u

BOLD=$(tput bold 2>/dev/null || true); OFF=$(tput sgr0 2>/dev/null || true)
say()  { printf "%s\n" "$*"; }
head2() { printf "\n%s%s%s\n" "$BOLD" "$*" "$OFF"; }

if [ "$(id -u)" -ne 0 ]; then
  say "Needs root for the i2c probes and dmesg. Re-run:  sudo bash $0"
  exit 1
fi

# Candidate sensor addresses across the modules people actually plug into a Jetson.
#   0x10 imx219 (Pi cam v2), imx477     0x1a imx477 alt, imx290
#   0x36 ov5647 (Pi cam v1)             0x3c ov5693, many Arducam
#   0x18 0x1b ar0234 and friends        0x64 0x6c on-module EEPROM variants
ADDRS="0x10 0x18 0x1a 0x1b 0x36 0x3c 0x64 0x6c"

head2 "1. Capture devices the kernel created"
if ls /dev/video* >/dev/null 2>&1; then
  ls -1 /dev/video* | sed 's/^/   /'
  say "   -> a capture device exists, so something IS working."
else
  say "   none. No /dev/video* means no sensor was successfully brought up."
fi

head2 "2. USB cameras attached"
if lsusb 2>/dev/null | grep -iqE "cam|webcam|uvc"; then
  lsusb | grep -iE "cam|webcam|uvc" | sed 's/^/   /'
else
  say "   none (a USB webcam would appear here and needs no reboot)"
fi

# Which physical connector is an i2c bus? Do NOT hardcode this: the bus numbers are assigned by the
# i2c mux in whatever order it enumerates, and on this board bus 9 is port C while bus 10 is port A --
# the opposite of the obvious guess. Read the sensor's device-tree node name instead (NVIDIA names them
# rbpcv2_imx219_a / _c), so the label always matches the hardware.
port_of_bus() {
  local bus="$1" node
  node=$(readlink -f "/sys/bus/i2c/devices/${bus}-0010/of_node" 2>/dev/null)
  case "${node##*/}" in
    *_a@*) echo "CAM0 (port A)";;
    *_c@*) echo "CAM1 (port C)";;
    *)     echo "bus $bus";;
  esac
}

head2 "3. CSI ports declared by the device tree"
FOUND_PORTS=0
for d in /sys/bus/i2c/devices/*-0010; do
  [ -e "$d" ] || continue
  BUS=${d##*/}; BUS=${BUS%%-*}
  say "   $(port_of_bus "$BUS")  (i2c bus $BUS, address 0x10)"
  FOUND_PORTS=1
done
[ "$FOUND_PORTS" = 0 ] && say "   NONE -- the camera overlay is not applied. Check OVERLAYS in /boot/extlinux/extlinux.conf."

head2 "4. What the kernel said when it probed at boot"
if dmesg | grep -qiE "imx219|imx477|ov5647"; then
  # head, not tail: step 5 below force-binds the driver ~50 times, so the LATEST probe lines are ones
  # this script caused itself. The earliest ones are the real boot probe, which is what matters.
  dmesg | grep -iE "imx219|imx477|ov5647" | head -6 | sed 's/^/   /'
  if dmesg | grep -q "error during i2c read probe (-121)"; then
    say ""
    say "   -121 = EREMOTEIO, 'no acknowledgement'. The Jetson's i2c controller drove the bus"
    say "   correctly and nothing answered. That is a cable or module fault, NOT the SoC:"
    say "   a dead bus would report -110 (timeout) or an arbitration error instead."
  fi
else
  say "   no sensor driver messages at all -- see step 3, the overlay may be missing."
fi

head2 "5. Live probe: does any sensor answer while powered?"
say "   Cycling the sensor rail and probing $(echo $ADDRS | wc -w) addresses on each port..."
ANY_HIT=0
for BUS in 9 10; do
  [ -e "/sys/bus/i2c/devices/${BUS}-0010" ] || continue
  PORT=$(port_of_bus "$BUS")
  HITS=$(mktemp)
  (
    for _ in $(seq 1 600); do
      for A in $ADDRS; do
        i2cget -y "$BUS" "$A" 0x00 b >/dev/null 2>&1 && echo "$A"
      done
    done
  ) > "$HITS" 2>&1 &
  SCAN=$!

  for DRV in /sys/bus/i2c/drivers/imx219 /sys/bus/i2c/drivers/imx477; do
    [ -w "$DRV/bind" ] || continue
    for _ in $(seq 1 25); do
      echo "${BUS}-0010" > "$DRV/bind" 2>/dev/null
      sleep 0.05
    done
  done

  sleep 1
  kill $SCAN 2>/dev/null; wait $SCAN 2>/dev/null

  if [ -s "$HITS" ]; then
    say "   $PORT: ANSWERED at $(sort -u "$HITS" | tr '\n' ' ')"
    ANY_HIT=1
  else
    say "   $PORT: silent at every address"
  fi
  rm -f "$HITS"
done

head2 "Verdict"
if ls /dev/video* >/dev/null 2>&1; then
  say "   A capture device exists. The camera path is up."
elif [ "$ANY_HIT" = 1 ]; then
  say "   A sensor IS answering, but no capture device was created."
  say "   The cable and module are fine -- this is a software mismatch. If the address above is not"
  say "   0x10, the module is not an IMX219 and the wrong overlay is loaded; pick the matching"
  say "   /boot/tegra234-p3767-camera-*.dtbo in /boot/extlinux/extlinux.conf and reboot."
else
  say "   Nothing answered on any port, at any address, while powered."
  say "   Software is ruled out: the driver loaded, the ports are declared, the bus transacted."
  say "   That leaves the RIBBON or the MODULE. Swap one at a time and re-run this:"
  say "     - different ribbon, same module  -> if it answers, the ribbon was bad (most common)"
  say "     - same ribbon, different module  -> if it answers, the module was bad"
  say "   A USB webcam is the fastest way to get a working camera meanwhile; it hot-plugs."
fi
say ""
