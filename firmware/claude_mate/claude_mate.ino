/*
 * Claude Mate - Arduino Nano firmware (main sketch)
 * ==================================================
 *
 * A USB hardware companion that shows the live status of Claude Code sessions.
 * This sketch drives a 128x32 SSD1306 I2C OLED, an indication LED (alert) and
 * three buttons. It speaks the daemon<->Arduino serial protocol over USB CDC
 * serial at 115200 baud, 8N1.
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
 * The indication LED is driven entirely by the daemon via V|<KIND> lines: it
 * decides per session WHEN to alert (job started / finished / needs-input /
 * error) and at what repeating cadence, and which pattern to blink. The firmware
 * just plays the pattern on the LED; the status word (D|) is visual only.
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
 *     SUBMIT button -> D2  (emits "B|1" on a short press: focus/proceed;
 *                          double-click opens LIST detail. "B|5" on a long
 *                          press: acknowledge the alert without focusing)
 *     NEXT   button -> D3  (emits "B|2"; next card / highlight down)
 *     MODE   button -> D4  (emits "B|3" on a short press, "B|4" on a long press;
 *                          short = prev card / highlight up, long = switch mode)
 *   Indication LED (the sole alert output; the vibration motor was removed):
 *     LED          -> D8   (OUTPUT; LED + ~220-1k series resistor to GND). Blinks
 *                          the alert pattern.
 *
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
 *     V|<kind>                                       LED alert control (light only):
 *                                                    START one-shot start blink,
 *                                                    INPUT one-shot needs-input
 *                                                    blink, DONE/ERROR looping
 *                                                    "until acknowledged" alerts
 *                                                    (repeat until V|OFF), OFF
 *                                                    stop/darken the LED now
 *   Arduino -> Daemon:
 *     H                                              hello, sent once after boot
 *     B|<n>                                          n=1 SUBMIT short, n=2 NEXT,
 *                                                    n=3 MODE short, n=4 MODE long,
 *                                                    n=5 SUBMIT long (acknowledge)
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
#define PIN_LED        8       // D8, OUTPUT, indication LED (+ ~220-1k series resistor
                               // to GND). The SOLE alert output: it blinks the alert
                               // pattern. (The vibration motor was removed.)

// ---- Protocol constants ------------------------------------------------------
#define SERIAL_BAUD    115200
#define LINE_MAX       96      // cap input line length to bound RAM use
                               // (fits S card + name + model + effort fields)
#define DEBOUNCE_MS    40UL    // ~40ms button debounce (snappy short taps)
#define LONGPRESS_MS   500UL   // MODE/SUBMIT held this long -> long-press

// ---- Alert-indicator tuning --------------------------------------------------
// The alert output is now just the LED on D8 (digital on/off) -- no vibration
// motor, so no PWM amplitude. Urgency reads through the rhythm of the pulses.
#define ALERT_MAX_STEPS   6       // longest pattern (DONE's 5-pulse heartbeat)
// A looping pattern (DONE/ERROR) repeats until the daemon sends V|OFF. As a
// failsafe, if the daemon goes silent (no serial at all) for this long we stop
// on our own so a crashed daemon can't leave the LED stuck on. The daemon pings
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

// ---- Render coalescing ---------------------------------------------------
// Serial handlers only MARK the display dirty (requestRender, in Helpers); the
// actual redraw happens in loop(), once the incoming burst has drained (the
// daemon sends D|/S|/T|/V| back-to-back). One redraw per burst instead of one
// per line, and never mid-burst -- so the RX buffer can't overflow behind a
// redraw.
static bool          needRender = false;
static unsigned long dirtyMs    = 0;      // when the display first went dirty

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
static VibroStep     vibroSteps[ALERT_MAX_STEPS];
static uint8_t       vibroStepCount = 0;     // pulses in the active pattern
static uint8_t       vibroStepIdx   = 0;     // pulse currently playing
static bool          vibroLoop      = false; // repeat the pattern until stopped?
static bool          vibroActive    = false; // a pattern is playing
static bool          vibroOn        = false; // LED lit right now?
static unsigned long vibroPhaseMs   = 0;     // millis() at the last phase change
static unsigned long lastRxMs       = 0;     // millis() of the last serial byte
                                             // (loop watchdog: daemon liveness)

// ---- Button debounce state ---------------------------------------------------
// Immediate-fire debounce: an edge is ACCEPTED (and its event emitted) the very
// tick it is seen, provided the last accepted edge is >= DEBOUNCE_MS old. Bounce
// after an accepted edge is ignored for the window; press latency is ~0 ms
// (the old scheme waited out the full window before emitting, and could miss a
// quick tap entirely).
//   NEXT emits on the press edge. SUBMIT and MODE distinguish a SHORT press
// (emit on release) from a LONG press (emit once at LONGPRESS_MS, then swallow
// the release).
struct Btn {
  uint8_t       pin;
  bool          pressed;    // debounced logical state (true = held down)
  unsigned long changeMs;   // when the last edge was accepted
  unsigned long pressMs;    // when the current press began
  bool          longFired;  // long-press already emitted this hold?
};
static Btn submitBtn = {PIN_BTN_SUBMIT, false, 0, 0, false};
static Btn nextBtn   = {PIN_BTN_NEXT,   false, 0, 0, false};
static Btn modeBtn   = {PIN_BTN_MODE,   false, 0, 0, false};

// -----------------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------------

// Mark the display dirty; loop() coalesces and performs the actual redraw.
static void requestRender() {
  if (!needRender) {
    needRender = true;
    dirtyMs = millis();
  }
}

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
// Alert output: indication LED (D8), non-blocking engine
// -----------------------------------------------------------------------------
// The pattern engine blinks the LED (on during each pulse's on-phase, off in the
// gaps). It always indicates -- there is no mute.

// Energise the ON-phase: light the LED.
static inline void alertOn()  { digitalWrite(PIN_LED, HIGH); }
// LED off (gap between pulses / stopped).
static inline void alertOff() { digitalWrite(PIN_LED, LOW); }

// Stop any alert immediately and leave both outputs off.
static void stopBuzz() {
  vibroActive    = false;
  vibroOn        = false;
  vibroStepCount = 0;
  alertOff();
}

// Begin an alert pattern: `count` LED pulses copied from `steps`. If `loop` is true
// the sequence repeats until stopBuzz() / a new pattern / the daemon-silence
// watchdog; otherwise it plays once. Returns immediately; the pulses play out in
// pollVibro(). A new call replaces any pattern in progress.
static void startPattern(const VibroStep *steps, uint8_t count, bool loop) {
  if (count == 0) { stopBuzz(); return; }
  if (count > ALERT_MAX_STEPS) count = ALERT_MAX_STEPS;
  for (uint8_t i = 0; i < count; i++) vibroSteps[i] = steps[i];
  vibroStepCount = count;
  vibroStepIdx   = 0;
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
  // the LED blinking forever. One-shots are short, so they need no watchdog.
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

// Daemon-driven alert patterns (blinked on the LED). Since the output is a LIGHT
// (not a buzz) the urgency has to read at a GLANCE, so each state has its own
// unmistakable rhythm and the "you need to act" states blink until acknowledged:
//
//   START  job (re)started : one long 1 s blink, then dark            (one-shot)
//   INPUT  needs your input: aggressive even blink (~2.8 Hz)          ; LOOPS until ack
//   ERROR  API error/alert : super-aggressive fast strobe (~7 Hz)     ; LOOPS until ack
//   DONE   turn finished   : cascade -- 4 quick blinks then a pause   ; LOOPS until ack
//   OFF    (or STOP)       : end any pattern now (daemon sends on acknowledge/clear)
//
// START is the only calm, one-shot signal (you just kicked something off, nothing
// is wrong). Everything that wants your attention keeps going until you focus the
// tab (the daemon sends V|OFF on FOCUS/clear); the firmware's silence watchdog
// still stops a loop if the daemon dies.
static void buzzForKind(const char *k) {
  if (!strcmp(k, "START")) {
    const VibroStep s[] = {{1000, 0}};                       // one long blink: job started
    startPattern(s, 1, false);
  } else if (!strcmp(k, "INPUT")) {
    const VibroStep s[] = {{180, 180}};                      // aggressive even blink: needs you
    startPattern(s, 1, true);
  } else if (!strcmp(k, "ERROR")) {
    const VibroStep s[] = {{70, 70}};                        // frantic strobe: error
    startPattern(s, 1, true);
  } else if (!strcmp(k, "DONE")) {
    const VibroStep s[] = {{110, 90}, {110, 90}, {110, 90}, {110, 650}}; // cascade burst + pause: finished
    startPattern(s, 4, true);
  } else if (!strcmp(k, "OFF") || !strcmp(k, "STOP")) {
    stopBuzz();
  }
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

static void render() {
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
      requestRender();
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
      requestRender();
      break;
    }

    case 'D': {                          // D|<WORD> -- set the status word
      char *bar = strchr(line, '|');
      if (!bar || bar[1] == 0) break;
      uint8_t w = parseWord(bar + 1);
      if (w == 255) break;               // ignore unknown word
      curWord = w;                       // dial/word only; haptics come via V|
      requestRender();
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
      requestRender();
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

static void emitBtn(uint8_t n) {
  Serial.print(F("B|"));
  Serial.println(n);
}

// One button, immediate-fire debounce. longN == 0: a plain button that emits
// shortN on the PRESS edge (instant). longN != 0: shortN is emitted on RELEASE
// (a short press), longN once when the hold crosses LONGPRESS_MS (the release
// is then swallowed).
static void pollBtn(Btn &b, uint8_t shortN, uint8_t longN) {
  bool raw = (digitalRead(b.pin) == LOW);   // pull-up: LOW = pressed
  unsigned long now = millis();
  if (raw != b.pressed && (now - b.changeMs) >= DEBOUNCE_MS) {
    b.pressed  = raw;                       // accept the edge NOW
    b.changeMs = now;
    if (raw) {                              // press edge
      b.pressMs   = now;
      b.longFired = false;
      if (longN == 0) emitBtn(shortN);      // plain button: fire on press
    } else {                                // release edge
      if (longN != 0 && !b.longFired) emitBtn(shortN);   // short press
    }
  }
  // Long-press fires while still held, once the threshold is crossed.
  if (longN != 0 && b.pressed && !b.longFired &&
      (now - b.pressMs) >= LONGPRESS_MS) {
    emitBtn(longN);
    b.longFired = true;
  }
}

static void pollButtons() {
  // SUBMIT: short = B|1 (focus; LIST double-click = detail, daemon-side),
  //         long (0.5s) = B|5 (acknowledge the alert without focusing).
  pollBtn(submitBtn, 1, 5);
  // NEXT: B|2 on press (next card / highlight down).
  pollBtn(nextBtn, 2, 0);
  // MODE: short = B|3 (prev / list-up), long (0.5s) = B|4 (toggle SCROLL/LIST).
  pollBtn(modeBtn, 3, 4);
}

// -----------------------------------------------------------------------------
// Setup / loop
// -----------------------------------------------------------------------------

void setup() {
  pinMode(PIN_BTN_SUBMIT, INPUT_PULLUP);
  pinMode(PIN_BTN_NEXT,   INPUT_PULLUP);
  pinMode(PIN_BTN_MODE,   INPUT_PULLUP);
  pinMode(PIN_LED,        OUTPUT);
  digitalWrite(PIN_LED,   LOW);          // LED off at boot
  lastRxMs = millis();                   // seed the haptic loop watchdog

  Serial.begin(SERIAL_BAUD);

  // Init the OLED. If it fails, blink the LED so a wiring fault is SEEN.
  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println(F("OLED init FAILED (try 0x3D)"));
    for (;;) {
      // A few short blinks, then a pause -- repeats forever. Hand-rolled here
      // (the alert engine is not pumped in this dead-end loop).
      for (uint8_t i = 0; i < 3; i++) {
        digitalWrite(PIN_LED, HIGH);
        delay(80);
        digitalWrite(PIN_LED, LOW);
        delay(120);
      }
      delay(600);
    }
  }

  // begin() already blanked and synced the panel. Clip overflowing text at the
  // screen edge instead of wrapping it onto the next row (which would corrupt
  // the multi-line card layout).
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
  display.pumpFlush();                   // ship ~32B (~2.3ms) of any armed frame

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
    if (cardBlink || listBlink) requestRender();
  }

  // Perform a pending redraw once the serial burst has drained (>=8ms of RX
  // silence), or after 60ms regardless so a continuous stream can't starve the
  // display. Drawing must wait for the previous frame's chunked transfer to
  // finish (flushBusy) -- rendering mutates the framebuffer mid-send otherwise.
  // render() itself only draws + ARMS the transfer (~1ms); the bytes go out via
  // pumpFlush() above, so buttons/serial stay live throughout.
  if (needRender && !display.flushBusy()) {
    unsigned long now = millis();
    bool rxQuiet = (Serial.available() == 0) && (now - lastRxMs) >= 8;
    if (rxQuiet || (now - dirtyMs) >= 60) {
      needRender = false;
      render();
    }
  }
}
