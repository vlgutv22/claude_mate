#pragma once
/*
 * SHIP IT -- the game, on the device.
 * ==================================
 *
 * The browser prototype in game/proto is where the feel of this was worked out;
 * this is the same game with the same numbers, driven by the four switches. Every
 * constant below is the one the prototype ships, deliberately: a physics constant
 * that exists twice in two places is a physics constant that will drift, and the
 * jump arc here is not a taste -- the level is BUILT to it (see level_01.h).
 *
 * WHAT IS DIFFERENT ON THE DEVICE, and why:
 *
 * - THE SIMULATION IS 60 Hz AND THE DISPLAY IS NOT. Arduino_Canvas::flush()
 *   pushes all 110 KB of the frame over SPI at 40 MHz whatever changed, which is
 *   ~28 ms, so the panel tops out near 30 fps. Running the physics at the frame
 *   rate would therefore halve every speed and turn the jump arc into a
 *   different jump arc. So tick() accumulates real elapsed time and runs as many
 *   fixed 60 Hz steps as it owes -- usually two per drawn frame -- and the arc
 *   is the arc regardless of what the panel manages.
 *
 * - INPUT IS READ AS PIN STATE, NOT AS EVENTS. The firmware's pollNavBtn() emits
 *   a press and then auto-repeats at 400/200 ms, which is right for a menu and
 *   useless for a platformer: what a platformer needs is "is it held down THIS
 *   step". So the nav buttons are read here, raw, with their own short debounce,
 *   and claude_mate_s3.ino suppresses its own handling while UI_GAME is up.
 *
 * - THERE IS NO SOUND. Not an omission: this board has no DAC, no speaker, and
 *   a backlight circuit with no inductor to abuse. Sound belongs to the daemon,
 *   over the link, and is not wired here yet.
 */

#include "level_01.h"

// Measured, not chosen. Walk 1.45, jump -4.75, gravity 0.30 puts the apex at
// 35.3 px and a full jump 45 px downrange: two tiles across with 13 px to spare,
// ONE row up with 19.3 px to spare and two rows up with 3.3 -- which is to say,
// not two. level_01.h is built to exactly these numbers, so changing one of them
// silently invalidates the level.
static const float SHIP_GRAV = 0.30f, SHIP_WALK = 1.45f,
                   SHIP_JUMP = -4.75f, SHIP_MAXFALL = 7.0f;

class ShipIt {
public:
  enum Result : uint8_t { RUNNING, EXIT };

  // ---- difficulty -----------------------------------------------------------
  // `walk` is the design input, not the drain: it is what a perfect straight run
  // SHOULD cost in days. The drain is derived from it. The first version of this
  // table set a flat drain and asserted a walk cost that was never measured --
  // the level actually cost 2.76 days against a 7-day budget, so the deadline,
  // which the whole design rests on, did nothing. Now the tier IS the gap
  // between `days` and `walk`, and above SPRINT the map has to be mined.
  struct Tier {
    const char *name;
    const char *sub;
    uint8_t     days;      // starting budget, and the cap: you can never bank
                           // more schedule than the sprint was given
    uint8_t     walk;      // days a perfect straight run costs
    float       spd;       // enemy px/step
    float       pr;        // days per merged PR
    float       bug;       // days lost when a bug reaches you
    float       prio;      // ...and when a priority change does
  };
  static const uint8_t TIERS = 5;

  void begin() { _tier = 0; _tutorial = true; _row = 0; _state = START; }

  // Called from the menu. Always lands on the start screen rather than resuming:
  // a half-finished sprint you cannot see the state of is worse than a new one.
  void open() { begin(); _lastMs = 0; _acc = 0; }

  Result tick();
  void   draw(Arduino_GFX *g);

  // The 4th button, forwarded from the .ino. Returns true if the game is done
  // with the screen and the caller should go back to the menu.
  bool  fourth() {
    if (_state == START) return true;       // nothing to abandon yet
    _state = START;                          // otherwise: save nothing, go back
    return false;                            //   to the start screen first, so a
  }                                          //   fat-fingered tap is not fatal

private:
  enum St : uint8_t { START, PLAY, CARD, SLIP, SHIPPED };

  // ---- geometry -------------------------------------------------------------
  static const int16_t T      = LVL1_TILE;         // 16
  static const int16_t ROWS   = LVL1_ROWS;         // 7
  static const int16_t COLS   = LVL1_COLS;         // 108
  static const int16_t HUD_H  = 24;
  static const int16_t PLAY_Y = HUD_H + 12;        // 36; 36 + 7*16 = 148 of 172

  // ---- physics --------------------------------------------------------------
  static const uint8_t COYOTE = 6, BUFFER = 6;     // frames

  struct Bug  { float c; uint8_t r, from, to; int8_t dir; bool dead; };
  struct Prio { float c, y, top, bot; int8_t dir; bool dead; };
  struct Pr   { uint8_t c, r; bool got; };

  // ---- state ----------------------------------------------------------------
  St       _state    = START;
  uint8_t  _tier     = 0;
  bool     _tutorial = true;
  uint8_t  _row      = 0;                    // start-screen selection

  float    _x = 0, _y = 0, _vx = 0, _vy = 0, _cam = 0, _days = 0;
  bool     _onGround = false;
  int8_t   _face     = 1;
  uint8_t  _coyote = 0, _buffer = 0, _inv = 0, _squash = 0;
  uint16_t _t = 0, _flash = 0, _shipT = 0;
  uint8_t  _merged = 0;
  bool     _seenBug = false, _seenPrio = false;
  uint8_t  _card    = 0;                     // 1 = bug, 2 = priority change
  char     _msg[26] = {0};
  uint16_t _msgCol  = GH_TEXT;

  Bug  _bugs[LVL1_BUG_COUNT];
  Prio _prios[LVL1_PRIO_COUNT];
  Pr   _prs[LVL1_PR_COUNT];

  // ---- input ----------------------------------------------------------------
  bool _left = false, _right = false, _jump = false, _jumpWas = false;
  bool _navWas[3]  = {false, false, false};  // start-screen edge detection
  unsigned long _debounce[3] = {0, 0, 0};

  // ---- timing ---------------------------------------------------------------
  unsigned long _lastMs = 0;
  uint16_t      _acc    = 0;                 // thirds of a millisecond

  void reset();
  void readPins();
  void startButton(uint8_t k);               // 0 = prev, 1 = go, 2 = next
  void step();
  void toast(const char *m, uint16_t col);
  void gain(float v, const char *label, uint16_t col);
  bool solid(int c, int r) const {
    return c >= 0 && c < COLS && r >= 0 && r < ROWS && LVL1_MAP[r][c] != '.';
  }
  bool hits(float px, float py) const;
  void drawStart(Arduino_GFX *g);
  void drawPlay(Arduino_GFX *g);
  void drawCard(Arduino_GFX *g);
  void blit(Arduino_GFX *g, const char *const *b, uint8_t h, uint8_t w,
            int16_t x, int16_t y, uint16_t col, bool flip);
  void dayStr(char *out, float v) const;
};

// -----------------------------------------------------------------------------
// The tiers
// -----------------------------------------------------------------------------
static const ShipIt::Tier SHIP_TIERS[ShipIt::TIERS] = {
  {"SPRINT",        "the plan, as written",              7, 5, 0.58f, 1.00f, 1.0f, 2.0f},
  {"CRUNCH",        "someone promised a demo",           6, 6, 0.72f, 1.00f, 1.0f, 2.0f},
  {"CODE FREEZE",   "the branch is cut, the date is not",5, 7, 0.88f, 1.00f, 1.5f, 2.5f},
  {"DEATH MARCH",   "the slack was spent last week",     4, 8, 1.05f, 0.75f, 1.5f, 3.0f},
  {"HOTFIX FRIDAY", "already broken in production",      3, 9, 1.25f, 0.50f, 2.0f, 3.0f},
};

// A straight run is 105 tiles at 1.45 px/step. Everything about the clock is
// derived from this one measurement.
#define SHIP_CROSS 1159.0f

// -----------------------------------------------------------------------------
// Lifecycle
// -----------------------------------------------------------------------------
inline void ShipIt::reset() {
  const Tier &d = SHIP_TIERS[_tier];
  _x = T; _y = 4 * T; _vx = _vy = 0; _cam = 0;
  _days = d.days;
  _onGround = false; _face = 1;
  _coyote = _buffer = _inv = _squash = 0;
  _t = _flash = _shipT = 0; _merged = 0;
  _seenBug = _seenPrio = !_tutorial;
  _card = 0; _msg[0] = 0;
  for (uint8_t i = 0; i < LVL1_BUG_COUNT; i++) {
    _bugs[i].c = LVL1_BUGS[i].col * T; _bugs[i].r = LVL1_BUGS[i].row;
    _bugs[i].from = LVL1_BUGS[i].from; _bugs[i].to = LVL1_BUGS[i].to;
    _bugs[i].dir = 1; _bugs[i].dead = false;
  }
  for (uint8_t i = 0; i < LVL1_PRIO_COUNT; i++) {
    _prios[i].c   = LVL1_PRIOS[i].col * T;
    _prios[i].y   = LVL1_PRIOS[i].row * T;
    _prios[i].top = LVL1_PRIOS[i].from * T;
    _prios[i].bot = LVL1_PRIOS[i].to * T;
    _prios[i].dir = 1; _prios[i].dead = false;
  }
  for (uint8_t i = 0; i < LVL1_PR_COUNT; i++) {
    _prs[i].c = LVL1_PRS[i].col; _prs[i].r = LVL1_PRS[i].row; _prs[i].got = false;
  }
  _jumpWas = true;      // swallow the GO that started the run
  _state = PLAY;
}

// -----------------------------------------------------------------------------
// Input
// -----------------------------------------------------------------------------
// Raw pin state with a short per-key debounce. No auto-repeat, no event queue:
// a platformer asks "is it down now", and a jump edge that bounces would fire a
// second jump inside the coyote window.
inline void ShipIt::readPins() {
  static const uint8_t PINS[3] = {PIN_BTN_PREV, PIN_BTN_GO, PIN_BTN_NEXT};
  unsigned long now = millis();
  bool now3[3];
  for (uint8_t i = 0; i < 3; i++) {
    bool raw = (digitalRead(PINS[i]) == LOW);
    if (raw != _navWas[i] && (now - _debounce[i]) >= 6) {
      _navWas[i]   = raw;
      _debounce[i] = now;
      if (raw && _state != PLAY) startButton(i);
    }
    now3[i] = _navWas[i];
  }
  _left = now3[0]; _jump = now3[1]; _right = now3[2];
}

// The start screen and the enemy cards use the SAME convention as the device's
// own settings page -- PREV/NEXT move, GO acts -- so there is nothing new to
// learn to start a run.
inline void ShipIt::startButton(uint8_t k) {
  if (_state == CARD) { _card = 0; _jumpWas = true; _state = PLAY; return; }
  if (_state == SLIP || _state == SHIPPED) { _state = START; return; }
  if (k == 0) _row = (uint8_t)((_row + 2) % 3);
  else if (k == 2) _row = (uint8_t)((_row + 1) % 3);
  else {
    if      (_row == 0) _tier = (uint8_t)((_tier + 1) % TIERS);
    else if (_row == 1) _tutorial = !_tutorial;
    else                reset();
  }
}

// -----------------------------------------------------------------------------
// The clock
// -----------------------------------------------------------------------------
inline ShipIt::Result ShipIt::tick() {
  unsigned long now = millis();
  if (!_lastMs) _lastMs = now;
  unsigned long dt = now - _lastMs;
  _lastMs = now;
  // A flush, a flash commit or a WiFi stall must not fast-forward the level.
  if (dt > 100) dt = 100;

  readPins();

  // Thirds of a millisecond, so 50 units is exactly 16.667 ms and the step rate
  // is exactly 60 Hz rather than the 62.5 that whole milliseconds would give.
  _acc += (uint16_t)(dt * 3);
  uint8_t n = 0;
  while (_acc >= 50 && n < 6) { step(); _acc -= 50; n++; }
  return RUNNING;
}

inline void ShipIt::toast(const char *m, uint16_t col) {
  uint8_t i = 0;
  while (m[i] && i < sizeof(_msg) - 1) { _msg[i] = m[i]; i++; }
  _msg[i] = 0;
  _msgCol = col;
  _flash  = 52;
}

inline void ShipIt::dayStr(char *out, float v) const {
  // Days come in quarters and halves, and "0.75 days" reads like a spreadsheet
  // rather than a schedule. The panel font has no fraction glyphs, so spell them.
  int   w = (int)v;
  float f = v - w;
  const char *fr = (f > 0.7f) ? "3/4" : (f > 0.45f) ? "1/2" : (f > 0.2f) ? "1/4" : "";
  if (w && *fr)      sprintf(out, "%d %s", w, fr);
  else if (*fr)      sprintf(out, "%s", fr);
  else               sprintf(out, "%d", w);
}

// You cannot bank more schedule than the sprint was given: merging early does
// not move the date, it only stops you losing it. When the cap bites, SAY so --
// a reward that silently does nothing teaches the wrong rule.
inline void ShipIt::gain(float v, const char *label, uint16_t col) {
  float cap = SHIP_TIERS[_tier].days;
  char  buf[26];
  if (_days >= cap) { sprintf(buf, "%s AT CAP", label); toast(buf, C_DIM); return; }
  float got = (_days + v > cap) ? cap - _days : v;
  _days += got;
  char d[8]; dayStr(d, got);
  sprintf(buf, "%s +%sd", label, d);
  toast(buf, col);
}

inline bool ShipIt::hits(float px, float py) const {
  int x0 = (int)floorf((px + 1) / T), x1 = (int)floorf((px + 12) / T);
  int y0 = (int)floorf(py / T),       y1 = (int)floorf((py + 11) / T);
  for (int c = x0; c <= x1; c++)
    for (int r = y0; r <= y1; r++)
      if (solid(c, r)) return true;
  return false;
}

// -----------------------------------------------------------------------------
// One 60 Hz step
// -----------------------------------------------------------------------------
inline void ShipIt::step() {
  _t++;
  if (_state == SHIPPED) { _shipT++; return; }
  if (_state != PLAY) return;

  const Tier &d = SHIP_TIERS[_tier];
  _days -= d.walk / SHIP_CROSS;
  if (_days <= 0) {
    _days = 0; _state = SLIP; toast("MILESTONE SLIPPED", C_ERROR); return;
  }

  _vx = (_right ? SHIP_WALK : 0) - (_left ? SHIP_WALK : 0);
  if (_vx != 0) _face = (_vx > 0) ? 1 : -1;

  // Coyote time and jump buffering are not polish. With a fixed arc and no air
  // control the launch window for these gaps is about 13 px -- eight frames --
  // and a sweep of scripted timings cleared the tutorial gap once in five
  // without them, four in five with.
  if (_onGround) _coyote = COYOTE; else if (_coyote) _coyote--;
  if (_jump && !_jumpWas) _buffer = BUFFER; else if (_buffer) _buffer--;
  if (_buffer && _coyote) { _vy = SHIP_JUMP; _onGround = false; _buffer = _coyote = 0; }
  if (!_jump && _vy < -1.8f) _vy = -1.8f;    // release to cut the jump short
  _jumpWas = _jump;

  _vy += SHIP_GRAV; if (_vy > SHIP_MAXFALL) _vy = SHIP_MAXFALL;

  float nx = _x + _vx; if (!hits(nx, _y)) _x = nx;
  float ny = _y + _vy;
  if (!hits(_x, ny)) { _y = ny; _onGround = false; }
  else {
    if (_vy > 0) { _y = floorf((ny + 11) / T) * T - 12; _onGround = true; }
    else         { _y = floorf(ny / T) * T + T; }
    _vy = 0;
  }

  // RESTING CONTACT. Without this, standing still is not a state -- it is a
  // three-frame cycle. Gravity applies every step, so from a clean landing the
  // character falls 0.3 px (no overlap yet, so onGround goes false), then 0.6,
  // then on the third step finally overlaps and is snapped back out: a 1 px
  // vertical vibration at 20 Hz that reads as the sprite shaking, with onGround
  // true on a THIRD of frames -- so coyote time, the thing that makes the jumps
  // land, was being refreshed off a flickering signal. Guarded on vy >= 0 or it
  // cancels the first frame of every jump.
  if (_vy >= 0 && hits(_x, _y + 1)) {
    _y = floorf((_y + 12) / T) * T - 12; _vy = 0; _onGround = true;
  }

  if (_x < 0) _x = 0;
  if (_x > (COLS - 1) * T) _x = (COLS - 1) * T;

  if (_y > ROWS * T + 40) {                  // fell out of the world
    _days -= 1;
    toast("-1d  fell", C_WAIT);
    int c = (int)(_x / T);
    while (c > 0 && !solid(c, 5) && !solid(c, 6)) c--;
    int r = 6; while (r > 0 && !solid(c, r)) r--;
    _x = c * T; _y = (r - 1) * T; _vy = 0; _inv = 60;
  }

  for (uint8_t i = 0; i < LVL1_BUG_COUNT; i++) {
    Bug &b = _bugs[i];
    if (b.dead) continue;
    b.c += b.dir * d.spd;
    int col = (int)floorf((b.c + 6) / T);
    // Turn at the ends of the patrol AND at any edge it would walk off: an enemy
    // that strolls into a hole is a bug in the game about bugs.
    if (col <= b.from || col >= b.to || !solid(col, b.r + 1)) b.dir = (int8_t)-b.dir;
    float bx = b.c, by = b.r * T + 6;
    if (fabsf(bx - _x) < 12 && fabsf(by - _y) < 12) {
      if (_vy > 0.6f && _y < by - 2) {
        b.dead = true; _vy = -3.2f; _squash = 18;
        gain(0.5f, "FIXED", GH_L2);
      } else if (!_inv) {
        _days -= d.bug;
        char m[26], ds[8]; dayStr(ds, d.bug); sprintf(m, "-%sd  bug", ds);
        toast(m, GH_BUG);
        _x -= _face * 15; _vy = -2.5f; _inv = 75;
      }
    }
  }
  if (_inv) _inv--;

  for (uint8_t i = 0; i < LVL1_PRIO_COUNT; i++) {
    Prio &p = _prios[i];
    if (p.dead) continue;
    p.y += p.dir * (d.spd * 1.3f);
    if (p.y <= p.top || p.y >= p.bot) p.dir = (int8_t)-p.dir;
    if (fabsf(p.c - _x) < 13 && fabsf(p.y - _y) < 13 && !_inv) {
      _days -= d.prio;
      char m[26], ds[8]; dayStr(ds, d.prio); sprintf(m, "RE-SCOPED -%sd", ds);
      toast(m, GH_PRIO);
      _x = (_x > 46) ? _x - 46 : 0; _vy = -2.2f; _inv = 80;
    }
  }

  for (uint8_t i = 0; i < LVL1_PR_COUNT; i++) {
    Pr &p = _prs[i];
    if (p.got) continue;
    if (fabsf(p.c * T - _x) < 14 && fabsf(p.r * T - _y) < 14) {
      p.got = true; _merged++;
      gain(d.pr, "PR MERGED", GH_PR);
    }
  }

  if (_flash) _flash--;
  if (_squash) _squash--;

  // The enemy codex: the first time each kind comes on screen, freeze and say
  // what it does. Being taught by dying is not a tutorial, it is a toll.
  float right = _cam + SCREEN_W;
  if (!_seenBug) {
    for (uint8_t i = 0; i < LVL1_BUG_COUNT; i++)
      if (!_bugs[i].dead && _bugs[i].c > _cam + 40 && _bugs[i].c < right - 24) {
        _seenBug = true; _card = 1; _state = CARD; break;
      }
  }
  if (_state == PLAY && !_seenPrio) {
    for (uint8_t i = 0; i < LVL1_PRIO_COUNT; i++)
      if (!_prios[i].dead && _prios[i].c > _cam + 40 && _prios[i].c < right - 24) {
        _seenPrio = true; _card = 2; _state = CARD; break;
      }
  }

  if ((int)(_x / T) >= COLS - 2) { _state = SHIPPED; _shipT = 0; }

  _cam = _x - SCREEN_W / 2 + 7;
  if (_cam < 0) _cam = 0;
  float camMax = COLS * T - SCREEN_W;
  if (_cam > camMax) _cam = camMax;
}

// -----------------------------------------------------------------------------
// Drawing
// -----------------------------------------------------------------------------
inline void ShipIt::blit(Arduino_GFX *g, const char *const *b, uint8_t h,
                         uint8_t w, int16_t x, int16_t y, uint16_t col,
                         bool flip) {
  for (uint8_t r = 0; r < h; r++) {
    const char *row = b[r];
    // Walk runs of set pixels rather than plotting one at a time: at 14 px wide
    // a row is one or two rects instead of fourteen calls.
    uint8_t c = 0;
    while (c < w) {
      if (row[c] != '1') { c++; continue; }
      uint8_t s = c;
      while (c < w && row[c] == '1') c++;
      int16_t px = flip ? (int16_t)(x + w - c) : (int16_t)(x + s);
      g->fillRect(px, (int16_t)(y + r), (int16_t)(c - s), 1, col);
    }
  }
}

inline void ShipIt::draw(Arduino_GFX *g) {
  if (_state == START) { drawStart(g); return; }
  drawPlay(g);
  if (_state == CARD) drawCard(g);
}

inline void ShipIt::drawStart(Arduino_GFX *g) {
  const Tier &d = SHIP_TIERS[_tier];
  g->fillScreen(GH_BG);

  g->setTextSize(3); g->setTextColor(GH_L1);
  g->setCursor(10, 12); g->print("SHIP IT");
  g->setTextSize(1); g->setTextColor(C_DIM);
  g->setCursor(12, 40); g->print("walk your graph to the milestone");

  // A little run of commits with Mate standing on it, so the start screen says
  // what the terrain is before the level has to.
  static const uint16_t ramp[5] = {GH_L1, GH_L2, GH_L2, GH_L4, GH_L2};
  for (uint8_t i = 0; i < 5; i++) g->fillRect(232 + i * 17, 34, 15, 15, ramp[i]);
  blit(g, MATE_BITS, MATE_H, MATE_W, 246, 20, GH_MATE, false);

  const char *labels[3] = {"DIFFICULTY", "TUTORIAL", "START SPRINT"};
  char sub[52], val[16];
  for (uint8_t i = 0; i < 3; i++) {
    int16_t y = 58 + i * 34;
    if (i == _row) {
      g->fillRect(0, y - 4, SCREEN_W, 30, RGB565(22, 27, 34));
      g->fillRect(0, y - 4, 3, 30, GH_MATE);
    }
    g->setTextSize(2); g->setTextColor(i == _row ? GH_TEXT : C_DIM);
    g->setCursor(10, y); g->print(labels[i]);

    val[0] = 0; sub[0] = 0;
    if (i == 0) { snprintf(val, sizeof(val), "%s", d.name);
                  snprintf(sub, sizeof(sub), "%s", d.sub); }
    if (i == 1) { snprintf(val, sizeof(val), "%s", _tutorial ? "ON" : "OFF");
                  snprintf(sub, sizeof(sub), "%s", _tutorial
                           ? "freeze and explain each new enemy"
                           : "no cards, straight in"); }
    if (i == 2) snprintf(sub, sizeof(sub), "%dd budget - the walk alone costs %d",
                         d.days, d.walk);
    if (val[0]) {
      int16_t vw = (int16_t)strlen(val) * GLYPH_W * 2;
      g->setTextColor(GH_L2);
      g->setCursor(SCREEN_W - 8 - vw, y); g->print(val);
    }
    g->setTextSize(1); g->setTextColor(C_DIM);
    g->setCursor(10, y + 18); g->print(sub);
  }
  g->setTextSize(1); g->setTextColor(C_DIMMER);
  g->setCursor(10, 162); g->print("PREV/NEXT move   GO change or start   4th exit");
}

inline void ShipIt::drawPlay(Arduino_GFX *g) {
  const Tier &d = SHIP_TIERS[_tier];
  g->fillScreen(GH_BG);

  // ---- HUD ----
  g->setTextSize(1); g->setTextColor(GH_TEXT);
  g->setCursor(6, 8); g->print(LVL1_NAME);
  blit(g, PR_BITS, PR_H, PR_W, 128, 4, GH_PR, false);
  char buf[24];
  sprintf(buf, "%d/%d", _merged, (int)LVL1_PR_COUNT);
  g->setTextColor(GH_PR); g->setCursor(143, 8); g->print(buf);

  float   days = _days > 0 ? _days : 0;
  int16_t bw   = 78;
  float   frac = days / d.days; if (frac > 1) frac = 1;
  g->fillRect(SCREEN_W - 6 - bw - 32, 5, bw, 10, C_DIMMER);
  g->fillRect(SCREEN_W - 6 - bw - 32, 5, (int16_t)(bw * frac), 10,
              frac > 0.5f ? GH_L2 : frac > 0.25f ? GH_PRIO : GH_BUG);
  sprintf(buf, "%dd", (int)ceilf(days));
  g->setTextColor(GH_TEXT); g->setCursor(SCREEN_W - 26, 8); g->print(buf);
  g->fillRect(0, HUD_H - 1, SCREEN_W, 1, C_DIMMER);

  // ---- terrain ----
  int16_t camI = (int16_t)_cam;
  int c0 = camI / T - 1; if (c0 < 0) c0 = 0;
  int c1 = c0 + 22;      if (c1 > COLS - 1) c1 = COLS - 1;
  for (int c = c0; c <= c1; c++) {
    int16_t sx = (int16_t)(c * T - camI);
    for (int r = 0; r < ROWS; r++) {
      int16_t sy = (int16_t)(PLAY_Y + r * T);
      char    ch = LVL1_MAP[r][c];
      if (ch == '.') g->drawRect(sx + 2, sy + 2, T - 4, T - 4, GH_GRID);
      else {
        uint16_t col = (ch == '1') ? GH_L1 : (ch == '2') ? GH_L2
                     : (ch == '3') ? GH_L3 : GH_L4;
        if (_state == SHIPPED && c * 3 < _shipT) col = GH_L1;
        g->fillRect(sx + 1, sy + 1, T - 2, T - 2, col);
      }
    }
  }
  // the milestone marker
  g->fillRect((int16_t)((COLS - 1) * T - camI + 2), PLAY_Y, 3, ROWS * T,
              _state == SHIPPED ? GH_L1 : GH_PR);

  // ---- actors ----
  for (uint8_t i = 0; i < LVL1_PR_COUNT; i++)
    if (!_prs[i].got) {
      int16_t bob = (int16_t)(sinf((_t + _prs[i].c * 9) / 17.0f) * 1.6f);
      blit(g, PR_BITS, PR_H, PR_W, (int16_t)(_prs[i].c * T - camI + 3),
           (int16_t)(PLAY_Y + _prs[i].r * T + 3 + bob), GH_PR, false);
    }
  for (uint8_t i = 0; i < LVL1_BUG_COUNT; i++)
    if (!_bugs[i].dead)
      blit(g, BUG_BITS, BUG_H, BUG_W, (int16_t)(_bugs[i].c - camI),
           (int16_t)(PLAY_Y + _bugs[i].r * T + 6), GH_BUG, _bugs[i].dir < 0);
  for (uint8_t i = 0; i < LVL1_PRIO_COUNT; i++)
    if (!_prios[i].dead)
      blit(g, PRIO_BITS, PRIO_H, PRIO_W, (int16_t)(_prios[i].c - camI),
           (int16_t)(PLAY_Y + _prios[i].y + 2), GH_PRIO, false);

  // Pick a pose; never scale one. Bottom-aligned to the feet so a pose change
  // does not read as a hop.
  const char *const *pose = MATE_BITS;
  uint8_t            ph   = MATE_H;
  if (_squash)                      { pose = MATE_SQUASH_BITS;  ph = MATE_SQUASH_H; }
  else if (!_onGround && _vy < -1)  { pose = MATE_STRETCH_BITS; ph = MATE_STRETCH_H; }
  if (!(_inv && ((_t >> 2) & 1)))
    blit(g, pose, ph, MATE_W, (int16_t)(_x - camI),
         (int16_t)(PLAY_Y + _y + (MATE_H - ph)), GH_MATE, _face < 0);

  // ---- footer ----
  if (_state == SLIP) {
    g->fillRect(0, 60, SCREEN_W, 50, RGB565(13, 17, 23));
    g->setTextSize(2); g->setTextColor(GH_BUG);
    g->setCursor(40, 72); g->print("MILESTONE SLIPPED");
    g->setTextSize(1); g->setTextColor(C_DIM);
    g->setCursor(78, 94); g->print("any button to go back");
  } else if (_state == SHIPPED) {
    g->fillRect(0, 58, SCREEN_W, 56, RGB565(13, 17, 23));
    g->fillRect(0, 58, SCREEN_W, 1, GH_L2);
    g->fillRect(0, 113, SCREEN_W, 1, GH_L2);
    g->setTextSize(2); g->setTextColor(GH_L1);
    g->setCursor(44, 72); g->print("MILESTONE SHIPPED");
    g->setTextSize(1); g->setTextColor(C_DIM);
    sprintf(buf, "%d/%d merged - %dd left", _merged, (int)LVL1_PR_COUNT,
            (int)ceilf(_days));
    g->setCursor(88, 96); g->print(buf);
  } else if (_flash && _msg[0]) {
    g->setTextSize(1); g->setTextColor(_msgCol);
    int16_t w = (int16_t)strlen(_msg) * GLYPH_W;
    g->setCursor(SCREEN_W / 2 - w / 2, 158); g->print(_msg);
  }
}

// The enemy codex. Freezing the game to explain a thing the player has not been
// hurt by yet is the whole point: the alternative is teaching by ambush.
inline void ShipIt::drawCard(Arduino_GFX *g) {
  bool bug = (_card == 1);
  g->fillRect(20, 34, SCREEN_W - 40, 104, RGB565(13, 17, 23));
  g->drawRect(20, 34, SCREEN_W - 40, 104, bug ? GH_BUG : GH_PRIO);

  if (bug) blit(g, BUG_BITS,  BUG_H,  BUG_W,  36, 50, GH_BUG,  false);
  else     blit(g, PRIO_BITS, PRIO_H, PRIO_W, 36, 50, GH_PRIO, false);

  g->setTextSize(2); g->setTextColor(bug ? GH_BUG : GH_PRIO);
  g->setCursor(60, 48); g->print(bug ? "BUG" : "PRIORITY CHANGE");

  g->setTextSize(1); g->setTextColor(GH_TEXT);
  const Tier &d = SHIP_TIERS[_tier];
  char l1[44], l2[44];
  char ds[8];
  if (bug) {
    dayStr(ds, d.bug);
    sprintf(l1, "reaches you: -%sd and knocked back", ds);
    sprintf(l2, "land on it:  +1/2d, and it is gone");
  } else {
    dayStr(ds, d.prio);
    sprintf(l1, "reaches you: -%sd and shoved back", ds);
    sprintf(l2, "CANNOT be stomped - go around it");
  }
  g->setCursor(36, 78);  g->print(l1);
  g->setCursor(36, 92);  g->print(l2);
  g->setTextColor(C_DIM);
  g->setCursor(36, 116); g->print("any button to continue");
}
