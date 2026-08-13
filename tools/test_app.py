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
import struct
import subprocess
import sys
import termios
import time

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


# --------------------------------------------------------------------------- #
# The key decoder, driven through a real pipe
# --------------------------------------------------------------------------- #
def decode(payload, n=1):
    """Feed bytes down a pipe and read n keys back out of them."""
    r, w = os.pipe()
    os.write(w, payload)
    os.close(w)
    M._KEYBUF.clear()                      # the decoder's own buffer is global
    keys = [M.read_key(r, timeout=0.5) for _ in range(n)]
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

print(f"\n{'FAILED' if fails else 'all passed'}: "
      f"{fails} failure{'' if fails == 1 else 's'}")
sys.exit(1 if fails else 0)
