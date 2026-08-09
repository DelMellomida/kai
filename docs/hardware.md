# Hardware

## Core

| Component | Notes |
|-----------|-------|
| NVIDIA Jetson Orin Nano | Any variant; tested on 8GB Super. 8 GB is the binding constraint — see [memory-budget.md](memory-budget.md) |
| Arduino Uno | CH340 clone works; needs the ch341 kernel module on the Jetson (not in the tegra kernel) |
| SG90 9g Micro Servo × 1 | **Pan** axis, Arduino pin 9. Orange = signal, Red = VCC, Brown = GND |
| SG90 9g Micro Servo × 1 | **Jaw**, Arduino pin 6. Driven by speech, and by mouth-mirroring when idle |
| USB-A to USB-B cable | Arduino to Jetson |
| Jumper wires (female-female) | 3 wires per servo to the Arduino |

## Camera — one of

| Component | Notes |
|-----------|-------|
| CSI camera (IMX219 or similar) | The ribbon camera. Probed via `nvarguscamerasrc`; see the CSI bind note in [operating.md](operating.md) |
| USB webcam | Genuinely hot-pluggable, unlike CSI |
| Laptop on the same network | Runs `vision/laptop_camera.py`, streams JPEG over TCP 8485 |

Kai runs with **none** of these attached — "no camera" is a reported state, not a failure.

## Voice

| Component | Notes |
|-----------|-------|
| INMP441 I2S MEMS microphone | The default mic. Captured raw on ALSA card `APE` at 48 kHz with PulseAudio suspended — see `config/voice.py` |
| USB audio dongle (C-Media) | Output DAC. Named as a PulseAudio sink in `TTS_SINK`, and its card profile is asserted on every start because Pulse flips it to S/PDIF unprompted |
| PAM8403 amplifier + speaker | Driven from the dongle's analog jack |
| USB microphone *(optional)* | Automatic fallback when the I2S mic reads silent or refuses to open |

> **The I2S wiring is not recorded in this repo.** `config/voice.py` documents the *software* side
> in detail — the XBAR/I2S2 route applied by `apply_i2s_route()`, the 48 kHz clock, stereo capture
> with real audio only in the left slot — but the physical pinout to the 40-pin header lived in a
> `mictest/RESULTS.md` that is not committed here. Recover it from the running robot before
> rewiring.

## Optional / not currently wired

| Component | Notes |
|-----------|-------|
| SG90 9g Micro Servo × 1 | **Tilt** axis, pin 10. The `--tilt` flag, the wire protocol and the dashboard field all exist, but the firmware declares `TILT_PIN` and never attaches a servo to it — see [ticket R10](tickets/R10-tilt-axis-plumbed-without-hardware.md) |
| Pan-tilt bracket | Only needed if the tilt axis is actually wired |

> **Servo quality matters.** A faulty servo (internally broken) can appear to respond but won't move or will behave erratically. If the servo buzzes but doesn't rotate during a standalone sweep test, replace it before debugging software.

> **Servos share the Arduino's USB 5V rail.** That rail is why `SEND_INTERVAL` (10 Hz pan) and
> `PAN_MAX_STEP` (8°/command) are what they are: faster or larger commands raise average current
> and can brown out the CH340, which drops the USB link mid-track. `config/servo.py` records the
> measurement. The real fix for faster motion is a separate servo supply, not a config change.

---

## Wiring


### As built — pan + jaw

| Servo wire | Pan servo | Jaw servo |
|------------|-----------|-----------|
| **Orange** (signal) | **Pin 9** | **Pin 6** |
| **Red** (power) | **5V** pin | **5V** pin (shared rail) |
| **Brown** (ground) | **GND** pin | **GND** pin (shared) |

The Arduino is powered entirely by its USB connection to the Jetson. Its 5V pin outputs USB power directly to the servos.

```
Arduino board
┌────────────────────────────────┐
│  Pin 9  ──── Pan  Orange       │
│  Pin 6  ──── Jaw  Orange       │
│  Pin 10 ──── (tilt, not wired) │
│  5V     ──── Pan Red + Jaw Red │
│  GND    ──── Pan + Jaw Brown   │
│  USB ←── Jetson USB port       │
└────────────────────────────────┘
```

Both servos are written by the same serial link but on **different channels and different rates**:
pan goes through the `"pan,tilt"` line at 10 Hz, the jaw through `"J<angle>"` at 20 Hz. That split
exists so speech animation stays smooth without raising the pan send rate into the brownout region.

### Pan only

Wire pin 9 as above and omit the jaw. Kai runs unchanged — the jaw channel simply commands nothing,
and `--jaw` (which is a hard AND with the `jaw_enabled` setting) is left off.

> **Do not connect the servo to the Jetson 40-pin header for signal.** 3.3V is insufficient. Power (5V from Pin 2) is fine for the Red wire IF you also share ground through the Arduino, but using Arduino 5V pin is simpler and safer.
