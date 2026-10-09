#!/usr/bin/env python3
"""
Unit tests for how bin/claude-mate-wrap reads a dynamic workflow's footer row.

Regression focus: "workflow not done but device saying its done". Once the turn
that launched a workflow has ended, Claude Code 2.1.295 says the run is still
going in exactly one place -- the workflow's row under the prompt box -- and the
row's status is not in its text but in its bullet's COLOUR:

    "  ◯ workflow-status-design-revi…  ▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱  0/5 · 1m01s · ↓ 491.6k tokens"

    no colour   running              -> keeps the session working
    warning     paused on a limit    -> keeps the session working (glyph ⏸)
    error       a failed agent, run still going (clock ticks) -> working
                OR failed/stopped and lingering (clock frozen)  -> NOT working
    success     completed, lingering ~30 s before Claude drops it -> NOT working

tools/test_detect.py pins the text-only reading. This file pins the rest:
  1. parse_wf_row() on every layout Claude can draw (widths, Ghostty, screen
     reader, paused, size warning, selected) and on the lines it must reject;
  2. _wf_role() on every theme's bullet colours, as pyte actually reports them
     (truecolor hex, 256-colour hex, ANSI names);
  3. the whole path through a REAL pyte screen: ANSI bytes in, colours read by
     wf_bullet_colours() exactly as evaluate() does, detect_state() out;
  4. WorkflowWatch: a red row's frozen clock (at every clock resolution), and
     rows that vanish -- hidden by a menu or a dialog, or gone for good.

Run:  python3 tools/test_workflow_rows.py     (needs pyte: pip install pyte)
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


# --------------------------------------------------------------------------- #
# 1. parse_wf_row
# --------------------------------------------------------------------------- #
print("parse_wf_row: every layout Claude draws")
NAME = "deployment-risk-sweep-3802"
ROWS = [
    # (what, line, expected subset)
    ("captured, wide terminal",
     "  ◯ workflow-status-design-revi…  ▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱  0/5 · 1m01s · ↓ 491.6k tokens   ",
     {"name": "workflow-status-design-revi…", "done": 0, "total": 5, "pct": 0,
      "clock": "1m01s", "elapsed_s": 61, "tokens": 491600, "paused": False}),
    ("80 columns: done/total dropped first",
     f"  ◯ {NAME}  ▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱  2m39s · ↓ 542.8k tokens",
     {"done": None, "total": None, "pct": 40, "elapsed_s": 159, "tokens": 542800}),
    ("72 columns: the word 'tokens' goes",
     f"  ◯ {NAME}  ▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱  2m39s · ↓ 542.8k",
     {"elapsed_s": 159, "tokens": 542800, "pct": 0}),
    ("60 columns: a 12-cell bar and the count",
     f"  ◯ {NAME}  ▰▰▰▰▰▰▱▱▱▱▱▱  ↓ 542.8k", {"pct": 50, "clock": None, "tokens": 542800}),
    ("50 columns: the bar alone",
     f"  ◯ {NAME}  ▰▰▱▱▱▱▱▱", {"pct": 25, "tokens": None, "elapsed_s": None}),
    ("a name cut to the column",
     "  ◯ cross-repo-dependency-audit…  ▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱  3/12 · 6m40s · ↓ 250.0k tokens",
     {"name": "cross-repo-dependency-audit…", "done": 3, "total": 12, "pct": 25}),
    ("selected with the arrow keys",
     f"❯ ◯ {NAME}  ▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱  2/5 · 1m05s · ↓ 98.8k tokens",
     {"done": 2, "total": 5, "tokens": 98800}),
    ("Ghostty draws the bar in █░",
     f"  ◯ {NAME}  ████████░░░░░░░░░░░░  2/5 · 1m05s · ↓ 98.8k tokens", {"done": 2, "pct": 40}),
    ("a screen reader gets NN% instead of a bar",
     f"  ◯ {NAME}  40%  2/5 · 1m05s · ↓ 98.8k tokens", {"done": 2, "pct": 40}),
    ("the size warning carries its own ' · '",
     "  ◯ big-sweep  ▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱  6/30 · 4m12s · ↓ 1.2m tokens · ⚠ Large workflow · /workflows to stop",
     {"done": 6, "total": 30, "tokens": 1200000, "elapsed_s": 252}),
    ("an hour-old run's clock", f"  ◯ {NAME}  ▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱  17/40 · 1h02m · ↓ 12.3m tokens",
     {"elapsed_s": 3720, "tokens": 12300000, "clock": "1h02m"}),
    ("a day-old run's clock", f"  ◯ {NAME}  ▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱  17/40 · 1d03h · ↓ 12.3m tokens",
     {"elapsed_s": 97200, "clock": "1d03h"}),
    ("just launched: a clock and nothing else", f"  ◯ {NAME}  ▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱  0s",
     {"elapsed_s": 0, "done": None}),
    ("paused on a usage limit, reset time today",
     f"  ⏸ {NAME}  paused · usage limit resets at 3:30pm",
     {"paused": True, "note": "resets 3:30pm", "pct": None}),
    ("paused, reset on another day", f"  ⏸ {NAME}  paused · usage limit resets Oct 11, 3pm",
     {"paused": True, "note": "resets Oct 11, 3pm"}),
    ("paused, narrow", f"  ⏸ {NAME}  paused", {"paused": True, "note": ""}),
    ("paused under a plan-mode hint row", f"  ⏸ plan-sweep  paused · usage limit", {"paused": True, "name": "plan-sweep"}),
    ("a summary used as the name (spaces, no meta.name)",
     "  ◯ Sweep deployment risk  ▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱  1/9 · 45s · ↓ 4.5k tokens",
     {"name": "Sweep deployment risk", "done": 1, "total": 9}),
]
for what, line, want in ROWS:
    got = cmw.parse_wf_row(line)
    bad = ("not parsed" if got is None else
           ", ".join(f"{k}={got.get(k)!r} (want {v!r})" for k, v in want.items() if got.get(k) != v))
    check(what, not bad, bad)

print("\nparse_wf_row: lines that are NOT workflow rows")
NOT_ROWS = [
    ("an agent row", "  ◯ Explore  Count .h files in /usr/include            9s · ↓ 7.5k tokens"),
    ("an agent row whose task starts with a percentage",
     "  ◯ Explore  50% faster builds            12s · ↓ 7.5k tokens"),
    ("the main-agent header", "  ⏺ main"),
    ("the agents panel's overflow line", "  ↓ 3 more"),
    ("the hint row", "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"),
    ("the turn-end banner", "✻ Waiting for 1 dynamic workflow to finish"),
    ("the tool result", "  ⎿  Running in background · /workflows to monitor and save"),
    ("a markdown table row", "| sweep | ▰▰▱▱ | 2/5 |"),
    ("a bar followed by prose", f"  ◯ {NAME}  ▰▰▱▱▱▱▱▱  and then some words"),
    ("a bar followed by an unknown segment", f"  ◯ {NAME}  ▰▰▱▱▱▱▱▱  2/5 · soon"),
    ("indented one column too far", f"   ◯ {NAME}  ▰▰▱▱▱▱▱▱  2/5"),
]
for what, line in NOT_ROWS:
    got = cmw.parse_wf_row(line)
    check(what, got is None, f"parsed as {got}")


# --------------------------------------------------------------------------- #
# 2. _wf_role -- every theme, as pyte reports it
# --------------------------------------------------------------------------- #
print("\n_wf_role: the bullet colours of all six themes, through pyte")
THEMES = {   # Claude Code 2.1.295's theme objects (success, error, warning)
    "dark":             ((78, 186, 101), (255, 107, 128), (255, 193, 7)),
    "light":            ((44, 122, 57), (171, 43, 63), (150, 108, 30)),
    "dark-daltonized":  ((51, 153, 255), (255, 102, 102), (255, 204, 0)),
    "light-daltonized": ((0, 102, 153), (204, 0, 0), (255, 153, 0)),
}
ANSI_THEMES = {"dark-ansi": (92, 91, 93), "light-ansi": (32, 31, 33)}
ROLES = ("success", "error", "warning")


def to256(r, g, b):
    """chalk/ansi-styles rgbToAnsi256 (Apple Terminal is forced to 256 colours),
    with JavaScript's round-half-up."""
    rnd = lambda x: int(x + 0.5)            # noqa: E731
    if r == g == b:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return rnd(((r - 8) / 247) * 24) + 232
    return 16 + 36 * rnd(r / 255 * 5) + 6 * rnd(g / 255 * 5) + rnd(b / 255 * 5)


def fg_of(sgr):
    scr = pyte.Screen(8, 1)
    pyte.ByteStream(scr).feed(f"  {sgr}◯ x".encode())
    return scr.buffer[0][2].fg


for theme, cols in THEMES.items():
    for role, (r, g, b) in zip(ROLES, cols):
        tc = fg_of(f"\x1b[38;2;{r};{g};{b}m")
        c256 = fg_of(f"\x1b[38;5;{to256(r, g, b)}m")
        check(f"{theme} {role}: truecolor {tc}", cmw._wf_role(tc) == role, cmw._wf_role(tc))
        check(f"{theme} {role}: 256-colour {c256}", cmw._wf_role(c256) == role, cmw._wf_role(c256))
for theme, codes in ANSI_THEMES.items():
    for role, code in zip(ROLES, codes):
        fg = fg_of(f"\x1b[{code}m")
        check(f"{theme} {role}: ANSI {fg}", cmw._wf_role(fg) == role, cmw._wf_role(fg))
check("a dim (running) bullet has no colour", cmw._wf_role(fg_of("\x1b[2m")) == "")
check("grey is no colour", cmw._wf_role("808080") == "" and cmw._wf_role("brightblack") == "")
check("garbage is no colour", cmw._wf_role("zz") == "" and cmw._wf_role(None) == "")


# --------------------------------------------------------------------------- #
# 3. Through a real pyte screen, exactly as evaluate() reads it
# --------------------------------------------------------------------------- #
print("\ndetect_state through a real pyte screen (colours read like evaluate())")
DIM, GREEN, RED, AMBER = "\x1b[2m", "\x1b[38;2;78;186;101m", "\x1b[38;2;255;107;128m", \
    "\x1b[38;2;255;193;7m"
OFF = "\x1b[0m"
RULE = "─" * 60
HINT_IDLE = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ? for shortcuts"
HINT = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"


def row(colour, clock="2m39s", name=NAME, glyph="◯", tail=None):
    seg = tail if tail is not None else f"0/5 · {clock} · ↓ 542.8k tokens"
    body = f"  {colour}{glyph} {OFF}{DIM}{name}{OFF}  ▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱  {seg}"
    return body if glyph != "⏸" else f"  {colour}⏸ {OFF}{name}  {seg}"


def frame(rows, transcript=("⏺ Launched the sweep; I will report back.",), hint=HINT):
    lines = list(transcript) + [RULE, "❯ ", RULE, hint, ""] + list(rows)
    scr = pyte.Screen(110, len(lines) + 2)
    pyte.ByteStream(scr).feed("\r\n".join(lines).encode())
    display = scr.display
    return "\n".join(display), cmw.wf_bullet_colours(scr, display)


def state(rows, watch=None, now=0.0, **kw):
    text, fg = frame(rows, **kw)
    return cmw.detect_state(text, fg, watch, now)[0]


check("running (no colour) at an idle prompt -> working", state([row(DIM)]) == "working")
check("completed (green), lingering -> idle", state([row(GREEN)]) == "idle")
check("paused on a usage limit (amber ⏸) -> working",
      state([row(AMBER, glyph="⏸", tail="paused · usage limit resets at 3pm")]) == "working")
check("one finished and one running -> working", state([row(GREEN, name="first"), row(DIM)]) == "working")
# The other half of the bug fix: finished rows are taken out of the windows the
# idle gate and the meter read. Four of them push "? for shortcuts" out of the
# 4-line footer window; a spinner QUOTED in the conversation must stay a quote.
quoted = ("⏺ The meter looked like this:", "✻ Forming… (14s · ↓ 610 tokens · thinking with xhigh effort)",
          "⏺ That was an example.")
four_done = [row(GREEN, name=f"run-{i}") for i in range(4)]
check("four finished rows do not push out the idle gate (quoted spinner stays idle)",
      state(four_done, transcript=quoted, hint=HINT_IDLE) == "idle")
check("...and the same frame read as text alone is held on working (why colours matter)",
      cmw.detect_state(frame(four_done, transcript=quoted, hint=HINT_IDLE)[0])[0] == "working")

print("\nthe banner a STOPPED run leaves behind")
BANNER = ("⏺ Launched the sweep.", "✻ Waiting for 1 dynamic workflow to finish")
w = cmw.WorkflowWatch()
first = state([row(RED, clock="3m02s")], w, now=100.0, transcript=BANNER)
later = state([row(RED, clock="3m02s")], w, now=100.0 + cmw.WF_FROZEN_S + 1, transcript=BANNER)
gone = state([], w, now=200.0, transcript=BANNER)
check("banner + a red row whose clock stopped: working at first", first == "working")
check("...idle once the clock has been still past WF_FROZEN_S, banner or not", later == "idle", later)
check("...and still idle after the row is evicted (the banner alone is stale)", gone == "idle", gone)
check("a banner with NO workflow ever watched still holds working (unchanged)",
      state([], cmw.WorkflowWatch(), now=0.0, transcript=BANNER) == "working")
MIXED = ("⏺ Launched both.", "✻ Waiting for 1 background agent and 1 dynamic workflow to finish")
w = cmw.WorkflowWatch()
state([row(GREEN)], w, now=0.0, transcript=MIXED)
check("a banner that also names an AGENT keeps working after the workflow ends",
      state([], w, now=10.0, transcript=MIXED) == "working")

print("\nan error-coloured row: still going, or ended?")
w = cmw.WorkflowWatch()
ticking = [state([row(RED, clock=f"2m{s:02d}s")], w, now=float(t))
           for t, s in enumerate(range(10, 40))]
check("red with a TICKING clock (an agent failed, the run goes on) -> working throughout",
      set(ticking) == {"working"}, str(ticking))
w = cmw.WorkflowWatch()
early = state([row(RED, clock="3m02s")], w, now=100.0)
held = state([row(RED, clock="3m02s")], w, now=100.0 + cmw.WF_FROZEN_S - 1)
ended = state([row(RED, clock="3m02s")], w, now=100.0 + cmw.WF_FROZEN_S + 1)
check("red with a FROZEN clock: working at first", early == "working")
check("...still working just inside WF_FROZEN_S", held == "working")
check("...idle once frozen past WF_FROZEN_S (failed or stopped, lingering)", ended == "idle")


# --------------------------------------------------------------------------- #
# 4. WorkflowWatch -- clock resolution and vanishing rows
# --------------------------------------------------------------------------- #
print("\nWorkflowWatch")
R = lambda clock=None, paused=False: {"name": NAME, "paused": paused, "clock": clock}  # noqa: E731


def watch_run(steps):
    """steps: [(now, rows, box)] -> the last observe() result."""
    w = cmw.WorkflowWatch()
    out = None
    for now, rows, box in steps:
        out = w.observe(rows, box, now)
    return out


check("'1h02m' ticks once a minute: frozen 30 s is still running",
      watch_run([(0, [(R("1h02m"), "error")], True), (30, [(R("1h02m"), "error")], True)]) is True)
check("'1h02m' frozen past a minute has ended",
      watch_run([(0, [(R("1h02m"), "error")], True), (75, [(R("1h02m"), "error")], True)]) is False)
check("'1d03h' ticks once an hour: frozen 2 minutes is still running",
      watch_run([(0, [(R("1d03h"), "error")], True), (120, [(R("1d03h"), "error")], True)]) is True)
check("no clock on screen (narrow terminal): a red row stays live",
      watch_run([(0, [(R(), "error")], True), (600, [(R(), "error")], True)]) is True)
G = cmw.WorkflowWatch.GONE_S
live = [(R("2m39s"), "")]
check("rows hidden by a menu (box up, no rows): still working inside GONE_S",
      watch_run([(0, live, True), (G - 1, [], True)]) is True)
check("...and released once GONE_S has passed without a row",
      watch_run([(0, live, True), (G + 1, [], True)]) is False)
check("rows that showed their OUTCOME before going: released at once",
      watch_run([(0, live, True), (5, [(R("2m44s"), "success")], True), (6, [], True)]) is False)
check("behind a dialog (no prompt box) for ten minutes: still working",
      watch_run([(0, live, True), (600, [], False)]) is True)
check("...the countdown starts only once the prompt box is back",
      watch_run([(0, live, True), (600, [], False), (600 + G - 1, [], True)]) is True)
check("...and ends GONE_S after that",
      watch_run([(0, live, True), (600, [], False), (600 + G + 1, [], True)]) is False)
check("a dialog with no workflow seen before it: not working",
      watch_run([(0, [], False)]) is False)
# A terminal that drops colour leaves every bullet uncoloured. A run cannot end
# without its clock stopping, so a still clock settles it either way.
check("no colour, clock ticking: running",
      watch_run([(0, [(R("2m39s"), "")], True), (8, [(R("2m47s"), "")], True)]) is True)
full = lambda clock: {"name": NAME, "paused": False, "clock": clock, "done": 5, "total": 5}  # noqa: E731
w = cmw.WorkflowWatch()
w.observe([(full("3m02s"), "")], True, 0.0)
w.observe([(full("3m02s"), "")], True, cmw.WF_FROZEN_S + 1)
check("no colour, clock stopped at 5/5: ended as completed (D), not live",
      w.active is False and w.rows[0][1] == "D", str(w.rows))
w = cmw.WorkflowWatch()
w.observe([(dict(full("3m02s"), done=3), "")], True, 0.0)
w.observe([(dict(full("3m02s"), done=3), "")], True, cmw.WF_FROZEN_S + 1)
check("no colour, clock stopped at 3/5: ended (F), not live",
      w.active is False and w.rows[0][1] == "F", str(w.rows))
w = cmw.WorkflowWatch()
check("settled: nothing seen yet -> not settled", w.settled is False)
w.observe(live, True, 0.0)
check("settled: a live row on screen -> not settled", w.settled is False)
w.observe([(R("2m44s"), "success")], True, 5.0)
check("settled: the row turned green -> settled", w.settled is True)

# --------------------------------------------------------------------------- #
# 5. The footer around the rows, as Claude 2.1.295 really draws it
# --------------------------------------------------------------------------- #
# Claude draws dimmed text in the theme's "inactive" colour, not with SGR 2, so
# these frames do too: a running row's bullet and name are the same grey.
print("\nbare ⏸ lines: a paused run, or the mode indicator?")
GREY = "\x1b[38;2;153;153;153m"                # dark theme "inactive"
TEAL = "\x1b[38;2;72;150;140m"                 # dark theme "planMode"
for line in ("  ⏸ plan mode on (shift+tab to cycle) · ← for agents",
             "  ⏸ manual mode on (shift+tab to cycle)", f"  ⏸ {NAME}"):
    got = cmw.parse_wf_row(line)
    check(f"parses structurally, marked bare: {line.strip()[:40]}", bool(got and got["bare"]), str(got))
plan = f"  {TEAL}⏸ plan mode on (shift+tab to cycle){OFF}{GREY} · ← for agents{OFF}"
check("the plan-mode indicator (glyph and words one colour) is not a run",
      state([], hint=plan) == "idle")
check("...nor the manual-mode indicator in grey",
      state([], hint=f"  {GREY}⏸ manual mode on (shift+tab to cycle){OFF}") == "idle")
bare = f"  {AMBER}⏸ {OFF}{GREY}{NAME}{OFF}"
check("Claude's narrowest paused row (amber ⏸, grey name) is a paused run", state([bare]) == "working")
check("...the same line with no colour to go on is not trusted",
      cmw.detect_state(frame([f"  ⏸ {NAME}"])[0])[0] == "idle")

print("\na running row's colour is its NAME's colour, in any theme")
for theme, ink in (("built-in grey", GREY), ("Dracula comment", "\x1b[38;2;98;114;164m"),
                   ("Nord", "\x1b[38;2;76;86;106m"), ("Tokyo Night", "\x1b[38;2;86;95;137m")):
    run = f"  {ink}◯ {OFF}{ink}{NAME}{OFF}  {ink}{'▱' * 20}{OFF}  {ink}0/5 · 2m39s · ↓ 542.8k tokens{OFF}"
    done_ = f"  {GREEN}◯ {OFF}{ink}{NAME}{OFF}  {'▰' * 20}  {ink}5/5 · 3m02s · ↓ 611.4k tokens{OFF}"
    check(f"{theme}: running (bullet = name colour) -> working", state([run]) == "working", state([run]))
    check(f"{theme}: completed (green bullet) -> idle", state([done_]) == "idle", state([done_]))

print("\nrows hidden by a dialog, help, shell mode or a suggestion list")
LIVE = [f"  {GREY}◯ {OFF}{GREY}{NAME}{OFF}  {'▱' * 20}  0/5 · 2m39s · ↓ 542.8k tokens"]


def held(hidden_lines, seconds):
    """Rows live at t=0, then `hidden_lines` (a whole screen) for `seconds`."""
    w = cmw.WorkflowWatch()
    state(LIVE, w, now=0.0)
    out = []
    for t in range(1, seconds + 1):
        scr = pyte.Screen(110, len(hidden_lines) + 2)
        pyte.ByteStream(scr).feed("\r\n".join(hidden_lines).encode())
        d = scr.display
        out.append(cmw.detect_state("\n".join(d), cmw.wf_bullet_colours(scr, d), w, float(t))[0])
    return out


TRANS = ["⏺ Launched the sweep; I will report back."]
long_ = int(cmw.WorkflowWatch.GONE_S) * 4
DETAIL = TRANS + ["", "─" * 104, " deployment-risk-sweep-3802",
                  " Sweep deployment risk                          0/5 agents · 2m49s",
                  "", "❯ 1 Find      0/5", "",
                  " ↑↓ select · x stop workflow · p pause · esc back · s save"]
check("the workflow's own detail view (one rule, no prompt box): working throughout",
      set(held(DETAIL, long_)) == {"working"})
HELP = TRANS + [RULE, "❯ ", RULE, "  ! for shell mode         double tap esc to clear input",
                "  / for commands           shift+tab to auto-accept edits",
                "  @ for file paths         ctrl+o for verbose output"]
check("help ('?') in the panel's place: working throughout", set(held(HELP, long_)) == {"working"})
SHELL = TRANS + [RULE, "! git log --oneline -20 | grep", RULE, "  ! for shell mode"]
check("shell mode ('!'): working throughout", set(held(SHELL, long_)) == {"working"})
SLASH = TRANS + [RULE, "❯ /", RULE, "  /workflows      Show dynamic workflow runs",
                 "  /tasks          List and manage background tasks"]
check("the '/' suggestion list: working throughout", set(held(SLASH, long_)) == {"working"})
GONE = TRANS + [RULE, "❯ ", RULE, HINT]
after = held(GONE, int(cmw.WorkflowWatch.GONE_S) + 2)
check("the normal footer back with no row: released after GONE_S",
      after[0] == "working" and after[-1] == "idle", str(after))
w = cmw.WorkflowWatch()
state(LIVE, w, now=0.0)
for t in range(1, long_):
    state([], w, now=float(t))
check("a hold that ran out is NOT 'seen to end' (a banner it names stays true)", w.settled is False)

print("\nthe prompt box, told from a dialog")
for name_, lines, want in (
        ("a plain box", ["⏺ hi", RULE, "❯ ", RULE, HINT], True),
        ("a box whose top rule carries a label", ["⏺ hi", "─" * 60 + " ultracode ─", "❯\xa0", RULE, HINT], True),
        ("one rule (a dialog's header)", ["⏺ hi", RULE, " deployment-risk-sweep-3802", " 0/5 agents · 2m49s"], False),
        ("shell mode", ["⏺ hi", RULE, "! ls", RULE, "  ! for shell mode"], False),
        ("help in the panel's place", ["⏺ hi", RULE, "❯ ", RULE, "  / for commands    ctrl+z to suspend"], False),
        ("exit-confirm", ["⏺ hi", RULE, "❯ ", RULE, "  Press Ctrl-C again to exit"], False)):
    check(f"prompt_footer_up: {name_}", cmw.prompt_footer_up(lines) is want)
labelled = ["⏺ Anything else?", "─" * 60 + " ultracode ─", "❯\xa0why does the footer say ↓ 600.8k tokens?",
            RULE, HINT]
check("a labelled top rule keeps the line you type out of the transcript (idle)",
      cmw.detect_state("\n".join(labelled))[0] == "idle")

print("\nodd rows that must not throw")
check("a 300-digit token count is not a row (and no OverflowError)",
      cmw.parse_wf_row("  ◯ x  ▱  ↓ " + "9" * 297 + "t") is None)
w = cmw.WorkflowWatch()
same = [(dict(R("3m02s"), done=2, total=5), "error"), (dict(R("1m10s"), done=1, total=5), "error")]
w.observe(same, True, 0.0)
w.observe(same, True, cmw.WF_FROZEN_S + 1)
check("two red rows with the SAME name both settle once their clocks stop",
      [st for _r, st in w.rows] == ["F", "F"] and w.active is False, str(w.rows))

print("\n================", "ALL PASSED" if not fails else f"{fails} FAILED", "================")
sys.exit(1 if fails else 0)
