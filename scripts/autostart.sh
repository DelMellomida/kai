#!/bin/bash
# Runs face_track.py on boot via cron @reboot (no sudo needed).
# Install: bash autostart.sh --install
# Remove:  bash autostart.sh --remove
# Logs:    tail -f /tmp/face-servo.log

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"   # project root (face_track.py lives here)
CRON_TAG="face-servo-autostart"
LOG="/tmp/face-servo.log"

# ── install / remove helpers ──────────────────────────────────────────────────
if [[ "$1" == "--install" ]]; then
    # Remove existing entry first, then add fresh
    ( crontab -l 2>/dev/null | grep -v "$CRON_TAG" ; \
      echo "@reboot sleep 15 && bash ${SCRIPT_DIR}/autostart.sh >> $LOG 2>&1  # $CRON_TAG" \
    ) | crontab -
    echo "Installed @reboot cron job."
    echo "Logs will appear at: $LOG"
    crontab -l | grep "$CRON_TAG"
    exit 0
fi

if [[ "$1" == "--remove" ]]; then
    crontab -l 2>/dev/null | grep -v "$CRON_TAG" | crontab -
    echo "Removed cron job."
    exit 0
fi

# ── wait for nvargus-daemon ───────────────────────────────────────────────────
for i in $(seq 1 10); do
    systemctl is-active --quiet nvargus-daemon && break
    echo "[autostart] Waiting for nvargus-daemon... ($i/10)"
    sleep 2
done

# ── apply INMP441 I2S capture route (best-effort) ─────────────────────────────
# The app itself also does this (ai/voice_assistant.py: apply_i2s_route) before probing the mic;
# doing it here too means the route is up even if face_track.py is launched some other way. It is
# idempotent, needs no sudo (user must be in the `audio` group), and mirrors mictest/RESULTS.md.
# If the APE card / amixer is absent it's skipped and the app falls back to the USB mic.
I2S_CARD="APE"
apply_ctl() { amixer -c "$I2S_CARD" cset name="$1" "$2" >/dev/null 2>&1; }
if amixer -c "$I2S_CARD" contents >/dev/null 2>&1; then
    apply_ctl "I2S2 codec master mode"        "cbs-cfs"   # Jetson = I2S master
    apply_ctl "I2S2 codec frame mode"         "i2s"
    apply_ctl "I2S2 Sample Rate"              "48000"
    apply_ctl "I2S2 Capture Audio Bit Format" "32"
    apply_ctl "I2S2 Client Bit Format"        "32"
    apply_ctl "I2S2 Client Channels"          "2"
    apply_ctl "I2S2 Capture Audio Channels"   "2"
    apply_ctl "I2S2 FSYNC Width"              "31"
    apply_ctl "ADMAIF1 Mux"                   "I2S2"       # XBAR: I2S2 -> ADMAIF1 (capture)
    echo "[autostart] applied I2S capture route on card ${I2S_CARD}"
else
    echo "[autostart] APE card not found — skipping I2S route (app will fall back to USB mic)"
fi

# ── PulseAudio access ─────────────────────────────────────────────────────────
# @reboot cron starts us with no XDG_RUNTIME_DIR, so pactl/paplay can't find the user's
# PulseAudio socket ($XDG_RUNTIME_DIR/pulse/native). Without it the app's mic setup
# (ai/voice_assistant.py: free_i2s_device -> pactl suspend-source) fails with "Connection
# refused", leaving pulse holding the INMP441's APE card at 44.1kHz + injecting noise, so
# Whisper only ever hears garble (~0.2 language confidence) and voice turns never work.
# Export it here (respecting an already-set value) so every pulse call in the app succeeds.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# ── secrets ───────────────────────────────────────────────────────────────────
# Optional: put `export PICOVOICE_ACCESS_KEY=...` in ~/.kai_env to keep it out of the repo and out
# of the crontab. The app also reads ~/.config/kai/porcupine.key directly, which is the more
# reliable of the two under @reboot cron (no login shell, and HOME may not even be set) — either
# source works, and with neither the wake word is simply off and push-to-talk still works.
# shellcheck source=/dev/null
[[ -f "${HOME}/.kai_env" ]] && source "${HOME}/.kai_env"

# ── wait for PulseAudio to exist ──────────────────────────────────────────────
# Exporting XDG_RUNTIME_DIR above only helps if a PulseAudio is actually LISTENING on that socket.
# Pulse is a per-user-session service, so under @reboot cron -- with nobody logged in -- it may not be
# running at all, and then every reply dies with "pa_context_connect() failed: Connection refused":
# Kai looks completely healthy, speaks in the dashboard, animates the jaw, and is silent. That is
# exactly the failure this waits for, and it is fixed permanently by letting the user manager start at
# boot without a login:
#
#     sudo loginctl enable-linger devconph     # already applied on this robot
#
# Kept as a wait-and-warn rather than a hard failure: playback is not worth refusing to start over, and
# the mic, camera, servos and dashboard are all unaffected.
PULSE_SOCK="${XDG_RUNTIME_DIR}/pulse/native"
for i in $(seq 1 20); do
    [[ -S "$PULSE_SOCK" ]] && pactl info >/dev/null 2>&1 && break
    echo "[autostart] waiting for PulseAudio at ${PULSE_SOCK}... ($i/20)"
    sleep 1
done
if pactl info >/dev/null 2>&1; then
    echo "[autostart] PulseAudio ready ($(pactl info 2>/dev/null | sed -n 's/^Default Sink: //p'))"
    # Hand every capture card back to ALSA BEFORE the app probes mics. The app does this itself
    # (free_i2s_device), but its first probe can start before the suspend has taken effect — and a probe
    # of a pulse-held device blocks for 3 s, then reads as "not live", so the real mic gets skipped for a
    # 44.1 kHz pulse device that cannot resample to 16 kHz and hands-free dies. Doing it here, once,
    # removes that race. Monitors are left alone: they tap an output and hold no capture hardware.
    while read -r _ name _; do
        case "$name" in
            *.monitor|"") continue;;
        esac
        pactl suspend-source "$name" 1 >/dev/null 2>&1 \
            && echo "[autostart] released capture source from pulse: $name"
    done < <(pactl list short sources 2>/dev/null)
else
    echo "[autostart] WARNING: no PulseAudio on ${PULSE_SOCK} — Kai will run but replies will be" \
         "SILENT (jaw and dashboard still work). Fix: sudo loginctl enable-linger $(id -un)"
fi

# ── wait for the capture device to be free ────────────────────────────────────
# The INMP441 is opened as a RAW hw device, which admits exactly one opener, and opening it while
# something else still holds it BLOCKS indefinitely rather than failing. On a cold boot nothing else
# has it; on a manual restart the outgoing face_track may not have released it yet, and the symptom is
# nasty — hands-free silently never comes up (sess_mic_live stays False) while face tracking runs
# perfectly, with nothing in the log to say why. A few seconds of patience here avoids that entirely.
# A function, not a one-shot: the supervisor below re-runs it before every relaunch, because a crash
# is exactly the case where the device was NOT released tidily.
wait_for_capture_device() {
    for i in $(seq 1 15); do
        busy=0
        for f in /dev/snd/pcm*c; do fuser "$f" >/dev/null 2>&1 && busy=1; done
        [[ "$busy" == 0 ]] && return 0
        echo "[autostart] capture device busy, waiting... ($i/15)"
        sleep 1
    done
}
wait_for_capture_device

# ── launch, and keep it running ───────────────────────────────────────────────
# Run from the project root so the servo/ ai/ vision/ packages are importable.
# --wake turns on hands-free listening ("Hey Kai" + VAD turn-taking, see config/wake.py). Drop it to
# go back to push-to-talk only; the dashboard mic button works either way.
#
# Supervised rather than exec'd. face_track.py's main loop only catches KeyboardInterrupt, so any
# other exception ends the process — and cron is @reboot, so nothing brought it back until somebody
# rebooted the robot. Ollama has had Restart=always this whole time; the robot itself had nothing.
#
# Exit codes decide whether to restart:
#   0                       — orderly shutdown (Ctrl-C, or SIGTERM now that face_track handles it).
#                             Someone asked it to stop, so stop.
#   3 (_EXIT_ALREADY_RUNNING) — another instance holds the lock. Restarting would spin forever
#                             against a robot that is already healthy.
#   7 (_EXIT_RESTART)       — the dashboard's restart button. Relaunch at once, and do NOT count it
#                             as a failure: a deliberate restart is not a crash.
#   anything else           — crash or kill: restart.
#
# Backoff exists so a start that fails instantly (missing hardware, a syntax error in a fresh edit)
# does not become a hot loop that buries the log and eats the CPU the running services need.
RESTART_DELAY=5          # seconds after a crash that had been running a while
RESTART_DELAY_MAX=60     # ceiling for repeated fast failures
MIN_HEALTHY_S=60         # ran at least this long => treat the next failure as a fresh one

# Tells face_track.py that something WILL start it again, so the dashboard's restart button can
# promise a robot that comes back. Exported here and nowhere else: a run started by hand
# (scripts/run.sh, or python3 face_track.py over ssh) genuinely has no supervisor, and the button
# warns instead of quietly leaving the robot off.
export KAI_SUPERVISED=1

fails=0
while true; do
    started=$SECONDS
    python3 "${ROOT}/face_track.py" --jaw --rotate 90 --no-display --wake
    rc=$?
    ran=$(( SECONDS - started ))

    if [[ $rc -eq 0 ]]; then
        echo "[autostart] face_track exited cleanly (ran ${ran}s) — not restarting"
        break
    fi
    if [[ $rc -eq 3 ]]; then
        echo "[autostart] another face_track instance is already running — not restarting"
        break
    fi
    if [[ $rc -eq 7 ]]; then
        echo "[autostart] restart requested from the dashboard (ran ${ran}s) — relaunching now"
        fails=0
        wait_for_capture_device
        continue
    fi

    if [[ $ran -ge $MIN_HEALTHY_S ]]; then
        fails=0                      # it was up and healthy; this is a new problem, not a loop
    else
        fails=$(( fails + 1 ))
    fi
    delay=$(( RESTART_DELAY * (fails > 0 ? fails : 1) ))
    [[ $delay -gt $RESTART_DELAY_MAX ]] && delay=$RESTART_DELAY_MAX

    echo "[autostart] face_track exited rc=${rc} after ${ran}s (consecutive fast failures: ${fails})" \
         "— restarting in ${delay}s"
    sleep "$delay"
    wait_for_capture_device
done
