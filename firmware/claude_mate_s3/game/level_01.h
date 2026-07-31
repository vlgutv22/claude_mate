#pragma once
/*
 * SHIP IT -- level 1, "M1 · KICKOFF"
 * ==================================
 *
 * DESIGN PROPOSAL. Nothing includes this yet; see docs/GAME.md for the concept.
 * It is written as real data in the shape an engine would consume, so the level
 * can be judged as a level rather than as a paragraph about one.
 *
 * THE TERRAIN IS A CONTRIBUTION GRAPH, and that is not decoration -- the same
 * grid is the collision map. A digit is a day you shipped something and you can
 * stand on it; a '.' is a day you did not and you fall through it. Everything
 * else in the design follows from that one equivalence.
 *
 *   '.'  nothing shipped   -> empty, no collision
 *   '1'  a quiet day       -> solid, #9BE9A8
 *   '2'  a normal day      -> solid, #40C463
 *   '3'  a good day        -> solid, #30A14E
 *   '4'  a heavy day       -> solid, #216E39
 *
 * 7 rows, because a week has 7 days and GitHub draws them top to bottom. Row 0
 * is Monday. 60 columns is one sprint; at 16 px a tile and 320 px of screen,
 * 20 columns are visible, so the level is three screens wide.
 *
 * LEVEL 1 IS A TUTORIAL AND TEACHES EXACTLY ONE VERB PER BEAT. The beats are
 * marked below. Nothing here is decorative: every gap is a jump the player has
 * already been taught, and the first of each kind is deliberately more forgiving
 * than it will ever be again.
 *
 * THE JUMP BUDGET IS TWO TILES, and it is a measurement rather than a choice.
 * With walk 1.45 px/frame, jump -4.75 and gravity 0.30, a full jump is airborne
 * 31 frames and covers 45 px -- 2.81 tiles. So:
 *
 *      1-tile gap (16 px)   clears with 29 px to spare
 *      2-tile gap (32 px)   clears with 13 px to spare
 *      3-tile gap (48 px)   IMPOSSIBLE -- 3 px short, which is worse than
 *                           obviously impossible because it looks reachable
 *
 * THE VERTICAL BUDGET IS ONE ROW PER JUMP, and this one was missed the first
 * time round -- the horizontal reach was computed carefully and the vertical
 * never was. Apex is 35.3 px. Standing on row R and landing on row R-k needs a
 * rise of k*16:
 *
 *      1 row (16 px)   +19.3 px of margin   comfortable
 *      2 rows (32 px)   +3.3 px of margin   PIXEL-PERFECT, i.e. unreachable
 *      3 rows (48 px)  -12.7 px             impossible
 *
 * Every raised ledge therefore needs ONE STEP PER ROW it rises. Without them a
 * BFS over standing positions found ALL SEVENTEEN pull requests unreachable --
 * the entire optional layer of the level was decoration. Reported from play as
 * "why can't I jump so high".
 *
 * Level 1's gaps are 1, 2, 2, 2. Later levels do NOT get a bigger jump to buy
 * wider gaps -- they get a platform in the middle. Raising the jump to clear
 * three tiles costs 2.7 tiles of height on a 7-row playfield, which makes the
 * character floaty and the ceilings meaningless.
 */

#include <stdint.h>

#define LVL1_ROWS 7
#define LVL1_COLS 108
#define LVL1_TILE 16          // px; 7 rows x 16 = 112 px of playfield

// ---- the sprint -------------------------------------------------------------
// col:        0         1         2         3         4         5
//             012345678901234567890123456789012345678901234567890123456789
static const char LVL1_MAP[LVL1_ROWS][LVL1_COLS + 1] = {
  /*Mon*/ "............................................................................................................",
  /*Tue*/ "...........................................................................................................4",
  /*Wed*/ ".............................222................................333........................444.............4",
  /*Thu*/ ".............222............2............2222......222.........3........22............222.4.........222....4",
  /*Fri*/ "............2...........22.2...........333333.....2...........3........222.333.......2...4.........2.......4",
  /*Sat*/ "2222222222.222222222..333333333333..333333333..333333333333..22222222..3333333333..333333333333..33333333334",
  /*Sun*/ "3333333333.333333333..444444444444..444444444..444444444444..33333333..4444444444..444444444444..44444444444",
};
//             ^beat1    ^2 ^beat3   ^4  ^beat5     ^6 ^beat7   ^beat8   ^flag
//
// beat 1  cols  0-9   flat ground. Walk right. Nothing can hurt you.
// beat 2  col   10    a ONE-tile gap. The first jump, and the widest margin for
//                     error in the game -- but you must be MOVING: a standing
//                     jump has no horizontal speed and drops straight in.
//                     Deliberate, and the cheapest possible way to teach that
//                     jumping and running are one motion here.
// beat 3  cols 11-19  flat, one bug patrolling. The first squash.
// beat 4  cols 20-21  a TWO-tile gap. Needs a walking start, not a standing one.
// beat 5  cols 22-33  the first climb: a step at cols 24-25 and a high ledge at
//                     cols 28-30 carrying optional cells. Skippable on purpose
//                     -- a tutorial should let you ignore the optional thing.
// beat 6  cols 34-35  a second two-tile gap, this one taken at speed off the
//                     descent rather than from a standstill.
// beat 7  cols 36-44  rising steps -- a streak that climbs -- with a bug at the
//                     top, so it is met mid-climb rather than on flat ground.
// beat 8  cols 47-58  two bugs sharing one long patrol, out of phase: the first
//                     time the player must deal with a second while still
//                     recovering from the first.
// flag    col   59    the milestone. A column of '4' the full height, which is
//                     both the goal post and the only unmissable landmark on
//                     the map.

// ---- actors -----------------------------------------------------------------
// Spawn positions are (col, row) in the grid above. A bug patrols between
// `from` and `to` inclusive, turning at each end AND at any edge it would walk
// off -- an enemy that walks into a hole it was placed above is a bug in the
// game about bugs.
struct GameSpawn { uint8_t col, row, from, to; };

static const GameSpawn LVL1_BUGS[] = {
  {  13, 2,  13, 15 }, {  18, 4,  16, 19 }, {  37, 4,  36, 38 }, {  43, 2,  41, 44 },
  {  51, 2,  51, 53 }, {  56, 4,  54, 58 }, {  64, 1,  64, 66 }, {  79, 4,  78, 80 },
  {  86, 2,  86, 88 }, { 100, 2, 100,102 }, { 105, 4, 103,106 },
};
#define LVL1_BUG_COUNT (sizeof(LVL1_BUGS) / sizeof(LVL1_BUGS[0]))

// PRIORITY CHANGES. Rare -- three, against eleven bugs -- and a different
// PROBLEM rather than a harder one. They drift vertically between `from` and
// `to` (rows, not columns), denying a whole lane instead of a tile, and they
// CANNOT be stomped: you do not fix a re-prioritisation by jumping on it. -2
// days and shoved four tiles back, which is rework, which is the point.
static const GameSpawn LVL1_PRIOS[] = {
  { 31, 1, 1, 4 },        // over the beat-5 climb, guarding the high PRs
  { 67, 0, 0, 3 },        // on the high road, where the richest PRs are
  { 93, 1, 1, 4 },        // the crunch stretch
};
#define LVL1_PRIO_COUNT (sizeof(LVL1_PRIOS) / sizeof(LVL1_PRIOS[0]))

// PULL REQUESTS -- the time economy, and the reason this is a route and not a
// walk. What crossing COSTS is set by the difficulty tier, not by this file:
// a straight run is 1159 steps, and each tier picks what that walk should cost
// in days (5 at SPRINT up to 9 at HOTFIX FRIDAY) with the drain derived from it.
// Above SPRINT the walk costs more than the budget, so some of these are not
// optional. Every one sits OFF the safe walking line.
static const GameSpawn LVL1_PRS[] = {
  { 14, 2, 0, 0 }, { 15, 2, 0, 0 }, { 30, 1, 0, 0 }, { 31, 1, 0, 0 },
  { 52, 2, 0, 0 }, { 53, 2, 0, 0 }, { 65, 1, 0, 0 }, { 66, 1, 0, 0 },
  { 72, 2, 0, 0 }, { 76, 3, 0, 0 }, { 87, 2, 0, 0 }, { 92, 1, 0, 0 },
  { 93, 1, 0, 0 }, {101, 2, 0, 0 }, {102, 2, 0, 0 },
};
#define LVL1_PR_COUNT (sizeof(LVL1_PRS) / sizeof(LVL1_PRS[0]))
#define LVL1_START_COL   1
#define LVL1_START_ROW   5
#define LVL1_FLAG_COL  107
// The starting budget is per-tier (7/6/5/4/3); this is the default the engine
// uses if no tier was chosen. It is deliberately equal to the SPRINT tier.
#define LVL1_DEADLINE_DAYS 7
#define LVL1_NAME  "M1 \xB7 KICKOFF"

// ---- palette (RGB565) -------------------------------------------------------
// GitHub's own contribution ramp, so a screenshot of the game and a screenshot
// of the graph are the same picture.
#define GH_L1  RGB565(155, 233, 168)      // #9BE9A8
#define GH_L2  RGB565( 64, 196,  99)      // #40C463
#define GH_L3  RGB565( 48, 161,  78)      // #30A14E
#define GH_L4  RGB565( 33, 110,  57)      // #216E39
#define GH_BG  RGB565( 13,  17,  23)      // #0D1117, GitHub's own dark canvas
#define GH_GRID RGB565(33, 38, 45)        // empty-cell outline, #21262D

// ---- sprites ----------------------------------------------------------------
// 1 bit per pixel, MSB first, padded to whole bytes per row. Drawn as filled
// rectangles rather than blitted: at this size a run-length walk of the bitmap
// is fewer SPI writes than a per-pixel blit, and the terrain is rectangles
// already so the same primitive serves both.

// Claude Mate, 14x12. The project's mascot: a wide body, two dark eye slots,
// stubby arms that step out at the shoulders, two legs.
#define MATE_W 14
#define MATE_H 12
static const uint16_t MATE_BITS[MATE_H] = {
  0b0111111111110000,   // .############.
  0b0111111111110000,   // .############.
  0b0110111111011000,   // .##.######.##.   <- eye slots knocked out
  0b0110111111011000,   // .##.######.##.
  0b0111111111110000,   // .############.
  0b1111111111111000,   // ##############   <- arms
  0b1111111111111000,   // ##############
  0b0111111111110000,   // .############.
  0b0111111111110000,   // .############.
  0b0111111111110000,   // .############.
  0b0011000001100000,   // ..##......##..   <- legs
  0b0011000001100000,   // ..##......##..
};
#define MATE_COL RGB565(217, 119, 87)     // #D97757, the accent used repo-wide

// A bug, 10x8. Round-ish on purpose: it is the only non-square thing on a
// screen made of squares, which is what makes it readable at this size.
#define BUG_W 10
#define BUG_H 8
static const uint16_t BUG_BITS[BUG_H] = {
  0b0011110000000000,   // ..####....
  0b0111111000000000,   // .######...
  0b1101101100000000,   // ##.##.##..   <- pale eyes knocked out
  0b1111111100000000,   // ########..
  0b1111111100000000,   // ########..
  0b0111111000000000,   // .######...
  0b1010010100000000,   // #.#..#.#..   <- legs
  0b1000000100000000,   // #......#..
};
#define BUG_COL RGB565(196, 48, 43)       // a red that is not the ERROR red, so
                                          // an alert and an enemy never read as
                                          // the same thing
