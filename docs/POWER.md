# Power — what the battery build actually costs

The cordless ESP32-S3 build runs on a 14500 cell. This page is where the numbers
live: what has been **measured**, what is **estimated**, and — where a figure is
still missing — exactly how to take it, so the next person with a meter can fill
it in rather than start from scratch.

Every number here is honest about which of those three it is. A power page full
of plausible figures nobody measured is worse than an empty one, because it
stops anyone from measuring.

---

## The three consumers

| | Draw | Basis |
|---|---|---|
| **Backlight** | tens of mA at full duty | measured indirectly: it is by far the largest single consumer, which is why *Sleep screen* exists and why five brightness steps do more for runtime than anything else in the menu |
| **WS2812 (the alert LED)** | ~1 mA idle, whenever the rail is up | it has no shutdown pin, so this is the floor while the board is powered — it dwarfs the ~8 µA the S3 draws in deep sleep, which is why "off" is a month of standby rather than a year |
| **Radio** | see below | the number this page exists for |

The ordering matters more than the values: **the backlight dominates whenever it
is on**, so a radio measurement taken with the screen lit measures the screen.
Every radio figure below must be taken with the backlight off (*Sleep screen* →
1m, then wait) or it means nothing.

---

## Radio: Wi-Fi versus duty-cycled BLE

### Where the BLE numbers come from

The design target for the BLE transport is **200 ms of advertising followed by
~4 s of silence**, repeating, while no daemon is connected. Those figures come
from [issue #21](https://github.com/vlgutv22/claude_mate/issues/21), credited
there to Jack Jansen in the ESP8266, ESP32 & Microcontrollers group, who
reported **roughly 5% of normal consumption** with that rhythm on his own
battery devices.

That is a **reported third-party figure for a different device**, and it is the
reason to build the thing, not evidence about this board. It is not a claim
about Claude Mate and must not be quoted as one.

### What the duty cycle can and cannot save here

Stopping the advertiser cuts the **radio's** share of the draw. It does not stop
the CPU: this firmware never light-sleeps, because the display, the LED pattern
engine and the button poll all want the loop running. So the ceiling on the
saving is the radio's share of a board that is otherwise fully awake — real, but
bounded, and nothing like 95% of the board's total.

The honest expectation, stated before measuring so it can be checked against:

- **BLE idle should beat Wi-Fi idle**, because a maintained association with
  DTIM wakeups costs more than 200 ms of advertising every 4 s.
- **Neither will matter much with the backlight on.**
- The interesting question the measurement answers is therefore
  **whether the radio or the OLED dominates once the radio is duty-cycled** —
  which is exactly how issue #21 framed it.

### The table

| Configuration | Idle draw at the cell | Status |
|---|---|---|
| Deep sleep (`SLEEP`, or a 2 s hold) | ~1 mA (WS2812 floor) + ~8 µA | **estimated** from the WS2812 datasheet floor; consistent with observed ~1 month standby on a 14500 |
| Wi-Fi linked, backlight off | — | **not yet measured** |
| BLE advertising (200/4000), no daemon, backlight off | — | **not yet measured** |
| BLE connected, backlight off | — | **not yet measured** |
| Any of the above, backlight at step 4 | — | **not yet measured** |

**These blanks are deliberate.** The BLE transport shipped without them because
the code is testable without a meter and the measurement is not — but issue #21
is not closed by the transport alone, and this table is the part still owed.

### How to take the measurement

So that two people's numbers can be compared:

1. Cell charged and **off the USB cable** — a board on USB measures the cable,
   and the charge controller's own draw sits on top of everything.
2. Meter in series with the **battery lead**, not the 3V3 rail: the regulator's
   quiescent draw is part of the answer.
3. *Sleep screen* → **1m**, then leave the device alone until the backlight goes
   out. Take the reading at least 30 s after that.
4. Take Wi-Fi and BLE readings **back to back on the same cell, at the same
   charge state, in the same room** — cell voltage and RF environment move these
   numbers more than the transport does.
5. Record the **average over at least 30 s**, not an instantaneous value: the
   duty cycle means the instantaneous reading alternates between two very
   different figures, and either one alone is a misleading answer.
6. Note the firmware version (`?` over serial prints it) alongside the number.

### The one figure that is measured

The BLE transport's **code** cost, against the same sketch built without it:

| | Flash | RAM |
|---|---|---|
| `firmware/claude_mate_s3` without the BLE link | 1,320,866 B | 55,536 B |
| …with it | 1,329,874 B | 58,384 B |
| **Cost of the transport** | **+9,008 B** | **+2,848 B** |

Cheap because the BLE stack was **already linked in** for the HID gamepad — that
~290 KB was paid for once, and the transport is the second thing to use it.
Both figures are `arduino-cli compile` output for
`esp32:esp32:waveshare_esp32_s3_lcd_147` with the board options `flash_s3.sh`
uses; the app partition is 3 MB, so the sketch sits at 42% of it.

---

## Runtime, and why the menu is where the wins are

Nothing on the radio side changes the fact that **the backlight is the budget**.
In rough order of effect on how long a cell lasts:

1. **Sleep screen** — off by default, and the single largest saving available.
2. **Brightness** — five non-linear steps; step 1 is genuinely usable indoors.
3. **Alert LED** — *off* is genuinely dark, and the alert still arrives through
   the flashing name row and the fleet letter.
4. **Link: ble** — the subject of this page, and fourth on the list.

That ordering is not an argument against the BLE transport. It is an argument
for measuring it before claiming anything about it.
