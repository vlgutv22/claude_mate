/*
 * Claude Mate - ESP32-S3 wireless firmware (main sketch)
 * =====================================================
 *
 * The cordless sibling of the Arduino Nano build: the same triage companion for
 * a fleet of Claude Code sessions, on a 1.47" colour LCD, talking to the daemon
 * over WiFi instead of a USB cable so it can sit anywhere on a battery.
 *
 * It speaks the SAME protocol, byte for byte (docs/PROTOCOL.md). The daemon
 * cannot tell the two devices apart, and both can be connected at once.
 *
 * THE INTERFACE: one screen, one queue, three buttons.
 *
 * The daemon keeps a STABLE, alphabetically-ordered list of sessions and
 * pre-renders ONE frame as four rows of at most 21 characters. This firmware is
 * a dumb renderer: it holds exactly one frame and draws it. All ordering,
 * selection, truncation and text layout live in the daemon -- the extra pixels
 * here buy TYPOGRAPHY and COLOUR, never a different information model.
 *
 *   +--------------------------------------------+
 *   | (wifi)  CLAUDE MATE               [ 87% ]  |  firmware-local status bar
 *   |--------------------------------------------|
 *   |  api-server                                |  r0 name, big, state colour
 *   |  WAIT   0:42                        work   |  r1 state tag + time (+acct)
 *   |  Opus 4.8 xhigh                    5h82%   |  r2 model + effort (+limit)
 *   |                                            |
 *   |  2/6  E  B  W  D  I                        |  r3 fleet strip; the active
 *   |  wifi 192.168.1.42                         |  tab sits in a filled box,
 *   |============================================|  unacked alerts BLINK
 *   +--------------------------------------------+  accent = worst state colour
 *
 * There are NO modes: the buttons mean the same thing at all times --
 *
 *     PREV          step the selection up the queue      (auto-repeats held)
 *     GO            short: raise that session's terminal window (+ack)
 *                   long : acknowledge the alert WITHOUT raising anything
 *                   double: toggle FOLLOW (the daemon disambiguates)
 *     NEXT          step the selection down the queue    (auto-repeats held)
 *     MIRROR        tap: open/close a live view of the session's TERMINAL
 *
 * MIRROR is the one thing three buttons could not do. Acknowledging and FOLLOW
 * were already reachable through GO, so a 4th button spent on either would have
 * bought a shortcut; showing the actual terminal was not reachable at all. The
 * daemon pulls the rendered TUI from that session's PTY wrapper about once a
 * second and pushes it as M| rows, and while the view is open it reinterprets
 * PREV/NEXT as scroll -- so this firmware still has NO modes: it emits the same
 * verbs regardless and simply draws whichever rows arrive.
 *
 * Only WRAPPED sessions can be mirrored. A hook-only session has no PTY for the
 * daemon to read, and says so rather than showing a blank screen that would
 * look like an idle session.
 *
 * The onboard WS2812 replaces the Nano's single LED: the daemon's V|<KIND>
 * alert class picks both a COLOUR and a RHYTHM, and loops until acknowledged.
 *
 * TWO TRANSPORTS, ONE PROTOCOL
 *
 *   USB   native USB CDC at 115200. Plugged into the Mac the board is a drop-in
 *         Nano replacement -- the daemon's autodetect already globs
 *         /dev/cu.usbmodem*, so it just works with no daemon flags at all.
 *   WiFi  a TCP connection to the daemon's --tcp listener, discovered over mDNS
 *         and authenticated with a nonce/HMAC handshake. See netcfg.h.
 *
 * Input is accepted from BOTH at once, but button events are sent to only ONE
 * (WiFi when it is up, else USB) -- emitting on both would make the daemon count
 * every press twice.
 *
 * If nothing is heard for ~30 s the firmware stops any LED loop and replaces the
 * (stale) frame with a LINK LOST screen: a dead daemon is an honest, visible
 * state rather than a frozen display.
 *
 * CONFIG LINES (USB serial only -- provisioning is not something a remote peer
 * should be able to do, and these are not part of the wire protocol):
 *
 *     ?                     print the current config (never the token itself)
 *     W|<ssid>|<password>   set WiFi credentials
 *     S|<host>|<port>       set the daemon address (empty host = mDNS discovery)
 *     T|<token>             set the shared secret (must match the daemon's)
 *     X|WIPE                clear all stored config
 *     R                     reboot
 *     Z                     start the WiFi setup portal now
 *
 * Or hold BOOT while powering on to bring up the setup portal: the LCD then
 * shows an access point name and password to join from a phone.
 *
 * REQUIRED LIBRARY (Arduino Library Manager):
 *   - GFX Library for Arduino  (Arduino_GFX, by moononournation)
 *
 * BOARD: "Waveshare ESP32-S3-LCD-1.47" with USB Mode "Hardware CDC and JTAG",
 * USB CDC On Boot enabled, and a partition scheme of at least 3 MB app
 * (WiFi + display does not fit the default 1.2 MB). See firmware/README.md.
 *
 * PIN MAP, LAYOUT GEOMETRY AND PALETTE: board_s3.h
 * WIFI TRANSPORT, CONFIG STORAGE AND SETUP PORTAL: netcfg.h
 */

#include <Arduino_GFX_Library.h>
#include "board_s3.h"
#include "netcfg.h"

// ---- Display -----------------------------------------------------------------
// Drawn into an off-screen canvas and flushed in one go: the frame is rebuilt
// from scratch on every change, and without a buffer the clear-then-draw would
// be visible as a flicker once a second while the state timer ticks.
Arduino_DataBus *bus = new Arduino_ESP32SPI(LCD_DC, LCD_CS, LCD_SCK, LCD_MOSI,
                                            GFX_NOT_DEFINED);
Arduino_GFX *panel = new Arduino_ST7789(bus, LCD_RST, LCD_ROTATION,
                                        true /* IPS */,
                                        LCD_NATIVE_W, LCD_NATIVE_H,
                                        LCD_COL_OFFSET, LCD_ROW_OFFSET,
                                        LCD_COL_OFFSET, LCD_ROW_OFFSET);
Arduino_Canvas *canvas = new Arduino_Canvas(SCREEN_W, SCREEN_H, panel);
Arduino_GFX *gfx = nullptr;        // canvas, or the panel if the buffer failed
static bool buffered = true;

// ---- Wireless transport ------------------------------------------------------
MateNet net;

// ---- Current frame (the whole UI state; the daemon owns everything else) -----
static char   frameRow[4][ROW_CHARS + 1] = {{0}};
static bool   frameFlash  = false;   // knock the name out of a filled band
static bool   frameFollow = false;   // FOLLOW mode: draw the play marker
static int8_t frameSel    = -1;      // active tab's fleet-letter column in r3
static bool   haveFrame   = false;   // false until the first F| arrives
static bool   linkLost    = false;   // nothing heard for LINK_WATCHDOG_MS
static bool   blinkOn     = true;    // shared blink phase (flash + fleet)

// ---- Render coalescing -------------------------------------------------------
// Handlers only MARK the display dirty; the redraw happens in loop() once the
// incoming burst has drained -- one flush per burst instead of one per line.
static bool          needRender = false;
static unsigned long dirtyMs    = 0;
static unsigned long lastRxMs   = 0;   // last byte from EITHER transport

// ---- Press feedback ----------------------------------------------------------
// The accent bar goes white for ~80 ms on every accepted button edge: instant
// "the device heard you", well before the daemon round-trips a new frame.
#define BLIP_MS 80UL
static unsigned long blipUntil = 0;

// ---- Serial line assembly ----------------------------------------------------
static char    usbLine[LINE_MAX];
static uint8_t usbLen = 0;
static bool    usbOverflow = false;
static char    netLine[LINE_MAX];
static uint8_t netLen = 0;
static bool    netOverflow = false;

// ---- Battery -----------------------------------------------------------------
static int      battPercent = -1;      // -1 = no cell wired / gauge disabled
static uint32_t battMv      = 0;
static unsigned long lastBattMs = 0;
// ---- Mirror (the terminal view) ---------------------------------------------
// The daemon pre-renders and clips every row to MIRROR_COLS, so this is a plain
// buffer: no wrapping, no scrolling, no truncation decisions taken here.
static bool mirrorOn = false;
static char mirrorTitle[MIRROR_COLS + 1] = {0};
static char mirrorRow[MIRROR_ROWS][MIRROR_COLS + 1] = {{0}};

static uint32_t battRawMv    = 0;      // this poll's median, before the EMA
static uint32_t battFiltMv   = 0;      // EMA state (0 = not yet seeded)
static bool     battCharging = false;  // inferred, not read from a status pin
static uint32_t battTrendMv  = 0;      // baseline the trend is measured against
static unsigned long battTrendMs = 0;  // ...and when it was taken (0 = none)

// -----------------------------------------------------------------------------
// LED: one WS2812, the daemon's alert class picks colour AND rhythm
// -----------------------------------------------------------------------------
// A pattern is a list of ON pulses (on-duration + the gap that follows). It
// either plays once or LOOPS until V|OFF, a new pattern, or the daemon-silence
// watchdog. Same engine and the same rhythms as the Nano build, plus colour:
//
//   START  job (re)started : one long 1 s white blink                (one-shot)
//   INPUT  needs your input: amber, aggressive even blink (~2.8 Hz)  ; LOOPS
//   ERROR  API error/alert : red, frantic strobe (~7 Hz)             ; LOOPS
//   DONE   turn finished   : green cascade, 4 quick blinks + pause   ; LOOPS
//   OFF    (or STOP)       : dark now
#define ALERT_MAX_STEPS 6
struct LedStep { uint16_t onMs; uint16_t offMs; };
static LedStep       ledSteps[ALERT_MAX_STEPS];
static uint8_t       ledStepCount = 0;
static uint8_t       ledStepIdx   = 0;
static bool          ledLoop      = false;
static bool          ledActive    = false;
static bool          ledOn        = false;
static unsigned long ledPhaseMs   = 0;
static uint8_t       ledR = 0, ledG = 0, ledB = 0;
static char          ledKindCode  = 0;   // first letter of the active LOOP kind

static void ledPaint(bool on) {
  if (on) rgbLedWrite(PIN_RGB, ledR, ledG, ledB);
  else    rgbLedWrite(PIN_RGB, 0, 0, 0);
}

static void stopLed() {
  ledActive = ledOn = false;
  ledStepCount = 0;
  ledPaint(false);
}

static void startPattern(const LedStep *steps, uint8_t count, bool loop,
                         uint8_t r, uint8_t g, uint8_t b) {
  if (count == 0) { stopLed(); return; }
  if (count > ALERT_MAX_STEPS) count = ALERT_MAX_STEPS;
  for (uint8_t i = 0; i < count; i++) ledSteps[i] = steps[i];
  // Scale to LED_BRIGHT so a 5 mm WS2812 at arm's length is a signal, not a
  // flashbulb -- and so it costs the battery less.
  ledR = (uint16_t)r * LED_BRIGHT / 255;
  ledG = (uint16_t)g * LED_BRIGHT / 255;
  ledB = (uint16_t)b * LED_BRIGHT / 255;
  ledStepCount = count;
  ledStepIdx   = 0;
  ledLoop      = loop;
  ledActive    = true;
  ledOn        = true;
  ledPhaseMs   = millis();
  ledPaint(true);
}

// Advance the LED state machine. Call every loop(); never blocks.
static void pollLed() {
  if (!ledActive) return;
  unsigned long now = millis();

  // Failsafe: a crashed daemon must not leave the LED blinking forever.
  if (ledLoop && (now - lastRxMs) >= LINK_WATCHDOG_MS) { stopLed(); return; }

  const LedStep &step = ledSteps[ledStepIdx];
  if (ledOn) {
    if ((now - ledPhaseMs) >= step.onMs) {
      ledPaint(false);
      ledOn = false;
      ledPhaseMs = now;
    }
  } else if ((now - ledPhaseMs) >= step.offMs) {
    ledStepIdx++;
    if (ledStepIdx >= ledStepCount) {
      if (!ledLoop) { stopLed(); return; }
      ledStepIdx = 0;
    }
    ledPaint(true);
    ledOn = true;
    ledPhaseMs = now;
  }
}

static void ledForKind(const char *k) {
  // Re-sending the loop that is already playing must not restart its phase (a
  // redundant resend would visibly glitch the rhythm).
  if (ledActive && ledLoop && ledKindCode == k[0] &&
      (k[0] == 'I' || k[0] == 'E' || k[0] == 'D')) {
    return;
  }
  ledKindCode = (k[0] == 'I' || k[0] == 'E' || k[0] == 'D') ? k[0] : 0;
  if (!strcmp(k, "START")) {
    ledKindCode = 0;
    const LedStep s[] = {{1000, 0}};
    startPattern(s, 1, false, 255, 255, 255);
  } else if (!strcmp(k, "INPUT")) {
    const LedStep s[] = {{180, 180}};
    startPattern(s, 1, true, 255, 176, 32);        // amber
  } else if (!strcmp(k, "ERROR")) {
    const LedStep s[] = {{70, 70}};
    startPattern(s, 1, true, 255, 40, 40);         // red
  } else if (!strcmp(k, "DONE")) {
    const LedStep s[] = {{110, 90}, {110, 90}, {110, 90}, {110, 650}};
    startPattern(s, 4, true, 52, 211, 153);        // green
  } else if (!strcmp(k, "OFF") || !strcmp(k, "STOP")) {
    ledKindCode = 0;
    stopLed();
  }
}

// -----------------------------------------------------------------------------
// Battery gauge
// -----------------------------------------------------------------------------
// A LiPo's voltage/charge curve is famously non-linear -- treating it as a
// straight line reads "50%" for most of the discharge and then falls off a
// cliff. This piecewise table is still an approximation, but an honest one.
static int battPercentFromMv(uint32_t mv) {
  static const uint16_t curve[][2] = {
      {4200, 100}, {4100, 92}, {4000, 84}, {3900, 74}, {3800, 62},
      {3700, 48},  {3600, 30}, {3500, 16}, {3400, 8},  {3300, 0},
  };
  if (mv >= curve[0][0]) return 100;
  const size_t n = sizeof(curve) / sizeof(curve[0]);
  if (mv <= curve[n - 1][0]) return 0;
  for (size_t i = 1; i < n; i++) {
    if (mv >= curve[i][0]) {
      uint16_t hiMv = curve[i - 1][0], loMv = curve[i][0];
      uint16_t hiPc = curve[i - 1][1], loPc = curve[i][1];
      return loPc + (int)((long)(mv - loMv) * (hiPc - loPc) / (hiMv - loMv));
    }
  }
  return 0;
}

// Median of BATT_SAMPLES readings taken BATT_SAMPLE_US apart. See the note by
// BATT_SAMPLES in board_s3.h for why a median of spaced samples beats a mean of
// a back-to-back burst on this board.
static uint32_t battMedianMv() {
  uint16_t s[BATT_SAMPLES];
  for (uint8_t i = 0; i < BATT_SAMPLES; i++) {
    s[i] = (uint16_t)analogReadMilliVolts(PIN_BATT_ADC);
    delayMicroseconds(BATT_SAMPLE_US);
  }
  for (uint8_t i = 1; i < BATT_SAMPLES; i++) {   // insertion sort; n is tiny
    uint16_t v = s[i];
    int8_t j = (int8_t)i - 1;
    while (j >= 0 && s[j] > v) { s[j + 1] = s[j]; j--; }
    s[j + 1] = v;
  }
  return s[BATT_SAMPLES / 2];
}

static void pollBattery() {
#if PIN_BATT_ADC >= 0
  unsigned long now = millis();
  if (lastBattMs && (now - lastBattMs) < BATT_POLL_MS) return;
  lastBattMs = now;
  uint32_t raw = (uint32_t)(battMedianMv() * BATT_DIVIDER);
  battRawMv = raw;
  // EMA, seeded on the first poll so the gauge is correct immediately instead
  // of crawling up from zero for the first minute. Rounded and computed in
  // signed arithmetic: a plain shift truncates toward zero, which would stall
  // the filter on small steps in one direction and make it drift only one way.
  if (!battFiltMv) {
    battFiltMv = raw;
  } else {
    const int32_t W = 1 << BATT_EMA_SHIFT;
    battFiltMv = (uint32_t)(((int32_t)battFiltMv * (W - 1) +
                             (int32_t)raw + W / 2) / W);
  }
  uint32_t mv = battFiltMv;
  // Nothing wired to the divider reads as a floating near-zero: report "no
  // gauge" rather than a permanent 0% that would look like a dying cell.
  int pc = (mv < BATT_MIN_MV) ? -1 : battPercentFromMv(mv);
  if (pc != battPercent) needRender = true;
  battMv = mv;
  battPercent = pc;

  // Charging: no status line exists on this board, so infer it. See the long
  // note by CHARGE_FULL_MV in board_s3.h for why this is sound rather than a
  // guess. Order matters -- the "held at the regulation point" test is checked
  // first because it is instant, and the trend is only consulted below it.
  bool chg = battCharging;
  bool usbHost = false;
#if ARDUINO_USB_MODE && ARDUINO_USB_CDC_ON_BOOT
  // Exact and instant, but only for a USB HOST: this watches for SOF frames,
  // which a laptop sends and a dumb wall charger never does. So it settles the
  // tethered case outright and leaves the charger case to the voltage tests.
  usbHost = HWCDC::isPlugged();
#endif
  if (pc < 0) {                             // no cell: nothing to charge
    chg = false;
    battTrendMs = 0;
  } else if (usbHost) {
    chg = true;
    battTrendMv = mv;
    battTrendMs = now;
  } else if (mv >= CHARGE_FULL_MV) {
    chg = true;
    battTrendMv = mv;                       // rebase, so dropping back below
    battTrendMs = now;                      // this starts a clean trend window
  } else if (!battTrendMs) {
    battTrendMv = mv;
    battTrendMs = now;
  } else if (now - battTrendMs >= CHARGE_TREND_MS) {
    long d = (long)mv - (long)battTrendMv;
    if      (d >=  CHARGE_TREND_MV) chg = true;   // climbing: on the charger
    else if (d <= -CHARGE_TREND_MV) chg = false;  // sinking: on the cell
    battTrendMv = mv;                       // ...otherwise flat: keep the last
    battTrendMs = now;                      // verdict rather than flapping
  }
  if (chg != battCharging) {
    battCharging = chg;
    needRender = true;
  }
#endif
}

// -----------------------------------------------------------------------------
// Rendering
// -----------------------------------------------------------------------------

// The state tag the daemon put at the head of r1 ("ERR "/"WAIT"/"DONE"/"WORK"/
// "IDLE", left-justified in 4 columns) drives every accent on the screen.
static uint16_t stateColour() {
  if (!haveFrame) return C_DIM;
  const char *r1 = frameRow[1];
  if (!strncmp(r1, "ERR",  3)) return C_ERROR;
  if (!strncmp(r1, "WAIT", 4)) return C_WAIT;
  if (!strncmp(r1, "WORK", 4)) return C_WORK;
  if (!strncmp(r1, "DONE", 4)) return C_DONE;
  return C_IDLE;
}

// Colour for one fleet letter: E error, B waiting/blocked, W working, D done,
// I idle (lowercase = the same class, with an unacknowledged alert).
static uint16_t letterColour(char c) {
  switch (c | 0x20) {          // fold to lowercase
    case 'e': return C_ERROR;
    case 'b': return C_WAIT;
    case 'w': return C_WORK;
    case 'd': return C_DONE;
    default:  return C_IDLE;
  }
}

static void drawRow(int16_t y, uint8_t size, const char *text, uint16_t colour) {
  gfx->setTextSize(size);
  gfx->setTextColor(colour);
  gfx->setCursor(PAD_X, y);
  gfx->print(text);
}

// A 4-bar signal strength meter, or a "USB" badge when that is the live link.
static void drawLinkGlyph(int16_t x, int16_t y) {
  bool wifiUp = net.connected();
  if (!wifiUp) {
    gfx->setTextSize(1);
    gfx->setTextColor(Serial ? C_OK : C_BAD);
    gfx->setCursor(x, y + 3);
    gfx->print("USB");
    return;
  }
  int8_t r = net.rssi();                      // dBm: -50 great, -90 unusable
  int bars = r >= -55 ? 4 : r >= -67 ? 3 : r >= -78 ? 2 : 1;
  for (int i = 0; i < 4; i++) {
    int16_t h = 3 + i * 3;
    gfx->fillRect(x + i * 4, y + 13 - h, 3, h, i < bars ? C_OK : C_DIMMER);
  }
}

// A 5x7 lightning bolt for the charging indicator: two triangles meeting at the
// waist. Small enough to sit inside the 22x11 battery body without crowding the
// fill bar, and recognisable at this size where a glyph would not be.
static void drawBolt(int16_t x, int16_t y, uint16_t col) {
  gfx->fillTriangle(x + 4, y,     x,     y + 4, x + 3, y + 4, col);  // upper
  gfx->fillTriangle(x + 1, y + 3, x + 4, y + 3, x,     y + 7, col);  // lower
}

// Battery chip: outline, proportional fill, nub, percentage. Falls back to the
// WiFi signal in dBm when no cell is wired, so the corner is never dead space.
static void drawBatteryChip(int16_t right, int16_t y) {
  if (battPercent < 0) {
    if (!net.connected()) return;
    char buf[12];
    snprintf(buf, sizeof(buf), "%ddBm", net.rssi());
    gfx->setTextSize(1);
    gfx->setTextColor(C_DIM);
    gfx->setCursor(right - (int16_t)strlen(buf) * GLYPH_W, y + 3);
    gfx->print(buf);
    return;
  }
  char buf[8];
  snprintf(buf, sizeof(buf), "%d%%", battPercent);
  int16_t textW = (int16_t)strlen(buf) * GLYPH_W;
  int16_t bodyW = 22, bodyH = 11;
  int16_t bx = right - textW - 4 - bodyW - 2;
  int16_t by = y + 2;
  // Charging outranks the low-battery warning colours: a cell at 8% that is on
  // the charger is good news, and colouring it red would say the opposite.
  uint16_t col = battCharging ? C_OK
                 : battPercent <= 10 ? C_ERROR
                 : battPercent <= 25 ? C_WAIT : C_DIM;
  gfx->drawRect(bx, by, bodyW, bodyH, col);
  gfx->fillRect(bx + bodyW, by + 3, 2, bodyH - 6, col);           // the nub
  int16_t fillW = (int16_t)((bodyW - 4) * battPercent / 100);
  if (fillW > 0) gfx->fillRect(bx + 2, by + 2, fillW, bodyH - 4, col);
  // The bolt sits ON TOP of the fill in the background colour, so it stays
  // legible at 100% (all fill) and at 0% (no fill) alike -- a bolt drawn in the
  // accent colour would vanish into a full bar.
  if (battCharging) drawBolt(bx + bodyW / 2 - 2, by + 2, C_BG);
  gfx->setTextSize(1);
  gfx->setTextColor(col);
  gfx->setCursor(right - textW, y + 2);
  gfx->print(buf);
}

static void drawStatusBar() {
  drawLinkGlyph(PAD_X, 4);
  gfx->setTextSize(1);
  gfx->setTextColor(C_DIM);
  gfx->setCursor(PAD_X + 24, 7);
  gfx->print("CLAUDE MATE");
  drawBatteryChip(SCREEN_W - PAD_X, 4);
  gfx->fillRect(0, BAR_RULE_Y, SCREEN_W, 1, C_DIMMER);
}

// The bottom band: what the link is doing (firmware-local, never from the
// daemon) plus the state accent bar.
static void drawFooter(uint16_t accent) {
  gfx->setTextSize(1);
  gfx->setTextColor(C_DIM);
  gfx->setCursor(PAD_X, LINK_Y);
  gfx->print(net.statusText());
  bool blip = (long)(millis() - blipUntil) < 0;
  gfx->fillRect(0, ACCENT_Y, SCREEN_W, ACCENT_H, blip ? C_TEXT : accent);
}

static void drawFrame() {
  uint16_t accent = stateColour();

  // r0 (name). Big when it fits, one size down when it does not -- the same
  // "best fit into the room available" instinct the daemon uses for the meta
  // row, rather than truncating a name the daemon already truncated once.
  size_t nameLen = strlen(frameRow[0]);
  uint8_t nameSize = nameLen <= R0_BIG_MAX ? R0_SIZE_BIG : R0_SIZE_FIT;
  int16_t nameH = GLYPH_H * nameSize;
  int16_t nameY = R0_Y + (R0_H - nameH) / 2;
  if (frameFlash && blinkOn) {
    // The colour equivalent of the Nano's inverted band: fill with the state
    // colour and knock the name out of it.
    gfx->fillRect(0, R0_Y - 2, SCREEN_W, R0_H + 4, accent);
    drawRow(nameY, nameSize, frameRow[0], C_BG);
  } else {
    drawRow(nameY, nameSize, frameRow[0], accent);
  }

  // r1 (state tag + time + account). The 4-column tag takes the state colour,
  // the rest stays neutral so the time is easy to read.
  gfx->setTextSize(ROW_SIZE);
  char tag[5] = {0};
  strncpy(tag, frameRow[1], 4);
  gfx->setTextColor(accent);
  gfx->setCursor(PAD_X, R1_Y);
  gfx->print(tag);
  if (strlen(frameRow[1]) > 4) {
    gfx->setTextColor(C_TEXT);
    gfx->setCursor(PAD_X + 4 * GLYPH_W * ROW_SIZE, R1_Y);
    gfx->print(frameRow[1] + 4);
  }

  // r2 (model + effort + limit chip): supporting detail, deliberately quieter.
  drawRow(R2_Y, ROW_SIZE, frameRow[2], C_DIM);

  // r3 (fleet strip), character by character so each letter can take its own
  // colour, an UNACKNOWLEDGED alert's letter (sent LOWERCASE) can BLINK, and
  // the active tab can sit in a filled box with the letter knocked out.
  gfx->setTextSize(ROW_SIZE);
  const int16_t cw = GLYPH_W * ROW_SIZE;
  if (frameSel >= 0) {
    int16_t bx = PAD_X + frameSel * cw - SEL_BOX_PAD;
    if (bx < 0) bx = 0;
    int16_t bw = SEL_BOX_W;
    if (bx + bw > SCREEN_W) bw = SCREEN_W - bx;
    gfx->fillRoundRect(bx, R3_Y - 2, bw, R3_H, 3, C_TEXT);
  }
  for (uint8_t c = 0; frameRow[3][c]; c++) {
    char ch = frameRow[3][c];
    if (ch == ' ') continue;
    bool unacked  = (ch >= 'a' && ch <= 'z');
    bool selected = ((int8_t)c == frameSel);
    char up = unacked ? (char)(ch - 32) : ch;
    uint16_t col;
    if (selected) {
      col = C_BG;                                  // knocked out of the box
    } else if (isdigit((unsigned char)ch) || ch == '/' || ch == '+') {
      col = C_DIM;                                 // the "2/6 " position prefix
    } else if (unacked && !blinkOn) {
      col = C_DIMMER;                              // blink phase: off
    } else {
      col = letterColour(up);
    }
    gfx->setTextColor(col);
    gfx->setCursor(PAD_X + c * cw, R3_Y);
    gfx->write(up);
  }

  // FOLLOW mode: a play marker at the right of the state row. The daemon keeps
  // r1's last two columns blank while following, so it never overdraws text.
  if (frameFollow) {
    int16_t x = PAD_X + 19 * cw + 2;
    gfx->fillTriangle(x, R1_Y + 1, x, R1_Y + ROW_H - 1,
                      x + 10, R1_Y + ROW_H / 2, C_TEXT);
  }

  drawFooter(accent);
}

// Firmware-local: nothing heard for LINK_WATCHDOG_MS. An honest state instead
// of a silently stale frame.
static void drawLinkLost() {
  gfx->setTextSize(3);
  gfx->setTextColor(C_BAD);
  gfx->setCursor(PAD_X, 48);
  gfx->print("NO LINK");
  gfx->setTextSize(1);
  gfx->setTextColor(C_DIM);
  gfx->setCursor(PAD_X, 84);
  gfx->print("waiting for the daemon");
  gfx->setCursor(PAD_X, 100);
  gfx->print(net.configured() ? "check it is running with --tcp"
                              : "hold BOOT at power-on to set up wifi");
  drawFooter(C_BAD);
}

// Firmware-local: booted, no frame from the daemon yet.
static void drawSplash() {
  gfx->setTextSize(3);
  gfx->setTextColor(C_TEXT);
  gfx->setCursor(PAD_X, 44);
  gfx->print("MATE");
  gfx->setTextSize(1);
  gfx->setTextColor(C_DIM);
  gfx->setCursor(PAD_X, 80);
  gfx->print("starting...");
  drawFooter(C_DIM);
}

// Firmware-local: the setup portal is up. Everything needed to join it is on
// the glass, because there is nowhere else to read it from.
static void drawSetup() {
  gfx->setTextSize(2);
  gfx->setTextColor(C_WORK);
  gfx->setCursor(PAD_X, 30);
  gfx->print("WIFI SETUP");
  gfx->setTextSize(1);
  gfx->setTextColor(C_DIM);
  gfx->setCursor(PAD_X, 58);
  gfx->print("join this network from a phone:");
  gfx->setTextSize(2);
  gfx->setTextColor(C_TEXT);
  gfx->setCursor(PAD_X, 74);
  gfx->print(net.apName());
  gfx->setTextSize(1);
  gfx->setTextColor(C_DIM);
  gfx->setCursor(PAD_X, 98);
  gfx->print("password");
  gfx->setTextSize(2);
  gfx->setTextColor(C_TEXT);
  gfx->setCursor(PAD_X + 60, 94);
  gfx->print(net.apPass());
  gfx->setTextSize(1);
  gfx->setTextColor(C_DIM);
  gfx->setCursor(PAD_X, LINK_Y);
  gfx->print("then open http://" + net.apIp());
  gfx->fillRect(0, ACCENT_Y, SCREEN_W, ACCENT_H, C_WORK);
}

// The terminal view: a title strip, then the rows exactly as the daemon clipped
// them. Monospace and unstyled on purpose -- this is a window onto a TUI whose
// own box drawing and alignment carry the meaning, so any prettifying here
// would fight it.
static void drawMirror() {
  gfx->setTextSize(1);
  gfx->setTextColor(C_WORK);
  gfx->setCursor(PAD_X, 7);
  gfx->print(mirrorTitle);
  gfx->fillRect(0, BAR_RULE_Y, SCREEN_W, 1, C_DIMMER);
  gfx->setTextColor(C_TEXT);
  for (uint8_t r = 0; r < MIRROR_ROWS; r++) {
    if (!mirrorRow[r][0]) continue;
    gfx->setCursor(PAD_X, MIRROR_Y + r * MIRROR_LH);
    gfx->print(mirrorRow[r]);
  }
}

static void render() {
  gfx->fillScreen(C_BG);
  if (mirrorOn) {
    // Deliberately ahead of the SETUP check: the mirror is only ever opened by
    // a button press on a linked device, so if it is on, it is what was asked
    // for.
    drawMirror();
  } else if (net.state() == MateNet::SETUP) {
    drawSetup();
  } else {
    drawStatusBar();
    if      (linkLost)   drawLinkLost();
    else if (!haveFrame) drawSplash();
    else                 drawFrame();
  }
  if (buffered) canvas->flush();
}

static void requestRender() {
  if (!needRender) {
    needRender = true;
    dirtyMs = millis();
  }
}

// -----------------------------------------------------------------------------
// Protocol
// -----------------------------------------------------------------------------

// Copy a field into dst with a hard cap (always NUL-terminated).
static void copyField(char *dst, uint8_t cap, const char *src) {
  if (!src) { dst[0] = 0; return; }
  uint8_t i = 0;
  while (src[i] && i < (cap - 1)) { dst[i] = src[i]; i++; }
  dst[i] = 0;
}

// Which transport button events go to. Exactly ONE, even when both are up:
// emitting on both would make the daemon count every press twice.
static void emitLine(const char *line) {
  if (net.connected()) net.write(line);
  else if (Serial)     Serial.println(line);
}

// Handle one complete, NUL-terminated protocol line (from either transport).
static void handleLine(char *line) {
  if (line[0] == 0) return;

  switch (line[0]) {
    case 'P':                    // ping -> keepalive ack. NOT 'H': the daemon
      emitLine("K");             // treats H as "I rebooted" and does a full
      break;                     // resend + LED re-arm, which would restart the
                                 // blink phase every 15 s.

    case 'F': {  // F|<flags>|<sel>|<r0>|<r1>|<r2>|<r3> -- the whole screen
      // Split off exactly the first 6 '|'; fields[6] (=r3) then holds the rest
      // of the line VERBATIM, including any literal '|'.
      char *fields[7];
      uint8_t n = 0;
      char *p = line;
      fields[n++] = p;
      while (n < 7) {
        char *bar = strchr(p, '|');
        if (!bar) break;
        *bar = 0;
        p = bar + 1;
        fields[n++] = p;
      }
      if (n < 7) break;                    // malformed: ignore
      uint8_t flags = (uint8_t)atoi(fields[1]);
      frameFlash  = flags & 1;
      frameFollow = flags & 2;
      frameSel    = (int8_t)atoi(fields[2]);
      for (uint8_t r = 0; r < 4; r++)
        copyField(frameRow[r], sizeof(frameRow[r]), fields[3 + r]);
      haveFrame = true;
      requestRender();
      break;
    }

    case 'V': {                            // V|<KIND> -- play an LED pattern
      char *bar = strchr(line, '|');
      if (!bar || bar[1] == 0) break;
      ledForKind(bar + 1);
      break;
    }

    case 'M': {  // the terminal MIRROR: M|T|<title>, M|<n>|<text>, M|END, M|OFF
      char *bar = strchr(line, '|');
      if (!bar) break;
      *bar = 0;
      char *arg = bar + 1;
      if (!strcmp(arg, "OFF")) {           // daemon closed the view
        mirrorOn = false;
        requestRender();
        break;
      }
      if (!strcmp(arg, "END")) {           // a full set of rows has landed:
        mirrorOn = true;                   // show them in ONE flush, so the
        requestRender();                   // view never tears mid-update
        break;
      }
      char *bar2 = strchr(arg, '|');
      if (!bar2) break;
      *bar2 = 0;
      char *text = bar2 + 1;               // rest of the line VERBATIM
      if (!strcmp(arg, "T")) {
        copyField(mirrorTitle, sizeof(mirrorTitle), text);
        break;
      }
      int row = atoi(arg);
      if (row >= 0 && row < MIRROR_ROWS)
        copyField(mirrorRow[row], sizeof(mirrorRow[row]), text);
      break;
    }

    default:
      break;                               // unknown line: ignore silently
  }
}

// -----------------------------------------------------------------------------
// Config lines (USB serial only)
// -----------------------------------------------------------------------------
// Provisioning is not part of the wire protocol and is deliberately NOT
// reachable over the network: a remote peer must not be able to repoint the
// device or read back its configuration.
static bool handleConfigLine(char *line) {
  switch (line[0]) {
    case '?':
      net.printConfig(Serial);
      // The gauge is inferred (divider ratio measured, charging deduced from
      // the cell), so print the raw millivolts it is working from. Without this
      // a wrong BATT_DIVIDER is invisible -- it just shows a plausible, wrong
      // percentage, which is exactly how the 2:1/3:1 mix-up hid for so long.
#if PIN_BATT_ADC >= 0
      // Both figures, because they answer different questions: the filtered mV
      // is what the gauge shows, while raw-vs-filtered is the live noise on the
      // node -- if those two are far apart the filter is being asked to hide a
      // hardware problem, not just dither.
      if (battPercent < 0)
        Serial.printf("batt  : no cell (%u mV filt, %u raw, x%.1f)\n",
                      (unsigned)battMv, (unsigned)battRawMv,
                      (double)BATT_DIVIDER);
      else
        Serial.printf("batt  : %d%% (%u mV filt, %u raw) %s\n", battPercent,
                      (unsigned)battMv, (unsigned)battRawMv,
                      battCharging ? "charging" : "on battery");
#else
      Serial.println("batt  : gauge compiled out");
#endif
      return true;

    case 'W': {                            // W|<ssid>|<password>
      char *a = strchr(line, '|');
      if (!a) return false;
      *a++ = 0;
      char *b = strchr(a, '|');
      if (b) *b++ = 0;
      net.setWifi(a, b ? b : "");
      Serial.printf("wifi set: %s\n", a);
      net.restart();
      requestRender();
      return true;
    }

    case 'S': {                            // S|<host>|<port>
      char *a = strchr(line, '|');
      if (!a) return false;
      *a++ = 0;
      char *b = strchr(a, '|');
      if (b) *b++ = 0;
      net.setDaemon(a, b ? (uint16_t)atoi(b) : 0);
      Serial.printf("daemon set: %s\n", a[0] ? a : "(mDNS discovery)");
      net.restart();
      return true;
    }

    case 'T': {                            // T|<token>
      char *a = strchr(line, '|');
      if (!a) return false;
      net.setToken(a + 1);
      Serial.println("token set");
      net.restart();
      return true;
    }

    case 'X':                              // X|WIPE -- deliberately awkward, so
      if (strcmp(line, "X|WIPE")) {        // a stray byte cannot erase setup
        Serial.println("refusing: send X|WIPE to clear config");
        return true;
      }
      net.wipe();
      Serial.println("config wiped; rebooting");
      delay(100);
      ESP.restart();
      return true;

    case 'R':
      if (line[1]) return false;
      Serial.println("rebooting");
      delay(100);
      ESP.restart();
      return true;

    case 'Z':                              // start the setup portal now
      if (line[1]) return false;
      Serial.println("starting setup portal");
      net.startPortalNow();
      requestRender();
      return true;
  }
  return false;
}

// -----------------------------------------------------------------------------
// Transport pumps
// -----------------------------------------------------------------------------
// Input is accepted from BOTH transports: whichever the daemon is on, the
// device follows. Frames are idempotent, so even a daemon reachable over both
// at once is harmless.

static void pumpUsb() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    lastRxMs = millis();
    if (c == '\n' || c == '\r') {
      if (!usbOverflow && usbLen > 0) {
        usbLine[usbLen] = 0;
        // Config lines are USB-only; anything else is the wire protocol.
        if (!handleConfigLine(usbLine)) handleLine(usbLine);
        if (linkLost) { linkLost = false; requestRender(); }
      }
      usbLen = 0;
      usbOverflow = false;
    } else if (usbLen < (LINE_MAX - 1)) {
      usbLine[usbLen++] = c;
    } else {
      usbOverflow = true;                  // drop to the newline, then resync
    }
  }
}

static void pumpNet() {
  int ci;
  while ((ci = net.read()) >= 0) {
    char c = (char)ci;
    lastRxMs = millis();
    if (c == '\n' || c == '\r') {
      if (!netOverflow && netLen > 0) {
        netLine[netLen] = 0;
        handleLine(netLine);
        if (linkLost) { linkLost = false; requestRender(); }
      }
      netLen = 0;
      netOverflow = false;
    } else if (netLen < (LINE_MAX - 1)) {
      netLine[netLen++] = c;
    } else {
      netOverflow = true;
    }
  }
}

// -----------------------------------------------------------------------------
// Buttons
// -----------------------------------------------------------------------------
// Immediate-fire debounce: an edge is ACCEPTED (and its event emitted) the very
// tick it is seen, provided the last accepted edge is >= DEBOUNCE_MS old, so
// press latency is ~0 ms. PREV/NEXT emit on the press edge and auto-repeat while
// held; GO distinguishes a SHORT press (emit on release) from a LONG press (emit
// once at LONGPRESS_MS, then swallow the release). Identical to the Nano build --
// the daemon's double-click and long-press handling depends on it.
// (struct Btn is in board_s3.h; see the note there.)
static Btn prevBtn = {PIN_BTN_PREV, false, 0, 0, false, 0};
static Btn goBtn   = {PIN_BTN_GO,   false, 0, 0, false, 0};
static Btn nextBtn = {PIN_BTN_NEXT, false, 0, 0, false, 0};
static Btn mirrorBtn = {PIN_BTN_MIRROR, false, 0, 0, false, 0};
static Btn bootBtn = {PIN_BTN_BOOT, false, 0, 0, false, 0};

static void emitBtn(char c) {
  char buf[4] = {'B', '|', c, 0};
  emitLine(buf);
  blipUntil = millis() + BLIP_MS;           // instant "heard you" feedback
  requestRender();
}

static void pollNavBtn(Btn &b, char ev) {
  bool raw = (digitalRead(b.pin) == LOW);   // pull-up: LOW = pressed
  unsigned long now = millis();
  if (raw != b.pressed && (now - b.changeMs) >= DEBOUNCE_MS) {
    b.pressed  = raw;
    b.changeMs = now;
    if (raw) {
      b.pressMs  = now;
      b.repeatMs = now;
      emitBtn(ev);
    }
  }
  if (b.pressed && (now - b.pressMs) >= REPEAT_DELAY_MS &&
      (now - b.repeatMs) >= REPEAT_MS) {
    b.repeatMs = now;
    emitBtn(ev);
  }
}

static void pollGoBtn(Btn &b) {
  bool raw = (digitalRead(b.pin) == LOW);
  unsigned long now = millis();
  if (raw != b.pressed && (now - b.changeMs) >= DEBOUNCE_MS) {
    b.pressed  = raw;
    b.changeMs = now;
    if (raw) {
      b.pressMs   = now;
      b.longFired = false;
    } else if (!b.longFired) {
      emitBtn('G');                         // short press: focus + acknowledge
    }
  }
  if (b.pressed && !b.longFired && (now - b.pressMs) >= LONGPRESS_MS) {
    emitBtn('K');                           // acknowledge without focusing
    b.longFired = true;
  }
}

// The 4th button: MIRROR. A plain tap, emitted on the press edge -- no repeat
// while held (that would flap the view on and off) and no long-press meaning.
// It is the one action that needed a button of its own: acknowledging and
// FOLLOW were already reachable through GO's long-press and double-click,
// whereas showing the terminal was not reachable at all.
//
// While the view is open the DAEMON reinterprets PREV/NEXT as scroll, so this
// firmware never learns that a mode exists: it emits the same two verbs either
// way and just draws whatever rows arrive.
static void pollTapBtn(Btn &b, char ev) {
  bool raw = (digitalRead(b.pin) == LOW);
  unsigned long now = millis();
  if (raw != b.pressed && (now - b.changeMs) >= DEBOUNCE_MS) {
    b.pressed  = raw;
    b.changeMs = now;
    if (raw) {
      b.pressMs = now;
      emitBtn(ev);
    }
  }
}

static void pollButtons() {
  pollNavBtn(prevBtn, 'P');
  pollGoBtn(goBtn);
  pollNavBtn(nextBtn, 'N');
  pollTapBtn(mirrorBtn, 'M');
  pollGoBtn(bootBtn);                       // BOOT is a second GO, so a board
                                            // with nothing soldered still works
}

// -----------------------------------------------------------------------------
// Setup / loop
// -----------------------------------------------------------------------------

void setup() {
  pinMode(PIN_BTN_PREV, INPUT_PULLUP);
  pinMode(PIN_BTN_GO,   INPUT_PULLUP);
  pinMode(PIN_BTN_NEXT, INPUT_PULLUP);
  pinMode(PIN_BTN_MIRROR, INPUT_PULLUP);
  pinMode(PIN_BTN_BOOT, INPUT_PULLUP);
  rgbLedWrite(PIN_RGB, 0, 0, 0);            // LED dark at boot
  lastRxMs = millis();                      // seed the liveness watchdog

  Serial.begin(SERIAL_BAUD);

  // Backlight on a PWM channel so it can be dimmed (and so a battery build can
  // trade brightness for runtime).
  pinMode(LCD_BL, OUTPUT);
  analogWrite(LCD_BL, BL_BRIGHTNESS);

  // Try the buffered canvas first; fall back to drawing straight at the panel
  // if the framebuffer will not fit. Slight flicker beats a blank screen.
  if (canvas->begin(LCD_SPI_SPEED)) {
    gfx = canvas;
    buffered = true;
  } else {
    panel->begin(LCD_SPI_SPEED);
    gfx = panel;
    buffered = false;
  }
  gfx->setTextWrap(false);                  // clip at the edge; never reflow a
                                            // row onto the next one
  gfx->fillScreen(C_BG);
  if (buffered) canvas->flush();

  // Holding BOOT (or GO) through power-on forces the WiFi setup portal: that is
  // how the device is moved to a new network with no serial console around.
  bool forcePortal = (digitalRead(PIN_BTN_BOOT) == LOW) ||
                     (digitalRead(PIN_BTN_GO) == LOW);
  net.begin(forcePortal);

  render();                                 // splash until the daemon talks

  // Emit hello once so a daemon already listening on USB sends full state. The
  // WiFi path sends its own H the moment the handshake completes (see loop()).
  if (Serial) Serial.println("H");
}

void loop() {
  static MateNet::State lastNetState = MateNet::OFF;

  net.poll();
  net.applyPendingConfig();
  pumpUsb();
  pumpNet();
  pollButtons();
  pollLed();
  pollBattery();

  unsigned long now = millis();

  // A freshly authenticated link needs the same kick a Nano's reset gives: H
  // makes the daemon resend the frame AND re-arm the LED loop, so an alert that
  // was already waiting is not silently missed.
  MateNet::State ns = net.state();
  if (ns != lastNetState) {
    if (ns == MateNet::LINKED) {
      net.write("H");
      lastRxMs = now;                       // the link is alive as of now
    }
    lastNetState = ns;
    requestRender();                        // the footer/status glyph changed
  }

  // Liveness watchdog: nothing heard for too long -> honest NO LINK screen
  // (pollLed() stops any LED loop on the same condition).
  if (!linkLost && (now - lastRxMs) >= LINK_WATCHDOG_MS) {
    linkLost = true;
    requestRender();
  }

  // Blink phase for the flashing name and the unacked fleet letters (~2.5 Hz).
  bool nb = (now / BLINK_MS) & 1;
  if (nb != blinkOn) {
    blinkOn = nb;
    bool blinkStrip = false;
    for (uint8_t c = 0; frameRow[3][c]; c++)
      if (frameRow[3][c] >= 'a' && frameRow[3][c] <= 'z') { blinkStrip = true; break; }
    if (haveFrame && !linkLost && (frameFlash || blinkStrip)) requestRender();
  }

  // The press blip expiring needs one more frame to clear it.
  if (blipUntil && (long)(now - blipUntil) >= 0) {
    blipUntil = 0;
    requestRender();
  }

  // Redraw once the incoming burst has drained (>= 8 ms of quiet), or after
  // 60 ms regardless so a continuous stream cannot starve the display. Ticking
  // times mean roughly one redraw a second in practice.
  if (needRender) {
    bool quiet = (Serial.available() == 0) && (now - lastRxMs) >= 8;
    if (quiet || (now - dirtyMs) >= 60) {
      needRender = false;
      render();
    }
  }
}
