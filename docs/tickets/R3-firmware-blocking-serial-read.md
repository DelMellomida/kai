# R3 — Firmware serial read can block the loop for a full second

| | |
|---|---|
| **Tier** | 2 |
| **Severity** | Medium |
| **Effort** | Small |
| **Confidence** | Medium |
| **Lens** | Robotics |

## Location

- `arduino/servo_serial/servo_serial.ino` — `loop()`, `Serial.readStringUntil('\n')` and the
  `String line` parsing that follows
- Related, and the precedent for this fix: the same file's `serviceGestureAck()` / `startGestureAck()`,
  which were made non-blocking for exactly this reason

## Problem

`Serial.readStringUntil('\n')` blocks until it sees the terminator **or** until the `Stream` class's
default 1000 ms timeout expires. Any line that arrives without a trailing newline — a truncated
write during a USB flap, a lost byte at 115200 baud, a host process killed mid-`write()` — stalls
`loop()` for a full second.

During that second nothing else runs: `serviceGestureAck()` is not called, so a gesture blink
freezes mid-sequence, and the 64-byte hardware RX buffer overflows while the host keeps sending pan
commands at 10 Hz and jaw commands at 20 Hz. When the timeout finally expires, the buffered
commands are drained back-to-back.

`String` is also the wrong tool on an AVR: `readStringUntil` plus `substring()` allocate and free
heap on every single command, at up to 30 commands per second for the life of the run, which
fragments the small heap over long uptimes.

## Why it matters

This is the same failure the LED-ack rework was introduced to eliminate, re-entering through the
parser. That comment records the measured consequence precisely: commands sitting in the RX buffer
while `loop()` is blocked, then drained at once, so "the head froze for a quarter second and lurched
through several `PAN_MAX_STEP`s of travel in one servo move." A 1000 ms stall is four times worse
than the 240 ms one that was already judged unacceptable — and it lands during USB instability,
i.e. alongside R1's host-side stall, compounding it.

## Acceptance criteria

- [ ] `loop()` contains no blocking read. Every pass consumes only the bytes currently available
      (`Serial.available()`) and returns.
- [ ] No `String` in the command path — parsing works over a fixed `char` buffer sized for the
      longest legal line, with a compile-time constant naming that length.
- [ ] A line longer than the buffer is discarded up to and including the next `'\n'`, without
      writing any servo and without corrupting the parse of the following line.
- [ ] A partial line with no newline leaves the parser in a waiting state indefinitely without
      stalling — subsequent bytes complete it, and `serviceGestureAck()` continues to run
      throughout.
- [ ] All four existing command forms still parse identically: `"pan\n"`, `"pan,tilt\n"`,
      `"pan,tilt,jaw\n"`, `"J<angle>\n"`, `"G:<code>\n"`. Two-field commands must still leave the
      jaw untouched; `J` must still leave pan untouched.
- [ ] `IDLE_DETACH_MS` behaviour is unchanged — `lastCmdMs` is updated on a *complete, valid* line,
      not on partial input.
- [ ] Verified on hardware: streaming deliberately truncated lines (no trailing newline) produces no
      stall — measured by keeping a concurrent 10 Hz pan stream running and confirming smooth motion
      throughout, and by an LED blink that completes on schedule.
- [ ] Verified on hardware over a long run: no heap-exhaustion symptoms after an extended session
      (free-RAM probe stable rather than declining).

## Suggested approach

Replace the read with an incremental, non-blocking accumulator:

```
// sketch
const uint8_t LINE_MAX = 24;
char lineBuf[LINE_MAX];
uint8_t lineLen = 0;
bool overflowed = false;

void loop() {
  serviceGestureAck();
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen && !overflowed) { lineBuf[lineLen] = '\0'; handleLine(lineBuf, lineLen); }
      lineLen = 0; overflowed = false;
      continue;
    }
    if (lineLen >= LINE_MAX - 1) { overflowed = true; continue; }  // swallow to end of line
    lineBuf[lineLen++] = c;
  }
  if (servosAttached && millis() - lastCmdMs > IDLE_DETACH_MS) detachServos();
}
```

`handleLine()` then does the existing dispatch — `G:` prefix, `J` prefix, else the comma-separated
numeric form — over `char*` using `strchr()` for the separators instead of `indexOf()`/`substring()`.

Two notes on scope:

- The `while (Serial.available())` loop drains the buffer each pass, which is what removes the
  overflow risk. It is bounded by the buffer size, so it cannot itself become a stall.
- This ticket pairs with **R4** (firmware angle limits and rejection of malformed fields). The
  `char*` parsing here is the natural place to add R4's `parseAngle()` validation, so if both are
  scheduled, do this one first and fold R4 into `handleLine()`.

The `TILT_PIN` constant and the ignored tilt field are out of scope here — see **R10**.
