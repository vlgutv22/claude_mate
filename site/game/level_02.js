// GENERATED FILE -- DO NOT EDIT BY HAND.
//
// Source:     firmware/claude_mate_s3/game/level_02.h
// Assets:     palette + sprites merged from every level header
// Generator:  tools/gen_level.py
// Regenerate: python3 tools/gen_level.py
// Verify:     python3 tools/gen_level.py --check   (CI runs this)
//
// The firmware headers are the single source of truth for level
// geometry, actors, palette and sprites. Edit a header and rerun the
// generator; anything hand-edited here is lost on the next run, and CI
// fails in the meantime so the two engines cannot drift apart.
//
// content-sha256: 81a13ef4a5a82ad1307411489ad5922f32aed996a0fabdc78c8d7f5223f58b97
// (over everything below this comment block -- a changed body with an
//  unchanged hash means someone edited the output by hand.)

export const LEVEL = {
  name: "M2 \u00b7 SCAFFOLDING",
  rows: 7,
  cols: 129,
  tile: 16,
  start: { col: 1, row: 5 },
  flagCol: 128,

  // The terrain IS the collision map: '.' is a day nothing shipped and
  // you fall through it, '1'..'4' are shipping days you can stand on.
  map: [
    ".................................................................................................................................",
    "................................................................................................................................4",
    "............................333333............2222........................333333333.......................444...................4",
    "................222........3......3.........2.................333........3.........3.....................4...4..................4",
    "...............2..........3................2.................2..........3.................4......4......4.....4.................4",
    "2222222222..22222222222..3333333333333..3333333333333..2222222222222..333333333333333..3333333333333..2222222222222..33..333..334",
    "3333333333..33333333333..4444444444444..4444444444444..3333333333333..444444444444444..4444444444444..3333333333333..44..444..444",
  ],

  // Actors: (col, row) spawn in the grid above. Bugs patrol columns
  // from..to; priority changes drift rows from..to; product managers
  // patrol columns from..to and chase on sight; PRs do not move.
  bugs: [ // 13 bugs
    { col:  13, row: 4, from:  12, to:  14 },
    { col:  20, row: 4, from:  19, to:  22 },
    { col:  41, row: 4, from:  40, to:  42 },
    { col:  47, row: 4, from:  45, to:  49 },
    { col:  51, row: 4, from:  50, to:  52 },
    { col:  57, row: 4, from:  55, to:  59 },
    { col:  65, row: 4, from:  63, to:  66 },
    { col:  79, row: 1, from:  75, to:  81 },
    { col:  76, row: 4, from:  74, to:  77 },
    { col:  80, row: 4, from:  78, to:  82 },
    { col:  89, row: 4, from:  88, to:  89 },
    { col: 107, row: 1, from: 106, to: 108 },
    { col: 122, row: 4, from: 121, to: 123 },
  ],

  prios: [ // 3 priority changes
    { col:  47, row: 1, from:   0, to:   3 },
    { col:  78, row: 0, from:   0, to:   3 },
    { col: 112, row: 1, from:   1, to:   4 },
  ],

  pms: [ // 2 product managers
    { col:  30, row: 4, from:  28, to:  34 },
    { col:  93, row: 4, from:  91, to:  96 },
  ],

  prs: [ // 18 pull requests
    { col:  16, row: 2, from:   0, to:   0 },
    { col:  17, row: 2, from:   0, to:   0 },
    { col:  30, row: 1, from:   0, to:   0 },
    { col:  31, row: 1, from:   0, to:   0 },
    { col:  32, row: 1, from:   0, to:   0 },
    { col:  47, row: 1, from:   0, to:   0 },
    { col:  48, row: 1, from:   0, to:   0 },
    { col:  62, row: 2, from:   0, to:   0 },
    { col:  63, row: 2, from:   0, to:   0 },
    { col:  64, row: 2, from:   0, to:   0 },
    { col:  75, row: 1, from:   0, to:   0 },
    { col:  76, row: 1, from:   0, to:   0 },
    { col:  80, row: 1, from:   0, to:   0 },
    { col:  81, row: 1, from:   0, to:   0 },
    { col:  90, row: 3, from:   0, to:   0 },
    { col:  97, row: 3, from:   0, to:   0 },
    { col: 107, row: 0, from:   0, to:   0 },
    { col: 108, row: 0, from:   0, to:   0 },
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
    pm:   "#58a6ff", // GH_PM = RGB565( 88, 166, 255) -> 0x5D3F
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
    pm: {
      w: 12,
      h: 13,
      bits: [
        "000111111000",
        "000111111000",
        "000110011000",
        "000111111000",
        "000011110000",
        "011111111110",
        "111101101111",
        "111101101111",
        "011101101110",
        "000111111000",
        "000110011000",
        "000110011000",
        "000100001000",
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
