#pragma once
/*
 * wfband.h -- the S3's WORKFLOW BAND: the strip under the fleet strip that
 * shows the shown session's workflow while the daemon says one is live (F|
 * flags bit2, content from the J| before it -- see workflow.h).
 *
 *   y132..139   o deployment-risk-sweep-3802                    running
 *   y146..161   [###][###][###][   ][   ]             3/5 2m39s 543k
 *
 * Line 1, size 1: a bullet in the status colour, the workflow's name, and a
 * status word right-aligned. Line 2, size 2: the bar on the left -- a segment
 * per agent while they fit, else one continuous bar -- and the daemon's info
 * right-aligned in the primary text colour.
 *
 * The colours say what Claude Code's own bullet for that row says -- a warning
 * while it waits out a usage limit, an error once an agent fails, success when
 * it completes -- in the palette this screen already uses for the same states:
 * running C_WORK, an agent failed C_ERROR (on the bullet only: the run itself
 * is still running), paused C_WAIT, done C_DONE, ended C_ERROR.
 *
 * NOTHING HERE BLINKS. On this device a blink means "an alert you have not
 * acknowledged", and nothing about a workflow can be acknowledged: a
 * usage-limit pause can last hours, and blinking that whole time would cry
 * wolf -- and cost a full-canvas flush 2.5 times a second on battery. Paused
 * is a HOLLOW bullet, held steady.
 *
 * Header-only and handed the GFX, the way game/ship_it.h is, so
 * tools/wf_harness.cpp can draw it into a recording stub and check that no
 * status, count or string length reaches outside y130..163.
 */

#include <stdint.h>
#include <string.h>

#if !defined(WF_BAND_Y) || !defined(C_WORK)
#error "wfband.h must be included after board_s3.h (geometry and palette)"
#endif
#if !defined(WF_INFO_MAX)
#error "wfband.h must be included after workflow.h"
#endif

static_assert(WF_BAND_Y >= R3_Y - 2 + R3_H,
              "the band starts below the fleet strip's selection box");
static_assert(WF_BAND_Y + WF_BAND_H <= ACCENT_Y,
              "...and ends above the accent bar");
static_assert(WF_LINE1_Y >= WF_BAND_Y &&
              WF_LINE2_Y + GLYPH_H * WF_INFO_SIZE <= WF_BAND_Y + WF_BAND_H,
              "both lines sit inside the band");

// The bar's and the word's colour.
static inline uint16_t wfColour(char st) {
  switch (st) {
    case 'R':
    case 'E': return C_WORK;
    case 'P': return C_WAIT;
    case 'D': return C_DONE;
    case 'F': return C_ERROR;
    default:  return C_IDLE;      // a status this firmware does not know
  }
}

// The bullet's. Only E differs: Claude Code turns a running row's bullet red
// when an agent fails, while the run itself carries on.
static inline uint16_t wfBulletColour(char st) {
  return st == 'E' ? C_ERROR : wfColour(st);
}

// `linkText`: nullptr while the link is up. Otherwise the link's own status,
// drawn dim in place of the name -- the band covers the line that would have
// said it, and "why is this not connecting" must stay on the glass.
static inline void wfDrawBand(Arduino_GFX *g, const WfStatus &s,
                              const char *linkText) {
  if (!s.st) return;
  const uint16_t col = wfColour(s.st);
  const int16_t right = SCREEN_W - PAD_X;

  // ---- line 1: bullet, name, status word ----
  const int16_t cx = PAD_X + WF_BULLET_R, cy = WF_LINE1_Y + WF_BULLET_R;
  if (s.st == 'P') g->drawCircle(cx, cy, WF_BULLET_R, wfBulletColour(s.st));
  else             g->fillCircle(cx, cy, WF_BULLET_R, wfBulletColour(s.st));

  const char *word = wfWord(s.st);
  const int16_t wordX = right - (int16_t)strlen(word) * GLYPH_W;
  g->setTextSize(1);
  // The name stops one blank cell short of the word. The daemon's 40 chars
  // always fit beside the longest word; a link status may not, and is cut.
  const int16_t room = (wordX - GLYPH_W - WF_NAME_X) / GLYPH_W;
  const char *label = linkText ? linkText : s.name;
  g->setTextColor(linkText ? C_DIM : C_TEXT);
  g->setCursor(WF_NAME_X, WF_LINE1_Y);
  for (int16_t i = 0; i < room && label[i]; i++) g->write((uint8_t)label[i]);
  if (*word) {
    g->setTextColor(col);
    g->setCursor(wordX, WF_LINE1_Y);
    g->print(word);
  }

  // ---- line 2: bar, info ----
  const int16_t infoX =
      right - (int16_t)strlen(s.info) * GLYPH_W * WF_INFO_SIZE;
  if (s.info[0]) {
    g->setTextSize(WF_INFO_SIZE);
    g->setTextColor(C_TEXT);
    g->setCursor(infoX, WF_LINE2_Y);
    g->print(s.info);
  }
  if (!s.total) return;           // nothing known: no bar, not an empty one
                                  // that would read as 0%
  const int16_t barW = infoX - WF_BAR_GAP - PAD_X;
  const uint16_t segs =
      wfSegments(s.total, (uint16_t)barW, WF_SEG_GAP, WF_SEG_MIN);
  if (segs) {
    // done, then failed, then pending -- the order the run fills them in.
    // The edges are spread so the last segment ends exactly where a
    // continuous bar would; one floored width for every segment would leave
    // up to a pixel per segment of ragged gap at the end.
    const uint32_t span = (uint32_t)barW + WF_SEG_GAP;
    for (uint16_t i = 0; i < segs; i++) {
      const int16_t x0 = PAD_X + (int16_t)(span * i / segs);
      const int16_t x1 =
          PAD_X + (int16_t)(span * (i + 1u) / segs) - WF_SEG_GAP;
      if (i < s.done)
        g->fillRect(x0, WF_BAR_Y, x1 - x0, WF_BAR_H, col);
      else if (i < s.done + s.failed)
        g->fillRect(x0, WF_BAR_Y, x1 - x0, WF_BAR_H, C_ERROR);
      else
        g->drawRect(x0, WF_BAR_Y, x1 - x0, WF_BAR_H, C_DIMMER);
    }
  } else {
    g->drawRect(PAD_X, WF_BAR_Y, barW, WF_BAR_H, C_DIMMER);
    const uint16_t f = wfFillPx(s.done, s.total, (uint16_t)(barW - 2));
    if (f) g->fillRect(PAD_X + 1, WF_BAR_Y + 1, f, WF_BAR_H - 2, col);
  }
}
