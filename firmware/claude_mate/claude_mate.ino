/*
 * Claude Mate - Arduino Nano firmware (main sketch)
 * ==================================================
 *
 * A USB hardware companion that shows the live status of Claude Code sessions.
 * This sketch drives a 128x64 SSD1306 I2C OLED, three traffic-light LEDs and
 * two buttons. It speaks the daemon<->Arduino serial protocol over USB CDC
 * serial at 115200 baud, 8N1.
 *
 * REQUIRED LIBRARIES (install via the Arduino Library Manager):
 *   - Adafruit GFX Library      (Adafruit_GFX)
 *   - Adafruit SSD1306          (Adafruit_SSD1306)
 *   Both pull in Adafruit BusIO + Wire (bundled with the IDE).
 *
 * PIN MAP (Arduino Nano, ATmega328P):
 *   OLED SSD1306 128x64 over I2C:
 *     VCC -> 5V, GND -> GND, SDA -> A4, SCL -> A5
 *     I2C address 0x3C (common alternative: 0x3D)
 *   Buttons (INPUT_PULLUP, other leg to GND):
 *     FOCUS button -> D2   (emits "B|1")
 *     NEXT  button -> D3   (emits "B|2")
 *   Traffic-light LEDs (each through a 220 ohm resistor to GND):
 *     GREEN  -> D5
 *     YELLOW -> D6
 *     RED    -> D7
 *   Optional piezo buzzer:
 *     BUZZER -> D8  (only active when ENABLE_BUZZER is defined)
 *
 * SERIAL PROTOCOL (115200 8N1, ASCII lines terminated by '\n', fields split by '|')
 *   Daemon -> Arduino:
 *     L|<color>                                      color = G | Y | R
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

// ---- Optional buzzer ---------------------------------------------------------
// Uncomment to enable a short chirp on entering the RED light state.
//#define ENABLE_BUZZER

// ---- Hardware configuration --------------------------------------------------
#define SCREEN_WIDTH   128
#define SCREEN_HEIGHT  64
#define OLED_RESET     -1      // share Arduino reset pin (no dedicated reset)
#define SCREEN_ADDRESS 0x3C    // common alternative: 0x3D

#define PIN_BTN_FOCUS  2       // D2, INPUT_PULLUP, emits B|1
#define PIN_BTN_NEXT   3       // D3, INPUT_PULLUP, emits B|2
#define PIN_LED_GREEN  5       // D5
#define PIN_LED_YELLOW 6       // D6
#define PIN_LED_RED    7       // D7
#define PIN_BUZZER     8       // D8 (optional)

// ---- Protocol constants ------------------------------------------------------
#define SERIAL_BAUD    115200
#define LINE_MAX       72      // cap input line length to bound RAM use
#define DEBOUNCE_MS    200UL   // ~200ms button debounce
#define RED_BLINK_MS   500UL   // slow blink period for RED LED + state line

// Light colors
enum Light { LIGHT_GREEN = 0, LIGHT_YELLOW, LIGHT_RED };

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

// ---- Light / blink state -----------------------------------------------------
static uint8_t curLight = LIGHT_GREEN;
static bool    blinkOn  = false;     // shared blink phase (RED LED + state line)
static unsigned long lastBlinkMs = 0;

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

// A state is "RED-worthy" when a human is needed: error or waiting.
static bool isRedState(uint8_t st) {
  return (st == ST_ERROR || st == ST_WAITING);
}

// Apply the traffic light: exactly one LED lit; RED also slow-blinks.
static void applyLight() {
  bool g = (curLight == LIGHT_GREEN);
  bool y = (curLight == LIGHT_YELLOW);
  bool r = (curLight == LIGHT_RED);
  // RED blinks; the others are steady.
  if (r) r = blinkOn;
  digitalWrite(PIN_LED_GREEN,  g ? HIGH : LOW);
  digitalWrite(PIN_LED_YELLOW, y ? HIGH : LOW);
  digitalWrite(PIN_LED_RED,    r ? HIGH : LOW);
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
// Rendering
// -----------------------------------------------------------------------------

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
  display.display();
}

static void drawCard() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  // Big name on top (size 2 = 16px tall). Truncate defensively to fit width.
  display.setTextSize(2);
  display.setCursor(0, 0);
  // 128px / 12px per size-2 char = 10 chars; name is already <=10.
  display.print(curName);

  // State line. On RED-worthy states, invert (or blink) for attention.
  display.setTextSize(2);
  bool redy = isRedState(curState);
  if (redy && blinkOn) {
    // Inverted band: white box, black text.
    display.fillRect(0, 22, SCREEN_WIDTH, 18, SSD1306_WHITE);
    display.setTextColor(SSD1306_BLACK);
  } else {
    display.setTextColor(SSD1306_WHITE);
  }
  display.setCursor(0, 24);
  display.print(stateLabel(curState));
  display.setTextColor(SSD1306_WHITE);

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

    case 'L': {                          // L|<color>
      // Find the color char after the first '|'.
      char *bar = strchr(line, '|');
      if (!bar || bar[1] == 0) break;
      char c = bar[1];
      uint8_t newLight = curLight;
      if      (c == 'G') newLight = LIGHT_GREEN;
      else if (c == 'Y') newLight = LIGHT_YELLOW;
      else if (c == 'R') newLight = LIGHT_RED;
      else break;                        // ignore unknown color
#ifdef ENABLE_BUZZER
      if (newLight == LIGHT_RED && curLight != LIGHT_RED) chirp();
#endif
      curLight = newLight;
      // Reset blink phase so a fresh RED lights immediately.
      blinkOn = true;
      lastBlinkMs = millis();
      applyLight();
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
// Blink tick (shared by RED LED and the inverted state line)
// -----------------------------------------------------------------------------

static void tickBlink() {
  unsigned long now = millis();
  if ((now - lastBlinkMs) >= RED_BLINK_MS) {
    lastBlinkMs = now;
    blinkOn = !blinkOn;
    applyLight();
    // Re-render only when blinking matters for the card (RED-worthy state).
    if (!showIdle && isRedState(curState)) render();
  }
}

// -----------------------------------------------------------------------------
// Setup / loop
// -----------------------------------------------------------------------------

void setup() {
  pinMode(PIN_BTN_FOCUS, INPUT_PULLUP);
  pinMode(PIN_BTN_NEXT,  INPUT_PULLUP);
  pinMode(PIN_LED_GREEN,  OUTPUT);
  pinMode(PIN_LED_YELLOW, OUTPUT);
  pinMode(PIN_LED_RED,    OUTPUT);
#ifdef ENABLE_BUZZER
  pinMode(PIN_BUZZER, OUTPUT);
#endif
  digitalWrite(PIN_LED_GREEN,  LOW);
  digitalWrite(PIN_LED_YELLOW, LOW);
  digitalWrite(PIN_LED_RED,    LOW);

  Serial.begin(SERIAL_BAUD);

  // Init the OLED. If it fails, blink RED forever so the failure is visible.
  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    for (;;) {
      digitalWrite(PIN_LED_RED, HIGH); delay(200);
      digitalWrite(PIN_LED_RED, LOW);  delay(200);
    }
  }

  display.clearDisplay();
  display.display();

  // Default to GREEN light and idle screen until the daemon talks to us.
  curLight = LIGHT_GREEN;
  blinkOn = true;
  lastBlinkMs = millis();
  applyLight();

  showIdle = true;
  render();

  // Emit hello once, after the display is initialized, so the daemon resends
  // the full current state (light + current card).
  Serial.println('H');
}

void loop() {
  pumpSerial();
  pollButtons();
  tickBlink();
}
