#!/bin/bash
set -e

BOARD="${1:-uno}"
PORT="/dev/ttyUSB0"

AVR_BIN="/usr/share/arduino/hardware/tools/avr/bin"
AVR_GCC="$AVR_BIN/avr-gcc"
AVR_GPP="$AVR_BIN/avr-g++"
OBJCOPY="$AVR_BIN/avr-objcopy"
AVRDUDE="/usr/bin/avrdude"
AVRDUDE_CONF="/etc/avrdude.conf"

CORE="/usr/share/arduino/hardware/arduino/avr/cores/arduino"
SERVO_SRC="/home/devconph/Arduino/libraries/Servo/src"
SKETCH="/home/devconph/Documents/kai/arduino/servo_standalone/servo_standalone.ino"
BUILD="/tmp/servo_standalone_build"

if [ "$BOARD" = "uno" ]; then
    VARIANT="/usr/share/arduino/hardware/arduino/avr/variants/standard"
    BDEF="-DARDUINO_AVR_UNO"
    UPLOAD_BAUD="115200"
else
    VARIANT="/usr/share/arduino/hardware/arduino/avr/variants/eightanaloginputs"
    BDEF="-DARDUINO_AVR_NANO"
    UPLOAD_BAUD="57600"
fi

MCU="atmega328p"
F_CPU="16000000L"
DEFINES="-mmcu=$MCU -DF_CPU=$F_CPU -DARDUINO=10815 $BDEF -DARDUINO_ARCH_AVR"
OPTS="-Os -w -ffunction-sections -fdata-sections"
CFLAGS="$DEFINES $OPTS -std=gnu11"
CPPFLAGS="$DEFINES $OPTS -std=gnu++11 -fpermissive -fno-exceptions -fno-threadsafe-statics"
INCLUDES="-I$CORE -I$VARIANT -I$SERVO_SRC"

echo "=== Building servo_standalone for Arduino $BOARD ==="
rm -rf "$BUILD" && mkdir -p "$BUILD"

echo "[1/4] Compiling Arduino core..."
ALL_OBJS=""

for f in "$CORE"/*.c; do
    obj="$BUILD/core_$(basename $f .c).o"
    $AVR_GCC -c $CFLAGS $INCLUDES "$f" -o "$obj"
    ALL_OBJS="$ALL_OBJS $obj"
done

for f in "$CORE"/*.cpp; do
    obj="$BUILD/core_$(basename $f .cpp).o"
    $AVR_GPP -c $CPPFLAGS $INCLUDES "$f" -o "$obj"
    ALL_OBJS="$ALL_OBJS $obj"
done

for f in "$CORE"/*.S; do
    [ -f "$f" ] || continue
    obj="$BUILD/core_$(basename $f .S)_asm.o"
    $AVR_GCC -c -x assembler-with-cpp $DEFINES $INCLUDES "$f" -o "$obj"
    ALL_OBJS="$ALL_OBJS $obj"
done

echo "[2/4] Compiling Servo library..."
$AVR_GPP -c $CPPFLAGS $INCLUDES "$SERVO_SRC/avr/Servo.cpp" -o "$BUILD/Servo.o"
ALL_OBJS="$ALL_OBJS $BUILD/Servo.o"

echo "[3/4] Compiling sketch..."
echo '#include <Arduino.h>' > "$BUILD/sketch.cpp"
cat "$SKETCH" >> "$BUILD/sketch.cpp"
$AVR_GPP -c $CPPFLAGS $INCLUDES "$BUILD/sketch.cpp" -o "$BUILD/sketch.o"

echo "[4/4] Linking..."
$AVR_GCC -w -Os -mmcu=$MCU -Wl,--gc-sections \
    -o "$BUILD/firmware.elf" \
    "$BUILD/sketch.o" $ALL_OBJS -lm

$OBJCOPY -O ihex -R .eeprom "$BUILD/firmware.elf" "$BUILD/firmware.hex"

echo ""
if [ ! -e "$PORT" ]; then
    echo "ERROR: $PORT not found."
    exit 1
fi

echo "=== Uploading to $PORT ==="
$AVRDUDE -C "$AVRDUDE_CONF" -q -p atmega328p -c arduino \
    -P "$PORT" -b $UPLOAD_BAUD -D \
    -U "flash:w:$BUILD/firmware.hex:i"

echo ""
echo "Done. Servo should now sweep 0-180-0 on its own. LED blinks in sync."
