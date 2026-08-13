// Run the DEVICE battery gauge's arithmetic on the host.
// ======================================================
//
// WHY THIS EXISTS. The gauge decides three things from a voltage -- is a cell
// even there, what percentage is it, and which of the three segments to light
// -- and until battery.h was split out, every one of them was checkable only by
// charging a cell, flashing a board, and watching a corner of the screen for
// forty-five seconds per transition. That is why the important case was wrong
// for so long: the "no cell wired" threshold sat at 3000 mV, which on this
// board can only ever be a REAL cell that is nearly empty, so the indicator
// deleted itself at exactly the moment someone needed it. Nothing failed. There
// was simply nowhere for a test to live.
//
// The cases below are the ones that were wrong, the ones next to them, and the
// invariants that must hold across a whole discharge. Driven by
// tools/test_battery.py, which is what CI runs.
//
// Only board_s3.h and battery.h are included, exactly as the sketch includes
// them -- no stubs, no copies. If a threshold moves in the header, it moves
// here.
#include <cstdint>
#include <cstdio>
#include <cstring>

#include "board_s3.h"
#include "battery.h"

static int fails = 0;

static void ok(bool cond, const char *what) {
  if (cond) printf("  ok    %s\n", what);
  else      { printf("  FAIL  %s\n", what); fails++; }
}

static void eqi(long got, long want, const char *what) {
  if (got == want) printf("  ok    %s (%ld)\n", what, got);
  else { printf("  FAIL  %s: got %ld, want %ld\n", what, got, want); fails++; }
}

// ---- the regression this file was written for -------------------------------
// A 14500 at 2.95 V is nearly empty and is unmistakably PRESENT. Before the
// fix, battCellPresent()'s threshold was 3000 mV, so this exact cell reported
// itself as absent: the chip vanished, the About page and `?` both printed
// "no cell", and the charge inference was forced to false so plugging it in
// showed no bolt either.
static void test_flat_cell_is_still_a_cell() {
  printf("a nearly-flat cell is present, not absent\n");
  ok(battCellPresent(2950), "2950 mV (nearly empty 14500) reads as present");
  ok(battCellPresent(3000), "3000 mV -- the OLD threshold -- reads as present");
  ok(battCellPresent(2400), "2400 mV, exactly at the floor, reads as present");
  ok(!battCellPresent(2399), "2399 mV reads as absent");
  ok(!battCellPresent(0),    "0 mV (nothing wired) reads as absent");

  // ...and it must still show something rather than nothing.
  eqi(battPercentFromMv(2950), 0, "2950 mV is 0%");
  eqi(battLevelFor(2950, 1), 1, "2950 mV lights one (red) segment");
  ok(battIsLow(2950, 1, false), "2950 mV on battery raises the LOW warning");

  // The regulator drops out around 2.7-2.9 V, so a reading under the floor
  // cannot be a running board's cell -- it is a wrong divider or no sense node.
  ok(!battCellPresent(1400),
     "1400 mV (BATT_DIVIDER left at 1) reads as absent, not as a flat cell");
}

// ---- the LOW warning ---------------------------------------------------------
static void test_low_warning() {
  printf("the LOW warning\n");
  ok(battIsLow(3300, 1, false),  "3300 mV (the curve's own 0%%) is low");
  ok(!battIsLow(3301, 1, false), "3301 mV is not low");
  ok(!battIsLow(3200, 1, true),
     "a flat cell ON THE CHARGER is not warned about -- that is good news");
  ok(!battIsLow(3200, -1, false), "no cell means no warning");
  ok(!battIsLow(0, 1, false),     "a zero reading is not a warning");
  ok(BATT_LOW_MV > BATT_ABSENT_MV,
     "the warning fires above the absent floor, so it is reachable at all");
}

// ---- volts -> percent --------------------------------------------------------
static void test_percent_curve() {
  printf("the percent curve\n");
  eqi(battPercentFromMv(4300), 100, "above the top of the curve clamps to 100");
  eqi(battPercentFromMv(4200), 100, "4200 mV is 100%");
  eqi(battPercentFromMv(3700), 48,  "3700 mV is 48%");
  eqi(battPercentFromMv(3300), 0,   "3300 mV is 0%");
  eqi(battPercentFromMv(3000), 0,   "below the curve clamps to 0");

  // Interpolation between two table rows, checked by hand: 3750 is halfway
  // between 3700 (48%) and 3800 (62%), so 48 + 50*14/100 = 55.
  eqi(battPercentFromMv(3750), 55, "3750 mV interpolates to 55%");

  // Monotonic across the whole range: a rising voltage must never report less.
  int prev = -1;
  bool mono = true;
  for (uint32_t mv = 3000; mv <= 4300; mv++) {
    int pc = battPercentFromMv(mv);
    if (pc < prev) { mono = false; break; }
    prev = pc;
  }
  ok(mono, "percent never decreases as voltage rises");
}

// ---- the three segments, and the hysteresis ---------------------------------
static void test_levels_and_hysteresis() {
  printf("segments and hysteresis\n");
  eqi(battLevelFor(4200, 0), 3, "a full cell is three segments");
  eqi(battLevelFor(BATT_L3_MV, 0), 3, "exactly at L3 is three");
  eqi(battLevelFor(BATT_L3_MV - 1, 0), 2, "one mV under L3 is two");
  eqi(battLevelFor(BATT_L2_MV - 1, 0), 1, "under L2 is one");

  // Asymmetric: crossing DOWN needs BATT_HYST_MV of margin, crossing UP does
  // not. This is what stops the chip flickering on a cell sitting on a
  // threshold while the backlight steps.
  eqi(battLevelFor(BATT_L3_MV - 10, 3), 3,
      "already at three, a 10 mV dip below L3 stays at three");
  eqi(battLevelFor(BATT_L3_MV - BATT_HYST_MV, 3), 3,
      "exactly BATT_HYST_MV below L3 still stays at three");
  eqi(battLevelFor(BATT_L3_MV - BATT_HYST_MV - 1, 3), 2,
      "one mV past the hysteresis band falls to two");
  eqi(battLevelFor(BATT_L3_MV, 2), 3,
      "coming back UP, touching L3 is enough to rise");

  // A cell can never be asked for level 0 or -1 by this function: "no cell" is
  // the caller's decision, made by battCellPresent().
  bool inRange = true;
  for (uint32_t mv = BATT_ABSENT_MV; mv <= 4300; mv += 7) {
    for (int8_t cur = -1; cur <= 3; cur++) {
      int8_t l = battLevelFor(mv, cur);
      if (l < 1 || l > 3) { inRange = false; break; }
    }
  }
  ok(inRange, "battLevelFor always returns 1, 2 or 3");
}

// ---- the hold ----------------------------------------------------------------
static void test_hold() {
  printf("the level hold\n");
  {
    BattLevelHold h;
    // The first reading shows immediately: nobody should stare at a blank
    // corner for 45 s to prove a point.
    ok(h.update(3, 1000), "the first reading is adopted at once");
    eqi(h.level, 3, "...and it is the level asked for");
  }
  {
    BattLevelHold h;
    h.update(3, 1000);
    ok(!h.update(2, 2000), "a change is NOT adopted immediately");
    eqi(h.level, 3, "...the old level stays on the glass");
    ok(!h.update(2, 2000 + BATT_LEVEL_HOLD_MS - 1), "...nor one ms early");
    ok(h.update(2, 2000 + BATT_LEVEL_HOLD_MS), "...and is adopted on time");
    eqi(h.level, 2, "...at the new level");
  }
  {
    // Dithering across a threshold must move nothing at all: every flip
    // restarts the clock, so the hold is never satisfied.
    BattLevelHold h;
    h.update(3, 0);
    unsigned long t = 1000;
    bool moved = false;
    for (int i = 0; i < 200; i++) {
      t += BATT_POLL_MS;
      if (h.update((i & 1) ? 2 : 3, t)) moved = true;
    }
    ok(!moved, "a level dithering every poll never moves the display");
    eqi(h.level, 3, "...and the display still shows what it started at");
  }
  {
    // A sustained change still gets through after the dither stops.
    BattLevelHold h;
    h.update(3, 0);
    unsigned long t = 1000;
    bool moved = false;
    for (int i = 0; i < 20; i++) {
      t += BATT_POLL_MS;
      if (h.update(2, t)) moved = true;
    }
    ok(moved, "a sustained change is adopted once the hold expires");
    eqi(h.level, 2, "...at the new level");
  }
  {
    // THE SENTINEL TRAP, which this test was what finally caught. The old code
    // stamped `since = now ? now : 1UL` so that 0 could mean "never stamped" --
    // and at millis() == 0 that is one millisecond in the FUTURE, so the
    // unsigned `now - since` underflows to ~4.29e9, clears the 45 s hold on the
    // spot, and the hold does not happen at all. An explicit `armed` flag
    // replaced it; these two lines are what stop it coming back.
    BattLevelHold h;
    h.update(3, 0);
    ok(!h.update(2, 0), "a change at millis()==0 is not adopted instantly");
    ok(h.armed && h.since == 0,
       "...and the clock stamps the real time, flagged rather than fudged");
    ok(h.update(2, BATT_LEVEL_HOLD_MS),
       "...and it is adopted at the right moment, measured from zero");
  }
}

// ---- a whole discharge -------------------------------------------------------
// The end-to-end property the user actually cares about: as a real cell runs
// down, the gauge must go 3 -> 2 -> 1 -> LOW and NEVER disappear on the way.
static void test_discharge_never_vanishes() {
  printf("a full discharge, 4.2 V down to 2.9 V\n");
  BattLevelHold h;
  unsigned long t = 0;
  int8_t seen3 = 0, seen2 = 0, seen1 = 0;
  bool vanished = false, lowSeen = false;
  int8_t prev = -1;
  bool wentUp = false;

  for (uint32_t mv = 4200; mv >= 2900; mv -= 2) {
    t += BATT_POLL_MS;
    bool present = battCellPresent(mv);
    if (!present) { vanished = true; break; }
    h.update(battLevelFor(mv, h.level), t);
    if (h.level == 3) seen3 = 1;
    if (h.level == 2) seen2 = 1;
    if (h.level == 1) seen1 = 1;
    if (prev > 0 && h.level > prev) wentUp = true;
    prev = h.level;
    if (battIsLow(mv, h.level, false)) lowSeen = true;
  }

  ok(!vanished, "the cell never reads as absent anywhere in the range");
  ok(seen3 && seen2 && seen1, "all three segment levels are visited");
  ok(!wentUp, "the level never rises while the cell only falls");
  ok(lowSeen, "the LOW warning is reached before the bottom");
  eqi(h.level, 1, "the discharge ends on one red segment, still displayed");
}

int main() {
  printf("battery gauge (host)\n");
  test_flat_cell_is_still_a_cell();
  test_low_warning();
  test_percent_curve();
  test_levels_and_hysteresis();
  test_hold();
  test_discharge_never_vanishes();
  printf(fails ? "\n%d FAILED\n" : "\nall passed\n", fails);
  return fails ? 1 : 0;
}
