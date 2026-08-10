#pragma once
/*
 * SHIP IT -- the game, on the device.
 * ==================================
 *
 * The browser prototype in game/proto is where the feel of this was worked out;
 * this is the same game with the same numbers, driven by the four switches. Every
 * constant below is the one the prototype ships, deliberately: a physics constant
 * that exists twice in two places is a physics constant that will drift, and the
 * jump arc here is not a taste -- the levels are BUILT to it (see level_01.h).
 *
 * TWO LEVELS, ONE ENGINE. Nothing below knows which level it is drawing: the
 * campaign table holds the map, the width and the actor rosters, and the engine
 * reads only through it. That is why level 2 cost a data row rather than a
 * second renderer, and it is the same shape the web engine uses.
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
 * - THE MAC IS THE SPEAKER. This board has no DAC, no speaker and a backlight
 *   circuit with no inductor to abuse, so a noise can only be made at the other
 *   end of the link: the game emits `O|SFX|<code>` and the daemon plays a system
 *   sound. The honest consequence is that a run played with the daemon down is
 *   silent, and nothing in this file can change that.
 */

#include <Preferences.h>
#include "level_01.h"
#include "level_02.h"

// Progress lives in its OWN NVS namespace, not in the settings one. A game
// record is not a device setting: it is written on a completely different
// schedule (once per cleared level, versus on every menu fiddle), and keeping
// them apart means a corrupt or version-bumped settings blob cannot take your
// record with it. factoryResetAll() clears this too -- a "factory reset" that
// left a high score behind would be a lie in the other direction.
#define GAME_NS "mate-game"

// Twelve milestones are designed (see docs/GAME.md); two are built. The bitmask
// was sized for all twelve from the start, so clearing level 2 needed no
// migration of the saved blob -- the record written by the level-1-only
// firmware is read back unchanged here.
#define SHIP_LEVELS 12

// -----------------------------------------------------------------------------
// The campaign
// -----------------------------------------------------------------------------
// How many are BUILT, as opposed to designed. Adding level 3 is adding a
// header and a row to the table below; nothing in the engine counts levels.
#define SHIP_BUILT 2

// The maps are [ROWS][COLS+1] arrays of different widths, so they cannot share
// a pointer type -- a table of row pointers can, at 7 words per level.
static const char *const SHIP_MAP_1[LVL1_ROWS] = {
  LVL1_MAP[0], LVL1_MAP[1], LVL1_MAP[2], LVL1_MAP[3],
  LVL1_MAP[4], LVL1_MAP[5], LVL1_MAP[6],
};
static const char *const SHIP_MAP_2[LVL2_ROWS] = {
  LVL2_MAP[0], LVL2_MAP[1], LVL2_MAP[2], LVL2_MAP[3],
  LVL2_MAP[4], LVL2_MAP[5], LVL2_MAP[6],
};

// Everything the engine needs to play a level, and nothing it needs to know
// about WHICH level: the simulation and the renderer below read only through
// this struct, which is what makes level 2 a data row rather than a second
// engine. `pms` is null before level 2 -- an actor kind a level does not field
// simply has a count of zero.
struct ShipLevel {
  const char        *name;
  const char        *sub;            // the start screen's one-line pitch
  const char *const *map;
  int16_t            cols;
  uint8_t            startCol, startRow;
  const GameSpawn   *bugs;   uint8_t bugCount;
  const GameSpawn   *prios;  uint8_t prioCount;
  const GameSpawn   *pms;    uint8_t pmCount;
  const GameSpawn   *prs;    uint8_t prCount;
};

static const ShipLevel SHIP_LEVEL[SHIP_BUILT] = {
  { LVL1_NAME, "dense, flat, forgiving", SHIP_MAP_1, LVL1_COLS,
    LVL1_START_COL, LVL1_START_ROW,
    LVL1_BUGS,  (uint8_t)LVL1_BUG_COUNT,
    LVL1_PRIOS, (uint8_t)LVL1_PRIO_COUNT,
    nullptr,    0,
    LVL1_PRS,   (uint8_t)LVL1_PR_COUNT },
  { LVL2_NAME, "rising streaks - a PM walks the floor", SHIP_MAP_2, LVL2_COLS,
    LVL2_START_COL, LVL2_START_ROW,
    LVL2_BUGS,  (uint8_t)LVL2_BUG_COUNT,
    LVL2_PRIOS, (uint8_t)LVL2_PRIO_COUNT,
    LVL2_PMS,   (uint8_t)LVL2_PM_COUNT,
    LVL2_PRS,   (uint8_t)LVL2_PR_COUNT },
};

// The engine's geometry constants assume every level shares the playfield band
// and the tile size. A level that does not is a compile error, not a mystery at
// runtime -- and the web generator enforces the same thing on its side.
static_assert(LVL1_ROWS == LVL2_ROWS, "every level must share ROWS");
static_assert(LVL1_TILE == LVL2_TILE, "every level must share TILE");

// Actor arrays are sized to the worst level once, at compile time, so switching
// level never allocates and a run cannot fail on a heap the panel already owns.
#define SHIP_WORST(a, b) ((a) > (b) ? (a) : (b))
#define SHIP_MAX_BUGS   SHIP_WORST(LVL1_BUG_COUNT,  LVL2_BUG_COUNT)
#define SHIP_MAX_PRIOS  SHIP_WORST(LVL1_PRIO_COUNT, LVL2_PRIO_COUNT)
#define SHIP_MAX_PMS    LVL2_PM_COUNT
#define SHIP_MAX_PRS    SHIP_WORST(LVL1_PR_COUNT,   LVL2_PR_COUNT)

class ShipProgress {
 public:
  void begin() {
    Preferences p;
    if (p.begin(GAME_NS, true)) {
      _done    = p.getUShort("done", 0);
      _hardest = p.getUChar("tier", 0xFF);
      _prs     = p.getUChar("prs", 0);
      _dx10    = p.getUChar("dx10", 0);
      p.end();
    }
  }

  bool    cleared(uint8_t lvl) const { return _done & (1u << lvl); }
  bool    any()     const { return _done != 0; }
  uint8_t hardest() const { return _hardest; }      // 0xFF = never cleared
  uint8_t bestPrs() const { return _prs; }
  float   bestDays() const { return _dx10 / 10.0f; }
  uint8_t count() const {
    uint8_t n = 0;
    for (uint8_t i = 0; i < SHIP_LEVELS; i++) if (_done & (1u << i)) n++;
    return n;
  }

  // Record a cleared level. Returns true if this run BEAT the stored record,
  // which the finish screen says out loud -- silently overwriting a record is
  // the same bug as silently discarding one.
  //
  // A harder tier always wins outright and RESETS the numbers rather than
  // keeping the old ones: 12 PRs on SPRINT and 12 on HOTFIX FRIDAY are not the
  // same achievement, so carrying the count across tiers would flatter it. An
  // easier tier still marks the level cleared but never downgrades the record.
  bool record(uint8_t lvl, uint8_t tier, uint8_t prs, float days) {
    uint16_t done = _done | (1u << lvl);
    uint8_t  dx10 = (uint8_t)(days * 10.0f + 0.5f);
    bool     best = false;
    if (_hardest == 0xFF || tier > _hardest) {
      best = true;
    } else if (tier == _hardest) {
      best = (prs > _prs) || (prs == _prs && dx10 > _dx10);
    }
    if (!best && done == _done) return false;      // nothing new to write
    if (best) { _hardest = tier; _prs = prs; _dx10 = dx10; }
    _done = done;
    Preferences p;
    if (p.begin(GAME_NS, false)) {
      p.putUShort("done", _done);
      p.putUChar("tier", _hardest);
      p.putUChar("prs",  _prs);
      p.putUChar("dx10", _dx10);
      p.end();
    }
    return best;
  }

 private:
  uint16_t _done    = 0;
  uint8_t  _hardest = 0xFF;
  uint8_t  _prs     = 0;
  uint8_t  _dx10    = 0;
};

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
    float       pm;        // ...and when a product manager catches you: the
                           // priority-change cost plus 2.5, which on the top
                           // two tiers is more than the whole budget. That is
                           // the reading -- some meetings you avoid.
  };
  static const uint8_t TIERS = 5;

  void begin() { _tier = 0; _tutorial = true; _row = 0; setLevel(0); _state = START; }

  // Called from the menu. Always lands on the start screen rather than resuming:
  // a half-finished sprint you cannot see the state of is worse than a new one.
  void open() { begin(); _lastMs = 0; _acc = 0; prog.begin(); }

  Result tick();
  void   draw(Arduino_GFX *g);

  // True when the game is showing a screen rather than running a level -- the
  // start screen, a codex card, or the end of a run. The sketch uses this to
  // decide when it may spend 600 ms blocking on a reconnect: never mid-level.
  bool idle() const { return _state != PLAY; }

  // Set by the sketch to the function that puts `O|SFX|<code>` on the link. A
  // function pointer rather than a call to the sketch directly, because this
  // header is included BEFORE the emit path exists -- and because a game that
  // hard-references the daemon protocol stops being portable to the bench.
  // Left null, the game is simply silent.
  void (*sfx)(char) = nullptr;

  ShipProgress prog;

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
  // T and ROWS are shared by every level (static_assert above); the WIDTH is
  // not, so it is read from the selected level rather than compiled in.
  static const int16_t T      = LVL1_TILE;         // 16
  static const int16_t ROWS   = LVL1_ROWS;         // 7
  static const int16_t HUD_H  = 24;
  static const int16_t PLAY_Y = HUD_H + 12;        // 36; 36 + 7*16 = 148 of 172

  // ---- physics --------------------------------------------------------------
  static const uint8_t COYOTE = 6, BUFFER = 6;     // frames

  struct Bug  { float c; uint8_t r, from, to; int8_t dir; bool dead; };
  struct Prio { float c, y, top, bot; int8_t dir; bool dead; };
  struct Pm   { float c; uint8_t r, from, to; int8_t dir; bool dead; };
  struct Pr   { uint8_t c, r; bool got; };

  // ---- state ----------------------------------------------------------------
  St       _state    = START;
  uint8_t  _tier     = 0;
  bool     _tutorial = true;
  uint8_t  _row      = 0;                    // start-screen selection
  uint8_t  _lvl      = 0;                    // selected milestone
  const ShipLevel *_lv = &SHIP_LEVEL[0];     // ...and its data, never null

  float    _x = 0, _y = 0, _vx = 0, _vy = 0, _cam = 0, _days = 0;
  bool     _onGround = false;
  int8_t   _face     = 1;
  uint8_t  _coyote = 0, _buffer = 0, _inv = 0, _squash = 0;
  uint16_t _t = 0, _flash = 0, _shipT = 0;
  uint8_t  _merged = 0;
  bool     _seenBug = false, _seenPrio = false, _seenPm = false;
  uint8_t  _card    = 0;                     // 1 = bug, 2 = prio, 3 = PM
  bool     _newBest = false;
  char     _msg[26] = {0};
  uint16_t _msgCol  = GH_TEXT;

  Bug  _bugs[SHIP_MAX_BUGS];
  Prio _prios[SHIP_MAX_PRIOS];
  Pm   _pms[SHIP_MAX_PMS];
  Pr   _prs[SHIP_MAX_PRS];

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
  void beep(char c) { if (sfx) sfx(c); }
  void gain(float v, const char *label, uint16_t col);

  // A milestone is playable once the one before it has shipped, on any tier: a
  // milestone is a month of one project, and you do not start month two because
  // month one ran out of days.
  bool unlocked(uint8_t i) const { return i == 0 || prog.cleared((uint8_t)(i - 1)); }
  void setLevel(uint8_t i) { _lvl = i; _lv = &SHIP_LEVEL[i]; }
  void cycleLevel();

  // What a perfect straight run costs on THIS level, in days. Level 1 is
  // SHIP_CROSS by construction, so this returns its `walk` exactly; a level 20%
  // longer costs 20% more, because the drain per step never changes.
  float walkCost(const Tier &d) const;

  // A knockback is a shove, not a teleport: walk back a pixel at a time and let
  // terrain stop it. Level 2's pyramid made this load-bearing -- a raw x-=46 at
  // its col 112 passes THROUGH the wall into a sealed hollow no jump can leave
  // -- and it turns out to matter on level 1 too, where the raw shove buried the
  // player in the staircase blocks behind the prios at cols 31 and 93.
  void shove(int px);

  bool solid(int c, int r) const {
    return c >= 0 && c < _lv->cols && r >= 0 && r < ROWS && _lv->map[r][c] != '.';
  }
  bool hits(float px, float py) const;
  void drawStart(Arduino_GFX *g);
  void drawPlay(Arduino_GFX *g);
  void drawCard(Arduino_GFX *g);
  void centre(Arduino_GFX *g, uint8_t size, int16_t y, const char *t,
              uint16_t col) {
    g->setTextSize(size);
    g->setTextColor(col);
    g->setCursor(SCREEN_W / 2 - (int16_t)strlen(t) * GLYPH_W * size / 2, y);
    g->print(t);
  }
  void blit(Arduino_GFX *g, const char *const *b, uint8_t h, uint8_t w,
            int16_t x, int16_t y, uint16_t col, bool flip);
  void dayStr(char *out, float v) const;
};

// -----------------------------------------------------------------------------
// The tiers
// -----------------------------------------------------------------------------
static const ShipIt::Tier SHIP_TIERS[ShipIt::TIERS] = {
  {"SPRINT",        "the plan, as written",              7, 5, 0.58f, 1.00f, 1.0f, 2.0f, 4.5f},
  {"CRUNCH",        "someone promised a demo",           6, 6, 0.72f, 1.00f, 1.0f, 2.0f, 4.5f},
  {"CODE FREEZE",   "the branch is cut, the date is not",5, 7, 0.88f, 1.00f, 1.5f, 2.5f, 5.0f},
  {"DEATH MARCH",   "the slack was spent last week",     4, 8, 1.05f, 0.75f, 1.5f, 3.0f, 5.5f},
  {"HOTFIX FRIDAY", "already broken in production",      3, 9, 1.25f, 0.50f, 2.0f, 3.0f, 5.5f},
};

// A straight run is 105 tiles at 1.45 px/step. Everything about the clock is
// derived from this one measurement.
#define SHIP_CROSS 1159.0f

// -----------------------------------------------------------------------------
// Lifecycle
// -----------------------------------------------------------------------------
inline void ShipIt::reset() {
  const Tier &d = SHIP_TIERS[_tier];
  _x = _lv->startCol * T; _y = (_lv->startRow - 1) * T; _vx = _vy = 0; _cam = 0;
  _days = d.days;
  _onGround = false; _face = 1;
  _coyote = _buffer = _inv = _squash = 0;
  _t = _flash = _shipT = 0; _merged = 0;
  _seenBug = _seenPrio = _seenPm = !_tutorial;
  _card = 0; _msg[0] = 0;
  for (uint8_t i = 0; i < _lv->bugCount; i++) {
    _bugs[i].c = _lv->bugs[i].col * T; _bugs[i].r = _lv->bugs[i].row;
    _bugs[i].from = _lv->bugs[i].from; _bugs[i].to = _lv->bugs[i].to;
    _bugs[i].dir = 1; _bugs[i].dead = false;
  }
  for (uint8_t i = 0; i < _lv->prioCount; i++) {
    _prios[i].c   = _lv->prios[i].col * T;
    _prios[i].y   = _lv->prios[i].row * T;
    _prios[i].top = _lv->prios[i].from * T;
    _prios[i].bot = _lv->prios[i].to * T;
    _prios[i].dir = 1; _prios[i].dead = false;
  }
  for (uint8_t i = 0; i < _lv->pmCount; i++) {
    _pms[i].c = _lv->pms[i].col * T; _pms[i].r = _lv->pms[i].row;
    _pms[i].from = _lv->pms[i].from; _pms[i].to = _lv->pms[i].to;
    _pms[i].dir = 1; _pms[i].dead = false;
  }
  for (uint8_t i = 0; i < _lv->prCount; i++) {
    _prs[i].c = _lv->prs[i].col; _prs[i].r = _lv->prs[i].row; _prs[i].got = false;
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
  if (k == 0) _row = (uint8_t)((_row + 3) % 4);
  else if (k == 2) _row = (uint8_t)((_row + 1) % 4);
  else {
    if      (_row == 0) cycleLevel();
    else if (_row == 1) _tier = (uint8_t)((_tier + 1) % TIERS);
    else if (_row == 2) _tutorial = !_tutorial;
    else                reset();
  }
}

// Cycle the MILESTONE row through the levels that are actually open. When
// nothing else is, REFUSE audibly rather than silently doing nothing: a control
// that appears dead is worse than one that says no.
inline void ShipIt::cycleLevel() {
  uint8_t n = (uint8_t)((_lvl + 1) % SHIP_BUILT);
  while (!unlocked(n)) n = (uint8_t)((n + 1) % SHIP_BUILT);
  if (n == _lvl) { beep('H'); return; }
  setLevel(n);
  beep('J');
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

// Steps for a straight run on this level, priced at the tier's drain. The drain
// per step (walk / SHIP_CROSS) is the same on every level, so length IS
// difficulty and the start screen can quote it honestly.
inline float ShipIt::walkCost(const Tier &d) const {
  float steps = (_lv->cols - 2 - _lv->startCol) * (float)T / SHIP_WALK;
  return steps * d.walk / SHIP_CROSS;
}

inline void ShipIt::shove(int px) {
  while (px-- > 0 && _x > 0 && !hits(_x - 1, _y)) _x -= 1;
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
    _days = 0; _state = SLIP; toast("MILESTONE SLIPPED", C_ERROR); beep('X'); return;
  }

  _vx = (_right ? SHIP_WALK : 0) - (_left ? SHIP_WALK : 0);
  if (_vx != 0) _face = (_vx > 0) ? 1 : -1;

  // Coyote time and jump buffering are not polish. With a fixed arc and no air
  // control the launch window for these gaps is about 13 px -- eight frames --
  // and a sweep of scripted timings cleared the tutorial gap once in five
  // without them, four in five with.
  if (_onGround) _coyote = COYOTE; else if (_coyote) _coyote--;
  if (_jump && !_jumpWas) _buffer = BUFFER; else if (_buffer) _buffer--;
  if (_buffer && _coyote) { _vy = SHIP_JUMP; _onGround = false; _buffer = _coyote = 0;
                            beep('J'); }
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
  if (_x > (_lv->cols - 1) * T) _x = (_lv->cols - 1) * T;

  if (_y > ROWS * T + 40) {                  // fell out of the world
    _days -= 1;
    toast("-1d  fell", C_WAIT); beep('F');
    int c = (int)(_x / T);
    while (c > 0 && !solid(c, 5) && !solid(c, 6)) c--;
    int r = 6; while (r > 0 && !solid(c, r)) r--;
    _x = c * T; _y = (r - 1) * T; _vy = 0; _inv = 60;
  }

  for (uint8_t i = 0; i < _lv->bugCount; i++) {
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
        gain(0.5f, "FIXED", GH_L2); beep('S');
      } else if (!_inv) {
        _days -= d.bug;
        char m[26], ds[8]; dayStr(ds, d.bug); sprintf(m, "-%sd  bug", ds);
        toast(m, GH_BUG); beep('H');
        _x -= _face * 15; _vy = -2.5f; _inv = 75;
      }
    }
  }
  if (_inv) _inv--;

  for (uint8_t i = 0; i < _lv->prioCount; i++) {
    Prio &p = _prios[i];
    if (p.dead) continue;
    p.y += p.dir * (d.spd * 1.3f);
    if (p.y <= p.top || p.y >= p.bot) p.dir = (int8_t)-p.dir;
    if (fabsf(p.c - _x) < 13 && fabsf(p.y - _y) < 13 && !_inv) {
      _days -= d.prio;
      char m[26], ds[8]; dayStr(ds, d.prio); sprintf(m, "RE-SCOPED -%sd", ds);
      toast(m, GH_PRIO); beep('R');
      shove(46); _vy = -2.2f; _inv = 80;
    }
  }

  // The product manager. It patrols its floor at 0.7x, and when it SEES you --
  // same floor, within six tiles -- it comes at you at 1.35x, capped at 1.2
  // px/step. The cap is the whole design: 1.2 is below the 1.45 walk, so it is
  // always escapable and never ignorable. A meeting you cannot outrun is not a
  // mechanic, it is a wall.
  for (uint8_t i = 0; i < _lv->pmCount; i++) {
    Pm &m = _pms[i];
    if (m.dead) continue;
    float  my   = m.r * T + 4;
    bool   sees = fabsf(my - _y) < 22 && fabsf(m.c - _x) < 96;
    int8_t dir  = sees ? (int8_t)(_x >= m.c ? 1 : -1) : m.dir;
    float  sp   = sees ? fminf(1.2f, d.spd * 1.35f) : d.spd * 0.7f;
    float  nc   = m.c + dir * sp;
    int    col  = (int)floorf((nc + 6) / T);
    if (col <= m.from || col >= m.to || !solid(col, m.r + 1)) {
      if (!sees) m.dir = (int8_t)-m.dir;   // a patrol turns; a chase waits at
    } else {                               //   the edge of its floor
      m.c = nc; m.dir = dir;
    }
    if (fabsf(m.c - _x) < 13 && fabsf(my - _y) < 13 && !_inv) {
      _days -= d.pm;
      char msg[26], ds[8]; dayStr(ds, d.pm); sprintf(msg, "QUICK SYNC -%sd", ds);
      toast(msg, GH_PM); beep('R');
      shove(60); _vy = -2.4f; _inv = 90;
    }
  }

  for (uint8_t i = 0; i < _lv->prCount; i++) {
    Pr &p = _prs[i];
    if (p.got) continue;
    if (fabsf(p.c * T - _x) < 14 && fabsf(p.r * T - _y) < 14) {
      p.got = true; _merged++;
      gain(d.pr, "PR MERGED", GH_PR); beep('M');
    }
  }

  if (_flash) _flash--;
  if (_squash) _squash--;

  // The enemy codex: the first time each kind comes on screen, freeze and say
  // what it does. Being taught by dying is not a tutorial, it is a toll.
  float right = _cam + SCREEN_W;
  if (!_seenBug) {
    for (uint8_t i = 0; i < _lv->bugCount; i++)
      if (!_bugs[i].dead && _bugs[i].c > _cam + 40 && _bugs[i].c < right - 24) {
        _seenBug = true; _card = 1; _state = CARD; break;
      }
  }
  if (_state == PLAY && !_seenPrio) {
    for (uint8_t i = 0; i < _lv->prioCount; i++)
      if (!_prios[i].dead && _prios[i].c > _cam + 40 && _prios[i].c < right - 24) {
        _seenPrio = true; _card = 2; _state = CARD; break;
      }
  }
  if (_state == PLAY && !_seenPm) {
    for (uint8_t i = 0; i < _lv->pmCount; i++)
      if (!_pms[i].dead && _pms[i].c > _cam + 40 && _pms[i].c < right - 24) {
        _seenPm = true; _card = 3; _state = CARD; break;
      }
  }

  if ((int)(_x / T) >= _lv->cols - 2) {
    _state = SHIPPED; _shipT = 0; beep('W');
    // Written the moment the level is cleared, not when the finish screen is
    // dismissed: the run is over either way, and a flat battery between the two
    // must not cost you the record. The level's OWN bit, so clearing M2 does not
    // claim M1 and unlocking is driven by what you actually shipped.
    _newBest = prog.record(_lvl, _tier, _merged, _days);
  }

  _cam = _x - SCREEN_W / 2 + 7;
  if (_cam < 0) _cam = 0;
  float camMax = _lv->cols * T - SCREEN_W;
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

  // The title block moved up 6 px and the ramp with it: a FOURTH row had to fit
  // between it and the record line, and 172 px does not stretch.
  g->setTextSize(3); g->setTextColor(GH_L1);
  g->setCursor(10, 6); g->print("SHIP IT");
  g->setTextSize(1); g->setTextColor(C_DIM);
  g->setCursor(12, 32); g->print("walk your graph to the milestone");

  // A little run of commits with Mate standing on it, so the start screen says
  // what the terrain is before the level has to.
  static const uint16_t ramp[5] = {GH_L1, GH_L2, GH_L2, GH_L4, GH_L2};
  for (uint8_t i = 0; i < 5; i++) g->fillRect(232 + i * 17, 16, 15, 15, ramp[i]);
  blit(g, MATE_BITS, MATE_H, MATE_W, 246, 2, GH_MATE, false);

  const char *labels[4] = {"MILESTONE", "DIFFICULTY", "TUTORIAL", "START SPRINT"};
  char sub[52], val[20];        // "M2 . SCAFFOLDING" is 16 chars plus the NUL
  for (uint8_t i = 0; i < 4; i++) {
    // 44 with a 27 pitch: four rows of label(16) + sub(8) that end at 150, with
    // the record line at 154 and the controls at 164. The old 54/32 spacing had
    // room for three and overran the record line by eight pixels on the fourth,
    // silently, because GFX text is TOP-left and stacks downward.
    int16_t y = 44 + i * 27;
    if (i == _row) {
      g->fillRect(0, y - 4, SCREEN_W, 25, RGB565(22, 27, 34));
      g->fillRect(0, y - 4, 3, 25, GH_MATE);
    }
    g->setTextSize(2); g->setTextColor(i == _row ? GH_TEXT : C_DIM);
    g->setCursor(10, y); g->print(labels[i]);

    val[0] = 0; sub[0] = 0;
    if (i == 0) {
      // The value is right-aligned at size 2, so it eats leftwards into the
      // label: "MILESTONE" ends at x=118 and "M2 . SCAFFOLDING" starts at 120.
      // SIXTEEN characters is the whole budget for a level name -- a longer one
      // collides, silently, and only on the panel.
      snprintf(val, sizeof(val), "%s", _lv->name);
      // When the next milestone is locked, the row says what opens it instead of
      // pitching the one you are already on. The lock is the information.
      if (_lvl + 1 < SHIP_BUILT && !unlocked((uint8_t)(_lvl + 1)))
        snprintf(sub, sizeof(sub), "ship this one to unlock %s",
                 SHIP_LEVEL[_lvl + 1].name);
      else
        snprintf(sub, sizeof(sub), "%s", _lv->sub);
    }
    if (i == 1) { snprintf(val, sizeof(val), "%s", d.name);
                  snprintf(sub, sizeof(sub), "%s", d.sub); }
    if (i == 2) { snprintf(val, sizeof(val), "%s", _tutorial ? "ON" : "OFF");
                  snprintf(sub, sizeof(sub), "%s", _tutorial
                           ? "freeze and explain each new enemy"
                           : "no cards, straight in"); }
    // The walk cost is the SELECTED level's, not level 1's: on M2 it is 20%
    // more, and quoting the old number would understate the tier by a day.
    if (i == 3) snprintf(sub, sizeof(sub), "%dd budget - the walk costs %d - %d PRs",
                         d.days, (int)(walkCost(d) + 0.5f), _lv->prCount);
    if (val[0]) {
      int16_t vw = (int16_t)strlen(val) * GLYPH_W * 2;
      g->setTextColor(GH_L2);
      g->setCursor(SCREEN_W - 8 - vw, y); g->print(val);
    }
    g->setTextSize(1); g->setTextColor(C_DIM);
    g->setCursor(10, y + 17); g->print(sub);
  }
  // The record, where you choose the run rather than only where you end one.
  // No "/N" denominator any more: the best run may have been set on a level
  // other than the one selected, so a total from THIS level would be wrong.
  char rec[52];
  if (!prog.any()) {
    snprintf(rec, sizeof(rec), "no milestone shipped yet");
  } else {
    char ds[8]; dayStr(ds, prog.bestDays());
    snprintf(rec, sizeof(rec), "best: %s  %d PRs  %sd left",
             SHIP_TIERS[prog.hardest() < TIERS ? prog.hardest() : 0].name,
             prog.bestPrs(), ds);
  }
  g->setTextSize(1); g->setTextColor(prog.any() ? GH_PR : C_DIMMER);
  g->setCursor(10, 154); g->print(rec);
  g->setTextColor(C_DIMMER);
  g->setCursor(10, 164); g->print("PREV/NEXT move   GO change or start   4th exit");
}

inline void ShipIt::drawPlay(Arduino_GFX *g) {
  const Tier &d = SHIP_TIERS[_tier];
  g->fillScreen(GH_BG);

  // ---- HUD ----
  g->setTextSize(1); g->setTextColor(GH_TEXT);
  g->setCursor(6, 8); g->print(_lv->name);
  blit(g, PR_BITS, PR_H, PR_W, 128, 4, GH_PR, false);
  // 64, not 24. The HUD's "12/15" fits in a handful of bytes, but the finish
  // screen's "milestone 1 of 12 - the next 11 are coming soon" is 47, and
  // snprintf would have truncated it to "milestone 1 of 12 - the" without
  // saying a word -- a silent wrong answer rather than a crash.
  char buf[64];
  sprintf(buf, "%d/%d", _merged, (int)_lv->prCount);
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
  int c1 = c0 + 22;      if (c1 > _lv->cols - 1) c1 = _lv->cols - 1;
  for (int c = c0; c <= c1; c++) {
    int16_t sx = (int16_t)(c * T - camI);
    for (int r = 0; r < ROWS; r++) {
      int16_t sy = (int16_t)(PLAY_Y + r * T);
      char    ch = _lv->map[r][c];
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
  g->fillRect((int16_t)((_lv->cols - 1) * T - camI + 2), PLAY_Y, 3, ROWS * T,
              _state == SHIPPED ? GH_L1 : GH_PR);

  // ---- actors ----
  for (uint8_t i = 0; i < _lv->prCount; i++)
    if (!_prs[i].got) {
      int16_t bob = (int16_t)(sinf((_t + _prs[i].c * 9) / 17.0f) * 1.6f);
      blit(g, PR_BITS, PR_H, PR_W, (int16_t)(_prs[i].c * T - camI + 3),
           (int16_t)(PLAY_Y + _prs[i].r * T + 3 + bob), GH_PR, false);
    }
  for (uint8_t i = 0; i < _lv->bugCount; i++)
    if (!_bugs[i].dead)
      blit(g, BUG_BITS, BUG_H, BUG_W, (int16_t)(_bugs[i].c - camI),
           (int16_t)(PLAY_Y + _bugs[i].r * T + 6), GH_BUG, _bugs[i].dir < 0);
  for (uint8_t i = 0; i < _lv->prioCount; i++)
    if (!_prios[i].dead)
      blit(g, PRIO_BITS, PRIO_H, PRIO_W, (int16_t)(_prios[i].c - camI),
           (int16_t)(PLAY_Y + _prios[i].y + 2), GH_PRIO, false);
  for (uint8_t i = 0; i < _lv->pmCount; i++)
    if (!_pms[i].dead)
      blit(g, PM_BITS, PM_H, PM_W, (int16_t)(_pms[i].c - camI),
           (int16_t)(PLAY_Y + _pms[i].r * T + 3), GH_PM, _pms[i].dir < 0);

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
    g->fillRect(0, 56, SCREEN_W, 58, RGB565(13, 17, 23));
    g->fillRect(0, 56, SCREEN_W, 1, GH_BUG);
    g->fillRect(0, 113, SCREEN_W, 1, GH_BUG);
    centre(g, 2, 66, "MILESTONE SLIPPED", GH_BUG);
    snprintf(buf, sizeof(buf), "%d/%d merged - ran out of days",
             _merged, (int)_lv->prCount);
    centre(g, 1, 90, buf, C_DIM);
    centre(g, 1, 102, "any button to go back", C_DIMMER);
  } else if (_state == SHIPPED) {
    // The whole panel, not a strip. Finishing a level is the one moment the
    // game has earned the screen, and the terrain keeps flashing green behind
    // the edges while this is up.
    g->fillRect(0, 40, SCREEN_W, 110, RGB565(13, 17, 23));
    g->fillRect(0, 40, SCREEN_W, 1, GH_L2);
    g->fillRect(0, 149, SCREEN_W, 1, GH_L2);

    centre(g, 2, 50, "MILESTONE SHIPPED", GH_L1);
    snprintf(buf, sizeof(buf), "%s PASSED", _lv->name);
    centre(g, 1, 72, buf, GH_TEXT);

    char ds[8]; dayStr(ds, _days);
    snprintf(buf, sizeof(buf), "%d/%d merged  %sd left  %s",
             _merged, (int)_lv->prCount, ds, d.name);
    centre(g, 1, 86, buf, C_DIM);

    // Say which of the two happened. A record silently overwritten and a record
    // silently discarded look identical from here, and both are worth knowing.
    centre(g, 1, 104, _newBest ? "NEW BEST - PROGRESS SAVED" : "PROGRESS SAVED",
           _newBest ? GH_PR : C_DIM);

    // Name the thing that just opened rather than counting what has not been
    // built: "the next 11 are coming soon" is true and useless when the very
    // next one is playable right now.
    if (_lvl + 1 < SHIP_BUILT)
      snprintf(buf, sizeof(buf), "milestone %d of %d - %s is unlocked",
               prog.count(), SHIP_LEVELS, SHIP_LEVEL[_lvl + 1].name);
    else
      snprintf(buf, sizeof(buf), "milestone %d of %d - the next %d are coming soon",
               prog.count(), SHIP_LEVELS, SHIP_LEVELS - prog.count());
    centre(g, 1, 124, buf, _lvl + 1 < SHIP_BUILT ? GH_L2 : C_DIM);
    centre(g, 1, 138, "any button to go back", C_DIMMER);
  } else if (_flash && _msg[0]) {
    g->setTextSize(1); g->setTextColor(_msgCol);
    int16_t w = (int16_t)strlen(_msg) * GLYPH_W;
    g->setCursor(SCREEN_W / 2 - w / 2, 158); g->print(_msg);
  }
}

// The enemy codex. Freezing the game to explain a thing the player has not been
// hurt by yet is the whole point: the alternative is teaching by ambush.
inline void ShipIt::drawCard(Arduino_GFX *g) {
  // 1 = bug, 2 = priority change, 3 = product manager.
  uint16_t col = (_card == 1) ? GH_BUG : (_card == 3) ? GH_PM : GH_PRIO;
  g->fillRect(20, 34, SCREEN_W - 40, 104, RGB565(13, 17, 23));
  g->drawRect(20, 34, SCREEN_W - 40, 104, col);

  if      (_card == 1) blit(g, BUG_BITS,  BUG_H,  BUG_W,  36, 50, GH_BUG,  false);
  else if (_card == 3) blit(g, PM_BITS,   PM_H,   PM_W,   36, 50, GH_PM,   false);
  else                 blit(g, PRIO_BITS, PRIO_H, PRIO_W, 36, 50, GH_PRIO, false);

  g->setTextSize(2); g->setTextColor(col);
  g->setCursor(60, 48);
  g->print(_card == 1 ? "BUG" : _card == 3 ? "PRODUCT MANAGER" : "PRIORITY CHANGE");

  g->setTextSize(1); g->setTextColor(GH_TEXT);
  const Tier &d = SHIP_TIERS[_tier];
  char l1[44], l2[44];
  char ds[8];
  if (_card == 1) {
    dayStr(ds, d.bug);
    sprintf(l1, "reaches you: -%sd and knocked back", ds);
    sprintf(l2, "land on it:  +1/2d, and it is gone");
  } else if (_card == 3) {
    // The number is the point. On the top two tiers it exceeds the whole
    // budget, and a player who is told that can choose to avoid it.
    dayStr(ds, d.pm);
    sprintf(l1, "catches you: -%sd, the worst in the game", ds);
    sprintf(l2, "chases on sight - but you walk faster");
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
