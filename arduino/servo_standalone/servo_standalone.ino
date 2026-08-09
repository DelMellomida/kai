#include <Servo.h>

Servo sg90;

void setup() {
  sg90.attach(9);
}

void loop() {
  sg90.write(0);
  delay(2000);
  sg90.write(180);
  delay(2000);
}
