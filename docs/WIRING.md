# Claude Mate — Wiring

Target board: **Arduino Nano (ATmega328P)**, 5 V logic.

All grounds are common. The OLED runs on 5 V over I2C. Buttons and the homing
endstop use the internal pull-ups (`INPUT_PULLUP`) so they need no external
resistors. The overall-status indicator is a **stepper-driven status wheel** — a
small dial that physically rotates to point at one of four words.

---

## The status wheel

Instead of three LEDs, the overall system state is shown by a wheel turned by a
stepper motor. Four words are arranged around the wheel **90° apart**, in
escalation order so that rotating the dial = the situation getting worse:

```
                 FREE  (0 deg, HOME)
                   |
        WTF -------+------- WIP
       (270)       |       (90)
                BLOCKED
                 (180)
```

| Word      | Angle  | Step position           | Meaning |
|-----------|--------|-------------------------|---------|
| `FREE`    | 0°     | `0` (HOME, endstop)     | No session needs you — all idle/done, or no sessions. |
| `WIP`     | 90°    | `STEPS_PER_REV / 4`     | At least one session working; none blocked/errored. |
| `BLOCKED` | 180°   | `STEPS_PER_REV / 2`     | At least one session waiting on your input; none errored. |
| `WTF`     | 270°   | `3 * STEPS_PER_REV / 4` | At least one session in error (StopFailure / API 5xx / overloaded / timeout). |

`FREE` sits at the **home endstop** (step 0). A small **tab on the wheel** clicks
the endstop microswitch exactly at the `FREE` position. The firmware homes
against this switch at boot and re-syncs to it every time the tab passes,
self-healing any mechanical drift (see [Homing](#endstop-homing) below).

Movement is **non-blocking** (driven by AccelStepper) so buttons and serial stay
responsive while the wheel turns. The daemon picks the word; the Arduino rotates
to it by the **shortest path** (either direction — a full wheel has no wire
constraint).

---

## Pin map

| Function                | Nano pin    | Notes |
|-------------------------|-------------|-------|
| OLED VCC                | 5V          | SSD1306 0.91" 128x32, I2C (module pin order: GND/VCC/SCK/SDA) |
| OLED GND                | GND         | common ground |
| OLED SDA                | **A4**      | I2C data (hardware SDA on the 328P) |
| OLED SCL                | **A5**      | I2C clock (hardware SCL on the 328P) |
| FOCUS button            | **D2**      | `INPUT_PULLUP`; other leg to GND |
| NEXT button             | **D3**      | `INPUT_PULLUP`; other leg to GND |
| ENDSTOP (home switch)   | **D4**      | `INPUT_PULLUP`; pressed = **LOW**; other leg to GND |
| Stepper IN1 / STEP      | **D5**      | ULN2003 `IN1` **or** A4988 `STEP` |
| Stepper IN2 / DIR       | **D6**      | ULN2003 `IN2` **or** A4988 `DIR`  |
| Stepper IN3 / EN        | **D7**      | ULN2003 `IN3` **or** A4988 `EN` (active **LOW**) |
| Stepper IN4             | **D8**      | ULN2003 `IN4` only (unused on A4988) |
| Buzzer (optional)       | **D9**      | piezo; behind `#define ENABLE_BUZZER` |

> There are **no LED pins**. The old D5/D6/D7 traffic-light LEDs are gone; D5–D8
> now drive the stepper.

### Buttons

Each button connects its **D2 / D3 pin to one leg** and **GND to the other leg**.
With `INPUT_PULLUP`, an unpressed button reads **HIGH** and a pressed button
reads **LOW**. The firmware debounces presses (~200 ms).

- **D2 = FOCUS** → emits `B|1` (focus the current card's session).
- **D3 = NEXT** → emits `B|2` (advance carousel, pause auto-rotation ~10 s).

### Endstop (home switch)

A **microswitch** (or optical/hall endstop) on **D4** marks the `FREE` / home
position. Wire it like a button: one leg to **D4**, the other to **GND**, using
`INPUT_PULLUP` so **pressed = LOW**. Mount it so the **tab on the wheel presses
it exactly when the dial points at `FREE`** (step 0).

---

## Driver options — pick one with a `#define`

The firmware abstracts the motor driver behind a single `#define` at the top of
`firmware/claude_mate/claude_mate.ino`. Switching driver is a one-line change:

```c
#define DRIVER_ULN2003     // default — 28BYJ-48 unipolar via ULN2003
// #define DRIVER_A4988    // alternative — NEMA17 bipolar via A4988 STEP/DIR
```

### Option A — ULN2003 + 28BYJ-48 (default)

The cheap, common combo. The 28BYJ-48 is a 5 V geared unipolar stepper; the
ULN2003 board has 4 input pins.

- Wiring: `IN1..IN4` → **D5, D6, D7, D8**.
- Library config (note the **28BYJ-48 coil order** `IN1, IN3, IN2, IN4`):

  ```c
  AccelStepper stepper(AccelStepper::HALF4WIRE, IN1, IN3, IN2, IN4);
  ```

- `STEPS_PER_REV` default **4096** (half-step, geared).
- Power: motor draws ~**240 mA**; feed it from the **USB 5 V rail**, not through
  the Nano's onboard regulator (see [Power](#power--brown-out)).

```
        Nano        ULN2003 board       28BYJ-48
        D5  -------- IN1
        D6  -------- IN2                 (4-wire motor plug
        D7  -------- IN3                  into the ULN2003)
        D8  -------- IN4
        5V  -------- +  (motor V+)
        GND -------- -  (GND)
```

### Option B — A4988 + NEMA17 (STEP/DIR)

For a larger bipolar stepper. The A4988 takes STEP/DIR pulses.

- Wiring: `STEP` → **D5**, `DIR` → **D6**, `EN` → **D7** (enable is **active
  LOW**). `IN4`/D8 is unused.
- Library config:

  ```c
  AccelStepper stepper(AccelStepper::DRIVER, STEP_PIN, DIR_PIN);  // STEP=D5, DIR=D6
  ```

- `STEPS_PER_REV` default **3200** (200 full steps × 16 microsteps) — left as a
  clearly-marked `#define` to tune for your microstep jumper settings.
- Power: drive the motor logic from an **external 12 V supply** into the A4988's
  VMOT, **not** through the Nano regulator. Set the A4988 current-limit pot.

```
        Nano        A4988                NEMA17
        D5  -------- STEP
        D6  -------- DIR                 1B 1A 2A 2B -> the 4 motor coils
        D7  -------- EN  (active LOW)
        5V  -------- VDD (logic)
        GND -------- GND (logic + motor)
        12V -------- VMOT  (external supply, + 100uF across VMOT/GND)
```

---

## Endstop homing

At boot the firmware runs a **two-stage homing routine** so step 0 (`FREE`) is
repeatable:

1. **Fast approach** — rotate in `HOMING_DIR` until the endstop reads LOW
   (debounced).
2. **Back off** — reverse `OFF_STEPS` to release the switch.
3. **Slow re-approach** — creep back until the switch presses again; **define
   that point as step 0 = `FREE`**.

**Guard:** if the endstop never triggers within ~**1.5 revolutions**, homing is
**aborted** — the firmware prints a warning over serial and assumes the current
position is `FREE`. It will **not spin forever**. (Check the switch/tab
alignment if you see that warning.)

**Continuous drift correction (in `loop()`, non-blocking):** the endstop is
monitored on every iteration. Whenever the wheel tab presses it (debounced rising
edge), the step counter is snapped to the known home (`FREE`) position modulo
`STEPS_PER_REV`, so drift self-heals every time the tab passes home — the
in-progress move continues toward its current target afterward.

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
        |                    D4 |---- ENDSTOP switch - GND   (INPUT_PULLUP, LOW=pressed)
        |                       |
        |                    D5 |---- IN1 / STEP  \
        |                    D6 |---- IN2 / DIR    > stepper driver
        |                    D7 |---- IN3 / EN    /  (ULN2003 or A4988)
        |                    D8 |---- IN4 (ULN2003 only)
        |                       |
        |                    D9 |---- piezo buzzer + (optional)
        +-----------------------+

  OLED module: VCC->5V, GND->GND, SDA->A4, SCL->A5   (I2C addr 0x3C)
  Wheel tab presses the D4 endstop at the FREE position (step 0).
  Motor power: USB 5V rail (ULN2003/28BYJ-48) or external 12V (A4988/NEMA17),
  NOT through the Nano regulator -- see Power below.
```

---

## Bill of materials (BOM)

| Qty | Part                                             | Notes |
|-----|--------------------------------------------------|-------|
| 1   | Arduino Nano (ATmega328P)                        | 5 V; USB CDC serial |
| 1   | I2C OLED 0.91" 128x32 — SSD1306                   | 4-pin GND/VCC/SCK(SCL)/SDA; a 0.96" 128x64 also works (set SCREEN_HEIGHT 64) |
| 2   | Momentary push buttons (tactile)                 | FOCUS + NEXT |
| 1   | Endstop microswitch (lever / optical)            | home switch at `FREE`; `INPUT_PULLUP`, LOW = pressed |
| 1   | Stepper motor                                    | **28BYJ-48** (default, with ULN2003) or **NEMA17** (with A4988) |
| 1   | Stepper driver board                             | **ULN2003** (for 28BYJ-48) or **A4988** (for NEMA17) |
| 1   | Wheel / dial face                                | printed/laser-cut disc with FREE/WIP/BLOCKED/WTF + a home tab |
| 1   | Piezo buzzer (optional)                          | only if `ENABLE_BUZZER`; chirps on entering `WTF` |
| —   | Breadboard / perfboard + jumper wires            | assembly |
| 1   | External 12 V PSU + 100 µF cap                   | only for the A4988 / NEMA17 option |
| 1   | USB cable (to the Mac)                            | data-capable, not charge-only |

> The **3 LEDs and 3 resistors are gone** — replaced by the stepper, driver
> board, endstop, and dial.

---

## Power / brown-out

**Do not power the motor through the Nano's onboard 5 V regulator** — the inrush
can brown-out the 328P and cause spurious resets.

- **ULN2003 / 28BYJ-48:** the motor draws ~**240 mA**. Power the ULN2003 `V+`
  from the **USB 5 V rail directly** (the Nano's `5V` pin, fed by USB) — this
  bypasses the regulator and is fine for a single 28BYJ-48.
- **A4988 / NEMA17:** power VMOT from an **external 12 V supply** (with a
  100 µF cap across VMOT/GND), never from the Nano. Share grounds.

Symptoms of brown-out (motor stutters, OLED flickers, board reboots and re-emits
`H`) almost always mean the motor is fighting the regulator — move it onto the
5 V rail or an external supply.

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
(After a reset the firmware also re-homes the wheel against the endstop.)

If you specifically want to **prevent** the auto-reset (for example, to keep the
display from blanking when the daemon reconnects), you can place a **10 µF
electrolytic capacitor between the RESET pin and GND** (observe polarity:
negative leg to GND). This holds RESET steady through the DTR pulse.

Important: that same cap will **block normal sketch uploads**, because uploading
*relies* on the auto-reset to enter the bootloader. **Remove the 10 µF cap
before flashing new firmware, then re-install it afterward.** For most users the
default behavior (no cap, rely on the `H` handshake) is the right choice.
