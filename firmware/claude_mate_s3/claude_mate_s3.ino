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
 *     MIRROR        tap : open/close a live view of the session's TERMINAL
 *                   hold: power off (deep sleep); tap again to wake
 *
 * MIRROR is the one thing three buttons could not do. Acknowledging and FOLLOW
 * were already reachable through GO, so a 4th button spent on either would have
 * bought a shortcut; showing the actual terminal was not reachable at all. The
 * daemon pulls the rendered TUI from that session's PTY wrapper about once a
 * second and pushes it as M| rows, and while the view is open it reinterprets
 * PREV/NEXT as scroll -- so this firmware still has NO modes: it emits the same
 * verbs regardless and simply draws whichever rows arrive.
 *
 * POWER OFF is deep sleep, not zero: the WS2812 has no shutdown pin and idles
 * ~1 mA whenever the rail is up, dwarfing the ~8 uA the S3 draws asleep. That is
 * roughly a month of standby on a 14500 -- off in every practical sense, though
 * a switch in the battery lead is the only true zero. Waking REBOOTS (deep sleep
 * does not resume), so rejoining WiFi costs a few seconds; NVS config survives.
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
#include <esp_sleep.h>
#include <driver/rtc_io.h>
#include "board_s3.h"
#include "netcfg.h"
#include "settings.h"
#include "game/ship_it.h"

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

// ---- Device settings (firmware-local; see settings.h for why) ---------------
MateSettings cfg;

// ---- UI mode -----------------------------------------------------------------
// CONDUCTOR is the triage view this device has always shown: the daemon's frame,
// its terminal mirror, and nothing of the firmware's own invention. It is named
// because it is now one of several things the screen can be, and because that is
// what the device does -- it conducts a fleet of agents the way a conductor
// conducts an orchestra, watching all of them so you only have to watch one
// surface.
//
// MENU and PAGE are entirely firmware-local. While either is up, PREV/NEXT/GO
// are handled HERE and never reach the daemon -- which is a real change to the
// old promise that "this firmware never learns that a mode exists". It has to
// be: forwarding a GO while you are aiming at a settings row would raise a
// terminal you were not looking at. The daemon simply sees no button events,
// needs no protocol change, and keeps its frame current the whole time, so
// leaving the menu is instant.
enum UiMode : uint8_t { UI_CONDUCTOR, UI_MENU, UI_PAGE, UI_GAME, UI_PAD };

// CONTROLLER MODE. The daemon turns this on (X|1) while a browser page is
// driving the device's buttons, and off (X|0) when the page goes away.
//
// It exists because the ordinary button semantics are unplayable as a gamepad,
// and not by a little: pollNavBtn debounces for 40 ms, then waits 400 ms before
// auto-repeating, then emits one event every 200 ms. That is correct for a
// selection list and it means a held direction moves you once, stalls for most
// of a second, then stutters. Reported as "huge delay", and it was.
//
// Here the pins are read raw every loop and PRESS/RELEASE EDGES go up the link
// (B|+P, B|-P, ...), so the page knows what is held down right now, which is
// the only question a platformer asks.
//
// It is also the low-power screen. Nobody is looking at the glass while they
// play on a monitor, so the pad face is drawn ONCE and the backlight drops --
// a full flush is 110 KB over SPI, and doing that 30 times a second to render a
// screen nobody is watching is exactly the overuse this mode avoids.
static bool     padActive   = false;
static bool     padDirty    = false;   // only the pad's OWN changes redraw it
static uint8_t  padHeld     = 0;      // bitmask, for the on-glass key preview
static UiMode   padReturnTo = UI_CONDUCTOR;
static UiMode uiMode = UI_CONDUCTOR;

enum MenuItem : uint8_t {
  MI_CONDUCTOR, MI_SETTINGS, MI_ABOUT, MI_WIFI, MI_GAME, MI_SLEEP
};
static uint8_t menuIdx = MI_CONDUCTOR;

enum PageId : uint8_t { PG_SETTINGS, PG_ABOUT };
static PageId  pageId  = PG_SETTINGS;
static uint8_t pageIdx = 0;          // selected row within the page

// Settings rows, in screen order.
enum SetRow : uint8_t {
  SR_SLEEP, SR_BRIGHT, SR_LED, SR_SOUND, SR_FLIP, SR_RESET, SR_COUNT
};
// Which row is at the top of the visible window. The page scrolls now: five
// rows fit and there are six, which is exactly the case the static_assert below
// used to reject outright. Scrolling was named in its own comment as one of the
// two ways out, and it is the one that keeps paying off.
static uint8_t pageTop = 0;
// The page does not scroll, so a sixth row would simply not be drawn -- and
// would look like a bug in the setting rather than in the layout. Adding one
// means either raising PAGE_ROWS_VIS (and finding the pixels) or teaching the
// page to scroll; this makes the compiler say so instead of the screen.
// The page scrolls, so rows may exceed PAGE_ROWS_VIS -- but the WINDOW must
// still be at least one row, and pageTop arithmetic assumes it.
static_assert(PAGE_ROWS_VIS >= 1, "the settings window needs at least one row");

// The factory-reset row asks twice. A single GO arms it and the row says what is
// about to be destroyed; the confirming gesture is a LONG press, which is a
// different motion rather than the same one again -- you cannot double-tap your
// way into wiping the token by accident. It disarms itself if you walk away.
#define RESET_ARM_MS 6000UL
static unsigned long resetArmedMs = 0;

// A flip only takes effect through the panel's rotation, which is applied once
// at begin(). Rather than re-initialising a live display -- the one failure mode
// in this firmware that looks exactly like a dead panel -- the row says it needs
// a restart and offers one. Compared against the value we BOOTED with, not a
// sticky flag, so toggling it twice correctly stops asking.
static bool flipAtBoot       = false;
static bool flipNeedsRestart = false;

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

// ---- Screen sleep (hibernate) ------------------------------------------------
// The backlight is by far the biggest draw on this board -- tens of milliamps
// against the ~1 mA the WS2812 idles at -- so turning it off is most of the
// runtime win available without touching the radio.
//
// ONLY the backlight goes off. displayOff() on the ST7789 would save a further
// milliamp or two, and would put the wake path one command away from the exact
// failure this firmware already warns about twice: a panel that is driven
// perfectly and never lights, indistinguishable from a dead board. Not worth it
// for 2 mA.
//
// The frame keeps arriving and keeps being parsed while the screen is dark, so
// waking shows the CURRENT state rather than a stale one that then jumps. The
// LED keeps playing its pattern too -- it is the alert channel, and an alert
// that only lit a screen nobody is looking at would be no alert at all.
static bool          screenOn   = true;
static unsigned long lastPokeMs = 0;   // last button press, or last alert

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
// Why the last boot happened: ESP_SLEEP_WAKEUP_UNDEFINED is a real power-on,
// EXT1 is a wake from powerOff(). Surfaced in `?` because a device that woke
// unexpectedly and one that browned out and rebooted look identical otherwise.
static esp_sleep_wakeup_cause_t wakeCause = ESP_SLEEP_WAKEUP_UNDEFINED;

static bool mirrorOn = false;
static char mirrorTitle[MIRROR_COLS + 1] = {0};
static char mirrorRow[MIRROR_ROWS][MIRROR_COLS + 1] = {{0}};

// The displayed level: 3, 2, 1, or -1 for "no cell". Deliberately separate from
// battPercent, which stays exact for the `?` readout and the About page --
// diagnosis wants the number, the glance wants the shape.
static int8_t   battLevel     = -1;
static int8_t   battLevelCand = -1;    // level the raw voltage is asking for...
static unsigned long battLevelSince = 0;  // ...and how long it has asked

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
static uint8_t       ledR = 0, ledG = 0, ledB = 0;              // driven
static uint8_t       ledBaseR = 0, ledBaseG = 0, ledBaseB = 0;  // before the cap
static char          ledKindCode  = 0;   // first letter of the active LOOP kind

// Re-derive the driven colour from the pattern's own colour and the configured
// cap. Separated out so the Alert LED setting can take effect on a pattern that
// is ALREADY playing: re-arming it through ledForKind() would not work at all
// (its anti-glitch guard returns early for a resend of the same looping kind)
// and would restart the rhythm's phase if it did.
static void ledApplyCap() {
  uint8_t cap = cfg.ledBright();
  ledR = (uint16_t)ledBaseR * cap / 255;
  ledG = (uint16_t)ledBaseG * cap / 255;
  ledB = (uint16_t)ledBaseB * cap / 255;
  if (ledActive && ledOn) rgbLedWrite(PIN_RGB, ledR, ledG, ledB);
  else                    rgbLedWrite(PIN_RGB, 0, 0, 0);
}

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
  // Keep the pattern's own colour, then scale it to the configured cap so a
  // 5 mm WS2812 at arm's length is a signal, not a flashbulb -- and so it costs
  // the battery less. Settings can take the cap to 0, which is a real silent
  // mode: the pattern still runs, the pixel just stays dark, and the alert still
  // reaches you through the flashing name row and fleet letter. LED_BRIGHT
  // remains the default.
  //
  // The UNSCALED colour is kept so that changing the cap can re-scale a pattern
  // that is already playing (see ledApplyCap) instead of re-arming it, which
  // would restart the rhythm's phase mid-blink.
  ledBaseR = r; ledBaseG = g; ledBaseB = b;
  ledApplyCap();
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
  battMv = mv;
  battPercent = pc;

  // ---- the displayed level: hysteresis, then a hold ----------------------
  // Two independent guards, because they stop different things. Hysteresis
  // stops a voltage sitting exactly on a threshold from flickering between two
  // levels. The hold stops a genuine but BRIEF excursion -- a WiFi TX burst, a
  // backlight step, the sag as the screen wakes -- from moving the display at
  // all. Neither alone is enough: hysteresis lets a long sag through, and a
  // hold alone would still flicker once it expired.
  int8_t want;
  if (pc < 0) {
    want = -1;                                  // no cell wired
  } else {
    // Fall only after crossing the threshold by BATT_HYST_MV; rise on touch.
    int8_t cur = battLevel > 0 ? battLevel : 0;
    uint32_t l3 = BATT_L3_MV - (cur >= 3 ? BATT_HYST_MV : 0);
    uint32_t l2 = BATT_L2_MV - (cur >= 2 ? BATT_HYST_MV : 0);
    want = (mv >= l3) ? 3 : (mv >= l2) ? 2 : 1;
  }
  if (want != battLevelCand) {                  // a new candidate: restart the
    battLevelCand  = want;                      // clock rather than accumulate
    battLevelSince = now ? now : 1UL;
  }
  bool settled = battLevelSince &&
                 (now - battLevelSince) >= BATT_LEVEL_HOLD_MS;
  // First reading after boot shows immediately -- making someone stare at a
  // blank corner for 45 s to prove a point would be its own kind of wrong.
  if (battLevel < 0 || settled) {
    if (want != battLevel) { battLevel = want; needRender = true; }
  }

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
// Three segments, filled from the left, coloured by how many are lit: 3 green,
// 2 amber, 1 red. No number -- see the note at BATT_L3_MV in board_s3.h for why
// a percentage was actively misleading here.
static void drawBatteryChip(int16_t right, int16_t y) {
  if (battLevel < 0) {
    if (!net.connected()) return;
    char buf[12];
    snprintf(buf, sizeof(buf), "%ddBm", (int)net.rssi());
    gfx->setTextSize(1);
    gfx->setTextColor(C_DIM);
    gfx->setCursor(right - (int16_t)strlen(buf) * GLYPH_W, y + 3);
    gfx->print(buf);
    return;
  }
  const int16_t segW = 9, segH = 12, gap = 3;
  const int16_t totalW = segW * 3 + gap * 2;
  int16_t bx = right - totalW;
  int16_t by = y + 2;

  // Charging outranks the low-battery colours: a nearly flat cell that is ON
  // the charger is good news, and painting it red would say the opposite.
  uint16_t col = battCharging ? C_OK
                 : battLevel >= 3 ? C_DONE
                 : battLevel == 2 ? C_WAIT : C_ERROR;

  for (int8_t i = 0; i < 3; i++) {
    int16_t sx = bx + i * (segW + gap);
    if (i < battLevel) gfx->fillRect(sx, by, segW, segH, col);
    else               gfx->drawRect(sx, by, segW, segH, C_DIMMER);
  }
  // The bolt sits ON the segments in the background colour, so it reads at
  // three lit segments and at one alike -- drawn in the accent colour it would
  // vanish into a filled block.
  if (battCharging) drawBolt(bx + totalW / 2 - 2, by + 2, C_BG);
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

// -----------------------------------------------------------------------------
// Menu rendering
// -----------------------------------------------------------------------------
// The five icons are drawn from primitives rather than stored as bitmaps. At
// 34-46 px a glyph from the built-in font is unrecognisable and a bitmap costs
// flash and a second place to keep the design; four rects and a circle each is
// cheaper than either and scales with the tile.

static void iconConductor(int16_t cx, int16_t cy, int16_t s, uint16_t col) {
  // The frame itself: four stacked rows, the top one long (the name) and the
  // bottom one short (the fleet strip) -- the shape of what this mode shows.
  int16_t w = s * 3 / 4, h = s * 5 / 8, x = cx - w / 2, y = cy - h / 2;
  int16_t lh = h / 4;
  gfx->fillRect(x, y,              w,         lh - 1, col);
  gfx->fillRect(x, y + lh,         w * 5 / 8, lh - 1, col);
  gfx->fillRect(x, y + lh * 2,     w * 7 / 8, lh - 1, col);
  gfx->fillRect(x, y + lh * 3,     w / 2,     lh - 1, col);
}

static void iconSettings(int16_t cx, int16_t cy, int16_t s, uint16_t col) {
  // Three sliders. A gear is the conventional icon and is illegible at this
  // size; sliders read instantly and say "things you can change" rather than
  // "machinery".
  int16_t w = s * 3 / 4, x = cx - w / 2;
  const int16_t knob[3] = {(int16_t)(w * 2 / 3), (int16_t)(w / 3),
                           (int16_t)(w * 3 / 4)};
  for (int i = 0; i < 3; i++) {
    int16_t y = cy - s / 4 + i * (s / 4);
    gfx->fillRect(x, y, w, 2, col);
    gfx->fillCircle(x + knob[i], y + 1, s / 12 + 1, col);
  }
}

static void iconAbout(int16_t cx, int16_t cy, int16_t s, uint16_t col) {
  int16_t r = s * 3 / 8;
  gfx->drawCircle(cx, cy, r, col);
  gfx->drawCircle(cx, cy, r - 1, col);
  gfx->fillRect(cx - 1, cy - r / 2 - 1, 3, 3, col);          // the dot
  gfx->fillRect(cx - 1, cy - r / 6,     3, r * 5 / 6, col);  // the stem
}

static void iconWifi(int16_t cx, int16_t cy, int16_t s, uint16_t col) {
  // Ascending bars rather than arcs: the same shape the status bar already uses
  // for signal strength, so the two are obviously about the same thing.
  int16_t bw = s / 7, gap = s / 5, x = cx - (gap * 3) / 2, base = cy + s / 3;
  for (int i = 0; i < 4; i++) {
    int16_t h = (s / 5) + i * (s / 6);
    gfx->fillRect(x + i * gap, base - h, bw, h, col);
  }
}

static void iconSleep(int16_t cx, int16_t cy, int16_t s, uint16_t col) {
  // A crescent: a filled disc with a background-coloured disc bitten out of it.
  // Works because the tile is always drawn on C_BG.
  int16_t r = s * 3 / 8;
  gfx->fillCircle(cx, cy, r, col);
  gfx->fillCircle(cx + r / 2, cy - r / 3, r * 7 / 8, C_BG);
}

static void iconGame(int16_t cx, int16_t cy, int16_t s, uint16_t col) {
  // Mate itself, not a controller glyph: the mascot IS the character you play,
  // and a tile that shows the thing you will be moving needs no caption. Drawn
  // from the same parts as the sprite -- body, two eye slots, two legs -- so the
  // tile and the game agree at a glance.
  int16_t w = s * 3 / 4, h = s * 5 / 8;
  int16_t x = cx - w / 2, y = cy - h / 2 - s / 8;
  gfx->fillRoundRect(x, y, w, h, 3, col);
  int16_t ew = w / 5, eh = h / 3;
  gfx->fillRect(x + w / 6, y + h / 5, ew, eh, C_BG);
  gfx->fillRect(x + w - w / 6 - ew, y + h / 5, ew, eh, C_BG);
  int16_t lw = w / 5, lh = s / 4;
  gfx->fillRect(x + w / 5, y + h, lw, lh, col);
  gfx->fillRect(x + w - w / 5 - lw, y + h, lw, lh, col);
}

struct MenuDef {
  const char *label;
  void (*icon)(int16_t, int16_t, int16_t, uint16_t);
  uint16_t colour;
};
// Colours are the palette's, reused for their existing meanings: the triage view
// in the "working" blue it spends most of its life showing, sleep in idle grey,
// and the two that change the device in white so they do not read as a status.
static const MenuDef MENU[MENU_COUNT] = {
  {"CONDUCTOR", iconConductor, C_WORK},
  {"SETTINGS",  iconSettings,  C_TEXT},
  {"ABOUT",     iconAbout,     C_TEXT},
  {"WI-FI",     iconWifi,      C_DONE},
  {"SHIP IT",   iconGame,      GH_MATE},
  {"SLEEP",     iconSleep,     C_IDLE},
};

ShipIt game;

// Centre text of the given size at x.
static void drawCentred(int16_t cx, int16_t y, uint8_t size, const char *s,
                        uint16_t col) {
  int16_t w = (int16_t)strlen(s) * GLYPH_W * size;
  gfx->setTextSize(size);
  gfx->setTextColor(col);
  gfx->setCursor(cx - w / 2, y);
  gfx->print(s);
}

static void drawMenu() {
  for (uint8_t i = 0; i < MENU_COUNT; i++) {
    bool     sel = (i == menuIdx);
    int16_t  cx  = MENU_CX0 + i * MENU_PITCH;
    int16_t  s   = sel ? MENU_ICON_SEL : MENU_ICON;
    uint16_t col = sel ? MENU[i].colour : C_DIMMER;
    if (sel) {
      // A rounded plate behind the selected tile. The enlargement alone reads
      // as selection on a still screen but not while you are stepping through,
      // where the eye tracks the box rather than the size.
      gfx->drawRoundRect(cx - s / 2 - 4, MENU_CY - s / 2 - 4,
                         s + 8, s + 8, 6, col);
    }
    MENU[i].icon(cx, MENU_CY, s, col);
  }
  // The label wants to sit under its tile, but "CONDUCTOR" at size 2 is 108 px
  // and the outer tiles are 44 px from the edge -- so clamp the centre to keep
  // the whole word on the glass rather than truncate it.
  const char *label = MENU[menuIdx].label;
  int16_t     lw    = (int16_t)strlen(label) * GLYPH_W * 2;
  int16_t     cx    = MENU_CX0 + menuIdx * MENU_PITCH;
  int16_t     lo    = PAD_X + lw / 2, hi = SCREEN_W - PAD_X - lw / 2;
  if (cx < lo) cx = lo;
  if (cx > hi) cx = hi;
  drawCentred(cx, MENU_LABEL_Y, 2, label, MENU[menuIdx].colour);
}

// One settings row: label left, value right in the accent colour.
static void drawSetRow(uint8_t row, int16_t y, bool sel) {
  const char *label = "";
  const char *value = "";
  char        buf[20];
  uint16_t    vcol = C_TEXT;

  switch (row) {
    case SR_SLEEP:
      label = "Sleep screen";
      value = cfg.hibLabel();
      vcol  = cfg.hibernates() ? C_DONE : C_DIM;
      break;
    case SR_BRIGHT: {
      label = "Brightness";
      // A five-block meter rather than a number: 120 means nothing, four blocks
      // out of five means something.
      char *p = buf;
      for (uint8_t i = 0; i < BL_STEPS; i++) *p++ = i <= cfg.blIdx() ? '#' : '.';
      *p = 0;
      value = buf;
      break;
    }
    case SR_LED:
      label = "Alert LED";
      value = cfg.ledLabel();
      vcol  = cfg.ledBright() ? C_WAIT : C_DIM;
      break;
    case SR_SOUND:
      label = "Mac sound";
      // Named for where it happens. "Sound: on" on a device with no speaker
      // would be a promise the hardware cannot keep, and the first thing anyone
      // would do is turn it on and wait for a beep that never comes.
      value = cfg.sound() ? "on" : "off";
      vcol  = cfg.sound() ? C_DONE : C_DIM;
      break;
    case SR_FLIP:
      label = "Flip screen";
      if (flipNeedsRestart) {
        value = "restart";
        vcol  = C_WAIT;
      } else {
        value = cfg.flipped() ? "on" : "off";
        vcol  = cfg.flipped() ? C_DONE : C_DIM;
      }
      break;
    case SR_RESET:
      label = "Factory reset";
      if (resetArmedMs) { value = "HOLD GO"; vcol = C_ERROR; }
      else              { value = "\x10";    vcol = C_DIM; }   // a right arrow
      break;
  }

  if (sel) gfx->fillRect(0, y, SCREEN_W, PAGE_ROW_H - 2, C_DIMMER);
  gfx->setTextSize(2);
  gfx->setTextColor(sel ? C_TEXT : C_DIM);
  gfx->setCursor(PAD_X, y + (PAGE_ROW_H - 2 - GLYPH_H * 2) / 2);
  gfx->print(label);
  int16_t vw = (int16_t)strlen(value) * GLYPH_W * 2;
  gfx->setTextColor(vcol);
  gfx->setCursor(SCREEN_W - PAD_X - vw, y + (PAGE_ROW_H - 2 - GLYPH_H * 2) / 2);
  gfx->print(value);
}

static void drawSettingsPage() {
  for (uint8_t i = 0; i < PAGE_ROWS_VIS && (pageTop + i) < SR_COUNT; i++) {
    uint8_t row = pageTop + i;
    drawSetRow(row, PAGE_ROW_Y + i * PAGE_ROW_H, row == pageIdx);
  }
  // A scrollbar, because otherwise there is nothing at all to say that a sixth
  // row exists -- the list simply looks complete.
  if (SR_COUNT > PAGE_ROWS_VIS) {
    int16_t trackY = PAGE_ROW_Y, trackH = PAGE_ROWS_VIS * PAGE_ROW_H - 2;
    int16_t thumbH = trackH * PAGE_ROWS_VIS / SR_COUNT;
    int16_t thumbY = trackY + (trackH - thumbH) * pageTop /
                     (SR_COUNT - PAGE_ROWS_VIS);
    gfx->fillRect(SCREEN_W - 3, trackY, 2, trackH, C_DIMMER);
    gfx->fillRect(SCREEN_W - 3, thumbY, 2, thumbH, C_DIM);
  }
  gfx->setTextSize(1);
  gfx->setTextColor(resetArmedMs ? C_ERROR : C_DIM);
  gfx->setCursor(PAD_X, MENU_HINT_Y);
  gfx->print(resetArmedMs ? "hold GO to wipe wifi + token + settings"
                          : "PREV/NEXT row   GO change   4th back");
}

// About: a readout, at size 1, because these are numbers you lean in for and
// there are more of them than five. This is the serial `?` output, on the glass
// -- which is the only place it can be read on a cordless device with no console
// attached, and it is how you tell a flat cell from a wrong BATT_DIVIDER.
static void drawAboutPage() {
  char rssiBuf[16], battBuf[40], sleepBuf[28];

  if (net.connected()) snprintf(rssiBuf, sizeof(rssiBuf), "%d dBm", net.rssi());
  else                 snprintf(rssiBuf, sizeof(rssiBuf), "--");

#if PIN_BATT_ADC >= 0
  if (battPercent < 0)
    snprintf(battBuf, sizeof(battBuf), "no cell (%u mV raw)",
             (unsigned)battRawMv);
  else
    snprintf(battBuf, sizeof(battBuf), "%d/3  %d%%  %u mV  %s", (int)battLevel,
             battPercent, (unsigned)battMv,
             battCharging ? "charging" : "on battery");
#else
  snprintf(battBuf, sizeof(battBuf), "gauge compiled out");
#endif

  snprintf(sleepBuf, sizeof(sleepBuf), "screen %s  led %s",
           cfg.hibLabel(), cfg.ledLabel());

  const char *k[] = {"link", "wifi", "batt", "boot", "sleep", "fw"};
  const char *v[] = {
      net.statusText(),
      rssiBuf,
      battBuf,
      wakeCause == ESP_SLEEP_WAKEUP_EXT1 ? "woke from sleep" : "power-on / reset",
      sleepBuf,
      FW_VERSION,
  };

  gfx->setTextSize(1);
  int16_t y = PAGE_INFO_Y;
  for (uint8_t i = 0; i < 6; i++) {
    gfx->setTextColor(C_DIM);
    gfx->setCursor(PAD_X, y);
    gfx->print(k[i]);
    gfx->setTextColor(C_TEXT);
    gfx->setCursor(PAD_X + 6 * GLYPH_W, y);
    gfx->print(v[i]);
    y += PAGE_INFO_LH;
  }
  gfx->setTextColor(C_DIM);
  gfx->setCursor(PAD_X, MENU_HINT_Y);
  gfx->print("4th button: back");
}

// The gamepad face. Drawn once per state change, never per frame: this screen
// is a still image and a full flush is 110 KB of SPI.
static void drawPad() {
  drawCentred(SCREEN_W / 2, 16, 2, "CONTROLLER", C_TEXT);
  gfx->setTextSize(1);
  gfx->setTextColor(C_DIM);
  const char *sub = "the Mac has the buttons";
  gfx->setCursor(SCREEN_W / 2 - (int16_t)strlen(sub) * GLYPH_W / 2, 40);
  gfx->print(sub);

  // The four keys in their real physical arrangement: the 4th sits alone above
  // the row of three, the way it does on the board.
  struct Cap { int16_t x, y; const char *label; uint8_t bit; };
  static const Cap CAPS[4] = {
    { 64, 60, "4th", 3 },
    { 64, 108, "PREV", 0 }, { 136, 108, "GO", 1 }, { 208, 108, "NEXT", 2 },
  };
  for (uint8_t i = 0; i < 4; i++) {
    bool down = padHeld & (1 << CAPS[i].bit);
    int16_t w = 60, h = 40;
    int16_t x = CAPS[i].x, y = CAPS[i].y;
    if (down) gfx->fillRoundRect(x, y, w, h, 7, C_WORK);
    else      gfx->drawRoundRect(x, y, w, h, 7, C_DIMMER);
    drawCentred(x + w / 2, y + h / 2 - 4, 1, CAPS[i].label, down ? C_BG : C_DIM);
  }
  gfx->setTextColor(C_DIMMER);
  const char *out = "hold 4th to leave";
  gfx->setCursor(SCREEN_W / 2 - (int16_t)strlen(out) * GLYPH_W / 2, 156);
  gfx->print(out);
}

static void render() {
  gfx->fillScreen(C_BG);
  bool blip = (long)(millis() - blipUntil) < 0;
  // SETUP outranks every firmware-local screen, including the menu. The portal's
  // credentials exist only on the glass, so nothing may cover them -- and the
  // portal can start from paths that do not go through the menu at all.
  if (net.state() == MateNet::SETUP) {
    drawSetup();
  } else if (uiMode == UI_MENU) {
    drawStatusBar();
    drawMenu();
    gfx->setTextSize(1);
    gfx->setTextColor(C_DIM);
    gfx->setCursor(PAD_X, MENU_HINT_Y);
    gfx->print("PREV/NEXT move   GO open   4th back");
    gfx->fillRect(0, ACCENT_Y, SCREEN_W, ACCENT_H,
                  blip ? C_TEXT : MENU[menuIdx].colour);
  } else if (uiMode == UI_PAD) {
    drawPad();
  } else if (uiMode == UI_GAME) {
    // No status bar and no accent bar: the game owns all 172 px. The battery
    // and the link are exactly the things you did not come here to look at.
    game.draw(gfx);
  } else if (uiMode == UI_PAGE) {
    drawStatusBar();
    if (pageId == PG_SETTINGS) drawSettingsPage();
    else                       drawAboutPage();
    gfx->fillRect(0, ACCENT_Y, SCREEN_W, ACCENT_H,
                  blip ? C_TEXT : (resetArmedMs ? C_ERROR : C_DIMMER));
  } else if (mirrorOn) {
    // The mirror is only ever opened by a button press on a linked device, so if
    // it is on, it is what was asked for. (It used to sit ahead of the SETUP
    // check for that reason; SETUP now wins, because a portal is only ever up
    // when the device is NOT linked, so the two cannot both be legitimate.)
    drawMirror();
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

    case 'G': {                            // G|1 / G|0 -- gamepad (controller)
                                           // mode. NOT 'X': that is X|WIPE in
                                           // handleConfigLine, which sees USB
                                           // serial lines first and answered
                                           // "refusing: send X|WIPE" instead --
                                           // so the mode never engaged, and a
                                           // sloppier value could have wiped
                                           // the config.
      char *bar = strchr(line, '|');
      if (!bar) break;
      bool on = (bar[1] == '1');
      if (on && uiMode != UI_PAD) {
        padReturnTo = (uiMode == UI_GAME) ? UI_CONDUCTOR : uiMode;
        uiMode = UI_PAD;
        padActive = true;
        padHeld = 0;
        padDirty = true;
        analogWrite(LCD_BL, PAD_BL_DUTY);    // nobody is looking at this screen
        requestRender();
      } else if (!on && uiMode == UI_PAD) {
        uiMode = padReturnTo;
        padActive = false;
        analogWrite(LCD_BL, cfg.blDuty());
        requestRender();
      }
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
        Serial.printf("batt  : level %d/3  %d%% (%u mV filt, %u raw) %s\n",
                      (int)battLevel, battPercent,
                      (unsigned)battMv, (unsigned)battRawMv,
                      battCharging ? "charging" : "on battery");
#else
      Serial.println("batt  : gauge compiled out");
#endif
      Serial.printf("boot  : %s\n",
                    wakeCause == ESP_SLEEP_WAKEUP_EXT1 ? "woke from power-off"
                                                       : "power-on / reset");
      // The firmware-local settings, so a device that is misbehaving can be
      // diagnosed without guessing at what someone set in the menu. `screen`
      // reporting "off" is the difference between a broken backlight and a
      // hibernate timeout doing its job.
      Serial.printf("ui    : %s  screen %s%s  bright %u/%u  led %s  flip %s\n",
                    uiMode == UI_CONDUCTOR ? "conductor"
                                           : uiMode == UI_MENU ? "menu" : "page",
                    cfg.hibLabel(), screenOn ? "" : " (asleep now)",
                    (unsigned)(cfg.blIdx() + 1), (unsigned)BL_STEPS,
                    cfg.ledLabel(), cfg.flipped() ? "on" : "off");
      Serial.printf("fw    : %s\n", FW_VERSION);
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

    case 'Y':                              // scan and list what the RADIO sees
      // A headless device that will not join has two very different problems --
      // the network is not there, or the password is wrong -- and from the
      // outside they look identical. This is the only way to tell them apart
      // without a serial console on the router. It also catches the trap that
      // this chip is 2.4 GHz only: an SSID the Mac joins happily can be
      // invisible here if the router moved it to 5 GHz.
      net.scanTo(Serial);
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

// Forward declarations: the button layer routes to whichever of the two owners
// of the buttons is currently in charge.
static void menuButton(char ev);
static void wakeScreen();
static void notePoke();

// EVERY button event funnels through here, and there are three things in front
// of the daemon now.
//
// 1. If the screen is hibernating, the press only wakes it and is SWALLOWED.
//    This is the phone convention, and the reason is specific rather than
//    conventional: GO raises a terminal window. A press aimed at a dark screen
//    is a press aimed at whatever tab happened to be selected, so obeying it
//    would occasionally yank you to the wrong session -- the exact thing this
//    device exists to stop.
// 2. It notes activity, which is what the hibernate timer counts from.
// 3. In MENU or PAGE mode the event is handled locally and never emitted, so
//    the daemon's queue cannot move while you are looking at settings.
static void onButton(char ev) {
  if (!screenOn) { wakeScreen(); return; }
  notePoke();
  if (uiMode == UI_CONDUCTOR) { emitBtn(ev); return; }
  menuButton(ev);
  blipUntil = millis() + BLIP_MS;
  requestRender();
}

static void pollNavBtn(Btn &b, char ev) {
  bool raw = (digitalRead(b.pin) == LOW);   // pull-up: LOW = pressed
  unsigned long now = millis();
  // The game reads these three pins directly, every step, because a platformer
  // needs "is it held down NOW" and this poller deals in events with a 400/200
  // ms auto-repeat. Track the level so that leaving the game does not deliver
  // the release of a press the menu never saw as an event.
  if (uiMode == UI_GAME) { b.pressed = raw; b.changeMs = now; b.longFired = true;
                           return; }
  if (raw != b.pressed && (now - b.changeMs) >= DEBOUNCE_MS) {
    b.pressed  = raw;
    b.changeMs = now;
    if (raw) {
      b.pressMs  = now;
      b.repeatMs = now;
      // The dark-screen swallow has to be latched for the whole PRESS, not just
      // its first event. onButton() decides by reading screenOn -- and sets it on
      // the way out -- so the guard is true only once, and a press held past
      // REPEAT_DELAY_MS would then auto-repeat into a screen it had just woken:
      // a press that meant "wake up" walking the daemon's selection, and with
      // FOLLOW on, raising a terminal by itself. Exactly what the swallow exists
      // to prevent. Read BEFORE the call, and re-armed on every press edge so a
      // spent hold cannot disable the next one's repeat.
      b.longFired = !screenOn;              // reused as "this press is spent"
      onButton(ev);
    }
  }
  // No auto-repeat outside CONDUCTOR. Five menu items and five settings rows
  // both wrap, so a repeat at 200 ms laps the list several times over and parks
  // the selection somewhere arbitrary -- on SLEEP, if you are unlucky.
  if (b.pressed && !b.longFired && uiMode == UI_CONDUCTOR &&
      (now - b.pressMs) >= REPEAT_DELAY_MS &&
      (now - b.repeatMs) >= REPEAT_MS) {
    b.repeatMs = now;
    onButton(ev);
  }
}

static void pollGoBtn(Btn &b) {
  bool raw = (digitalRead(b.pin) == LOW);
  unsigned long now = millis();
  if (uiMode == UI_GAME) { b.pressed = raw; b.changeMs = now; b.longFired = true;
                           return; }
  if (raw != b.pressed && (now - b.changeMs) >= DEBOUNCE_MS) {
    b.pressed  = raw;
    b.changeMs = now;
    if (raw) {
      b.pressMs   = now;
      b.longFired = false;
    } else if (!b.longFired) {
      onButton('G');                        // short press: focus + acknowledge
    }
  }
  if (b.pressed && !b.longFired && (now - b.pressMs) >= LONGPRESS_MS) {
    onButton('K');                          // acknowledge without focusing
    b.longFired = true;
  }
}

// -----------------------------------------------------------------------------
// Power off (deep sleep)
// -----------------------------------------------------------------------------
// "Off" here means DEEP SLEEP, not zero: the WS2812 has no shutdown pin and
// idles around 1 mA whenever the rail is up, which dwarfs the ~8 uA the S3
// itself draws asleep. That is roughly a month of standby on a 14500 -- off in
// every practical sense, but a physical switch in the battery lead is still the
// only true zero.
//
// Waking runs setup() again from the top: deep sleep does not resume, it
// reboots. Config lives in NVS so nothing is lost, but rejoining WiFi costs a
// few seconds.
static void powerOff() {
  // Tell the daemon to stop mirroring. Without this, holding the button while a
  // terminal view is open leaves the daemon polling that session's wrapper once
  // a second, forever, for a device that is asleep -- and the board wakes back
  // into a stale mirror shown as though it were live.
  if (mirrorOn) { emitBtn('M'); mirrorOn = false; }

  // The backlight may be at zero because hibernate turned it off, and the
  // goodbye screen below would then be a 900 ms pause at a dark panel: the whole
  // gesture invisible, which reads as "the hold did nothing".
  analogWrite(LCD_BL, cfg.blDuty());
  screenOn = true;

  // Say so while the backlight is still on, then hold it long enough to read.
  if (gfx) {
    gfx->fillScreen(C_BG);
    gfx->setTextSize(2);
    gfx->setTextColor(C_TEXT);
    gfx->setCursor(PAD_X, SCREEN_H / 2 - 16);
    gfx->print("powering off");
    gfx->setTextSize(1);
    gfx->setTextColor(C_DIM);
    gfx->setCursor(PAD_X, SCREEN_H / 2 + 8);
    gfx->print("press the 4th button to wake");
    if (buffered) canvas->flush();
  }
  // Leave POLITELY: a half-open socket leaves the daemon listing this device as
  // present until its own timeout notices.
  net.shutdown();
  rgbLedWrite(PIN_RGB, 0, 0, 0);
  delay(900);

  panel->displayOff();

  // The backlight needs BOTH a low level and a latch. Deep sleep releases every
  // GPIO, so an unheld pin floats and the panel can sit there lit -- burning
  // exactly the current this is meant to save. analogWrite drives it through
  // LEDC, which stops in sleep, so detach that first and drive the pad directly
  // before latching it.
  analogWrite(LCD_BL, 0);
  ledcDetach(LCD_BL);
  pinMode(LCD_BL, OUTPUT);
  digitalWrite(LCD_BL, LOW);
  gpio_hold_en((gpio_num_t)LCD_BL);
  gpio_deep_sleep_hold_en();

  // WAIT FOR RELEASE. The wake is level-triggered on LOW, so sleeping with the
  // button still down wakes the chip instantly and reads as "power off is
  // broken".
  while (digitalRead(PIN_BTN_MIRROR) == LOW) delay(10);
  delay(80);                                  // let the release settle

  // The internal pull-up must be re-armed through the RTC domain; the ordinary
  // GPIO pull-up does not survive into deep sleep, and a floating wake pin
  // would wake the device again immediately.
  rtc_gpio_pullup_en((gpio_num_t)PIN_BTN_MIRROR);
  rtc_gpio_pulldown_dis((gpio_num_t)PIN_BTN_MIRROR);
  esp_sleep_enable_ext1_wakeup(1ULL << PIN_BTN_MIRROR, ESP_EXT1_WAKEUP_ANY_LOW);
  esp_deep_sleep_start();                     // never returns
}

// -----------------------------------------------------------------------------
// Screen sleep (hibernate)
// -----------------------------------------------------------------------------

// Does anything in the fleet want you RIGHT NOW? Answered from the frame the
// device already has, so hibernate needs nothing from the daemon and no protocol
// change. Three signals, and the third is the authoritative one:
//
//   * the shown session's alert is unacknowledged (the name row is flashing);
//   * ANY fleet letter is lowercase, which is how the daemon marks an
//     unacknowledged alert on a tab you are not looking at;
//   * the LED is playing a LOOPING pattern -- the daemon drives that from
//     exactly "worst unacknowledged alert", so if it loops, something needs you.
//
// Note what is deliberately NOT here. A `working` session does not count: a
// fleet grinding away for an hour with nothing to say is precisely the case this
// setting exists for. Neither does NO LINK -- a dead daemon must not be able to
// hold the backlight on until the cell is flat, which is what would happen the
// moment you carried the device out of WiFi range.
static bool needsAttention() {
  if (haveFrame && !linkLost) {
    if (frameFlash) return true;
    for (const char *c = frameRow[3]; *c; c++)
      if (*c >= 'a' && *c <= 'z') return true;
  }
  return ledActive && ledLoop;
}

static void notePoke() { lastPokeMs = millis(); }

static void wakeScreen() {
  if (screenOn) return;
  screenOn = true;
  analogWrite(LCD_BL, cfg.blDuty());
  notePoke();
  requestRender();                            // the frame may have moved on
}

static void sleepScreen() {
  if (!screenOn) return;
  screenOn = false;
  analogWrite(LCD_BL, 0);
  // If the terminal mirror was open, close it. Leaving it open would have the
  // daemon polling a wrapper once a second to render rows onto a dark panel --
  // the one place where hibernating saves nothing and costs the Mac work.
  //
  // mirrorOn is cleared HERE rather than waiting for the daemon's M|OFF, because
  // emitLine() is fire-and-forget: with the link down the request reaches nobody,
  // and the flag would still be set on wake, showing a frozen terminal as though
  // it were live.
  if (mirrorOn) { emitBtn('M'); mirrorOn = false; }
}

// Called every loop. Kept in one place so the two directions cannot disagree.
static void pollHibernate() {
  if (!cfg.hibernates()) { wakeScreen(); return; }
  // Never over the WiFi setup portal. Its AP name, its per-start password and
  // its URL exist NOWHERE else -- not in the daemon, not over serial, not even
  // in NVS, since the password is regenerated on every portal start. Blanking
  // that screen mid-setup strands you with no way back to the credentials.
  if (net.state() == MateNet::SETUP) { wakeScreen(); notePoke(); return; }
  if (needsAttention()) {
    // An alert wakes the screen AND re-arms the timer -- note the notePoke()
    // outside wakeScreen(), which early-returns when the screen is already lit.
    // Without it the delay would be measured from the last button press, so
    // acknowledging a long-standing alert would blank the screen in your face.
    wakeScreen();
    notePoke();
    return;
  }
  if (screenOn && (millis() - lastPokeMs) >= cfg.hibernateMs()) sleepScreen();
}

// -----------------------------------------------------------------------------
// Menu input
// -----------------------------------------------------------------------------
// Keep the selected row inside the visible window, scrolling by the minimum
// needed. Wrapping from the last row to the first jumps the window back to the
// top, which is what "the list wrapped" should look like.
static void scrollToSelection() {
  if (pageIdx < pageTop) pageTop = pageIdx;
  else if (pageIdx >= pageTop + PAGE_ROWS_VIS)
    pageTop = (uint8_t)(pageIdx - PAGE_ROWS_VIS + 1);
}

// The Mac's alert sound is a DAEMON setting reached from the device, so it is
// the one preference here that has to cross the link. Sent on every change and
// again on every (re)connect, because the daemon keeps no per-device state and
// would otherwise forget it the moment the link dropped.
static void sendSoundPref() {
  char buf[12];
  snprintf(buf, sizeof(buf), "O|SND|%d", cfg.sound() ? 1 : 0);
  emitLine(buf);
}

// The game's noises, made by the Mac. This board has no DAC, no speaker and a
// backlight circuit with no inductor to abuse, so the link is the only speaker
// there is -- which does mean a run played with the daemon down is silent, and
// nothing can be done about that from here.
//
// Gated on the device's own SOUND setting as well as the daemon's: the daemon
// would ignore these anyway with sound off, but a jump is several frames of
// link traffic and there is no reason to spend it on something already muted.
static void sendSfx(char code) {
  if (!cfg.sound()) return;
  char buf[10];
  snprintf(buf, sizeof(buf), "O|SFX|%c", code);
  emitLine(buf);
}

// PREV/NEXT/GO while a firmware-local screen is up. Never reaches the daemon.
static void menuButton(char ev) {
  if (uiMode == UI_MENU) {
    if (ev == 'P') menuIdx = (uint8_t)((menuIdx + MENU_COUNT - 1) % MENU_COUNT);
    else if (ev == 'N') menuIdx = (uint8_t)((menuIdx + 1) % MENU_COUNT);
    // 'K' is GO's long press. On the strip it means the same as 'G': there is no
    // long-press gesture here, and LONGPRESS_MS is 500 ms, which is easy to
    // overshoot -- a press that opened nothing because you held it a beat too
    // long reads as a dead button. On the SETTINGS page 'K' keeps its own
    // meaning (confirm), which is why this is not a blanket alias.
    else if (ev == 'G' || ev == 'K') {
      switch (menuIdx) {
        case MI_CONDUCTOR: uiMode = UI_CONDUCTOR; break;
        case MI_SETTINGS:  uiMode = UI_PAGE; pageId = PG_SETTINGS; pageIdx = 0;
                           pageTop = 0; resetArmedMs = 0; break;
        case MI_ABOUT:     uiMode = UI_PAGE; pageId = PG_ABOUT; break;
        case MI_GAME:      uiMode = UI_GAME; game.open(); break;
        case MI_WIFI:      // The portal takes the screen over on its own, via
                           // net.state() == SETUP in render().
                           uiMode = UI_CONDUCTOR;
                           net.startPortalNow();
                           break;
        case MI_SLEEP:     cfg.flush();      // the deferred commit will not run
                           powerOff();       // never returns
                           break;
      }
    }
    return;
  }

  // A page. About has nothing to operate, so only the 4th button (handled by its
  // own poller) does anything there.
  if (pageId != PG_SETTINGS) return;

  if (ev == 'P') { pageIdx = (uint8_t)((pageIdx + SR_COUNT - 1) % SR_COUNT);
                   scrollToSelection(); resetArmedMs = 0; return; }
  if (ev == 'N') { pageIdx = (uint8_t)((pageIdx + 1) % SR_COUNT);
                   scrollToSelection(); resetArmedMs = 0; return; }

  // 'K' is GO's long press. On the three rows that only cycle a value it means
  // the same as 'G': LONGPRESS_MS is 500 ms, easy to overshoot, and a press that
  // changed nothing because you held it a beat too long reads as a dead row. On
  // FLIP and RESET the long press keeps its own meaning -- it is the confirming
  // gesture -- so this is not a blanket alias.
  if (ev == 'K' && pageIdx != SR_FLIP && pageIdx != SR_RESET) ev = 'G';

  if (ev == 'G') {                            // short press: change the value
    switch (pageIdx) {
      case SR_SLEEP:  cfg.cycleHib(); break;
      case SR_BRIGHT: cfg.cycleBl();
                      analogWrite(LCD_BL, cfg.blDuty());   // apply immediately;
                      break;                               // a preview you can
                                                           // see is the point
      case SR_LED:    cfg.cycleLed();
                      // Take effect on whatever is playing RIGHT NOW, not at the
                      // next alert -- picking "off" while a 7 Hz strobe is going
                      // is exactly when you reach for this setting.
                      ledApplyCap();
                      break;
      case SR_SOUND:  cfg.toggleSound();
                      // Tell the daemon at once: the sound plays there, so a
                      // toggle that only reached NVS would do nothing until the
                      // next reconnect and read as a dead row.
                      sendSoundPref();
                      break;
      case SR_FLIP:   cfg.toggleFlip();
                      flipNeedsRestart = (cfg.flipped() != flipAtBoot);
                      break;
      // Same sentinel trap as MateSettings::touch(): `| 1UL` would stamp one
      // millisecond into the future on any even millis(), the unsigned subtract
      // in loop() would underflow, and the arm would disarm itself in the same
      // loop iteration -- so half the time the confirmation never appeared and
      // the row looked broken.
      case SR_RESET:  { unsigned long t = millis(); resetArmedMs = t ? t : 1UL; }
                      break;
    }
    return;
  }

  if (ev == 'K') {                            // long press: confirm / commit
    if (pageIdx == SR_RESET && resetArmedMs) {
      cfg.factoryResetAll();
      ESP.restart();                          // never returns
    }
    if (pageIdx == SR_FLIP && flipNeedsRestart) {
      cfg.flush();
      ESP.restart();                          // never returns
    }
  }
}

// The 4th button. Three gestures on one switch, and the ordering of the checks
// is what keeps them from colliding:
//
//   tap          in CONDUCTOR -> toggle the terminal mirror (what it always did)
//                in MENU/PAGE -> back out one level
//   double-tap   anywhere     -> open the menu
//   hold 2 s     anywhere     -> long sleep (deep sleep; a tap wakes the board)
//
// The tap is DEFERRED by DBLCLICK_MS, because a first tap is not yet knowably a
// single one. That is the cost of putting three gestures here, and it is paid by
// the least latency-sensitive of the three: the mirror already waits a daemon
// round-trip, so 300 ms vanishes into it. The alternative -- acting immediately
// and undoing it on the second tap -- would emit B|M twice and have the daemon
// open and close a terminal view nobody asked for.
//
// Nothing is emitted on the press edge, which is what makes the hold possible at
// all: firing on press would toggle the mirror on the way to powering off.
// Still no auto-repeat -- holding must not flap the view.
static bool          clickPending = false;
static unsigned long clickMs      = 0;

static void fourthTap() {
  // In the game the 4th button steps OUT one level at a time -- play back to the
  // start screen, start screen back to the menu -- so a mis-tap mid-level costs
  // you the run but not the screen you were on.
  if (uiMode == UI_GAME)      { if (game.fourth()) uiMode = UI_MENU;
                                requestRender(); return; }
  if (uiMode == UI_MENU)      { uiMode = UI_CONDUCTOR; requestRender(); return; }
  if (uiMode == UI_PAGE)      { uiMode = UI_MENU; resetArmedMs = 0;
                                requestRender(); return; }
  onButton('M');                              // CONDUCTOR: toggle the mirror
}

static void fourthDoubleTap() {
  // NOT over the WiFi setup portal. That screen shows an AP name and a password
  // that is regenerated on every portal start, and the glass is the only place
  // either can be read -- covering it with a menu would strand you mid-setup
  // with no way back to the credentials.
  if (net.state() == MateNet::SETUP) return;
  if (mirrorOn) emitBtn('M');                 // close it; the menu owns the
                                              // screen from here
  uiMode  = UI_MENU;
  menuIdx = MI_CONDUCTOR;                     // always land on the way back out
  requestRender();
}

static void pollTapBtn(Btn &b) {
  bool raw = (digitalRead(b.pin) == LOW);
  unsigned long now = millis();
  if (raw != b.pressed && (now - b.changeMs) >= DEBOUNCE_MS) {
    b.pressed  = raw;
    b.changeMs = now;
    if (raw) {
      b.pressMs   = now;
      b.longFired = false;
    } else if (!b.longFired) {
      // A dark screen: the tap only wakes, and must not also arm a click --
      // otherwise waking the device would open the mirror or the menu.
      if (!screenOn) { wakeScreen(); return; }
      notePoke();
      if (clickPending && (now - clickMs) <= DBLCLICK_MS) {
        clickPending = false;
        fourthDoubleTap();
      } else {
        clickPending = true;
        clickMs      = now;
      }
    }
  }
  // The hold works whatever the screen is doing: reaching for the device in the
  // dark to switch it off should not need two goes.
  if (b.pressed && !b.longFired && (now - b.pressMs) >= POWEROFF_HOLD_MS) {
    b.longFired  = true;                      // swallow the release
    // NOT during a game. The 4th button is the only way out of a level, and a
    // thumb that rests on it for two seconds while thinking about a jump would
    // switch the device off mid-run. Holding it leaves the game instead.
    if (uiMode == UI_GAME) { uiMode = UI_MENU; requestRender(); return; }
    cfg.flush();                              // deferred commit will not run
    powerOff();                               // never returns
  }
  // Note what is NOT here: cancelling a pending single tap. A tap immediately
  // followed by a hold lets the tap land first -- it has to, since it fires at
  // 300 ms and the hold is only knowable at 2000 ms, and deferring the tap that
  // long would make the mirror unusable. So tap-then-hold opens the mirror and
  // then powers off. Harmless, because powerOff() closes the mirror on its way
  // out; before it did, that sequence left the daemon polling a wrapper forever
  // for a device that was asleep.
}

// The deferred single tap fires from the loop once the double-click window has
// closed with no second tap.
static void pollPendingClick() {
  if (!clickPending) return;
  if ((millis() - clickMs) <= DBLCLICK_MS) return;
  clickPending = false;
  fourthTap();
}

// Controller mode reads the pins itself, every loop, and sends edges. No
// debounce beyond 8 ms, no auto-repeat, no deferred tap: the page needs to know
// what is held down THIS frame, and every one of those mechanisms exists to
// answer a different question.
static void pollPadButtons() {
  static const uint8_t PINS[4] = {PIN_BTN_PREV, PIN_BTN_GO, PIN_BTN_NEXT,
                                  PIN_BTN_MIRROR};
  static const char    CODE[4] = {'P', 'G', 'N', 'M'};
  static bool          was[4]  = {false, false, false, false};
  static unsigned long chg[4]  = {0, 0, 0, 0};
  static unsigned long mirrorDownMs = 0;

  unsigned long now = millis();
  for (uint8_t i = 0; i < 4; i++) {
    bool raw = (digitalRead(PINS[i]) == LOW);
    if (raw == was[i] || (now - chg[i]) < PAD_DEBOUNCE_MS) continue;
    was[i] = raw;
    chg[i] = now;
    char buf[6] = {'B', '|', raw ? '+' : '-', CODE[i], 0, 0};
    emitLine(buf);
    if (raw) padHeld |= (1 << i); else padHeld &= ~(1 << i);
    padDirty = true;
    notePoke();          // playing is not idling: do not blank mid-game
    requestRender();                        // only on a change: the pad face is
                                            // otherwise a still image
    if (i == 3) mirrorDownMs = raw ? now : 0;
  }
  // The way OUT if the Mac never says X|0 -- a crashed tab, a daemon killed
  // mid-game. Hold the 4th button for two seconds. It does NOT power off here:
  // that is the same thumb-resting hazard the game has, and a controller you
  // cannot leave is worse than one you have to hold a button to leave.
  if (mirrorDownMs && (now - mirrorDownMs) >= POWEROFF_HOLD_MS) {
    mirrorDownMs = 0;
    was[3] = false;
    emitLine("B|-M");                       // never leave the page holding it
    uiMode = padReturnTo;
    padActive = false;
    padHeld = 0;
    analogWrite(LCD_BL, cfg.blDuty());
    requestRender();
  }
}

static void pollButtons() {
  if (uiMode == UI_PAD) { pollPadButtons(); return; }
  pollNavBtn(prevBtn, 'P');
  pollGoBtn(goBtn);
  pollNavBtn(nextBtn, 'N');
  pollTapBtn(mirrorBtn);
  pollGoBtn(bootBtn);                       // BOOT is a second GO, so a board
                                            // with nothing soldered still works
  pollPendingClick();
}

// -----------------------------------------------------------------------------
// Setup / loop
// -----------------------------------------------------------------------------

void setup() {
  // FIRST, before anything touches the backlight: release the pad latch that
  // powerOff() set. Deep sleep keeps held GPIOs held THROUGH the reboot, so
  // skipping this leaves the backlight pinned low and the board wakes to a
  // black screen -- indistinguishable from the dead-panel failure that a wrong
  // LCD_BL causes, and just as slow to diagnose.
  wakeCause = esp_sleep_get_wakeup_cause();
  if (wakeCause != ESP_SLEEP_WAKEUP_UNDEFINED) {
    gpio_hold_dis((gpio_num_t)LCD_BL);
    gpio_deep_sleep_hold_dis();
    rtc_gpio_deinit((gpio_num_t)PIN_BTN_MIRROR);   // hand the pin back to GPIO
    // The wake is level-triggered on LOW, so the chip resumed the instant the
    // pin went down and the button is STILL HELD as this runs. Seed the 4th
    // button as already-pressed and already-spent, so the release that follows
    // is consumed rather than read as a fresh tap -- and so a firm two-second
    // press to wake the board does not immediately power it off again.
    mirrorBtn.pressed   = true;
    mirrorBtn.changeMs  = millis();
    mirrorBtn.pressMs   = millis();
    mirrorBtn.longFired = true;
  }

  pinMode(PIN_BTN_PREV, INPUT_PULLUP);
  pinMode(PIN_BTN_GO,   INPUT_PULLUP);
  pinMode(PIN_BTN_NEXT, INPUT_PULLUP);
  pinMode(PIN_BTN_MIRROR, INPUT_PULLUP);
  pinMode(PIN_BTN_BOOT, INPUT_PULLUP);
  rgbLedWrite(PIN_RGB, 0, 0, 0);            // LED dark at boot
  lastRxMs   = millis();                    // seed the liveness watchdog
  lastPokeMs = millis();                    // ...and the hibernate timer, so a
                                            // fresh boot gets its full delay

  Serial.begin(SERIAL_BAUD);

  // Settings before anything reads them: the backlight duty, the LED cap and the
  // rotation all come out of NVS.
  cfg.begin();
  flipAtBoot = cfg.flipped();

  // Backlight on a PWM channel so it can be dimmed (and so a battery build can
  // trade brightness for runtime).
  pinMode(LCD_BL, OUTPUT);
  analogWrite(LCD_BL, cfg.blDuty());

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
  // Rotation is applied AFTER begin(), which is the only order that works:
  // Arduino_TFT::begin() ends by applying the rotation the constructor was
  // given, so setting it earlier would be overwritten. Done here, once, before
  // a single pixel is drawn -- never on a live display, because a half-rotated
  // panel is the failure this file already warns about twice.
  // 1 and 3 are the two landscape orientations; the driver holds a column
  // offset for each pair, so the 34-px offset this 172x320 panel needs follows
  // the flip on its own.
  panel->setRotation(cfg.flipped() ? 3 : LCD_ROTATION);
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
  if (Serial) { Serial.println("H"); sendSoundPref(); }
  game.sfx = sendSfx;                       // the Mac is the game's only speaker
}

void loop() {
  static MateNet::State lastNetState = MateNet::OFF;

  // Hold off reconnection attempts while a firmware-local screen is up. Both
  // the mDNS browse and the TCP connect BLOCK this loop, so with no daemon to
  // find they stop the button poll for most of every second -- a press and its
  // release can land entirely inside one and be dropped, which made the menu
  // close to unusable exactly when you most need it (no link, so you have come
  // to the menu to fix something). The link is not wanted while you are in
  // Settings anyway, and it resumes the instant you leave.
  // ...but the GAME's start screen is an exception to the exception. The hold
  // exists because the mDNS browse and the TCP connect BLOCK this loop for most
  // of a second, which made the menu near-unusable with no daemon to find. In a
  // level that would be far worse -- a 600 ms freeze mid-jump. But the game's
  // only speaker is the daemon, so holding unconditionally means a link that
  // dropped before you pressed START stays dropped, and the whole run is silent
  // with no way to fix it short of leaving. Reconnecting is therefore allowed on
  // the start screen and the codex cards, where nothing is moving and a hitch
  // costs nothing, and forbidden the instant a level is running.
  // UI_PAD is exempt for the same reason the game's start screen is: the pad is
  // a still image, so a blocking browse costs nothing to look at -- and if the
  // link drops while the Mac holds the grab, nothing can send X|0 to release
  // it. Holding reconnection there would strand the device in controller mode.
  net.holdReconnect(uiMode != UI_CONDUCTOR && uiMode != UI_PAD &&
                    !(uiMode == UI_GAME && game.idle()));
  net.poll();
  net.applyPendingConfig();
  pumpUsb();
  pumpNet();
  pollButtons();
  pollLed();
  pollBattery();
  pollHibernate();
  cfg.commit();                             // deferred flash write, if any

  unsigned long now = millis();

  // The factory-reset confirmation disarms itself. An armed wipe left sitting on
  // the glass is a trap for the next person who picks the device up looking for
  // the battery percentage.
  if (resetArmedMs && (now - resetArmedMs) >= RESET_ARM_MS) {
    resetArmedMs = 0;
    requestRender();
  }

  // A freshly authenticated link needs the same kick a Nano's reset gives: H
  // makes the daemon resend the frame AND re-arm the LED loop, so an alert that
  // was already waiting is not silently missed.
  MateNet::State ns = net.state();
  if (ns != lastNetState) {
    if (ns == MateNet::LINKED) {
      net.write("H");
      sendSoundPref();                      // the daemon keeps no per-device
                                            // state; re-assert it every link
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
  //
  // Not while the screen is dark: a full flush is ~28 ms of SPI at 40 MHz and
  // there is nothing to see. needRender stays SET, so wakeScreen() shows the
  // current frame rather than the one from when the screen went out.
  // A sprint in progress renders every pass and never coalesces: the throttle
  // below exists to stop a chatty daemon starving the display, and a game has
  // the opposite problem. notePoke() keeps the hibernate timer from blanking
  // the screen under someone mid-level, which idle-detection would otherwise do
  // to a player who is holding a button but sending the daemon nothing.
  if (uiMode == UI_GAME) {
    game.tick();
    notePoke();
    if (screenOn) { needRender = false; render(); }
    return;
  }

  // THE PAD IS A STILL IMAGE. Frames, LED updates and the blink phase all call
  // requestRender() about once a second, and honouring that here would spend a
  // full 110 KB flush -- ~28 ms of SPI -- redrawing a screen nobody is looking
  // at, on battery, while the Mac has the buttons. Only the pad's own changes
  // are worth a redraw; everything else is dropped, not queued.
  if (uiMode == UI_PAD) {
    if (needRender && screenOn && padDirty) {
      needRender = false;
      padDirty   = false;
      render();
    } else {
      needRender = false;
    }
    return;
  }

  if (needRender && screenOn) {
    bool quiet = (Serial.available() == 0) && (now - lastRxMs) >= 8;
    if (quiet || (now - dirtyMs) >= 60) {
      needRender = false;
      render();
    }
  }
}
