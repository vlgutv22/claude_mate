/*
 * Claude Mate - Arduino Nano firmware (main sketch)
 * ==================================================
 *
 * A USB hardware companion that shows the live status of Claude Code sessions.
 * This sketch drives a 128x64 SSD1306 I2C OLED, a STEPPER-DRIVEN STATUS WHEEL
 * and two buttons. It speaks the daemon<->Arduino serial protocol over USB CDC
 * serial at 115200 baud, 8N1.
 *
 * The status wheel replaces the old 3-LED traffic light. A tab on the wheel
 * clicks an endstop microswitch at the FREE (home) position. The wheel carries
 * four words spaced 90 deg apart so that rotation == escalation:
 *
 *     FREE (0 deg, at the endstop) -> WIP (90) -> BLOCKED (180) -> WTF (270)
 *
 *   FREE     no session needs you (all idle/done, or no sessions). HOME.
 *   WIP      at least one session working (none blocked/errored).
 *   BLOCKED  at least one session waiting for your input (none errored).
 *   WTF      at least one session in error (StopFailure / API 5xx / timeout).
 *   Priority when several apply: WTF > BLOCKED > WIP > FREE.
 *
 * REQUIRED LIBRARIES (install via the Arduino Library Manager):
 *   - AccelStepper             (by Mike McCauley) -- non-blocking stepping
 *   - Adafruit GFX Library     (Adafruit_GFX)
 *   - Adafruit SSD1306         (Adafruit_SSD1306)
 *   Adafruit GFX/SSD1306 pull in Adafruit BusIO + Wire (bundled with the IDE).
 *
 * DRIVER SELECTION (one-line change at the top of this file):
 *   #define DRIVER_ULN2003   // DEFAULT: 28BYJ-48 geared motor + ULN2003 board
 *   #define DRIVER_A4988     // alternative: NEMA17 + A4988/DRV8825 STEP/DIR
 *
 * PIN MAP (Arduino Nano, ATmega328P):
 *   OLED SSD1306 128x64 over I2C:
 *     VCC -> 5V, GND -> GND, SDA -> A4, SCL -> A5
 *     I2C address 0x3C (common alternative: 0x3D)
 *   Buttons (INPUT_PULLUP, other leg to GND):
 *     FOCUS button -> D2   (emits "B|1")
 *     NEXT  button -> D3   (emits "B|2")
 *   Endstop microswitch (INPUT_PULLUP, pressed = LOW):
 *     ENDSTOP      -> D4   (the wheel tab clicks it at FREE / home)
 *   Stepper:
 *     ULN2003 (28BYJ-48): IN1->D5, IN2->D6, IN3->D7, IN4->D8
 *                         (driven in AccelStepper order IN1,IN3,IN2,IN4)
 *     A4988 (NEMA17):     STEP->D5, DIR->D6, EN->D7 (EN active LOW)
 *   Optional piezo buzzer:
 *     BUZZER       -> D9   (only when ENABLE_BUZZER is defined; chirps on WTF)
 *
 * POWER (IMPORTANT - avoid brown-out resets):
 *   Power the motor/driver from the USB 5V rail DIRECTLY (the 28BYJ-48 draws
 *   ~240 mA) or from an external supply (12V for a NEMA17 via A4988) -- NOT
 *   through the Nano's onboard 5V regulator. The regulator cannot source the
 *   stepper current and the resulting voltage sag will reset the board mid-move.
 *   Tie all grounds together (Nano GND, driver GND, supply GND).
 *
 * SERIAL PROTOCOL (115200 8N1, ASCII lines terminated by '\n', fields split '|')
 *   Daemon -> Arduino:
 *     D|<word>                                       word = FREE|WIP|BLOCKED|WTF
 *     S|<idx>|<total>|<name>|<state>|<runtime>|<limit>
 *         state = working | waiting | error | done | idle
 *     I                                              idle screen (no sessions)
 *     P                                              ping/keepalive (we reply H)
 *   Arduino -> Daemon:
 *     H                                              hello, sent once after boot
 *     B|<n>                                          n=1 FOCUS, n=2 NEXT
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
#include <AccelStepper.h>

// ---- Driver selection --------------------------------------------------------
// Pick exactly ONE. ULN2003 (28BYJ-48) is the default. Switching is one line.
#define DRIVER_ULN2003
//#define DRIVER_A4988

#if defined(DRIVER_ULN2003) && defined(DRIVER_A4988)
#error "Define only ONE of DRIVER_ULN2003 / DRIVER_A4988"
#endif
#if !defined(DRIVER_ULN2003) && !defined(DRIVER_A4988)
#error "Define one of DRIVER_ULN2003 / DRIVER_A4988"
#endif

// ---- Optional buzzer ---------------------------------------------------------
// Uncomment to enable a short chirp when the wheel enters the WTF state.
//#define ENABLE_BUZZER

// ---- OLED configuration ------------------------------------------------------
#define SCREEN_WIDTH   128
#define SCREEN_HEIGHT  64
#define OLED_RESET     -1      // share Arduino reset pin (no dedicated reset)
#define SCREEN_ADDRESS 0x3C    // common alternative: 0x3D

// ---- Pin map -----------------------------------------------------------------
#define PIN_BTN_FOCUS  2       // D2, INPUT_PULLUP, emits B|1
#define PIN_BTN_NEXT   3       // D3, INPUT_PULLUP, emits B|2
#define PIN_ENDSTOP    4       // D4, INPUT_PULLUP, pressed = LOW (home/FREE)
#define PIN_BUZZER     9       // D9 (optional)

#if defined(DRIVER_ULN2003)
// 28BYJ-48 + ULN2003. NOTE the half-step pin order is IN1,IN3,IN2,IN4.
#define PIN_IN1        5       // D5
#define PIN_IN2        6       // D6
#define PIN_IN3        7       // D7
#define PIN_IN4        8       // D8
AccelStepper stepper(AccelStepper::HALF4WIRE, PIN_IN1, PIN_IN3, PIN_IN2, PIN_IN4);
#define STEPS_PER_REV  4096L   // 28BYJ-48 in half-step (8-step seq * 64 * 8)
#define MAX_SPEED      700.0
#define ACCEL          400.0
#define HOME_SPEED     200.0   // slow re-approach speed
#elif defined(DRIVER_A4988)
// NEMA17 + A4988/DRV8825 STEP/DIR.
#define PIN_STEP       5       // D5
#define PIN_DIR        6       // D6
#define PIN_EN         7       // D7 (enable, active LOW)
AccelStepper stepper(AccelStepper::DRIVER, PIN_STEP, PIN_DIR);
#define STEPS_PER_REV  3200L   // 200 full steps * 16 microsteps -- tune to taste
#define MAX_SPEED      1600.0
#define ACCEL          1200.0
#define HOME_SPEED     400.0
#endif

// ---- Wheel word positions (steps from FREE/home) -----------------------------
// FREE at the endstop; rotation = escalation: FREE -> WIP -> BLOCKED -> WTF.
#define POS_FREE     (0L)
#define POS_WIP      (STEPS_PER_REV / 4)
#define POS_BLOCKED  (STEPS_PER_REV / 2)
#define POS_WTF      (3L * STEPS_PER_REV / 4)

// ---- Homing parameters -------------------------------------------------------
#define HOMING_DIR   (1)               // +1 = CW search toward the endstop
#define OFF_STEPS    (STEPS_PER_REV / 16)   // back-off before slow re-approach
#define HOME_GUARD   (3L * STEPS_PER_REV / 2)   // ~1.5 rev guard before abort

// ---- Protocol constants ------------------------------------------------------
#define SERIAL_BAUD    115200
#define LINE_MAX       72      // cap input line length to bound RAM use
#define DEBOUNCE_MS    200UL   // ~200ms button debounce
#define ENDSTOP_DEBOUNCE_MS 20UL    // endstop edge debounce

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
static char  curName[11]  = {0};     // up to 10 chars + NUL
static char  curRuntime[8] = {0};
static char  curLimit[8]  = {0};
static uint8_t curIdx   = 0;
static uint8_t curTotal = 0;
static uint8_t curState = ST_IDLE;
static bool  showIdle   = true;      // true => idle screen, no card

// ---- Wheel state -------------------------------------------------------------
static uint8_t curWord = W_FREE;     // current target word (also OLED label)

// ---- Endstop debounce state --------------------------------------------------
static int           endstopLastRaw = HIGH;  // pull-up idle = HIGH (released)
static bool          endstopPressed = false; // debounced "currently pressed"
static unsigned long endstopEdgeMs  = 0;

// ---- Button debounce state ---------------------------------------------------
static bool          focusStable = true;   // pull-up idle = HIGH (released)
static bool          nextStable  = true;
static unsigned long focusEdgeMs = 0;
static unsigned long nextEdgeMs  = 0;
static int           focusLastRaw = HIGH;
static int           nextLastRaw  = HIGH;

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

// Word label for the OLED text fallback / confirmation of the dial.
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

// Target step position (absolute, normalized 0..STEPS_PER_REV-1) for a word.
static long wordTarget(uint8_t w) {
  switch (w) {
    case W_WIP:     return POS_WIP;
    case W_BLOCKED: return POS_BLOCKED;
    case W_WTF:     return POS_WTF;
    default:        return POS_FREE;
  }
}

// Positive modulo into [0, STEPS_PER_REV).
static long modRev(long v) {
  long m = v % STEPS_PER_REV;
  if (m < 0) m += STEPS_PER_REV;
  return m;
}

#ifdef ENABLE_BUZZER
static void chirp() {
  // Short two-tone chirp, blocking but brief (~110ms total).
  tone(PIN_BUZZER, 1800, 60);
  delay(70);
  tone(PIN_BUZZER, 2400, 40);
}
#endif

// -----------------------------------------------------------------------------
// Wheel motion (non-blocking)
// -----------------------------------------------------------------------------

// Move the wheel to `w` by the SHORTEST modular path. We keep the AccelStepper
// reference frame normalized: currentPosition() is held in [0, STEPS_PER_REV).
static void moveToWord(uint8_t w) {
  long target = wordTarget(w);
  long cur = modRev(stepper.currentPosition());
  // Re-anchor currentPosition to its normalized value so positions never grow
  // unbounded (this is a no-op if cur already equals the raw position).
  stepper.setCurrentPosition(cur);

  long diff = target - cur;            // in (-STEPS_PER_REV, STEPS_PER_REV)
  // Choose the shorter direction around the full circle.
  if (diff > STEPS_PER_REV / 2)  diff -= STEPS_PER_REV;
  if (diff < -STEPS_PER_REV / 2) diff += STEPS_PER_REV;
  stepper.moveTo(cur + diff);
}

// Snap the absolute reference to home (FREE) when the tab passes the endstop,
// so any accumulated drift self-heals. We preserve the in-progress relative
// move: shift both currentPosition and targetPosition by the same correction.
static void snapHome() {
  long cur = modRev(stepper.currentPosition());
  // The tab presses the switch at FREE (step 0). Whatever the counter thinks,
  // declare "here" to be 0 and carry the remaining distance of the active move.
  long remaining = stepper.targetPosition() - stepper.currentPosition();
  stepper.setCurrentPosition(0);       // clears speed; re-issue the target
  stepper.moveTo(modRev(remaining));   // keep heading where we were going
}

// -----------------------------------------------------------------------------
// Endstop (debounced) -- used for homing and continuous drift correction
// -----------------------------------------------------------------------------

// Update the debounced endstop state. Returns true on a fresh press edge.
static bool pollEndstop() {
  int raw = digitalRead(PIN_ENDSTOP);
  unsigned long now = millis();
  if (raw != endstopLastRaw) {
    endstopLastRaw = raw;
    endstopEdgeMs = now;
  }
  bool edge = false;
  if ((now - endstopEdgeMs) >= ENDSTOP_DEBOUNCE_MS) {
    bool pressedNow = (raw == LOW);    // pull-up: LOW = pressed
    if (pressedNow && !endstopPressed) edge = true;  // rising edge into pressed
    endstopPressed = pressedNow;
  }
  return edge;
}

// -----------------------------------------------------------------------------
// Homing (blocking is OK here -- only runs once in setup)
// -----------------------------------------------------------------------------

// Returns true if the endstop is currently pressed (raw, lightly settled read).
static bool endstopDown() {
  if (digitalRead(PIN_ENDSTOP) != LOW) return false;
  delay(2);
  return (digitalRead(PIN_ENDSTOP) == LOW);
}

// Two-stage homing for repeatability. Aborts (assumes FREE) if the endstop is
// not found within ~1.5 revolutions, so we never spin forever.
static void homeWheel() {
  Serial.println(F("homing..."));

  long moved;

  // ---- Stage 1: fast approach toward the endstop ----
  // Count ACTUAL steps (via currentPosition deltas), not loop iterations, so
  // the ~1.5-rev guard reflects real wheel travel.
  stepper.setMaxSpeed(MAX_SPEED);
  stepper.setCurrentPosition(0);
  stepper.setSpeed((float)HOMING_DIR * MAX_SPEED * 0.6);
  bool found = false;
  while ((moved = labs(stepper.currentPosition())) < HOME_GUARD) {
    if (endstopDown()) { found = true; break; }
    stepper.runSpeed();
  }
  if (!found) {
    Serial.println(F("WARN: endstop not found; assuming FREE"));
    stepper.setMaxSpeed(MAX_SPEED);
    stepper.setAcceleration(ACCEL);
    stepper.setCurrentPosition(0);
    return;
  }

  // ---- Stage 2: back off, then slow re-approach for a precise edge ----
  stepper.setCurrentPosition(0);
  stepper.setMaxSpeed(MAX_SPEED);
  stepper.setAcceleration(ACCEL);
  stepper.runToNewPosition(-(long)HOMING_DIR * OFF_STEPS);  // blocking back-off

  stepper.setCurrentPosition(0);
  stepper.setSpeed((float)HOMING_DIR * HOME_SPEED);
  found = false;
  long slowGuard = OFF_STEPS * 4L + 32;   // a little past the back-off distance
  while ((moved = labs(stepper.currentPosition())) < slowGuard) {
    if (endstopDown()) { found = true; break; }
    stepper.runSpeed();
  }
  if (!found) {
    Serial.println(F("WARN: re-approach missed endstop; assuming FREE"));
  }

  // Define this spot as FREE / step 0.
  stepper.setMaxSpeed(MAX_SPEED);
  stepper.setAcceleration(ACCEL);
  stepper.setCurrentPosition(0);
  stepper.moveTo(0);
  endstopPressed = true;     // we know we're on the switch right now
  endstopLastRaw = digitalRead(PIN_ENDSTOP);
  Serial.println(F("homed: FREE"));
}

// -----------------------------------------------------------------------------
// Rendering
// -----------------------------------------------------------------------------

// Top-right corner: always show the current dial word as a text confirmation.
static void drawWordBadge() {
  // Right-align the word in a small size-1 strip at the very top.
  const __FlashStringHelper *w = wordLabel(curWord);
  // Width of a size-1 char is 6px; longest word "BLOCKED" = 7 chars = 42px.
  display.setTextSize(1);
  int16_t bx, by; uint16_t bw, bh;
  display.getTextBounds(w, 0, 0, &bx, &by, &bw, &bh);
  int16_t x = SCREEN_WIDTH - (int16_t)bw;
  if (x < 0) x = 0;
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(x, 0);
  display.print(w);
}

static void drawIdle() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.println(F("Claude Mate"));
  display.setTextSize(2);
  display.setCursor(0, 24);
  display.println(F("idle"));
  display.setTextSize(1);
  display.setCursor(0, 56);
  display.print(F("no sessions"));
  drawWordBadge();
  display.display();
}

static void drawCard() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  // Dial word badge, top-right.
  drawWordBadge();

  // Big name on top (size 2 = 16px tall). Truncate defensively to fit width.
  display.setTextSize(2);
  display.setCursor(0, 0);
  // 128px / 12px per size-2 char = 10 chars; name is already <=10.
  display.print(curName);

  // State line.
  display.setTextSize(2);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 24);
  display.print(stateLabel(curState));

  // Runtime + limit line (size 1).
  display.setTextSize(1);
  display.setCursor(0, 46);
  display.print(F("t "));
  display.print(curRuntime[0] ? curRuntime : "-");
  display.print(F("  lim "));
  display.print(curLimit[0] ? curLimit : "-");

  // Small idx/total indicator, bottom-left.
  display.setCursor(0, 56);
  display.print(curIdx);
  display.print('/');
  display.print(curTotal);

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

    case 'D': {                          // D|<WORD> -- set the dial
      char *bar = strchr(line, '|');
      if (!bar || bar[1] == 0) break;
      uint8_t w = parseWord(bar + 1);
      if (w == 255) break;               // ignore unknown word
#ifdef ENABLE_BUZZER
      if (w == W_WTF && curWord != W_WTF) chirp();
#endif
      curWord = w;
      moveToWord(w);                     // non-blocking; pumped in loop()
      render();                          // refresh the OLED word badge now
      break;
    }

    case 'S': {                          // S|idx|total|name|state|runtime|limit
      // Tokenize in place on '|'. We expect 7 tokens (type + 6 fields).
      char *fields[7];
      uint8_t n = 0;
      char *p = line;
      fields[n++] = p;                   // fields[0] = "S"
      while (n < 7) {
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
}

// -----------------------------------------------------------------------------
// Setup / loop
// -----------------------------------------------------------------------------

void setup() {
  pinMode(PIN_BTN_FOCUS, INPUT_PULLUP);
  pinMode(PIN_BTN_NEXT,  INPUT_PULLUP);
  pinMode(PIN_ENDSTOP,   INPUT_PULLUP);
#ifdef ENABLE_BUZZER
  pinMode(PIN_BUZZER, OUTPUT);
#endif
#ifdef DRIVER_A4988
  pinMode(PIN_EN, OUTPUT);
  digitalWrite(PIN_EN, LOW);             // enable the driver (active LOW)
#endif

  Serial.begin(SERIAL_BAUD);

  stepper.setMaxSpeed(MAX_SPEED);
  stepper.setAcceleration(ACCEL);

  // Init the OLED. If it fails, blink the buzzer pin / loop so it's visible.
  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println(F("OLED init FAILED (try 0x3D)"));
    for (;;) {
#ifdef ENABLE_BUZZER
      tone(PIN_BUZZER, 2000, 100);
#endif
      delay(400);
    }
  }

  display.clearDisplay();
  display.display();

  // Home the wheel to FREE before anything else (blocking is OK only here).
  homeWheel();

  // Default to FREE and the idle screen until the daemon talks to us.
  curWord = W_FREE;
  showIdle = true;
  render();

  // Emit hello once, after the display + homing are done, so the daemon resends
  // the full current state (dial + current card).
  Serial.println('H');
}

void loop() {
  pumpSerial();
  pollButtons();

  // Continuous drift correction: whenever the tab presses the endstop, snap the
  // absolute reference to home (FREE) so accumulated drift self-heals.
  if (pollEndstop()) {
    snapHome();
  }

  // Pump the stepper every iteration so motion stays non-blocking.
  stepper.run();
}
