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
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unicodedata

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
                              capture_output=True, text=True, timeout=30)


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


def drive(keystrokes, settle=0.45, warmup=1.5):
    m_in, s_in = pty.openpty()
    m_out, s_out = pty.openpty()
    # GIVE THE PTY A SIZE. openpty() leaves the window at 0x0, and a program
    # that trusts os.get_terminal_size() then renders into a zero-row screen --
    # which is exactly how the app came to be silently dropping its last line.
    # A real terminal always has a size, so the test should provide one too;
    # the app now also floors it, and both halves are worth keeping.
    fcntl.ioctl(s_out, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 100, 0, 0))
    env = dict(os.environ)
    # A socket that cannot exist, so the app runs in its no-daemon shape and
    # this test never depends on a daemon being up on the machine running it.
    env["CLAUDE_MATE_SOCK"] = "/tmp/claude-mate-test-nonexistent.sock"
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

    frames = [drain(warmup)]
    for k in keystrokes:
        os.write(m_in, k)
        time.sleep(settle)
        frames.append(drain(settle))
    os.write(m_in, b"q")
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

# --------------------------------------------------------------------------- #
# The shared chooser, and the picker every session goes through
# --------------------------------------------------------------------------- #
# THE LEVELS UNDER THE TOP ONE are the point of claude_mate_ui existing: the app
# had arrow keys and choosing an account still meant typing a number. These
# drive the real chooser and the real wrapper picker under a pty, because the
# whole class of bug here (escape sequences, terminal state, unprintable names)
# only exists on a terminal.
print("\n== the shared chooser ==")


def drive_snippet(code, keystrokes, accounts_dir=None, warmup=1.5, settle=0.45):
    """Run a python snippet under its own pty and feed it keys."""
    m_in, s_in = pty.openpty()
    m_out, s_out = pty.openpty()
    fcntl.ioctl(s_out, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 110, 0, 0))
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
        os.write(m_in, k)
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

print(f"\n{'FAILED' if fails else 'all passed'}: "
      f"{fails} failure{'' if fails == 1 else 's'}")
sys.exit(1 if fails else 0)
