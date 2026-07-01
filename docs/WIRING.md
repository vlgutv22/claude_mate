# Claude Mate — Wiring

Target board: **Arduino Nano (ATmega328P)**, 5 V logic.

All grounds are common. The OLED runs on 5 V over I2C. The three buttons use the
internal pull-ups (`INPUT_PULLUP`) so they need no external resistors. The
overall-status indicator is the **OLED itself** — it shows a big word (FREE /
WIP / BLOCKED / WTF) plus a session-detail line. A **micro vibration motor**
provides a per-session haptic alert, driven by the daemon via `V|<kind>` (the
word itself is visual only and never buzzes).

---

## The status display + haptic

The OLED is the **sole visual status**: a large word (FREE / WIP / BLOCKED /
WTF) on top of a session-detail line. The daemon picks the word and sends it; the
Arduino renders it.

A **micro vibration motor on D5** is the haptic alert. The word (`D|`) is
**visual only** — it never buzzes. Haptics are driven **per session** by the
daemon via `V|<kind>`, at a graduated-but-soft PWM amplitude:

| `V|` kind | Haptic | Repeat |
|-----------|--------|--------|
| `START`   | 3 gentle 0.3 s ticks | one-shot |
| `INPUT`   | soft double-tap | re-tapped ~every 10 s until FOCUS |
| `DONE`    | 5×0.2 s heartbeat (gaps 0.2/0.4 s) | **loops** until `V|OFF` |
| `ERROR`   | 0.4 s on / 0.2 s off alarm | **loops** until `V|OFF` |
| `OFF`     | stop the motor | sent on FOCUS / clear |

There is no wheel, no dial, no homing, and no endstop — those are gone. See
[PROTOCOL.md](PROTOCOL.md) for the exact haptic contract.

---

## Pin map

| Function                | Nano pin    | Notes |
|-------------------------|-------------|-------|
| OLED VCC                | 5V          | SSD1306 0.91" 128x32, I2C (module pin order: GND/VCC/SCK/SDA) |
| OLED GND                | GND         | common ground |
| OLED SDA                | **A4**      | I2C data |
| OLED SCL                | **A3**      | I2C clock — **software (bit-banged) I2C** (hardware SCL A5 was damaged; the 328P's TWI is fixed to A4/A5 and can't be remapped, so SCL moved to the plain GPIO A3). See `firmware/claude_mate/softssd1306.h`. |
| SUBMIT button           | **D2**      | `INPUT_PULLUP`; other leg to GND |
| NEXT button             | **D3**      | `INPUT_PULLUP`; other leg to GND |
| MODE button             | **D4**      | `INPUT_PULLUP`; other leg to GND |
| Vibration motor (drive) | **D5**      | drives a vibro-motor module / NPN transistor / ULN2003 channel (see below) — **not** the motor directly |

> There are **no LED pins** and **no stepper pins**. The old traffic-light LEDs
> and the stepper status wheel (with its ULN2003 driver and D4 endstop) are both
> gone. D5 now drives the vibration motor; D4 is the MODE button.

### Buttons

Layout left→right is **MODE | SUBMIT | NEXT**. Each button connects its
**D2 / D3 / D4 pin to one leg** and **GND to the other leg**. With `INPUT_PULLUP`,
an unpressed button reads **HIGH** and a pressed button reads **LOW**. The
firmware debounces presses (~40 ms — snappy).

- **D2 = SUBMIT** → emits `B|1` (focus/proceed to the selected tab).
- **D3 = NEXT** → emits `B|2` (SCROLL: next card / LIST: highlight down).
- **D4 = MODE** → emits `B|3` on a **short** press (SCROLL: previous card /
  LIST: highlight up) and `B|4` on a **long** press ≥ ~500 ms (toggle
  SCROLL ⇄ LIST mode).

---

## Driving the vibration motor (pick one)

A micro vibration motor pulls **~tens of mA** — too much to hang directly off a
Nano GPIO pin, which can source only a few mA safely. Drive it through one of
these three options. All of them power the motor from the **USB 5 V rail** and
share a **common ground** with the Nano.

### Option A — 3-pin vibration-motor module (simplest)

Many breakout boards package the motor with the transistor and clamp diode
already on the board, exposing three pins: **VCC / GND / IN**.

```
        Nano        Vibro module
        5V  -------- VCC
        GND -------- GND
        D5  -------- IN
```

Wire `IN → D5`, `VCC → 5V`, `GND → GND`. Nothing else needed — the board has the
driver transistor and flyback diode built in.

### Option B — bare motor + NPN transistor + flyback diode

Drive a bare motor through a small NPN transistor (e.g. **2N2222** or **S8050**)
switched by **D5** through a **~1 kΩ base resistor**, with a **1N4148 flyback
diode** across the motor to clamp the inductive kick.

```
                         +5V
                          |
                         [motor]
                          |  +----|<|----+   (1N4148, cathode/band to +5V)
                          +--+           |
                          |              |
        D5 --[1k]--+--- base            (diode in parallel with the motor)
                   |   (NPN: 2N2222 / S8050)
                  collector ---- motor (-) side
                  emitter ------ GND
```

- `D5 → 1 kΩ → base`.
- Collector → motor's low side; motor's high side → **+5 V**.
- Emitter → **GND**.
- **1N4148** across the motor terminals, **band (cathode) toward +5 V**, so the
  flyback current has somewhere to go when D5 goes low.

### Option C — one channel of a ULN2003 board

If you have a ULN2003 board on hand, use a single channel — it already contains
the Darlington transistor **and** the built-in clamp diode.

```
        Nano        ULN2003          motor
        D5  -------- IN1
        GND -------- GND
                     OUT1 ----------- motor (-)
        5V  ------------------------- motor (+)
```

Wire `D5 → IN1`, the **motor between OUT1 and +5 V**, and tie grounds common. The
ULN2003 supplies the transistor and flyback clamp; the other six channels are
unused.

```
        Arduino Nano                Vibration motor (one of A / B / C)
        D5  ----> [module IN | NPN base via 1k | ULN2003 IN1] ----> motor
        5V  ----> motor + rail
        GND ----> common ground
```

---

## Power note

The vibration motor is **tiny** — it draws only tens of mA, so the **USB 5 V
rail is fine** to power it; there is no brown-out concern like the old stepper
had. Just keep two things in mind:

- **Tie all grounds common** (Nano GND, motor driver GND, motor low side).
- **Add a flyback diode** across the motor (a **1N4148** in Option B; already
  built into the module in Option A and the ULN2003 in Option C) so the
  motor's inductive kick can't stress the transistor or the Nano.

That's the whole story — no separate supply, no homing, no endstop.

---

## ASCII wiring overview

```
        Arduino Nano (ATmega328P)
        +-----------------------+
   5V --| 5V              A4(SDA)|---- OLED SDA
  GND --| GND             A3(SCL)|---- OLED SCL  (software I2C; A5 was damaged)
        |                       |
        |                    D2 |---- SUBMIT button -- GND   (INPUT_PULLUP)
        |                    D3 |---- NEXT   button -- GND   (INPUT_PULLUP)
        |                    D4 |---- MODE   button -- GND   (INPUT_PULLUP)
        |                       |
        |                    D5 |---- vibro driver IN -> motor (+5V / GND)
        |                       |       (module, or NPN+1k+1N4148, or ULN2003 ch.)
        +-----------------------+

  OLED module: VCC->5V, GND->GND, SDA->A4, SCL->A3   (software I2C, addr 0x3C)
  Vibration motor: powered from the USB 5V rail, common ground, flyback diode
  across the motor. The motor buzzes per session via V|<kind> (not the word).
```

---

## Bill of materials (BOM)

| Qty | Part                                             | Notes |
|-----|--------------------------------------------------|-------|
| 1   | Arduino Nano (ATmega328P)                        | 5 V; USB CDC serial |
| 1   | I2C OLED 0.91" 128x32 — SSD1306                   | 4-pin GND/VCC/SCK(SCL)/SDA; a 0.96" 128x64 also works (set SCREEN_HEIGHT 64) |
| 3   | Momentary push buttons (tactile)                 | MODE + SUBMIT + NEXT |
| 1   | Micro vibration motor                            | coin/pager type; haptic alert on D5, per-session buzz via V|<kind> |
| 1   | NPN transistor (2N2222 / S8050)                  | Option B driver — *or* use a 3-pin vibro module / a spare ULN2003 channel instead |
| 1   | Resistor ~1 kΩ                                    | Option B — base resistor on D5 |
| 1   | Diode 1N4148                                     | Option B — flyback across the motor (built in to a module / ULN2003) |
| —   | Breadboard / perfboard + jumper wires            | assembly |
| 1   | USB cable (to the Mac)                            | data-capable, not charge-only |

> The **stepper, ULN2003 driver, endstop microswitch, and dial/wheel are gone**,
> as are the old 3 LEDs and resistors — all replaced by the micro vibration
> motor and its small driver (module, NPN+1k+1N4148, or a ULN2003 channel).

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
