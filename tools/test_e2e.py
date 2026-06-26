#!/usr/bin/env python3
"""
End-to-end test of the Claude Mate daemon with NO hardware.

  - a PTY pretends to be the Arduino: we read what the daemon "displays" and
    write fake button/handshake bytes back to it
  - a stub `open`/`code`/`osascript` on PATH captures the FOCUS deep-link, so the
    must-have focus action is verified WITHOUT launching anything on this Mac
  - real hook lines are fed through the Unix socket, exactly like claude-status.sh

Drives a scenario and asserts the traffic light, carousel cards, and focus URI.

Run:   python3 tools/test_e2e.py      (needs pyserial: pip install pyserial)
"""
import os, pty, tty, sys, time, socket, threading, subprocess, tempfile, textwrap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMON = os.path.join(REPO, "daemon", "claude_mate_daemon.py")

try:
    import serial  # noqa: F401  (the daemon needs it; fail early & clearly)
except ImportError:
    print("SKIP: pyserial not installed. Run: pip install pyserial")
    sys.exit(0)

# --- stub bin dir so FOCUS's open/code/osascript don't launch anything --------
binhome = tempfile.mkdtemp(prefix="cm-bin-")
focuslog = os.path.join(binhome, "focus.log")
for name in ("open", "code", "osascript"):
    p = os.path.join(binhome, name)
    with open(p, "w") as fh:
        fh.write(textwrap.dedent(f"""\
            #!/bin/bash
            echo "{name} $*" >> "{focuslog}"
            exit 0
        """))
    os.chmod(p, 0o755)

sock = os.path.join(binhome, "cm.sock")

# --- fake Arduino over a PTY --------------------------------------------------
master_fd, slave_fd = pty.openpty()
slave_name = os.ttyname(slave_fd)
tty.setraw(master_fd)

display = []
display_lock = threading.Lock()

def pty_reader():
    buf = b""
    while True:
        try:
            data = os.read(master_fd, 1024)
        except OSError:
            break
        if not data:
            break
        buf += data
        while b"\n" in buf:
            ln, buf = buf.split(b"\n", 1)
            s = ln.decode(errors="replace").strip()
            if s:
                with display_lock:
                    display.append(s)
                print(f"   OLED <= {s}")
threading.Thread(target=pty_reader, daemon=True).start()

env = dict(os.environ)
env["PATH"] = binhome + os.pathsep + env.get("PATH", "")
env["CLAUDE_MATE_PORT"] = slave_name
env["CLAUDE_MATE_SOCK"] = sock
env["CLAUDE_MATE_BAUD"] = "115200"

print(f"== starting daemon (fake port {slave_name}) ==")
proc = subprocess.Popen([sys.executable, DAEMON], env=env,
                        stderr=subprocess.PIPE, text=True)

def err_reader():
    for ln in proc.stderr:
        print(f"   [daemon] {ln.rstrip()}")
threading.Thread(target=err_reader, daemon=True).start()

def feed(line):
    for _ in range(50):
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(sock); c.send((line + "\n").encode()); c.close()
            print(f"   HOOK => {line}")
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("socket never came up")

def arduino_send(s):
    print(f"   BTN  => {s}")
    os.write(master_fd, (s + "\n").encode())

def saw(pred):
    with display_lock:
        return any(pred(l) for l in display)

time.sleep(2.5)

print("\n-- phase 1: one working session (expect YELLOW) --")
feed("working|sid-1|webapp"); time.sleep(2.0)

print("\n-- phase 2: FOCUS button, single session (deterministic) --")
arduino_send("B|1"); time.sleep(1.5)

print("\n-- phase 3: an API error arrives (expect RED) --")
feed("error|sid-2|api"); time.sleep(3.5)

print("\n-- phase 4: handshake H -> full resend --")
arduino_send("H"); time.sleep(1.5)

print("\n-- phase 5: everything finishes (expect GREEN) --")
feed("done|sid-2|api"); feed("done|sid-1|webapp"); time.sleep(3.0)

proc.terminate()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()

focus_lines = [l.strip() for l in open(focuslog)] if os.path.exists(focuslog) else []

print("\n================ ASSERTIONS ================")
checks = []
def check(name, ok):
    checks.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

check("idle frame at boot (I)", saw(lambda l: l == "I"))
check("GREEN at boot (L|G)", saw(lambda l: l == "L|G"))
check("YELLOW while working (L|Y)", saw(lambda l: l == "L|Y"))
check("RED on API error (L|R)", saw(lambda l: l == "L|R"))
check("webapp working card", saw(lambda l: l.startswith("S|") and "|webapp|working|" in l))
check("api error card", saw(lambda l: l.startswith("S|") and "|api|error|" in l))
check("FOCUS opened a vscode deep link",
      any("vscode://anthropic.claude-code/open?session=" in l for l in focus_lines))
check("FOCUS targeted live session sid-1",
      any("session=sid-1" in l for l in focus_lines))

ok = all(checks)
print("\n  focus.log:", [l.strip() for l in focus_lines] or "(empty)")
print("  display frames:", len(display))
print("\n================", "ALL PASSED" if ok else "SOME FAILED", "================")
sys.exit(0 if ok else 1)
