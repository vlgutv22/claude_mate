/*
 * softssd1306.h — software (bit-banged) I2C SSD1306 driver
 * ========================================================
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
 * I2C is open-drain emulated: a line is pulled LOW by driving the pin OUTPUT-LOW
 * and released HIGH by switching it to INPUT_PULLUP (relies on the bus pull-ups;
 * the internal ~30-50k pull-ups are a fallback if the OLED module has none). The
 * clock is naturally slow (Arduino digitalWrite/pinMode overhead), well within
 * the SSD1306's tolerance. The panel is write-only here, so no register reads.
 */
#ifndef SOFTSSD1306_H
#define SOFTSSD1306_H

#include <Arduino.h>
#include <Adafruit_GFX.h>

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
    sda1(); scl1();                         // idle the bus high
    static const uint8_t init32[] = {
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
    for (uint8_t i = 0; i < sizeof(init32); i++) i2cWrite(init32[i]);
    i2cStop();
    clearDisplay();
    display();
    return ok;
  }

  void clearDisplay() { memset(_buf, 0, sizeof(_buf)); }

  void display() {
    cmd3(0x21, 0x00, (uint8_t)(_w - 1));        // column range 0..W-1
    cmd3(0x22, 0x00, (uint8_t)((_h / 8) - 1));  // page range 0..(H/8 -1)
    i2cStart();
    i2cWrite(_addr << 1);
    i2cWrite(0x40);                              // control: data stream
    for (uint16_t i = 0; i < sizeof(_buf); i++) i2cWrite(_buf[i]);
    i2cStop();
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
  uint8_t  _buf[128 * 32 / 8];            // 512B framebuffer (same size Adafruit used)
  uint8_t  _w, _h;
  uint8_t  _addr = 0x3C;

  // ---- software I2C (open-drain: LOW = drive output low, HIGH = release+pullup)
  static inline void scl1() { pinMode(OLED_SW_SCL, INPUT_PULLUP); }
  static inline void scl0() { digitalWrite(OLED_SW_SCL, LOW); pinMode(OLED_SW_SCL, OUTPUT); }
  static inline void sda1() { pinMode(OLED_SW_SDA, INPUT_PULLUP); }
  static inline void sda0() { digitalWrite(OLED_SW_SDA, LOW); pinMode(OLED_SW_SDA, OUTPUT); }
  static inline void hold() { delayMicroseconds(3); }      // half-bit settle margin

  void i2cStart() { sda1(); scl1(); hold(); sda0(); hold(); scl0(); hold(); }
  void i2cStop()  { sda0(); hold(); scl1(); hold(); sda1(); hold(); }

  bool i2cWrite(uint8_t b) {
    for (uint8_t i = 0; i < 8; i++) {
      if (b & 0x80) sda1(); else sda0();
      hold(); scl1(); hold(); scl0();
      b <<= 1;
    }
    sda1();                                 // release SDA so the slave can ACK
    hold(); scl1(); hold();
    bool ack = (digitalRead(OLED_SW_SDA) == LOW);
    scl0();
    return ack;
  }

  void cmd3(uint8_t a, uint8_t b, uint8_t c) {
    i2cStart();
    i2cWrite(_addr << 1);
    i2cWrite(0x00);                         // command stream
    i2cWrite(a); i2cWrite(b); i2cWrite(c);
    i2cStop();
  }
};

#endif  // SOFTSSD1306_H
