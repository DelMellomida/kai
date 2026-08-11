#include <Servo.h>

Servo pan_servo;
Servo jaw_servo;

const int PAN_PIN  = 9;
const int TILT_PIN = 10;   // no tilt hardware — pin left undriven
const int JAW_PIN  = 6;
const int LED_PIN  = 13;

// Mechanical travel limits. MUST be kept in step with SERVO_MIN / SERVO_MAX in config/servo.py,
// which is the source of truth and carries the reason: a Tower Pro SG90 overshoots into its
// mechanical stop at 0 and at 180. tests/test_servo.py::TestFirmwareAngleLimits reads this file
// and fails if the two ever drift apart, because a comment asking to keep them in step is not a
// mechanism and nothing else here would notice.
//
// servo/servo.py's send()/send_jaw() clamp to the same window, and this is NOT a duplicate of that
// check. It is the copy that still holds when the wire corrupts a line — which is precisely when
// the host's clamp has already been applied and then destroyed in transit. The link is
// fire-and-forget by design (no checksum, no echo, no ack), so nothing downstream can tell a
// mangled line from a real one. The CH340 is documented as flapping under servo brownout, so this
// is a correlated failure rather than a hypothetical: a slam to 0 draws stall current on the same
// rail whose sag caused the flap.
//
// One window covers both axes: JAW_OPEN in config/tracking.py is already pinned at SERVO_MAX.
const int ANGLE_MIN = 10;
const int ANGLE_MAX = 170;

// Detach the servos after this long with no command, so they stop holding position
// (SG90s buzz and draw current while attached, even when idle).
//
// There IS a one-time twitch on the first move after idle, but not for the reason it looks like:
// Servo::attach() does not reset the pulse width (only the constructor sets DEFAULT_PULSE_WIDTH —
// see the library's avr/Servo.cpp), so re-attaching resumes pulsing at the last angle written, not
// at centre. The twitch is MECHANICAL: while detached the head is unpowered and sags a little, and
// the first pulse after re-attach snaps it back to where the firmware still thinks it is. Nothing
// here can fix that; the only cures are keeping the servos attached (the current draw and buzz this
// detach exists to avoid) or a stiffer mount. Raising IDLE_DETACH_MS makes it rarer, not smaller.
const unsigned long IDLE_DETACH_MS = 4000;
unsigned long lastCmdMs = 0;
bool servosAttached = false;

void attachServos() {
  if (!servosAttached) {
    pan_servo.attach(PAN_PIN, 600, 2300);
    jaw_servo.attach(JAW_PIN, 600, 2300);
    servosAttached = true;
  }
}

void detachServos() {
  if (servosAttached) {
    pan_servo.detach();
    jaw_servo.detach();
    servosAttached = false;
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  attachServos();
  pan_servo.write(90);
  jaw_servo.write(90);
  digitalWrite(LED_PIN, LOW);
  lastCmdMs = millis();
  Serial.println("READY");
}

// Gesture ack: flash the LED twice (no serial echo — fire-and-forget).
//
// NON-BLOCKING, and that matters more than it looks. This used to be four delay(60) calls: 240 ms in
// which loop() never read the serial port. The host sends pan at 10 Hz and MOUTH_COOLDOWN in
// config/gesture.py is 0.8 s, so a person simply TALKING to Kai triggers a gesture about every 0.8 s
// — meaning pan commands spent roughly a third of every conversation sitting in the 64-byte RX buffer.
// They were then drained back-to-back, so the head froze for a quarter second and lurched through
// several PAN_MAX_STEPs of travel in one servo move. That was the twitch people actually noticed.
// Driven off millis() instead, the ack looks identical and serial never stops being read.
const unsigned long ACK_BLINK_MS    = 60;
const int           ACK_BLINK_EDGES = 4;    // on, off, on, off
int           ackEdgesLeft = 0;
unsigned long ackNextMs    = 0;

void startGestureAck(char code) {
  ackEdgesLeft = ACK_BLINK_EDGES;
  ackNextMs    = millis();
}

// Owns the LED while a blink is in flight; the pan command path defers to it (see loop()), so the
// two don't fight over the pin now that they can overlap in time.
void serviceGestureAck() {
  if (ackEdgesLeft <= 0) return;
  if ((long)(millis() - ackNextMs) < 0) return;
  digitalWrite(LED_PIN, (ackEdgesLeft % 2) ? HIGH : LOW);
  ackEdgesLeft--;
  ackNextMs = millis() + ACK_BLINK_MS;
}

// Parse one numeric field, strictly. Returns false — and writes nothing to `out` — for an empty
// field or any character that is not a digit.
//
// This replaces String::toInt(), which does not report failure: it returns 0 for "", for "J", for
// "9O" and for a field of pure line noise. 0 is not an inert value here, it is a hard slam to the
// end of travel, so the one input the parser cannot distinguish was also the worst thing it could
// command. Rejecting is always safe by comparison — the host re-sends pan at 10 Hz and jaw at
// 20 Hz, so a dropped line costs at most one frame of an already self-correcting stream.
//
// No sign is accepted: every angle on this wire is 0..180 by construction, and a '-' can only be
// corruption. The 3-digit cap keeps `value` far from overflow and rejects a run-together line
// ("90,9012,90") that would otherwise parse as a plausible-looking number.
bool parseAngle(const String &field, int &out) {
  int n = field.length();
  if (n == 0 || n > 3) return false;
  int value = 0;
  for (int i = 0; i < n; i++) {
    char c = field[i];
    if (c < '0' || c > '9') return false;
    value = value * 10 + (c - '0');
  }
  out = value;
  return true;
}

void loop() {
  serviceGestureAck();

  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) return;

    attachServos();
    lastCmdMs = millis();

    if (line.startsWith("G:") && line.length() >= 3) {
      startGestureAck(line[2]);
      return;
    }

    // Jaw-only fast channel: "J<angle>" — writes only the jaw servo (20 Hz speech path),
    // leaving pan untouched so it doesn't fight the pan/tilt command stream.
    if (line[0] == 'J') {
      int jaw;
      if (!parseAngle(line.substring(1), jaw)) return;   // malformed: write nothing at all
      jaw_servo.write(constrain(jaw, ANGLE_MIN, ANGLE_MAX));
      return;
    }

    // Accepts "pan\n", "pan,tilt\n", or "pan,tilt,jaw\n". Tilt has no hardware (ignored).
    // The jaw servo is written ONLY when a 3rd (jaw) field is present, so 2-field pan/tilt
    // commands don't clobber the jaw the 'J' channel is driving.
    int comma  = line.indexOf(',');
    int comma2 = comma >= 0 ? line.indexOf(',', comma + 1) : -1;

    // Every field is parsed and checked BEFORE any servo is written. A line is applied whole or
    // not at all: a good pan with a corrupt jaw must not move the pan either, or a line truncated
    // mid-flight becomes a half-command — the head turning to a real angle while the mouth holds a
    // stale one, with nothing to indicate the command was never complete.
    int pan;
    int jaw = -1;                        // -1 = this line carries no jaw field
    if (comma >= 0) {
      if (!parseAngle(line.substring(0, comma), pan)) return;
      // Tilt is validated and then thrown away. There is no tilt hardware (see R10), but garbage
      // in the tilt field means this LINE is corrupt, and the pan field sitting next to it on the
      // same line has no better claim to being intact.
      int tilt;
      String tiltField = comma2 >= 0 ? line.substring(comma + 1, comma2)
                                     : line.substring(comma + 1);
      if (!parseAngle(tiltField, tilt)) return;
      if (comma2 >= 0 && !parseAngle(line.substring(comma2 + 1), jaw)) return;
    } else {
      if (!parseAngle(line, pan)) return;
    }

    if (jaw >= 0) {
      jaw_servo.write(constrain(jaw, ANGLE_MIN, ANGLE_MAX));
    }
    pan = constrain(pan, ANGLE_MIN, ANGLE_MAX);
    pan_servo.write(pan);
    if (ackEdgesLeft <= 0) {           // a gesture blink in flight owns the LED; don't fight it
      digitalWrite(LED_PIN, pan < 90 ? HIGH : LOW);
    }
    return;
  }

  if (servosAttached && millis() - lastCmdMs > IDLE_DETACH_MS) {
    detachServos();
  }
}
