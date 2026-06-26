/*
 * Claude Mate - SELF-TEST sketch (standalone, NO host/daemon needed)
 * ==================================================================
 *
 * Use this sketch to verify the motor + driver + endstop + OLED + buttons of the
 * STATUS WHEEL build WITHOUT a Mac, Python daemon or Claude Code hooks.
 *
 * What it does:
 *   - On boot, runs the two-stage endstop HOMING routine (same as the main
 *     firmware), defining FREE as step 0.
 *   - Rotates the wheel FREE -> WIP -> BLOCKED -> WTF in turn, ~every 2s,
 *     printing each target word and the live endstop state over USB serial.
 *   - Renders the current word on the OLED as a big label.
 *   - Prints a line over serial whenever a button is pressed:
 *       "FOCUS button (D2) pressed"  /  "NEXT button (D3) pressed"
 *     Open the Serial Monitor at 115200 to watch the events.
 *
 * REQUIRED LIBRARIES (Arduino Library Manager):
 *   - AccelStepper             (by Mike McCauley)
 *   - Adafruit GFX Library     (Adafruit_GFX)
 *   - Adafruit SSD1306         (Adafruit_SSD1306)
 *
 * DRIVER SELECTION (one-line change): default ULN2003 (28BYJ-48).
 *   #define DRIVER_ULN2003   // DEFAULT
 *   #define DRIVER_A4988     // NEMA17 + A4988 STEP/DIR
 *
 * PIN MAP (identical to the main firmware, Arduino Nano / ATmega328P):
 *   OLED SSD1306 128x64 over I2C: VCC->5V, GND->GND, SDA->A4, SCL->A5
 *     I2C address 0x3C is used here. If your module is unresponsive, try the
 *     common alternative 0x3D (change SCREEN_ADDRESS below). Many cheap
 *     SSD1306 boards are 0x3C; some are 0x3D depending on a solder jumper.
 *   Buttons (INPUT_PULLUP, other leg to GND): FOCUS -> D2, NEXT -> D3
 *   Endstop (INPUT_PULLUP, pressed = LOW): ENDSTOP -> D4 (wheel tab @ FREE)
 *   Stepper: ULN2003 IN1->D5, IN2->D6, IN3->D7, IN4->D8
 *            (or A4988 STEP->D5, DIR->D6, EN->D7)
 *
 * POWER: drive the motor/driver from the USB 5V rail directly (28BYJ-48
 *   ~240mA) or an external 12V supply (NEMA17/A4988) -- NOT through the Nano
 *   onboard regulator -- to avoid brown-out resets. Common all grounds.
 */

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <AccelStepper.h>

// ---- Driver selection --------------------------------------------------------
#define DRIVER_ULN2003
//#define DRIVER_A4988

#if defined(DRIVER_ULN2003) && defined(DRIVER_A4988)
#error "Define only ONE of DRIVER_ULN2003 / DRIVER_A4988"
#endif
#if !defined(DRIVER_ULN2003) && !defined(DRIVER_A4988)
#error "Define one of DRIVER_ULN2003 / DRIVER_A4988"
#endif

// ---- OLED configuration ------------------------------------------------------
#define SCREEN_WIDTH   128
#define SCREEN_HEIGHT  64
#define OLED_RESET     -1
#define SCREEN_ADDRESS 0x3C    // fallback / common alternative: 0x3D

// ---- Pin map -----------------------------------------------------------------
#define PIN_BTN_FOCUS  2       // D2
#define PIN_BTN_NEXT   3       // D3
#define PIN_ENDSTOP    4       // D4, INPUT_PULLUP, pressed = LOW

#if defined(DRIVER_ULN2003)
#define PIN_IN1        5
#define PIN_IN2        6
#define PIN_IN3        7
#define PIN_IN4        8
AccelStepper stepper(AccelStepper::HALF4WIRE, PIN_IN1, PIN_IN3, PIN_IN2, PIN_IN4);
#define STEPS_PER_REV  4096L
#define MAX_SPEED      700.0
#define ACCEL          400.0
#define HOME_SPEED     200.0
#elif defined(DRIVER_A4988)
#define PIN_STEP       5
#define PIN_DIR        6
#define PIN_EN         7       // active LOW
AccelStepper stepper(AccelStepper::DRIVER, PIN_STEP, PIN_DIR);
#define STEPS_PER_REV  3200L
#define MAX_SPEED      1600.0
#define ACCEL          1200.0
#define HOME_SPEED     400.0
#endif

// ---- Wheel word positions ----------------------------------------------------
#define POS_FREE     (0L)
#define POS_WIP      (STEPS_PER_REV / 4)
#define POS_BLOCKED  (STEPS_PER_REV / 2)
#define POS_WTF      (3L * STEPS_PER_REV / 4)

// ---- Homing parameters -------------------------------------------------------
#define HOMING_DIR   (1)
#define OFF_STEPS    (STEPS_PER_REV / 16)
#define HOME_GUARD   (3L * STEPS_PER_REV / 2)

#define SERIAL_BAUD    115200
#define STEP_MS        2000UL  // advance the wheel word every ~2s
#define DEBOUNCE_MS    200UL
#define ENDSTOP_DEBOUNCE_MS 20UL

enum Word { W_FREE = 0, W_WIP, W_BLOCKED, W_WTF };
const uint8_t WORD_COUNT = 4;

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

uint8_t wordIdx = W_FREE;
unsigned long lastStepMs = 0;

// Endstop debounce.
int           endstopLastRaw = HIGH;
bool          endstopPressed = false;
unsigned long endstopEdgeMs  = 0;

// Button debounce state.
bool          focusStable = true, nextStable = true;
int           focusLastRaw = HIGH, nextLastRaw = HIGH;
unsigned long focusEdgeMs = 0, nextEdgeMs = 0;

static const __FlashStringHelper *wordLabel(uint8_t w) {
  switch (w) {
    case W_WIP:     return F("WIP");
    case W_BLOCKED: return F("BLOCKED");
    case W_WTF:     return F("WTF");
    default:        return F("FREE");
  }
}

static long wordTarget(uint8_t w) {
  switch (w) {
    case W_WIP:     return POS_WIP;
    case W_BLOCKED: return POS_BLOCKED;
    case W_WTF:     return POS_WTF;
    default:        return POS_FREE;
  }
}

static long modRev(long v) {
  long m = v % STEPS_PER_REV;
  if (m < 0) m += STEPS_PER_REV;
  return m;
}

static void moveToWord(uint8_t w) {
  long target = wordTarget(w);
  long cur = modRev(stepper.currentPosition());
  stepper.setCurrentPosition(cur);
  long diff = target - cur;
  if (diff > STEPS_PER_REV / 2)  diff -= STEPS_PER_REV;
  if (diff < -STEPS_PER_REV / 2) diff += STEPS_PER_REV;
  stepper.moveTo(cur + diff);
}

static void snapHome() {
  long remaining = stepper.targetPosition() - stepper.currentPosition();
  stepper.setCurrentPosition(0);
  stepper.moveTo(modRev(remaining));
}

static bool pollEndstop() {
  int raw = digitalRead(PIN_ENDSTOP);
  unsigned long now = millis();
  if (raw != endstopLastRaw) {
    endstopLastRaw = raw;
    endstopEdgeMs = now;
  }
  bool edge = false;
  if ((now - endstopEdgeMs) >= ENDSTOP_DEBOUNCE_MS) {
    bool pressedNow = (raw == LOW);
    if (pressedNow && !endstopPressed) edge = true;
    endstopPressed = pressedNow;
  }
  return edge;
}

static bool endstopDown() {
  if (digitalRead(PIN_ENDSTOP) != LOW) return false;
  delay(2);
  return (digitalRead(PIN_ENDSTOP) == LOW);
}

// Two-stage homing (blocking; only runs once at boot). Aborts after ~1.5 rev.
static void homeWheel() {
  Serial.println(F("homing..."));
  long moved;

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

  stepper.setCurrentPosition(0);
  stepper.setMaxSpeed(MAX_SPEED);
  stepper.setAcceleration(ACCEL);
  stepper.runToNewPosition(-(long)HOMING_DIR * OFF_STEPS);

  stepper.setCurrentPosition(0);
  stepper.setSpeed((float)HOMING_DIR * HOME_SPEED);
  found = false;
  long slowGuard = OFF_STEPS * 4L + 32;
  while ((moved = labs(stepper.currentPosition())) < slowGuard) {
    if (endstopDown()) { found = true; break; }
    stepper.runSpeed();
  }
  if (!found) {
    Serial.println(F("WARN: re-approach missed endstop; assuming FREE"));
  }

  stepper.setMaxSpeed(MAX_SPEED);
  stepper.setAcceleration(ACCEL);
  stepper.setCurrentPosition(0);
  stepper.moveTo(0);
  endstopPressed = true;
  endstopLastRaw = digitalRead(PIN_ENDSTOP);
  Serial.println(F("homed: FREE"));
}

static void drawWord(uint8_t w) {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print(F("selftest: wheel"));

  display.setTextSize(2);
  display.setCursor(0, 24);
  display.print(wordLabel(w));

  display.setTextSize(1);
  display.setCursor(0, 48);
  display.print(F("endstop: "));
  display.print((digitalRead(PIN_ENDSTOP) == LOW) ? F("DOWN") : F("up"));

  display.setCursor(0, 56);
  display.print((uint8_t)(w + 1));
  display.print('/');
  display.print(WORD_COUNT);

  display.display();
}

static void pollButton(uint8_t pin, const __FlashStringHelper *label,
                       bool &stable, int &lastRaw, unsigned long &edgeMs) {
  int raw = digitalRead(pin);
  unsigned long now = millis();
  if (raw != lastRaw) {
    lastRaw = raw;
    edgeMs = now;
  }
  if ((now - edgeMs) >= DEBOUNCE_MS) {
    bool pressedNow = (raw == LOW);
    bool wasPressed = !stable;
    if (pressedNow && !wasPressed) {
      Serial.println(label);
    }
    stable = !pressedNow;
  }
}

void setup() {
  pinMode(PIN_BTN_FOCUS, INPUT_PULLUP);
  pinMode(PIN_BTN_NEXT,  INPUT_PULLUP);
  pinMode(PIN_ENDSTOP,   INPUT_PULLUP);
#ifdef DRIVER_A4988
  pinMode(PIN_EN, OUTPUT);
  digitalWrite(PIN_EN, LOW);             // enable driver (active LOW)
#endif

  Serial.begin(SERIAL_BAUD);
  Serial.println(F("Claude Mate self-test (status wheel) starting..."));

  stepper.setMaxSpeed(MAX_SPEED);
  stepper.setAcceleration(ACCEL);

  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println(F("OLED init FAILED at 0x3C - try 0x3D"));
    for (;;) { delay(500); }             // halt; nothing else to test visually
  }
  Serial.println(F("OLED OK."));

  // Home first, then cycle the words.
  homeWheel();

  wordIdx = W_FREE;
  moveToWord(wordIdx);
  Serial.print(F("target: "));
  Serial.println(wordLabel(wordIdx));
  drawWord(wordIdx);

  lastStepMs = millis();
  Serial.println(F("cycling wheel; press buttons to test."));
}

void loop() {
  unsigned long now = millis();

  // Advance the target word every STEP_MS, but only once the wheel has settled.
  if ((now - lastStepMs) >= STEP_MS && stepper.distanceToGo() == 0) {
    lastStepMs = now;
    wordIdx = (wordIdx + 1) % WORD_COUNT;
    moveToWord(wordIdx);
    Serial.print(F("target: "));
    Serial.print(wordLabel(wordIdx));
    Serial.print(F("  endstop="));
    Serial.println((digitalRead(PIN_ENDSTOP) == LOW) ? F("DOWN") : F("up"));
    drawWord(wordIdx);
  }

  // Drift correction: snap to home whenever the tab presses the endstop.
  if (pollEndstop()) {
    snapHome();
  }

  pollButton(PIN_BTN_FOCUS, F("FOCUS button (D2) pressed"),
             focusStable, focusLastRaw, focusEdgeMs);
  pollButton(PIN_BTN_NEXT,  F("NEXT button (D3) pressed"),
             nextStable,  nextLastRaw,  nextEdgeMs);

  stepper.run();
}
