#!/usr/bin/env python3
"""The arrow-key app in bin/claude-mate: key decoding, and the TTY gate.

TWO THINGS THIS PINS, and both of them broke during development.

THE KEY DECODER. It is the first escape-sequence decoder in this repo, and the
first version of it did not work: it selected on the file descriptor but read
through `sys.stdin`, so the three bytes of an arrow key landed in Python's
buffer, the follow-up select asked the kernel and correctly said "nothing more",
and every arrow decoded as a bare ESC. The selection never moved. The burst
cases below are that bug, written down.

THE TTY GATE. `claude-mate` with no arguments is now an interactive app on a
terminal and the same one-shot queue print everywhere else. Hooks, cron, pipes
and CI all depend on the second half, and CI runs the suite with `< /dev/null`,
so the gate has to test stdin AND stdout -- either one alone gets it wrong on
some machine that matters.

Run:   python3 tools/test_app.py
"""
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import pty
import re
import select
import shutil
import site
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unicodedata
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "bin", "claude-mate")

fails = 0


def check(what, cond, detail=""):
    global fails
    if cond:
        print(f"  ok    {what}")
    else:
        print(f"  FAIL  {what} {detail}")
        fails += 1


def load(path, name):
    """Import an extensionless executable. Safe: main() is behind __main__."""
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load(CLI, "cmate")
# The decoder and the chooser live in the shared module now -- that is the whole
# point of it, since the wrapper's account picker calls the same code. Test it
# where it lives rather than through whichever script happens to re-export it.
sys.path.insert(0, os.path.join(REPO, "bin"))
import claude_mate_ui as UI  # noqa: E402

# A HOME OF ITS OWN for every run of the CLI in this file. The app reads the
# chats the daemon kept from ~/.config/claude-mate/history, and a test run
# against the developer's real one would put his chats in its frames: one
# result on his machine, another in CI. TMPDIR too, because the terminals'
# context files there say which chats are open, and the accounts dir, because a
# chat with no recorded login is looked for in every profile.
SANDBOX = tempfile.mkdtemp(prefix="cm-app-")
HOME = os.path.join(SANDBOX, "home")
os.makedirs(HOME)


def cli_env(home=HOME):
    env = dict(os.environ)
    env.update({"HOME": home, "PYTHONUSERBASE": site.getuserbase(),
                "TMPDIR": SANDBOX,
                "CLAUDE_MATE_ACCOUNTS_DIR": os.path.join(SANDBOX, "no-accounts")})
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CLAUDE_MATE_ACCOUNT", None)
    return env


# --------------------------------------------------------------------------- #
# The key decoder, driven through a real pipe
# --------------------------------------------------------------------------- #
def decode(payload, n=1):
    """Feed bytes down a pipe and read n keys back out of them."""
    r, w = os.pipe()
    os.write(w, payload)
    os.close(w)
    UI._KEYBUF.clear()                     # the decoder's own buffer is global
    keys = [UI.read_key(r, timeout=0.5) for _ in range(n)]
    os.close(r)
    return keys if n > 1 else keys[0]


print("\n== the key decoder ==")
check("up arrow", decode(b"\x1b[A") == "up")
check("down arrow", decode(b"\x1b[B") == "down")
check("right arrow", decode(b"\x1b[C") == "right")
check("left arrow", decode(b"\x1b[D") == "left")
# Some terminals (and tmux, and a real Terminal.app in application-cursor mode)
# send SS3 instead of CSI for the arrows. Both have to work or the app is
# arrow-driven only on some machines.
check("SS3 up (ESC O A), as tmux and app-cursor mode send it",
      decode(b"\x1bOA") == "up")
check("SS3 down", decode(b"\x1bOB") == "down")
check("page up", decode(b"\x1b[5~") == "pgup")
check("home", decode(b"\x1b[H") == "home")
check("enter (CR)", decode(b"\r") == "enter")
check("enter (LF)", decode(b"\n") == "enter")
check("ctrl-c arrives as a name, not a byte", decode(b"\x03") == "ctrl-c")
check("an ordinary letter is itself", decode(b"q") == "q")
check("EOF is reported, not spun on", decode(b"") == "eof")

# THE BUG. Three bytes in ONE write is what a terminal actually does, and it is
# the case the first implementation got wrong.
check("a whole arrow arriving in one burst decodes as one key",
      decode(b"\x1b[B") == "down")
check("two arrows in one burst decode as two keys, in order",
      decode(b"\x1b[B\x1b[A", n=2) == ["down", "up"])
check("an arrow followed by a letter in one burst",
      decode(b"\x1b[Aq", n=2) == ["up", "q"])
check("a letter followed by an arrow in one burst",
      decode(b"j\x1b[B", n=2) == ["j", "down"])

# A LONE ESC is only knowable by waiting. It must not swallow the key after it.
check("a bare ESC with nothing behind it is the ESC key",
      decode(b"\x1b") == "esc")
check("a malformed CSI does not hang or eat the next key",
      decode(b"\x1b[", n=1) == "esc")


# --------------------------------------------------------------------------- #
# The TTY gate
# --------------------------------------------------------------------------- #
print("\n== piped output is still the old one-shot print ==")


def run_piped(args, stdin_devnull=True):
    with open(os.devnull) as null:
        return subprocess.run([sys.executable, CLI] + args,
                              stdin=null if stdin_devnull else None,
                              capture_output=True, text=True, timeout=30,
                              env=cli_env())


r = run_piped([])
# EITHER OUTCOME IS RIGHT, and which one depends on whether a daemon happens to
# be up on the machine running this: 0 with the queue printed, or 1 with "no
# daemon" on stderr. CI has no daemon and a developer usually does, so pinning
# 0 here passed locally and failed in CI on the first run. What the check is
# actually about is that it EXITED -- an app would still be sitting there.
check("no-arg with a pipe exits instead of opening the app",
      r.returncode in (0, 1), f"rc={r.returncode}")
if r.returncode == 1:
    check("...and says why, rather than failing silently",
          "no daemon" in (r.stdout + r.stderr), repr(r.stderr[:100]))
# The alternate screen is the app's fingerprint. If either of these reaches a
# pipe, something has started an interactive session where it should not.
check("...and does not emit the alternate-screen escape",
      "\x1b[?1049h" not in r.stdout)
check("...and does not hide the cursor", "\x1b[?25l" not in r.stdout)
check("...and prints no ANSI colour into a pipe",
      "\x1b[" not in r.stdout, repr(r.stdout[:80]))

r = run_piped(["app"])
check("`claude-mate app` refuses politely without a terminal",
      r.returncode != 0 and "needs a terminal" in (r.stdout + r.stderr))

r = run_piped(["--version"])
check("--version prints something", r.returncode == 0 and r.stdout.strip())
check("...naming the tool", r.stdout.startswith("claude-mate "))

r = run_piped(["--help"])
check("--help documents the app", r.returncode == 0 and "the app" in r.stdout)
check("...and the daemon command", "claude-mate daemon" in r.stdout)


# --------------------------------------------------------------------------- #
# The app itself, under a real terminal
# --------------------------------------------------------------------------- #
# SEPARATE PTYs for stdin and stdout, the pattern test_wrapper_lifecycle.py uses
# for the same reason: with one shared PTY the terminal echoes every keystroke
# back into the output being asserted on.
print("\n== the app, driven with arrow keys ==")


def drive(keystrokes, settle=0.45, warmup=1.5, home=HOME, size=(40, 100),
          env_extra=None):
    m_in, s_in = pty.openpty()
    m_out, s_out = pty.openpty()
    # GIVE THE PTY A SIZE. openpty() leaves the window at 0x0, and a program
    # that trusts os.get_terminal_size() then renders into a zero-row screen --
    # which is exactly how the app came to be silently dropping its last line.
    # A real terminal always has a size, so the test should provide one too;
    # the app now also floors it, and both halves are worth keeping.
    fcntl.ioctl(s_out, termios.TIOCSWINSZ,
                struct.pack("HHHH", size[0], size[1], 0, 0))
    env = cli_env(home)
    # A socket that cannot exist, so the app runs in its no-daemon shape and
    # this test never depends on a daemon being up on the machine running it.
    env["CLAUDE_MATE_SOCK"] = "/tmp/claude-mate-test-nonexistent.sock"
    env.update(env_extra or {})
    p = subprocess.Popen([sys.executable, CLI], stdin=s_in, stdout=s_out,
                         stderr=s_out, close_fds=True, env=env)
    os.close(s_in)
    os.close(s_out)

    def drain(t):
        buf, end = b"", time.time() + t
        while time.time() < end:
            if select.select([m_out], [], [], 0.05)[0]:
                try:
                    buf += os.read(m_out, 65536)
                except OSError:
                    break
        return buf.decode("utf-8", errors="replace")

    def press(k):
        try:
            os.write(m_in, k)
        except OSError:             # it has gone: what it printed says why
            pass

    frames = [drain(warmup)]
    for k in keystrokes:
        press(k)
        time.sleep(settle)
        frames.append(drain(settle))
    press(b"q")
    time.sleep(settle)
    frames.append(drain(1.0))
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
    os.close(m_in)
    os.close(m_out)
    return p.returncode, frames


def selected(frame):
    """The row the cursor is on, from the last painted frame."""
    plain = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", frame)
    marked = [ln for ln in plain.splitlines() if "▸" in ln]
    return marked[-1].strip() if marked else ""


rc, frames = drive([b"\x1b[B", b"\x1b[B", b"\x1b[A"])
check("it exits 0 on q", rc == 0, f"rc={rc}")
check("it enters the alternate screen", "\x1b[?1049h" in frames[0])
check("...and leaves it again on the way out", "\x1b[?1049l" in frames[-1])
check("...and puts the cursor back", "\x1b[?25h" in frames[-1])
check("it never clears the whole screen mid-loop (that is the flicker)",
      "\x1b[2J" not in "".join(frames[1:-1]))
check("with no daemon it says so rather than dying",
      "not running" in re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", frames[0]))

first, second, third = selected(frames[1]), selected(frames[2]), selected(frames[3])
check("one down moves the selection", first and first != selected(frames[0]),
      f"{selected(frames[0])!r} -> {first!r}")
check("a second down moves it again", second and second != first,
      f"{first!r} -> {second!r}")
check("up goes back to where it was", third == first,
      f"{third!r} != {first!r}")

# The menu has to offer the thing the whole feature exists for.
menu = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", frames[0])
check("the menu offers a new session", "Start new session" in menu)
check("...and says which one skips permissions",
      "skip permissions" in menu)
check("...and offers to start the daemon, saying start rather than restart "
      "when it is down", "start daemon" in menu)
check("...and the History of past chats, as one row",
      re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", frames[0].rsplit("\x1b[H", 1)[-1])
      .count("History · resume or delete a past chat") == 1)

# --------------------------------------------------------------------------- #
# A session's workflow, drawn under it
# --------------------------------------------------------------------------- #
# The sub-line is DISPLAY ONLY: not a row, never selectable -- and it must never
# carry the '▸' the cursor row is marked with, which is exactly how selected()
# above (and a person) finds the cursor. A workflow is named by whoever wrote
# it, so the name is the attack: one made of '▸'s.
print("\n== a session's workflow, under it in the app ==")


class _Term:
    def size(self):
        return (110, 40)


_wf_snap = {"queue": [
    {"i": 0, "key": "a", "name": "alpha", "state": "working", "secs": 12,
     "model": "", "effort": "", "account": "", "limit": "", "cwd": "",
     "acked": True, "shown": True,
     "workflows": [
         {"name": "▸sneaky▸", "st": "R", "status": "running", "done": 1,
          "total": 4, "failed": None, "pct": 25, "secs": 75, "tokens": 9000,
          "note": "til ▸3:30pm"},
         {"name": "second", "st": "D", "status": "done", "done": 4,
          "total": 4, "failed": None, "pct": 100, "secs": 60, "tokens": 12,
          "note": ""}]},
    # An older daemon's entry: no 'workflows' key at all.
    {"i": 1, "key": "b", "name": "bravo", "state": "idle", "secs": 3,
     "model": "", "effort": "", "account": "", "limit": "", "cwd": "",
     "acked": True, "shown": False}],
    "follow": False, "shown": "a", "selected": "a"}
_rows_ = M._rows(_wf_snap)


def _plain(lines):
    return [re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", ln) for ln in lines]


_drawn = _plain(M._draw(_Term(), _wf_snap, 0, _rows_, None, True))
_row = next((i for i, ln in enumerate(_drawn) if "alpha" in ln), -1)
_sub = _drawn[_row + 1] if 0 <= _row < len(_drawn) - 1 else ""
check("the session's workflow is drawn on the line under it",
      "sneaky" in _sub and "running" in _sub and "1/4" in _sub
      and "til >3:30pm" in _sub, repr(_sub))
# The run's clock is not a time in state: the band writes it in Claude's
# units, so the line beside it does too -- 1m15s, not r1's 1:15.
check("...its clock in Claude's units and its tokens as the band writes them",
      "1m15s" in _sub and "9.0k tokens" in _sub and "1:15" not in _sub,
      repr(_sub))
check("...ONE line however many it runs, the rest as '+N more'",
      "+1 more" in _sub and not any("second" in ln for ln in _drawn))
_brow = next((i for i, ln in enumerate(_drawn) if "bravo" in ln), -1)
check("...and no line at all for a session from an older daemon",
      _brow >= 0 and not any("↳" in ln for ln in _drawn[_brow + 1:]))
_clean = True
for _sel in range(len(_rows_)):
    _lines = _plain(M._draw(_Term(), _wf_snap, _sel, _rows_, None, True))
    _marked = [ln for ln in _lines if "▸" in ln]
    _want = (_rows_[_sel]["s"]["name"] if _rows_[_sel]["kind"] == "session"
             else _rows_[_sel]["label"])
    if len(_marked) != 1 or _want not in _marked[0]:
        _clean = False
        print(f"        sel={_sel}: {_marked!r}")
check("a workflow named with '▸' never puts one on its line -- the only '▸' "
      "is the cursor's, on every row", _clean)
check("...so selected() still finds the cursor row",
      selected("\n".join(M._draw(_Term(), _wf_snap, 0, _rows_, None, True)))
      .lstrip("▸• ").startswith("alpha"))

# AN 80x24 TERMINAL, the size a new one opens at. The app paints one terminal
# row per line, so a line wider than the terminal WRAPS: every row below moves
# down, the terminal scrolls, and the header and the cursor row go off the top
# -- Enter then acts on a row nobody can see. A failing run with a 40-character
# name (the most the wire carries) made a 108-column sub-line; a waiting
# session's model, effort, account and limit, a 104-column row.
print("\n== at 80 columns, nothing wraps ==")


def _cols(line):
    """Columns a painted line takes: colour codes none, East Asian wide
    characters two. The test's own count, so the app is not measured by the
    rule it is being checked against."""
    plain = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", line)
    return sum(0 if unicodedata.category(c) in ("Mn", "Me", "Cf")
               else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in plain)


class _TermOf:
    def __init__(self, cols):
        self.cols = cols

    def size(self):
        return (self.cols, 24)


_LONG = "nightly-dependency-upgrade-risk-sweep-42"
_E = {"st": "E", "status": "running", "done": 3, "total": 7, "failed": None,
      "pct": 42, "secs": 37375, "tokens": 23065249, "note": ""}
_snap80 = {"queue": [
    {"i": i, "key": f"k{i}", "name": name, "state": state, "secs": 754,
     "model": "Opus 4.8 (1M context)", "effort": "xhigh",
     "account": "work-account", "limit": "5h82%", "cwd": "",
     "acked": state == "working", "shown": i == 0,
     "workflows": [dict(_E, name=wf), dict(_E, name="b", st="R"),
                   dict(_E, name="c", st="D", status="done")]}
    for i, (name, state, wf) in enumerate((
        ("api-gateway", "working", _LONG),
        ("billing-reconciliation", "waiting", _LONG),
        ("checkout", "working", "名前" * 10),       # 40 columns of CJK
        ("docs-site", "error", _LONG),
        ("edge-cache", "working", _LONG),
        ("frontend", "working", _LONG)))],
    "follow": False, "shown": "k0", "selected": "k0"}
_rows80 = M._rows(_snap80)
_wide, _marks = [], True
for _sel in range(len(_rows80)):
    _lines = M._draw(_TermOf(80), _snap80, _sel, _rows80, None, True)
    _wide += [(_cols(ln), ln) for ln in _lines if _cols(ln) > 79]
    _marked = [ln for ln in _plain(_lines) if "▸" in ln]
    _want = (_rows80[_sel]["s"]["name"] if _rows80[_sel]["kind"] == "session"
             else _rows80[_sel]["label"])
    _marks = _marks and len(_marked) == 1 and _want in _marked[0]
check("no painted line is wider than 79 columns, on any row the cursor is on",
      not _wide, repr(_wide[:2]))
check("...and the cursor's '▸' is still on exactly the selected row", _marks)


def _row_of(lines, name):
    """Where a session's own row is (not its sub-line); -1 if nowhere."""
    return next((i for i, ln in enumerate(lines)
                 if f" {name} " in ln and "↳" not in ln), -1)


_d80 = _plain(M._draw(_TermOf(80), _snap80, 0, _rows80, None, True))
_sub80 = _d80[_row_of(_d80, "api-gateway") + 1]
check("the name gives first -- cut, with an ellipsis -- and every fact after "
      "it stays", "nightly-dep" in _sub80 and "…" in _sub80
      and _LONG not in _sub80 and all(
          x in _sub80 for x in ("running", "an agent failed", "3/7", "10h22m",
                                "23M tokens", "+2 more")), repr(_sub80))
_cjk = _d80[_row_of(_d80, "checkout") + 1]
check("...counting a wide character as the two columns it takes",
      "名前" in _cjk and "…" in _cjk and _cols(_cjk) <= 79, repr(_cjk))
_row = _d80[_row_of(_d80, "billing-reconciliation")]
check("a session row that does not fit drops its meta from the end, so "
      "'needs you' outlasts the limit chip and the account",
      "needs you" in _row and "Opus 4.8" in _row and "5h82%" not in _row
      and "work-account" not in _row and _cols(_row) <= 79, repr(_row))
_d60 = _plain(M._draw(_TermOf(60), _snap80, 0, _rows80, None, True))
_sub60 = _d60[_row_of(_d60, "api-gateway") + 1]
check("narrower still (60): then the tokens and '+N more' go, and the word, "
      "the failing agent and the count stay", "23M tokens" not in _sub60
      and "+2 more" not in _sub60 and _cols(_sub60) <= 59 and all(
          x in _sub60 for x in ("running", "an agent failed", "3/7")),
      repr(_sub60))

# THE NOTE UNDER THE MENU, by the same rule. It is whatever a level below had
# to say -- a resume refused because its directory is gone runs to 175
# columns -- and painted raw it wrapped, pushed the frame past the bottom and
# scrolled the header off the top: the failure above, by another door.
print("\n== ...and neither does the note under the menu ==")
_NOTE = ("\033[31mits directory /Users/me/Projects/claude_mate/.claude/"
         "worktrees/feat-history no longer exists — claude finds the "
         "conversation through it; recreate it, or delete the record\033[0m")
_dn = M._draw(_TermOf(80), None, 0, M._rows(None), _NOTE, False)
_nrows = [ln for ln in _dn if "\033[31m" in ln]
check("a 175-column note is wrapped into at most three rows, and no line of "
      "the frame is wider than 79 columns", 1 <= len(_nrows) <= 3
      and all(_cols(ln) <= 79 for ln in _dn),
      repr([(_cols(ln), ln) for ln in _dn if _cols(ln) > 79][:2]))
check("...with every word of it, the end too",
      "".join(_plain(_nrows)).replace(" ", "")
      == _plain([_NOTE])[0].replace(" ", ""), repr(_plain(_nrows)))
check("...each row in the note's colour, and reset at its end, so the colour "
      "cannot run on", all(ln.startswith(" \033[31m") and ln.endswith("\033[0m")
                           for ln in _nrows), repr(_nrows))
_ML = ("\033[31mstarted it, but it exited immediately:\n"
       "    ModuleNotFoundError: No module named 'serial'\n"
       "  full log:  tail -f /Users/me/Library/Logs/claude-mate.err.log\033[0m")
_mrows = [ln for ln in M._draw(_TermOf(40), None, 0, M._rows(None), _ML, False)
          if "\033[31m" in ln]
check("a note with line breaks of its own -- why the daemon died -- keeps "
      "every line, each fitted to the width",
      len(_mrows) >= 3 and all(_cols(ln) <= 39 and "\n" not in ln
                               for ln in _mrows)
      and "".join(_plain(_mrows)).replace(" ", "")
      == _plain([_ML])[0].replace("\n", "").replace(" ", ""), repr(_mrows))

# A REPAINT LEAVES NOTHING OF THE FRAME BEFORE. Term.paint erased only at the
# end of each line, so the first row of a two-row note -- the chat menu's
# cwd, the thing to read before Resume or Delete -- kept whatever the frame
# before had drawn past it: another chat's title, run on after the path.
print("\n== a repaint leaves nothing of the frame before ==")
_CSI = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")


def _screen(data, rows=24, cols=80):
    """The screen after `data`: as much of a terminal as Term.paint needs --
    home, erase to the end of the line or of the screen, CR, LF and text,
    colour codes taking no room."""
    grid = [[" "] * cols for _ in range(rows)]
    r = c = i = 0
    while i < len(data):
        m = _CSI.match(data, i)
        if m:
            if m.group(2) == "H":
                r = c = 0
            elif m.group(2) in ("K", "J"):
                grid[r][c:] = [" "] * (cols - c)
                if m.group(2) == "J":
                    grid[r + 1:] = [[" "] * cols for _ in range(rows - r - 1)]
            i = m.end()
            continue
        if data[i] == "\r":
            c = 0
        elif data[i] == "\n":
            r = min(r + 1, rows - 1)
        elif c < cols:
            grid[r][c] = data[i]
            c += 1
        i += 1
    return ["".join(row).rstrip() for row in grid]


class _PaintTerm(UI.Term):
    def __init__(self):             # nothing to set up: it paints into stdout
        self.saved = None

    def size(self):
        return (80, 24)


_buf, _stdout_was = io.StringIO(), sys.stdout
sys.stdout = _buf
try:
    _pt = _PaintTerm()
    _pt.paint([" HISTORY", ""] + [" " + "x" * 70] * 4)
    _pt.paint([" CHAT · a chat", "", " /Users/me/p\n work · 3f2a9c1e",
               " Back"])
finally:
    sys.stdout = _stdout_was
_scr = _screen(_buf.getvalue())
check("a two-row note painted over a wider frame: each of its rows is erased "
      "past its end, so nothing of the old frame shows beside it",
      _scr[2:6] == [" /Users/me/p", " work · 3f2a9c1e", " Back", ""],
      repr(_scr[:6]))

# --------------------------------------------------------------------------- #
# The History row
# --------------------------------------------------------------------------- #
# ONE ROW ON THE TOP LEVEL, after the two that start a session -- the chats
# themselves are a level down. The loops above already hold it to the rules
# every row keeps (a label, one '▸', 79 columns); these pin where it sits and
# that drawing it reads nothing: the top level repaints every second, and a
# history is kept until someone deletes it, so it only ever grows.
print("\n== the History row ==")
_kinds = [r["kind"] for r in M._rows(None)]
check("one History row, after the two Start rows and before Device",
      _kinds == ["new", "new", "history", "device", "accounts", "quit"],
      repr(_kinds))
check("...labelled the way the other rows are: what it is · what it does",
      M._rows(None)[2].get("label") == "History · resume or delete a past chat")
check("...in the same place with sessions above it",
      [r["kind"] for r in M._rows(_wf_snap)][2:5] == ["new", "new", "history"])
_saved = (M.load_history, M.live_conversations, M.chat_title)


def _no_disk(*_a, **_k):
    raise AssertionError("the top level read the history")


M.load_history = M.live_conversations = M.chat_title = _no_disk
try:
    M._draw(_Term(), _wf_snap, 2, M._rows(_wf_snap), None, True)
    _pure = True
except AssertionError:
    _pure = False
finally:
    M.load_history, M.live_conversations, M.chat_title = _saved
check("...and drawing the top level reads no history at all", _pure)

# --------------------------------------------------------------------------- #
# The shared chooser, and the picker every session goes through
# --------------------------------------------------------------------------- #
# THE LEVELS UNDER THE TOP ONE are the point of claude_mate_ui existing: the app
# had arrow keys and choosing an account still meant typing a number. These
# drive the real chooser and the real wrapper picker under a pty, because the
# whole class of bug here (escape sequences, terminal state, unprintable names)
# only exists on a terminal.
print("\n== the shared chooser ==")


def drive_snippet(code, keystrokes, accounts_dir=None, warmup=1.5, settle=0.45,
                  size=(40, 110)):
    """Run a python snippet under its own pty and feed it keys."""
    m_in, s_in = pty.openpty()
    m_out, s_out = pty.openpty()
    fcntl.ioctl(s_out, termios.TIOCSWINSZ,
                struct.pack("HHHH", size[0], size[1], 0, 0))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(REPO, "bin")
    if accounts_dir:
        env["CLAUDE_MATE_ACCOUNTS_DIR"] = accounts_dir
    p = subprocess.Popen([sys.executable, "-c", code], stdin=s_in,
                         stdout=s_out, stderr=s_out, close_fds=True,
                         cwd=REPO, env=env)
    os.close(s_in)
    os.close(s_out)

    def drain(t):
        buf, end = b"", time.time() + t
        while time.time() < end:
            if select.select([m_out], [], [], 0.05)[0]:
                try:
                    buf += os.read(m_out, 65536)
                except OSError:
                    break
        return buf.decode("utf-8", errors="replace")

    out = drain(warmup)
    for k in keystrokes:
        try:
            os.write(m_in, k)
        except OSError:             # it has gone: what it printed says why
            break
        time.sleep(settle)
        out += drain(settle)
    try:
        p.wait(timeout=6)
    except subprocess.TimeoutExpired:
        p.kill()
        out += drain(0.3)
    os.close(m_in)
    os.close(m_out)
    return out


PICK = ("import claude_mate_ui as U;"
        "items=[U.Item('alpha'),U.Item('beta'),U.Item('gamma')];"
        "print('CHOSE',repr(U.pick('PICK ONE',items)))")

out = drive_snippet(PICK, [b"\x1b[B", b"\r"])
check("down then enter chooses the second item", "CHOSE 'beta'" in out, out[-120:])
out = drive_snippet(PICK, [b"\x1b[A", b"\r"])
check("up from the top wraps to the last", "CHOSE 'gamma'" in out)
out = drive_snippet(PICK, [b"\x1b"])
check("esc backs out with nothing chosen", "CHOSE None" in out)
out = drive_snippet(PICK, [b"q"])
check("...and so does q", "CHOSE None" in out)
out = drive_snippet(PICK, [b"2"])
check("a digit still picks that row, for the old muscle memory",
      "CHOSE 'gamma'" in out)
# A KEYBOARD LEFT IN ANOTHER LAYOUT. read_key hands a UTF-8 key over a byte at
# a time, and the second byte of a Cyrillic 'в' is '²': a digit to
# str.isdigit(), a ValueError to int() -- and a traceback where the chooser
# was, the wrapper's account picker at the start of every session among them.
out = drive_snippet(PICK, ["в".encode(), b"\r"])
check("a Cyrillic 'в' (its second byte reads as '²') chooses nothing and "
      "crashes nothing: Enter after it still takes the first row",
      "CHOSE 'alpha'" in out and "Traceback" not in out, out[-200:])
out = drive_snippet(PICK, ["й".encode(), "ó".encode(), "β".encode(), b"\r"])
check("...nor й, ó or β (whose second bytes read as ¹, ³ and ²)",
      "CHOSE 'alpha'" in out and "Traceback" not in out, out[-200:])

# UNPRINTABLE NAMES ARE THE REASON printable() EXISTS. A profile really is
# called "\033" on a real machine, so its label and its path both carry a raw
# ESC -- printing that does not show a character, it starts a control sequence.
NASTY = ("import claude_mate_ui as U;"
         "items=[U.Item('\\x1b[31mred', detail='\\x1b]0;title\\x07'),U.Item('ok')];"
         "print('CHOSE',repr(U.pick('T',items)))")
out = drive_snippet(NASTY, [b"\r"])
body = out.split("CHOSE")[0]
stray = re.sub(r"\x1b(\[[0-9;?]*[a-zA-Z]|\][^\x07]*\x07)", "", body).count("\x1b")
check("an item label containing a raw ESC cannot emit one", stray == 0,
      f"{stray} stray ESC bytes")
check("...it is shown escaped instead", "\\x1b" in body)

# --------------------------------------------------------------------------- #
# The chooser, past the bottom of the screen
# --------------------------------------------------------------------------- #
# THE CHOOSER DREW EVERY ROW AND THE TERMINAL KEPT WHAT FITTED: Term.paint drops
# whatever is past rows-1, the cursor's row with it once you had arrowed far
# enough, and Enter then chose a row nobody could see. Nothing had that many
# rows until the history, which only grows. Now a window of the list is drawn
# around the cursor -- and every caller whose list fits must get exactly the
# bytes it got before, the wrapper's account picker first among them: it runs
# at the start of every session.
print("\n== the chooser keeps its cursor on the screen ==")


def _old_render(title, items, sel, hint, note):
    """claude_mate_ui._render as it was before it could scroll, byte for byte:
    the reference its no-size path is held to."""
    U = UI
    out = [f" {U.B}{title}{U.OFF}", ""]
    labels = [U.printable(i.label) for i in items]
    width = max((len(x) for x in labels), default=0)
    for i, it in enumerate(items):
        if it.gap:
            out.append("")
        cur = i == sel
        label = labels[i]
        mark = f"{U.C_SEL}{U.B}▸{U.OFF}" if cur else " "
        col = U.C_WARN if it.danger else (U.C_SEL if cur else "")
        body = f"{col}{U.B}{label}{U.OFF}" if cur else f"{col}{label}{U.OFF}"
        pad = " " * max(0, width - len(label))
        det = U.printable(it.detail)
        detail = f"  {U.D}{det}{U.OFF}" if det else ""
        out.append(f" {mark} {body}{pad}{detail}")
    out.append("")
    if note:
        out.append(f" {note}")
        out.append("")
    out.append(f" {U.D}{hint or '↑↓ move   ⏎ choose   esc back'}{U.OFF}")
    return out


# The shapes the chooser is really called with. The account picker's rows, as
# _pick_account builds them: the default login, a profile, an untypeable one
# shown as a repr, a Cyrillic one, the gapped "New account…" and "Device…".
_NEW, _DEV = object(), object()
_SHAPES = [
    ("claude-mate — which account?", [
        UI.Item("default", value="", detail="me@example.com"),
        UI.Item("work", value="work", detail="(not logged in)"),
        UI.Item(repr("\x1b"), value="\x1b", detail="other@example.com"),
        UI.Item("в", value="в", detail="(not logged in)"),
        UI.Item("New account…", value=_NEW, gap=True,
                detail="create a profile and log in there"),
        UI.Item("Device…", value=_DEV,
                detail="connect a Claude Mate, or see why it is not")],
     "↑↓ move   ⏎ start   esc cancel", None),
    ("DELETE work?", [UI.Item("No, leave it alone", value=False),
                      UI.Item("Yes", value=True, danger=True)],
     "me@example.com · /tmp/accounts/work · there is no undo", None),
    ("ACCOUNT · work", [
        UI.Item("Use for the next new session", value="use",
                detail="sets CLAUDE_MATE_ACCOUNT for a session started here"),
        UI.Item("Delete this profile", value="rm", danger=True, gap=True,
                detail="its login and every transcript under it"),
        UI.Item("Back", value="back", gap=True)],
     None, "\033[2mme@example.com\n  /tmp/accounts/work\033[0m"),
]
# Real colour codes for the comparison even when this runs into a pipe (CI),
# where the module blanks its own: where the codes land is part of the bytes.
_colours = (UI.B, UI.D, UI.OFF, UI.C_SEL, UI.C_WARN)
UI.B, UI.D, UI.OFF, UI.C_SEL, UI.C_WARN = (
    "\033[1m", "\033[2m", "\033[0m", "\033[36m", "\033[31m")
try:
    _same = _fits_same = True
    for _t, _its, _h, _n in _SHAPES:
        for _s in range(len(_its)):
            _want = _old_render(_t, _its, _s, _h, _n)
            _same = _same and UI._render(_t, _its, _s, _h, _n) == _want
            _fits_same = (_fits_same
                          and UI._render(_t, _its, _s, _h, _n, 110, 40) == _want)
finally:
    UI.B, UI.D, UI.OFF, UI.C_SEL, UI.C_WARN = _colours
check("with no size, _render is the same bytes it always was -- the account "
      "picker's rows, a confirm, a two-line note", _same)
check("...and with the terminal's size, when everything fits, still the same "
      "bytes", _fits_same)

_many = [UI.Item(f"i{n:02d} " + "名前" * (n % 4),
                 detail=" ".join(["detail"] * (n % 6)),
                 gap=(n % 10 == 9), danger=(n % 13 == 0)) for n in range(40)]
_vbad = []
for _rows_n, _cols_n in ((24, 80), (10, 40), (24, 40)):
    for _note in (None, "a note", "a note\n on two lines"):
        for _s in range(len(_many)):
            _lines, _whole = UI._layout("PICK ONE", _many, _s, "the hint",
                                        _note, _cols_n, _rows_n)
            _phys = "\n".join(_plain(_lines)).split("\n")
            _at = [i for i, ln in enumerate(_phys) if "▸" in ln]
            _up = [ln for ln in _phys if "↑" in ln and "more" in ln]
            _dn = [ln for ln in _phys if "↓" in ln and "more" in ln]
            _seen = sum(1 for ln in _phys if re.match(r"^ [▸ ] i\d\d", ln))
            _n_up = int(_up[0].split()[1]) if _up else 0
            _n_dn = int(_dn[0].split()[1]) if _dn else 0
            _ok = (len(_phys) <= _rows_n - 1
                   and all(_cols(ln) <= _cols_n - 1 for ln in _phys)
                   and len(_at) == 1 and f"i{_s:02d}" in _phys[_at[0]]
                   and "PICK ONE" in _phys[0] and "the hint" in _phys[-1]
                   and (_note is None or "a note" in "\n".join(_phys))
                   and _n_up + _seen + _n_dn == len(_many) and not _whole)
            if not _ok:
                _vbad.append((_rows_n, _cols_n, _note, _s))
check("40 rows in a 24- or 10-row terminal: on every row the cursor can be on, "
      "the frame fits, the cursor's row is drawn, and title, note and hint stay",
      not _vbad, repr(_vbad[:3]))
check("...every line fits the width, wide characters counted as two "
      "(checked above, for 80 and 40 columns)", not _vbad)
check("...and '↑ N more' / '↓ N more' account for every row not drawn "
      "(checked above)", not _vbad)
def _cursor_row(lines):
    phys = "\n".join(_plain(lines)).split("\n")
    return next((i for i, ln in enumerate(phys) if "▸" in ln), -1)


_floor = all(0 <= _cursor_row(UI._render("PICK ONE", _many, _s, "the hint",
                                         _note, 40, 6)) < 5
             for _s in range(len(_many)) for _note in (None, "a note"))
check("...and at the smallest screen Term will report (6 rows) the cursor's row "
      "is still drawn", _floor)
check("a list that fits says so, so its digits still work",
      UI._layout("T", _many[:5], 0, None, None, 80, 24)[1] is True)

# DIGITS. A number chooses its row at once, which is the old typed picker's
# muscle memory -- and in confirm() it was a YES to a destructive question
# without the cursor ever leaving No, and in a scrolled list a row off the
# screen. Driven under a pty, as a person would press them.
print("\n== the number shortcuts ==")
CONFIRM = ("import claude_mate_ui as U;"
           "print('CHOSE',repr(U.confirm('SURE?','there is no undo')))")
out = drive_snippet(CONFIRM, [b"1", b"\r"])
check("in a confirm, '1' does not answer Yes: Enter after it is still No",
      "CHOSE False" in out, out.split("CHOSE")[-1][:30] if "CHOSE" in out
      else out[-120:])
out = drive_snippet(CONFIRM, [b"\x1b[B", b"\r"])
check("...Yes is still one arrow and Enter away", "CHOSE True" in out)
NODIG = PICK.replace("U.pick('PICK ONE',items)",
                     "U.pick('PICK ONE',items,digits=False)")
out = drive_snippet(NODIG, [b"2", b"\r"])
check("pick(digits=False) ignores a digit", "CHOSE 'alpha'" in out)
LONG = ("import claude_mate_ui as U;"
        "items=[U.Item('i%02d' % n) for n in range(30)];"
        "print('CHOSE',repr(U.pick('LONG',items)))")
out = drive_snippet(LONG, [b"2", b"\r"], size=(12, 80))
check("a list longer than the screen: a digit does nothing (it could choose a "
      "row nobody can see)", "CHOSE 'i00'" in out,
      out.split("CHOSE")[-1][:30] if "CHOSE" in out else out[-120:])
check("...and the screen says how much is below", re.search(
    r"↓.*\b2\d more", re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", out)) is not None)
out = drive_snippet(LONG, [b"\x1b[A", b"\r"], size=(12, 80))
_last = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "",
               out.split("CHOSE")[0].rsplit("\x1b[H", 1)[-1])
check("...up from the top wraps to the last row, and that row is on the "
      "screen with the cursor on it",
      "CHOSE 'i29'" in out and re.search(r"▸ i29", _last) is not None,
      repr(_last[-200:]))

print("\n== the account picker the wrapper shows ==")
tmp = tempfile.mkdtemp(prefix="cm-pick-")
for name in ("work", "personal", "\x1b"):
    os.makedirs(os.path.join(tmp, name), exist_ok=True)
WRAPPICK = (
    "import importlib.machinery,importlib.util,os,sys;"
    "s=importlib.util.spec_from_loader('w',importlib.machinery.SourceFileLoader("
    "'w','bin/claude-mate-wrap'));"
    "W=importlib.util.module_from_spec(s);s.loader.exec_module(W);"
    "W.ACCOUNTS_DIR=os.environ['CLAUDE_MATE_ACCOUNTS_DIR'];"
    "print('CHOSE',repr(W._pick_account(W.list_profiles())))")

out = drive_snippet(WRAPPICK, [b"\r"], accounts_dir=tmp, warmup=2.0)
check("it opens as a chooser, not a typed prompt",
      "which account?" in out and "account>" not in out)
check("enter on the first row means the default account", "CHOSE ''" in out)

out = drive_snippet(WRAPPICK, [b"\x1b[B", b"\r"], accounts_dir=tmp, warmup=2.0)
check("arrowing down picks a real profile",
      "CHOSE" in out and "CHOSE None" not in out and "CHOSE ''" not in out,
      out.split("CHOSE")[-1][:40] if "CHOSE" in out else "")

out = drive_snippet(WRAPPICK, [b"\x1b"], accounts_dir=tmp, warmup=2.0)
check("esc cancels rather than choosing something",
      "_PICK_CANCELLED" in out or "object at" in out.split("CHOSE")[-1],
      out.split("CHOSE")[-1][:60] if "CHOSE" in out else "no CHOSE")

# THE BUG THAT MADE THIS NECESSARY: the old picker typed names, so an arrow key
# typed an escape sequence and CREATED a profile called "\033".
before = set(os.listdir(tmp))
drive_snippet(WRAPPICK, [b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D", b"\x1b"],
              accounts_dir=tmp, warmup=2.0)
check("mashing the arrow keys at the picker creates no profiles",
      set(os.listdir(tmp)) == before,
      f"{sorted(set(os.listdir(tmp)) - before)!r}")
shutil.rmtree(tmp, ignore_errors=True)

# --------------------------------------------------------------------------- #
# History, in the app
# --------------------------------------------------------------------------- #
# The whole path a person takes, in the real app on an 80x24 terminal, against
# a HOME made here: two kept chats -- one whose transcript Claude still has, one
# that survives only as the copy History kept, and whose title is made of the
# cursor's own glyph -- then into the first, onto Delete, a '1' that must not
# answer the confirmation, and the Yes that does.
print("\n== History, in the app ==")


def last_paint(chunk):
    """The lines of the last full paint in a chunk of output, colour gone."""
    part = chunk.rsplit("\x1b[H", 1)[-1].replace("\r", "")
    return _plain(part.split("\n"))


_hhome = os.path.join(SANDBOX, "hist-home")
_hist = os.path.join(_hhome, ".config", "claude-mate", "history")
_proj = os.path.join(SANDBOX, "some_project")
_tdir = os.path.join(_hhome, ".claude", "projects",
                     re.sub(r"[^a-zA-Z0-9]", "-", _proj))
for _d in (_hist, _proj, _tdir):
    os.makedirs(_d)
_A, _B = str(uuid.uuid4()), str(uuid.uuid4())
for _conv, _ago, _title, _flags in (
        (_A, 60, "add a history log to the daemon", ["--model", "opus"]),
        (_B, 7200, "▸▸ a title made of cursors", None)):
    with open(os.path.join(_hist, _conv + ".json"), "w") as fh:
        json.dump({"v": 1, "conversation": _conv, "name": "some_project",
                   "cwd": _proj, "config_dir": "", "account": "default",
                   "flags": _flags, "last_active": time.time() - _ago,
                   "ended": None}, fh)
    with open(os.path.join(_tdir, _conv + ".jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "user", "cwd": _proj}) + "\n")
        fh.write(json.dumps({"type": "ai-title", "aiTitle": _title}) + "\n")
os.link(os.path.join(_tdir, _B + ".jsonl"), os.path.join(_hist, _B + ".jsonl"))
os.unlink(os.path.join(_tdir, _B + ".jsonl"))       # Claude's copy is gone

_D, _E = b"\x1b[B", b"\r"
rc, fr = drive([_D, _D, _E, _E, _D, _D, _E, b"1", _D, _E], home=_hhome,
               size=(24, 80))
_list, _chat, _conf, _after1, _end = (last_paint(fr[i]) for i in (3, 4, 7, 8, 10))
_txt = "\n".join
_ia = _txt(_list).find("add a history log")
_ib = _txt(_list).find("a title made of cursors")
check("Enter on History lists the kept chats, newest first, by title",
      "HISTORY" in _txt(_list) and 0 <= _ia < _ib, _txt(_list)[-600:])
check("...a title made of '▸' shows as '>', so the cursor's is the only one",
      ">> a title made of cursors" in _txt(_list)
      and sum(ln.count("▸") for ln in _list) == 1)
check("...the note says where they are and how much exists only here",
      "chats" in _txt(_list) and "kept only here" in _txt(_list))
check("...and no line is wider than 79 columns",
      all(_cols(ln) <= 79 for ln in _list + _chat + _conf), repr(
          [ln for ln in _list + _chat + _conf if _cols(ln) > 79][:2]))
check("Enter on a chat offers Resume and Delete, and says where it ran",
      "Resume" in _txt(_chat) and "Delete from history" in _txt(_chat)
      and "some_project" in _txt(_chat), _txt(_chat)[-600:])
check("Delete asks first, and says Claude Code still has its own copy",
      "DELETE" in _txt(_conf) and "No, leave it alone" in _txt(_conf)
      and "Claude Code keeps its own copy" in _txt(_conf), _txt(_conf)[-400:])
check("...a '1' there does not answer Yes: the question is still on screen",
      "No, leave it alone" in _txt(_after1) and "forgot" not in _txt(_after1))
check("...and Yes forgets the chat: its record goes, and says so",
      rc == 0 and "forgot" in _txt(_end)
      and not os.path.exists(os.path.join(_hist, _A + ".json")),
      _txt(_end)[-300:])
check("...Claude's own transcript of it is left where it was",
      os.path.exists(os.path.join(_tdir, _A + ".jsonl")))
check("...and the other chat, and the only copy of its conversation, stay",
      os.path.exists(os.path.join(_hist, _B + ".json"))
      and os.path.exists(os.path.join(_hist, _B + ".jsonl")))

# A CHAT WRITTEN MOMENTS AGO, with no terminal the app can see on it: what
# has it may be something claude-mate cannot see (claude without the wrapper,
# the VS Code panel, a picker session before its first turn), and a second
# claude on it forks the transcript. So Resume asks, No where the cursor
# starts, and No starts nothing. If it ever goes ahead regardless, it meets
# stubs -- a claude that only leaves a mark, no usage poll, no Keychain, no
# network -- so the check fails without anything real being run.
_ahome = os.path.join(SANDBOX, "ask-home")
_ahist = os.path.join(_ahome, ".config", "claude-mate", "history")
_aproj = os.path.join(SANDBOX, "ask_project")
_atdir = os.path.join(_ahome, ".claude", "projects",
                      re.sub(r"[^a-zA-Z0-9]", "-", _aproj))
_stubs = os.path.join(SANDBOX, "stubs")
for _d in (_ahist, _aproj, _atdir, _stubs):
    os.makedirs(_d)
_C = str(uuid.uuid4())
with open(os.path.join(_ahist, _C + ".json"), "w") as fh:
    json.dump({"v": 1, "conversation": _C, "name": "ask_project",
               "cwd": _aproj, "config_dir": "", "account": "default",
               "flags": ["--model", "opus"], "last_active": time.time() - 30,
               "ended": None}, fh)
with open(os.path.join(_atdir, _C + ".jsonl"), "w") as fh:   # written now
    fh.write(json.dumps({"type": "user", "cwd": _aproj}) + "\n")
    fh.write(json.dumps({"type": "ai-title",
                         "aiTitle": "a chat still being written"}) + "\n")
_mark = os.path.join(SANDBOX, "claude-ran")
for _prog, _body in (("claude", f"touch '{_mark}'\n"), ("security", "exit 1\n"),
                     ("curl", "exit 1\n")):
    with open(os.path.join(_stubs, _prog), "w") as fh:
        fh.write("#!/bin/sh\n" + _body)
    os.chmod(os.path.join(_stubs, _prog), 0o755)
rc, fr = drive([_D, _D, _E, _E, _E, _E, b"q"], home=_ahome, size=(24, 80),
               env_extra={"CLAUDE_REAL": os.path.join(_stubs, "claude"),
                          "CLAUDE_MATE_USAGE_POLL_S": "0",
                          "PATH": _stubs + os.pathsep + os.environ["PATH"]})
_ask, _back = last_paint(fr[5]), last_paint(fr[6])
check("Resume on a chat whose transcript was written moments ago asks first: "
      "what it saw, with No where the cursor starts",
      "RESUME" in _txt(_ask) and "its transcript was written" in _txt(_ask)
      and "▸ No, leave it alone" in _txt(_ask), _txt(_ask)[-600:])
check("...the question wrapped to the width: no line wider than 79 columns",
      all(_cols(ln) <= 79 for ln in _ask),
      repr([ln for ln in _ask if _cols(ln) > 79][:2]))
check("...and No starts nothing: back on the list, claude never run",
      rc == 0 and "HISTORY" in _txt(_back) and not os.path.exists(_mark),
      _txt(_back)[-300:])
shutil.rmtree(SANDBOX, ignore_errors=True)

print(f"\n{'FAILED' if fails else 'all passed'}: "
      f"{fails} failure{'' if fails == 1 else 's'}")
sys.exit(1 if fails else 0)
