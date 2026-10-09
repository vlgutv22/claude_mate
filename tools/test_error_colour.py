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
in the plain text colour. Colour alone is not enough, though -- code blocks,
diffs, the spinner and failed tool output are coloured too -- and what none of
them does is START a row with the phrase. So a phrase counts only where it
opens a row; above the prompt box's lower rule it must also be in an error
colour and still current (no prompt submitted after it); in Claude's chrome
below the box (where the usage-limit notices live) opening a row is enough;
with no colours at all, a plain substring as before.

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
check("wide characters before the phrase: it does not open the row (idle)",
      state(("⏺ 日本語のメモ: api error の説明",)) == "idle")

print("\nClaude's own error UI is still an error")
API_ROW = '⏺ API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
for name, colour in (("dark theme", WARN_DARK), ("light theme", WARN_LIGHT),
                     ("ANSI theme", WARN_ANSI), ("256-colour terminal", WARN_256)):
    got = state((f"{colour}{API_ROW}{OFF}",))
    check(f"an API-error row in the warning colour ({name}) reads error", got == "error", got)
got = state((f"{ERR_DARK}✻ API error{OFF}{DIM} · Retrying in 5s · attempt 1/10{OFF}",))
check("the live retry line (attempts 1-2), error in the error colour, reads error", got == "error", got)
got = state((f"{WARN_DARK}⏺ API Error: 500 内部サーバーエラー Internal server error{OFF}",))
check("an API-error row with wide characters in it reads error", got == "error", got)
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

print("\ncoloured, but not an error row: it does not OPEN the row")
RED = "\x1b[31m"                              # the code highlighter's string colour
MONO = "\x1b[38;2;230;219;116m"               # the diff's Monokai string colour
ORANGE = "\x1b[38;2;215;119;87m"              # Claude's own colour (the spinner)
cases = (
    ("a code block whose string literals name the phrases",
     ("⏺ The strong list now reads:", f'  {RED}"api error"{OFF}, {RED}"usage limit reached"{OFF},',
      f'  {RED}"you\'ve hit your"{OFF}', "  Restart your claude sessions."), "idle"),
    ("an Edit diff with the phrases in Monokai strings",
     ("⏺ Update(bin/claude-mate-wrap)", "  ⎿  Updated bin/claude-mate-wrap with 1 addition",
      f'      133 +          {MONO}"usage limit reached"{OFF}, {MONO}"you\'ve hit your"{OFF},',
      "⏺ Done."), "idle"),
    ("a failed command's output in the error colour",
     ("⏺ Bash(python3 tools/test_error_colour.py)", f"  ⎿  {ERR_DARK}Error: Exit code 1{OFF}",
      f"     {ERR_DARK}  [PASS] reported: a reply explaining the 'api error' case{OFF}",
      "⏺ One case fails; looking at it now."), "idle"),
    ("a stderr log line in the error colour",
     ("⏺ Bash(python3 scripts/replay.py)", "  ⎿  replayed 12 frames",
      f"     {ERR_DARK}WARNING:root:frame 7: API error 529 in a fixture, skipped{OFF}",
      "⏺ The replay is clean."), "idle"),
)
for name_, rows, want in cases:
    got = state(rows)
    check(f"{name_}: {want}", got == want, got)
spin = ("⏺ I'll fix the detector.",
        f"{ORANGE}✻ Fixing the api error false positive… {OFF}{DIM}(2m 3s · ↓ 4.5k tokens){OFF}")
got = state(spin, chrome=("  esc to interrupt",))
check("the spinner naming the task in Claude's orange: working, not error", got == "working", got)
got = state(("⏺ Launching an explorer.",),
            chrome=(HINT, "  ◯ Explore  Find the api error checks            9s · ↓ 7.5k tokens"))
check("an agent row in the chrome whose description names the phrase: not error", got != "error", got)

print("\nan error row is over once you have moved on")
STALE_API = (f"{WARN_DARK}{API_ROW}{OFF}", "❯ try again", "⏺ Done -- all 42 tests pass.")
got = state(STALE_API)
check("an old API-error row with a prompt submitted after it: idle", got == "idle", got)
STALE_HIT = (f"  ⎿  {ERR_DARK}You've hit your session limit · resets 3pm{OFF}", "❯ continue",
             "⏺ Picking up where I left off.")
got = state(STALE_HIT)
check("an old limit row after the limit reset and you continued: idle", got == "idle", got)
got = state((f"  ⎿  {ERR_DARK}You've hit your session limit · resets 3pm{OFF}",))
check("...but with nothing submitted since, the limit still reads error", got == "error", got)

print("\na permission prompt is not overruled by an error phrase in its command")
scr_lines = ["⏺ Let me look.", RULE, " Bash command", "",
             '   grep -rn "api error" bin/claude-mate-wrap tools/', "", " Do you want to proceed?",
             " ❯ 1. Yes", "   2. Yes, and don't ask again for grep commands", "   3. No, and tell Claude what to do differently (esc)"]
scr = pyte.Screen(110, len(scr_lines) + 2)
pyte.ByteStream(scr).feed("\r\n".join(scr_lines).encode())
d = scr.display
cells = cmw.colour_phrases(scr, d, cmw._patterns["error"] + cmw._patterns.get("error_footer", []))
got = cmw.detect_state("\n".join(d), err_cells=cells)[0]
check("a Bash permission prompt whose command names the phrase: waiting", got == "waiting", got)

print("\nevery limit row 2.1.295 can end a turn with")
for text in ("You're out of usage credits · resets 3pm", "You've reached your Fable limit.",
             "Your org is out of usage · add funds to continue", "You're out of extra usage"):
    got = state((f"  ⎿  {ERR_DARK}{text}{OFF}",))
    check(f"limit row: {text[:34]}...", got == "error", got)

print("\nClaude with colour OFF (NO_COLOR, TERM=dumb, a CI environment)")
plain = ["⏺ " + API_ROW[2:], "⏺ The fix: the api error check now looks at colours."]
scr = pyte.Screen(110, 12)
pyte.ByteStream(scr).feed("\r\n".join(plain[:1] + [RULE, "❯ ", RULE, HINT]).encode())
check("a screen without a single colour is recognised as such", cmw.screen_has_colour(scr) is False)
scr2 = pyte.Screen(110, 12)
pyte.ByteStream(scr2).feed(f"{WARN_DARK}{API_ROW}{OFF}".encode())
check("...and one coloured cell is enough to say otherwise", cmw.screen_has_colour(scr2) is True)
d = scr.display
cells = cmw.colour_phrases(scr, d, cmw._patterns["error"] + cmw._patterns.get("error_footer", []))
got = cmw.detect_state("\n".join(d), err_cells=cells, err_colour=False)[0]
check("colour off: an API-error row still reads error", got == "error", got)
scr = pyte.Screen(110, 12)
pyte.ByteStream(scr).feed("\r\n".join(plain[1:] + [RULE, "❯ ", RULE, HINT]).encode())
d = scr.display
cells = cmw.colour_phrases(scr, d, cmw._patterns["error"] + cmw._patterns.get("error_footer", []))
got = cmw.detect_state("\n".join(d), err_cells=cells, err_colour=False)[0]
check("colour off: a reply that mentions the phrase mid-sentence still reads idle", got == "idle", got)

print("\ncolour_phrases itself")
text, cells = screen((f"{WARN_DARK}{API_ROW}{OFF}", "⏺ the api error is gone"))
coloured = [ln.strip()[:24] for ln in cells]
check("only the row the phrase opens is reported", coloured == ["⏺ API Error: 529 {\"type\""], str(coloured))
check("...with the phrase that opens it, marked coloured",
      cells[next(iter(cells))] == {"api error": True}, str(cells))
check("no phrases, no work", cmw.colour_phrases(None, ["anything"], []) == {})
scr = pyte.Screen(110, 4)
pyte.ByteStream(scr).feed(f"{WARN_DARK}{API_ROW}{OFF}".encode())
got = cmw.colour_phrases(scr, scr.display, [" api error "])
check("a pattern written with spaces around it still anchors on its text",
      list(got.values()) == [{" api error ": True}], str(got))

print("\n================", "ALL PASSED" if not fails else f"{fails} FAILED", "================")
sys.exit(1 if fails else 0)
