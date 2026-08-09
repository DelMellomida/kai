# R&D Findings

### Jetson GPIO voltage is a hard constraint
There is no software workaround for the 3.3V GPIO limit. PWM duty cycle, frequency, and signal shape are all correct at 3.3V — the issue is purely voltage level. Any servo requiring 5V signal needs a level shifter or a microcontroller bridge.

### pulseIn() on Arduino is unreliable with 3.3V input
Even with a correct 1500µs PWM signal from the Jetson, Arduino's `pulseIn()` consistently read ~535µs due to marginal voltage detection. The signal physically crossed the digital threshold but noise caused early false-low detection. This approach was abandoned in favour of serial communication.

### Serial communication is more reliable than PWM passthrough
Sending integer angle values over USB serial (115200 baud) is deterministic, noise-immune, and removes all analog signal issues. The Arduino parses `"90\n"` and calls `sg90.write(90)`. Latency is under 5ms.

### MediaPipe FaceMesh is viable on Jetson Orin Nano at 320×240
At full 640×480 resolution, MediaPipe struggles to maintain real-time throughput on CPU. Resizing to 320×240 before processing gives ~25–30fps with acceptable landmark accuracy. Nose tip (landmark #1) is stable enough for servo tracking.

### EMA + dead zone vs PD controller
v1 used EMA (low-pass filter) + dead zone (output gate). The combination works but has two failure modes: EMA adds lag uniformly regardless of motion speed, and the dead zone causes abrupt snapping when the threshold is crossed. v2 replaces both with a PD controller: the D term naturally dampens noise without gating (no snapping), and the P term scales correction to actual error (no uniform lag). The PD approach is more principled and produces smoother, more responsive motion.

### The ch341 module must be manually compiled for Jetson
NVIDIA does not include `ch341` in the tegra kernel. The module can be compiled from Linux 5.15 kernel source against Jetson headers. `usbserial` must be loaded first as a dependency. Without a udev rule, the module loads but doesn't auto-bind to the device on hot-plug.

### Servo startup calibration must bypass smoothing logic
On startup, `send(90)` hit the dead zone check (`|90 - 90| = 0 < 8`) and silently skipped. The servo never actually centered. Fix: write directly to serial (`self._ser.write(b"90\n")`) during `__init__`, bypassing all filtering.
