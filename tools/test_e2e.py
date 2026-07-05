#!/usr/bin/env python3
"""
End-to-end test of the Claude Mate daemon with NO hardware.

  - a PTY pretends to be the Arduino: we read what the daemon "displays" and
    write fake button/handshake bytes back to it
  - a stub `open`/`code`/`osascript` on PATH captures the FOCUS deep-link, so the
    must-have focus action is verified WITHOUT launching anything on this Mac
  - real hook lines are fed through the Unix socket, exactly like claude-status.sh
  - a fake PTY-wrapper control socket records every window op the daemon sends,
    so the "navigation NEVER touches windows / focus is raise-only" invariants
    are asserted, not assumed

Drives a scenario and asserts the F| frames (the single pre-rendered screen),
the V| LED lines, the triage-queue ordering, the press-grace wrong-target
guard, and the focus URI.

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

def saw_before(idx, pred):
    with display_lock:
        return any(pred(l) for l in display[:idx])

def wait_for(idx, pred, timeout=2.0):
    """Poll until a line matching pred appears at/after idx. Returns success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if saw_after(idx, pred):
            return True
        time.sleep(0.02)
    return False

def frame_subject(f):
    """Name field of an F|flash|name|info|fleet line ('' if malformed)."""
    parts = f.split("|")
    return parts[2] if len(parts) >= 5 else ""

def mark():
    with display_lock:
        return len(display)

# --- fake PTY-wrapper control socket: records every window op ------------------
ctrl_ops = []                     # (cmd, arrival time) in arrival order
ctrl_lock = threading.Lock()

def fake_ctrl(name):
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
            try:
                data = conn.recv(64).decode(errors="ignore").strip()
                if data:
                    with ctrl_lock:
                        ctrl_ops.append((data, time.monotonic()))
                conn.sendall(b"go\n")
                conn.sendall(b"ok\n")
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
    threading.Thread(target=loop, daemon=True).start()
    return path

time.sleep(2.5)

print("\n-- phase 1: empty boot -> MATE/no-sessions frame, LED cleared --")
time.sleep(0.5)

print("\n-- phase 2: one working session -> WORK frame + V|START --")
feed("working|sid-1|webapp||Opus 4.8|xhigh"); time.sleep(1.5)

print("\n-- phase 3: a session starts waiting -> auto-surfaces, flashes, V|INPUT --")
idx_wait = mark()
feed("waiting|sid-3|infra"); time.sleep(1.5)

print("\n-- phase 4: an API error arrives -> outranks waiting, V|ERROR --")
idx_err = mark()
feed("error|sid-2|api"); time.sleep(1.5)

print("\n-- phase 5: handshake H -> full resend (frame + re-armed LED loop) --")
idx_H = mark()
arduino_send("H"); time.sleep(1.5)

print("\n-- phase 6: PREV/NEXT browse the queue; no window ops, no acks --")
# Queue now: api(ERR,unacked) > infra(WAIT,unacked) > webapp(WORK).
idx_nav = mark()
arduino_send("B|N"); time.sleep(0.8)      # down: api -> infra
arduino_send("B|N"); time.sleep(0.8)      # down: infra -> webapp
arduino_send("B|P"); time.sleep(0.8)      # up:   webapp -> infra
with display_lock:
    nav_frames = [l for l in display[idx_nav:] if l.startswith("F|")]
# The 1 Hz ticker interleaves time-update frames of the SAME subject between
# the nav-driven frames; collapse consecutive repeats before checking the walk.
nav_subjects = []
for f in nav_frames:
    s = frame_subject(f)
    if not nav_subjects or nav_subjects[-1] != s:
        nav_subjects.append(s)

print("\n-- phase 7: GO acks + focuses the shown alert; next alert surfaces --")
# Subject is infra (from nav). GO must focus infra (deep link sid-3), ack it,
# and snap home -> api (still unacked ERR) becomes the subject.
idx_go = mark()
arduino_send("B|G"); time.sleep(1.5)
with display_lock:
    frames_after_go = [l for l in display[idx_go:] if l.startswith("F|")]
go_snapped_to_api = any(frame_subject(f) == "api" for f in frames_after_go)

print("\n-- phase 8: GO long (B|K) acks WITHOUT focusing; LED steps down --")
# Subject is api (ERR, unacked). B|K must ack it (no focus), snap home; with
# no unacked alerts left the LED must drop to V|OFF.
focus_before_k = ([l.strip() for l in open(focuslog)]
                  if os.path.exists(focuslog) else [])
idx_k = mark()
arduino_send("B|K"); time.sleep(1.5)
focus_after_k = ([l.strip() for l in open(focuslog)]
                 if os.path.exists(focuslog) else [])
k_no_focus = (focus_after_k == focus_before_k)
with display_lock:
    k_led_off = any(l == "V|OFF" for l in display[idx_k:])

print("\n-- phase 8b: a new alert must NOT steal the screen inside the "
      "interaction window (subject pinned; LED updates immediately) --")
# We pressed B|K moments ago, so the 10s interaction window is open and the
# subject is pinned. A fresh waiting alert must move the LED (V|INPUT) but NOT
# the shown subject.
idx_pin = mark()
feed("waiting|sidQ|quux"); time.sleep(2.0)
with display_lock:
    pin_frames = [l for l in display[idx_pin:] if l.startswith("F|")]
    pin_led = any(l == "V|INPUT" for l in display[idx_pin:])
pin_no_swap = all(frame_subject(f) != "quux" for f in pin_frames)
feed("end|sidQ|quux"); time.sleep(0.8)

print("\n-- phase 9: done alert + uniqueness truncation of long sibling names --")
# The subject is still PINNED (phase 8's interaction window), so the done
# alert won't surface until the window expires during phase 10's wait; the
# uniqueness assertion is computed there, scanning frames from this mark on.
idx_done = mark()
feed("done|sidA|project-alpha-one")
feed("working|sidB|project-alpha-two"); time.sleep(1.5)

print("\n-- phase 10: press-grace: autonomous subject swap right before GO --")
# Wait out the interaction window so the screen is free to move on its own,
# then land a fresh, worse alert (error) which auto-surfaces over the unacked
# 'done' alert being read -- and press GO immediately. The press must apply to
# the PREVIOUS subject (project-alpha-one, sidA), not the just-arrived beta.
print("   (waiting out the 10s interaction window...)")
time.sleep(10.5)
# The window expired mid-wait: the done alert auto-surfaced. Its frames carry
# the disambiguated sibling name (phase 9's assertion).
with display_lock:
    done_frames = [l for l in display[idx_done:] if l.startswith("F|")]
uniq_names = any("proj~a-one" in f for f in done_frames)
# The screen must now rest on the done alert (queue head, project-alpha-one).
idx_grace = mark()
feed("error|sidE|beta")
# Poll until the autonomous swap frame (flashing beta) is actually on the wire,
# then press IMMEDIATELY -- the poll interval keeps the press inside the 0.5s
# grace window, and a starved daemon fails loudly instead of passing vacuously.
grace_swap_seen = wait_for(idx_grace, lambda l: l.startswith("F|1|beta"))
arduino_send("B|G"); time.sleep(1.5)
focus_lines = [l.strip() for l in open(focuslog)] if os.path.exists(focuslog) else []
grace_focused_prev = any("session=sidA" in l for l in focus_lines)
grace_not_beta = not any("session=sidE" in l for l in focus_lines)

print("\n-- phase 11: navigation moves NO windows; GO sends ONLY 'focus' --")
# Attach ctrl sockets to SEVERAL sessions the browse will dwell on, so a
# reintroduced settle-timer preview (the old 0.45s terminal-follow) would be
# caught red-handed in nav_window_ops.
ctrl_w = fake_ctrl("webapp2")
ctrl_a = fake_ctrl("webapp")
ctrl_i = fake_ctrl("infra")
feed(f"working|sidW|webapp2|{ctrl_w}")
feed(f"working|sid-1|webapp|{ctrl_a}")
feed(f"waiting|sid-3|infra|{ctrl_i}")
time.sleep(1.2)
with ctrl_lock:
    ops_before_nav = len(ctrl_ops)
# Browse across the whole queue, dwelling longer than the old settle timer.
for ev in ("B|N", "B|N", "B|N", "B|P", "B|P"):
    arduino_send(ev); time.sleep(0.7)
time.sleep(1.0)
with ctrl_lock:
    nav_window_ops = ctrl_ops[ops_before_nav:]
# Now navigate TO webapp2 and GO: exactly one 'focus', never 'collapse'.
# Find it by pressing NEXT up to queue-size times, checking the subject.
focused_webapp2 = False
for _ in range(8):
    with display_lock:
        cur = [l for l in display if l.startswith("F|")]
    if cur and frame_subject(cur[-1]) == "webapp2":
        focused_webapp2 = True
        break
    arduino_send("B|N"); time.sleep(0.5)
with ctrl_lock:
    ops_before_go = len(ctrl_ops)   # the find-loop navs must have moved nothing
arduino_send("B|G"); time.sleep(2.0)
with ctrl_lock:
    go_ops = ctrl_ops[ops_before_go:]
    all_ops = list(ctrl_ops)
nav_moved_nothing = (len(nav_window_ops) == 0 and ops_before_go == ops_before_nav)

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

def wellformed(f):
    return len(f.split("|")) >= 5

# ---- the single-frame protocol ------------------------------------------------
check("empty boot shows the no-sessions frame (F|0|MATE|no sessions|)",
      saw(lambda l: l.startswith("F|0|MATE|no sessions")))
check("every F| frame is well-formed (5 fields)",
      all(wellformed(l) for l in display if l.startswith("F|")))
check("working session frame: webapp + WORK tag, not flashing",
      saw(lambda l: l.startswith("F|0|webapp|WORK") ))
check("model/effort rendered on the info row (Opus... xhigh)",
      saw(lambda l: l.startswith("F|") and "xhigh" in l and "Opus" in l))
check("waiting session auto-surfaces and flashes (F|1|infra|WAIT...)",
      saw_after(idx_wait, lambda l: l.startswith("F|1|infra|WAIT")))
check("error outranks waiting: api auto-surfaces flashing (F|1|api|ERR...)",
      saw_after(idx_err, lambda l: l.startswith("F|1|api|ERR")))
check("fleet strip shows the queue shape (!?> for err/wait/work)",
      saw(lambda l: l.startswith("F|") and "!?>" in l.split("|")[-1]))

# ---- LED (V|<KIND>) ------------------------------------------------------------
check("V|OFF clears the loop at startup (before the first alert)",
      saw_before(idx_wait, lambda l: l == "V|OFF"))
check("V|START blink when a job starts with nothing else pending",
      saw(lambda l: l == "V|START"))
check("V|INPUT loop when a session starts waiting", saw(lambda l: l == "V|INPUT"))
check("V|ERROR loop when an API error arrives", saw(lambda l: l == "V|ERROR"))
check("V|DONE loop when a turn finishes", saw(lambda l: l == "V|DONE"))
check("handshake H re-sends the frame", saw_after(idx_H, lambda l: l.startswith("F|")))
check("handshake H re-arms the active LED loop (V|ERROR re-sent after reset)",
      saw_after(idx_H, lambda l: l == "V|ERROR"))

# ---- navigation ----------------------------------------------------------------
def has_run(seq, run):
    return any(seq[i:i + len(run)] == run for i in range(len(seq)))
check("NEXT/PREV walk the queue (api -> infra -> webapp -> infra)",
      has_run(nav_subjects, ["infra", "webapp", "infra"]))
infra_navs = [f for f in nav_frames if frame_subject(f) == "infra"]
check("browsing acks nothing (infra still flashing when REVISITED)",
      bool(infra_navs) and infra_navs[-1].startswith("F|1|"))

# ---- GO / ACK triage sweep -----------------------------------------------------
check("GO focuses the shown session (deep link session=sid-3 for infra)",
      any("session=sid-3" in l for l in focus_lines))
check("GO snaps home to the next unacked alert (api surfaces)",
      go_snapped_to_api)
check("GO long (B|K) acknowledges WITHOUT focusing (no new open calls)",
      k_no_focus)
check("last unacked alert acked -> LED off (V|OFF)", k_led_off)

# ---- screen ownership (the 10s interaction window) -------------------------------
check("a new alert inside the interaction window does NOT steal the screen",
      pin_no_swap and bool(pin_frames))
check("...but the LED updates immediately (V|INPUT while pinned)", pin_led)

# ---- naming --------------------------------------------------------------------
check("sibling long names disambiguated (proj~a-one)", uniq_names)

# ---- press-grace wrong-target guard --------------------------------------------
check("press-grace: the autonomous beta swap frame rendered BEFORE GO was sent",
      grace_swap_seen)
check("press-grace: GO right after an autonomous swap acts on the PREVIOUS "
      "subject (focused sidA, the alert being read)", grace_focused_prev)
check("press-grace: the just-arrived alert was NOT focused (no session=sidE)",
      grace_not_beta)

# ---- window-op invariants (the reason for the redesign) -------------------------
check("navigation sends ZERO window ops to wrapper ctrl sockets "
      "(browse + find loops, dwelling on ctrl-socket sessions)",
      nav_moved_nothing)
check("GO on a wrapper session sends exactly one 'focus'",
      [c for (c, t) in go_ops] == ["focus"] if focused_webapp2 else False)
check("the daemon NEVER sends 'collapse' (raise-only, always; non-vacuous)",
      bool(all_ops) and all(c == "focus" for (c, t) in all_ops))

ok = all(checks)
print("\n  focus.log:", focus_lines or "(empty)")
print("  ctrl ops:", [(c) for (c, t) in all_ops] or "(none)")
print("  display frames:", len(display))
print("\n================", "ALL PASSED" if ok else "SOME FAILED", "================")
sys.exit(0 if ok else 1)
