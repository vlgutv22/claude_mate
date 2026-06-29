/*
 * Claude Mate - SELF-TEST sketch (standalone, NO host/daemon needed)
 * ==================================================================
 *
 * Use this sketch to verify the OLED + vibration motor + three buttons of the
 * HAPTIC build WITHOUT a Mac, Python daemon or Claude Code hooks.
 *
 * What it does:
 *   - Cycles the status word FREE -> WIP -> BLOCKED -> WTF in turn, ~every 2s.
 *   - On each change, fires the matching haptic pattern via buzzForWord() so the
 *     vibration motor pulses (FREE = 1 short tick, BLOCKED = 2, WTF = 3, WIP =
 *     silent), exactly like the main firmware.
 *   - Renders the current word big on the 128x32 OLED.
 *   - Prints a line over serial whenever a button is pressed:
 *       "FOCUS button (D2) pressed" / "NEXT button (D3) pressed" /
 *       "PREV button (D4) pressed"
 *     Open the Serial Monitor at 115200 to watch the events.
 *
 * REQUIRED LIBRARIES (Arduino Library Manager):
 *   - Adafruit GFX Library     (Adafruit_GFX)
 *   - Adafruit SSD1306         (Adafruit_SSD1306)
 *
 * PIN MAP (identical to the main firmware, Arduino Nano / ATmega328P):
 *   OLED SSD1306 128x32 (0.91") over I2C: VCC->5V, GND->GND, SDA->A4, SCL->A5
 *     I2C address 0x3C is used here. If your module is unresponsive, try the
 *     common alternative 0x3D (change SCREEN_ADDRESS below). Many cheap
 *     SSD1306 boards are 0x3C; some are 0x3D depending on a solder jumper.
 *   Buttons (INPUT_PULLUP, other leg to GND): FOCUS -> D2, NEXT -> D3, PREV -> D4
 *   Micro vibration motor: VIBRO -> D5 (OUTPUT; via an NPN transistor or one
 *     ULN2003 channel + flyback diode -- do NOT drive the motor straight off a
 *     pin). Tens of mA, so the USB 5V rail is fine. Common all grounds.
 */

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ---- OLED configuration ------------------------------------------------------
#define SCREEN_WIDTH   128
#define SCREEN_HEIGHT  32     // 0.91" SSD1306 (use 64 for a 0.96" panel)
#define OLED_RESET     -1
#define SCREEN_ADDRESS 0x3C    // fallback / common alternative: 0x3D

// ---- Pin map -----------------------------------------------------------------
#define PIN_BTN_FOCUS  2       // D2
#define PIN_BTN_NEXT   3       // D3
#define PIN_BTN_PREV   4       // D4
#define PIN_VIBRO      5       // D5, OUTPUT, drives the vibration motor (HIGH=on)

#define SERIAL_BAUD    115200
#define STEP_MS        2000UL  // advance the displayed word every ~2s
#define DEBOUNCE_MS    200UL

enum Word { W_FREE = 0, W_WIP, W_BLOCKED, W_WTF };
const uint8_t WORD_COUNT = 4;

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

uint8_t wordIdx = W_FREE;
unsigned long lastStepMs = 0;

// ---- Vibration (haptic) state machine ----------------------------------------
uint8_t       vibroPulses   = 0;
bool          vibroOn       = false;
uint16_t      vibroOnMs     = 0;
uint16_t      vibroOffMs    = 0;
unsigned long vibroPhaseMs  = 0;

// ---- Button debounce state ---------------------------------------------------
bool          focusStable = true, nextStable = true, prevStable = true;
int           focusLastRaw = HIGH, nextLastRaw = HIGH, prevLastRaw = HIGH;
unsigned long focusEdgeMs = 0, nextEdgeMs = 0, prevEdgeMs = 0;

static const __FlashStringHelper *wordLabel(uint8_t w) {
  switch (w) {
    case W_WIP:     return F("WIP");
    case W_BLOCKED: return F("BLOCKED");
    case W_WTF:     return F("WTF");
    default:        return F("FREE");
  }
}

// ---- Non-blocking haptic engine (mirrors the main firmware) ------------------
static void startBuzz(uint8_t pulses, uint16_t onMs, uint16_t offMs) {
  if (pulses == 0) {
    vibroPulses = 0;
    vibroOn = false;
    digitalWrite(PIN_VIBRO, LOW);
    return;
  }
  vibroPulses  = pulses;
  vibroOnMs    = onMs;
  vibroOffMs   = offMs;
  vibroOn      = true;
  vibroPhaseMs = millis();
  digitalWrite(PIN_VIBRO, HIGH);
}

static void pollVibro() {
  if (vibroPulses == 0) return;
  unsigned long now = millis();
  if (vibroOn) {
    if ((now - vibroPhaseMs) >= vibroOnMs) {
      digitalWrite(PIN_VIBRO, LOW);
      vibroOn = false;
      vibroPhaseMs = now;
      vibroPulses--;
      if (vibroPulses == 0) return;
    }
  } else {
    if ((now - vibroPhaseMs) >= vibroOffMs) {
      digitalWrite(PIN_VIBRO, HIGH);
      vibroOn = true;
      vibroPhaseMs = now;
    }
  }
}

static void buzzForWord(uint8_t w) {
  switch (w) {
    case W_WTF:     startBuzz(3, 200, 120); break;  // urgent
    case W_BLOCKED: startBuzz(2, 150, 130); break;  // needs you
    case W_FREE:    startBuzz(1,  90,   0); break;  // single short tick (done)
    default:        /* W_WIP: no buzz */    break;
  }
}

static void drawWord(uint8_t w) {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  // Big word on top (size 2), word index on the line below.
  display.setTextSize(2);
  display.setCursor(0, 0);
  display.print(wordLabel(w));

  display.setTextSize(1);
  display.setCursor(0, 22);
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
  pinMode(PIN_BTN_PREV,  INPUT_PULLUP);
  pinMode(PIN_VIBRO,     OUTPUT);
  digitalWrite(PIN_VIBRO, LOW);

  Serial.begin(SERIAL_BAUD);
  Serial.println(F("Claude Mate self-test (haptic) starting..."));

  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println(F("OLED init FAILED at 0x3C - try 0x3D"));
    for (;;) { delay(500); }             // halt; nothing else to test visually
  }
  Serial.println(F("OLED OK."));

  wordIdx = W_FREE;
  buzzForWord(wordIdx);
  Serial.print(F("word: "));
  Serial.println(wordLabel(wordIdx));
  drawWord(wordIdx);

  lastStepMs = millis();
  Serial.println(F("cycling words; press buttons to test."));
}

void loop() {
  unsigned long now = millis();

  // Advance the displayed word every STEP_MS and pulse the vibro on each change.
  if ((now - lastStepMs) >= STEP_MS) {
    lastStepMs = now;
    wordIdx = (wordIdx + 1) % WORD_COUNT;
    buzzForWord(wordIdx);
    Serial.print(F("word: "));
    Serial.println(wordLabel(wordIdx));
    drawWord(wordIdx);
  }

  pollVibro();                           // advance the haptic state machine

  pollButton(PIN_BTN_FOCUS, F("FOCUS button (D2) pressed"),
             focusStable, focusLastRaw, focusEdgeMs);
  pollButton(PIN_BTN_NEXT,  F("NEXT button (D3) pressed"),
             nextStable,  nextLastRaw,  nextEdgeMs);
  pollButton(PIN_BTN_PREV,  F("PREV button (D4) pressed"),
             prevStable,  prevLastRaw,  prevEdgeMs);
}
