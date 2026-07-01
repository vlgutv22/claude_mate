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
 *   Buttons (INPUT_PULLUP, other leg to GND):
 *     FOCUS button -> D2   (emits "B|1")
 *     NEXT  button -> D3   (emits "B|2")
 *     PREV  button -> D4   (emits "B|3")
 *   Micro vibration motor (haptic alert):
 *     VIBRO        -> D5   (OUTPUT; drive via an NPN transistor or one ULN2003
 *                          channel -- do NOT drive the motor straight off a pin.
 *                          Add a flyback diode across the motor.)
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
 *     B|<n>                                          n=1 FOCUS, n=2 NEXT, n=3 PREV
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
#define PIN_BTN_FOCUS  2       // D2, INPUT_PULLUP, emits B|1
#define PIN_BTN_NEXT   3       // D3, INPUT_PULLUP, emits B|2
#define PIN_BTN_PREV   4       // D4, INPUT_PULLUP, emits B|3
#define PIN_VIBRO      5       // D5, OUTPUT, drives the vibration motor (HIGH=on)

// ---- Protocol constants ------------------------------------------------------
#define SERIAL_BAUD    115200
#define LINE_MAX       96      // cap input line length to bound RAM use
                               // (fits S card + name + model + effort fields)
#define DEBOUNCE_MS    200UL   // ~200ms button debounce

// ---- Haptic tuning -----------------------------------------------------------
// PWM amplitude (0-255) per pattern, kept "graduated but soft": urgency reads as
// rhythm + a little more push, never full-power buzz. Start/Done are the gentlest.
#define VIBRO_MAX_STEPS   6       // longest pattern (DONE's 5-pulse heartbeat)
#define DUTY_SOFT         80      // START tick + DONE heartbeat (gentlest)
#define DUTY_INPUT       120      // needs-input double-tap (a touch firmer)
#define DUTY_ALERT       160      // ERROR / warning loop (firmest; never 255)
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
static bool  gBlinkOn   = true;      // blink phase for the unacknowledged dot

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
static uint8_t       vibroDuty      = 0;     // PWM amplitude of this pattern
static bool          vibroLoop      = false; // repeat the pattern until stopped?
static bool          vibroActive    = false; // a pattern is playing
static bool          vibroOn        = false; // motor energised right now?
static unsigned long vibroPhaseMs   = 0;     // millis() at the last phase change
static unsigned long lastRxMs       = 0;     // millis() of the last serial byte
                                             // (loop watchdog: daemon liveness)

// ---- Button debounce state ---------------------------------------------------
static bool          focusStable = true;   // pull-up idle = HIGH (released)
static bool          nextStable  = true;
static bool          prevStable  = true;
static unsigned long focusEdgeMs = 0;
static unsigned long nextEdgeMs  = 0;
static unsigned long prevEdgeMs  = 0;
static int           focusLastRaw = HIGH;
static int           nextLastRaw  = HIGH;
static int           prevLastRaw  = HIGH;

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
// Vibration motor (non-blocking haptic engine)
// -----------------------------------------------------------------------------

// Stop any haptic immediately and leave the motor off.
static void stopBuzz() {
  vibroActive    = false;
  vibroOn        = false;
  vibroStepCount = 0;
  analogWrite(PIN_VIBRO, 0);
}

// Begin a haptic pattern: `count` pulses copied from `steps`, all at PWM
// amplitude `duty` (0-255). If `loop` is true the sequence repeats until
// stopBuzz() / a new pattern / the daemon-silence watchdog; otherwise it plays
// once. Returns immediately; the pulses play out in pollVibro(). A new call
// replaces any pattern in progress. PIN_VIBRO (D5) is PWM-capable, so duty sets
// how hard the motor pushes (lower = gentler); analogWrite does not disturb millis().
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
  analogWrite(PIN_VIBRO, duty);
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
      analogWrite(PIN_VIBRO, 0);       // end of on-phase -> start this step's gap
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
      analogWrite(PIN_VIBRO, vibroDuty);
      vibroOn = true;
      vibroPhaseMs = now;
    }
  }
}

// Daemon-driven haptic patterns, gentlest -> firmest. The daemon decides WHEN to
// buzz and how the "until acknowledged" alerts repeat; the firmware just plays
// what it is told. Amplitude (PWM duty) stays well below full power so urgency
// reads as rhythm + a little more push, never a jarring jolt.
//
//   START  job (re)started : 3 gentle 0.3s pulses               (one-shot)
//   DONE   turn finished   : 5x0.2s heartbeat, gaps 0.2/0.4, then rest; LOOPS
//   INPUT  needs your input: soft double-tap                    (daemon re-taps ~10s)
//   ERROR  API error/alert : 0.4s on / 0.2s off               ; LOOPS until ack
//   OFF    (or STOP)       : end any pattern now (daemon sends on acknowledge/clear)
static void buzzForKind(const char *k) {
  if (!strcmp(k, "START")) {
    // 3 gentle pulses of 0.3s, ~0.18s apart. One-shot.
    const VibroStep s[] = {{300, 180}, {300, 180}, {300, 0}};
    startPattern(s, 3, DUTY_SOFT, false);
  } else if (!strcmp(k, "DONE")) {
    // 5 pulses of 0.2s with alternating 0.2/0.4 gaps (a soft heartbeat), then a
    // 0.9s rest before it repeats. Loops until the daemon sends OFF (you focus).
    const VibroStep s[] = {{200, 200}, {200, 400}, {200, 200}, {200, 400}, {200, 900}};
    startPattern(s, 5, DUTY_SOFT, true);
  } else if (!strcmp(k, "INPUT")) {
    // Soft double-tap. One-shot; the daemon re-taps every ~10s until you focus.
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

static void render() {
  if (showIdle) drawIdle();
  else          drawCard();
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
      curState = ST_IDLE;
      render();
      break;

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

// Debounce one button. Emits "B|<n>" on a press (HIGH->LOW transition).
static void pollButton(uint8_t pin, uint8_t n, bool &stable,
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
    if (pressedNow && !wasPressed) {
      // Edge into pressed -> emit event.
      Serial.print(F("B|"));
      Serial.println(n);
    }
    stable = !pressedNow ? true : false; // stable=true means released
  }
}

static void pollButtons() {
  pollButton(PIN_BTN_FOCUS, 1, focusStable, focusLastRaw, focusEdgeMs);
  pollButton(PIN_BTN_NEXT,  2, nextStable,  nextLastRaw,  nextEdgeMs);
  pollButton(PIN_BTN_PREV,  3, prevStable,  prevLastRaw,  prevEdgeMs);
}

// -----------------------------------------------------------------------------
// Setup / loop
// -----------------------------------------------------------------------------

void setup() {
  pinMode(PIN_BTN_FOCUS, INPUT_PULLUP);
  pinMode(PIN_BTN_NEXT,  INPUT_PULLUP);
  pinMode(PIN_BTN_PREV,  INPUT_PULLUP);
  pinMode(PIN_VIBRO,     OUTPUT);
  digitalWrite(PIN_VIBRO, LOW);          // motor off at boot
  lastRxMs = millis();                   // seed the haptic loop watchdog

  Serial.begin(SERIAL_BAUD);

  // Init the OLED. If it fails, buzz the vibro motor so a wiring fault is FELT.
  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println(F("OLED init FAILED (try 0x3D)"));
    for (;;) {
      // A few short buzzes, then a pause -- repeats forever. Hand-rolled here
      // (the haptic engine is not pumped in this dead-end loop).
      for (uint8_t i = 0; i < 3; i++) {
        digitalWrite(PIN_VIBRO, HIGH);
        delay(80);
        digitalWrite(PIN_VIBRO, LOW);
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

  // Blink the unacknowledged-alert dot (~every 400ms). Only that card actually
  // changes with the blink phase, so re-render only then.
  bool nb = (millis() / 400) & 1;
  if (nb != gBlinkOn) {
    gBlinkOn = nb;
    if (!showIdle && isAlertState(curState) && !curAck) render();
  }
}
