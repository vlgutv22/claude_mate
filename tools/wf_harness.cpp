// Run the DEVICE workflow band's parser and geometry on the host.
// ===============================================================
//
// WHY THIS EXISTS. The band is the one thing on either screen the firmware lays
// out itself: the daemon sends numbers (J|) and each device turns them into a
// bar at absolute pixel positions. CI compiles both sketches and runs neither,
// so without this every one of the following was checkable only by flashing a
// board and squinting at it:
//
//   * THE GRAMMAR. A J| line that is half another line -- a lost newline on a
//     full BLE ring glues the next F| onto it -- must be refused whole, not
//     drawn as a workflow called "deployF|4|4|api-server|WORK". Refusing must
//     also leave the last good status exactly as it was.
//   * THE ARITHMETIC. Counts saturate instead of wrapping (atoi is 16-bit on
//     the Nano and would pass on a Mac), and the fill is Claude Code's own
//     floor(done * w / total), so a bar here reads the same fraction as the
//     one in the terminal.
//   * THE LAYOUT. The Nano has 128 px for a bar AND up to 14 characters of
//     info; the S3 band has 34 px of height between the fleet strip and the
//     accent bar. A string one character too long runs into its neighbour and
//     nothing warns. Every status, every info length, every count is drawn
//     into a recording stub and its extents checked.
//   * THE STAGING. J| alone must change nothing on the glass; the F| after it
//     commits it.
//
// Only board_s3.h, workflow.h and wfband.h are included, exactly as the sketch
// includes them -- no copies. tools/test_workflow_fw.py builds this TWICE:
// with -DWF_NAME_MAX=1, the way the Nano includes workflow.h, and with the
// default the S3 uses. C headers only: on macOS <sys/syslimits.h>, which the
// C++ containers drag in, defines its own LINE_MAX and collides with
// board_s3.h's.
#include <stdint.h>
#include <stdio.h>
#include <string.h>

// Arduino_GFX's own macro; board_s3.h writes its palette in it.
#define RGB565(r, g, b) \
  ((uint16_t)((((r) & 0xF8) << 8) | (((g) & 0xFC) << 3) | ((b) >> 3)))

#ifdef LINE_MAX
#undef LINE_MAX            // a host's (2048); board_s3.h's 160 is the real one
#endif
#include "board_s3.h"
#include "workflow.h"

// ---- a GFX that draws nothing and remembers everything ----------------------
// Text is recorded as runs: everything printed or written from one cursor
// position in one size and colour is one op, with its full pixel extent.
struct Op {
  char     kind;           // T text, R fillRect, r drawRect, C fillCircle,
                           // c drawCircle
  int      x, y, w, h;     // [x, x+w) x [y, y+h)
  uint16_t col;
  int      size;
  char     text[80];
};

#define MAX_OPS 256
class Arduino_GFX {
 public:
  Op       ops[MAX_OPS];
  int      n = 0, lost = 0;
  int      size = 1, cx = 0, cy = 0;
  uint16_t col = 0;
  Op      *run = nullptr;

  void reset() { n = 0; lost = 0; size = 1; cx = cy = 0; col = 0; run = nullptr; }
  Op *add(char k, int x, int y, int w, int h, uint16_t c) {
    run = nullptr;
    if (n >= MAX_OPS) { lost++; return nullptr; }
    Op &o = ops[n++];
    o.kind = k; o.x = x; o.y = y; o.w = w; o.h = h; o.col = c; o.size = size;
    o.text[0] = 0;
    return &o;
  }
  void setTextSize(uint8_t s) { size = s; run = nullptr; }
  void setTextColor(uint16_t c) { col = c; run = nullptr; }
  void setCursor(int16_t x, int16_t y) { cx = x; cy = y; run = nullptr; }
  size_t write(uint8_t ch) {
    if (!run) {
      Op *o = add('T', cx, cy, 0, GLYPH_H * size, col);
      if (!o) return 0;
      run = o;
    }
    size_t len = strlen(run->text);
    if (len + 1 < sizeof(run->text)) { run->text[len] = (char)ch; run->text[len + 1] = 0; }
    run->w += GLYPH_W * size;
    cx += GLYPH_W * size;
    return 1;
  }
  size_t print(const char *s) {
    size_t k = 0;
    while (*s) k += write((uint8_t)*s++);
    return k;
  }
  void fillRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t c) { add('R', x, y, w, h, c); }
  void drawRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t c) { add('r', x, y, w, h, c); }
  void fillCircle(int16_t x, int16_t y, int16_t r, uint16_t c) { add('C', x - r, y - r, 2 * r + 1, 2 * r + 1, c); }
  void drawCircle(int16_t x, int16_t y, int16_t r, uint16_t c) { add('c', x - r, y - r, 2 * r + 1, 2 * r + 1, c); }
};

#include "wfband.h"

static int fails = 0;
static int checks = 0;

static void ok(bool cond, const char *what) {
  checks++;
  if (cond) printf("  ok    %s\n", what);
  else      { printf("  FAIL  %s\n", what); fails++; }
}

// For the sweeps: silent while they pass, loud on the first few failures, one
// summary line either way -- a 10,000-case loop must not bury the output.
struct Sweep {
  const char *name;
  long cases = 0, bad = 0;
  explicit Sweep(const char *n) : name(n) {}
  void check(bool cond, const char *detail) {
    cases++;
    if (cond) return;
    if (++bad <= 5) printf("  FAIL  %s: %s\n", name, detail);
  }
  void done() {
    checks++;
    if (bad) { fails++; printf("  FAIL  %s (%ld of %ld cases)\n", name, bad, cases); }
    else     printf("  ok    %s (%ld cases)\n", name, cases);
  }
};

static void fill(char *buf, char c, int n) {
  memset(buf, c, (size_t)n);
  buf[n] = 0;
}

static const char *kStatuses = "REPDFQ";   // Q: a letter nobody defined yet

// ---- the grammar -------------------------------------------------------------
static void test_accepts() {
  printf("the grammar: what is accepted\n");
  WfStatus s;
  memset(&s, 0, sizeof(s));

  ok(wfParse("J|R|0|0|5|0/5 2m39s 543k|deployment-risk-sweep-3802", s),
     "the screenshot's running workflow parses");
  ok(s.st == 'R' && s.done == 0 && s.failed == 0 && s.total == 5,
     "...status and counts, in the wire's done|failed|total order");
  ok(!strcmp(s.info, "0/5 2m39s 543k"), "...info verbatim");
  if (WF_NAME_MAX >= 26)
    ok(!strcmp(s.name, "deployment-risk-sweep-3802"), "...name verbatim");
  else
    ok(strlen(s.name) == WF_NAME_MAX && s.name[0] == 'd',
       "...the name cut to what this build keeps (WF_NAME_MAX)");

  ok(wfParse("J|E|2|1|5|2/5 4m10s 880k|x", s) && s.done == 2 && s.failed == 1,
     "done and failed are separate fields");

  ok(wfParse("J|Q|1|0|5|x|y", s) && s.st == 'Q',
     "an unknown capital letter is accepted (drawn neutrally, not refused)");

  ok(wfParse("J|R|0|0|0|running|Dynamic workflow", s) && s.total == 0 &&
         !strcmp(s.info, "running"),
     "nothing numeric known: 0|0|0 with the status word as info");

  ok(wfParse("J|R|0|0|0||", s) && s.info[0] == 0 && s.name[0] == 0,
     "empty info and empty name are both legal");

  char info[WF_INFO_MAX + 1], name[WF_NAME_WIRE + 1], line[200];
  fill(info, 'i', WF_INFO_MAX);
  fill(name, 'n', WF_NAME_WIRE);
  snprintf(line, sizeof(line), "J|R|65535|65535|65535|%s|%s", info, name);
  ok(strlen(line) == 77, "the longest legal line is 77 bytes");
  ok(strlen(line) <= 95, "...inside the Nano's 95-byte line buffer");
  ok(wfParse(line, s) && strlen(s.info) == WF_INFO_MAX &&
         (int)strlen(s.name) == (WF_NAME_MAX < WF_NAME_WIRE ? WF_NAME_MAX
                                                            : WF_NAME_WIRE),
     "...and it parses, every field at its cap");

  memset(&s, 0x5a, sizeof(s));
  ok(wfParse("J|-", s) && s.st == 0, "J|- clears");
}

static void test_refuses() {
  printf("the grammar: what is refused, and refusing changes nothing\n");
  char longInfo[40], longName[80], l1[200], l2[200];
  fill(longInfo, 'i', WF_INFO_MAX + 1);
  fill(longName, 'n', WF_NAME_WIRE + 1);
  snprintf(l1, sizeof(l1), "J|R|1|0|5|%s|x", longInfo);
  snprintf(l2, sizeof(l2), "J|R|1|0|5|x|%s", longName);

  struct { const char *line, *why; } bad[] = {
      {"J|R|1|0|5|1/5|deployF|4|4|api|WORK  00:01|Opus|1/1 W",
       "a J| with the next F| spliced on (lost newline)"},
      {"J|R|1|0|5|x|a|b", "a '|' in the name"},
      {"J|R|1|0|5|x|y|", "a trailing '|'"},
      {"J|R|1|0|5|x", "six fields"},
      {"J|R|1|0|5|", "six fields, the last one empty"},
      {"J|R|1|0|5", "five fields"},
      {"J|R||0|5|x|y", "an empty count"},
      {"J|R|1||5|x|y", "an empty failed count"},
      {"J|R|1|0||x|y", "an empty total"},
      {"J|R|123456|0|5|x|y", "a count of six digits"},
      {"J|R|000001|0|5|x|y", "six digits, even with leading zeros"},
      {"J|R|1a|0|5|x|y", "a count with a letter in it"},
      {"J|R|-1|0|5|x|y", "a negative count"},
      {"J|R| 1|0|5|x|y", "a count with a space"},
      {"J|R|+1|0|5|x|y", "a count with a sign"},
      {"J|RR|1|0|5|x|y", "a two-letter status"},
      {"J|r|1|0|5|x|y", "a lowercase status"},
      {"J||1|0|5|x|y", "an empty status"},
      {"J|1|1|0|5|x|y", "a digit for a status"},
      {"J|@|1|0|5|x|y", "the character before 'A'"},
      {"J|[|1|0|5|x|y", "the character after 'Z'"},
      {l1, "info one character over 14"},
      {l2, "a name one character over 40"},
      {"J|", "a bare J|"},
      {"J", "a bare J"},
      {"J|-x", "J|- with something after it"},
      {"J|-|", "J|- with a field after it"},
      {"J|--", "J|--"},
      {"F|R|1|0|5|x|y", "another verb"},
      {"j|R|1|0|5|x|y", "a lowercase verb"},
      {"JR|1|0|5|x|y", "no bar after the verb"},
      {"", "an empty line"},
  };

  WfStatus good, s;
  memset(&good, 0, sizeof(good));
  wfParse("J|D|5|0|5|5/5 7m42s 1.4M|the-last-good-one", good);
  int refused = 0, untouched = 0;
  const int total = (int)(sizeof(bad) / sizeof(bad[0]));
  for (int i = 0; i < total; i++) {
    s = good;
    bool accepted = wfParse(bad[i].line, s);
    if (!accepted) refused++;
    if (!memcmp(&s, &good, sizeof(s))) untouched++;
    if (accepted || memcmp(&s, &good, sizeof(s))) {
      char what[256];
      snprintf(what, sizeof(what), "refused, state untouched: %s", bad[i].why);
      ok(false, what);
    }
  }
  char sum[128];
  snprintf(sum, sizeof(sum), "all %d malformed lines refused (%d)", total, refused);
  ok(refused == total, sum);
  snprintf(sum, sizeof(sum), "...and every one left the last good status alone (%d)",
           untouched);
  ok(untouched == total, sum);
  ok(!wfParse("J|R|1|0|5|1/5|deployF|4|4|api|WORK  00:01|Opus|1/1 W", s),
     "the spliced line from the review, by name");
}

static void test_numbers() {
  printf("counts: saturating, clamped\n");
  WfStatus s;
  memset(&s, 0, sizeof(s));
  ok(wfParse("J|R|99999|0|99999|x|y", s) && s.done == 65535 && s.total == 65535,
     "99999 saturates to 65535 rather than wrapping");
  ok(wfParse("J|R|65536|0|65535|x|y", s) && s.done == 65535,
     "65536 saturates too");
  ok(wfParse("J|R|40000|0|40001|x|y", s) && s.done == 40000 && s.total == 40001,
     "counts past 32767 survive (atoi would not, on the Nano)");
  ok(wfParse("J|R|00007|0|00009|x|y", s) && s.done == 7 && s.total == 9,
     "leading zeros inside five digits are fine");
  ok(wfParse("J|R|9|4|5|x|y", s) && s.done == 5 && s.failed == 0,
     "done past total is clamped to total, and failed to what is left");
  ok(wfParse("J|R|3|4|5|x|y", s) && s.done == 3 && s.failed == 2,
     "failed is clamped to total - done");
  ok(wfParse("J|E|2|1|5|x|y", s) && s.done == 2 && s.failed == 1 && s.total == 5,
     "consistent counts are left exactly as sent");
  ok(wfParse("J|R|3|4|0|x|y", s) && s.done == 3 && s.failed == 4 && s.total == 0,
     "total 0 clamps nothing -- there is no bar to keep them inside");
}

// ---- the arithmetic ------------------------------------------------------------
static void test_fill() {
  printf("the fill is Claude Code's floor(done * w / total)\n");
  const uint16_t widths[] = {1, 2, 32, 39, 41, 123, 125, 132, 134, 300, 306};
  Sweep sw("fill parity over total 1..120, every done, eleven widths");
  for (uint16_t total = 1; total <= 120; total++)
    for (uint16_t done = 0; done <= total; done++)
      for (uint16_t w : widths) {
        long want = (long)done * w / total;
        char d[160];
        snprintf(d, sizeof(d), "done %u total %u w %u: got %u want %ld",
                 done, total, w, wfFillPx(done, total, w), want);
        sw.check(wfFillPx(done, total, w) == want, d);
      }
  sw.done();
  ok(wfFillPx(999, 1000, 306) == 305, "999/1000 of 306 is 305 (no 16-bit overflow)");
  ok(wfFillPx(65534, 65535, 300) == 299, "65534/65535 of 300 is 299");
  ok(wfFillPx(65535, 65535, 300) == 300, "65535/65535 is full");
  ok(wfFillPx(0, 5, 306) == 0 && wfFillPx(5, 5, 306) == 306, "0/5 empty, 5/5 full");
  ok(wfFillPx(3, 0, 306) == 0, "total 0 fills nothing");
  ok(wfFillPx(9, 5, 40) == 40, "done past total never overfills");
}

static void test_segments() {
  printf("segments: one per agent while each gets >= 4 px\n");
  ok(wfSegments(5, 302, 2, 4) == 5, "5 agents in 302 px: five segments");
  ok(wfSegments(50, 302, 2, 4) == 50, "50 in 302 px (50*4 + 49*2 = 298): still segments");
  ok(wfSegments(51, 302, 2, 4) == 0, "51 in 302 px (304) falls back to one bar");
  ok(wfSegments(22, 134, 2, 4) == 22 && wfSegments(23, 134, 2, 4) == 0,
     "beside a 14-char info (134 px): 22 agents get segments, 23 do not");
  ok(wfSegments(80, 302, 2, 4) == 0, "80 agents: one continuous bar");
  ok(wfSegments(0, 302, 2, 4) == 0, "no agents known: no segments");
  ok(wfSegments(1, 4, 2, 4) == 1 && wfSegments(1, 3, 2, 4) == 0,
     "a single agent needs only its own minimum");
  Sweep sw("wfSegments matches its definition, total 0..120 x width 0..320");
  for (uint16_t total = 0; total <= 120; total++)
    for (uint16_t w = 0; w <= 320; w++) {
      long need = (long)total * 4 + ((long)total - 1) * 2;
      uint16_t want = (total && need <= w) ? total : 0;
      char d[160];
      snprintf(d, sizeof(d), "total %u width %u: got %u want %u", total, w,
               wfSegments(total, w, 2, 4), want);
      sw.check(wfSegments(total, w, 2, 4) == want, d);
    }
  sw.done();
}

static void test_words() {
  printf("the status words\n");
  ok(!strcmp(wfWord('R'), "running") && !strcmp(wfWord('E'), "running"),
     "R and E are both running (E is running with an agent failed)");
  ok(!strcmp(wfWord('P'), "paused"), "P is paused");
  ok(!strcmp(wfWord('D'), "done"), "D is done");
  ok(!strcmp(wfWord('F'), "ended"), "F is ended (failed or stopped: the screen cannot tell)");
  ok(!strcmp(wfWord('Q'), "") && !strcmp(wfWord(0), "") && !strcmp(wfWord('r'), ""),
     "anything else has no word rather than a guessed one");
}

// ---- the Nano's r2 ---------------------------------------------------------------
struct Counts { uint16_t done, failed, total; };
static const Counts kCounts[] = {
    {0, 0, 0}, {0, 0, 5}, {3, 1, 5}, {5, 0, 5}, {2, 1, 80},
    {999, 0, 1000}, {65535, 0, 65535},
};

static void test_nano_row() {
  printf("the Nano's r2: every status, every info length 0..14, inside 128 px\n");
  Sweep geom("bar / '!' / word never reach the info, everything inside r2");
  Sweep words("P/D/F words fit beside the longest info");
  for (const char *st = kStatuses; *st; st++)
    for (const Counts &c : kCounts)
      for (uint8_t len = 0; len <= WF_INFO_MAX; len++) {
        const WfRowLayout L = wfRowLayout(*st, c.done, c.total, len);
        char d[256];
        snprintf(d, sizeof(d), "st %c %u/%u info %u: infoX %d area %d bar %d fill %d bang %d word %d",
                 *st, c.done, c.total, len, L.infoX, L.areaW, L.barW, L.fillW,
                 L.bangX, (int)L.word);
        const int infoX = WF_ROW_W - len * WF_ROW_GLYPH;
        bool g = L.infoX == infoX && L.areaW == infoX - WF_ROW_GAP;
        // the bar: inside [0, areaW), its fill inside its outline, CLI's math
        g = g && L.barW >= 0 && L.barW <= L.areaW;
        if (L.barW) {
          g = g && L.barW >= 8 && c.total > 0;
          g = g && L.fillW == (int)wfFillPx(c.done, c.total, (uint16_t)(L.barW - 2));
          g = g && L.fillW >= 0 && L.fillW <= L.barW - 2;
        } else {
          g = g && L.fillW == 0;
        }
        // E's '!' cell: after the bar with a blank column, before the gap
        if (L.bangX >= 0) {
          g = g && *st == 'E' && L.bangX >= L.barW + 1 &&
              L.bangX + WF_ROW_GLYPH <= L.areaW;
        }
        // which status draws what
        switch (*st) {
          case 'R': g = g && L.barW == (c.total ? L.areaW : 0) && L.bangX < 0 && !L.word; break;
          case 'E': g = g && L.barW == (c.total ? L.areaW - WF_ROW_BANG_W : 0) &&
                        L.bangX == L.areaW - WF_ROW_GLYPH && !L.word; break;
          case 'P': case 'D': case 'F': g = g && L.word && !L.barW && L.bangX < 0; break;
          default:  g = g && !L.word && !L.barW && L.bangX < 0; break;
        }
        geom.check(g, d);
        if (L.word) {
          const int wordPx = (int)strlen(wfWord(*st)) * WF_ROW_GLYPH;
          words.check(wordPx <= L.areaW, d);
        }
      }
  geom.done();
  words.done();
  ok(WF_ROW_BAR_Y >= WF_ROW_Y && WF_ROW_BAR_Y + WF_ROW_BAR_H <= WF_ROW_Y + WF_ROW_H,
     "the bar's outline stays inside r2 (y16..23)");
  const WfRowLayout widest = wfRowLayout('E', 1, 5, WF_INFO_MAX);
  ok(widest.areaW == 41 && widest.barW == 34,
     "a 14-char info leaves 41 px: a 34 px bar and the '!' for E");
  const WfRowLayout hook = wfRowLayout('R', 0, 0, 7);
  ok(hook.infoX == 128 - 42 && hook.barW == 0,
     "nothing known ('running' as info): the text, and no empty box");
  const WfRowLayout r55 = wfRowLayout('R', 5, 5, 3), d55 = wfRowLayout('D', 5, 5, 3);
  ok(r55.barW && r55.fillW == r55.barW - 2 && d55.word && !d55.barW,
     "done at 5/5 says 'done'; running at 5/5 is a full bar -- not the same picture");
}

// ---- the S3 band -------------------------------------------------------------------
static bool inside(const Op &o, int x0, int y0, int x1, int y1) {
  return o.x >= x0 && o.y >= y0 && o.x + o.w <= x1 && o.y + o.h <= y1 && o.w >= 0 && o.h >= 0;
}

static void test_s3_band() {
  printf("the S3 band: every status inside y%d..%d, x0..%d\n", WF_BAND_Y,
         WF_BAND_Y + WF_BAND_H - 1, SCREEN_W - 1);
  static Arduino_GFX g;
  const int right = SCREEN_W - PAD_X;
  char n40[WF_NAME_WIRE + 1], longLink[100];
  fill(n40, 'n', WF_NAME_WIRE);
  snprintf(longLink, sizeof(longLink),
           "x dialing a host whose name is far too long to fit here.local:8765");
  const char *names[] = {"", "x", n40};
  const char *links[] = {nullptr, "usb cable", longLink};
  const int infoLens[] = {0, 1, 7, 13, 14};

  Sweep box("every op inside the band's strip and on its own line");
  Sweep line1("bullet, name and word: placed, coloured, never touching");
  Sweep line2("info right-aligned in C_TEXT at size 2, the bar clear of it");
  Sweep bar("segments (done, failed, pending) or one bar, as the counts say");
  for (const char *st = kStatuses; *st; st++)
    for (const Counts &c : kCounts)
      for (int il : infoLens)
        for (const char *nm : names)
          for (const char *lk : links) {
            char wire[200], info[WF_INFO_MAX + 1];
            fill(info, '9', il);
            snprintf(wire, sizeof(wire), "J|%c|%u|%u|%u|%s|%s", *st, c.done,
                     c.failed, c.total, info, nm);
            WfStatus s;
            memset(&s, 0, sizeof(s));
            if (!wfParse(wire, s)) { box.check(false, wire); continue; }
            g.reset();
            wfDrawBand(&g, s, lk);
            char d[512];
            snprintf(d, sizeof(d), "%s (link %s)", wire, lk ? lk : "up");

            // -- extents --
            bool b = g.lost == 0 && g.n > 0;
            for (int i = 0; i < g.n; i++) {
              const Op &o = g.ops[i];
              b = b && inside(o, 0, WF_BAND_Y, SCREEN_W, WF_BAND_Y + WF_BAND_H);
              const bool onLine1 = inside(o, 0, WF_LINE1_Y, SCREEN_W, WF_LINE1_Y + GLYPH_H);
              const bool onLine2 = inside(o, 0, WF_LINE2_Y, SCREEN_W,
                                          WF_LINE2_Y + GLYPH_H * WF_INFO_SIZE);
              b = b && (onLine1 || onLine2);
            }
            box.check(b, d);

            // -- line 1 --
            const char *word = wfWord(s.st);
            const int wordX = right - (int)strlen(word) * GLYPH_W;
            const Op *bullet = nullptr, *label = nullptr, *wordOp = nullptr;
            for (int i = 0; i < g.n; i++) {
              const Op &o = g.ops[i];
              if (o.kind == 'C' || o.kind == 'c') bullet = &o;
              else if (o.kind == 'T' && o.y == WF_LINE1_Y && o.x == WF_NAME_X) label = &o;
              else if (o.kind == 'T' && o.y == WF_LINE1_Y) wordOp = &o;
            }
            bool l1 = bullet && bullet->x == PAD_X && bullet->y == WF_LINE1_Y &&
                      bullet->kind == (s.st == 'P' ? 'c' : 'C') &&
                      bullet->col == wfBulletColour(s.st) &&
                      bullet->x + bullet->w < WF_NAME_X;
            if (*word)
              l1 = l1 && wordOp && !strcmp(wordOp->text, word) && wordOp->x == wordX &&
                   wordOp->x + wordOp->w == right && wordOp->col == wfColour(s.st) &&
                   wordOp->size == 1;
            else
              l1 = l1 && !wordOp;
            const char *want = lk ? lk : s.name;
            if (*want) {
              l1 = l1 && label && label->size == 1 &&
                   label->col == (lk ? C_DIM : C_TEXT) &&
                   label->x + label->w <= wordX - GLYPH_W &&
                   !strncmp(label->text, want, strlen(label->text));
              // the daemon's 40 chars always fit, whole, beside any word
              if (!lk) l1 = l1 && !strcmp(label->text, s.name);
            } else {
              l1 = l1 && !label;
            }
            line1.check(l1, d);

            // -- line 2 --
            const int infoX = right - il * GLYPH_W * WF_INFO_SIZE;
            const Op *infoOp = nullptr;
            int rects = 0;
            for (int i = 0; i < g.n; i++) {
              const Op &o = g.ops[i];
              if (o.kind == 'T' && o.y == WF_LINE2_Y) infoOp = &o;
              if (o.kind == 'R' || o.kind == 'r') rects++;
            }
            bool l2 = il ? (infoOp && infoOp->x == infoX && infoOp->x + infoOp->w == right &&
                            infoOp->size == WF_INFO_SIZE && infoOp->col == C_TEXT &&
                            !strcmp(infoOp->text, s.info))
                         : !infoOp;
            for (int i = 0; i < g.n; i++) {
              const Op &o = g.ops[i];
              if (o.kind == 'R' || o.kind == 'r')
                l2 = l2 && inside(o, PAD_X, WF_BAR_Y, infoX - WF_BAR_GAP, WF_BAR_Y + WF_BAR_H);
            }
            line2.check(l2, d);

            // -- the bar --
            const int barW = infoX - WF_BAR_GAP - PAD_X;
            const uint16_t segs = wfSegments(s.total, (uint16_t)barW, WF_SEG_GAP, WF_SEG_MIN);
            const uint16_t col = wfColour(s.st);
            bool br = true;
            if (!s.total) {
              br = rects == 0;                    // no bar, not an empty one
            } else if (segs) {
              br = rects == segs;
              int k = 0, prevEnd = PAD_X - WF_SEG_GAP;
              for (int i = 0; i < g.n && br; i++) {
                const Op &o = g.ops[i];
                if (o.kind != 'R' && o.kind != 'r') continue;
                const bool isDone = k < s.done, isFailed = !isDone && k < s.done + s.failed;
                br = br && o.w >= WF_SEG_MIN && o.x == prevEnd + WF_SEG_GAP &&
                     o.y == WF_BAR_Y && o.h == WF_BAR_H;
                if (isDone)        br = br && o.kind == 'R' && o.col == col;
                else if (isFailed) br = br && o.kind == 'R' && o.col == C_ERROR;
                else               br = br && o.kind == 'r' && o.col == C_DIMMER;
                prevEnd = o.x + o.w;
                k++;
              }
              br = br && prevEnd == PAD_X + barW;   // ends where one bar would
            } else {
              const uint16_t f = wfFillPx(s.done, s.total, (uint16_t)(barW - 2));
              br = rects == (f ? 2 : 1);
              for (int i = 0; i < g.n && br; i++) {
                const Op &o = g.ops[i];
                if (o.kind == 'r')
                  br = o.x == PAD_X && o.w == barW && o.y == WF_BAR_Y && o.h == WF_BAR_H &&
                       o.col == C_DIMMER;
                if (o.kind == 'R')
                  br = o.x == PAD_X + 1 && o.w == f && o.y == WF_BAR_Y + 1 &&
                       o.h == WF_BAR_H - 2 && o.col == col;
              }
            }
            bar.check(br, d);
          }
  box.done();
  line1.done();
  line2.done();
  bar.done();

  WfStatus none;
  memset(&none, 0, sizeof(none));
  g.reset();
  wfDrawBand(&g, none, nullptr);
  ok(g.n == 0, "no workflow (st 0): the band draws nothing at all");

  WfStatus q;
  memset(&q, 0, sizeof(q));
  wfParse("J|Q|1|0|5|x|y", q);
  g.reset();
  wfDrawBand(&g, q, nullptr);
  bool neutral = true, anyWord = false;
  for (int i = 0; i < g.n; i++) {
    const Op &o = g.ops[i];
    if (o.kind == 'C' || o.kind == 'R') neutral = neutral && o.col == C_IDLE;
    if (o.kind == 'T' && o.y == WF_LINE1_Y && o.x != WF_NAME_X) anyWord = true;
  }
  ok(neutral && !anyWord, "an unknown status draws neutrally: C_IDLE, no word");

  WfStatus e;
  memset(&e, 0, sizeof(e));
  wfParse("J|E|2|1|5|2/5 4m10s 880k|sweep", e);
  g.reset();
  wfDrawBand(&g, e, nullptr);
  const uint16_t eWant[] = {C_WORK, C_WORK, C_ERROR, C_DIMMER, C_DIMMER};
  bool eOk = g.n > 0 && g.ops[0].kind == 'C' && g.ops[0].col == C_ERROR;
  int seg = 0;
  for (int i = 0; i < g.n; i++) {
    const Op &o = g.ops[i];
    if (o.kind == 'T' && !strcmp(o.text, "running")) eOk = eOk && o.col == C_WORK;
    if (o.kind == 'R' || o.kind == 'r') eOk = eOk && seg < 5 && o.col == eWant[seg++];
  }
  ok(eOk && seg == 5,
     "E 2/1/5: a red bullet, 'running' and two done segments in C_WORK, "
     "one failed in C_ERROR, two pending");
  ok(wfColour('R') == C_WORK && wfColour('P') == C_WAIT && wfColour('D') == C_DONE &&
         wfColour('F') == C_ERROR && wfColour('Q') == C_IDLE,
     "R C_WORK, P C_WAIT, D C_DONE, F C_ERROR, unknown C_IDLE");
}

// ---- staging -----------------------------------------------------------------------
static void test_staging() {
  printf("J| stages, F| commits\n");
  WfPair p;
  memset(&p, 0, sizeof(p));
  ok(wfStage(p, "J|R|1|0|5|1/5 0m12s 3k|sweep") && p.live.st == 0,
     "a J| with no F| after it changes nothing on the glass");
  wfCommit(p);
  ok(p.live.st == 'R' && p.live.done == 1 && !strcmp(p.live.info, "1/5 0m12s 3k"),
     "the F| after it commits it");

  WfStatus before = p.live;
  wfStage(p, "J|D|5|0|5|5/5 7m42s 1.4M|other");
  ok(!memcmp(&p.live, &before, sizeof(before)),
     "the next session's J| does not reach the screen before its F|");
  wfCommit(p);
  ok(p.live.st == 'D' && p.live.done == 5, "...and its F| shows it");

  before = p.live;
  ok(!wfStage(p, "J|R|1|0|5|1/5|deployF|4|4|api|WORK  00:01|Opus|1/1 W"),
     "a spliced J| is refused at the stage");
  wfCommit(p);
  ok(!memcmp(&p.live, &before, sizeof(before)),
     "...so the F| after it keeps the last good one");

  wfCommit(p);
  ok(!memcmp(&p.live, &before, sizeof(before)),
     "an F| with no new J| (a time tick) commits the same thing again");

  wfStage(p, "J|-");
  ok(p.live.st == 'D', "J|- is staged like any other J|");
  wfCommit(p);
  ok(p.live.st == 0, "...and its F| clears the band");
}

int main(int argc, char **argv) {
  // --parse: a verdict per stdin line -- "1 <line>" accepted, "0 <line>"
  // refused -- so tools/test_workflow_fw.py can hold the J| lines the docs
  // tell you to type by hand to the real grammar. A refused one leaves the
  // previous band up, which reads as the firmware drawing the new one wrong.
  if (argc > 1 && !strcmp(argv[1], "--parse")) {
    char buf[512];
    while (fgets(buf, sizeof(buf), stdin)) {
      buf[strcspn(buf, "\r\n")] = 0;
      WfStatus st;
      printf("%d %s\n", wfParse(buf, st) ? 1 : 0, buf);
    }
    return 0;
  }
  printf("workflow.h with WF_NAME_MAX %d (%s)\n", WF_NAME_MAX,
         WF_NAME_MAX == 1 ? "the Nano's build" : "the S3's build");
  test_accepts();
  test_refuses();
  test_numbers();
  test_fill();
  test_segments();
  test_words();
  test_nano_row();
  test_s3_band();
  test_staging();
  if (fails) printf("\n%d of %d FAILED\n", fails, checks);
  else       printf("\nall %d passed\n", checks);
  return fails ? 1 : 0;
}
