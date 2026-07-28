/*
 * Claude Mate - ESP32-S3-LCD-1.47 board definition
 * ================================================
 *
 * Everything hardware- or geometry-specific about the wireless companion lives
 * here, so the sketch itself is about the PROTOCOL and the UI, not about pins.
 *
 * TARGET BOARD: Waveshare ESP32-S3-LCD-1.47B, an ESP32-S3R8 (dual core, 8 MB
 * OPI PSRAM, 16 MB flash) with a 1.47" 172x320 IPS LCD on an ST7789, a WS2812
 * RGB LED, a TF-card slot and native USB. The B revision also carries a QMI8658
 * 6-axis IMU and battery charging, neither of which this firmware uses.
 *
 * The LCD pins are NOT in the Arduino core's variant file (which only defines
 * PIN_RGB_LED and the header pins), so they are spelled out below. They match
 * the Waveshare schematic and the values the TFT_eSPI community settled on.
 *
 * The two revisions differ in EXACTLY ONE pin: the backlight is GPIO 46 on the
 * -1.47B and GPIO 48 on the original -1.47. See LCD_BL below -- getting it wrong
 * costs hours, because nothing looks broken except that the screen stays dark.
 *
 * WIRING YOU ADD (the bare board has only BOOT + RESET):
 *
 *   FOUR buttons, each INPUT_PULLUP with its other leg to GND. Physical layout
 *   left -> right; the first three are exactly the Nano build, so muscle memory
 *   carries over unchanged:
 *
 *       PREV -> GPIO 4    GO -> GPIO 5    NEXT -> GPIO 6    ACK -> GPIO 7
 *
 *   GPIO 4/5/6/7 sit next to each other on the header, are free on this board
 *   (verified: an ADC sweep found every one of them floating), and none is a
 *   strapping pin -- so a held button cannot change how the chip boots. BOOT
 *   (GPIO 0) doubles as GO, so a board with nothing soldered to it yet is still
 *   usable.
 *
 *   The 4th button is the one the 3-button build could not have. Short press
 *   ACKNOWLEDGES the shown alert without raising anything -- the commonest
 *   triage action, previously only reachable by holding GO for half a second.
 *   Held, it toggles FOLLOW mode, which until now was hidden behind a
 *   double-click on GO that nobody would discover unaided. Both map onto verbs
 *   the daemon already understands (K) or now understands (F); GO keeps its
 *   short/long/double gestures, so nothing regresses for a 3-button device.
 *
 *   Battery: NOTHING TO WIRE on the -1.47B. The board has its own charger and
 *   its own sense divider already landing on GPIO 1 (ADC1_CH0), so the gauge
 *   works with just a cell plugged into the battery header.
 *
 *   Waveshare does not publish the sense pin or the divider ratio for this
 *   revision, so both were measured: sweeping ADC1 (GPIO 1-10) with a cell
 *   attached, GPIO 1 read a rock-steady 1399 mV while every other pin wandered
 *   randomly between 40 and 1030 mV -- floating. 1399 mV against a LiPo that
 *   can only sit between 3.0 and 4.2 V means the divider is 3:1 (4.20 V), not
 *   the 2:1 a hand-built pair of equal resistors would give (that would imply
 *   2.80 V, under a LiPo's floor).
 */

#pragma once

// ---- LCD (ST7789, 172x320 IPS, hardware SPI) --------------------------------
#define LCD_MOSI      45
#define LCD_SCK       40
#define LCD_CS        42
#define LCD_DC        41
#define LCD_RST       39
#define LCD_BL        46      // backlight, active HIGH (PWM-dimmable). THIS PIN
                              // DIFFERS BY REVISION: 46 on the -1.47B, 48 on the
                              // original -1.47. Every other LCD pin is identical
                              // between them, so the wrong value here fails in a
                              // deeply confusing way -- the ST7789 is driven
                              // perfectly and the panel simply never lights,
                              // which reads as a dead board or a bad flash.

// The panel is 172x320, but the ST7789 controller is a 240x320 part: the 172
// visible columns sit in the MIDDLE of its memory, so every write needs a
// (240-172)/2 = 34 column offset or the image lands 34 px off and wraps.
#define LCD_NATIVE_W  172
#define LCD_NATIVE_H  320
#define LCD_COL_OFFSET 34
#define LCD_ROW_OFFSET 0

// Rotation 1 = landscape, USB-C on the left; 3 flips it for the other hand.
// Landscape is the default because the protocol's frame is four rows of up to
// 21 characters, which needs the long axis to stay readable (see LAYOUT).
#define LCD_ROTATION  1
#define SCREEN_W      LCD_NATIVE_H     // 320 (rotated)
#define SCREEN_H      LCD_NATIVE_W     // 172 (rotated)

#define LCD_SPI_SPEED 40000000L        // 40 MHz: a full-screen flush in ~28 ms
#define BL_BRIGHTNESS 200              // 0-255 backlight duty (battery vs glare)

// ---- Buttons (INPUT_PULLUP, other leg to GND) -------------------------------
// Physical layout left -> right: PREV | GO | NEXT | ACK.
#define PIN_BTN_PREV  4
#define PIN_BTN_GO    5
#define PIN_BTN_NEXT  6
#define PIN_BTN_ACK   7       // 4th button: short = acknowledge, held = FOLLOW
#define PIN_BTN_BOOT  0       // the onboard BOOT button, a second GO. Held at
                              // power-on it forces the WiFi setup portal.

// Per-button debounce state. It lives in this header rather than the sketch
// because the .ino preprocessor hoists generated prototypes above every
// definition in the sketch -- a function taking `Btn &` would be declared
// before the type existed. Types reached through an #include are safe.
struct Btn {
  uint8_t       pin;
  bool          pressed;    // debounced logical state (true = held down)
  unsigned long changeMs;   // when the last edge was accepted
  unsigned long pressMs;    // when the current press began
  bool          longFired;  // long-press already emitted this hold?
  unsigned long repeatMs;   // when the last auto-repeat event fired
};

// ---- Onboard WS2812 RGB LED -------------------------------------------------
// The Nano build blinks one plain LED; here the alert class gets a COLOUR as
// well as a rhythm, which reads across a room. Driven by the core's
// rgbLedWrite(), so no LED library is needed.
#define PIN_RGB       38
#define LED_BRIGHT    60      // 0-255 cap: a WS2812 at full tilt is blinding
                              // and would drain the cell for no benefit

// ---- Battery sense ----------------------------------------------------------
#define PIN_BATT_ADC  1       // ADC1_CH0, the board's own sense divider; set to
                              // -1 to compile the gauge out
#define BATT_DIVIDER  3.0f    // VBAT / measured. 3:1 on the -1.47B (measured --
                              // see the note above). At 2.0f the gauge reads
                              // ~2.8 V, falls under BATT_MIN_MV, and the chip
                              // silently HIDES ITSELF -- which looks like a
                              // missing feature rather than a wrong constant.
// ---- Charging detection -----------------------------------------------------
// This board gives us NO charge-status line: Waveshare does not publish one,
// and an ADC sweep of GPIO 1-10 found every pin except the sense divider
// floating -- a status output would have read solidly high or low. So charging
// is inferred from the cell itself. That is physics rather than guesswork:
//
//   * A LiPo's open-circuit voltage never exceeds ~4.2 V, and this device pulls
//     100 mA+ with the backlight and WiFi up, which sags it further. Sitting AT
//     the charger's regulation point therefore means something external is
//     holding it there -- the charger, in its constant-voltage phase.
//   * Below that, charging shows up as a RISING trend. A cell under a constant
//     load only ever falls, so the sign of the trend separates the two.
//
// The trend needs a LONG baseline. A 3.7 -> 4.2 V charge takes roughly an hour,
// i.e. ~0.14 mV/s, which is far under per-sample ADC noise; comparing against a
// sample CHARGE_TREND_MS old and demanding CHARGE_TREND_MV of movement gets
// above the noise floor. The cost is that plugging a charger in takes about
// that long to be noticed, unless the cell is already near full (where the
// threshold catches it immediately).
#define CHARGE_FULL_MV    4150    // at/above this, an external supply is
                                  // holding the rail up -- treat as charging
#define CHARGE_TREND_MS   90000UL // baseline age before a trend is judged
#define CHARGE_TREND_MV   12      // movement needed to call it (beats ADC noise)

#define BATT_MIN_MV   3000    // below this the reading is treated as "no cell
                              // wired" and the chip is hidden entirely. NOTE:
                              // on this board the charger rail sits at ~4.2 V
                              // whenever USB is attached, so with USB plugged
                              // in the gauge shows ~100% even with no cell --
                              // the "nothing wired" case is only detectable on
                              // battery power.

// ---- LAYOUT (320 x 172 landscape) ------------------------------------------
// The daemon owns all text: it pre-renders FOUR rows of at most 21 characters
// and this firmware draws them, exactly like the 128x32 Nano build. The extra
// pixels buy typography and colour, never a different information model.
//
//   +--------------------------------------------+
//   | (wifi)  CLAUDE MATE              [ 87% ]   |  status bar: link + battery
//   |--------------------------------------------|
//   |  api-server                                |  r0 name, big, state colour
//   |  WAIT   0:42                        work   |  r1 state tag + time
//   |  Opus 4.8 xhigh                    5h82%   |  r2 model + limit chip
//   |                                            |
//   |  2/6  E  B  W  D  I                        |  r3 fleet strip, per-letter
//   |  wifi 192.168.1.42                         |  link detail (firmware-local)
//   |============================================|  state accent bar
//   +--------------------------------------------+
//
// The built-in 5x7 font is used deliberately: it is MONOSPACE, and the protocol
// addresses the active tab by CHARACTER COLUMN within r3 (the `sel` field), so
// column * advance has to be exact. A proportional font would misplace the
// selection box.
#define GLYPH_W       6       // built-in font advance at size 1
#define GLYPH_H       8       // ...and its cell height
#define ROW_CHARS     21      // the protocol's row width

#define PAD_X         6       // left margin for every text row

#define BAR_H         23      // status bar height
#define BAR_RULE_Y    23      // 1px hairline under it

#define R0_Y          30      // name band top
#define R0_H          28
#define R0_SIZE_BIG   3       // 18 px advance -> 17 chars fit in 320 px
#define R0_SIZE_FIT   2       // fallback for long names (21 chars = 252 px)
#define R0_BIG_MAX    17      // longest name still drawn at R0_SIZE_BIG

#define R1_Y          64      // state row
#define R2_Y          86      // meta row
#define ROW_SIZE      2       // 12 px advance: 21 chars = 252 px, fits 320
#define ROW_H         16      // 8 px cell * size 2

#define R3_Y          112     // fleet strip
#define R3_H          20      // band height (the selection box lives in here)
#define SEL_BOX_W     22      // wide box centred on the 10 px selected glyph
#define SEL_BOX_PAD   6       // (SEL_BOX_W - GLYPH_W*ROW_SIZE) / 2 + 1

#define LINK_Y        142     // firmware-local link detail (size 1, dim)
#define ACCENT_Y      164     // full-width state colour bar
#define ACCENT_H      8

// ---- Palette (RGB565) -------------------------------------------------------
// State colours differ in BRIGHTNESS as well as hue, so they stay distinct for
// a red/green-colourblind reader and in direct sunlight.
#define C_BG          RGB565(11, 13, 16)      // near-black
#define C_TEXT        RGB565(232, 234, 237)   // primary text
#define C_DIM         RGB565(107, 114, 128)   // secondary text, rules
#define C_DIMMER      RGB565(55, 60, 70)      // hairlines
#define C_ERROR       RGB565(255, 77, 77)     // E  error
#define C_WAIT        RGB565(255, 176, 32)    // B  waiting / blocked
#define C_WORK        RGB565(53, 196, 240)    // W  working
#define C_DONE        RGB565(52, 211, 153)    // D  done
#define C_IDLE        RGB565(107, 114, 128)   // I  idle
#define C_OK          RGB565(52, 211, 153)    // link up
#define C_BAD         RGB565(255, 77, 77)     // link down

// ---- Timing (identical semantics to the Nano build) ------------------------
#define SERIAL_BAUD     115200
#define LINE_MAX        160    // > the 94-byte worst-case F| frame, with room
                              // for the handshake lines
#define DEBOUNCE_MS     40UL   // immediate-fire debounce
#define LONGPRESS_MS    500UL  // GO held this long -> acknowledge only
#define REPEAT_DELAY_MS 400UL  // PREV/NEXT held this long -> auto-repeat
#define REPEAT_MS       200UL  // ...then one event every 200 ms
#define BLINK_MS        400UL  // flash / blink half-period (~2.5 Hz)
#define LINK_WATCHDOG_MS 30000UL  // daemon silent this long -> NO LINK screen
#define BATT_POLL_MS    5000UL    // battery is slow-moving; don't burn cycles
