# Setup

### On the Jetson

The full list lives in **[`requirements.txt`](../requirements.txt)** — 16 third-party packages, one
line each, annotated with which file imports it and which ones cannot come from PyPI. Read its
header before installing anything: **do not `pip3 install -r` it on the live robot.** The Jetson's
torch is hand-built with CUDA, and a PyPI reinstall silently replaces it with a CPU-only wheel.

The face-tracking half alone needs:

```bash
pip3 install mediapipe opencv-python pyserial numpy

# Kernel module for the Arduino's CH340 USB chip
# (ch341 is NOT in the Jetson's tegra kernel — see docs/rnd/challenges.md)
# Pre-built: hardware/ch341_build/ch341.ko
```

The voice half adds `sounddevice openwakeword pvporcupine webrtcvad-wheels faster-whisper scipy
requests piper-tts fastembed pypdf flask`, plus Ollama, plus the I2S/PulseAudio routing in
[`scripts/autostart.sh`](../scripts/autostart.sh) — again, see `requirements.txt` for the order and
the traps.

To record the exact versions this robot is running (read-only, safe on a live robot):

```bash
./scripts/freeze_requirements.sh    # → requirements.lock.txt
```

That lock file is what an SD-card rebuild restores from. `requirements.txt` says *what*;
the lock file says *which version*.

### On the Laptop (network camera mode only)

```bash
pip install opencv-python
```

`laptop_camera.py` is self-contained and needs nothing else.

---

## Setup Guide


All commands below are run **from the project root** (`~/Documents/kai` on this robot). Several
scripts and every `python3 -m` invocation depend on that — running a module by file path instead
puts its own directory on `sys.path` and the package imports fail.

### Step 1 — Load the CH340 USB driver

The Jetson's stock kernel does not include `ch341` (the driver for the Arduino's CH340 USB chip).

```bash
# One-time: load usbserial first, then the custom ch341
sudo modprobe usbserial
sudo insmod hardware/ch341_build/ch341.ko

# Verify
ls /dev/ttyUSB0   # should appear
```

If `/dev/ttyUSB0` doesn't appear after insmod, bind manually:

```bash
sudo bash hardware/fix_usb.sh
```

For persistent auto-binding on every plug (run once):

```bash
sudo bash hardware/install_udev.sh
```

You do not normally have to do any of this by hand. `scripts/run.sh` loads the driver and falls
back to the manual bind if needed, and `servo/servo.py` calls the same path on demand when the port
is missing at startup.

### Step 2 — Upload the Arduino sketch

The sketch receives servo commands over serial and drives the servos at 5V. Compile and upload from
the Jetson — no Arduino IDE needed:

```bash
bash hardware/ch341_build/build_servo_serial.sh
```

This uses `avr-gcc` from `/usr/share/arduino/hardware/tools/avr/bin/` and uploads via `avrdude`.

> **The wire protocol.** The sketch accepts `"90\n"` (pan only), `"90,45\n"` (pan,tilt) and
> `"90,45,120\n"` (pan,tilt,jaw), plus `"J<angle>\n"` for the jaw-only fast channel and
> `"G:<code>\n"` for a gesture acknowledgement. **Pan and jaw are attached; tilt is not** — pin 10
> is declared and left undriven, so the tilt field is parsed and discarded. A two-field command
> deliberately leaves the jaw untouched, so pan updates never fight the 20 Hz jaw channel.

### Step 3 — Verify the servos work

```bash
python3 -m servo.servo_serial --sweep
```

The pan servo should physically sweep 0° → 180° → 0° and park at 90°. If it doesn't move, check the
wiring (Orange on pin 9, Red on 5V, Brown on GND) before debugging software.

### Step 4 — Run it

**Everything (how the robot actually runs):**
```bash
python3 -u face_track.py --no-display --flip --jaw --wake
```

**With a laptop as the camera** — on the laptop:
```bash
python vision/laptop_camera.py     # prints the laptop's IP
```
then on the Jetson:
```bash
python3 -u face_track.py --network <laptop-ip> --no-display --flip
```

Open `http://<jetson-ip>:8081` for the dashboard.

> Add `--flip` if the head tracks in the wrong direction. Drop `--wake` to disable hands-free and
> use the dashboard's push-to-talk button only.

### Step 5 — Start on boot

`scripts/autostart.sh` is the supervisor: it exports `KAI_SUPERVISED=1`, waits for the capture
device to be released between runs, relaunches on exit code 7 (the dashboard's Restart button) and
treats exit code 3 (another instance already holds the lock) as terminal. Install it as an
`@reboot` cron entry — and run `loginctl enable-linger devconph` so that session survives logout,
or PulseAudio will not be up and every reply is silent before anyone logs in.
