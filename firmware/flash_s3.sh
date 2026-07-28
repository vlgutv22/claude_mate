#!/usr/bin/env bash
#
# Compile and flash the ESP32-S3 wireless companion.
#
# Use this INSTEAD of `arduino-cli upload`, which cannot flash this board: the
# Waveshare ESP32-S3-LCD-1.47B has three quirks that all have to be worked
# around at once, and the stock upload recipe trips over every one of them.
#
#   1. DOWNLOAD MODE. `--before default-reset` (esptool's default, and what
#      arduino-cli uses) does not work here -- it drives the virtual DTR/RTS
#      boot straps and the chip ignores them, so you get "Failed to connect:
#      No serial data received". `--before usb-reset` does a USB-level reset
#      instead and works from a running app, so no BOOT/RESET press is needed.
#
#   2. THE LINK CANNOT SUSTAIN A LARGE TRANSFER. A single ~680 KB compressed
#      stream stalls at a random point (seen anywhere from 2% to 24%) with the
#      USB node still present. Failure probability scales with bytes moved per
#      connection: ~20 KB writes are reliable, 128 KB ones mostly are not. So
#      the app is written in 32 KB pieces, one esptool run each, and every
#      piece is hash-verified by the chip. Expect ~10 absorbed stalls per run;
#      that is normal here, not a fault.
#
#      A stall can leave the chip out of the bootloader, which `--before
#      no-reset` cannot recover from, so retries alternate no-reset (fast) and
#      usb-reset (re-enters download mode). That makes the flash self-healing.
#
#   3. BOOTING AFTERWARDS. `--after hard-reset` re-asserts the GPIO0 strap and
#      lands straight back in download mode: dark screen, silent serial,
#      esptool refusing to reconnect -- indistinguishable from a bricked
#      board. Boot with `--after watchdog-reset` instead.
#
# Verifying the whole app with `verify-flash` can never complete (a 1 MB
# readback hits the same ceiling as a 1 MB write). That is a readback limit,
# not a bad image -- trust the per-piece "Hash of data verified", which is a
# device-side MD5, and the read-back verify of the small regions below.
#
# Usage:  firmware/flash_s3.sh [/dev/cu.usbmodemXXX]
#
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SKETCH_DIR=$HERE/claude_mate_s3
BUILD=$HERE/build/s3          # under build/, which .gitignore already covers
CHUNK=32768
MAX_TRY=12

# CDCOnBoot=cdc is REQUIRED: without it Serial maps to UART0 instead of USB and
# the `?`/`W|`/`S|`/`T|` config commands are unreachable. The 3 MB app partition
# is needed because WiFi + display does not fit the default 1.2 MB.
FQBN="esp32:esp32:waveshare_esp32_s3_lcd_147:CDCOnBoot=cdc,USBMode=hwcdc,PartitionScheme=app3M_fat9M_16MB"

ARDUINO_CLI=${ARDUINO_CLI:-$(command -v arduino-cli || echo "$HOME/bin/arduino-cli")}
[ -x "$ARDUINO_CLI" ] || { echo "arduino-cli not found (set ARDUINO_CLI=)" >&2; exit 1; }

DATA=$("$ARDUINO_CLI" config get directories.data)
PKG=$DATA/packages/esp32
CORE_VER=$(ls "$PKG/hardware/esp32" 2>/dev/null | sort -V | tail -1)
[ -n "$CORE_VER" ] || { echo "esp32 core not installed: arduino-cli core install esp32:esp32" >&2; exit 1; }
ESPTOOL=$(ls -d "$PKG"/tools/esptool_py/*/esptool 2>/dev/null | sort -V | tail -1)
[ -x "${ESPTOOL:-}" ] || { echo "esptool not found under $PKG/tools/esptool_py" >&2; exit 1; }
BOOT_APP0=$PKG/hardware/esp32/$CORE_VER/tools/partitions/boot_app0.bin
[ -f "$BOOT_APP0" ] || { echo "boot_app0.bin not found at $BOOT_APP0" >&2; exit 1; }

PORT=${1:-$(ls /dev/cu.usbmodem* 2>/dev/null | head -1 || true)}
[ -n "$PORT" ] || { echo "no /dev/cu.usbmodem* -- is the board plugged in?" >&2; exit 1; }

echo "=== compiling ==="
"$ARDUINO_CLI" compile -b "$FQBN" --build-path "$BUILD" "$SKETCH_DIR" | tail -3

APP=$BUILD/claude_mate_s3.ino.bin
LOG=$BUILD/flash.log

echo
echo "=== entering download mode via usb-reset on $PORT ==="

# usb-reset reliably puts the chip in the bootloader, but esptool 5.3.1 often
# dies during the sync that follows -- sometimes a clean "failed to connect",
# sometimes an unhandled IndexError traceback -- because this board's link is
# lossy. The reset itself still took effect, so alternate usb-reset (force the
# bootloader) with no-reset (we are probably already there) and just retry.
enter_download() {
  local try=1 before
  while [ "$try" -le 8 ]; do
    if [ $((try % 2)) -eq 1 ]; then before=usb-reset; else before=no-reset; fi
    if "$ESPTOOL" --chip esp32s3 --port "$PORT" --before $before --after no-reset \
         --connect-attempts 2 flash-id >"$LOG" 2>&1; then
      [ "$try" -gt 1 ] && echo "  (entered on attempt $try via $before)"
      return 0
    fi
    try=$((try + 1))
  done
  return 1
}

if ! enter_download; then
  echo "  could not enter download mode automatically. Last output:" >&2
  tr '\r' '\n' <"$LOG" | grep -v '^[[:space:]]*$' | tail -4 | sed 's/^/    /' >&2
  echo >&2
  echo "  Hold BOOT, tap RESET, release BOOT -- waiting up to 3 min..." >&2
  ok=no
  for _ in $(seq 1 60); do
    if "$ESPTOOL" --chip esp32s3 --port "$PORT" --before no-reset --after no-reset \
         --connect-attempts 1 flash-id >"$LOG" 2>&1; then ok=yes; break; fi
    sleep 3
  done
  [ "$ok" = yes ] || { echo "  never entered download mode." >&2; exit 1; }
  echo "  manual download mode detected."
fi
grep -iE '^MAC|chip type|detected flash' "$LOG" | sed 's/^/  /'

FAILS=0

# put <addr> <file> <label>
put() {
  local addr=$1 file=$2 label=$3 try=1 before size
  size=$(stat -f%z "$file")
  while [ "$try" -le "$MAX_TRY" ]; do
    if [ $((try % 2)) -eq 1 ]; then before=no-reset; else before=usb-reset; fi
    if "$ESPTOOL" --chip esp32s3 --port "$PORT" --before $before --after no-reset \
         --connect-attempts 2 \
         write-flash -z --flash-mode keep --flash-freq keep --flash-size keep \
         "$(printf '0x%x' "$addr")" "$file" >"$LOG" 2>&1 \
       && grep -q "Hash of data verified" "$LOG"; then
      printf '  0x%06x +%-7d ok%s  %s\n' "$addr" "$size" \
        "$([ "$try" -gt 1 ] && printf ' (try %d)' "$try")" "$label"
      return 0
    fi
    FAILS=$((FAILS + 1)); try=$((try + 1))
  done
  printf '  0x%06x +%-7d FAILED after %d tries  %s\n' "$addr" "$size" "$MAX_TRY" "$label" >&2
  tr '\r' '\n' <"$LOG" | grep -i fatal | tail -1 | sed 's/^/    /' >&2
  return 1
}

echo
echo "=== bootloader / partition table / otadata ==="
put 0     "$BUILD/claude_mate_s3.ino.bootloader.bin" bootloader
put 32768 "$BUILD/claude_mate_s3.ino.partitions.bin" partitions
put 57344 "$BOOT_APP0"                               otadata

echo
echo "=== app image, 32 KB pieces ==="
W=$BUILD/chunks; rm -rf "$W"; mkdir -p "$W"
split -b "$CHUNK" "$APP" "$W/p_"
i=0; total=$(ls "$W"/p_* | wc -l | tr -d ' ')
for f in "$W"/p_*; do
  put $((65536 + i * CHUNK)) "$f" "piece $((i + 1))/$total"
  i=$((i + 1))
done

echo
echo "=== read-back verify of the small regions ==="
for spec in "0:$BUILD/claude_mate_s3.ino.bootloader.bin:bootloader" \
            "32768:$BUILD/claude_mate_s3.ino.partitions.bin:partitions"; do
  a=${spec%%:*}; rest=${spec#*:}; f=${rest%%:*}; l=${rest##*:}
  ok=no
  for t in 1 2 3 4 5 6 7 8; do
    if [ $((t % 2)) -eq 1 ]; then bf=no-reset; else bf=usb-reset; fi
    if "$ESPTOOL" --chip esp32s3 --port "$PORT" --before $bf --after no-reset \
         --connect-attempts 2 verify-flash "$(printf '0x%x' "$a")" "$f" >"$LOG" 2>&1; then
      ok=yes; break
    fi
    grep -qi differ "$LOG" && { ok=DIFFERS; break; }
  done
  echo "  $l: $ok"
done

echo
echo "$FAILS stalled attempts absorbed (a handful is normal on this board)"
echo "=== booting the app (RTC watchdog, NOT hard-reset) ==="
"$ESPTOOL" --chip esp32s3 --port "$PORT" --before no-reset --after watchdog-reset \
  --connect-attempts 2 flash-id 2>&1 | tr '\r' '\n' | grep -iE 'watchdog|fatal' | tail -1

echo
echo "Flashed. An unprovisioned board comes up in the WiFi setup portal;"
echo "send 'W|<ssid>|<pass>' and 'T|<token>' over $PORT, or hold BOOT at"
echo "power-on for the portal. '?' prints the current config."
