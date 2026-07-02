/*
 * Claude Mate - Arduino Nano firmware (main sketch)
 * ==================================================
 *
 * A USB hardware companion that shows the live status of Claude Code sessions.
 * This sketch drives a 128x32 SSD1306 I2C OLED, a MICRO VIBRATION MOTOR (haptic
 * alert) and three buttons. It speaks the daemon<->Arduino serial protocol over
 * USB CDC serial at 115200 baud, 8N1.
 *
 * The OLED shows one of four status words; rotation == escalation:
 *
 *     FREE -> WIP -> BLOCKED -> WTF
 *
 *   FREE     no session needs you (all idle/done, or no sessions).
 *   WIP      at least one session working (none blocked/errored).
 *   BLOCKED  at least one session waiting for your input (none errored).
 *   WTF      at least one session in error (StopFailure / API 5xx / timeout).
 *   Priority when several apply: WTF > BLOCKED > WIP > FREE.
 *
 * The vibration motor is driven entirely by the daemon via V|<KIND> lines: it
 * decides per session WHEN to buzz (job started / finished / needs-input /
 * error) and at what repeating cadence, and which gentle, graduated pattern to
 * play. The firmware just plays the pattern; the status word (D|) is visual
 * only and never buzzes on its own.
 *
 * REQUIRED LIBRARIES (install via the Arduino Library Manager):
 *   - Adafruit GFX Library     (Adafruit_GFX)
 *   - Adafruit SSD1306         (Adafruit_SSD1306)
 *   Adafruit GFX/SSD1306 pull in Adafruit BusIO + Wire (bundled with the IDE).
 *
 * PIN MAP (Arduino Nano, ATmega328P):
 *   OLED SSD1306 128x32 (0.91") over SOFTWARE I2C (hardware SCL A5 was damaged):
 *     VCC -> 5V, GND -> GND, SDA -> A4, SCL -> A3  (bit-banged; see softssd1306.h)
 *     I2C address 0x3C (common alternative: 0x3D)
 *   Buttons (INPUT_PULLUP, other leg to GND) -- layout MODE | SUBMIT | NEXT:
 *     SUBMIT button -> D2  (short "B|1" = focus/proceed; long "B|5" = toggle quiet
 *                          mode, which mutes all haptics on the device)
 *     NEXT   button -> D3  (emits "B|2"; next card / highlight down)
 *     MODE   button -> D4  (emits "B|3" on a short press, "B|4" on a long press;
 *                          short = prev card / highlight up, long = switch mode)
 *   Micro vibration motor (haptic alert):
 *     VIBRO        -> D5   (OUTPUT; drive via an NPN transistor or one ULN2003
 *                          channel -- do NOT drive the motor straight off a pin.
 *                          Add a flyback diode across the motor.)
 *   Indication LED:
 *     LED          -> D8   (OUTPUT; LED + ~220-1k series resistor to GND). Blinks
 *                          the alert pattern alongside the motor; in QUIET mode it
 *                          is the only output (motor muted).
 *
 * POWER:
 *   The vibration motor is tiny (tens of mA) and is switched through a
 *   transistor / ULN2003 channel, so the Nano's USB 5V rail is fine. Tie all
 *   grounds together (Nano GND, transistor/driver GND, motor supply GND).
 *
 * SERIAL PROTOCOL (115200 8N1, ASCII lines terminated by '\n', fields split '|')
 *   Daemon -> Arduino:
 *     D|<word>                                       word = FREE|WIP|BLOCKED|WTF
 *     S|<idx>|<total>|<name>|<state>|<runtime>|<limit>|<ack>|<model>|<effort>
 *         state  = working | waiting | error | done | idle
 *         ack    = 1 acknowledged / 0 not (optional; alert states show a dot:
 *                  filled+blinking = unacknowledged, hollow = acknowledged)
 *         model  = model name, e.g. "Opus 4.8" (optional; PTY-wrapper sessions)
 *         effort = effort level, e.g. "xhigh"  (optional; PTY-wrapper sessions)
 *                  model/effort render as a small "model · effort" middle row;
 *                  omitted/empty keeps the original two-line card.
 *     I                                              idle screen (no sessions)
 *     T|<total>|<sel>|<row>|<row>|...                LIST-mode frame (up to 4 rows);
 *                                                    row = <name>;<status>;<hl>;<ack>
 *                                                    status = WIP|WAIT|ERR|DONE|IDLE
 *                                                    total = tab count, sel = 0-based
 *                                                    highlighted index (scrollbar),
 *                                                    hl = 1 for the highlighted row,
 *                                                    ack = 0 for an unacknowledged
 *                                                    alert (draws a blinking dot).
 *     P                                              ping/keepalive (we reply H)
 *     V|<kind>                                       haptic control (motor only):
 *                                                    START one-shot start tick,
 *                                                    INPUT one-shot needs-input
 *                                                    tap, DONE/ERROR looping
 *                                                    "until acknowledged" alerts
 *                                                    (repeat until V|OFF), OFF
 *                                                    stop/silence the motor now
 *   Arduino -> Daemon:
 *     H                                              hello, sent once after boot
 *     B|<n>                                          n=1 SUBMIT short, n=2 NEXT,
 *                                                    n=3 MODE short, n=4 MODE long,
 *                                                    n=5 SUBMIT long (quiet toggle)
 *
 * NOTE: opening the USB serial port resets the Nano (~1.5s). That is why we emit
 * H once in setup() so the daemon can (re)send the full current state.
 *
 * RAM is tight on the '328P; we use fixed char buffers and the F() macro for
 * literals, and avoid String churn in loop().
 */

#include <Adafruit_GFX.h>
// Hardware I2C SCL (A5) was damaged, so the OLED runs on SOFTWARE I2C with SCL on
// A3 (a plain GPIO; the 328P's TWI is fixed to A4/A5 and can't be remapped) and
// SDA on A4. softssd1306.h is a drop-in Adafruit_GFX subclass, so all the drawing
// code below is unchanged. To go back to hardware I2C (A4/A5), restore the
// Adafruit_SSD1306 + <Wire.h> include and the matching constructor.
#include "softssd1306.h"      // defines OLED_SW_SCL=A3, OLED_SW_SDA=A4

// ---- OLED configuration ------------------------------------------------------
#define SCREEN_WIDTH   128
#define SCREEN_HEIGHT  32     // 0.91" SSD1306 (use 64 for a 0.96" panel)
#define SCREEN_ADDRESS 0x3C    // common alternative: 0x3D

// ---- Pin map -----------------------------------------------------------------
// Physical layout (left -> right): MODE | SUBMIT | NEXT.
#define PIN_BTN_SUBMIT 2       // D2, INPUT_PULLUP, emits B|1 (focus/proceed)
#define PIN_BTN_NEXT   3       // D3, INPUT_PULLUP, emits B|2 (next / highlight down)
#define PIN_BTN_MODE   4       // D4, INPUT_PULLUP, emits B|3 short / B|4 long
#define PIN_VIBRO      5       // D5, OUTPUT, drives the vibration motor (HIGH=on)
#define PIN_LED        8       // D8, OUTPUT, indication LED (add a ~220-1k series
                               // resistor to GND). Mirrors the alert pattern: it
                               // blinks with every buzz in normal mode, and is the
                               // ONLY output in quiet mode (motor muted).

// ---- Protocol constants ------------------------------------------------------
#define SERIAL_BAUD    115200
#define LINE_MAX       96      // cap input line length to bound RAM use
                               // (fits S card + name + model + effort fields)
#define DEBOUNCE_MS    40UL    // ~40ms button debounce (snappy short taps)
#define LONGPRESS_MS   500UL   // MODE held this long -> long-press (mode toggle)
#define MUTE_HOLD_MS   2000UL  // SUBMIT held this long -> mute toggle. Deliberately
                               // long so focusing a tab (a quick tap) never mutes.

// ---- Haptic tuning -----------------------------------------------------------
// The motor is a LOW-VOLTAGE (1.5-3V) motor driven from the 5V pin, so it MUST be
// PWM'd to keep the AVERAGE voltage within its rating -- driving it full-on (5V)
// over-volts it. Effective volts ~= 5 * duty / 255. Kept conservative; raise once
// the motor's rated voltage is known.
#define VIBRO_MAX_STEPS   6       // longest pattern (DONE's 5-pulse heartbeat)
#define DUTY_SOFT         75      // START + DONE   (~1.5V)
#define DUTY_INPUT        95      // needs-input    (~1.9V)
#define DUTY_ALERT       115      // ERROR / alert  (~2.3V; lower for a 1.5V motor)
// A looping pattern (DONE/ERROR) repeats until the daemon sends V|OFF. As a
// failsafe, if the daemon goes silent (no serial at all) for this long we stop
// on our own so a crashed daemon can't leave the motor buzzing. The daemon pings
// (P) every ~15s and streams S cards ~1/s, so this only trips when it is gone.
#define VIBRO_WATCHDOG_MS 30000UL

// Status words. Index order matches escalation FREE<WIP<BLOCKED<WTF.
enum Word { W_FREE = 0, W_WIP, W_BLOCKED, W_WTF };

// Session states (parsed from the S command)
enum State { ST_IDLE = 0, ST_WORKING, ST_WAITING, ST_ERROR, ST_DONE };

SoftSSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT);   // software I2C on A3(SCL)/A4(SDA)

// ---- Serial line assembly ----------------------------------------------------
static char  lineBuf[LINE_MAX];
static uint8_t lineLen = 0;
static bool  lineOverflow = false;   // drop the rest of an over-long line

// ---- Current display model ---------------------------------------------------
static char  curName[21]  = {0};     // up to 20 chars + NUL
static char  curRuntime[8] = {0};
static char  curLimit[8]  = {0};
static char  curModel[14]  = {0};    // model name, e.g. "Opus 4.8" (may be empty)
static char  curEffort[12] = {0};    // effort level, e.g. "xhigh"   (may be empty)
static uint8_t curIdx   = 0;
static uint8_t curTotal = 0;
static uint8_t curState = ST_IDLE;
static bool  curAck     = true;      // alert acknowledged (focused)? -> dot style
static bool  showIdle   = true;      // true => idle screen, no card
static bool  showList   = false;     // true => LIST-mode frame (overrides card)
static bool  gBlinkOn   = true;      // blink phase for the unacknowledged dot

// ---- LIST-mode model (T| frame) ----------------------------------------------
// The daemon owns the tab list + selection and sends a pre-windowed frame of up
// to LIST_ROWS visible rows; we just draw it. Highlighted row is drawn inverted.
#define LIST_ROWS 4                  // 128x32 fits 4 size-1 text rows (8px each)
#define LIST_DOT_X  27               // x of the per-row unacknowledged blink dot
#define LIST_NAME_X 33               // x where the name column starts (after status/dot)
static char    listName[LIST_ROWS][19] = {{0}};  // per-row name (18 chars + NUL)
static char    listStatus[LIST_ROWS][6] = {{0}}; // per-row status label (WIP/WAIT/...)
static bool    listHl[LIST_ROWS]       = {false};// per-row highlighted?
static bool    listUnacked[LIST_ROWS]  = {false};// per-row unacknowledged alert? (blink dot)
static uint8_t listRows  = 0;        // visible rows in the frame (0..LIST_ROWS)
static uint8_t listTotal = 0;        // total tabs (for the scrollbar)
static uint8_t listSel   = 0;        // highlighted global index (for the scrollbar)

// ---- Status word -------------------------------------------------------------
static uint8_t curWord = W_FREE;     // current OLED word

// ---- Vibration (haptic) state machine ----------------------------------------
// A pattern is a short list of ON pulses; each pulse has an on-duration and the
// gap that FOLLOWS it. All pulses in a pattern share one PWM amplitude (duty).
// A pattern either plays once or LOOPS (repeats until V|OFF / a new pattern /
// the daemon-silence watchdog). This step form lets us do the gentle alternating
// "heartbeat" DONE rhythm and the looping "until acknowledged" alerts, which the
// old single-gap engine could not.
struct VibroStep { uint16_t onMs; uint16_t offMs; };
static VibroStep     vibroSteps[VIBRO_MAX_STEPS];
static uint8_t       vibroStepCount = 0;     // pulses in the active pattern
static uint8_t       vibroStepIdx   = 0;     // pulse currently playing
static uint8_t       vibroDuty      = 0;     // PWM amplitude of this pattern (0-255)
static bool          vibroLoop      = false; // repeat the pattern until stopped?
static bool          vibroActive    = false; // a pattern is playing
static bool          vibroOn        = false; // motor energised right now?
static unsigned long vibroPhaseMs   = 0;     // millis() at the last phase change
static unsigned long lastRxMs       = 0;     // millis() of the last serial byte
                                             // (loop watchdog: daemon liveness)

// ---- Button debounce state ---------------------------------------------------
// NEXT is a simple press-edge button. SUBMIT and MODE each distinguish a SHORT
// press (emit on release) from a LONG press (emit once at LONGPRESS_MS, then
// swallow the release). SUBMIT long-press ALSO toggles quiet mode locally.
struct BtnLong {
  bool          stable;     // released?
  int           lastRaw;
  unsigned long edgeMs;     // debounce edge time
  unsigned long pressMs;    // when the current press began
  bool          longFired;  // long-press already emitted this hold?
};
static BtnLong submitBtn = {true, HIGH, 0, 0, false};
static BtnLong modeBtn   = {true, HIGH, 0, 0, false};
static bool          nextStable   = true;  // pull-up idle = HIGH (released)
static int           nextLastRaw  = HIGH;
static unsigned long nextEdgeMs   = 0;

// ---- Quiet mode (motor muted; LED still indicates) ---------------------------
static bool quietMode = false;              // SUBMIT long-press toggles this
static unsigned long quietToastUntil = 0;   // show the ON/OFF toast until this ms

// -----------------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------------

// Map a state string to the State enum. Defaults to idle on unknown input.
static uint8_t parseState(const char *s) {
  if (!s) return ST_IDLE;
  if (strcmp(s, "working") == 0) return ST_WORKING;
  if (strcmp(s, "waiting") == 0) return ST_WAITING;
  if (strcmp(s, "error")   == 0) return ST_ERROR;
  if (strcmp(s, "done")    == 0) return ST_DONE;
  return ST_IDLE;
}

// Human-readable, uppercase state label for the card.
static const __FlashStringHelper *stateLabel(uint8_t st) {
  switch (st) {
    case ST_WORKING: return F("WORKING");
    case ST_WAITING: return F("WAITING");
    case ST_ERROR:   return F("ERROR");
    case ST_DONE:    return F("DONE");
    default:         return F("IDLE");
  }
}

// Word label for the OLED text.
static const __FlashStringHelper *wordLabel(uint8_t w) {
  switch (w) {
    case W_WIP:     return F("WIP");
    case W_BLOCKED: return F("BLOCKED");
    case W_WTF:     return F("WTF");
    default:        return F("FREE");
  }
}

// Map a status word string -> Word enum. Returns 255 on unknown.
static uint8_t parseWord(const char *s) {
  if (!s) return 255;
  if (strcmp(s, "FREE")    == 0) return W_FREE;
  if (strcmp(s, "WIP")     == 0) return W_WIP;
  if (strcmp(s, "BLOCKED") == 0) return W_BLOCKED;
  if (strcmp(s, "WTF")     == 0) return W_WTF;
  return 255;
}

// -----------------------------------------------------------------------------
// Alert outputs: vibration motor (D5) + indication LED (D8), non-blocking engine
// -----------------------------------------------------------------------------
// The pattern engine drives BOTH outputs. The LED mirrors every pulse (on during
// the on-phase); the motor does too EXCEPT in quiet mode, where it stays off so
// the LED is the only indication. Because the pattern keeps running in quiet mode
// (for the LED), un-muting simply re-enables the motor mid-pattern -- no replay.

// Energise the ON-phase: LED full-on; motor PWM'd at vibroDuty (unless muted) so a
// LOW-VOLTAGE motor is not over-driven by the 5V pin. analogWrite on D5 (PWM-capable)
// does not disturb millis().
static inline void alertOn() {
  digitalWrite(PIN_LED,   HIGH);
  analogWrite(PIN_VIBRO,  quietMode ? 0 : vibroDuty);
}
// Both outputs off (gap between pulses / stopped).
static inline void alertOff() {
  digitalWrite(PIN_LED,   LOW);
  analogWrite(PIN_VIBRO,  0);
}

// Stop any alert immediately and leave both outputs off.
static void stopBuzz() {
  vibroActive    = false;
  vibroOn        = false;
  vibroStepCount = 0;
  alertOff();
}

// Begin a haptic pattern: `count` pulses copied from `steps`, at PWM amplitude
// `duty`. If `loop` is true the sequence repeats until stopBuzz() / a new pattern /
// the daemon-silence watchdog; otherwise it plays once. Returns immediately; the
// pulses play out in pollVibro(). A new call replaces any pattern in progress.
static void startPattern(const VibroStep *steps, uint8_t count,
                         uint8_t duty, bool loop) {
  if (count == 0) { stopBuzz(); return; }
  if (count > VIBRO_MAX_STEPS) count = VIBRO_MAX_STEPS;
  for (uint8_t i = 0; i < count; i++) vibroSteps[i] = steps[i];
  vibroStepCount = count;
  vibroStepIdx   = 0;
  vibroDuty      = duty;
  vibroLoop      = loop;
  vibroActive    = true;
  vibroOn        = true;                // start the first pulse now
  vibroPhaseMs   = millis();
  alertOn();
}

// Advance the haptic state machine. Call every loop(); never blocks.
static void pollVibro() {
  if (!vibroActive) return;            // idle: nothing playing
  unsigned long now = millis();

  // Loop failsafe: if we are repeating a pattern but the daemon has gone silent
  // (no serial for VIBRO_WATCHDOG_MS), stop -- a crashed daemon must not leave
  // the motor buzzing forever. One-shots are short, so they need no watchdog.
  if (vibroLoop && (now - lastRxMs) >= VIBRO_WATCHDOG_MS) {
    stopBuzz();
    return;
  }

  const VibroStep &step = vibroSteps[vibroStepIdx];
  if (vibroOn) {
    if ((now - vibroPhaseMs) >= step.onMs) {
      alertOff();                      // end of on-phase -> start this step's gap
      vibroOn = false;
      vibroPhaseMs = now;
    }
  } else {
    if ((now - vibroPhaseMs) >= step.offMs) {
      // Gap elapsed: advance to the next pulse, looping or finishing at the end.
      vibroStepIdx++;
      if (vibroStepIdx >= vibroStepCount) {
        if (!vibroLoop) { stopBuzz(); return; }   // one-shot complete -- stay off
        vibroStepIdx = 0;                          // loop back to the first pulse
      }
      alertOn();
      vibroOn = true;
      vibroPhaseMs = now;
    }
  }
}

// Daemon-driven haptic patterns. The daemon decides WHEN to buzz and how the
// "until acknowledged" alerts repeat; the firmware just plays what it is told. The
// motor runs full-on per pulse -- gentleness comes from the short, rhythmic pulses
// and the gaps, not from a weak amplitude (a coin motor won't run at low PWM).
//
//   START  job (re)started : 3x0.3s pulses                       (one-shot)
//   DONE   turn finished   : 5x0.2s heartbeat, gaps 0.2/0.4, then rest; LOOPS
//   INPUT  needs your input: double-tap                          (daemon re-taps ~10s)
//   ERROR  API error/alert : 0.4s on / 0.2s off                ; LOOPS until ack
//   OFF    (or STOP)       : end any pattern now (daemon sends on acknowledge/clear)
static void buzzForKind(const char *k) {
  if (!strcmp(k, "START")) {
    const VibroStep s[] = {{300, 180}, {300, 180}, {300, 0}};
    startPattern(s, 3, DUTY_SOFT, false);
  } else if (!strcmp(k, "DONE")) {
    // 5 pulses of 0.2s with alternating 0.2/0.4 gaps (a heartbeat), then a 0.9s
    // rest before it repeats. Loops until the daemon sends OFF (you focus).
    const VibroStep s[] = {{200, 200}, {200, 400}, {200, 200}, {200, 400}, {200, 900}};
    startPattern(s, 5, DUTY_SOFT, true);
  } else if (!strcmp(k, "INPUT")) {
    // Double-tap. One-shot; the daemon re-taps every ~10s until you focus.
    const VibroStep s[] = {{90, 150}, {90, 0}};
    startPattern(s, 2, DUTY_INPUT, false);
  } else if (!strcmp(k, "ERROR")) {
    // 0.4s buzz, 0.2s pause, forever. Loops until the daemon sends OFF.
    const VibroStep s[] = {{400, 200}};
    startPattern(s, 1, DUTY_ALERT, true);
  } else if (!strcmp(k, "OFF") || !strcmp(k, "STOP")) {
    stopBuzz();
  }
}

// Toggle quiet mode (SUBMIT long-press). Only the MOTOR is gated -- the alert
// pattern (and the LED) keeps running -- so muting just drops the motor and
// un-muting resumes it mid-pattern (if a tab still needs you, you feel it at once).
static void toggleQuiet() {
  quietMode = !quietMode;
  // Apply the motor gate NOW (mid-pulse), so it doesn't wait for the next phase.
  analogWrite(PIN_VIBRO, (!quietMode && vibroActive && vibroOn) ? vibroDuty : 0);
  quietToastUntil = millis() + 1200;  // confirm the toggle with a brief toast
  render();
}

// -----------------------------------------------------------------------------
// Rendering
// -----------------------------------------------------------------------------

// Short, fixed state label so "STATUS time" fits on one size-2 row.
static const __FlashStringHelper *shortState(uint8_t st) {
  switch (st) {
    case ST_WORKING: return F("WORK");
    case ST_WAITING: return F("WAIT");
    case ST_ERROR:   return F("ERR");
    case ST_DONE:    return F("DONE");
    default:         return F("IDLE");
  }
}

static void drawIdle() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print(F("claude-mate"));
  display.setTextSize(2);
  display.setCursor(0, 14);
  display.print(F("IDLE"));
  display.display();
}

// Alert states need your attention and carry an acknowledgment dot.
static bool isAlertState(uint8_t s) {
  return s == ST_DONE || s == ST_WAITING || s == ST_ERROR;
}

static void drawCard() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  // ---- Acknowledgment dot (top-left), for alert states only ----
  //   filled + blinking = UNacknowledged (needs you); hollow ring =
  //   acknowledged (you focused it). Non-alert states show no dot.
  int16_t nameX = 0;
  if (isAlertState(curState)) {
    if (curAck)             display.drawCircle(3, 3, 3, SSD1306_WHITE);  // seen
    else if (gBlinkOn)      display.fillCircle(3, 3, 3, SSD1306_WHITE);  // needs you
    nameX = 9;              // reserve the slot so the name doesn't overlap it
  }

  // ---- Top line (size 1): name (left, truncated) + idx/total (right) ----
  char pos[12];
  snprintf(pos, sizeof(pos), "%u/%u", (unsigned)curIdx, (unsigned)curTotal);
  display.setTextSize(1);
  int16_t px = SCREEN_WIDTH - (int16_t)(strlen(pos) * 6);
  if (px < 0) px = 0;
  int16_t avail = px - nameX;                                // width left for name
  uint8_t nameMax = (avail > 6) ? (uint8_t)(avail / 6 - 1) : 1;
  char nm[21];
  uint8_t i = 0;
  for (; curName[i] && i < nameMax && i < 20; i++) nm[i] = curName[i];
  nm[i] = 0;
  display.setCursor(nameX, 0);
  display.print(nm);
  display.setCursor(px, 0);
  display.print(pos);

  // ---- Middle line (size 1): "model · effort" (PTY-wrapper sessions only) ----
  // Drawn only when we actually have model/effort, so hook-driven cards keep the
  // original two-line look. The separator is a small filled dot to match the
  // device's ack-dot aesthetic (the classic 6x8 font has no real middot glyph).
  bool hasMeta = (curModel[0] || curEffort[0]);
  if (hasMeta) {
    display.setTextSize(1);
    int16_t mx = 0;
    if (curModel[0]) {
      display.setCursor(0, 11);
      display.print(curModel);
      mx = (int16_t)strlen(curModel) * 6;
    }
    if (curModel[0] && curEffort[0]) {
      display.fillCircle(mx + 2, 14, 1, SSD1306_WHITE);      // "·" separator
      mx += 6;
    }
    if (curEffort[0]) {
      display.setCursor(mx, 11);
      display.print(curEffort);
    }
  }

  // ---- Status + time ----
  // 3-line card (model row present): keep the status SMALL (size 1) too, so the
  // three rows (name y0 / model·effort y11 / status y22) share the 32px panel
  // evenly instead of the big size-2 word crowding/overflowing the others.
  // 2-line card (no model row): the status stays big (size 2) and centred.
  if (hasMeta) {
    display.setTextSize(1);
    display.setCursor(0, 22);
  } else {
    display.setTextSize(2);
    display.setCursor(0, 14);
  }
  display.print(shortState(curState));
  display.print(' ');
  display.print(curRuntime[0] ? curRuntime : "-");

  display.display();
}

// LIST mode: draw up to LIST_ROWS tab rows (glyph + name), the highlighted row
// inverted, and a scrollbar on the right when there are more tabs than fit.
static void drawList() {
  display.clearDisplay();
  const bool hasBar = (listTotal > LIST_ROWS);
  const int16_t barX = SCREEN_WIDTH - 3;          // 3px scrollbar strip on the right
  const int16_t rowW = hasBar ? barX - 1 : SCREEN_WIDTH;

  for (uint8_t r = 0; r < listRows && r < LIST_ROWS; r++) {
    int16_t y = (int16_t)r * 8;
    if (listHl[r]) {                               // highlighted row: inverted bar
      display.fillRect(0, y, rowW, 8, SSD1306_WHITE);
      display.setTextColor(SSD1306_BLACK);
    } else {
      display.setTextColor(SSD1306_WHITE);
    }
    display.setTextSize(1);
    display.setCursor(1, y);
    display.print(listStatus[r]);                  // status label (WIP/WAIT/...)
    // Unacknowledged alert: a filled dot that BLINKS (gBlinkOn), like the card's
    // ack dot -- so a tab needing you stands out in the list at a glance.
    if (listUnacked[r] && gBlinkOn) {
      display.fillCircle(LIST_DOT_X, y + 3, 2,
                         listHl[r] ? SSD1306_BLACK : SSD1306_WHITE);
    }
    display.setCursor(LIST_NAME_X, y);             // aligned name column
    display.print(listName[r]);                    // clipped at the edge (no wrap)
  }

  if (hasBar) {                                    // scrollbar: track + thumb
    display.drawFastVLine(barX + 1, 0, SCREEN_HEIGHT, SSD1306_WHITE);
    uint8_t denom = (listTotal > 1) ? (listTotal - 1) : 1;
    int16_t thumbH = 6;
    int16_t ty = (int16_t)listSel * (SCREEN_HEIGHT - thumbH) / denom;
    display.fillRect(barX, ty, 3, thumbH, SSD1306_WHITE);
  }

  display.display();
}

// Full-screen toggle toast, shown for ~1.2s after a quiet-mode toggle (SUBMIT
// long-press) to confirm it -- instead of a persistent corner badge, which would
// clobber the card's idx/total counter and the list scrollbar and cost a second
// slow software-I2C frame push on every render.
static void drawQuietToast() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 2);
  display.print(F("VIBRATION"));
  display.setTextSize(2);
  display.setCursor(0, 14);
  display.print(quietMode ? F("OFF") : F("ON"));
  display.display();
}

static void render() {
  if (quietToastUntil && millis() < quietToastUntil) { drawQuietToast(); return; }
  if      (showList) drawList();
  else if (showIdle) drawIdle();
  else               drawCard();
}

// -----------------------------------------------------------------------------
// Protocol parsing
// -----------------------------------------------------------------------------

// Copy a field into dst with a hard cap (always NUL-terminated).
static void copyField(char *dst, uint8_t cap, const char *src) {
  if (!src) { dst[0] = 0; return; }
  uint8_t i = 0;
  while (src[i] && i < (cap - 1)) { dst[i] = src[i]; i++; }
  dst[i] = 0;
}

// Handle one complete, NUL-terminated line.
static void handleLine(char *line) {
  if (line[0] == 0) return;              // ignore empty lines

  char type = line[0];

  switch (type) {
    case 'P':                            // ping -> reply hello/keepalive
      Serial.println('H');
      break;

    case 'I':                            // idle screen
      showIdle = true;
      showList = false;
      curState = ST_IDLE;
      render();
      break;

    case 'T': {  // T|total|sel|<name;st;hl;ack>|...  -- LIST-mode frame
      // The daemon owns the tab list + selection and windows it to <=LIST_ROWS
      // visible rows. Tokenize on '|': fields[1]=total, [2]=sel, [3..]=rows.
      char *fields[3 + LIST_ROWS];
      uint8_t n = 0;
      char *p = line;
      fields[n++] = p;                   // fields[0] = "T"
      while (n < (uint8_t)(3 + LIST_ROWS)) {
        char *bar = strchr(p, '|');
        if (!bar) break;
        *bar = 0;
        p = bar + 1;
        fields[n++] = p;
      }
      if (n < 3) break;                  // malformed: need at least total + sel
      listTotal = (uint8_t)atoi(fields[1]);
      listSel   = (uint8_t)atoi(fields[2]);
      listRows  = 0;
      for (uint8_t f = 3; f < n && listRows < LIST_ROWS; f++) {
        // Each row is "name;status;hl;ack". Split on ';' in place.
        char *row = fields[f];
        char *nm  = row;
        char *st  = "";
        bool  hl  = false;
        bool  unacked = false;
        char *s1 = strchr(row, ';');
        if (s1) {
          *s1 = 0;
          st = s1 + 1;
          char *s2 = strchr(st, ';');
          if (s2) {
            *s2 = 0;
            hl = (s2[1] == '1');
            char *s3 = strchr(s2 + 1, ';');
            if (s3) unacked = (s3[1] == '0');   // ack field: 0 = unacked -> blink
          }
        }
        copyField(listName[listRows],   sizeof(listName[0]),   nm);
        copyField(listStatus[listRows], sizeof(listStatus[0]), st);
        listHl[listRows]      = hl;
        listUnacked[listRows] = unacked;
        listRows++;
      }
      showList = true;
      showIdle = false;
      render();
      break;
    }

    case 'D': {                          // D|<WORD> -- set the status word
      char *bar = strchr(line, '|');
      if (!bar || bar[1] == 0) break;
      uint8_t w = parseWord(bar + 1);
      if (w == 255) break;               // ignore unknown word
      curWord = w;                       // dial/word only; haptics come via V|
      render();                          // refresh the OLED word now
      break;
    }

    case 'V': {                          // V|<KIND> -- buzz a haptic pattern NOW
      // The daemon owns all haptics: it decides per-session WHEN to buzz (job
      // start, finish, needs-input, error) and at what cadence, and sends the
      // KIND here. We just play it -- no change to the dial/OLED.
      char *bar = strchr(line, '|');
      if (!bar || bar[1] == 0) break;
      buzzForKind(bar + 1);              // START | DONE | INPUT | ERROR
      break;
    }

    case 'S': {  // S|idx|total|name|state|runtime|limit|ack|model|effort
      // Tokenize in place on '|'. 7 core tokens (type + 6), optional 8th 'ack',
      // optional 9th 'model' and 10th 'effort'.
      char *fields[10];
      uint8_t n = 0;
      char *p = line;
      fields[n++] = p;                   // fields[0] = "S"
      while (n < 10) {
        char *bar = strchr(p, '|');
        if (!bar) break;
        *bar = 0;
        p = bar + 1;
        fields[n++] = p;
      }
      if (n < 7) break;                  // malformed: ignore
      curIdx   = (uint8_t)atoi(fields[1]);
      curTotal = (uint8_t)atoi(fields[2]);
      copyField(curName,    sizeof(curName),    fields[3]);
      curState = parseState(fields[4]);
      copyField(curRuntime, sizeof(curRuntime), fields[5]);
      copyField(curLimit,   sizeof(curLimit),   fields[6]);
      // 8th field (if present): "1" acknowledged, "0" not. Default acknowledged
      // so an older daemon (7 fields) never shows a spurious blinking dot.
      curAck = (n < 8) || (fields[7][0] != '0');
      // 9th/10th fields (if present): model + effort. Empty when an older daemon
      // or a hook-driven session omits them -- drawCard just skips the row then.
      copyField(curModel,  sizeof(curModel),  (n > 8) ? fields[8] : "");
      copyField(curEffort, sizeof(curEffort), (n > 9) ? fields[9] : "");
      showIdle = false;
      showList = false;                  // a card frame leaves LIST mode
      render();
      break;
    }

    default:
      // Unknown line type: ignore silently (robust to garbage).
      break;
  }
}

// Feed serial bytes into the line buffer; dispatch on newline.
static void pumpSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    lastRxMs = millis();                  // feed the haptic loop watchdog
    if (c == '\n' || c == '\r') {
      if (!lineOverflow && lineLen > 0) {
        lineBuf[lineLen] = 0;
        handleLine(lineBuf);
      }
      lineLen = 0;
      lineOverflow = false;
    } else {
      if (lineLen < (LINE_MAX - 1)) {
        lineBuf[lineLen++] = c;
      } else {
        // Over-long line: drop until newline so we resync cleanly.
        lineOverflow = true;
      }
    }
  }
}

// -----------------------------------------------------------------------------
// Buttons
// -----------------------------------------------------------------------------

// Simple press-edge button (NEXT): debounce, emit "B|<n>" on press.
static void pollPressButton(uint8_t pin, uint8_t n, bool &stable,
                            int &lastRaw, unsigned long &edgeMs) {
  int raw = digitalRead(pin);
  unsigned long now = millis();
  if (raw != lastRaw) {
    lastRaw = raw;
    edgeMs = now;                        // start debounce window
  }
  if ((now - edgeMs) >= DEBOUNCE_MS) {
    bool pressedNow = (raw == LOW);      // pull-up: LOW = pressed
    bool wasPressed = !stable;
    if (pressedNow && !wasPressed) {      // edge into pressed -> emit
      Serial.print(F("B|"));
      Serial.println(n);
    }
    stable = !pressedNow;                 // stable=true means released
  }
}

// Short/long-press button (SUBMIT, MODE): emit "B|<shortN>" on a SHORT press
// (released before LONGPRESS_MS) or "B|<longN>" once when the hold crosses
// LONGPRESS_MS (the release is then swallowed). Returns true on the ONE tick the
// long-press fires, so the caller can also act locally (SUBMIT toggles quiet).
static bool pollLongButton(uint8_t pin, uint8_t shortN, uint8_t longN,
                           unsigned long longMs, BtnLong &b) {
  bool longEdge = false;
  int raw = digitalRead(pin);
  unsigned long now = millis();
  if (raw != b.lastRaw) {
    b.lastRaw = raw;
    b.edgeMs = now;
  }
  if ((now - b.edgeMs) >= DEBOUNCE_MS) {
    bool pressedNow = (raw == LOW);
    bool wasPressed = !b.stable;
    if (pressedNow && !wasPressed) {      // press edge: start timing the hold
      b.pressMs = now;
      b.longFired = false;
    } else if (!pressedNow && wasPressed) {
      if (!b.longFired) { Serial.print(F("B|")); Serial.println(shortN); }  // short
    }
    b.stable = !pressedNow;
  }
  // Long-press fires while still held, once the threshold is crossed.
  if (!b.stable && !b.longFired && (now - b.pressMs) >= longMs) {
    Serial.print(F("B|")); Serial.println(longN);
    b.longFired = true;
    longEdge = true;
  }
  return longEdge;
}

static void pollButtons() {
  // SUBMIT: short = B|1 (focus/proceed), long HOLD (2s) = B|5 (+ toggle quiet mode).
  if (pollLongButton(PIN_BTN_SUBMIT, 1, 5, MUTE_HOLD_MS, submitBtn)) toggleQuiet();
  pollPressButton(PIN_BTN_NEXT, 2, nextStable, nextLastRaw, nextEdgeMs);
  // MODE: short = B|3 (prev / list-up), long (0.5s) = B|4 (toggle SCROLL/LIST mode).
  pollLongButton(PIN_BTN_MODE, 3, 4, LONGPRESS_MS, modeBtn);
}

// -----------------------------------------------------------------------------
// Setup / loop
// -----------------------------------------------------------------------------

void setup() {
  pinMode(PIN_BTN_SUBMIT, INPUT_PULLUP);
  pinMode(PIN_BTN_NEXT,   INPUT_PULLUP);
  pinMode(PIN_BTN_MODE,   INPUT_PULLUP);
  pinMode(PIN_VIBRO,      OUTPUT);
  pinMode(PIN_LED,        OUTPUT);
  digitalWrite(PIN_VIBRO, LOW);          // motor off at boot
  digitalWrite(PIN_LED,   LOW);          // LED off at boot
  lastRxMs = millis();                   // seed the haptic loop watchdog

  Serial.begin(SERIAL_BAUD);

  // Init the OLED. If it fails, buzz the motor + blink the LED so a wiring fault
  // is FELT and SEEN.
  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println(F("OLED init FAILED (try 0x3D)"));
    for (;;) {
      // A few short buzzes/blinks, then a pause -- repeats forever. Hand-rolled
      // here (the haptic engine is not pumped in this dead-end loop).
      for (uint8_t i = 0; i < 3; i++) {
        analogWrite(PIN_VIBRO, DUTY_ALERT); digitalWrite(PIN_LED, HIGH);  // PWM: don't over-volt
        delay(80);
        analogWrite(PIN_VIBRO, 0);          digitalWrite(PIN_LED, LOW);
        delay(120);
      }
      delay(600);
    }
  }

  display.clearDisplay();
  display.display();
  // Clip overflowing text at the screen edge instead of wrapping it onto the
  // next row (which would corrupt the multi-line card layout).
  display.setTextWrap(false);

  // Default to FREE and the idle screen until the daemon talks to us.
  curWord = W_FREE;
  showIdle = true;
  render();

  // Emit hello once, after the display is up, so the daemon resends the full
  // current state (status word + current card).
  Serial.println('H');
}

void loop() {
  pumpSerial();
  pollButtons();
  pollVibro();                           // advance the haptic state machine

  // Restore the normal screen when the quiet-toggle toast expires.
  if (quietToastUntil && millis() >= quietToastUntil) {
    quietToastUntil = 0;
    render();
  }

  // Blink the unacknowledged-alert dot (~every 400ms) -- on the card, or on any
  // unacknowledged row in the LIST. Re-render only when a blinking dot is shown.
  bool nb = (millis() / 400) & 1;
  if (nb != gBlinkOn) {
    gBlinkOn = nb;
    bool cardBlink = !showIdle && !showList && isAlertState(curState) && !curAck;
    bool listBlink = false;
    if (showList) {
      for (uint8_t r = 0; r < listRows && r < LIST_ROWS; r++)
        if (listUnacked[r]) { listBlink = true; break; }
    }
    if (cardBlink || listBlink) render();
  }
}
