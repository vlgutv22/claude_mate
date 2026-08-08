#pragma once
/*
 * SHIP IT -- level 2, "M2 · SCAFFOLDING"
 * ======================================
 *
 * LEVEL DATA, in the same shape as level_01.h and generated into the web
 * engine's level_02.js by tools/gen_level.py. The device engine (ship_it.h)
 * does not include this yet -- like level 1 before it, the level ships as data
 * first and the web edition plays it; the firmware port follows.
 *
 * THE SIZING IS ARITHMETIC, NOT TASTE. Level 1's crossing is 105 tiles and
 * every tier prices its clock off that measurement (SHIP_CROSS = 1159 steps).
 * This level's crossing is 126 tiles -- exactly 20% longer -- so at the same
 * per-step drain a straight run costs 1.2x the tier's `walk` in days. The
 * hazard density rises about 5% on top of that: level 1 fields 14 enemies and
 * 8 gaps over 105 tiles (0.210 hazards/tile); this level fields 18 enemies
 * (13 bugs, 3 priority changes, 2 product managers) and 10 gaps over 126
 * (0.222/tile, +6% -- the nearest integer enemy count to +5%). Per-hit costs
 * per tier are unchanged, so a chosen difficulty stays the difficulty chosen.
 *
 * THE SHAPE IS THE ONE §4 OF docs/GAME.md PROMISED: rising streaks. Where
 * level 1 taught verbs on flat ground, this one is built out of scaffolds --
 * step-ups, catwalks, perches, one pyramid you must climb rather than walk
 * past. Every rise still obeys the measured jump: ONE row per hop, gaps of at
 * most TWO tiles (three is impossible by 3 px), one step per row of climb.
 * A BFS over standing positions (tools/test_levels.py, run in CI) proves the
 * flag and all 18 pull requests reachable under exactly those moves.
 *
 * NEW HERE: THE PRODUCT MANAGER. A third enemy and a different QUESTION.
 * Bugs are met head-on; priority changes deny a lane; the PM denies a FLOOR.
 * It patrols its stretch at 0.7x the tier's enemy speed, and when it sees you
 * -- same floor, within six tiles -- it walks toward you at 1.35x, CAPPED at
 * 1.2 px/step, deliberately below your 1.45 walk: a meeting you cannot outrun
 * is not a mechanic, it is a wall. It cannot be stomped (it is your manager),
 * and a collision is the most expensive in the game: 2.5 days MORE than a
 * priority change on the same tier (4.5 / 4.5 / 5 / 5.5 / 5.5), a 60 px
 * shove and 90 frames of invulnerability. On DEATH MARCH and HOTFIX FRIDAY
 * that is more than the whole budget -- the card says so, and avoiding the
 * PM entirely is the intended reading at those tiers. Two ways past, both
 * taught by the terrain: take the catwalk above its floor, or bait the chase
 * and walk away from it.
 */

#include <stdint.h>
#include "level_01.h"        // GameSpawn, the shared palette and sprites

#define LVL2_ROWS 7
#define LVL2_COLS 129
#define LVL2_TILE 16          // px; same playfield band as level 1

// ---- the sprint -------------------------------------------------------------
// col:        0         1         2         3         4         5         6         7         8         9        10        11        12
//             0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789
static const char LVL2_MAP[LVL2_ROWS][LVL2_COLS + 1] = {
  /*Mon*/ ".................................................................................................................................",
  /*Tue*/ "................................................................................................................................4",
  /*Wed*/ "............................333333............2222........................333333333.......................444...................4",
  /*Thu*/ "................222........3......3.........2.................333........3.........3.....................4...4..................4",
  /*Fri*/ "...............2..........3................2.................2..........3.................4......4......4.....4.................4",
  /*Sat*/ "2222222222..22222222222..3333333333333..3333333333333..2222222222222..333333333333333..3333333333333..2222222222222..33..333..334",
  /*Sun*/ "3333333333..33333333333..4444444444444..4444444444444..3333333333333..444444444444444..4444444444444..3333333333333..44..444..444",
};
//             ^beat1     ^beat2       ^beat3        ^beat4        ^beat5        ^beat6         ^beat7       ^beat8        ^beat9  ^flag
//
// beat 1  cols   0-9   flat runway, then the ONLY warm-up this level gives:
//                      a two-tile gap at 10-11. M1 taught the jump; M2 assumes
//                      it. Every gap from here on is two tiles.
// beat 2  cols 12-22   the first scaffold: a step at 15, a ledge at 16-18 with
//                      two PRs, and two bugs on the floor -- the M1 curriculum
//                      compressed into one screen.
// beat 3  cols 25-37   PRODUCT MANAGER #1, patrolling the floor at 28-34. The
//                      catwalk at 28-33 (steps at 26, 27) is the bypass, and
//                      it is deliberately generous: the first PM should be a
//                      lesson in reading its sight line, not a toll. Three PRs
//                      on the catwalk pay for taking the lesson.
// beat 4  cols 40-52   the climb: steps at 43, 44, then a rising jump across
//                      col 45 to the high ledge at 46-49. A priority change
//                      sweeps the ledge, a bug patrols beneath it, and the
//                      drop off the right end lands on another bug's patrol.
//                      The rich route and the guarded route are the same route.
// beat 5  cols 55-67   two bugs out of phase on the floor, a platform at 62-64
//                      (step at 61) carrying three PRs above the second bug.
// beat 6  cols 70-84   high road or low road. The catwalk at 74-82 holds four
//                      PRs, a bug pacing it, and a priority change sweeping
//                      its middle; the floor beneath is a corridor with two
//                      out-of-phase bugs and a ceiling that shortens the jump.
//                      Neither road is free; they are priced differently.
// beat 7  cols 87-99   the gauntlet: PRODUCT MANAGER #2 patrols 91-96 between
//                      two perches (90, 97), each with a PR overhead. Mount a
//                      perch, bait the chase, cross behind it -- the mechanic
//                      beat 3 taught, now with the training wheels off.
// beat 8  cols 102-114 the pyramid at 104-110: the one climb you cannot walk
//                      around, with a bug pacing the plateau and two PRs
//                      floating a jump above it. A priority change guards the
//                      valley after, right where the descent lands.
// beat 9  cols 117-127 island rhythm: three two-tile jumps in a row, a bug on
//                      the middle island as the final exam -- stomp it or
//                      thread the jump over it at speed.
// flag    col 128      the milestone.

// ---- actors -----------------------------------------------------------------
// Same conventions as level 1: bugs patrol columns from..to and turn at ends
// and edges; priority changes drift rows from..to and cannot be stomped; PRs
// do not move. Thirteen bugs against level 1's eleven, three priority changes
// against three -- the +5% is spread across the roster, not piled on one type.
static const GameSpawn LVL2_BUGS[] = {
  {  13, 4,  12,  14 }, {  20, 4,  19,  22 },                        // beat 2
  {  41, 4,  40,  42 }, {  47, 4,  45,  49 }, {  51, 4,  50,  52 },  // beat 4
  {  57, 4,  55,  59 }, {  65, 4,  63,  66 },                        // beat 5
  {  79, 1,  75,  81 }, {  76, 4,  74,  77 }, {  80, 4,  78,  82 },  // beat 6
  {  89, 4,  88,  89 },                                              // beat 7
  { 107, 1, 106, 108 },                                              // beat 8
  { 122, 4, 121, 123 },                                              // beat 9
};
#define LVL2_BUG_COUNT (sizeof(LVL2_BUGS) / sizeof(LVL2_BUGS[0]))

static const GameSpawn LVL2_PRIOS[] = {
  {  47, 1, 0, 3 },       // sweeps the beat-4 high ledge and its two PRs
  {  78, 0, 0, 3 },       // the catwalk's midpoint; the low road never sees it
  { 112, 1, 1, 4 },       // the valley after the pyramid, where the descent lands
};
#define LVL2_PRIO_COUNT (sizeof(LVL2_PRIOS) / sizeof(LVL2_PRIOS[0]))

// PRODUCT MANAGERS. Two, introduced the way priority changes were in level 1:
// rare, each in an arena that teaches the counter-play before it punishes.
// Patrol 0.7x tier speed; chase 1.35x capped at 1.2 px/step (always slower
// than your 1.45 walk); sight is the same floor within 96 px; cannot be
// stomped; costs the tier's priority-change penalty PLUS 2.5 days, shoves
// 60 px back, 90 frames of invulnerability.
static const GameSpawn LVL2_PMS[] = {
  { 30, 4, 28, 34 },      // beat 3: the floor under the teaching catwalk
  { 93, 4, 91, 96 },      // beat 7: between the perches
};
#define LVL2_PM_COUNT (sizeof(LVL2_PMS) / sizeof(LVL2_PMS[0]))

// PULL REQUESTS. Eighteen -- level 1's density carried over the longer
// crossing (15 over 105 tiles ~= 18 over 126), NOT raised with the hazard
// count: the economy has to scale with length or the upper tiers stop being
// mathematically clearable. HOTFIX FRIDAY needs 10.8 days of a 3 + 9 possible;
// it was 9 of a 3 + 7.5 on level 1. Every one sits off the safe walking line.
static const GameSpawn LVL2_PRS[] = {
  {  16, 2, 0, 0 }, {  17, 2, 0, 0 },                                      // beat 2 ledge
  {  30, 1, 0, 0 }, {  31, 1, 0, 0 }, {  32, 1, 0, 0 },                    // beat 3 catwalk
  {  47, 1, 0, 0 }, {  48, 1, 0, 0 },                                      // beat 4 ledge
  {  62, 2, 0, 0 }, {  63, 2, 0, 0 }, {  64, 2, 0, 0 },                    // beat 5 platform
  {  75, 1, 0, 0 }, {  76, 1, 0, 0 }, {  80, 1, 0, 0 }, {  81, 1, 0, 0 },  // beat 6 catwalk
  {  90, 3, 0, 0 }, {  97, 3, 0, 0 },                                      // beat 7 perches
  { 107, 0, 0, 0 }, { 108, 0, 0, 0 },                                      // beat 8, over the plateau
};
#define LVL2_PR_COUNT (sizeof(LVL2_PRS) / sizeof(LVL2_PRS[0]))
#define LVL2_START_COL   1
#define LVL2_START_ROW   5
#define LVL2_FLAG_COL  128
#define LVL2_DEADLINE_DAYS 7
#define LVL2_NAME  "M2 \xB7 SCAFFOLDING"

// ---- the product manager's colour -------------------------------------------
// GitHub's link blue. Every actor keeps its platform-native colour -- bug red,
// priority-change yellow, PR purple -- and blue is the one primary the screen
// does not use yet, so the PM reads as a fourth THING at a glance rather than
// a variant of an existing one.
#define GH_PM   RGB565( 88, 166, 255)     // #58A6FF

// ---- the product manager ----------------------------------------------------
// The only humanoid enemy, on purpose: bugs and priority changes are shapes,
// the PM is a person. Head with the same knocked-out eye slots as Mate (they
// are, after all, on the same team), shoulders, a tie picked out in negative
// space, two legs mid-stride.
#define PM_W 12
#define PM_H 13
static const char *const PM_BITS[PM_H] = {
  "000111111000",
  "000111111000",
  "000110011000",   // the same eye slots as Mate; same team, different incentives
  "000111111000",
  "000011110000",
  "011111111110",   // shoulders
  "111101101111",   // the tie, in negative space
  "111101101111",
  "011101101110",
  "000111111000",
  "000110011000",   // legs, mid-stride
  "000110011000",
  "000100001000",
};
