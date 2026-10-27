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
     rows that vanish -- hidden by a menu or a dialog, or gone for good;
  5. field 8, what the daemon is told: one record per row; with more than
     three, a run seen to end in the last ten seconds first, then the running
     ones, then the most recently ended -- at most three, and 300 bytes; the
     usage-limit note cut down to 14 characters; each run's clock estimated
     for NOW, on screen and while held out of view; a finished run listed for
     as long as Claude shows its row -- behind a dialog, through a half-drawn
     frame -- and '-' once it does not; reports that go out for a change of
     shape but not for a ticking clock; a failure anywhere in it costing the
     field ('') rather than the state report; and the caps it shares with
     the daemon, read from the daemon's own source. Beside it, field 9 -- the
     conversation, its directory, login and flags as one percent-encoded JSON
     object, each key worked out on its own, within its byte budget -- and
     nothing at all sent after `end`;
  6. the footer around the rows, as 2.1.295 draws it: bare ⏸ lines, a running
     row's colour in any theme, rows hidden by a dialog, help, shell mode or a
     suggestion list (live ones holding the session, finished ones still
     listed), and the prompt box told from a dialog however tall the prompt
     in it is.

Run:  python3 tools/test_workflow_rows.py     (needs pyte: pip install pyte)
"""
import os, re, sys, socket, shutil, tempfile, importlib.util, json, urllib.parse
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
     {"paused": True, "note": "til 3:30pm", "pct": None}),
    ("paused, reset on another day", f"  ⏸ {NAME}  paused · usage limit resets Oct 11, 3pm",
     {"paused": True, "note": "til Oct 11 3pm"}),
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
# 5. Field 8 -- what the daemon is told
# --------------------------------------------------------------------------- #
print("\nthe usage-limit note, cut down for the device (14 ASCII characters)")
NOTES = [
    # every paused form SPEC_v2 lists...
    ("paused · usage limit resets at 3:30pm", "til 3:30pm"),
    ("paused · usage limit resets at 3:30pm (Europe/Madrid)", "til 3:30pm"),
    ("paused · usage limit resets Oct 11, 3pm", "til Oct 11 3pm"),
    ("paused · usage limit", "usage limit"),
    ("paused", ""),
    # ...and what the same rule makes of the rest. Past 14, the "til" goes,
    ("paused · usage limit resets Oct 11, 3:30pm (Europe/Madrid)", "Oct 11 3:30pm"),
    ("paused · usage limit resets Sep 11, 11:30pm", "Sep 11 11:30pm"),
    # and only then the end of the text;
    ("paused · usage limit resets tomorrow at 11:30pm", "tomorrow at 11"),
    # a time zone the terminal cut short still goes;
    ("paused · usage limit resets at 3:30pm (Europe/Ma…", "til 3:30pm"),
    # the narrow no-break space Intl puts before am/pm is a space, in ASCII;
    ("paused · usage limit resets at 3:30\u202fpm", "til 3:30 pm"),
    # and "resets" with nothing after it says what it can.
    ("paused · usage limit resets", "usage limit"),
]
for text, want in NOTES:
    got = cmw.parse_wf_row(f"  ⏸ {NAME}  {text}")
    note = None if got is None else got["note"]
    check(f"{text!r} -> {want!r}", note == want, repr(note))
odd = [n for n in (cmw.parse_wf_row(f"  ⏸ {NAME}  {t}")["note"] for t, _w in NOTES)
       if len(n) > 14 or not n.isascii() or set(n) & set(";~|")]
check("every note is ASCII, at most 14 characters, and free of ; ~ |", not odd, str(odd))

print("\nfield 8: one record per row")
SHOT = f"  ◯ {NAME}  {'▱' * 20}  0/5 · 2m39s · ↓ 542.8k tokens"    # the screenshot's row


def seen(lines, now=0.0, box=True, watch=None):
    """[(line, role)] on screen in one frame -> (field 8, the watch)."""
    w = watch if watch is not None else cmw.WorkflowWatch()
    w.observe([(cmw.parse_wf_row(ln), role) for ln, role in lines], box, now)
    return cmw.wf_field(w, now), w


def recs(field):
    return [r.split(";", 8) for r in field.split("~")] if field not in ("", "-") else []


RECORDS = [
    ("the screenshot: running, 0/5, 2m39s, 542.8k tokens", [(SHOT, "")],
     f"R;0;5;;0;159;542800;;{NAME}"),
    ("paused on a usage limit: no bar, no clock, a note",
     [("  ⏸ nightly-triage  paused · usage limit resets at 3:30pm", "warning")],
     "P;;;;;;;til 3:30pm;nightly-triage"),
    ("completed (green), lingering",
     [("  ◯ nightly  ▰▰▰▰▰▰▰▰  5/5 · 3m02s · ↓ 1.2m tokens", "success")],
     "D;5;5;;100;182;1200000;;nightly"),
    ("running with a failed agent (red, clock ticking)",
     [("  ◯ sweep  ▰▰▱▱▱▱▱▱  2/9 · 12m05s · ↓ 98.8k tokens", "error")],
     "E;2;9;;22;725;98800;;sweep"),
    ("80 columns: no counts, so the bar's fraction",
     [(f"  ◯ {NAME}  ▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱  2m39s · ↓ 542.8k tokens", "")],
     f"R;;;;40;159;542800;;{NAME}"),
    ("50 columns: the bar alone", [(f"  ◯ {NAME}  ▰▰▱▱▱▱▱▱", "")], f"R;;;;25;;;;{NAME}"),
    ("a name cut to its column keeps its '…' (the daemon maps it)",
     [("  ◯ cross-repo-dependency-audit…  ▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱  3/12 · 6m40s · ↓ 250.0k tokens", "")],
     "R;3;12;;25;400;250000;;cross-repo-dependency-audit…"),
]
for what, lines, want in RECORDS:
    got = seen(lines)[0]
    check(what, got == want, f"{got!r} (want {want!r})")
w = cmw.WorkflowWatch()
RED = [("  ◯ sweep  ▰▰▱▱▱▱▱▱  2/9 · 12m05s · ↓ 98.8k tokens", "error")]
seen(RED, 0.0, watch=w)
got = seen(RED, cmw.WF_FROZEN_S + 1, watch=w)[0]
check("red, clock stopped: F, with the clock where it stopped",
      got == "F;2;9;;22;725;98800;;sweep", got)

print("\nfield 8: which rows, when there are more than three")
ROLE_OF = {"R": "", "E": "error", "P": "warning", "D": "success", "F": "error"}


def watch_of(spec, t0=1000.0):
    """[(name, status)] -> a WorkflowWatch that has watched exactly those rows,
    in that order. F is a red row whose clock stood still past WF_FROZEN_S;
    the others show no clock, so their colour alone decides."""
    def lines():
        out = []
        for name, st in spec:
            if st == "P":
                ln = f"  ⏸ {name}  paused · usage limit"
            elif st == "F":
                ln = f"  ◯ {name}  ▰▰▱▱▱▱▱▱  1/4 · 3m02s · ↓ 4.0k tokens"
            else:
                ln = f"  ◯ {name}  ▰▰▱▱▱▱▱▱  1/4"
            out.append((ln, ROLE_OF[st]))
        return out
    w = cmw.WorkflowWatch()
    seen(lines(), t0, watch=w)
    seen(lines(), t0 + cmw.WF_FROZEN_S + 1, watch=w)
    return w


SELECT = [
    ("the three oldest finished, the fourth running: the running one goes out",
     [("old-a", "D"), ("old-b", "F"), ("old-c", "D"), ("live", "R"), ("old-d", "D")],
     [("old-a", "D"), ("old-b", "F"), ("live", "R")]),
    ("four in flight: the first three, in the panel's order",
     [("a", "R"), ("b", "E"), ("c", "P"), ("d", "R")],
     [("a", "R"), ("b", "E"), ("c", "P")]),
    ("running ones before finished ones, then sent in the panel's order",
     [("a", "D"), ("b", "R"), ("c", "D"), ("d", "R"), ("e", "D")],
     [("a", "D"), ("b", "R"), ("d", "R")]),
    # c was read red and ticking (E), then still (F): an end the watch SAW,
    # just now -- the daemon shows that run as ended for ten seconds, so it
    # has to be on the wire to be shown at all.
    ("...but a run seen to end just now before them all",
     [("a", "D"), ("b", "R"), ("c", "F"), ("d", "R"), ("e", "D")],
     [("b", "R"), ("c", "F"), ("d", "R")]),
    ("two rows: both, in order", [("a", "R"), ("b", "D")], [("a", "R"), ("b", "D")]),
]
for what, spec, want in SELECT:
    w = watch_of(spec)
    statuses = [st for _r, st in w.rows]
    got = [(r[8], r[0]) for r in recs(cmw.wf_field(w, 1000.0 + cmw.WF_FROZEN_S + 1))]
    check(what, statuses == [st for _n, st in spec] and got == want,
          f"watched {statuses}, sent {got}")
w = watch_of([("old-a", "D"), ("old-b", "F"), ("old-c", "D"), ("live", "R"), ("old-d", "D")])
check("...and the session's background tell counts every row, sent or not", w.active is True)
FIRST = [(f"  ◯ old-{c}  ▰▰▰▰▰▰▰▰  4/4", "success") for c in "abc"] + [("  ◯ live  ▰▰▱▱▱▱▱▱  1/4", "")]
got = [(r[8], r[0]) for r in recs(seen(FIRST)[0])]
check("rows read already finished make no claim to have just ended (nobody saw them end): "
      "the running one still goes out", got == [("old-a", "D"), ("old-b", "D"), ("live", "R")],
      str(got))

print("\nfield 8: four rows, two runs ending one after the other")
FOUR = (("audit-a", 190), ("build-b", 180), ("check-c", 120), ("docs-d", 60))
ENDS = {"audit-a": 1.0, "build-b": 20.0}


def four(t):
    """The panel at t: four runs, started one after another, their clocks
    ticking; audit-a completes at t=1 and build-b at t=20, and Claude drops a
    finished row 30 s after it ended."""
    out = []
    for name, base in FOUR:
        end = ENDS.get(name)
        if end is not None and t >= end + 30:
            continue
        over = end is not None and t >= end
        secs = int(base + (end if over else t))
        out.append((f"  ◯ {name}  {'▰' * 8 if over else '▰▰▱▱▱▱▱▱'}  {5 if over else 2}/5 · "
                    f"{secs // 60}m{secs % 60:02d}s · ↓ 12.0k tokens", "success" if over else ""))
    return out


w = cmw.WorkflowWatch()
wire = []                                       # [(t, [(name, st)] sent)]
for t, half_drawn in ((0.0, False), (1.0, False), (1.25, True), (1.5, False), (5.0, False),
                      (11.5, False), (20.0, False), (25.0, False), (31.0, False), (40.0, False)):
    wire.append((t, [(r[8], r[0]) for r in recs(seen([] if half_drawn else four(t), t, watch=w)[0])]))
on = dict(wire)
check("four running: the first three", on[0.0] == [("audit-a", "R"), ("build-b", "R"), ("check-c", "R")],
      str(on[0.0]))
check("audit-a completes beside three running runs: it goes out ENDED, at its own time "
      "(left off, it was never shown as done -- its record just went away)",
      on[1.0] == [("audit-a", "D"), ("build-b", "R"), ("check-c", "R")], str(on[1.0]))
check("...through a half-drawn frame and after it (a blink is not a new end)",
      on[1.25] == on[1.5] == on[5.0] == [("audit-a", "D"), ("build-b", "R"), ("check-c", "R")],
      f"{on[1.25]} / {on[1.5]} / {on[5.0]}")
check("...and gives its place back to the running ones once ten seconds have passed",
      on[11.5] == [("build-b", "R"), ("check-c", "R"), ("docs-d", "R")], str(on[11.5]))
check("build-b completes: it goes out ended in turn -- not audit-a again, ended 19 s ago "
      "(which the daemon took for a run that had just ended, and showed as one)",
      on[20.0] == on[25.0] == [("build-b", "D"), ("check-c", "R"), ("docs-d", "R")], str(on[20.0]))
check("Claude drops audit-a: the rest as they were",
      on[31.0] == on[40.0] == [("build-b", "D"), ("check-c", "R"), ("docs-d", "R")], str(on[31.0]))
left, back, before = set(), [], set()
for t, sent in wire:
    names = {n for n, _st in sent}
    back += [(t, n) for n in sorted(names & left)]
    left |= before - names
    before = names
check("no run comes back to the wire after leaving it (each comeback was a new run to the "
      "daemon: a new linger, and a spotlight for an end long past)", not back, str(back))

RUN, OVER = "  ◯ {}  ▰▰▱▱▱▱▱▱  1/4", "  ◯ {}  ▰▰▰▰▰▰▰▰  4/4"


def xyzv(t):
    """x completes at t=1, y at t=3; z and v run on."""
    return [(OVER.format("x"), "success") if t >= 1 else (RUN.format("x"), ""),
            (OVER.format("y"), "success") if t >= 3 else (RUN.format("y"), ""),
            (RUN.format("z"), ""), (RUN.format("v"), "")]


w = cmw.WorkflowWatch()
for t in (0.0, 1.0, 3.0):
    seen(xyzv(t), t, watch=w)
got = [(r[8], r[0]) for r in recs(seen(xyzv(20.0), 20.0, watch=w)[0])]
check("past those ten seconds, the running ones, then the most recently ended: y (ended at "
      "t=3) before x (at t=1)", got == [("y", "D"), ("z", "R"), ("v", "R")], str(got))

print("\nfield 8: at most 300 bytes, whatever the names are written in")


def long_row(name, role=""):
    return (f"  ◯ {name}  ▰▰▰▱▱▱▱▱▱▱  12/40 · 1h02m · ↓ 12.3m tokens", role)


for label, ch in (("Cyrillic, 2 bytes a letter", "Д"), ("CJK, 3 bytes", "界"),
                  ("emoji, 4 bytes", "🙂")):
    names = [ch * 39 + c for c in "abc"]
    field = seen([long_row(n) for n in names])[0]
    rs = recs(field)
    size = len(field.split("~")[0].encode())      # every record is this long
    fit = min(3, max(k for k in (1, 2, 3) if k * size + k - 1 <= 300))
    check(f"{label}: {len(rs)} whole record(s), {len(field.encode())} bytes",
          len(field.encode()) <= 300 and len(rs) == fit
          and all(len(r) == 9 for r in rs) and [r[8] for r in rs] == names[:fit],
          f"{len(rs)} records (want {fit}), {len(field.encode())} bytes")
field = seen([("  ◯ done-first  ▰▰▰▰▰▰▰▰  4/4 · 9m00s · ↓ 1.0k tokens", "success")]
             + [long_row("界" * 39 + c) for c in "ab"])[0]
check("when the cap bites, the finished record goes before a running one",
      [r[0] for r in recs(field)] == ["R", "R"], field)
w = cmw.WorkflowWatch()
seen([("  ◯ done-first  ▰▰▰▰▱▱▱▱  2/4 · 8m59s · ↓ 1.0k tokens", "")]
     + [long_row("界" * 39 + c) for c in "ab"], 0.0, watch=w)
field = seen([("  ◯ done-first  ▰▰▰▰▰▰▰▰  4/4 · 9m00s · ↓ 1.0k tokens", "success")]
             + [long_row("界" * 39 + c) for c in "ab"], 1.0, watch=w)[0]
check("...unless it was just seen to end: that one goes out, ahead of a running one",
      [(r[0], r[8][:1]) for r in recs(field)] == [("D", "d"), ("R", "界")], field)
w = cmw.WorkflowWatch()
w.observe([(dict(cmw.parse_wf_row(SHOT), tokens=10 ** 300), "")], True, 0.0)
check("one record too big for the cap on its own: '' (keep), never half a record",
      cmw.wf_field(w, 0.0) == "")

print("\nfield 8: names as the wire carries them")
w = cmw.WorkflowWatch()
w.observe([(dict(cmw.parse_wf_row(SHOT), name="a|b~c\x07d;e\u2028f\x9bg"), ""),
           (dict(cmw.parse_wf_row(SHOT), name="x" * 60), "")], True, 0.0)
rs = recs(cmw.wf_field(w, 0.0))
check("'|' -> '/', '~' -> '-', control characters and line separators dropped, "
      "';' kept (the name is the last sub-field)",
      len(rs) == 2 and rs[0][8] == "a/b-cd;efg", str(rs))
check("a name is cut to 40 characters", len(rs) == 2 and rs[1][8] == "x" * 40, str(rs))

print("\nfield 8: each run's clock, estimated for NOW")


def clocks(frames):
    """[(now, clock text, or None while the rows are hidden; Claude's normal
    prompt footer on screen?)] -> the elapsed_s field 8 carries after each
    frame. A menu or a dialog hides the rows AND the normal footer (False)."""
    w = cmw.WorkflowWatch()
    out = []
    for now, clock, box in frames:
        lines = ([(f"  ◯ {NAME}  {'▱' * 20}  0/5 · {clock} · ↓ 542.8k tokens", "")]
                 if clock else [])
        f = seen(lines, now, box, watch=w)[0]
        out.append(f.split(";")[5] if f not in ("", "-") else f)
    return out


CLOCKS = [
    ("seconds: '2m39s' is 159 s and counting -- by at most one tick while its "
     "text stands", [(100, "2m39s", True), (100.6, "2m39s", True), (101.2, "2m39s", True),
                     (104, "2m39s", True), (104.5, "2m44s", True)],
     ["159", "159", "160", "160", "164"]),
    ("under a minute: '45s'", [(0, "45s", True), (0.5, "45s", True), (1.25, "45s", True)],
     ["45", "45", "46"]),
    ("minutes: '1h02m' climbs to 3780 over its minute, and no further",
     [(0, "1h02m", True), (30, "1h02m", True), (59.5, "1h02m", True), (65, "1h02m", True),
      (66, "1h03m", True)],
     ["3720", "3750", "3779", "3780", "3780"]),
    ("days: '1d03h' climbs over its hour",
     [(0, "1d03h", True), (1800, "1d03h", True), (3600, "1d03h", True), (3605, "1d03h", True)],
     ["97200", "99000", "100800", "100800"]),
    ("held out of view (a menu, then a dialog): the clock keeps going",
     [(0, "2m39s", True), (0.8, "2m39s", True), (10.8, None, False), (40.8, None, False),
      (300.3, None, False)],
     ["159", "159", "169", "199", "459"]),
    ("...and is read off the screen again when the rows come back",
     [(0, "2m39s", True), (30, None, False), (31, "3m10s", True)],
     ["159", "189", "190"]),
]
for what, frames, want in CLOCKS:
    got = clocks(frames)
    check(what, got == want, f"{got} (want {want})")
w = cmw.WorkflowWatch()
a = seen([("  ◯ nightly  ▰▰▰▰▰▰▰▰  5/5 · 3m02s · ↓ 1.2m tokens", "success"), (SHOT, "")],
         0.0, watch=w)[0]
b = seen([], 30.0, box=False, watch=w)[0]       # hidden by a menu: held
check("a finished row's clock is taken as it reads, held or not; a running one's moves",
      [r[5] for r in recs(a)] == ["182", "159"] and [r[5] for r in recs(b)] == ["182", "189"],
      f"{a!r} / {b!r}")
check("...and holding the rows is no reason to report: same shape", cmw.wf_shape(a) == cmw.wf_shape(b))

print("\nfield 8: '-' when there is nothing to show")
check("before any frame -- the launch report -- it says '-'", cmw.g_meta["wf"] == "-")
check("a watch that has seen nothing: '-'", cmw.wf_field(cmw.WorkflowWatch(), 0.0) == "-")
w = cmw.WorkflowWatch()
check("frames with a prompt box and no rows: '-'",
      seen([], 0.0, watch=w)[0] == "-" and seen([], 5.0, watch=w)[0] == "-")
check("...and behind a dialog: '-'", seen([], 6.0, box=False, watch=w)[0] == "-")
K = cmw.WF_REDRAW_S
w = cmw.WorkflowWatch()
seen([(SHOT, "")], 0.0, watch=w)
seen([(SHOT.replace("2m39s", "2m44s"), "success")], 5.0, watch=w)
check("rows that showed their outcome, then went: '-' once WF_REDRAW_S has passed",
      seen([], 5.0 + K + 0.1, watch=w)[0] == "-")
w = cmw.WorkflowWatch()
seen([(SHOT, "")], 0.0, watch=w)
check("running rows hidden with the prompt box up: held inside GONE_S",
      seen([], G - 1, watch=w)[0].startswith("R;"))
check("...'-' once GONE_S has passed without them", seen([], G + 1, watch=w)[0] == "-")
check("...after which a row is a record again", seen([(SHOT, "")], G + 2, watch=w)[0].startswith("R;"))

print("\nfield 8: a finished run stays listed for as long as Claude shows its row")
# The daemon keeps a finished record visible for a minute from the line that
# first listed it ended, and hidden after that for as long as lines still list
# it. A '-' in between, then the row listed again, is a NEW finished run to it:
# its minute and its ten-second spotlight start over, and the device's band
# blinks off and back on.
NIGHTLY = [("  ◯ nightly  ▰▰▰▰▰▰▰▰  5/5 · 3m02s · ↓ 1.2m tokens", "success")]
NIGHTLY_REC = "D;5;5;;100;182;1200000;;nightly"
w = cmw.WorkflowWatch()
seen(NIGHTLY, 0.0, watch=w)
got = [seen([], t, box=False, watch=w)[0] for t in (1.0, 30.0, 300.0)]
check("behind a dialog, help, shell mode or a suggestion list: still listed, however long it "
      "lasts (the daemon's minute decides how long it shows)", got == [NIGHTLY_REC] * 3, str(got))
check("...and the session's verdict is untouched: not working, settled",
      w.active is False and w.settled is True, f"active={w.active} settled={w.settled}")
got = [seen([], t, watch=w)[0] for t in (300.0 + K / 2, 300.0 + K + 0.1)]
check("...until the footer is back without it: '-' once WF_REDRAW_S has passed",
      got == [NIGHTLY_REC, "-"], str(got))
w = cmw.WorkflowWatch()
seen(NIGHTLY, 0.0, watch=w)
got = [seen(lines, t, watch=w)[0] for t, lines in
       ((0.25, []), (0.5, NIGHTLY), (0.75, []), (1.0, []), (1.25, NIGHTLY))]
check("frames caught mid-redraw (the footer drawn, the rows not yet): listed throughout -- "
      "never a '-' between two frames that show it", got == [NIGHTLY_REC] * 5, str(got))
got = [seen([], t, watch=w)[0] for t in (1.25 + K - 0.1, 1.25 + K + 0.1)]
check("...and '-' once no row has been drawn for WF_REDRAW_S: Claude has dropped it",
      got == [NIGHTLY_REC, "-"], str(got))
w = cmw.WorkflowWatch()
RED_ROW = [("  ◯ sweep  ▰▰▱▱▱▱▱▱  2/9 · 12m05s · ↓ 98.8k tokens", "error")]
T = cmw.WF_FROZEN_S + 1
seen(RED_ROW, 0.0, watch=w)
got = [seen(lines, t, watch=w)[0][:1] for t, lines in ((T, RED_ROW), (T + 0.25, []), (T + 0.5, RED_ROW))]
check("a red row read as ended (F) is still F after a half-drawn frame -- not E again, running "
      "and holding the session, until its clock had stood still another WF_FROZEN_S",
      got == ["F", "F", "F"] and w.active is False, f"{got}, active={w.active}")

print("\nfield 8: two runs with one name are two rows")
w = cmw.WorkflowWatch()
for t in (0.0, 6.0, 12.0):
    seen([("  ◯ sweep  ▰▰▱▱▱▱▱▱  2/9 · 12m05s · ↓ 98.8k tokens", "error"),
          (f"  ◯ sweep  ▱▱▱▱▱▱▱▱  0/9 · {int(t)}s · ↓ 1.0k tokens", "")], t, watch=w)
rs = recs(cmw.wf_field(w, 12.0))
check("the old run's stopped red clock reads F, the relaunch's ticking one R "
      "(one clock per name had them resetting each other)",
      [r[0] for r in rs] == ["F", "R"], str(rs))
check("...each with its own clock", [r[5] for r in rs] == ["725", "12"], str(rs))

print("\nfield 8: what is a reason to report, and what rides the keepalive")
A = "R;2;5;;40;159;542800;;sweep"
check("the clock and the tokens are not part of the shape",
      cmw.wf_shape(A) == cmw.wf_shape("R;2;5;;40;999;999999;;sweep"))
for what, other in (("the status", "E;2;5;;40;159;542800;;sweep"),
                    ("done", "R;3;5;;60;159;542800;;sweep"),
                    ("total", "R;2;6;;33;159;542800;;sweep"),
                    ("pct", "R;2;5;;45;159;542800;;sweep"),
                    ("the note", "R;2;5;;40;159;542800;til 3pm;sweep"),
                    ("the name", "R;2;5;;40;159;542800;;sweep-2"),
                    ("the number of records", A + "~D;5;5;;100;182;1;;other"),
                    ("a clear", "-")):
    check(f"a change of {what} is", cmw.wf_shape(A) != cmw.wf_shape(other))
check("a ';' in a name stays in the name", cmw.wf_shape("R;;;;;;;;a;b") == (("R", "", "", "", "", "", "a;b"),))
w = cmw.WorkflowWatch()
shapes, last = set(), ""
for k in range(240):                            # a minute of 0.25 s monitor passes
    now, secs = 100.0 + k * 0.25, 159 + int(k * 0.25)
    last = seen([(SHOT.replace("2m39s", f"{secs // 60}m{secs % 60:02d}s"), "")], now, watch=w)[0]
    shapes.add(cmw.wf_shape(last))
check("a minute of a ticking clock is ONE shape: reported by the keepalive alone",
      len(shapes) == 1, f"{len(shapes)} shapes, e.g. {sorted(shapes)[:2]}")
check("...and the field keeps that clock current for the keepalive",
      last.split(";")[5] == str(159 + 59), last)

print("\nfenced: a failure costs the workflow field, never the state report")
STATES = ("working", "waiting", "error", "idle")
text, fg = frame([row(DIM)])
w = cmw.WorkflowWatch()
cmw.detect_state(text, fg, w, 0.0)
check("(a running row on screen reads as a record)", cmw.wf_field(w, 0.0).startswith("R;"))
real_parse = cmw.parse_wf_row
cmw.parse_wf_row = lambda line: 1 / 0
try:
    got = cmw.detect_state(text, fg, w, 1.0)
finally:
    cmw.parse_wf_row = real_parse
check("the row parser raising: detect_state still answers", got[0] in STATES, str(got))
check("...and field 8 says unknown -- ('', None), keep and no reason to report -- "
      "not the last frame's rows", cmw.wf_report(w, 1.0) == ("", None), cmw.wf_report(w, 1.0))
cmw.detect_state(text, fg, w, 2.0)
check("...until the next frame reads", cmw.wf_field(w, 2.0).startswith("R;"))
real_status = cmw.WorkflowWatch._status
cmw.WorkflowWatch._status = lambda self, *a: 1 / 0
try:
    got = cmw.detect_state(text, fg, w, 3.0)
finally:
    cmw.WorkflowWatch._status = real_status
check("the watch raising mid-frame: still a state, and ''",
      got[0] in STATES and cmw.wf_field(w, 3.0) == "", str(got))
w = cmw.WorkflowWatch()
w.rows = [("not a row", "R")]
check("a record that cannot be built: ('', None)", cmw.wf_report(w, 0.0) == ("", None))

print("\nthe report line itself (send_daemon)")
tmpd = tempfile.mkdtemp(prefix="cmw-wire-")
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(os.path.join(tmpd, "d.sock"))
srv.listen(8)
srv.settimeout(3.0)
saved = (cmw.DAEMON_SOCK, cmw.NAME, dict(cmw.g_meta), os.environ.get("CLAUDE_CONFIG_DIR"))
cmw.DAEMON_SOCK = os.path.join(tmpd, "d.sock")     # never the real daemon's socket


def report(state="working", **meta):
    """send_daemon(state) with g_meta updated by `meta` -> the bytes received."""
    cmw.g_meta.update(meta)
    cmw.send_daemon(state)
    try:
        conn, _ = srv.accept()
    except OSError:
        return b""
    data = b""
    with conn:
        conn.settimeout(3.0)
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
    return data


try:
    cmw.NAME = "my|proj\r\nx"
    field = seen([long_row("界" * 39 + c) for c in "ab"])[0]       # ~300 bytes of it
    raw = report(model="Opus|5.5", effort="xhigh", account="work", limit="5h82%", wf=field)
    parts = raw.decode().rstrip("\n").split("|")
    check("one line, ended by its only LF, no CR", raw.endswith(b"\n")
          and raw.count(b"\n") == 1 and b"\r" not in raw, repr(raw[:60]))
    check("always ten fields", len(parts) == 10, str(len(parts)))
    check("NAME is sanitised like the rest ('|' -> '/', CR/LF gone)",
          len(parts) == 10 and parts[2] == "my/projx", parts[2:3])
    check("...as is the model", len(parts) == 10 and parts[4] == "Opus/5.5", parts[4:5])
    check("field 8 is the workflow field, as built", len(parts) == 10 and parts[8] == field)
    parts = report(wf="-").decode().rstrip("\n").split("|")
    check("'-' goes out as '-'", len(parts) == 10 and parts[8] == "-", parts[8:9])
    parts = report(wf=None).decode().rstrip("\n").split("|")
    check("a field that is not text: '' -- and the state report still goes out",
          len(parts) == 10 and parts[0] == "working" and parts[8] == "", parts)
    cmw.NAME = "Д" * 20
    raw = report(account="claudealaddin2", wf=field)
    head = "|".join(raw.decode().rstrip("\n").split("|")[:9]).encode()
    # Fields 0-8 only: field 9 holds a path and flags as long as the machine
    # makes them, and the daemon reads a line to its newline.
    check(f"fields 0-8 of a full line with non-ASCII names fit one recv(512) "
          f"({len(head)} bytes)", 0 < len(head) < 512)

    def f9(**meta):
        """Field 9 of a report, decoded."""
        line = report(**meta).decode().rstrip("\n").split("|")
        return json.loads(urllib.parse.unquote(line[9])) if len(line) == 10 and line[9] else {}

    print("\nfield 9: the conversation, its directory, login and flags")
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    got = f9()
    check("one percent-encoded JSON object: where it runs, '' for the default login",
          got == {"cwd": os.getcwd(), "config": ""}, str(got))
    check("...no id until the wrapper has seen the conversation's transcript, no flags "
          "while they are unknown", "id" not in got and "flags" not in got, str(got))
    cmw.g_conv = "3f2a9c1e-7b4d-4c1a-9e8f-0123456789ab"
    cmw.g_resume_flags = ["--model", "opus", "--name", "a|b;c"]
    got = f9()
    check("...then both, as they are -- never _field()ed: a '|' stays a '|'",
          got.get("id") == cmw.g_conv and got.get("flags") == cmw.g_resume_flags, str(got))
    real_getcwd = os.getcwd
    os.getcwd = lambda: "/tmp/we|ird\nname \udce9"       # '|', a newline, a byte not UTF-8
    try:
        raw = report()
        got = f9()
    finally:
        os.getcwd = real_getcwd
    check("a directory with '|', a newline and a non-UTF-8 byte arrives whole, "
          "on one line of ten fields",
          got.get("cwd") == "/tmp/we|ird\nname \udce9" and raw.count(b"\n") == 1
          and len(raw.decode().split("|")) == 10, str(got))
    os.environ["CLAUDE_CONFIG_DIR"] = "/Users/me/.claude-accounts/work 2/"
    check("an absolute CLAUDE_CONFIG_DIR goes as the exact string (claude hashes the "
          "raw value into its Keychain entry)",
          f9().get("config") == "/Users/me/.claude-accounts/work 2/")
    os.environ["CLAUDE_CONFIG_DIR"] = "rel/prof"
    check("...a relative one, made absolute",
          f9().get("config") == os.path.abspath("rel/prof"))
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    cmw.g_resume_flags = ["--name", "x" * 5000]
    got = f9()
    check(f"over {cmw.HIST_FIELD_MAX} bytes the flags go as NONE, the rest goes "
          "(a missing key would let a record keep an older session's bypass)",
          got.get("flags") == [] and got.get("id") == cmw.g_conv, str(got)[:80])
    os.getcwd = lambda: "/" + "d" * 5000
    try:
        parts = report().decode().rstrip("\n").split("|")
    finally:
        os.getcwd = real_getcwd
    check("...and still over without them, field 9 goes out empty -- the state "
          "report regardless", len(parts) == 10 and parts[9] == "" and parts[0] == "working")
    cmw.g_resume_flags = 42                              # not a list: that key fails
    os.getcwd = lambda: 1 / 0                            # ...and so does this one
    try:
        parts = report().decode().rstrip("\n").split("|")
    except Exception as exc:                             # send_daemon raised: say so
        parts = [repr(exc)]
    finally:
        os.getcwd = real_getcwd
    got = json.loads(urllib.parse.unquote(parts[9])) if len(parts) == 10 and parts[9] else None
    check("an input that fails costs its own key, never the field or the report",
          got == {"config": "", "id": cmw.g_conv} and parts[0] == "working", str(parts))

    print("\nnothing after 'end'")
    cmw.g_resume_flags = []
    end = report("end").decode().rstrip("\n").split("|")
    check("'end' goes out, field 9 and all", end[0] == "end" and len(end) == 10
          and bool(end[9]))
    srv.settimeout(0.5)
    check("a report after it does not: a late keepalive would bring the session "
          "back as a ghost", report("working") == b"" and report("idle") == b"")
finally:
    cmw.DAEMON_SOCK, cmw.NAME = saved[0], saved[1]
    cmw.g_meta.clear()
    cmw.g_meta.update(saved[2])
    cmw.g_conv, cmw.g_resume_flags, cmw.g_ended = "", None, False
    if saved[3] is None:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
    else:
        os.environ["CLAUDE_CONFIG_DIR"] = saved[3]
    srv.close()
    shutil.rmtree(tmpd, ignore_errors=True)

# --------------------------------------------------------------------------- #
# 6. The footer around the rows, as Claude 2.1.295 really draws it
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

print("\na finished row out of view: still listed, the session still idle")
DONE_ROW = [f"  {GREEN}◯ {OFF}{GREY}{NAME}{OFF}  {'▰' * 20}  5/5 · 3m02s · ↓ 611.4k tokens"]


def listed(hidden_lines, seconds):
    """A finished row on screen at t=0, then `hidden_lines` (a whole screen)
    once a second: [(state, field 8)]."""
    w = cmw.WorkflowWatch()
    state(DONE_ROW, w, now=0.0)
    out = []
    for t in range(1, seconds + 1):
        scr = pyte.Screen(110, len(hidden_lines) + 2)
        pyte.ByteStream(scr).feed("\r\n".join(hidden_lines).encode())
        d = scr.display
        out.append((cmw.detect_state("\n".join(d), cmw.wf_bullet_colours(scr, d), w, float(t))[0],
                    cmw.wf_field(w, float(t))))
    return out


for what, hidden in (("the workflow's detail view", DETAIL), ("help", HELP),
                     ("shell mode", SHELL), ("the '/' list", SLASH)):
    got = listed(hidden, 5)
    check(f"{what}: still listed (D), the session idle",
          all(st == "idle" and f.startswith("D;") for st, f in got), str(got[:2]))
got = listed(GONE, 3)
check("the normal footer back with no row: listed for WF_REDRAW_S, then '-'; idle throughout",
      [f.startswith("D;") for _st, f in got] == [t < cmw.WF_REDRAW_S for t in (1, 2, 3)]
      and got[-1][1] == "-" and {st for st, _f in got} == {"idle"}, str(got))

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

print("\na prompt taller than PROMPT_BOX_MAX_LINES")
# A long prompt wraps, and the box grows with it: twelve lines here. Read as
# "footer away", the rows under it were held, never read -- a run that
# completed there went on being reported as running, its clock ticking, for
# as long as the prompt stayed that long.
TALL = (["❯ go through each of these and tell me which ones changed since the last sweep"]
        + [f"  item {k}: a service whose risk score the sweep tracks" for k in range(2, 13)])


def tall_frame(rows, typed=TALL):
    lines = list(TRANS) + [RULE] + list(typed) + [RULE, HINT, ""] + list(rows)
    scr = pyte.Screen(110, len(lines) + 2)
    pyte.ByteStream(scr).feed("\r\n".join(lines).encode())
    display = scr.display
    return "\n".join(display), cmw.wf_bullet_colours(scr, display)


check(f"prompt_footer_up: a box {len(TALL)} lines tall is the box",
      cmw.prompt_footer_up(["⏺ hi", RULE] + TALL + [RULE, HINT]) is True)
check("prompt_footer_up: ...in shell mode, however tall, the rows are hidden",
      cmw.prompt_footer_up(["⏺ hi", RULE, "! for f in *.log; do"] + TALL[1:]
                           + [RULE, "  ! for shell mode"]) is False)
check("prompt_footer_up: two rules far apart with no input line under the top one are no box",
      cmw.prompt_footer_up(["⏺ hi", RULE, " Bash command"] + TALL[1:] + [RULE, HINT]) is False)
w = cmw.WorkflowWatch()
got = []
for t, colour, clock in ((0.0, DIM, "2m39s"), (1.0, DIM, "2m40s"), (2.0, GREEN, "2m41s"),
                         (30.0, GREEN, "2m41s")):
    text, fg = tall_frame([row(colour, clock=clock)])
    got.append((cmw.detect_state(text, fg, w, t)[0], cmw.wf_field(w, t)[:1]))
check(f"a {len(TALL)}-line prompt: the row under it that turns green reads D, the session idle "
      f"-- not a run still going, its clock ticking on",
      got == [("working", "R"), ("working", "R"), ("idle", "D"), ("idle", "D")], str(got))
w = cmw.WorkflowWatch()
text, fg = tall_frame([row(DIM)])
cmw.detect_state(text, fg, w, 0.0)
text, fg = tall_frame([])
got = [cmw.detect_state(text, fg, w, float(t))[0] for t in (1, G - 1, G + 1)]
check("...and with no row under it the countdown runs, as under any box: released after GONE_S",
      got == ["working", "working", "idle"], str(got))
real_footer = cmw.prompt_footer_up
cmw.prompt_footer_up = lambda nonblank: False      # a frame it cannot place, whatever the cause
try:
    w = cmw.WorkflowWatch()
    got = []
    for t, colour, clock in ((0.0, DIM, "2m39s"), (1.0, GREEN, "2m40s")):
        text, fg = frame([row(colour, clock=clock)])
        cmw.detect_state(text, fg, w, t)
        got.append(cmw.wf_field(w, t)[:1])
finally:
    cmw.prompt_footer_up = real_footer
check("rows on screen are read even where prompt_footer_up sees no footer: Claude draws them "
      "only under it, so a row is proof enough that it is up", got == ["R", "D"], str(got))

print("\nodd rows that must not throw")
check("a 300-digit token count is not a row (and no OverflowError)",
      cmw.parse_wf_row("  ◯ x  ▱  ↓ " + "9" * 297 + "t") is None)
w = cmw.WorkflowWatch()
same = [(dict(R("3m02s"), done=2, total=5), "error"), (dict(R("1m10s"), done=1, total=5), "error")]
w.observe(same, True, 0.0)
w.observe(same, True, cmw.WF_FROZEN_S + 1)
check("two red rows with the SAME name both settle once their clocks stop",
      [st for _r, st in w.rows] == ["F", "F"] and w.active is False, str(w.rows))

# --------------------------------------------------------------------------- #
# 7. The caps field 8 shares with the daemon
# --------------------------------------------------------------------------- #
# Each side keeps its own copy of these, and nothing else ties them together:
# a daemon that took more records than the wrapper sends would count runs the
# wire never carries; one that cut a name or a note shorter would show what
# the wrapper never meant. Read from the daemon's source, not imported -- the
# daemon pulls in its own dependencies, and this file needs none of them.
print("\nthe caps field 8 shares with the daemon (daemon/claude_mate_daemon.py)")
with open(os.path.join(REPO, "daemon", "claude_mate_daemon.py"), encoding="utf-8") as fh:
    daemon_src = fh.read()
for ours, theirs, what in (("WF_MAX_RECORDS", "WF_RECORDS", "records per line"),
                           ("WF_NAME_MAX", "WF_NAME_CHARS", "characters of a name"),
                           ("WF_NOTE_MAX", "WF_INFO_CHARS", "characters of a note"),
                           ("WF_JUST_ENDED_S", "WF_SPOTLIGHT_S", "seconds a run counts as just ended")):
    mine = getattr(cmw, ours, None)
    m = re.search(rf"^{theirs}[ \t]*=[ \t]*(\d+(?:\.\d+)?)\b", daemon_src, re.M)
    missing = ([f"bin/claude-mate-wrap has no {ours}"] if mine is None else []) + \
              ([f"daemon/claude_mate_daemon.py has no '{theirs} = <number>'"] if m is None else [])
    if missing:
        check(f"{what}: {ours} / {theirs} -- MISSING", False,
              "; ".join(missing) + " (renamed? this check must follow it)")
        continue
    check(f"{what}: the wrapper's {ours} ({mine}) is the daemon's {theirs} ({m.group(1)})",
          float(mine) == float(m.group(1)), f"{mine} != {m.group(1)}")

print("\n================", "ALL PASSED" if not fails else f"{fails} FAILED", "================")
sys.exit(1 if fails else 0)
