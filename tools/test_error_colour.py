#!/usr/bin/env python3
"""
Unit tests for how bin/claude-mate-wrap tells Claude's own error UI from text
that merely mentions an error.

Regression focus: "device saying its error". The strong error phrases ("api
error", "overloaded_error", "usage limit reached", ...) are matched in the
bottom 20 lines of the screen, and those lines are mostly conversation. The
reply on screen when it was reported explained, in so many words, that "a
workflow named after 'the api error' read ERROR" -- so the session read ERR.

Claude Code draws every error it reports in a colour: an API-error row
("⏺ API Error: 529 {...}") in the warning colour, glyph and text; the live retry
line's error in the error colour. Prose, quoted logs and the line you type are
in the plain text colour. So above the prompt box's lower rule a phrase counts
only on an error- or warning-coloured cell; in Claude's chrome below it (where
the usage-limit notices live), or with no colours to go on, as before.

Every case runs ANSI bytes through a REAL pyte screen and reads the cells with
colour_phrases(), exactly as evaluate() does.

Run:  python3 tools/test_error_colour.py     (needs pyte: pip install pyte)
"""
import os, sys, importlib.util
from importlib.machinery import SourceFileLoader

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAP = os.path.join(REPO, "bin", "claude-mate-wrap")

loader = SourceFileLoader("cmw", WRAP)
spec = importlib.util.spec_from_loader("cmw", loader)
cmw = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(cmw)
    import pyte
except ImportError as e:                       # match the suite's convention
    print(f"SKIP: could not import wrapper ({e}). Run: pip install pyte")
    sys.exit(0)

fails = 0


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail and not ok else ""))


RULE = "─" * 60
HINT = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
OFF = "\x1b[0m"
WARN_DARK = "\x1b[38;2;255;193;7m"           # Claude's dark theme "warning"
WARN_LIGHT = "\x1b[38;2;150;108;30m"         # ...light theme
WARN_ANSI = "\x1b[33m"                       # ...light-ansi (pyte: "brown")
WARN_256 = "\x1b[38;5;220m"                  # ...dark theme on Apple Terminal
ERR_DARK = "\x1b[38;2;255;107;128m"          # Claude's dark theme "error"
DIM = "\x1b[2m"


def screen(transcript, typed="", chrome=(HINT,), cols=110):
    lines = list(transcript) + [RULE, "❯ " + typed, RULE] + list(chrome)
    scr = pyte.Screen(cols, len(lines) + 2)
    pyte.ByteStream(scr).feed("\r\n".join(lines).encode())
    display = scr.display
    phrases = cmw._patterns["error"] + cmw._patterns.get("error_footer", [])
    return "\n".join(display), cmw.colour_phrases(scr, display, phrases)


def state(*a, **kw):
    text, cells = screen(*a, **kw)
    return cmw.detect_state(text, err_cells=cells)[0]


def text_only(*a, **kw):
    return cmw.detect_state(screen(*a, **kw)[0])[0]


print("prose that MENTIONS an error is not an error")
REPORTED = (
    "⏺ The fix, in bin/claude-mate-wrap:",
    "  - Workflow rows no longer confuse the error check. A workflow named after",
    '    "the api error" read ERROR before.',
    "  To pick it up, restart your claude sessions.",
)
check("reported: a reply explaining the 'api error' case reads idle, not ERR",
      state(REPORTED) == "idle", state(REPORTED))
check("...and read as text alone it is the ERR that was reported (why colours matter)",
      text_only(REPORTED) == "error", text_only(REPORTED))
PROSE = ("⏺ The upstream service answered with an internal server error yesterday,",
         "  so the retry helper now backs off. Nothing is failing right now.")
check("a content-prone phrase in prose reads idle", state(PROSE) == "idle", state(PROSE))
check("the phrase in the line you are TYPING reads idle",
      state(("⏺ Done.",), typed="why did the api error come back?") == "idle")
check("a quoted log line in the plain colour reads idle",
      state(("⏺ The log said:", "  2026-10-09 12:00:01 usage limit reached for key k-12")) == "idle")
check("wide characters before the phrase do not shift the colour read",
      state(("⏺ 日本語のメモ: api error の説明",)) == "idle")

print("\nClaude's own error UI is still an error")
API_ROW = '⏺ API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
for name, colour in (("dark theme", WARN_DARK), ("light theme", WARN_LIGHT),
                     ("ANSI theme", WARN_ANSI), ("256-colour terminal", WARN_256)):
    got = state((f"{colour}{API_ROW}{OFF}",))
    check(f"an API-error row in the warning colour ({name}) reads error", got == "error", got)
got = state((f"✻ {ERR_DARK}API Error (Connection error.){OFF}{DIM} · Retrying in 5s · attempt 3/10{OFF}",))
check("the live retry line, error in the error colour, reads error", got == "error", got)
got = state((f"{WARN_DARK}⏺ 日本語 API Error: 500 Internal server error{OFF}",))
check("an API-error row after wide characters reads error", got == "error", got)
got = state(REPORTED + (f"{WARN_DARK}{API_ROW}{OFF}",))
check("prose that mentions it AND a real error row below it: error", got == "error", got)
got = state(("⏺ Working on it.",), chrome=(HINT, "  Usage limit reached · limit resets 3pm"))
check("a usage-limit notice in Claude's chrome (plain colour) reads error, as before",
      got == "error", got)

print("\na usage limit, in 2.1.295's own words")
GREY = "\x1b[38;2;153;153;153m"               # Claude's "inactive" (a notice)
HIT = "⏺ You've hit your 5-hour limit · resets 3pm"
AUTO = "⏺ Usage limit reached · continuing automatically at 3pm · esc to cancel"
got = state((f"{WARN_DARK}{HIT}{OFF}",))
check("the turn-ending limit row ('You've hit your ...', warning colour) reads error",
      got == "error", got)
got = state((f"{WARN_DARK}{HIT}{OFF}", f"{GREY}{AUTO}{OFF}"))
check("...and still does with the auto-continue notice under it", got == "error", got)
got = state((f"{GREY}{AUTO}{OFF}",))
check("the grey auto-continue notice alone is a notice, not an error (idle)", got == "idle", got)
got = state(("⏺ Nice -- you've hit your stride with these tests.",))
check("a reply that says 'you've hit your' in the plain colour reads idle", got == "idle", got)

print("\ncolour_phrases itself")
text, cells = screen((f"{WARN_DARK}{API_ROW}{OFF}", "⏺ the api error is gone"))
coloured = [ln.strip()[:24] for ln in cells]
check("only the coloured row is reported", coloured == ["⏺ API Error: 529 {\"type\""], str(coloured))
check("with every phrase that starts on a coloured cell",
      set(next(iter(cells.values()))) == {"api error", "overloaded_error"}, str(cells))
check("no phrases, no work", cmw.colour_phrases(None, ["anything"], []) == {})

print("\n================", "ALL PASSED" if not fails else f"{fails} FAILED", "================")
sys.exit(1 if fails else 0)
