#!/bin/bash
# Re-probe the CSI camera after boot, so a camera the kernel missed still comes up.
#
# Install: sudo bash camera_bind_retry.sh --install     (root's crontab, @reboot)
# Remove:  sudo bash camera_bind_retry.sh --remove
# Manual:  sudo bash camera_bind_retry.sh --now         (skips the post-boot wait)
# Logs:    tail -f /tmp/camera-bind.log
#
# WHY THIS EXISTS
# The kernel probes a CSI sensor exactly ONCE, about 10 s into boot. If that probe fails the camera is
# invisible for the entire boot -- no /dev/video0, nothing for face_track.py's supervisor to find, and
# no way for it to recover, because creating that device node needs a driver bind and that needs root.
#
# Two everyday situations hit this:
#   * the ribbon or module was reconnected while the board was off, and the boot probe still lost;
#   * a CSI camera was connected after boot (CSI is not hot-pluggable in the kernel's eyes).
# Both were observed on this robot: a forced re-bind brought the camera straight up, with the same
# cable and the same configuration that had just failed at boot.
#
# So: if no capture device exists shortly after boot, ask the driver to bind again a few times. When it
# works, /dev/video0 appears and face_track.py's camera supervisor picks it up live within ~5 s -- no
# restart. When there is genuinely no camera, this costs a handful of harmless -121 lines in dmesg.
#
# Deliberately NOT part of autostart.sh: that runs as devconph, and writing to
# /sys/bus/i2c/drivers/*/bind requires root. Keeping it as its own root @reboot entry avoids granting
# the robot process any privilege it does not otherwise need.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CRON_TAG="kai-camera-bind-retry"
LOG="/tmp/camera-bind.log"

ROUNDS=5          # attempts before giving up -- a real camera binds on the first or second
GAP_S=2           # between attempts, so a sensor still settling gets another chance
SETTLE_S=20       # wait after boot before the first attempt: past the kernel's own ~10 s probe and
                  # past nvargus-daemon coming up

if [[ "$1" == "--install" ]]; then
    ( crontab -l 2>/dev/null | grep -v "$CRON_TAG" ; \
      echo "@reboot bash ${SCRIPT_DIR}/camera_bind_retry.sh >> $LOG 2>&1  # $CRON_TAG" \
    ) | crontab -
    echo "Installed @reboot cron job in root's crontab."
    echo "Logs will appear at: $LOG"
    crontab -l | grep "$CRON_TAG"
    exit 0
fi

if [[ "$1" == "--remove" ]]; then
    crontab -l 2>/dev/null | grep -v "$CRON_TAG" | crontab -
    echo "Removed cron job."
    exit 0
fi

# ── watch mode: live feedback while you work on the hardware ──────────────────
# CSI only becomes visible via a driver bind, so after swapping a cable or module you would otherwise
# have to keep running this by hand to find out whether it took. This keeps trying and prints a line
# only when the state CHANGES -- so you can swap a ribbon, re-seat a connector, or wiggle a suspect
# cable and see the make/break the moment it happens. Ctrl-C to stop.
if [[ "$1" == "--watch" ]]; then
    if [ "$(id -u)" -ne 0 ]; then
        echo "Needs root. Re-run:  sudo bash $0 --watch"
        exit 1
    fi
    echo "Watching for camera make/break — wiggle the ribbon and watch. Ctrl-C to stop."
    echo "(only state CHANGES are printed; silence means nothing changed)"
    LAST="unknown"
    while true; do
        if ls /dev/video* >/dev/null 2>&1; then
            STATE="PRESENT"
        else
            # Try to bring it back; the sensor only becomes visible via a driver bind.
            for drv in /sys/bus/i2c/drivers/imx219 /sys/bus/i2c/drivers/imx477; do
                [ -w "$drv/bind" ] || continue
                for d in /sys/bus/i2c/devices/*-0010; do
                    [ -e "$d" ] || continue
                    b=${d##*/}
                    echo "${b%%-*}-0010" > "$drv/bind" 2>/dev/null
                done
            done
            ls /dev/video* >/dev/null 2>&1 && STATE="PRESENT" || STATE="absent"
        fi
        if [ "$STATE" != "$LAST" ]; then
            if [ "$STATE" = "PRESENT" ]; then
                echo "$(date '+%H:%M:%S')  CAMERA APPEARED  ($(ls /dev/video* | tr '\n' ' '))"
            else
                echo "$(date '+%H:%M:%S')  camera lost / not answering"
            fi
            LAST="$STATE"
        fi
        sleep 1
    done
fi

log() { echo "[$(date '+%F %T')] [camera-bind] $*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "Needs root to bind the sensor driver. Re-run:  sudo bash $0 $*"
    exit 1
fi

# Which physical connector is a bus? Derived from the device-tree node name (NVIDIA names them
# rbpcv2_imx219_a / _c), never hardcoded -- the i2c mux assigns bus numbers in its own order, and on
# this board bus 9 is port C while bus 10 is port A.
port_of_bus() {
    local node
    node=$(readlink -f "/sys/bus/i2c/devices/${1}-0010/of_node" 2>/dev/null)
    case "${node##*/}" in
        *_a@*) echo "CAM0/portA";;
        *_c@*) echo "CAM1/portC";;
        *)     echo "bus$1";;
    esac
}

have_capture() { ls /dev/video* >/dev/null 2>&1; }

# At boot (no argument, the cron case) wait past the kernel's own probe first. `--now` skips the wait
# for a manual run.
if [ "${1:-}" != "--now" ]; then
    log "waiting ${SETTLE_S}s for the kernel's own camera probe to finish"
    sleep "$SETTLE_S"
fi

if have_capture; then
    log "capture device already present ($(ls /dev/video* | tr '\n' ' ')) — nothing to do"
    exit 0
fi

# Every declared sensor address, across whichever sensor drivers are loaded.
BUSES=""
for d in /sys/bus/i2c/devices/*-0010; do
    [ -e "$d" ] || continue
    b=${d##*/}; BUSES="$BUSES ${b%%-*}"
done
if [ -z "$BUSES" ]; then
    log "no CSI sensor declared in the device tree — is the camera overlay applied? (OVERLAYS in /boot/extlinux/extlinux.conf)"
    exit 1
fi

log "no capture device after boot; retrying the sensor bind on:$(for b in $BUSES; do printf ' %s(bus %s)' "$(port_of_bus "$b")" "$b"; done)"

for round in $(seq 1 "$ROUNDS"); do
    for drv in /sys/bus/i2c/drivers/imx219 /sys/bus/i2c/drivers/imx477; do
        [ -w "$drv/bind" ] || continue
        for b in $BUSES; do
            echo "${b}-0010" > "$drv/bind" 2>/dev/null
        done
    done
    sleep 0.5
    if have_capture; then
        log "SUCCESS on attempt $round: $(ls /dev/video* | tr '\n' ' ') — face_track.py will pick it up within ~5s"
        for b in $BUSES; do
            [ -e "/sys/bus/i2c/drivers/imx219/${b}-0010" ] && log "  bound: $(port_of_bus "$b") (bus $b)"
        done
        exit 0
    fi
    [ "$round" -lt "$ROUNDS" ] && sleep "$GAP_S"
done

log "still no capture device after $ROUNDS attempts — the sensor is not answering on i2c."
log "That is a cable or module fault, not software. Diagnose with: sudo bash ${SCRIPT_DIR}/camera_diag.sh"
exit 1
