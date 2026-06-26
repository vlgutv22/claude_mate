/*
 * Claude Mate - SELF-TEST sketch (standalone, NO host/daemon needed)
 * ==================================================================
 *
 * Use this sketch to verify wiring of the OLED, the three traffic-light LEDs
 * and the two buttons WITHOUT a Mac, Python daemon or Claude Code hooks.
 *
 * What it does:
 *   - Cycles the session card through working -> waiting -> error -> done,
 *     advancing one step per second (so you can see the OLED render each state).
 *   - Cycles the traffic-light LEDs GREEN -> YELLOW -> RED in lockstep so each
 *     LED and its resistor can be checked.
 *   - Prints a line over USB serial (115200 baud) whenever a button is pressed:
 *       "FOCUS button (D2) pressed"  /  "NEXT button (D3) pressed"
 *     Open the Serial Monitor at 115200 to watch button events.
 *
 * REQUIRED LIBRARIES (Arduino Library Manager):
 *   - Adafruit GFX Library      (Adafruit_GFX)
 *   - Adafruit SSD1306          (Adafruit_SSD1306)
 *
 * PIN MAP (identical to the main firmware, Arduino Nano / ATmega328P):
 *   OLED SSD1306 128x64 over I2C: VCC->5V, GND->GND, SDA->A4, SCL->A5
 *     I2C address 0x3C is used here. If your module is unresponsive, try the
 *     common alternative 0x3D (change SCREEN_ADDRESS below). Many cheap
 *     SSD1306 boards are 0x3C; some are 0x3D depending on a solder jumper.
 *   Buttons (INPUT_PULLUP, other leg to GND): FOCUS -> D2, NEXT -> D3
 *   LEDs (each via 220 ohm to GND): GREEN -> D5, YELLOW -> D6, RED -> D7
 */

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#define SCREEN_WIDTH   128
#define SCREEN_HEIGHT  64
#define OLED_RESET     -1
#define SCREEN_ADDRESS 0x3C    // fallback / common alternative: 0x3D

#define PIN_BTN_FOCUS  2       // D2
#define PIN_BTN_NEXT   3       // D3
#define PIN_LED_GREEN  5       // D5
#define PIN_LED_YELLOW 6       // D6
#define PIN_LED_RED    7       // D7

#define SERIAL_BAUD    115200
#define STEP_MS        1000UL  // advance card + LEDs every second
#define DEBOUNCE_MS    200UL

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// The four card states we cycle through.
const char *STATES[] = { "WORKING", "WAITING", "ERROR", "DONE" };
const uint8_t STATE_COUNT = 4;
uint8_t stepIdx = 0;
unsigned long lastStepMs = 0;

// Button debounce state.
bool          focusStable = true, nextStable = true;
int           focusLastRaw = HIGH, nextLastRaw = HIGH;
unsigned long focusEdgeMs = 0, nextEdgeMs = 0;

static void drawCard(uint8_t idx) {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  // Big "name" on top.
  display.setTextSize(2);
  display.setCursor(0, 0);
  display.print(F("selftest"));

  // State line, inverted for ERROR/WAITING (attention demo).
  bool redy = (idx == 1 /*WAITING*/ || idx == 2 /*ERROR*/);
  display.setTextSize(2);
  if (redy) {
    display.fillRect(0, 22, SCREEN_WIDTH, 18, SSD1306_WHITE);
    display.setTextColor(SSD1306_BLACK);
  }
  display.setCursor(0, 24);
  display.print(STATES[idx]);
  display.setTextColor(SSD1306_WHITE);

  // Runtime + limit demo line.
  display.setTextSize(1);
  display.setCursor(0, 46);
  display.print(F("t 00:0"));
  display.print(idx);
  display.print(F("  lim 42%"));

  // idx/total indicator.
  display.setCursor(0, 56);
  display.print(idx + 1);
  display.print('/');
  display.print(STATE_COUNT);

  display.display();
}

// Light the LEDs in lockstep with the card state. Exactly one lit per step:
// WORKING->GREEN, WAITING->YELLOW, ERROR->RED, DONE->GREEN.
static void drawLeds(uint8_t idx) {
  bool g = (idx == 0 || idx == 3);
  bool y = (idx == 1);
  bool r = (idx == 2);
  digitalWrite(PIN_LED_GREEN,  g ? HIGH : LOW);
  digitalWrite(PIN_LED_YELLOW, y ? HIGH : LOW);
  digitalWrite(PIN_LED_RED,    r ? HIGH : LOW);
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
  pinMode(PIN_LED_GREEN,  OUTPUT);
  pinMode(PIN_LED_YELLOW, OUTPUT);
  pinMode(PIN_LED_RED,    OUTPUT);

  Serial.begin(SERIAL_BAUD);
  Serial.println(F("Claude Mate self-test starting..."));

  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println(F("OLED init FAILED at 0x3C - try 0x3D"));
    // Blink RED forever so a display fault is obvious without serial.
    for (;;) {
      digitalWrite(PIN_LED_RED, HIGH); delay(200);
      digitalWrite(PIN_LED_RED, LOW);  delay(200);
    }
  }
  Serial.println(F("OLED OK. Cycling card + LEDs; press buttons to test."));

  lastStepMs = millis();
  drawCard(stepIdx);
  drawLeds(stepIdx);
}

void loop() {
  unsigned long now = millis();
  if ((now - lastStepMs) >= STEP_MS) {
    lastStepMs = now;
    stepIdx = (stepIdx + 1) % STATE_COUNT;
    drawCard(stepIdx);
    drawLeds(stepIdx);
  }

  pollButton(PIN_BTN_FOCUS, F("FOCUS button (D2) pressed"),
             focusStable, focusLastRaw, focusEdgeMs);
  pollButton(PIN_BTN_NEXT,  F("NEXT button (D3) pressed"),
             nextStable,  nextLastRaw,  nextEdgeMs);
}
