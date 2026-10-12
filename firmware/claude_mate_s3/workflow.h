#pragma once
/*
 * workflow.h -- the WORKFLOW BAND's parser and arithmetic, shared by both
 * devices.
 *
 * THIS FILE EXISTS TWICE, BYTE FOR BYTE: firmware/claude_mate/workflow.h and
 * firmware/claude_mate_s3/workflow.h. arduino-cli copies a sketch into its
 * build directory before compiling it, so a header outside the sketch folder
 * cannot be included at all. tools/test_workflow_fw.py fails the moment the
 * two copies differ -- edit one, copy it over the other.
 *
 * The band is the one place the daemon sends NUMBERS rather than a finished
 * row. Everything else on the glass is pre-rendered text, but a progress bar
 * cannot be: the wire is ASCII, the bar glyphs Claude Code draws never reach a
 * device, and the two panels are 128 and 320 px wide. So the daemon sends the
 * counts, and each device draws them in its own geometry. Every WORD is still
 * the daemon's -- `info` arrives pre-rendered -- except the four status words
 * below, which are fixed.
 *
 *   J|<st>|<done>|<failed>|<total>|<info>|<name>      7 fields, frozen
 *   J|-                                                nothing to show
 *
 *     st      R running, E running with an agent failed, P paused on a usage
 *             limit, D completed, F ended without completing. Any other
 *             capital letter is accepted and drawn neutrally, so a status
 *             added later degrades instead of freezing the band.
 *     done    \
 *     failed   } agents; ONLY the bar's geometry. 0|0|0 = nothing known:
 *     total   /  no bar at all.
 *     info    up to 14 chars, pre-rendered ("3/5 2m39s 543k", "til 3:30pm").
 *     name    up to 40 chars, LAST -- and still never contains a '|': the
 *             daemon turns every '|' into '/', so one here can only be a lost
 *             newline that glued the next line on.
 *
 * A J| line is only ever STAGED. It becomes what the screen draws when the
 * F| frame after it arrives -- and the band shows only while that F| carries
 * flags bit2. See WfPair below for why.
 *
 * NO GLOBALS, no float, no String, no heap, no atoi. atoi is 16-bit on the
 * Nano and 32-bit on a host, so a harness built on a Mac would pass while the
 * Nano wrapped a count past 32767; the parser below saturates the same way on
 * every target.
 */

#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define WF_INFO_MAX     14    // the daemon's WF_INFO_CHARS
#define WF_NAME_WIRE    40    // the longest name the wire carries (the
                              // daemon's WF_NAME_CHARS)
#ifndef WF_NAME_MAX
#define WF_NAME_MAX     WF_NAME_WIRE   // how much of it THIS device keeps. The
                                       // Nano never draws the name, so it
                                       // defines 1 and saves 78 bytes of RAM
                                       // (39 in each half of a WfPair).
#endif
#define WF_COUNT_DIGITS 5     // 65535 is the most a count can say

static_assert(WF_NAME_MAX >= 1 && WF_NAME_MAX <= WF_NAME_WIRE,
              "keep between one character and what the wire can carry");

struct WfStatus {
  char     st;                       // 0 = nothing to show
  uint16_t done, failed, total;      // clamped: done + failed <= total
  char     info[WF_INFO_MAX + 1];
  char     name[WF_NAME_MAX + 1];
};

// ---- the line ---------------------------------------------------------------
// Accepts exactly
//     ^J\|-$   or   ^J\|[A-Z]\|\d{1,5}\|\d{1,5}\|\d{1,5}\|[^|]{0,14}\|[^|]{0,40}$
// and nothing else, which caps a line at 77 bytes -- inside the Nano's 95.
// Returns false and leaves `out` UNTOUCHED for anything else: a rejected line
// keeps the last good one, and the daemon's next J|+F| pair (about a second
// later) heals it. Parsing a half-line instead would draw its garbage.
static inline bool wfParse(const char *line, WfStatus &out) {
  if (line[0] != 'J' || line[1] != '|') return false;
  const char *p = line + 2;
  if (p[0] == '-' && p[1] == 0) {      // J|-
    out.st = 0;
    return true;
  }
  const char st = p[0];
  if (st < 'A' || st > 'Z' || p[1] != '|') return false;
  p += 2;

  uint16_t n[3];                       // done, failed, total -- in wire order
  for (uint8_t i = 0; i < 3; i++) {
    uint32_t v = 0;
    uint8_t digits = 0;
    while (*p >= '0' && *p <= '9') {
      if (++digits > WF_COUNT_DIGITS) return false;
      v = v * 10 + (uint32_t)(*p++ - '0');
    }
    if (!digits || *p != '|') return false;
    p++;
    n[i] = v > 65535UL ? 65535 : (uint16_t)v;   // 99999 says "a lot", not 34463
  }

  const char *info = p;
  while (*p && *p != '|') p++;
  const size_t infoLen = (size_t)(p - info);
  if (*p != '|' || infoLen > WF_INFO_MAX) return false;

  const char *name = ++p;
  while (*p && *p != '|') p++;
  const size_t nameLen = (size_t)(p - name);
  if (*p || nameLen > WF_NAME_WIRE) return false;   // a '|' here = a spliced line

  // Never more than full. The daemon already sends consistent counts; this is
  // what keeps a bar inside its outline if one day it does not.
  uint16_t done = n[0], failed = n[1];
  const uint16_t total = n[2];
  if (total) {
    if (done > total) done = total;
    if (failed > total - done) failed = (uint16_t)(total - done);
  }

  out.st = st;
  out.done = done;
  out.failed = failed;
  out.total = total;
  memcpy(out.info, info, infoLen);
  out.info[infoLen] = 0;
  const size_t keep = nameLen < WF_NAME_MAX ? nameLen : WF_NAME_MAX;
  memcpy(out.name, name, keep);
  out.name[keep] = 0;
  return true;
}

// ---- J| stages, F| commits --------------------------------------------------
// The daemon writes a J| immediately before every F| that sets bit2, and the
// J| is DRAWN ONLY BY THAT F|. Rendering on the J| alone would draw the new
// session's workflow under the old session's name for one frame on every
// PREV/NEXT -- both firmwares redraw after 8 ms of quiet, and over BLE the two
// lines can land in different connection events. Same rule as the mirror's
// M|END: rows arrive, one line commits them.
//
// So a frame on the glass is always one F| plus the J| that preceded it, and a
// lost J| costs one frame: the next pair, a second later, is whole again.
struct WfPair {
  WfStatus staged;   // the last J| that parsed; nothing draws this
  WfStatus live;     // what the screen draws, as of the last F|
};

// The J| handler. Deliberately not a reason to redraw.
static inline bool wfStage(WfPair &wf, const char *line) {
  return wfParse(line, wf.staged);
}

// The F| handler, before it asks for a redraw. One struct copy.
static inline void wfCommit(WfPair &wf) {
  wf.live = wf.staged;
}

// ---- the bar ----------------------------------------------------------------
// Claude Code's own arithmetic -- floor(done * w / total), nothing when total
// is 0 -- so a bar here fills to the same fraction as the one in the terminal.
// 32-bit: 999 * 306 does not fit in 16.
static inline uint16_t wfFillPx(uint16_t done, uint16_t total, uint16_t w) {
  if (!total) return 0;
  if (done >= total) return w;
  return (uint16_t)((uint32_t)done * w / total);
}

// One segment per agent when every one of them gets at least minSeg px with
// `gap` px between them; 0 = draw one continuous bar instead. A five-agent run
// reads as five boxes; an eighty-agent one would read as noise.
static inline uint16_t wfSegments(uint16_t total, uint16_t width, uint8_t gap,
                                  uint8_t minSeg) {
  if (!total) return 0;
  const uint32_t need = (uint32_t)total * minSeg + (uint32_t)(total - 1) * gap;
  return need <= width ? total : 0;
}

// ---- the status word --------------------------------------------------------
// "" for a letter this firmware does not know: a word guessed for a status
// nobody defined yet would be a lie in a fixed place on the glass.
//
// ON AVR THE WORDS STAY IN FLASH. Every literal the Nano reads through a plain
// pointer is copied into its 2 KB of RAM at boot, and these are 27 bytes of it
// -- more than a third of what the whole band may cost there. So on AVR the
// pointer is a PROGMEM address: print it through (const __FlashStringHelper *)
// and never strlen() it. Everywhere else it is an ordinary string.
//
// An if-chain and not a switch, for the same 2 KB: avr-gcc turns a switch
// that returns a value per letter into a lookup table D..R, and that table
// lands in RAM too -- 30 bytes, more than the words it was keeping out.
#if defined(__AVR__)
#include <avr/pgmspace.h>
#define WF_WORD_LIT(s) PSTR(s)
#else
#define WF_WORD_LIT(s) (s)
#endif

static inline const char *wfWord(char st) {
  if (st == 'R' || st == 'E') return WF_WORD_LIT("running");
  if (st == 'P')              return WF_WORD_LIT("paused");
  if (st == 'D')              return WF_WORD_LIT("done");
  if (st == 'F')              return WF_WORD_LIT("ended");
  return WF_WORD_LIT("");
}

// ---- the Nano's r2 ----------------------------------------------------------
// The Nano has no free row -- four size-1 rows fill its 32 px -- so while the
// band is up it takes r2 (y16..23) and gives up model/effort/limit:
//
//     [==========              ] 3/5 2m39s 543k     R: the bar
//     [=======          ]  !     2/5 4m10s 880k     E: shorter, then a '!'
//     paused                         til 3:30pm     P/D/F: the word instead
//
// The info is right-aligned and the bar or word takes what is left of it, so
// the longest info (14 chars) still leaves 41 px: room for "paused". Pure, so
// a host can check every info length against the 128 px it has to fit.
#define WF_ROW_W      128     // the Nano's panel
#define WF_ROW_Y      16      // r2's top
#define WF_ROW_H      8
#define WF_ROW_GLYPH  6       // size-1 advance
#define WF_ROW_GAP    3       // between the bar (or word) and the info
#define WF_ROW_BAR_Y  17      // outline y17..22, fill y18..21: centred on the
#define WF_ROW_BAR_H  6       // text, clear of r1's FOLLOW mark and r3's box
#define WF_ROW_BANG_W 7       // what E's bar gives up for its '!' cell

struct WfRowLayout {
  int16_t infoX;              // the info's first column
  int16_t areaW;              // [0, areaW) is the bar's, or the word's
  int16_t barW;               // outline width; 0 = no bar
  int16_t fillW;              // lit px inside it, from x = 1
  int16_t bangX;              // E: the '!' cell's left edge; -1 = none
  bool    word;               // P/D/F: wfWord(st) at x = 0 instead of a bar
};

static inline WfRowLayout wfRowLayout(char st, uint16_t done, uint16_t total,
                                      uint8_t infoLen) {
  WfRowLayout r;
  r.infoX = (int16_t)(WF_ROW_W - (int16_t)infoLen * WF_ROW_GLYPH);
  r.areaW = (int16_t)(r.infoX - WF_ROW_GAP);
  r.barW  = 0;
  r.fillW = 0;
  r.bangX = -1;
  r.word  = false;
  switch (st) {
    case 'R':
    case 'E':
      // E is R that has lost an agent: the same bar, plus a mark a mono panel
      // can show -- the S3 says it with a red bullet instead.
      if (st == 'E') r.bangX = (int16_t)(r.areaW - WF_ROW_GLYPH);
      if (total) {
        r.barW  = st == 'E' ? (int16_t)(r.areaW - WF_ROW_BANG_W) : r.areaW;
        r.fillW = (int16_t)wfFillPx(done, total, (uint16_t)(r.barW - 2));
      }
      break;
    case 'P':
    case 'D':
    case 'F':
      // A word, not a bar style. "done" at 5/5 and "running" at 5/5 would be
      // the same full bar, and a paused run usually has no counts at all.
      r.word = true;
      break;
    default:
      break;                  // a letter this firmware does not know: just info
  }
  return r;
}
