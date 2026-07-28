# Firmware

Two devices speak the same protocol, byte for byte. The daemon cannot tell them
apart, and **both can be connected at once**.

| | `claude_mate/` (iteration 1) | `claude_mate_s3/` (iteration 2) |
|---|---|---|
| Board | Arduino Nano | Waveshare ESP32-S3-LCD-1.47**B** |
| Display | 128×32 mono OLED (SSD1306) | 172×320 colour IPS (ST7789) |
| Transport | USB serial | **Wi-Fi (TCP)**, USB as fallback |
| Buttons | 3 — PREV / GO / NEXT | 4 — PREV / GO / NEXT / **MIRROR** |
| Alert LED | one LED, rhythm only | WS2812, rhythm **+ colour** |
| Power | USB | USB or Li-ion cell, with a battery gauge |
| Flash with | `arduino-cli upload` | **`./flash_s3.sh`** (see below) |

The Nano build is **unchanged** by the S3 work and remains fully supported. The
S3 is an addition, not a replacement — the extra pixels buy typography, colour
and the terminal mirror, never a different information model.

---

## Arduino Nano — `claude_mate/`

Wiring is in [`docs/WIRING.md`](../docs/WIRING.md).

```bash
arduino-cli compile -b arduino:avr:nano:cpu=atmega328 firmware/claude_mate
arduino-cli upload  -b arduino:avr:nano:cpu=atmega328 -p /dev/cu.usbserial-XXXX \
                    firmware/claude_mate
```

Boards shipping the **new** bootloader need `cpu=atmega328` (as above) at
115200; older ones want `cpu=atmega328old`. Libraries: Adafruit SSD1306 and
Adafruit GFX.

`selftest/` is a standalone sketch that exercises the OLED, buttons and LED with
no daemon attached — flash it first when bringing up new hardware.

---

## ESP32-S3 — `claude_mate_s3/`

A cordless companion: colour LCD, four buttons, and a Wi-Fi link to the daemon
so it can sit anywhere on a battery.

- `claude_mate_s3.ino` — protocol and UI
- `board_s3.h` — pin map, layout geometry, palette, battery/charging constants
- `netcfg.h` — Wi-Fi transport, mDNS discovery, HMAC handshake, NVS config,
  setup portal

**Library:** *GFX Library for Arduino* (Arduino_GFX, by moononournation).

### Board options

```
esp32:esp32:waveshare_esp32_s3_lcd_147
    CDCOnBoot=cdc          REQUIRED
    USBMode=hwcdc
    PartitionScheme=app3M_fat9M_16MB
```

`CDCOnBoot=cdc` is **not** the board default, and without it `Serial` maps to
UART0 instead of USB — the `?` / `W|` / `S|` / `T|` config commands then have
nowhere to go. The 3 MB app partition is required because Wi-Fi plus the display
does not fit the default 1.2 MB.

`PSRAM=enabled` already means **OPI** on this board (`psram_type=opi`, correct
for the S3R8); do not "fix" it.

### Wiring you add

The bare board has only BOOT and RESET. Add four momentary switches, each
`INPUT_PULLUP` with its other leg to **GND** — no resistors, no debounce caps
(the firmware debounces in software, and all four grounds may share one wire):

```
PREV → GPIO 3     GO → GPIO 4     NEXT → GPIO 5     MIRROR → GPIO 6
```

GPIO 3 is a strapping pin but an inert one: it selects the JTAG source and is
only sampled when `STRAP_JTAG_SEL` is burned, which is not the factory default.
Boot mode is decided by GPIO 0 and GPIO 46, neither of which is wired to a
button, so no button held through a reset can strand the board in download mode.
Verify with `espefuse summary` if in doubt.

**Battery:** nothing to solder. Plug a Li-ion cell into the onboard battery
header; the charger and the sense divider are on-board, landing on GPIO 1. Do
not connect anything to GPIO 1 yourself.

**Board revisions differ in exactly one pin:** the backlight is **GPIO 46** on
the ‑1.47B and GPIO 48 on the original ‑1.47. The wrong value fails in a
maddening way — the ST7789 is driven perfectly and the panel simply never
lights, which looks like a dead board or a bad flash.

### Flashing: use `./flash_s3.sh`

```bash
./firmware/flash_s3.sh              # compiles, then flashes the first usbmodem
./firmware/flash_s3.sh /dev/cu.usbmodemXXX
```

**`arduino-cli upload` does not work on this board.** Three quirks bite at once:

1. **Download mode.** `--before default-reset` (esptool's default, and what
   arduino-cli uses) drives the virtual DTR/RTS straps, which this board
   ignores — you get *"Failed to connect: No serial data received"*.
   `--before usb-reset` does a USB-level reset and works from a running app,
   with no BOOT/RESET press.
2. **The link cannot sustain a large transfer.** One ~680 KB stream stalls at a
   random point (2 %–24 % observed) with the USB node still present. So the app
   is written in **32 KB pieces**, one esptool run each, every piece verified by
   a device-side MD5. Roughly ten absorbed stalls per run is normal here, not a
   fault; retries alternate `no-reset` / `usb-reset` so a stall self-heals.
3. **Booting afterwards.** `--after hard-reset` re-asserts the GPIO 0 strap and
   lands straight back in download mode — dark screen, silent serial, esptool
   refusing to reconnect, indistinguishable from a bricked board. The script
   finishes with `--after watchdog-reset`.

Verifying the whole app with `verify-flash` can never complete: a 1 MB readback
hits the same ceiling as a 1 MB write, so it would fail on a perfectly good
image. Trust the per-piece hash plus the read-back verify of the small regions,
which the script does.

If automatic entry fails, the script falls back to prompting for a manual
**BOOT + RESET** (hold BOOT, tap RESET, release BOOT).

### First boot and provisioning

An unprovisioned board comes up in a **Wi-Fi setup portal** and shows an access
point name (`Claude-Mate-XXXX`) and a password that is regenerated on every
portal start. Join it from a phone, open `http://192.168.4.1`, and fill in the
network, password, **shared token**, and port (8787); leave the host blank to
discover the daemon over mDNS.

> The token box is written unconditionally — submitting the form with it **empty
> erases a token already stored**. Re-enter it every time you use the portal.

Or provision over USB serial:

```
?                     print the current config (never the token itself)
W|<ssid>|<password>   set Wi-Fi credentials
S|<host>|<port>       set the daemon address (empty host = mDNS discovery)
T|<token>             set the shared secret (must match the daemon's)
X|WIPE                clear all stored config
R                     reboot
Z                     start the setup portal now
```

`?` also reports the link state and the battery gauge's raw millivolts, which is
the quickest way to tell a mis-set `BATT_DIVIDER` from a genuinely flat cell.

The daemon side needs `--tcp` (or `CLAUDE_MATE_TCP=1`) and a token in
`~/.config/claude-mate/token`. Config lives in NVS, a separate partition, so it
**survives a reflash**.

### The terminal mirror

Tapping MIRROR opens a live view of the selected session's real terminal,
refreshed about once a second; PREV/NEXT then scroll it and GO closes it and
raises the actual window. Only **wrapped** sessions can be mirrored — a
hook-only session has no PTY for the daemon to read and says so. See
[`docs/PROTOCOL.md`](../docs/PROTOCOL.md) for the wire format.

Terminal contents cross the network on the same token-authenticated but
**plaintext** TCP link as everything else. That is a bigger exposure than a
status string; keep it to networks you trust.
