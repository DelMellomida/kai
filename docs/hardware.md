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
| Self-powered USB desktop speaker | Driven from the dongle's analog jack; USB carries power only. **Not** the PAM8403 this table claimed until 2026-08-11 — that amp is not in the build, and several tuning comments in `config/voice.py` still rest on its response |
| USB microphone *(optional)* | Automatic fallback when the I2S mic reads silent or refuses to open |

> **The USB dongle drives only one speaker channel. It is faulty and wants replacing.** Established
> 2026-08-11 by elimination, in this order:
>
> 1. **The Jetson is clean.** Sink `analog-stereo`, channel map `front-left,front-right`, `Mute: no`,
>    balance 0.00, both channels at 86% / −4.00 dB; ALSA `Front Left`/`Front Right` both on; no
>    remap, mono or combine module loaded, stock `default.pa`, every remix setting in `daemon.conf`
>    at its commented default; and every WAV Kai plays is 2-channel with per-channel RMS matched to
>    within 0.05 dB.
> 2. **The speaker is fine.** Both drivers play correctly from a phone, and from a laptop.
> 3. **The dongle reproduces the fault on other hosts.** Moved to a laptop with the same speaker,
>    still one channel.
>
> The by-ear tests in between were inconclusive and are recorded here only so nobody repeats them: a
> left-only and a right-only tone both seemed to come from the working driver, which implies the
> channels are shorted, yet inverting one channel produced no audible cancellation, which implies
> they never meet. Those two cannot both be true. **Swapping the hardware settled it where listening
> could not** — reach for substitution earlier than ear tests next time.

> **Replacing the dongle is not plug-and-play: four constants in `config/voice.py` name this exact
> device.** Work through all of them, and read the new names off the robot with
> `pactl list short sinks`, `pactl list short cards` and `python3 -c "import sounddevice"` rather
> than guessing:
>
> | Constant | Holds | If you forget |
> |---|---|---|
> | `TTS_SINK` | `alsa_output.usb-C-Media_…analog-stereo` | `paplay` fails on every reply — Kai is mute, and the log says `playback failed` |
> | `TTS_CARD` | `alsa_card.usb-C-Media_…` | the profile assertion fails; the sink may vanish when Pulse flips the card to S/PDIF |
> | `TTS_CARD_PROFILE` | `output:analog-stereo+input:mono-fallback` | a different adapter may name its profiles differently; check `pactl list cards` |
> | `SPEAKER_CARD_NAME_HINTS` | `("usb audio device",)` | ⚠️ **fails silently.** This is the guard that stops Kai capturing from the speaker's own card, which segfaulted the process at the startup greeting. It is a case-insensitive substring match on the PortAudio device name — a new dongle with a different name simply stops matching, and the crash comes back with nothing in the log to say why |
>
> The last row is the dangerous one, because everything keeps working until the I2S mic happens to
> fail its liveness probe, which is rare and non-deterministic. **After swapping, confirm the guard
> still bites**: `[mic] skipping input device N … it is on the speaker's own card` should appear in
> `/tmp/face-servo.log`, or the new device should not offer an input at all.

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
