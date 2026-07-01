#!/usr/bin/env python3
"""
End-to-end test of the Claude Mate daemon with NO hardware.

  - a PTY pretends to be the Arduino: we read what the daemon "displays" and
    write fake button/handshake bytes back to it
  - a stub `open`/`code`/`osascript` on PATH captures the FOCUS deep-link, so the
    must-have focus action is verified WITHOUT launching anything on this Mac
  - real hook lines are fed through the Unix socket, exactly like claude-status.sh

Drives a scenario and asserts the status wheel (D|<WORD>), carousel cards, and
focus URI. The stepper itself lives only on the Arduino; the daemon just emits
"D|FREE|WIP|BLOCKED|WTF" lines, so this passes with no hardware.

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

def saw_after(idx, pred):
    with display_lock:
        return any(pred(l) for l in display[idx:])

time.sleep(2.5)

print("\n-- phase 1: one working session (expect WIP) --")
feed("working|sid-1|webapp"); time.sleep(2.0)

print("\n-- phase 2: FOCUS button, single session (deterministic) --")
arduino_send("B|1"); time.sleep(1.5)

print("\n-- phase 3: a session starts waiting for input (expect BLOCKED) --")
feed("waiting|sid-3|infra"); time.sleep(3.5)

print("\n-- phase 4: an API error arrives (expect WTF) --")
feed("error|sid-2|api"); time.sleep(3.5)

print("\n-- phase 5: handshake H -> full resend (must RE-ARM the motor loop) --")
# Snapshot the frame count so we can prove the handshake re-emits the looping
# alert haptic (a Nano reset/replug reboots motor-off; the daemon must re-arm it,
# else an unacknowledged error/done alert goes silent forever).
with display_lock:
    idx_before_H = len(display)
arduino_send("H"); time.sleep(1.5)

print("\n-- phase 5b: LIST mode, key-stable highlight across a re-sort --")
# 3 tabs live: sid-2 api(error), sid-3 infra(waiting), sid-1 webapp(working).
# ordered() = [api, infra, webapp]. Enter LIST (seeds on the shown card = api),
# NEXT to highlight infra (sid-3). Then RE-SORT the fleet (webapp -> error, so
# ordered() becomes [api, webapp, infra]) and SUBMIT: the highlight is tracked by
# KEY, so it must still focus infra (sid-3), NOT whatever slid into its old slot.
with display_lock:
    idx_before_list = len(display)
arduino_send("B|4"); time.sleep(1.2)     # MODE long -> LIST mode (expect T| frame)
arduino_send("B|2"); time.sleep(0.8)     # NEXT -> highlight infra (sid-3)
feed("error|sid-1|webapp"); time.sleep(1.5)   # re-sort: webapp jumps into slot 1
arduino_send("B|1"); time.sleep(1.2)     # SUBMIT -> must still focus infra (sid-3)
with display_lock:
    saw_T = any(l.startswith("T|") for l in display[idx_before_list:])
    list_totals = [int(l.split("|")[1]) for l in display[idx_before_list:]
                   if l.startswith("T|") and len(l.split("|")) > 1 and l.split("|")[1].isdigit()]
arduino_send("B|4"); time.sleep(1.2)     # MODE long -> back to SCROLL (S| resumes)
with display_lock:
    idx_after_scroll = len(display)
feed("working|sid-1|webapp"); time.sleep(1.5)  # nudge a refresh in scroll mode

print("\n-- phase 6: everything finishes (expect FREE) --")
feed("done|sid-2|api"); feed("done|sid-3|infra")
feed("done|sid-1|webapp"); time.sleep(3.0)

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
check("FREE at boot (D|FREE)", saw(lambda l: l == "D|FREE"))
check("WIP while working (D|WIP)", saw(lambda l: l == "D|WIP"))
check("BLOCKED on waiting session (D|BLOCKED)", saw(lambda l: l == "D|BLOCKED"))
check("WTF on API error (D|WTF)", saw(lambda l: l == "D|WTF"))
check("webapp working card", saw(lambda l: l.startswith("S|") and "|webapp|working|" in l))
check("infra waiting card", saw(lambda l: l.startswith("S|") and "|infra|waiting|" in l))
check("api error card", saw(lambda l: l.startswith("S|") and "|api|error|" in l))
check("FOCUS opened a vscode deep link",
      any("vscode://anthropic.claude-code/open?session=" in l for l in focus_lines))
check("FOCUS targeted live session sid-1",
      any("session=sid-1" in l for l in focus_lines))

# ---- haptics (V|<KIND>) ----------------------------------------------------
# The fake-Arduino reader captures V| lines too. Assert the new haptic model:
# a start tick, a needs-input tap, a looping error alert, a looping done alert,
# and that the daemon clears the firmware loop to a known state (V|OFF).
check("V|OFF clears the loop (startup / on error->done)",
      saw(lambda l: l == "V|OFF"))
check("V|START tick when a job starts with nothing else pending",
      saw(lambda l: l == "V|START"))
check("V|INPUT tap when a session starts waiting",
      saw(lambda l: l == "V|INPUT"))
check("V|ERROR loop when an API error arrives",
      saw(lambda l: l == "V|ERROR"))
check("V|DONE loop when a turn finishes (until acknowledged)",
      saw(lambda l: l == "V|DONE"))
check("handshake H re-arms the active loop haptic (V|ERROR/V|DONE re-sent after reset)",
      saw_after(idx_before_H, lambda l: l in ("V|ERROR", "V|DONE")))
check("MODE long-press (B|4) enters LIST mode -> T| frame sent", saw_T)
check("LIST frame lists all live tabs (total >= 3)",
      any(t >= 3 for t in list_totals))
check("SUBMIT in LIST focuses the highlighted tab BY KEY, stable across a re-sort -> sid-3",
      bool(focus_lines) and "session=sid-3" in focus_lines[-1])
check("MODE long-press again returns to SCROLL -> S| card resumes",
      saw_after(idx_after_scroll, lambda l: l.startswith("S|")))

ok = all(checks)
print("\n  focus.log:", [l.strip() for l in focus_lines] or "(empty)")
print("  display frames:", len(display))
print("\n================", "ALL PASSED" if ok else "SOME FAILED", "================")
sys.exit(0 if ok else 1)
