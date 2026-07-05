/*
 * Claude Mate - Arduino Nano firmware (main sketch)
 * ==================================================
 *
 * A USB hardware companion for triaging a fleet of Claude Code sessions.
 * This sketch drives a 128x32 SSD1306 I2C OLED, an indication LED (alert) and
 * three buttons. It speaks the daemon<->Arduino serial protocol over USB CDC
 * serial at 115200 baud, 8N1.
 *
 * THE INTERFACE: one screen, one queue, three buttons.
 *
 * The daemon keeps an urgency-sorted triage queue of sessions
 * (error > waiting > done > working > idle) and pre-renders ONE frame as four
 * size-1 rows:
 *
 *     +---------------------+
 *     |api-server           |   r0: session name -- flashes (inverts ~2.5 Hz)
 *     |WAIT  0:42           |   r1: state tag + time-in-state
 *     |Opus 4.8  xhigh      |   r2: model + effort
 *     |2/6 W|B|E|D|I        |   r3: queue position + one status LETTER per
 *     +---------------------+       session, '|'-separated (E/B/W/D/I)
 *
 * The firmware is a dumb renderer: it holds exactly one frame (3 text rows +
 * a flash flag) and draws it. All ordering, selection, truncation, and text
 * layout live in the daemon. There are NO modes: the buttons mean the same
 * thing at all times --
 *
 *     PREV (left)   step the selection up the queue      (auto-repeats held)
 *     GO   (middle) short: raise that session's terminal window (+ack)
 *                   long : acknowledge the alert WITHOUT raising anything
 *     NEXT (right)  step the selection down the queue    (auto-repeats held)
 *
 * The indication LED is driven entirely by the daemon via V|<KIND> lines: it
 * plays a status-distinct pattern for the worst UNacknowledged alert class
 * and loops until acknowledged. The firmware just plays the pattern.
 *
 * If the daemon goes silent for ~30s the firmware stops any LED loop AND
 * replaces the (stale) frame with a LINK LOST screen, so a dead daemon is an
 * honest, visible state instead of a frozen display.
 *
 * REQUIRED LIBRARIES (install via the Arduino Library Manager):
 *   - Adafruit GFX Library     (Adafruit_GFX)
 *   (the SSD1306 itself is driven by the bundled softssd1306.h)
 *
 * PIN MAP (Arduino Nano, ATmega328P):
 *   OLED SSD1306 128x32 (0.91") over SOFTWARE I2C (hardware SCL A5 was damaged):
 *     VCC -> 5V, GND -> GND, SDA -> A4, SCL -> A3  (bit-banged; see softssd1306.h)
 *     I2C address 0x3C (common alternative: 0x3D)
 *   Buttons (INPUT_PULLUP, other leg to GND) -- layout PREV | GO | NEXT:
 *     GO   button -> D2  (emits "B|G" on a short press: focus the shown
 *                        session; "B|K" on a long press: acknowledge only)
 *     NEXT button -> D3  (emits "B|N" on press; auto-repeats while held)
 *     PREV button -> D4  (emits "B|P" on press; auto-repeats while held)
 *   Indication LED (the sole alert output):
 *     LED          -> D8   (OUTPUT; LED + ~220-1k series resistor to GND). Blinks
 *                          the alert pattern.
 *
 * SERIAL PROTOCOL (115200 8N1, ASCII lines terminated by '\n', fields split '|')
 *   Daemon -> Arduino:
 *     F|<flash>|<r0>|<r1>|<r2>|<r3>      the whole screen, pre-rendered as four
 *                                        size-1 rows (each up to 21 chars).
 *         flash = 1: invert row 0 (the name) at ~2.5 Hz (unacked alert)
 *         r0    = session name
 *         r1    = state tag + time-in-state
 *         r2    = model + effort
 *         r3    = queue position + '|'-separated status letters. r3 is the LAST
 *                 field and MAY contain literal '|' (the tokenizer stops at the
 *                 5th bar and takes the rest verbatim) -- that is what lets the
 *                 fleet strip use '|' as its on-screen divider.
 *     V|<kind>                           LED alert control (light only):
 *                                        START one-shot start blink; INPUT /
 *                                        ERROR / DONE looping "until
 *                                        acknowledged" alerts (repeat until
 *                                        V|OFF); OFF darkens the LED now
 *     P                                  ping/keepalive (we reply K)
 *   Arduino -> Daemon:
 *     H                                  hello, sent once after boot/reset --
 *                                        the daemon answers with a full resend
 *     K                                  keepalive ack (reply to P)
 *     B|P  B|N                           PREV / NEXT pressed (or auto-repeat)
 *     B|G                                GO short press (focus + acknowledge)
 *     B|K                                GO long press (acknowledge only)
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
// Physical layout (left -> right): PREV | GO | NEXT.
#define PIN_BTN_GO     2       // D2, INPUT_PULLUP, B|G short / B|K long
#define PIN_BTN_NEXT   3       // D3, INPUT_PULLUP, B|N (repeats while held)
#define PIN_BTN_PREV   4       // D4, INPUT_PULLUP, B|P (repeats while held)
#define PIN_LED        8       // D8, OUTPUT, indication LED (+ ~220-1k series
                               // resistor to GND). The SOLE alert output.

// ---- Protocol constants ------------------------------------------------------
#define SERIAL_BAUD    115200
#define LINE_MAX       96      // cap input line length to bound RAM use
                               // (the F frame worst case is ~60 bytes)
#define DEBOUNCE_MS    40UL    // ~40ms button debounce (snappy short taps)
#define LONGPRESS_MS   500UL   // GO held this long -> long-press (acknowledge)
#define REPEAT_DELAY_MS 400UL  // PREV/NEXT held this long -> start auto-repeat
#define REPEAT_MS      200UL   // ...then one repeat event every 200ms (5/s)

// ---- Screen text geometry ----------------------------------------------------
#define ROW_CHARS      21      // size-1 rows: 21 chars of 6px
#define ROW_H          8       // each size-1 row is 8px tall (4 rows fill 32px)

// ---- Alert-indicator tuning --------------------------------------------------
// The alert output is the LED on D8 (digital on/off). Urgency reads through the
// rhythm of the pulses.
#define ALERT_MAX_STEPS   6       // longest pattern (DONE's cascade)
// A looping pattern (INPUT/DONE/ERROR) repeats until the daemon sends V|OFF. As
// a failsafe, if the daemon goes fully silent for this long we stop on our own
// (and show the LINK LOST screen) so a crashed daemon can't leave the LED stuck
// or the display lying. The daemon pings (P) every ~15s and refreshes the frame
// ~1/s while times tick, so this only trips when it is gone.
#define LINK_WATCHDOG_MS 30000UL

SoftSSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT);   // software I2C on A3(SCL)/A4(SDA)

// ---- Serial line assembly ----------------------------------------------------
static char  lineBuf[LINE_MAX];
static uint8_t lineLen = 0;
static bool  lineOverflow = false;   // drop the rest of an over-long line

// ---- Current frame (the whole UI state; the daemon owns everything else) -----
// Four size-1 rows, drawn top-to-bottom. row[0] (the name) inverts while flash.
static char  frameRow[4][ROW_CHARS + 1] = {{0}};
static bool  frameFlash = false;     // invert row 0 (the name) at ~2.5 Hz
static bool  haveFrame  = false;     // false until the first F| arrives
static bool  linkLost   = false;     // daemon silent > LINK_WATCHDOG_MS
static bool  gBlinkOn   = true;      // shared blink phase for the flash band

// ---- Render coalescing ---------------------------------------------------
// Serial handlers only MARK the display dirty (requestRender); the actual
// redraw happens in loop(), once the incoming burst has drained. One redraw
// per burst instead of one per line, and never mid-burst -- so the RX buffer
// can't overflow behind a redraw.
static bool          needRender = false;
static unsigned long dirtyMs    = 0;      // when the display first went dirty

// ---- LED pattern engine ------------------------------------------------------
// A pattern is a short list of ON pulses; each pulse has an on-duration and the
// gap that FOLLOWS it. A pattern either plays once or LOOPS (repeats until
// V|OFF / a new pattern / the daemon-silence watchdog).
struct LedStep { uint16_t onMs; uint16_t offMs; };
static LedStep       ledSteps[ALERT_MAX_STEPS];
static uint8_t       ledStepCount = 0;     // pulses in the active pattern
static uint8_t       ledStepIdx   = 0;     // pulse currently playing
static bool          ledLoop      = false; // repeat the pattern until stopped?
static bool          ledActive    = false; // a pattern is playing
static bool          ledOn        = false; // LED lit right now?
static unsigned long ledPhaseMs   = 0;     // millis() at the last phase change
static unsigned long lastRxMs     = 0;     // millis() of the last serial byte
                                           // (watchdog: daemon liveness)

// ---- Press feedback blip -------------------------------------------------
// On every accepted button edge the panel does a hardware invert for ~80ms
// (SSD1306 0xA7/0xA6): instant "the device heard you" feedback with zero
// framebuffer cost, before the daemon round-trips a new frame. The command
// can only go on the wire while no chunked frame transfer is open, so the
// blip is best-effort: pending flags are applied from loop() when the bus
// is free (and simply dropped if a newer state supersedes them).
#define BLIP_MS 80UL
static bool          blipPending = false;  // want to start a blip
static bool          blipLit     = false;  // panel currently inverted
static unsigned long blipOffAt   = 0;      // when to un-invert

// ---- Button debounce state ---------------------------------------------------
// Immediate-fire debounce: an edge is ACCEPTED (and its event emitted) the very
// tick it is seen, provided the last accepted edge is >= DEBOUNCE_MS old. Bounce
// after an accepted edge is ignored for the window; press latency is ~0 ms.
//   PREV/NEXT emit on the press edge and auto-repeat while held. GO
// distinguishes a SHORT press (emit on release) from a LONG press (emit once at
// LONGPRESS_MS, then swallow the release).
struct Btn {
  uint8_t       pin;
  bool          pressed;    // debounced logical state (true = held down)
  unsigned long changeMs;   // when the last edge was accepted
  unsigned long pressMs;    // when the current press began
  bool          longFired;  // long-press already emitted this hold?
  unsigned long repeatMs;   // when the last auto-repeat event fired
};
static Btn goBtn   = {PIN_BTN_GO,   false, 0, 0, false, 0};
static Btn nextBtn = {PIN_BTN_NEXT, false, 0, 0, false, 0};
static Btn prevBtn = {PIN_BTN_PREV, false, 0, 0, false, 0};

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

// Copy a field into dst with a hard cap (always NUL-terminated).
static void copyField(char *dst, uint8_t cap, const char *src) {
  if (!src) { dst[0] = 0; return; }
  uint8_t i = 0;
  while (src[i] && i < (cap - 1)) { dst[i] = src[i]; i++; }
  dst[i] = 0;
}

// -----------------------------------------------------------------------------
// Alert output: indication LED (D8), non-blocking engine
// -----------------------------------------------------------------------------

static inline void alertOn()  { digitalWrite(PIN_LED, HIGH); }
static inline void alertOff() { digitalWrite(PIN_LED, LOW); }

// Stop any alert immediately and leave the LED off.
static void stopLed() {
  ledActive    = false;
  ledOn        = false;
  ledStepCount = 0;
  alertOff();
}

// Begin an alert pattern: `count` LED pulses copied from `steps`. If `loop` is
// true the sequence repeats until stopLed() / a new pattern / the daemon-silence
// watchdog; otherwise it plays once. Returns immediately; the pulses play out in
// pollLed(). A new call replaces any pattern in progress.
static void startPattern(const LedStep *steps, uint8_t count, bool loop) {
  if (count == 0) { stopLed(); return; }
  if (count > ALERT_MAX_STEPS) count = ALERT_MAX_STEPS;
  for (uint8_t i = 0; i < count; i++) ledSteps[i] = steps[i];
  ledStepCount = count;
  ledStepIdx   = 0;
  ledLoop      = loop;
  ledActive    = true;
  ledOn        = true;                // start the first pulse now
  ledPhaseMs   = millis();
  alertOn();
}

// Advance the LED state machine. Call every loop(); never blocks.
static void pollLed() {
  if (!ledActive) return;            // idle: nothing playing
  unsigned long now = millis();

  // Loop failsafe: if we are repeating a pattern but the daemon has gone silent
  // (no serial for LINK_WATCHDOG_MS), stop -- a crashed daemon must not leave
  // the LED blinking forever. One-shots are short, so they need no watchdog.
  if (ledLoop && (now - lastRxMs) >= LINK_WATCHDOG_MS) {
    stopLed();
    return;
  }

  const LedStep &step = ledSteps[ledStepIdx];
  if (ledOn) {
    if ((now - ledPhaseMs) >= step.onMs) {
      alertOff();                    // end of on-phase -> start this step's gap
      ledOn = false;
      ledPhaseMs = now;
    }
  } else {
    if ((now - ledPhaseMs) >= step.offMs) {
      // Gap elapsed: advance to the next pulse, looping or finishing at the end.
      ledStepIdx++;
      if (ledStepIdx >= ledStepCount) {
        if (!ledLoop) { stopLed(); return; }   // one-shot complete -- stay off
        ledStepIdx = 0;                        // loop back to the first pulse
      }
      alertOn();
      ledOn = true;
      ledPhaseMs = now;
    }
  }
}

// Daemon-driven alert patterns (blinked on the LED). Since the output is a LIGHT
// the urgency has to read at a GLANCE, so each state has its own unmistakable
// rhythm and the "you need to act" states blink until acknowledged:
//
//   START  job (re)started : one long 1 s blink, then dark            (one-shot)
//   INPUT  needs your input: aggressive even blink (~2.8 Hz)          ; LOOPS until ack
//   ERROR  API error/alert : super-aggressive fast strobe (~7 Hz)     ; LOOPS until ack
//   DONE   turn finished   : cascade -- 4 quick blinks then a pause   ; LOOPS until ack
//   OFF    (or STOP)       : end any pattern now (daemon sends on acknowledge/clear)
static char ledKindCode = 0;             // first letter of the active LOOP kind

static void ledForKind(const char *k) {
  // Re-sending the loop that is ALREADY playing must not restart its phase
  // (a redundant resend would visibly glitch the rhythm).
  if (ledActive && ledLoop && ledKindCode == k[0] &&
      (k[0] == 'I' || k[0] == 'E' || k[0] == 'D')) {
    return;
  }
  ledKindCode = (k[0] == 'I' || k[0] == 'E' || k[0] == 'D') ? k[0] : 0;
  if (!strcmp(k, "START")) {
    ledKindCode = 0;
    const LedStep s[] = {{1000, 0}};                       // one long blink: job started
    startPattern(s, 1, false);
  } else if (!strcmp(k, "INPUT")) {
    const LedStep s[] = {{180, 180}};                      // aggressive even blink: needs you
    startPattern(s, 1, true);
  } else if (!strcmp(k, "ERROR")) {
    const LedStep s[] = {{70, 70}};                        // frantic strobe: error
    startPattern(s, 1, true);
  } else if (!strcmp(k, "DONE")) {
    const LedStep s[] = {{110, 90}, {110, 90}, {110, 90}, {110, 650}}; // cascade burst + pause
    startPattern(s, 4, true);
  } else if (!strcmp(k, "OFF") || !strcmp(k, "STOP")) {
    ledKindCode = 0;
    stopLed();
  }
}

// -----------------------------------------------------------------------------
// Rendering -- one frame type plus two firmware-local states
// -----------------------------------------------------------------------------

static void drawFrame() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);

  // Four size-1 rows stacked 8px apart. Row 0 is the name; when its alert is
  // unacknowledged the whole row inverts on the shared blink phase (~2.5 Hz).
  for (uint8_t r = 0; r < 4; r++) {
    display.setCursor(0, r * ROW_H);
    display.print(frameRow[r]);
  }
  if (frameFlash && gBlinkOn) {
    display.fillRect(0, 0, SCREEN_WIDTH, ROW_H, SSD1306_INVERSE);
  }

  display.display();
}

// Firmware-local screen: the daemon has gone silent (watchdog tripped). An
// honest state instead of a silently stale frame.
static void drawLinkLost() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(2);
  display.setCursor(0, 0);
  display.print(F("NO LINK"));
  display.setTextSize(1);
  display.setCursor(0, 24);
  display.print(F("waiting for daemon"));
  display.display();
}

// Firmware-local screen: booted, no frame from the daemon yet.
static void drawSplash() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(2);
  display.setCursor(0, 0);
  display.print(F("MATE"));
  display.setTextSize(1);
  display.setCursor(0, 24);
  display.print(F("starting..."));
  display.display();
}

static void render() {
  if      (linkLost)   drawLinkLost();
  else if (!haveFrame) drawSplash();
  else                 drawFrame();
}

// -----------------------------------------------------------------------------
// Protocol parsing
// -----------------------------------------------------------------------------

// Handle one complete, NUL-terminated line.
static void handleLine(char *line) {
  if (line[0] == 0) return;              // ignore empty lines

  switch (line[0]) {
    case 'P':                            // ping -> keepalive ack. NOT 'H': the
      Serial.println('K');               // daemon treats H as "I rebooted" and
      break;                             // does a full resend + LED re-arm,
                                         // which would restart the pattern
                                         // phase every 15 s.

    case 'F': {  // F|<flash>|<r0>|<r1>|<r2>|<r3> -- the whole screen
      // Split off exactly the first 5 '|'; fields[5] (=r3) then holds the rest
      // of the line VERBATIM, including any literal '|' (the fleet divider).
      char *fields[6];
      uint8_t n = 0;
      char *p = line;
      fields[n++] = p;                   // fields[0] = "F"
      while (n < 6) {
        char *bar = strchr(p, '|');
        if (!bar) break;
        *bar = 0;
        p = bar + 1;
        fields[n++] = p;
      }
      if (n < 6) break;                  // malformed: ignore
      frameFlash = (fields[1][0] == '1');
      for (uint8_t r = 0; r < 4; r++)
        copyField(frameRow[r], sizeof(frameRow[r]), fields[2 + r]);
      haveFrame = true;
      requestRender();
      break;
    }

    case 'V': {                          // V|<KIND> -- play an LED pattern NOW
      char *bar = strchr(line, '|');
      if (!bar || bar[1] == 0) break;
      ledForKind(bar + 1);               // START | INPUT | DONE | ERROR | OFF
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
    lastRxMs = millis();                  // feed the liveness watchdog
    if (c == '\n' || c == '\r') {
      if (!lineOverflow && lineLen > 0) {
        lineBuf[lineLen] = 0;
        handleLine(lineBuf);
        if (linkLost) {                   // daemon is back: drop the overlay
          linkLost = false;
          requestRender();
        }
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

static void emitBtn(char c) {
  Serial.print(F("B|"));
  Serial.println(c);
  blipPending = true;                    // instant "heard you" panel blip
}

// PREV/NEXT: emit on the press edge (instant), then auto-repeat while held
// (REPEAT_DELAY_MS to start, one event per REPEAT_MS after that).
static void pollNavBtn(Btn &b, char ev) {
  bool raw = (digitalRead(b.pin) == LOW);   // pull-up: LOW = pressed
  unsigned long now = millis();
  if (raw != b.pressed && (now - b.changeMs) >= DEBOUNCE_MS) {
    b.pressed  = raw;                       // accept the edge NOW
    b.changeMs = now;
    if (raw) {                              // press edge: fire immediately
      b.pressMs  = now;
      b.repeatMs = now;
      emitBtn(ev);
    }
  }
  if (b.pressed && (now - b.pressMs) >= REPEAT_DELAY_MS &&
      (now - b.repeatMs) >= REPEAT_MS) {
    b.repeatMs = now;
    emitBtn(ev);
  }
}

// GO: short press (emit 'G' on release) vs long press (emit 'K' once at
// LONGPRESS_MS; the release is then swallowed).
static void pollGoBtn(Btn &b) {
  bool raw = (digitalRead(b.pin) == LOW);
  unsigned long now = millis();
  if (raw != b.pressed && (now - b.changeMs) >= DEBOUNCE_MS) {
    b.pressed  = raw;
    b.changeMs = now;
    if (raw) {                              // press edge
      b.pressMs   = now;
      b.longFired = false;
    } else {                                // release edge
      if (!b.longFired) emitBtn('G');       // short press: focus + acknowledge
    }
  }
  // Long-press fires while still held, once the threshold is crossed.
  if (b.pressed && !b.longFired && (now - b.pressMs) >= LONGPRESS_MS) {
    emitBtn('K');                           // acknowledge without focusing
    b.longFired = true;
  }
}

static void pollButtons() {
  pollGoBtn(goBtn);
  pollNavBtn(nextBtn, 'N');
  pollNavBtn(prevBtn, 'P');
}

// -----------------------------------------------------------------------------
// Press feedback blip (hardware invert, no framebuffer cost)
// -----------------------------------------------------------------------------
// The invert command can only be sent while no chunked frame transfer holds the
// I2C data transaction open, so both edges are applied from loop() when the bus
// is free. Best-effort by design: a blip that can't get on the bus in time is
// simply dropped (the daemon's frame response lands moments later anyway).
static void pollBlip() {
  unsigned long now = millis();
  if (blipPending && !display.flushBusy()) {
    display.command(0xA7);                 // invert on
    blipOffAt = now + BLIP_MS;
    blipLit = true;
    blipPending = false;
  }
  if (blipLit && (long)(now - blipOffAt) >= 0 && !display.flushBusy()) {
    display.command(0xA6);                 // back to normal
    blipLit = false;
  }
}

// -----------------------------------------------------------------------------
// Setup / loop
// -----------------------------------------------------------------------------

void setup() {
  pinMode(PIN_BTN_GO,   INPUT_PULLUP);
  pinMode(PIN_BTN_NEXT, INPUT_PULLUP);
  pinMode(PIN_BTN_PREV, INPUT_PULLUP);
  pinMode(PIN_LED,      OUTPUT);
  digitalWrite(PIN_LED, LOW);            // LED off at boot
  lastRxMs = millis();                   // seed the liveness watchdog

  Serial.begin(SERIAL_BAUD);

  // Init the OLED. If it fails, blink the LED so a wiring fault is SEEN.
  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println(F("OLED init FAILED (try 0x3D)"));
    for (;;) {
      // A few short blinks, then a pause -- repeats forever. Hand-rolled here
      // (the LED engine is not pumped in this dead-end loop).
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
  // the fixed 3-row layout).
  display.setTextWrap(false);

  render();                              // splash until the daemon talks to us

  // Emit hello once, after the display is up, so the daemon sends the full
  // current state (frame + LED kind).
  Serial.println('H');
}

void loop() {
  pumpSerial();
  pollButtons();
  pollLed();                             // advance the LED state machine
  pollBlip();                            // apply any pending press blip
  display.pumpFlush();                   // ship ~32B (~2.3ms) of any armed frame

  unsigned long now = millis();

  // Liveness watchdog: daemon silent too long -> honest LINK LOST screen
  // (pollLed() stops any LED loop on the same condition).
  if (!linkLost && (now - lastRxMs) >= LINK_WATCHDOG_MS) {
    linkLost = true;
    requestRender();
  }

  // Blink phase for the flashing name band (~2.5 Hz, 400ms half-period).
  // Re-render only when a flashing band is actually shown.
  bool nb = (now / 400) & 1;
  if (nb != gBlinkOn) {
    gBlinkOn = nb;
    if (haveFrame && !linkLost && frameFlash) requestRender();
  }

  // Perform a pending redraw once the serial burst has drained (>=8ms of RX
  // silence), or after 60ms regardless so a continuous stream can't starve the
  // display. Drawing must wait for the previous frame's chunked transfer to
  // finish (flushBusy) -- rendering mutates the framebuffer mid-send otherwise.
  // render() itself only draws + ARMS the transfer (~1ms); the bytes go out via
  // pumpFlush() above, so buttons/serial stay live throughout.
  if (needRender && !display.flushBusy()) {
    bool rxQuiet = (Serial.available() == 0) && (now - lastRxMs) >= 8;
    if (rxQuiet || (now - dirtyMs) >= 60) {
      needRender = false;
      render();
    }
  }
}
