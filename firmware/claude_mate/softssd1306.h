/*
 * softssd1306.h — fast, non-blocking software (bit-banged) I2C SSD1306 driver
 * ===========================================================================
 *
 * Drop-in for the slice of the Adafruit_SSD1306 API this firmware uses, needed
 * because the Nano's HARDWARE I2C SCL pin (A5) was damaged. Hardware I2C (the
 * Wire/TWI peripheral) is fixed to A4=SDA / A5=SCL on the ATmega328P and cannot
 * be remapped, so SCL now lives on A3 — an ordinary GPIO — driven by this
 * bit-banged I2C master. SDA stays on A4.
 *
 * It subclasses Adafruit_GFX, so every drawing/text call the sketch already
 * makes (print, setCursor, setTextSize, setTextColor, setTextWrap, drawCircle,
 * fillCircle, ...) works UNCHANGED — only begin()/clearDisplay()/display() and
 * the single drawPixel() primitive are reimplemented here.
 *
 * SPEED — the two things that make this driver fast where the old one froze
 * the whole firmware for ~130 ms per frame:
 *
 *  1. Direct port registers. The bus pins are toggled with single sbi/cbi
 *     instructions on DDRC/PORTC (~0.25 µs per transition) instead of
 *     pinMode()/digitalWrite() (~4-8 µs each). The settle/rise delays are
 *     sized for the WORST corner (internal-only 20-50 kΩ pullups, see below),
 *     giving ~125 kHz — still ~3.5x the old per-byte throughput (full frame
 *     ~37 ms of BUS time instead of ~130 ms — and no longer blocking, see 2).
 *     The pins are therefore HARDWIRED to A3 = PC3 (SCL) and A4 = PC4 (SDA).
 *
 *  2. Chunked, non-blocking flush. display() only ARMS a frame transfer; the
 *     sketch calls pumpFlush() from loop(), which ships one 32-byte chunk
 *     (~2.3 ms) per call inside a single I2C data transaction held open
 *     across calls (I2C is static — SCL parked low between chunks is legal).
 *     Buttons and serial are serviced between chunks, so a repaint never
 *     blocks input and never overflows the 64-byte RX buffer. No shadow
 *     buffer, no extra RAM: the '328P has 2 KB and the framebuffer already
 *     takes 512 B of it.
 *
 * CONCURRENCY CONTRACT: drawing (clearDisplay / any GFX call / display()) must
 * not run while a flush is in progress — poll flushBusy() first. The sketch
 * guarantees this by rendering only from loop() when flushBusy() is false;
 * serial handlers merely mark the display dirty.
 *
 * I2C is open-drain emulated: a line is pulled LOW by driving the pin OUTPUT-LOW
 * and released HIGH by switching it to INPUT_PULLUP (relies on the bus pull-ups;
 * the internal 20-50 kΩ pull-ups are a fallback if the OLED module has none —
 * the settle delays below are sized for that worst corner). The panel is
 * write-only here, so no register reads.
 */
#ifndef SOFTSSD1306_H
#define SOFTSSD1306_H

#include <Arduino.h>
#include <Adafruit_GFX.h>
#include <avr/pgmspace.h>

// Documentation only — the implementation is hardwired to PC3/PC4 below.
#ifndef OLED_SW_SCL
#define OLED_SW_SCL A3      // software I2C clock (hardware SCL A5 was damaged)
#endif
#ifndef OLED_SW_SDA
#define OLED_SW_SDA A4      // software I2C data
#endif

// Adafruit_SSD1306 colour / option constants the sketch references.
#ifndef SSD1306_BLACK
#define SSD1306_BLACK   0
#define SSD1306_WHITE   1
#define SSD1306_INVERSE 2
#endif
#ifndef SSD1306_SWITCHCAPVCC
#define SSD1306_SWITCHCAPVCC 0x02   // value unused (charge pump always enabled)
#endif

class SoftSSD1306 : public Adafruit_GFX {
 public:
  SoftSSD1306(uint8_t w, uint8_t h) : Adafruit_GFX(w, h), _w(w), _h(h) {}

  // Signature-compatible with Adafruit_SSD1306::begin(vcc, addr). Returns whether
  // the panel ACKed its address (best-effort; we drive the bus regardless).
  bool begin(uint8_t vcc = SSD1306_SWITCHCAPVCC, uint8_t addr = 0x3C) {
    (void)vcc;
    _addr = addr;
    sdaHi(); sclHi();                       // idle the bus high
    static const uint8_t init32[] PROGMEM = {
      0xAE,             // display off
      0xD5, 0x80,       // clock divide ratio / osc freq
      0xA8, 0x1F,       // multiplex ratio = height-1 (31 for 32px)
      0xD3, 0x00,       // display offset 0
      0x40,             // start line 0
      0x8D, 0x14,       // charge pump ON (internal Vcc)
      0x20, 0x00,       // memory addressing mode: horizontal
      0xA1,             // segment remap (column 127 -> SEG0)
      0xC8,             // COM output scan direction remapped
      0xDA, 0x02,       // COM pins config for 128x32
      0x81, 0x8F,       // contrast
      0xD9, 0xF1,       // pre-charge period
      0xDB, 0x40,       // VCOMH deselect level
      0xA4,             // resume to RAM content
      0xA6,             // normal display (not inverted)
      0x2E,             // deactivate scroll
      0xAF,             // display ON
    };
    i2cStart();
    bool ok = i2cWrite(_addr << 1);         // address + write
    i2cWrite(0x00);                         // control: command stream
    for (uint8_t i = 0; i < sizeof(init32); i++)
      i2cWrite(pgm_read_byte(&init32[i]));
    i2cStop();
    clearDisplay();
    display();
    while (flushBusy()) pumpFlush();        // sync the panel RAM (random at power-on)
    return ok;
  }

  void clearDisplay() { memset(_buf, 0, sizeof(_buf)); }

  // Arm a full-frame transfer. The actual bytes go out via pumpFlush() from
  // loop(), one chunk at a time. Never call GFX drawing while flushBusy().
  void display() {
    if (_flushActive) endData();            // restarted mid-flush: close cleanly
    _flushPos = 0;
    _flushActive = true;
  }

  bool flushBusy() const { return _flushActive; }

  // Ship one chunk of the armed frame (~2.3 ms), keeping the I2C data
  // transaction open between calls. Call every loop() iteration; no-op when idle.
  void pumpFlush() {
    if (!_flushActive) return;
    if (_flushPos == 0) {
      // Address the full frame, then open the data transaction. Horizontal
      // addressing auto-advances through columns and wraps pages, so one
      // window setting covers all 512 bytes.
      i2cStart();
      i2cWrite(_addr << 1);
      i2cWrite(0x00);                       // command stream
      i2cWrite(0x21); i2cWrite(0); i2cWrite((uint8_t)(_w - 1));       // columns
      i2cWrite(0x22); i2cWrite(0); i2cWrite((uint8_t)(_h / 8 - 1));   // pages
      i2cStop();
      i2cStart();
      i2cWrite(_addr << 1);
      i2cWrite(0x40);                       // data stream (stays open)
    }
    uint16_t end = _flushPos + FLUSH_CHUNK;
    if (end > sizeof(_buf)) end = sizeof(_buf);
    while (_flushPos < end) i2cWrite(_buf[_flushPos++]);
    if (_flushPos >= sizeof(_buf)) endData();
  }

  // The single primitive Adafruit_GFX builds everything else on.
  void drawPixel(int16_t x, int16_t y, uint16_t color) override {
    if (x < 0 || x >= _w || y < 0 || y >= _h) return;     // rotation 0 only (sketch never rotates)
    uint16_t i = (uint16_t)x + (uint16_t)(y >> 3) * _w;
    uint8_t  m = 1 << (y & 7);
    if (color == SSD1306_BLACK)        _buf[i] &= ~m;
    else if (color == SSD1306_INVERSE) _buf[i] ^=  m;
    else                               _buf[i] |=  m;      // SSD1306_WHITE
  }

 private:
  static const uint16_t FLUSH_CHUNK = 32;   // bytes per pumpFlush() call (~2.3 ms)

  uint8_t  _buf[128 * 32 / 8];              // 512B framebuffer
  uint8_t  _w, _h;
  uint8_t  _addr = 0x3C;
  volatile bool _flushActive = false;       // a frame transfer is in progress
  uint16_t _flushPos = 0;                   // next framebuffer byte to send

  void endData() { i2cStop(); _flushActive = false; }

  // ---- fast software I2C on PC3 (SCL, pin A3) / PC4 (SDA, pin A4) ------------
  // Open-drain emulation with single-instruction sbi/cbi register writes.
  //   release HIGH: DDR<-0 (input) THEN PORT<-1 (pullup)  — never drives high
  //   drive LOW   : PORT<-0 (drop pullup) THEN DDR<-1 (output low)
  // The ordering guarantees the pin never actively drives HIGH, so a slave
  // holding SDA low (ACK) is never shorted against.
  static const uint8_t SCL_MASK = _BV(3);   // A3 = PC3
  static const uint8_t SDA_MASK = _BV(4);   // A4 = PC4

  // always_inline: the bit timing below counts on each transition being the
  // two-instruction sbi/cbi pair (4 cycles), not an out-of-line call the
  // optimizer may or may not add. Deterministic timing > compiler mood.
  __attribute__((always_inline)) static inline void sclHi() { DDRC &= ~SCL_MASK; PORTC |= SCL_MASK; }
  __attribute__((always_inline)) static inline void sclLo() { PORTC &= ~SCL_MASK; DDRC |= SCL_MASK; }
  __attribute__((always_inline)) static inline void sdaHi() { DDRC &= ~SDA_MASK; PORTC |= SDA_MASK; }
  __attribute__((always_inline)) static inline void sdaLo() { PORTC &= ~SDA_MASK; DDRC |= SDA_MASK; }

  // Compile-time delays (16 MHz: 16 cycles = 1 µs), sized for the TRUE worst
  // case of internal-only pullups: the ATmega328P pullup spec is 20-50 kΩ; at
  // 50 kΩ × ~30 pF of bus (τ ≈ 1.5 µs) a released line needs 1.61τ ≈ 2.4 µs to
  // reach the SSD1306's guaranteed VIH of 0.8·VDD. So: released phases get
  // 2.5 µs (dSettle) and the SCL-high window 4 µs (dClkHi) — 2.4 µs rise +
  // 1.6 µs of valid high vs the SSD1306's 0.6 µs tHIGH minimum, ~2.7× margin.
  // Module pullups (usual, 4.7-10k) make all of this comfortable. ~8 µs/bit
  // ≈ 125 kHz; a full 512 B frame is ~37 ms of BUS time, shipped in
  // non-blocking ~2.3 ms chunks by pumpFlush().
  static inline void dSettle() { __builtin_avr_delay_cycles(40); }  // released-line rise (2.5 µs)
  static inline void dClkHi()  { __builtin_avr_delay_cycles(64); }  // SCL high incl. rise (4 µs)
  static inline void dClkLo()  { __builtin_avr_delay_cycles(8);  }  // SCL low tail (0.5 µs)

  // START: give BOTH lines a full double settle from a possibly-just-released
  // bus before pulling SDA low (SDA must be solidly ≥ VIH first).
  void i2cStart() { sdaHi(); sclHi(); dSettle(); dSettle(); sdaLo(); dSettle(); sclLo(); dClkLo(); }
  void i2cStop()  { sdaLo(); dSettle(); sclHi(); dSettle(); sdaHi(); dSettle(); }

  bool i2cWrite(uint8_t b) {
    for (uint8_t i = 0; i < 8; i++) {
      if (b & 0x80) sdaHi(); else sdaLo();
      dSettle();
      sclHi(); dClkHi();
      sclLo(); dClkLo();
      b <<= 1;
    }
    sdaHi();                                // release SDA so the slave can ACK
    dSettle();
    sclHi(); dClkHi();
    bool ack = !(PINC & SDA_MASK);
    sclLo(); dClkLo();
    return ack;
  }
};

#endif  // SOFTSSD1306_H
