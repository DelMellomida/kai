#include <Servo.h>

Servo pan_servo;
Servo jaw_servo;

const int PAN_PIN  = 9;
const int TILT_PIN = 10;   // no tilt hardware — pin left undriven
const int JAW_PIN  = 6;
const int LED_PIN  = 13;

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
      int jaw = constrain(line.substring(1).toInt(), 0, 180);
      jaw_servo.write(jaw);
      return;
    }

    // Accepts "pan\n", "pan,tilt\n", or "pan,tilt,jaw\n". Tilt has no hardware (ignored).
    // The jaw servo is written ONLY when a 3rd (jaw) field is present, so 2-field pan/tilt
    // commands don't clobber the jaw the 'J' channel is driving.
    int comma  = line.indexOf(',');
    int comma2 = comma >= 0 ? line.indexOf(',', comma + 1) : -1;

    int pan;
    if (comma >= 0) {
      pan = constrain(line.substring(0, comma).toInt(), 0, 180);
      if (comma2 >= 0) {
        int jaw = constrain(line.substring(comma2 + 1).toInt(), 0, 180);
        jaw_servo.write(jaw);
      }
    } else {
      pan = constrain(line.toInt(), 0, 180);
    }

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
