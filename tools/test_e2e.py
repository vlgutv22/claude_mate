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

print("\n-- phase 5b: LIST -- ack dot, double-click detail, key-stable focus, quiet --")
# 3 tabs live: sid-2 api(error,unacked), sid-3 infra(waiting,unacked), sid-1 webapp(working).
with display_lock:
    idx_before_list = len(display)
arduino_send("B|4"); time.sleep(1.2)     # MODE long -> LIST mode (T| with per-row ack)

def _row_acks(tline):                    # ack is the 4th ';' field of each row
    return [f.split(";")[-1] for f in tline.split("|")[3:] if f.count(";") >= 3]
with display_lock:
    t_frames = [l for l in display[idx_before_list:] if l.startswith("T|")]
saw_unacked_dot = any("0" in _row_acks(l) for l in t_frames)   # an unacked-alert row

arduino_send("B|2"); time.sleep(0.8)     # NEXT -> highlight infra (sid-3)
feed("error|sid-1|webapp"); time.sleep(1.5)   # re-sort: webapp jumps into slot 1

# Double-click SUBMIT -> open the highlighted tab's (infra) full card while in LIST.
with display_lock:
    idx_before_detail = len(display)
arduino_send("B|1"); time.sleep(0.15); arduino_send("B|1"); time.sleep(1.2)
with display_lock:
    detail_opened = any(l.startswith("S|") and "|infra|" in l
                        for l in display[idx_before_detail:])
# Double-click again -> close detail -> the T| list resumes.
with display_lock:
    idx_before_close = len(display)
arduino_send("B|1"); time.sleep(0.15); arduino_send("B|1"); time.sleep(1.2)
with display_lock:
    detail_closed = any(l.startswith("T|") for l in display[idx_before_close:])

# Single-click SUBMIT -> deferred focus of the highlighted tab; key-tracked to
# infra (sid-3) even though webapp slid into its old index slot.
arduino_send("B|1"); time.sleep(1.2)
# SUBMIT long -> ack the highlighted tab (already acked by the focus above; a
# redundant ack must be a harmless no-op).
arduino_send("B|5"); time.sleep(0.5)

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

print("\n-- phase 7: SCROLL ack stays on the acknowledged tab (no jump) --")
# Clean slate, then webapp2(working) + beta(done). beta is the top alert so it
# auto-surfaces as the current card. Acknowledging (SUBMIT) turns it done->idle,
# which re-sorts it BELOW webapp2 -- the view must STAY on beta, not jump to the
# top tab. (Last phase: on_ack's auto-surface pause can't affect earlier phases.)
for s in ("sid-1|webapp", "sid-2|api", "sid-3|infra"):
    feed("end|" + s)
time.sleep(1.0)
feed("working|sidW|webapp2"); feed("done|sidB|beta"); time.sleep(2.2)
with display_lock:
    idx_before_ack = len(display)
arduino_send("B|1"); time.sleep(1.5)          # SUBMIT: ack beta -> must stay on beta
with display_lock:
    cards_after_ack = [l for l in display[idx_before_ack:] if l.startswith("S|")]
first_card_after_ack = cards_after_ack[0] if cards_after_ack else ""

print("\n-- phase 8: LIST detail auto-closes when the detailed tab ends --")
# beta is the current card; enter LIST (seeds on beta), double-click to open its
# detail, then END beta: detail must auto-close back to the list (not stick on a
# stale card).
arduino_send("B|4"); time.sleep(1.2)          # SCROLL -> LIST (seeds highlight on beta)
arduino_send("B|1"); time.sleep(0.15); arduino_send("B|1"); time.sleep(1.0)  # dbl-click -> detail
with display_lock:
    idx_before_end = len(display)
feed("end|sidB|beta"); time.sleep(1.5)        # end the detailed tab
with display_lock:
    detail_autoclosed = any(l.startswith("T|") for l in display[idx_before_end:])

print("\n-- phase 9: SUBMIT long (B|5) acknowledges WITHOUT focusing --")
# Still in LIST mode (webapp2 working; the highlight re-anchored onto it when
# beta ended -- key-stable). A fresh 'done' tab sorts to row 0; move the
# highlight onto it, then B|5 must ack it (done -> idle, V|OFF) WITHOUT
# opening/focusing anything.
feed("done|sidG|gamma"); time.sleep(2.2)
arduino_send("B|3"); time.sleep(0.8)          # highlight up -> gamma (row 0)
focus_lines_before_b5 = ([l.strip() for l in open(focuslog)]
                         if os.path.exists(focuslog) else [])
with display_lock:
    idx_before_b5 = len(display)
arduino_send("B|5"); time.sleep(1.8)
with display_lock:
    b5_acked = any(l.startswith("T|") and "gamma;IDLE" in l
                   for l in display[idx_before_b5:])
    b5_led_off = any(l == "V|OFF" for l in display[idx_before_b5:])
focus_lines_after_b5 = ([l.strip() for l in open(focuslog)]
                        if os.path.exists(focuslog) else [])
b5_no_focus = (focus_lines_after_b5 == focus_lines_before_b5)

print("\n-- phase 10: LIST nav terminal-follow preview (focus new / collapse old) --")
# Still in LIST (webapp2 working, gamma idle; highlight on gamma from phase 9).
# Attach fake wrapper ctrl sockets to both (keepalive feeds, states unchanged),
# then move the highlight to webapp2: after the ~0.45s settle debounce the
# daemon must send 'focus' to webapp2's wrapper and 'collapse' to gamma's.
ctrl_cmds = {"webapp2": [], "gamma": []}
ctrl_lock = threading.Lock()

def _fake_ctrl(name):
    path = os.path.join(binhome, f"ctrl-{name}.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(4)
    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            with conn:
                try:
                    data = conn.recv(64).decode(errors="ignore").strip()
                except OSError:
                    data = ""
            if data:
                with ctrl_lock:
                    ctrl_cmds[name].append(data)
    threading.Thread(target=loop, daemon=True).start()
    return path

ctrl_w = _fake_ctrl("webapp2")
ctrl_g = _fake_ctrl("gamma")
feed(f"working|sidW|webapp2|{ctrl_w}")
feed(f"idle|sidG|gamma|{ctrl_g}")
time.sleep(1.2)
arduino_send("B|2"); time.sleep(1.5)          # highlight -> webapp2, settle > 0.45s
with ctrl_lock:
    preview_focus = "focus" in ctrl_cmds["webapp2"]
    preview_collapse = "collapse" in ctrl_cmds["gamma"]

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
      any("session=sid-3" in l for l in focus_lines))  # sid-3 is focused only in the LIST phase
check("LIST row carries an ack flag; an unacknowledged alert row = ;0 (blink dot)",
      saw_unacked_dot)
check("double-click SUBMIT in LIST opens the highlighted tab's detail card (infra)",
      detail_opened)
check("double-click SUBMIT again closes detail -> the T| list resumes",
      detail_closed)
check("LIST detail auto-closes when the detailed tab ends (no stuck stale card)",
      detail_autoclosed)
check("MODE long-press again returns to SCROLL -> S| card resumes",
      saw_after(idx_after_scroll, lambda l: l.startswith("S|")))
check("SCROLL: acknowledging a tab stays on it, no jump to the top tab "
      f"(first card after ack = beta, got {first_card_after_ack!r})",
      "|beta|" in first_card_after_ack)
check("SUBMIT long (B|5) acknowledges the highlighted tab (done -> IDLE row)",
      b5_acked)
check("SUBMIT long (B|5) silences the LED loop (V|OFF after ack)",
      b5_led_off)
check("SUBMIT long (B|5) does NOT focus (no new deep-link/open calls)",
      b5_no_focus)
check("LIST nav preview: newly highlighted tab's wrapper gets 'focus' (expand)",
      preview_focus)
check("LIST nav preview: previously expanded tab's wrapper gets 'collapse'",
      preview_collapse)

ok = all(checks)
print("\n  focus.log:", [l.strip() for l in focus_lines] or "(empty)")
print("  display frames:", len(display))
print("\n================", "ALL PASSED" if ok else "SOME FAILED", "================")
sys.exit(0 if ok else 1)
