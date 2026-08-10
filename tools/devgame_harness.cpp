// Drive the DEVICE game engine on the host, with the panel and NVS stubbed.
// =========================================================================
//
// WHY THIS EXISTS. CI compiles the firmware and never runs it, so every
// gameplay and layout bug in ship_it.h used to be discoverable only by flashing
// a board and looking at it. Two classes of defect in particular are invisible
// to a compiler and expensive on hardware:
//
//   * LAYOUT. The panel is 320x172 and the text is drawn at absolute pixel
//     coordinates with a fixed-advance font, so a string one word too long
//     silently runs off the glass, and a right-aligned value collides with its
//     own label. Nothing warns. This harness records every draw call and checks
//     the extents -- every screen, both levels, all five tiers.
//
//   * THE TWO ENGINES DRIFTING. site/game/engine.js and ship_it.h are the same
//     game written twice; the shared numbers are the whole reason that is
//     tolerable. The shove cases below are the exact pixels measured in a
//     browser, asserted here against the firmware.
//
// Driven by tools/test_device_game.py, which is what CI runs. Every system
// header comes first, because the `#define private public` below -- how the
// harness reaches the engine's own state -- would otherwise wreck libstdc++.
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cmath>
#include <string>
#include <vector>
#include <map>

// ---- the board, as far as the game is concerned -----------------------------
#define RGB565(r, g, b) \
  ((uint16_t)((((r) >> 3) << 11) | (((g) >> 2) << 5) | ((b) >> 3)))
#define SCREEN_W 320
#define SCREEN_H 172
#define GLYPH_W  6
#define C_DIM    RGB565(107, 114, 128)
#define C_DIMMER RGB565(55, 60, 70)
#define C_ERROR  RGB565(255, 77, 77)
#define C_WAIT   RGB565(255, 176, 32)
#define PIN_BTN_PREV 3
#define PIN_BTN_GO   4
#define PIN_BTN_NEXT 5
#define LOW 0

static bool          g_pin[8] = {false, false, false, false, false, false, false, false};
static unsigned long g_ms     = 0;
unsigned long millis() { return g_ms; }
int  digitalRead(int pin) { return g_pin[pin] ? LOW : 1; }

// A GFX that draws nothing and REMEMBERS everything, so the layout of a panel
// this machine cannot see is still checkable.
struct Txt { std::string s; int x, y, size; };
class Arduino_GFX {
 public:
  std::vector<Txt> texts;
  int _size = 1, _cx = 0, _cy = 0;
  void fillScreen(uint16_t) {}
  void setTextSize(int s) { _size = s; }
  void setTextColor(uint16_t) {}
  void setCursor(int x, int y) { _cx = x; _cy = y; }
  void print(const char *s) {
    texts.push_back({s, _cx, _cy, _size});
    _cx += (int)strlen(s) * GLYPH_W * _size;
  }
  void fillRect(int, int, int, int, uint16_t) {}
  void drawRect(int, int, int, int, uint16_t) {}
  void clear() { texts.clear(); }
};

#define private public          // the harness reaches into the engine's state
#include "ship_it.h"
#undef private

static int fails = 0;
static void ok(bool cond, const char *what, const char *detail = "") {
  if (cond) { printf("  ok    %s %s\n", what, detail); }
  else      { printf("  FAIL  %s %s\n", what, detail); fails++; }
}

int main() {
  printf("== the campaign table ==\n");
  for (uint8_t i = 0; i < SHIP_BUILT; i++) {
    const ShipLevel &L = SHIP_LEVEL[i];
    char t[96];
    bool rowsOk = true;
    for (int r = 0; r < LVL1_ROWS; r++)
      if ((int)strlen(L.map[r]) != L.cols) rowsOk = false;
    snprintf(t, sizeof(t), "%s: %d cols, %d bugs %d prios %d pms %d prs",
             L.name, L.cols, L.bugCount, L.prioCount, L.pmCount, L.prCount);
    ok(rowsOk, "every map row is `cols` chars", t);
    ok(L.bugCount <= SHIP_MAX_BUGS && L.prioCount <= SHIP_MAX_PRIOS &&
       L.pmCount <= SHIP_MAX_PMS && L.prCount <= SHIP_MAX_PRS,
       "actor counts fit the compile-time arrays", L.name);
    bool inRange = true;
    for (uint8_t k = 0; k < L.bugCount; k++)
      if (L.bugs[k].col >= L.cols) inRange = false;
    for (uint8_t k = 0; k < L.pmCount; k++)
      if (L.pms[k].col >= L.cols || L.pms[k].to >= L.cols) inRange = false;
    for (uint8_t k = 0; k < L.prCount; k++)
      if (L.prs[k].col >= L.cols) inRange = false;
    ok(inRange, "every spawn is inside the grid", L.name);
  }

  ShipIt      game;
  Arduino_GFX gfx;
  game.open();

  printf("\n== the lock ==\n");
  Preferences::store.clear();
  game.prog.begin();
  game.begin();
  ok(game._lvl == 0, "opens on milestone 1");
  ok(!game.unlocked(1), "M2 is locked with no progress");
  game.cycleLevel();
  ok(game._lvl == 0, "cycling refuses while everything else is locked");

  game.prog.record(0, 0, 15, 3.0f);         // ship M1 on SPRINT
  ok(game.unlocked(1), "M2 unlocks once M1 has shipped");
  game.cycleLevel();
  ok(game._lvl == 1, "cycling now reaches M2");
  char nm[64]; snprintf(nm, sizeof(nm), "-> %s", game._lv->name);
  ok(strcmp(game._lv->name, LVL2_NAME) == 0, "and _lv follows the selection", nm);
  game.cycleLevel();
  ok(game._lvl == 0, "and it wraps back to M1");

  printf("\n== the walk cost is the SELECTED level's ==\n");
  for (uint8_t t = 0; t < ShipIt::TIERS; t++) {
    game._tier = t;
    game.setLevel(0);
    int c1 = (int)(game.walkCost(SHIP_TIERS[t]) + 0.5f);
    game.setLevel(1);
    int c2 = (int)(game.walkCost(SHIP_TIERS[t]) + 0.5f);
    char d[96];
    snprintf(d, sizeof(d), "%s: M1 %d (tier walk %d), M2 %d",
             SHIP_TIERS[t].name, c1, SHIP_TIERS[t].walk, c2);
    ok(c1 == SHIP_TIERS[t].walk, "M1 quotes the tier's own measured walk", d);
    ok(c2 > c1, "M2 costs more, because it is 20% longer", d);
  }

  printf("\n== the product manager is escapable on every tier ==\n");
  for (uint8_t t = 0; t < ShipIt::TIERS; t++) {
    float chase = fminf(1.2f, SHIP_TIERS[t].spd * 1.35f);
    char  d[96];
    snprintf(d, sizeof(d), "%s: chase %.3f px/step vs walk %.2f, costs %.1fd",
             SHIP_TIERS[t].name, chase, SHIP_WALK, SHIP_TIERS[t].pm);
    ok(chase < SHIP_WALK, "chase speed stays below the walk", d);
    ok(SHIP_TIERS[t].pm == SHIP_TIERS[t].prio + 2.5f,
       "the cost is the prio penalty plus 2.5", d);
  }
  game.setLevel(0);
  ok(game._lv->pmCount == 0, "level 1 fields no PM, so nothing changes there");

  printf("\n== the swept shove agrees with the web engine, pixel for pixel ==\n");
  // These two landings were measured in the BROWSER against site/game/engine.js.
  // Two independent engines, one number each: that is the check.
  struct SCase { uint8_t lvl; float x, y; int px; float want; const char *what; };
  const SCase cases[] = {
    {0, 484.0f,  68.0f, 46,  447.0f, "L1 prio col 31 (was 438, inside terrain)"},
    {1, 1780.0f, 68.0f, 46, 1775.0f, "L2 prio col 112 (was 1734, in the hollow)"},
  };
  for (const SCase &c : cases) {
    game.setLevel(c.lvl);
    game._x = c.x; game._y = c.y;
    game.shove(c.px);
    char d[128];
    snprintf(d, sizeof(d), "%s: %.0f -> %.0f (want %.0f)", c.what, c.x, game._x, c.want);
    ok(game._x == c.want, "swept shove stops at the wall", d);
  }

  printf("\n== nothing is drawn off a 320x172 panel ==\n");
  // Every screen, both levels, all five tiers, locked and unlocked.
  int checked = 0, worstY = 0;
  std::string worst;
  for (uint8_t lvl = 0; lvl < SHIP_BUILT; lvl++) {
    for (uint8_t t = 0; t < ShipIt::TIERS; t++) {
      for (int locked = 0; locked < 2; locked++) {
        Preferences::store.clear();
        game.prog = ShipProgress();
        if (!locked) { game.prog.record(0, t, 15, 3.0f); }
        game.prog.begin();
        game.setLevel(lvl); game._tier = t;
        for (uint8_t row = 0; row < 4; row++) {
          game._row = row; game._state = ShipIt::START;
          gfx.clear(); game.draw(&gfx);
          for (const Txt &x : gfx.texts) {
            int w = (int)x.s.size() * GLYPH_W * x.size, h = 8 * x.size;
            bool fits = x.x >= 0 && x.x + w <= SCREEN_W &&
                        x.y >= 0 && x.y + h <= SCREEN_H;
            checked++;
            if (!fits) { printf("  FAIL  start screen overflows: \"%s\" at (%d,%d) "
                                "size %d -> right %d, bottom %d\n",
                                x.s.c_str(), x.x, x.y, x.size, x.x + w, x.y + h);
                         fails++; }
            if (x.y + h > worstY) { worstY = x.y + h; worst = x.s; }
          }
        }
        // ...and the in-play screens, including the finish panel and each card.
        game.reset();
        for (int scr = 0; scr < 5; scr++) {
          game._merged = 7; game._days = 2.5f;
          if (scr == 0) game._state = ShipIt::PLAY;
          if (scr == 1) { game._state = ShipIt::SLIP; }
          if (scr == 2) { game._state = ShipIt::SHIPPED; game._newBest = true; }
          if (scr >= 3) { game._state = ShipIt::CARD; game._card = (uint8_t)(scr - 2); }
          gfx.clear(); game.draw(&gfx);
          for (const Txt &x : gfx.texts) {
            int w = (int)x.s.size() * GLYPH_W * x.size, h = 8 * x.size;
            bool fits = x.x >= 0 && x.x + w <= SCREEN_W &&
                        x.y >= 0 && x.y + h <= SCREEN_H;
            checked++;
            if (!fits) { printf("  FAIL  play/card screen overflows: \"%s\" at (%d,%d) "
                                "size %d -> right %d, bottom %d\n",
                                x.s.c_str(), x.x, x.y, x.size, x.x + w, x.y + h);
                         fails++; }
            if (x.y + h > worstY) { worstY = x.y + h; worst = x.s; }
          }
        }
      }
    }
  }
  char d[160];
  snprintf(d, sizeof(d), "%d strings across every screen; lowest pixel %d of %d (\"%s\")",
           checked, worstY, SCREEN_H, worst.c_str());
  ok(true, "checked", d);

  // In-bounds is not enough: the label and the right-aligned value share a row,
  // and "MILESTONE" next to "M2 . SCAFFOLDING" at size 2 is the tightest pair on
  // the screen. A collision there would read as corruption on the panel.
  printf("\n== nothing collides with anything else on its own row ==\n");
  int tightest = 9999; std::string tl, tr;
  for (uint8_t lvl = 0; lvl < SHIP_BUILT; lvl++) {
    for (uint8_t t = 0; t < ShipIt::TIERS; t++) {
      Preferences::store.clear();
      game.prog = ShipProgress(); game.prog.record(0, t, 15, 3.0f); game.prog.begin();
      game.setLevel(lvl); game._tier = t; game._row = 0;
      game._state = ShipIt::START;
      gfx.clear(); game.draw(&gfx);
      for (size_t a = 0; a < gfx.texts.size(); a++)
        for (size_t b = a + 1; b < gfx.texts.size(); b++) {
          const Txt &A = gfx.texts[a], &B = gfx.texts[b];
          if (A.y != B.y) continue;                       // different rows
          const Txt &L = (A.x <= B.x) ? A : B, &R = (A.x <= B.x) ? B : A;
          int gap = R.x - (L.x + (int)L.s.size() * GLYPH_W * L.size);
          if (gap < tightest) { tightest = gap; tl = L.s; tr = R.s; }
        }
    }
  }
  snprintf(d, sizeof(d), "tightest pair is %d px: \"%s\" | \"%s\"",
           tightest, tl.c_str(), tr.c_str());
  ok(tightest >= 0, "no two strings on a row overlap", d);

  // The PM card carries the biggest number, so check the widest case explicitly.
  game.setLevel(1); game._tier = 4; game._state = ShipIt::CARD; game._card = 3;
  gfx.clear(); game.draw(&gfx);
  int widest = 0; std::string ws;
  for (const Txt &x : gfx.texts) {
    int right = x.x + (int)x.s.size() * GLYPH_W * x.size;
    if (right > widest) { widest = right; ws = x.s; }
  }
  snprintf(d, sizeof(d), "widest line reaches x=%d of %d (\"%s\")",
           widest, SCREEN_W, ws.c_str());
  ok(widest <= SCREEN_W, "the PM card fits on HOTFIX FRIDAY", d);

  printf("\n%s (%d failure%s)\n", fails ? "FAILED" : "ALL PASSED", fails,
         fails == 1 ? "" : "s");
  return fails ? 1 : 0;
}
