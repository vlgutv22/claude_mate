# Firmware

Two devices speak the same protocol, byte for byte. The daemon cannot tell them
apart, and **both can be connected at once**.

| | `claude_mate/` (iteration 1) | `claude_mate_s3/` (iteration 2) |
|---|---|---|
| Board | Arduino Nano | Waveshare ESP32-S3-LCD-1.47**B** |
| Display | 128×32 mono OLED (SSD1306) | 172×320 colour IPS (ST7789) |
| Transport | USB serial | **Wi-Fi (TCP)**, USB as fallback |
| Buttons | 3 — PREV / GO / NEXT | 4 — PREV / GO / NEXT / **MENU**, Kailh Choc (1350) |
| Alert LED | one LED, rhythm only | WS2812, rhythm **+ colour**, adjustable (incl. off) |
| Power | USB | USB or a 14500 Li-ion cell, with a battery gauge |
| On-device UI | — | a menu: settings, about, Wi-Fi setup, sleep |
| Screen sleep | — | off after 1–30 min when nothing needs you |
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
115200; older ones want `cpu=atmega328old`. Library: **Adafruit GFX**, and only
that one — the SSD1306 is driven by the bundled `softssd1306.h`, a software-I2C
`Adafruit_GFX` subclass that puts SCL on A3 (hardware SCL A5 was damaged) and
leaves SDA on A4. Adafruit SSD1306 is *not* needed; CI compiles this sketch with
Adafruit GFX alone.

`selftest/` is a standalone sketch that exercises the OLED, buttons and LED with
no daemon attached — flash it first when bringing up new hardware.

---

## ESP32-S3 — `claude_mate_s3/`

A cordless companion: colour LCD, four buttons, and a Wi-Fi link to the daemon
so it can sit anywhere on a battery.

- `claude_mate_s3.ino` — protocol, the CONDUCTOR view, the menu, hibernate
- `board_s3.h` — pin map, layout geometry, palette, battery/charging constants
- `netcfg.h` — Wi-Fi transport, mDNS discovery, HMAC handshake, NVS config,
  setup portal
- `settings.h` — the device's own preferences (NVS, `mate-ui` namespace). The
  only state here that is neither the daemon's nor the network's; none of it is
  about a session, so the daemon has no business knowing

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
PREV → GPIO 3     GO → GPIO 4     NEXT → GPIO 5     MENU → GPIO 6
```

The reference build uses **Kailh Choc low-profile (1350) switches** with Choc
keycaps, which is what the printed enclosure
([`assets/3d-model/claude_mate_s3.stl`](../assets/3d-model/claude_mate_s3.stl))
is cut for — three across the bottom, one above them at the left, with the LCD
on a raised shelf to their right. Tactile or linear is taste; the firmware
debounces either. Any momentary switch works if you print your own plate.

GPIO 3 is a strapping pin but an inert one: it selects the JTAG source and is
only sampled when `STRAP_JTAG_SEL` is burned, which is not the factory default.
Boot mode is decided by GPIO 0 and GPIO 46, neither of which is wired to a
button, so no button held through a reset can strand the board in download mode.
Verify with `espefuse summary` if in doubt.

**Battery:** nothing to solder. Plug a **14500 Li-ion cell (AA-sized, 3.7 V,
~800 mAh)** into the onboard battery header; the charger and the sense divider
are on-board, landing on GPIO 1. Do not connect anything to GPIO 1 yourself.

The chemistry is not interchangeable. The board's charger is a **4.2 V Li-ion**
charger, and `board_s3.h` calibrates both the percent curve and the
charge-trend detection for that (`CHARGE_FULL_MV 4150`, `BATT_MIN_MV 3000`). A
3.2 V LiFePO4 cell of the same physical size would be over-charged by the board
*and* read as permanently flat. A larger Li-ion cell (an 18650) is electrically
fine but creeps too slowly for the trend fallback — see the note at the
constants.

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

**Where the token comes from.** Run the daemon with `--tcp` once and it creates
one, prints it, and saves it to `~/.config/claude-mate/token` (mode 0600). Copy
that string into the portal's **Shared token** box. It used to refuse to start
without one, which left you having to know to create the file by hand before the
portal would ever be satisfiable.

> **An empty token box means "keep the one I have".** It used to write
> unconditionally, so coming back to the portal to change networks — filling in
> the Wi-Fi password and nothing else — silently erased the token, after which
> the device could never finish the handshake and just said it was not
> connected. To clear a token deliberately, tick **Erase the stored token**, or
> send `X|WIPE` over serial.

If a connection attempt fails, the reason now shows on the device's bottom line
for 20 seconds (`x no token configured`, `x wifi lost`, …) instead of the device
silently cycling back through `dialing…` — every failure used to look identical
from across the room.

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

### The battery indicator is three segments, not a percentage

Three green, two amber, one red. That is not a simplification for its own sake —
a percentage was actively misleading here, and the failure is easy to reproduce:
unplug a full cell and the reading falls from 4200 mV to about 3950 mV within
minutes, so the display went **100 % → 79 %** with nothing wrong.

Every one of those numbers was honest. The display was still a lie:

- **4200 mV is the charger's constant-voltage point, not the cell's charge.**
  While plugged in you are reading the charger, not the battery.
- **A Li-ion just off charge carries surface charge** that relaxes over tens of
  minutes. That fall is not consumption.
- **The curve is steep here** — roughly 9 mV per percent — so ordinary ADC noise
  on an unfiltered divider is worth whole percentage points.

A number invites you to trust its last digit. Three segments promise only what
one voltage reading through a noisy divider can deliver, and the thresholds
(3900 / 3700 mV) are placed so a fresh cell's relaxation stays inside the top
segment instead of visibly draining.

Two guards sit behind it, stopping different things. **Hysteresis** (60 mV, fall
only — rise on touch) stops a voltage sitting on a threshold flickering between
levels. A **45-second hold** stops a genuine but brief excursion — a Wi-Fi TX
burst, a backlight step, the sag as the screen wakes — from moving the display at
all. Neither alone is enough: hysteresis lets a long sag through, and a hold
alone still flickers once it expires. The EMA also slowed from 1/8 to 1/16
(~80 s), since a three-level display has no use for speed.

The exact percentage and millivolts are still in `?` and on the About page.
Diagnosis wants the number; a glance wants the shape.

The daemon side needs `--tcp` (or `CLAUDE_MATE_TCP=1`) and a token in
`~/.config/claude-mate/token`. Config lives in NVS, a separate partition, so it
**survives a reflash**.

### The 4th button carries three gestures

It is the only button whose meaning the daemon does not own, which makes it the
only place the device can grow a UI of its own.

| Gesture | In CONDUCTOR | In the menu |
|---|---|---|
| **Tap** | open/close the terminal mirror | back out one level |
| **Double-tap** | open the menu | — |
| **Hold 2 s** | long sleep (deep sleep) — a tap wakes the board | same |

A tap is **deferred by 300 ms**, because a first tap is not yet knowably a single
one. That cost is paid by the least latency-sensitive of the three on purpose:
the mirror already waits a daemon round-trip, so 300 ms disappears into latency
that was there anyway. 300 ms is also the daemon's own `DOUBLE_CLICK_S`, so both
double-taps on this device — GO's FOLLOW toggle and this one — want the same
rhythm from your thumb.

The hold is deliberately much longer than any other gesture: switching off by
fumbling a button would be a poor joke on a device whose job is to be
glanceable. It works with the screen dark, too — reaching for the device to
switch it off should not need two goes.

### The menu

Firmware-local, and the **only** part of this device the daemon knows nothing
about. While it is up, PREV/NEXT/GO are handled here and **never emitted** — a
GO forwarded while you were aiming at a settings row would raise a terminal you
were not looking at. The daemon simply sees no button events, keeps its frame
current the whole time, and needs no protocol change.

```
  ┌──────────────────────────────────────┐
  │ ▁▄█ CLAUDE MATE               ▰ 64%  │
  ├──────────────────────────────────────┤
  │    ┌────────┐                        │
  │    │  ▤▤▤   │   ⚟      ⓘ     ▟   ☾   │  ← the selected tile is enlarged
  │    └────────┘                        │
  │          CONDUCTOR                   │  ← only the selected item is labelled
  │  PREV/NEXT move   GO open   4th back │
  └══════════════════════════════════════┘
```

A horizontal strip rather than a list, and not as a style preference: this panel
is 320×172, so a vertical list uses a fifth of the width and runs out of height
at five rows, while a strip has room to spare in the axis it actually has. Only
the selected item is labelled — five labels are either unreadable at size 1 or
collide at size 2, and the icons carry the recognition once you have been here
twice. They are drawn from primitives: at 34–46 px a font glyph is
unrecognisable, and a bitmap costs flash plus a second place to keep the design.

| Item | What it does |
|---|---|
| **CONDUCTOR** | back to the triage view — the daemon's frame, unchanged since iteration 2 shipped |
| **SETTINGS** | the five rows below |
| **ABOUT** | link, RSSI, battery % + raw mV, boot cause, firmware version. The serial `?` output, on the glass — which is the only place it can be read on a cordless device with no console attached |
| **WI-FI** | start the setup portal now. Previously this needed BOOT held through power-on, or `Z` over serial |
| **SLEEP** | the same deep sleep the 2 s hold does, made discoverable |

### Settings

| Row | Values | Notes |
|---|---|---|
| **Sleep screen** | off · 1m · 2m · 5m · 10m · 30m | One row, not a toggle plus a duration — the two can never disagree, and it costs one row on a screen that has five. Defaults to **off**: nobody's screen should start going dark because they took an update |
| **Brightness** | 5 steps | Non-linear in duty (20/60/120/200/255). Equal duty steps feel like one enormous jump at the bottom and four identical ones at the top. Applied live, so the step you are on is the step you can see |
| **Alert LED** | off · low · med · high | **off is genuinely dark.** A 7 Hz red strobe is the right answer to a failed turn at a desk and the wrong one in a bedroom — and the alert still arrives, through the flashing name row and fleet letter |
| **Flip screen** | on / off | Applies **on restart**; the row says so and a long GO does it. Rotation is set once after `begin()`, and re-rotating a live panel is the one failure in this firmware that looks exactly like dead hardware |
| **Factory reset** | hold GO to confirm | Wipes Wi-Fi, token **and** settings. Asks twice, and the second gesture is a **long** press rather than another tap — you cannot double-tap your way into wiping the token. Disarms itself after 6 s |

Settings live in their own NVS namespace (`mate-ui`), separate from the network
config, which is what lets a factory reset choose what it destroys. Writes are
deferred 1.5 s, so holding NEXT through the brightness steps costs one flash
write rather than five.

### Screen sleep

The backlight is by far the biggest draw on this board — tens of milliamps
against the ~1 mA the WS2812 idles at — so turning it off is most of the runtime
available without touching the radio.

It goes off after the configured delay **only when nothing is waiting on you**,
and what counts as waiting is answered from the frame the device already has:
the flashing name row, any lowercase fleet letter, or a **looping** LED pattern
— which the daemon drives from exactly *"worst unacknowledged alert"* and is
therefore the authoritative signal. An alert turns the screen back on by itself.

Two things deliberately do **not** hold the backlight on:

- **A `working` fleet.** An hour of grinding with nothing to say is precisely
  the case this setting exists for.
- **`NO LINK`.** A dead daemon must not be able to burn the cell flat, which is
  what would happen the moment you carried the device out of Wi-Fi range.

Only the backlight goes off. `displayOff()` would save another milliamp or two
and would put the wake path one command away from the dead-panel failure above;
not worth 2 mA. Rendering is skipped while dark (a full flush is ~28 ms of SPI),
but the frame keeps arriving and being parsed, so waking shows the **current**
state rather than a stale one that then jumps. The terminal mirror is closed on
the way down — polling a wrapper once a second to draw onto a dark panel is the
one case where hibernating saves nothing and costs the Mac work.

**A press on a dark screen only wakes, and is swallowed.** Phone convention, but
for a specific reason: GO raises a terminal window, so obeying a press aimed at
a screen you cannot read would occasionally yank you to the wrong session — the
exact thing this device exists to prevent. The 2 s hold is the one exception.

"Off" means **deep sleep, not zero**. The WS2812 has no shutdown pin and idles
around 1 mA whenever the rail is up, which dwarfs the ~8 µA the S3 itself draws
asleep. That works out to roughly a month of standby on a 14500 — off in every
practical sense, but a switch in the battery lead remains the only true zero.

Waking **reboots**: deep sleep does not resume, it re-runs `setup()`. Config is
in NVS so nothing is lost, but rejoining Wi-Fi costs a few seconds. `?` reports
`boot : woke from power-off` or `power-on / reset`, which is the only way to
tell a deliberate wake from a brownout after the fact.

Three details in the implementation are load-bearing, and all three fail
silently if skipped:

- The backlight pin is driven low **and latched** (`gpio_hold_en`). Deep sleep
  releases every GPIO, so an unheld pin floats and the panel can sit there lit,
  burning the current this is meant to save. The latch survives the reboot, so
  `setup()` must release it first — otherwise the board wakes to a black screen,
  indistinguishable from the dead-panel failure a wrong `LCD_BL` causes.
- It **waits for the button to be released** before sleeping. The wake is
  level-triggered on LOW, so sleeping with the button still down wakes the chip
  instantly and reads as "power off is broken".
- The Wi-Fi link is closed **politely**. A half-open socket leaves the daemon
  holding a dead client and still listing the device as present.

### The terminal mirror

Tapping the 4th button in **CONDUCTOR** opens a live view of the selected
session's real terminal, refreshed about once a second; PREV/NEXT then scroll it
and GO closes it and raises the actual window. Only **wrapped** sessions can be
mirrored — a hook-only session has no PTY for the daemon to read and says so.
See [`docs/PROTOCOL.md`](../docs/PROTOCOL.md) for the wire format.

While the view is open the **daemon** reinterprets PREV/NEXT as scroll, so the
firmware never learns that this particular mode exists: it emits the same two
verbs either way and draws whatever rows arrive. That is still true of the
mirror. It is *not* true of the menu, which is the firmware's own and swallows
those buttons — see [The menu](#the-menu).

Terminal contents cross the network on the same token-authenticated but
**plaintext** TCP link as everything else. That is a bigger exposure than a
status string; keep it to networks you trust.
