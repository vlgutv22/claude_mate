/*
 * Claude Mate - SELF-TEST sketch (standalone, NO host/daemon needed)
 * ==================================================================
 *
 * Use this sketch to verify the OLED + indication LED + three buttons of the
 * device WITHOUT a Mac, Python daemon or Claude Code hooks.
 *
 * What it does:
 *   - Cycles a demo frame through the session states ERR -> WAIT -> DONE ->
 *     WORK -> IDLE, ~every 3s, drawn in the REAL interface layout (size-2 name
 *     band + info row + fleet strip).
 *   - On each change, plays the matching LED pattern on D8 exactly like the
 *     main firmware (ERROR strobe / INPUT blink / DONE cascade / START blink),
 *     and flashes the name band for the alert states.
 *   - Prints a line over serial whenever a button is pressed:
 *       "GO button (D2) pressed" / "NEXT button (D3) pressed" /
 *       "PREV button (D4) pressed"
 *     Open the Serial Monitor at 115200 to watch the events.
 *
 * REQUIRED LIBRARIES (Arduino Library Manager):
 *   - Adafruit GFX Library     (Adafruit_GFX)
 *   (the SSD1306 is driven by the bundled softssd1306.h -- a COPY of
 *    ../claude_mate/softssd1306.h; keep the two in sync)
 *
 * PIN MAP (identical to the main firmware, Arduino Nano / ATmega328P):
 *   OLED SSD1306 128x32 (0.91") over SOFTWARE I2C (hardware SCL A5 is damaged
 *     on this board): VCC->5V, GND->GND, SDA->A4, SCL->A3. Address 0x3C
 *     (common alternative: 0x3D -- change SCREEN_ADDRESS below).
 *   Buttons (INPUT_PULLUP, other leg to GND): GO -> D2, NEXT -> D3, PREV -> D4
 *   Indication LED: D8 (OUTPUT; LED + ~220-1k series resistor to GND).
 */

#include <Adafruit_GFX.h>
#include "softssd1306.h"      // software I2C on A3(SCL)/A4(SDA)

// ---- OLED configuration ------------------------------------------------------
#define SCREEN_WIDTH   128
#define SCREEN_HEIGHT  32     // 0.91" SSD1306
#define SCREEN_ADDRESS 0x3C    // fallback / common alternative: 0x3D

// ---- Pin map -----------------------------------------------------------------
#define PIN_BTN_GO     2       // D2
#define PIN_BTN_NEXT   3       // D3
#define PIN_BTN_PREV   4       // D4
#define PIN_LED        8       // D8, OUTPUT, indication LED

#define SERIAL_BAUD    115200
#define STEP_MS        3000UL  // advance the demo state every ~3s
#define DEBOUNCE_MS    200UL

SoftSSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT);

// ---- Demo states (mirror the real interface) ----------------------------------
struct Demo {
  const char *name;    // size-2 name band (<=10 chars)
  const char *info;    // info row (<=21 chars)
  bool flash;          // alert: flash the name band
};
const Demo DEMOS[] = {
  {"api-server", "ERR  00:42 Opus xhigh", true },
  {"webapp",     "WAIT 01:07 Sonnet high", true },
  {"infra",      "DONE 00:15 Haiku med",  true },
  {"claude-mat", "WORK 12:03 Opus xhigh", false},
  {"notes",      "IDLE 05:30",            false},
};
const char *FLEET = "1/5 !?*>.";
const uint8_t DEMO_COUNT = sizeof(DEMOS) / sizeof(DEMOS[0]);

uint8_t demoIdx = 0;
unsigned long lastStepMs = 0;
bool blinkOn = true;

// ---- LED pattern engine (mirrors the main firmware) ---------------------------
struct LedStep { uint16_t onMs; uint16_t offMs; };
LedStep       ledSteps[6];
uint8_t       ledStepCount = 0, ledStepIdx = 0;
bool          ledLoop = false, ledActive = false, ledOn = false;
unsigned long ledPhaseMs = 0;

static void stopLed() {
  ledActive = false; ledOn = false; ledStepCount = 0;
  digitalWrite(PIN_LED, LOW);
}

static void startPattern(const LedStep *steps, uint8_t count, bool loop) {
  if (count == 0) { stopLed(); return; }
  if (count > 6) count = 6;
  for (uint8_t i = 0; i < count; i++) ledSteps[i] = steps[i];
  ledStepCount = count; ledStepIdx = 0;
  ledLoop = loop; ledActive = true; ledOn = true;
  ledPhaseMs = millis();
  digitalWrite(PIN_LED, HIGH);
}

static void pollLed() {
  if (!ledActive) return;
  unsigned long now = millis();
  const LedStep &step = ledSteps[ledStepIdx];
  if (ledOn) {
    if ((now - ledPhaseMs) >= step.onMs) {
      digitalWrite(PIN_LED, LOW);
      ledOn = false; ledPhaseMs = now;
    }
  } else {
    if ((now - ledPhaseMs) >= step.offMs) {
      ledStepIdx++;
      if (ledStepIdx >= ledStepCount) {
        if (!ledLoop) { stopLed(); return; }
        ledStepIdx = 0;
      }
      digitalWrite(PIN_LED, HIGH);
      ledOn = true; ledPhaseMs = now;
    }
  }
}

static void ledForDemo(uint8_t i) {
  switch (i) {
    case 0: { const LedStep s[] = {{70, 70}};   startPattern(s, 1, true); break; }  // ERROR strobe
    case 1: { const LedStep s[] = {{180, 180}}; startPattern(s, 1, true); break; }  // INPUT blink
    case 2: { const LedStep s[] = {{110, 90}, {110, 90}, {110, 90}, {110, 650}};
              startPattern(s, 4, true); break; }                                    // DONE cascade
    case 3: { const LedStep s[] = {{1000, 0}};  startPattern(s, 1, false); break; } // START one-shot
    default: stopLed(); break;                                                      // IDLE: dark
  }
}

// ---- Frame rendering (the real interface layout) -------------------------------
static void drawDemo(uint8_t i) {
  const Demo &d = DEMOS[i];
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(2);
  display.setCursor(0, 0);
  display.print(d.name);
  if (d.flash && blinkOn) {
    display.fillRect(0, 0, SCREEN_WIDTH, 16, SSD1306_INVERSE);
  }
  display.setTextSize(1);
  display.setCursor(0, 16);
  display.print(d.info);
  display.setCursor(0, 24);
  display.print(FLEET);
  display.display();
}

// ---- Buttons -------------------------------------------------------------------
bool          goStable = true, nextStable = true, prevStable = true;
int           goLastRaw = HIGH, nextLastRaw = HIGH, prevLastRaw = HIGH;
unsigned long goEdgeMs = 0, nextEdgeMs = 0, prevEdgeMs = 0;

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
  pinMode(PIN_BTN_GO,   INPUT_PULLUP);
  pinMode(PIN_BTN_NEXT, INPUT_PULLUP);
  pinMode(PIN_BTN_PREV, INPUT_PULLUP);
  pinMode(PIN_LED,      OUTPUT);
  digitalWrite(PIN_LED, LOW);

  Serial.begin(SERIAL_BAUD);
  Serial.println(F("Claude Mate self-test starting..."));

  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println(F("OLED init FAILED at 0x3C - try 0x3D"));
    for (;;) { delay(500); }             // halt; nothing else to test visually
  }
  Serial.println(F("OLED OK."));

  demoIdx = 0;
  ledForDemo(demoIdx);
  Serial.print(F("demo: "));
  Serial.println(DEMOS[demoIdx].name);
  drawDemo(demoIdx);
  while (display.flushBusy()) display.pumpFlush();

  lastStepMs = millis();
  Serial.println(F("cycling states; press buttons to test."));
}

void loop() {
  unsigned long now = millis();

  // Advance the demo state every STEP_MS; play its LED pattern.
  if ((now - lastStepMs) >= STEP_MS) {
    lastStepMs = now;
    demoIdx = (demoIdx + 1) % DEMO_COUNT;
    ledForDemo(demoIdx);
    Serial.print(F("demo: "));
    Serial.println(DEMOS[demoIdx].name);
    drawDemo(demoIdx);
  }

  pollLed();
  display.pumpFlush();                   // ship any armed frame, chunk by chunk

  // Flash the name band on alert demos (like the real firmware).
  bool nb = (now / 400) & 1;
  if (nb != blinkOn) {
    blinkOn = nb;
    if (DEMOS[demoIdx].flash && !display.flushBusy()) drawDemo(demoIdx);
  }

  pollButton(PIN_BTN_GO,   F("GO button (D2) pressed"),
             goStable,   goLastRaw,   goEdgeMs);
  pollButton(PIN_BTN_NEXT, F("NEXT button (D3) pressed"),
             nextStable, nextLastRaw, nextEdgeMs);
  pollButton(PIN_BTN_PREV, F("PREV button (D4) pressed"),
             prevStable, prevLastRaw, prevEdgeMs);
}
