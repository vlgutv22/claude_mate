# Claude Mate — Wiring

Target board: **Arduino Nano (ATmega328P)**, 5 V logic.

All grounds are common. The OLED runs on 5 V over I2C. The three buttons use the
internal pull-ups (`INPUT_PULLUP`) so they need no external resistors. The
**OLED** shows the single triage frame (session name + info row + fleet strip);
the **indication LED on D8** is the alert output, driven by the daemon via
`V|<kind>`.

---

## The display + indication LED

The OLED is the **sole visual status**: one pre-rendered frame — a size-2
session name band (flashing while its alert is unacknowledged), an info row,
and a fleet strip. The daemon composes it and sends it; the Arduino renders it.

The **indication LED on D8** is the alert output. Alerts are driven by the
daemon via `V|<kind>` for the worst unacknowledged alert class. Because the
output is a light, each state has its own glanceable blink rhythm and every
"act now" state blinks until you acknowledge it (GO, or hold GO):

| `V|` kind | LED pattern | Repeat |
|-----------|-------------|--------|
| `START`   | one long 1 s blink, then dark | one-shot |
| `INPUT`   | aggressive even blink (~2.8 Hz) | **loops** until `V|OFF` |
| `ERROR`   | super-aggressive fast strobe (~7 Hz) | **loops** until `V|OFF` |
| `DONE`    | cascade — 4 quick blinks, then a pause | **loops** until `V|OFF` |
| `OFF`     | LED off | sent on acknowledge / clear |

There is no wheel, no dial, no motor — those are gone. See
[PROTOCOL.md](PROTOCOL.md) for the exact contract.

---

## Pin map

| Function                | Nano pin    | Notes |
|-------------------------|-------------|-------|
| OLED VCC                | 5V          | SSD1306 0.91" 128x32, I2C (module pin order: GND/VCC/SCK/SDA) |
| OLED GND                | GND         | common ground |
| OLED SDA                | **A4**      | I2C data |
| OLED SCL                | **A3**      | I2C clock — **software (bit-banged) I2C** (hardware SCL A5 was damaged; the 328P's TWI is fixed to A4/A5 and can't be remapped, so SCL moved to the plain GPIO A3). See `firmware/claude_mate/softssd1306.h`. |
| GO button               | **D2**      | `INPUT_PULLUP`; other leg to GND |
| NEXT button             | **D3**      | `INPUT_PULLUP`; other leg to GND |
| PREV button             | **D4**      | `INPUT_PULLUP`; other leg to GND |
| Indication LED          | **D8**      | LED + **~220 Ω–1 kΩ series resistor** to GND. Blinks the alert pattern; the sole alert output. |

> There are **no motor pins**, **no stepper pins**, and **no traffic-light LED
> pins** — the vibration motor (old D5) and the stepper status wheel are gone.
> D4 is the PREV button.

### Buttons

Layout left→right is **PREV | GO | NEXT**. Each button connects its
**D2 / D3 / D4 pin to one leg** and **GND to the other leg**. With `INPUT_PULLUP`,
an unpressed button reads **HIGH** and a pressed button reads **LOW**. The
firmware debounces presses (~40 ms — snappy) and emits:

- **D2 = GO** → `B|G` on a **short** press (acknowledge + raise the shown
  session's window) and `B|K` on a **long** press ≥ ~500 ms (acknowledge only).
- **D3 = NEXT** → `B|N` on press (selection down the queue; auto-repeats ~5/s
  while held).
- **D4 = PREV** → `B|P` on press (selection up the queue; auto-repeats ~5/s
  while held).

---

## Power note

Everything runs off the **USB 5 V rail** — the OLED and the LED draw only a few
tens of mA combined. Tie all grounds common and always use the series resistor
on the LED so the pin isn't over-driven. No separate supply, no homing, no
endstop.

---

## ASCII wiring overview

```
        Arduino Nano (ATmega328P)
        +-----------------------+
   5V --| 5V              A4(SDA)|---- OLED SDA
  GND --| GND             A3(SCL)|---- OLED SCL  (software I2C; A5 was damaged)
        |                       |
        |                    D2 |---- GO     button -- GND   (INPUT_PULLUP)
        |                    D3 |---- NEXT   button -- GND   (INPUT_PULLUP)
        |                    D4 |---- PREV   button -- GND   (INPUT_PULLUP)
        |                       |
        |                    D8 |---- LED +anode, -cathode -> [~220-1k R] -> GND
        +-----------------------+

  OLED module: VCC->5V, GND->GND, SDA->A4, SCL->A3   (software I2C, addr 0x3C)
  Indication LED on D8 blinks the alert pattern for the worst unacknowledged
  alert (V|<kind>); always use a series resistor so the pin isn't over-driven.
```

---

## Bill of materials (BOM)

| Qty | Part                                             | Notes |
|-----|--------------------------------------------------|-------|
| 1   | Arduino Nano (ATmega328P)                        | 5 V; USB CDC serial |
| 1   | I2C OLED 0.91" 128x32 — SSD1306                   | 4-pin GND/VCC/SCK(SCL)/SDA |
| 3   | Momentary push buttons (tactile)                 | PREV + GO + NEXT |
| 1   | Indication LED + ~220 Ω–1 kΩ resistor            | on D8; blinks the alert pattern |
| —   | Breadboard / perfboard + jumper wires            | assembly |
| 1   | USB cable (to the Mac)                            | data-capable, not charge-only |

> The **stepper, ULN2003 driver, endstop microswitch, dial/wheel, old 3 LEDs,
> and the vibration motor + its driver are all gone** — the indication LED on
> D8 is the only alert output.

---

## Notes and caveats

### I2C address: 0x3C (alt 0x3D)

The default I2C address used by the firmware is **0x3C**, which is by far the
most common for these 128x32 modules. Some boards are strapped to **0x3D** (the
common alternative). If the screen stays blank, try 0x3D — many modules have a
solder jumper labeled `0x78 / 0x7A` (the 8-bit forms of 0x3C / 0x3D). Change the
address constant in the firmware to match.

### SSD1306 vs SH1106 (1.3" panels)

The pin map and I2C address are identical, but the **controller chip differs**:

- **0.91" 128x32** (this build's default) and **0.96" 128x64** modules are almost always **SSD1306**.
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
