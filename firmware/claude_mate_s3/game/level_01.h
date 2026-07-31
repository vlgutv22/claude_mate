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

// ---- palette ----------------------------------------------------------------
// GitHub's own contribution ramp, so a screenshot of the game and a screenshot
// of the graph are the same picture. RGB565() comes from board_s3.h.
#define GH_L1   RGB565(155, 233, 168)     // #9BE9A8
#define GH_L2   RGB565( 64, 196,  99)     // #40C463
#define GH_L3   RGB565( 48, 161,  78)     // #30A14E
#define GH_L4   RGB565( 33, 110,  57)     // #216E39
#define GH_BG   RGB565( 13,  17,  23)     // #0D1117, GitHub's own dark canvas
#define GH_GRID RGB565( 22,  27,  34)     // empty-cell outline, #161B22
#define GH_MATE RGB565(217, 119,  87)     // #D97757, the accent used repo-wide
#define GH_BUG  RGB565(196,  48,  43)     // a red that is NOT the alert red, so
                                          // an enemy and an error never read as
                                          // the same thing
#define GH_PRIO RGB565(210, 153,  34)     // #D29922
#define GH_PR   RGB565(163, 113, 247)     // #A371F7, GitHub's own PR purple
#define GH_TEXT RGB565(230, 237, 243)     // #E6EDF3

// ---- sprites ----------------------------------------------------------------
// One character per pixel, one string per row. That is the same literal the
// browser prototype carries, which is the point: the prototype is where the
// feel of this thing is worked out, and a sprite that exists twice in two
// formats is a sprite that will drift. Drawn as runs of fillRect -- at this
// size a run-length walk beats a per-pixel blit, and the terrain is rectangles
// already, so one primitive serves both.

// THREE POSES, NEVER ONE SPRITE SCALED. The squash and stretch was first done
// with a vertical scale over a bitmap drawn as 1x1 rects, which turns every
// pixel into a FRACTIONAL rect for the canvas to antialias -- a shimmer that
// read as a glitch, and one that could never have shipped here anyway: this
// panel has no antialiasing at all. Discrete poses are honest on both.
#define MATE_W 14
#define MATE_H 12
static const char *const MATE_BITS[MATE_H] = {
  "01111111111100",
  "01111111111100",
  "01101111110110",   // eye slots knocked out
  "01101111110110",
  "01111111111100",
  "11111111111110",   // arms
  "11111111111110",
  "01111111111100",
  "01111111111100",
  "01111111111100",
  "00110000011000",   // legs
  "00110000011000",
};

// landing: flatter and wider, legs tucked
#define MATE_SQUASH_H 8
static const char *const MATE_SQUASH_BITS[MATE_SQUASH_H] = {
  "00111111111100",
  "01111111111110",
  "11101111101110",
  "11101111101110",
  "11111111111110",
  "11111111111110",
  "01111111111100",
  "00110000011000",
};

// rising: taller and narrower, legs extended
#define MATE_STRETCH_H 14
static const char *const MATE_STRETCH_BITS[MATE_STRETCH_H] = {
  "00011111111000",
  "00111111111100",
  "00110111101100",
  "00110111101100",
  "00111111111100",
  "01111111111110",
  "01111111111110",
  "00111111111100",
  "00111111111100",
  "00111111111100",
  "00111111111100",
  "00011000011000",
  "00011000011000",
  "00001000001000",
};

// A bug: antennae, a shell with a seam down it, six legs. Round-ish on purpose,
// because it is the only non-square thing on a screen made of squares, and that
// is what makes it readable at 12 px.
#define BUG_W 12
#define BUG_H 10
static const char *const BUG_BITS[BUG_H] = {
  "001000000100",
  "000100001000",
  "000011110000",
  "001111111100",
  "011110011110",
  "111110011111",
  "011110011110",
  "111110011111",
  "001111111100",
  "010010010010",
};

// A PRIORITY CHANGE: a two-headed arrow, pointing both ways at once. It drifts
// vertically and cannot be stomped -- you do not fix a re-prioritisation by
// jumping on it.
#define PRIO_W 12
#define PRIO_H 12
static const char *const PRIO_BITS[PRIO_H] = {
  "000001100000",
  "000011110000",
  "000110011000",
  "001110011100",
  "011110011110",
  "111110011111",
  "111110011111",
  "011111111110",
  "001110011100",
  "000111111000",
  "000011110000",
  "000001100000",
};

// A pull request. An 11x11 REDRAWING of the shape GitHub's git-pull-request
// Octicon uses -- a branch line with a node at each end and an arrow merging
// back in -- not a copy of it. Octicons is MIT-licensed, so a direct copy would
// be fine with the notice kept; credited here because the design is
// recognisable, not because it has to be. Outline squares said "collectible";
// this says what the collectible IS.
#define PR_W 11
#define PR_H 11
static const char *const PR_BITS[PR_H] = {
  "01110000000",
  "11011000000",
  "10001000000",
  "11011001110",
  "01110011011",
  "00100010001",
  "00100011011",
  "00100001110",
  "01110000000",
  "11011000000",
  "01110000000",
};
