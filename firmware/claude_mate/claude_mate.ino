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
 *   OLED SSD1306 128x32 (0.91") over I2C:
 *     VCC -> 5V, GND -> GND, SDA -> A4, SCL -> A5
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
 *     S|<idx>|<total>|<name>|<state>|<runtime>|<limit>|<ack>
 *         state = working | waiting | error | done | idle
 *         ack   = 1 acknowledged / 0 not (optional; alert states show a dot:
 *                 filled+blinking = unacknowledged, hollow = acknowledged)
 *     I                                              idle screen (no sessions)
 *     P                                              ping/keepalive (we reply H)
 *     V|<kind>                                       buzz now; kind = START|DONE|
 *                                                    INPUT|ERROR (haptic only)
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

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ---- OLED configuration ------------------------------------------------------
#define SCREEN_WIDTH   128
#define SCREEN_HEIGHT  32     // 0.91" SSD1306 (use 64 for a 0.96" panel)
#define OLED_RESET     -1      // share Arduino reset pin (no dedicated reset)
#define SCREEN_ADDRESS 0x3C    // common alternative: 0x3D

// ---- Pin map -----------------------------------------------------------------
#define PIN_BTN_FOCUS  2       // D2, INPUT_PULLUP, emits B|1
#define PIN_BTN_NEXT   3       // D3, INPUT_PULLUP, emits B|2
#define PIN_BTN_PREV   4       // D4, INPUT_PULLUP, emits B|3
#define PIN_VIBRO      5       // D5, OUTPUT, drives the vibration motor (HIGH=on)

// ---- Protocol constants ------------------------------------------------------
#define SERIAL_BAUD    115200
#define LINE_MAX       72      // cap input line length to bound RAM use
#define DEBOUNCE_MS    200UL   // ~200ms button debounce

// Status words. Index order matches escalation FREE<WIP<BLOCKED<WTF.
enum Word { W_FREE = 0, W_WIP, W_BLOCKED, W_WTF };

// Session states (parsed from the S command)
enum State { ST_IDLE = 0, ST_WORKING, ST_WAITING, ST_ERROR, ST_DONE };

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// ---- Serial line assembly ----------------------------------------------------
static char  lineBuf[LINE_MAX];
static uint8_t lineLen = 0;
static bool  lineOverflow = false;   // drop the rest of an over-long line

// ---- Current display model ---------------------------------------------------
static char  curName[21]  = {0};     // up to 20 chars + NUL
static char  curRuntime[8] = {0};
static char  curLimit[8]  = {0};
static uint8_t curIdx   = 0;
static uint8_t curTotal = 0;
static uint8_t curState = ST_IDLE;
static bool  curAck     = true;      // alert acknowledged (focused)? -> dot style
static bool  showIdle   = true;      // true => idle screen, no card
static bool  gBlinkOn   = true;      // blink phase for the unacknowledged dot

// ---- Status word -------------------------------------------------------------
static uint8_t curWord = W_FREE;     // current OLED word

// ---- Vibration (haptic) state machine ----------------------------------------
static uint8_t       vibroPulses   = 0;     // pulses remaining (incl. current)
static bool          vibroOn       = false; // motor currently energised?
static uint16_t      vibroOnMs     = 0;     // on-duration for the active pattern
static uint16_t      vibroOffMs    = 0;     // gap between pulses
static uint8_t       vibroDuty     = 255;   // PWM amplitude (0-255) of the pattern
static uint8_t       vibroDutyLast = 255;   // amplitude of the FINAL pulse (accent)
static unsigned long vibroPhaseMs  = 0;     // millis() at the last phase change

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

// Kick off a haptic pattern: `pulses` buzzes of `onMs` each, separated by
// `offMs` gaps, at PWM amplitude `duty` (0-255). Returns immediately; the
// pulses play out in pollVibro(). A new call replaces any pattern in progress.
// PIN_VIBRO (D5) is PWM-capable, so duty sets how hard the motor pushes --
// lower = gentler. analogWrite on D5 does not disturb millis().
// `duty` is the amplitude of every pulse except the LAST, which uses `dutyLast`
// -- so a pattern can be e.g. soft then hard ("felt") in one buzz.
static void startBuzz(uint8_t pulses, uint16_t onMs, uint16_t offMs,
                      uint8_t duty, uint8_t dutyLast) {
  if (pulses == 0) {                   // nothing to do -- make sure motor is off
    vibroPulses = 0;
    vibroOn = false;
    analogWrite(PIN_VIBRO, 0);
    return;
  }
  vibroPulses   = pulses;
  vibroOnMs     = onMs;
  vibroOffMs    = offMs;
  vibroDuty     = duty;
  vibroDutyLast = dutyLast;
  vibroOn       = true;                 // start the first pulse now
  vibroPhaseMs  = millis();
  // First pulse: if it is also the last (pulses==1) use the accent amplitude.
  analogWrite(PIN_VIBRO, pulses == 1 ? dutyLast : duty);
}

// Advance the haptic state machine. Call every loop(); never blocks.
static void pollVibro() {
  if (vibroPulses == 0) return;        // idle: nothing playing
  unsigned long now = millis();
  if (vibroOn) {
    if ((now - vibroPhaseMs) >= vibroOnMs) {
      // End of the on-phase: drop the motor and start the gap (or finish).
      analogWrite(PIN_VIBRO, 0);
      vibroOn = false;
      vibroPhaseMs = now;
      vibroPulses--;                   // this pulse is complete
      if (vibroPulses == 0) return;    // last pulse done -- stay off
    }
  } else {
    if ((now - vibroPhaseMs) >= vibroOffMs) {
      // Gap elapsed: start the next pulse. The final pulse gets the accent
      // amplitude (vibroPulses == 1 means the one we're about to play is last).
      analogWrite(PIN_VIBRO, vibroPulses == 1 ? vibroDutyLast : vibroDuty);
      vibroOn = true;
      vibroPhaseMs = now;
    }
  }
}

// Daemon-driven haptic patterns, gentlest -> firmest. The daemon decides WHEN
// to buzz (per session, with repeats) and sends V|<KIND>; the firmware only
// plays the pattern. Amplitude is PWM duty, kept well below full power so even
// the firmest is a nudge, not a jolt -- urgency reads as a bit more push and a
// few more pulses, not a longer jarring buzz.
static void buzzForKind(const char *k) {
  //                              pulses onMs offMs duty dutyLast
  if      (!strcmp(k, "START")) startBuzz(1, 25,   0,  70,  70);  // job started: a tick
  else if (!strcmp(k, "DONE"))  startBuzz(2, 70,  45,  90, 255);  // finished: soft then HARD
  else if (!strcmp(k, "INPUT")) startBuzz(3, 50,  80, 150, 150);  // needs you (repeats 10s)
  else if (!strcmp(k, "ERROR")) startBuzz(4, 60,  80, 200, 200);  // error     (repeats 5s)
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

  // ---- Bottom line (size 2): STATUS + time ----
  display.setTextSize(2);
  display.setCursor(0, 14);
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

    case 'S': {                          // S|idx|total|name|state|runtime|limit|ack
      // Tokenize in place on '|'. 7 core tokens (type + 6) + optional 8th 'ack'.
      char *fields[8];
      uint8_t n = 0;
      char *p = line;
      fields[n++] = p;                   // fields[0] = "S"
      while (n < 8) {
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
