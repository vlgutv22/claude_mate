#pragma once
/*
 * battery.h -- the gauge's ARITHMETIC, separated from its plumbing so a host
 * can run it.
 *
 * WHY THIS IS ITS OWN FILE. Everything here is a pure function of millivolts
 * and a clock: volts to percent, percent to one of three segments, and the
 * hysteresis-plus-hold that decides when the displayed segment is allowed to
 * move. None of it needs an ADC, a panel or a radio -- and all of it was
 * previously buried in the middle of pollBattery(), where the only way to check
 * any of it was to flash a board and watch a corner of the screen for
 * forty-five seconds.
 *
 * That mattered, because this is the code that got the important case wrong.
 * The "is a cell even wired" threshold sat at 3000 mV, which on this board can
 * only ever be a REAL cell that is nearly empty -- so the gauge deleted itself
 * at exactly the moment it was worth reading. A test would have said so; there
 * was nowhere to put one. tools/test_battery.py is that test now, and it runs
 * in CI beside the game engine's, which was split out for the same reason.
 *
 * The stateful half -- sampling the ADC, the EMA across polls, inferring the
 * charger from the trend -- stays in the sketch. It needs hardware to mean
 * anything, and splitting it would buy a mock rather than a test.
 *
 * NOTHING HERE READS A GLOBAL. Every input is an argument, which is what lets
 * the harness drive a whole discharge curve through it in a millisecond.
 */

#include <stdint.h>

#if !defined(BATT_L3_MV)
#error "battery.h must be included after board_s3.h (it needs the thresholds)"
#endif

// ---- volts -> percent --------------------------------------------------------
// A LiPo's voltage/charge curve is famously non-linear -- treating it as a
// straight line reads "50%" for most of the discharge and then falls off a
// cliff. This piecewise table is still an approximation, but an honest one.
//
// The table's own floor (3300 mV = 0%) is the gauge's definition of empty, and
// BATT_LOW_MV is deliberately the same number: the warning fires when the
// firmware's own arithmetic says there is nothing left, rather than at a second
// threshold that could disagree with the percentage next to it.
static inline int battPercentFromMv(uint32_t mv) {
  static const uint16_t curve[][2] = {
      {4200, 100}, {4100, 92}, {4000, 84}, {3900, 74}, {3800, 62},
      {3700, 48},  {3600, 30}, {3500, 16}, {3400, 8},  {3300, 0},
  };
  if (mv >= curve[0][0]) return 100;
  const size_t n = sizeof(curve) / sizeof(curve[0]);
  if (mv <= curve[n - 1][0]) return 0;
  for (size_t i = 1; i < n; i++) {
    if (mv >= curve[i][0]) {
      uint16_t hiMv = curve[i - 1][0], loMv = curve[i][0];
      uint16_t hiPc = curve[i - 1][1], loPc = curve[i][1];
      return loPc + (int)((long)(mv - loMv) * (hiPc - loPc) / (hiMv - loMv));
    }
  }
  return 0;
}

// Is anything plausibly wired to the sense divider? See the long note by
// BATT_ABSENT_MV in board_s3.h for why this is 2400 and not 3000 -- it is the
// single change that stops a nearly-flat cell reporting itself as absent.
static inline bool battCellPresent(uint32_t mv) { return mv >= BATT_ABSENT_MV; }

// Which of the three segments does THIS voltage ask for, given the one
// currently shown? Asymmetric on purpose: a level must be crossed by
// BATT_HYST_MV to fall, but only touched to rise. Charge going up is real;
// going down through a threshold is where noise and IR sag live.
//
// `cur` is the displayed level (3, 2, 1) or <= 0 for "nothing shown yet".
static inline int8_t battLevelFor(uint32_t mv, int8_t cur) {
  int8_t c = cur > 0 ? cur : 0;
  uint32_t l3 = BATT_L3_MV - (c >= 3 ? BATT_HYST_MV : 0);
  uint32_t l2 = BATT_L2_MV - (c >= 2 ? BATT_HYST_MV : 0);
  return (mv >= l3) ? 3 : (mv >= l2) ? 2 : 1;
}

// The hold, kept next to the hysteresis because neither is sufficient alone:
// hysteresis lets a long sag through, and a hold alone would still flicker once
// it expired. A new candidate restarts the clock rather than accumulating, so a
// voltage dithering across a threshold never moves the display at all.
//
// Returns true when `level` changed and the caller owes a redraw.
struct BattLevelHold {
  int8_t        level = -1;      // what is on the glass (-1 = no cell)
  int8_t        cand  = -1;      // what the voltage is asking for...
  unsigned long since = 0;       // ...and since when
  // AN EXPLICIT FLAG RATHER THAN A 0-SENTINEL, and that is the whole point of
  // this member. The old code wrote `since = now ? now : 1UL` so that 0 could
  // keep meaning "never stamped" -- the same trick this firmware has had to
  // unpick three times already, and it was wrong here too: at millis() == 0 it
  // stamps one millisecond into the FUTURE, the unsigned `now - since`
  // underflows to about 4.29e9, that clears BATT_LEVEL_HOLD_MS instantly, and
  // the hold the whole struct exists to enforce simply does not happen.
  //
  // A one-millisecond window at boot is not the reason to fix it. The reason is
  // that a sentinel which cannot represent a legal value keeps producing this
  // bug, and a bool cannot. Caught by tools/test_battery.py, which is the first
  // thing able to drive this function at now == 0.
  bool          armed = false;

  bool update(int8_t want, unsigned long now) {
    if (!armed || want != cand) {
      cand  = want;
      since = now;               // exactly now: never rounded, never nudged
      armed = true;
    }
    bool settled = (now - since) >= BATT_LEVEL_HOLD_MS;
    // The first reading after boot shows immediately -- making someone stare at
    // a blank corner for 45 s to prove a point would be its own kind of wrong.
    if (level < 0 || settled) {
      if (want != level) { level = want; return true; }
    }
    return false;
  }
};

// "This cell is nearly empty." Not while charging: a flat cell that is ON the
// charger is good news, and flashing LOW at it would say the opposite.
static inline bool battIsLow(uint32_t mv, int8_t level, bool charging) {
  return level >= 0 && !charging && mv && mv <= BATT_LOW_MV;
}
