// GENERATED FILE -- DO NOT EDIT BY HAND.
//
// Source:     firmware/claude_mate_s3/game/level_01.h
// Generator:  tools/gen_level.py
// Regenerate: python3 tools/gen_level.py
// Verify:     python3 tools/gen_level.py --check   (CI runs this)
//
// The firmware header is the single source of truth for this level's
// geometry, actors, palette and sprites. Edit the header and rerun the
// generator; anything hand-edited here is lost on the next run, and CI
// fails in the meantime so the two engines cannot drift apart.
//
// content-sha256: ab85c61dfe1e41efb39a3ae474b0ad777df7d0e47f272f660fc6ee83304354e1
// (over everything below this comment block -- a changed body with an
//  unchanged hash means someone edited the output by hand.)

export const LEVEL = {
  name: "M1 \u00b7 KICKOFF",
  rows: 7,
  cols: 108,
  tile: 16,
  start: { col: 1, row: 5 },
  flagCol: 107,

  // The terrain IS the collision map: '.' is a day nothing shipped and
  // you fall through it, '1'..'4' are shipping days you can stand on.
  map: [
    "............................................................................................................",
    "...........................................................................................................4",
    ".............................222................................333........................444.............4",
    ".............222............2............2222......222.........3........22............222.4.........222....4",
    "............2...........22.2...........333333.....2...........3........222.333.......2...4.........2.......4",
    "2222222222.222222222..333333333333..333333333..333333333333..22222222..3333333333..333333333333..33333333334",
    "3333333333.333333333..444444444444..444444444..444444444444..33333333..4444444444..444444444444..44444444444",
  ],

  // Actors: (col, row) spawn in the grid above. Bugs patrol columns
  // from..to; priority changes drift rows from..to; PRs do not move.
  bugs: [ // 11 bugs
    { col:  13, row: 2, from:  13, to:  15 },
    { col:  18, row: 4, from:  16, to:  19 },
    { col:  37, row: 4, from:  36, to:  38 },
    { col:  43, row: 2, from:  41, to:  44 },
    { col:  51, row: 2, from:  51, to:  53 },
    { col:  56, row: 4, from:  54, to:  58 },
    { col:  64, row: 1, from:  64, to:  66 },
    { col:  79, row: 4, from:  78, to:  80 },
    { col:  86, row: 2, from:  86, to:  88 },
    { col: 100, row: 2, from: 100, to: 102 },
    { col: 105, row: 4, from: 103, to: 106 },
  ],

  prios: [ // 3 priority changes
    { col:  31, row: 1, from:   1, to:   4 },
    { col:  67, row: 0, from:   0, to:   3 },
    { col:  93, row: 1, from:   1, to:   4 },
  ],

  prs: [ // 15 pull requests
    { col:  14, row: 2, from:   0, to:   0 },
    { col:  15, row: 2, from:   0, to:   0 },
    { col:  30, row: 1, from:   0, to:   0 },
    { col:  31, row: 1, from:   0, to:   0 },
    { col:  52, row: 2, from:   0, to:   0 },
    { col:  53, row: 2, from:   0, to:   0 },
    { col:  65, row: 1, from:   0, to:   0 },
    { col:  66, row: 1, from:   0, to:   0 },
    { col:  72, row: 2, from:   0, to:   0 },
    { col:  76, row: 3, from:   0, to:   0 },
    { col:  87, row: 2, from:   0, to:   0 },
    { col:  92, row: 1, from:   0, to:   0 },
    { col:  93, row: 1, from:   0, to:   0 },
    { col: 101, row: 2, from:   0, to:   0 },
    { col: 102, row: 2, from:   0, to:   0 },
  ],

  // Palette. The CSS hex below is the EXACT 8-bit argument the header
  // passes to RGB565(), not a round-trip back out of the packed word:
  // 5/6/5 throws away 3/2/3 bits per channel, so re-expanding would
  // shift every colour by a few counts for no reason. The rgb565 value
  // in each comment is what the firmware actually pushes to the LCD.
  palette: {
    l1:   "#9be9a8", // GH_L1 = RGB565(155, 233, 168) -> 0x9F55
    l2:   "#40c463", // GH_L2 = RGB565( 64, 196,  99) -> 0x462C
    l3:   "#30a14e", // GH_L3 = RGB565( 48, 161,  78) -> 0x3509
    l4:   "#216e39", // GH_L4 = RGB565( 33, 110,  57) -> 0x2367
    bg:   "#0d1117", // GH_BG = RGB565( 13,  17,  23) -> 0x0882
    grid: "#161b22", // GH_GRID = RGB565( 22,  27,  34) -> 0x10C4
    mate: "#d97757", // GH_MATE = RGB565(217, 119,  87) -> 0xDBAA
    bug:  "#c4302b", // GH_BUG = RGB565(196,  48,  43) -> 0xC185
    prio: "#d29922", // GH_PRIO = RGB565(210, 153,  34) -> 0xD4C4
    pr:   "#a371f7", // GH_PR = RGB565(163, 113, 247) -> 0xA39E
    text: "#e6edf3", // GH_TEXT = RGB565(230, 237, 243) -> 0xE77E
  },

  // Sprites keep the firmware's row-string form: one character per
  // pixel, one string per row, '1' painted and '0' transparent.
  sprites: {
    mate: {
      w: 14,
      h: 12,
      bits: [
        "01111111111100",
        "01111111111100",
        "01101111110110",
        "01101111110110",
        "01111111111100",
        "11111111111110",
        "11111111111110",
        "01111111111100",
        "01111111111100",
        "01111111111100",
        "00110000011000",
        "00110000011000",
      ],
    },
    mateSquash: {
      w: 14,
      h: 8,
      bits: [
        "00111111111100",
        "01111111111110",
        "11101111101110",
        "11101111101110",
        "11111111111110",
        "11111111111110",
        "01111111111100",
        "00110000011000",
      ],
    },
    mateStretch: {
      w: 14,
      h: 14,
      bits: [
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
      ],
    },
    bug: {
      w: 12,
      h: 10,
      bits: [
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
      ],
    },
    prio: {
      w: 12,
      h: 12,
      bits: [
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
      ],
    },
    pr: {
      w: 11,
      h: 11,
      bits: [
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
      ],
    },
  },
};

export default LEVEL;
