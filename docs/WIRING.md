# Claude Mate — Wiring

Target board: **Arduino Nano (ATmega328P)**, 5 V logic.

All grounds are common. The OLED runs on 5 V over I2C. Buttons use the internal
pull-ups (`INPUT_PULLUP`) so they need no external resistors; each LED needs its
own 220 Ω series resistor.

---

## Pin map

| Function                | Nano pin | Notes |
|-------------------------|----------|-------|
| OLED VCC                | 5V       | SSD1306 128x64, I2C |
| OLED GND                | GND      | common ground |
| OLED SDA                | **A4**   | I2C data (hardware SDA on the 328P) |
| OLED SCL                | **A5**   | I2C clock (hardware SCL on the 328P) |
| FOCUS button            | **D2**   | `INPUT_PULLUP`; other leg to GND |
| NEXT button             | **D3**   | `INPUT_PULLUP`; other leg to GND |
| GREEN LED               | **D5**   | via 220 Ω to GND |
| YELLOW LED              | **D6**   | via 220 Ω to GND |
| RED LED                 | **D7**   | via 220 Ω to GND |
| Buzzer (optional)       | **D8**   | piezo; behind `#define ENABLE_BUZZER` |

### Buttons

Each button connects its **D2 / D3 pin to one leg** and **GND to the other leg**.
With `INPUT_PULLUP`, an unpressed button reads **HIGH** and a pressed button
reads **LOW**. The firmware debounces presses (~200 ms).

- **D2 = FOCUS** → emits `B|1` (focus the current card's session).
- **D3 = NEXT** → emits `B|2` (advance carousel, pause auto-rotation ~10 s).

### LEDs (the traffic light)

```
D5 ---[220R]---|>|--- GND    (GREEN)
D6 ---[220R]---|>|--- GND    (YELLOW)
D7 ---[220R]---|>|--- GND    (RED)
```

The LED **anode** (long leg, `+`) goes toward the Nano pin through the resistor;
the **cathode** (short leg, flat side) goes to GND. The 220 Ω resistor may be on
either leg.

### Buzzer (optional)

A small **piezo buzzer** on **D8** chirps **once on the transition into RED**
(rising edge), as an attention alert. It is **optional** and compiled in only
when `ENABLE_BUZZER` is defined in the firmware. Wire `+` to D8 and `-` to GND.

---

## ASCII wiring overview

```
        Arduino Nano (ATmega328P)
        +-----------------------+
   5V --| 5V              A4(SDA)|---- OLED SDA
  GND --| GND             A5(SCL)|---- OLED SCL
        |                       |
        |                    D2 |---- FOCUS button --- GND   (INPUT_PULLUP)
        |                    D3 |---- NEXT  button --- GND   (INPUT_PULLUP)
        |                       |
        |                    D5 |--[220R]--|>|-- GND   GREEN LED
        |                    D6 |--[220R]--|>|-- GND   YELLOW LED
        |                    D7 |--[220R]--|>|-- GND   RED LED
        |                       |
        |                    D8 |---- piezo buzzer + (optional)
        +-----------------------+

  OLED module: VCC->5V, GND->GND, SDA->A4, SCL->A5   (I2C addr 0x3C)
```

---

## Bill of materials (BOM)

| Qty | Part                                             | Notes |
|-----|--------------------------------------------------|-------|
| 1   | Arduino Nano (ATmega328P)                        | 5 V; USB CDC serial |
| 1   | I2C OLED 128x64 — SSD1306 (or SH1106 1.3")        | 4-pin VCC/GND/SDA/SCL; see driver note below |
| 2   | Momentary push buttons (tactile)                 | FOCUS + NEXT |
| 3   | LEDs — green, yellow, red                         | the traffic light |
| 3   | 220 Ω resistors                                  | one per LED |
| 1   | Piezo buzzer (optional)                          | only if `ENABLE_BUZZER` |
| —   | Breadboard / perfboard + jumper wires            | assembly |
| 1   | USB cable (to the Mac)                            | data-capable, not charge-only |

---

## Notes and caveats

### I2C address: 0x3C (alt 0x3D)

The default I2C address used by the firmware is **0x3C**, which is by far the
most common for these 128x64 modules. Some boards are strapped to **0x3D** (the
common alternative). If the screen stays blank, try 0x3D — many modules have a
solder jumper labeled `0x78 / 0x7A` (the 8-bit forms of 0x3C / 0x3D). Change the
address constant in the firmware to match.

### SSD1306 vs SH1106 (1.3" panels)

The pin map and I2C address are identical, but the **controller chip differs**:

- **0.96" 128x64** modules are almost always **SSD1306**.
- **1.3" 128x64** modules are often **SH1106**, not SSD1306.

The SH1106 has a slightly different internal column offset (its RAM is 132 px
wide vs the 128 px panel), so driving an SH1106 with an SSD1306 driver typically
produces a **2-pixel horizontal shift and/or garbage at the edges**. If you use a
1.3" panel, select the **SH1106** driver/library instead of the SSD1306 one.
Everything else (wiring, address, protocol) is unchanged.

### Serial reset / 10 µF cap caveat

On the Nano, **DTR toggling from opening the USB serial port pulses RESET**, so
the board reboots (~1.5 s) every time the daemon opens the port. This is by
design and is exactly why the firmware emits **`H` on boot** and the daemon
**re-sends full state on `H`** — the display self-heals after each reconnect.

If you specifically want to **prevent** the auto-reset (for example, to keep the
display from blanking when the daemon reconnects), you can place a **10 µF
electrolytic capacitor between the RESET pin and GND** (observe polarity:
negative leg to GND). This holds RESET steady through the DTR pulse.

Important: that same cap will **block normal sketch uploads**, because uploading
*relies* on the auto-reset to enter the bootloader. **Remove the 10 µF cap
before flashing new firmware, then re-install it afterward.** For most users the
default behavior (no cap, rely on the `H` handshake) is the right choice.
