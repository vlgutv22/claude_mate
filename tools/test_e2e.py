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

# F|<flags>|<sel>|<r0>|<r1>|<r2>|<r3>
def frame_flags(f):
    p = f.split("|")
    return int(p[1]) if len(p) >= 7 and p[1].lstrip("-").isdigit() else 0
def frame_flash(f):  return bool(frame_flags(f) & 1)
def frame_follow(f): return bool(frame_flags(f) & 2)
def frame_sel(f):
    p = f.split("|")
    return int(p[2]) if len(p) >= 7 and p[2].lstrip("-").isdigit() else -99
def frame_subject(f):
    """Name field (r0) of an F| line ('' if malformed)."""
    p = f.split("|")
    return p[3] if len(p) >= 7 else ""
def frame_r1(f):
    p = f.split("|")
    return p[4] if len(p) >= 7 else ""
def frame_r2(f):
    p = f.split("|")
    return p[5] if len(p) >= 7 else ""
def frame_fleet(f):
    """Fleet field (r3, last), kept intact incl. its '|' dividers."""
    p = f.split("|", 6)
    return p[6] if len(p) >= 7 else ""

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
feed("working|sid-1|webapp||Opus 4.8|xhigh|work|5h82%"); time.sleep(1.5)

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

print("\n-- phase 7: GO acks + focuses the shown alert, then STAYS on it --")
# Subject is infra (from nav). GO must focus infra (deep link sid-3), ack it,
# and STAY on infra (no auto-switch to another tab).
idx_go = mark()
arduino_send("B|G"); time.sleep(1.5)
with display_lock:
    frames_after_go = [l for l in display[idx_go:] if l.startswith("F|")]
go_stayed = bool(frames_after_go) and frame_subject(frames_after_go[-1]) == "infra"

print("\n-- phase 8: GO long (B|K) acks the shown alert WITHOUT focusing; stays --")
# infra is now acked. Navigate to the remaining unacked alert (api, ERR), then
# B|K acks it (no focus) and stays on api; with no unacked alerts left the LED
# drops to V|OFF.
for _ in range(6):
    with display_lock:
        cur = [l for l in display if l.startswith("F|")]
    if cur and frame_subject(cur[-1]) == "api":
        break
    arduino_send("B|N"); time.sleep(0.6)
focus_before_k = ([l.strip() for l in open(focuslog)]
                  if os.path.exists(focuslog) else [])
idx_k = mark()
arduino_send("B|K"); time.sleep(1.5)
focus_after_k = ([l.strip() for l in open(focuslog)]
                 if os.path.exists(focuslog) else [])
k_no_focus = (focus_after_k == focus_before_k)
with display_lock:
    k_frames = [l for l in display[idx_k:] if l.startswith("F|")]
    k_led_off = any(l == "V|OFF" for l in display[idx_k:])
k_stayed = bool(k_frames) and frame_subject(k_frames[-1]) == "api"

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

print("\n-- phase 9: done alert + uniqueness squeeze of long sibling names --")
# Names >21 chars sharing a long prefix: they collide when truncated to the row
# width, so the daemon middle-squeezes them (first 9 + '~' + last 10) to keep
# them distinct. The subject is still PINNED (phase 8's window), so the done
# alert surfaces only after the window expires in phase 10.
idx_done = mark()
feed("done|sidA|webapp-backend-service-one")
feed("working|sidB|webapp-backend-service-two"); time.sleep(1.5)

print("\n-- phase 10: WYSIWYG -- GO focuses EXACTLY the shown session + uniqueness --")
# Wait out the interaction window so the done alert auto-surfaces as the head.
print("   (waiting out the 10s interaction window...)")
time.sleep(10.5)
with display_lock:
    done_frames = [l for l in display[idx_done:] if l.startswith("F|")]
    shown10 = frame_subject([l for l in display if l.startswith("F|")][-1])
# The colliding siblings render squeezed (contain '~'); the head's name is one.
uniq_names = ("~" in shown10) and any("~" in frame_subject(f) for f in done_frames)
# The done alert (webapp-backend-service-one, sidA) is the head and on the glass.
# GO must focus EXACTLY it (deep link session=sidA), never a recomputed target.
focus_before10 = [l.strip() for l in open(focuslog)] if os.path.exists(focuslog) else []
arduino_send("B|G"); time.sleep(1.5)
focus_after10 = [l.strip() for l in open(focuslog)] if os.path.exists(focuslog) else []
new_focus10 = focus_after10[len(focus_before10):]
wysiwyg_head = ("~" in shown10
                and any("session=sidA" in l for l in new_focus10))

print("\n-- phase 11: WYSIWYG on a NON-head calm session; nav moves NO windows --")
# Attach ctrl sockets to SEVERAL sessions the browse will dwell on, so a
# reintroduced settle-timer preview (the old 0.45s terminal-follow) would be
# caught red-handed in nav_window_ops.
ctrl_w = fake_ctrl("webapp2")
ctrl_a = fake_ctrl("webapp")
ctrl_i = fake_ctrl("infra")
feed(f"working|sidW|webapp2|{ctrl_w}")
feed(f"working|sid-1|webapp|{ctrl_a}")
feed(f"waiting|sid-3|infra|{ctrl_i}")   # infra is the ALERT head; webapp2 is calm
time.sleep(1.2)
with ctrl_lock:
    ops_before_nav = len(ctrl_ops)
# Browse across the whole queue, dwelling longer than the old settle timer.
for ev in ("B|N", "B|N", "B|N", "B|P", "B|P"):
    arduino_send(ev); time.sleep(0.7)
time.sleep(1.0)
with ctrl_lock:
    nav_window_ops = ctrl_ops[ops_before_nav:]
# Navigate TO webapp2 (a CALM working session that is NOT the head -- the head
# is the infra WAIT alert). This reproduces the user's bug: GO must raise
# webapp2 (what's shown), never the head alert, and STAY on webapp2 afterward.
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
idx_go11 = mark()
arduino_send("B|G"); time.sleep(2.0)
with ctrl_lock:
    go_ops = ctrl_ops[ops_before_go:]
    all_ops = list(ctrl_ops)
nav_moved_nothing = (len(nav_window_ops) == 0 and ops_before_go == ops_before_nav)
# Calm focus -> the screen STAYS on webapp2 (does not jump to the infra alert).
with display_lock:
    frames_after_go11 = [l for l in display[idx_go11:] if l.startswith("F|")]
stayed_on_calm = (bool(frames_after_go11)
                  and frame_subject(frames_after_go11[-1]) == "webapp2")

print("\n-- phase 12: double-click GO toggles FOLLOW; nav auto-raises in FOLLOW --")
# Fresh clean slate of 3 working sessions, all with ctrl sockets so any raise is
# observable. End the accumulated sessions first.
for s in ("sid-1|webapp", "sid-2|api", "sid-3|infra", "sidW|webapp2",
          "sidA|webapp-backend-service-one", "sidB|webapp-backend-service-two"):
    feed("end|" + s)
time.sleep(1.0)
fa, fb, fc = fake_ctrl("folA"), fake_ctrl("folB"), fake_ctrl("folC")
feed(f"working|sidFA|folA|{fa}")
feed(f"working|sidFB|folB|{fb}")
feed(f"working|sidFC|folC|{fc}")
time.sleep(1.2)
# Baseline: FOLLOW is off (no frame has the follow bit).
idx_f = mark()
with display_lock:
    follow_off_at_start = not any(frame_follow(l) for l in display[idx_f:]
                                  if l.startswith("F|"))
# Double-click GO (two presses inside DOUBLE_CLICK_S) -> FOLLOW ON.
with ctrl_lock:
    ops_before_dbl = len(ctrl_ops)
arduino_send("B|G"); time.sleep(0.10); arduino_send("B|G"); time.sleep(1.0)
with display_lock:
    follow_on_frame = any(frame_follow(l) for l in display[idx_f:] if l.startswith("F|"))
with ctrl_lock:
    dbl_raised = ctrl_ops[ops_before_dbl:]        # toggle-on raises the shown one
# In FOLLOW, a nav must auto-raise the newly-shown terminal after the settle.
with ctrl_lock:
    ops_before_fnav = len(ctrl_ops)
arduino_send("B|N"); time.sleep(0.8)              # > FOLLOW_SETTLE_S
with ctrl_lock:
    follow_nav_ops = ctrl_ops[ops_before_fnav:]
follow_nav_raised = any(c == "focus" for (c, t) in follow_nav_ops)
# Double-click again -> FOLLOW OFF; a nav must then raise NOTHING.
idx_off = mark()
arduino_send("B|G"); time.sleep(0.10); arduino_send("B|G"); time.sleep(1.0)
with display_lock:
    off_frames = [l for l in display[idx_off:] if l.startswith("F|")]
follow_off_frame = bool(off_frames) and not frame_follow(off_frames[-1])
with ctrl_lock:
    ops_before_offnav = len(ctrl_ops)
arduino_send("B|N"); time.sleep(0.8)
with ctrl_lock:
    off_nav_ops = ctrl_ops[ops_before_offnav:]
follow_off_no_raise = (len(off_nav_ops) == 0)

print("\n-- phase 13: tab ORDER is stable (alphabetical), never urgency-shuffled --")
# Clean slate, then two sessions whose alphabetical order (apple < zebra) is the
# OPPOSITE of their urgency once zebra errors. The strip must keep apple first.
for s in ("sidFA|folA", "sidFB|folB", "sidFC|folC"):
    feed("end|" + s)
time.sleep(1.0)
feed("working|sidAP|apple")
feed("working|sidZE|zebra"); time.sleep(1.2)
feed("error|sidZE|zebra"); time.sleep(1.5)          # zebra now most urgent
with display_lock:
    order_fleets = [frame_fleet(l) for l in display if l.startswith("F|")]
# On a 2-session strip, apple(working=W) must come BEFORE zebra(error, unacked
# -> lowercase e) -- alphabetical, NOT urgency (which would put e first).
stable_order = False
for fl in reversed(order_fleets):
    parts = fl.split()
    if parts and parts[0].endswith("/2"):
        letters = [c for c in "".join(parts[1:]) if c.isalpha()]
        if letters == ["W", "e"]:
            stable_order = True
            break

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
    return len(f.split("|")) >= 7          # F|flags|sel|r0|r1|r2|r3 (r3 may add more)

# ---- the single-frame protocol ------------------------------------------------
check("empty boot shows the no-sessions frame (MATE / no sessions)",
      saw(lambda l: l.startswith("F|") and frame_subject(l) == "MATE"))
check("every F| frame is well-formed (>=7 fields)",
      all(wellformed(l) for l in display if l.startswith("F|")))
check("working session frame: webapp name (r0) + WORK tag (r1), not flashing",
      saw(lambda l: frame_subject(l) == "webapp" and frame_r1(l).startswith("WORK")
          and not frame_flash(l)))
check("model/effort rendered on its own row (Opus... xhigh)",
      saw(lambda l: l.startswith("F|") and "xhigh" in l and "Opus" in l))
check("account rendered right-aligned on the state row (r1 ends ' work')",
      saw(lambda l: frame_subject(l) == "webapp"
          and frame_r1(l).endswith(" work")))
check("remaining-limit chip right-aligned on the meta row (r2 = model+effort "
      "+ '5h82%')",
      saw(lambda l: frame_subject(l) == "webapp" and "Opus" in frame_r2(l)
          and frame_r2(l).endswith(" 5h82%")))
check("waiting session auto-surfaces and flashes (infra/WAIT, flash bit set)",
      saw_after(idx_wait, lambda l: frame_subject(l) == "infra"
                and frame_r1(l).startswith("WAIT") and frame_flash(l)))
check("error outranks waiting: api auto-surfaces flashing (api/ERR)",
      saw_after(idx_err, lambda l: frame_subject(l) == "api"
                and frame_r1(l).startswith("ERR") and frame_flash(l)))
check("fleet row: status letters (E/B/W/D/I), space-separated, no '|'",
      saw(lambda l: l.startswith("F|") and "|" not in frame_fleet(l)
          and any(c in frame_fleet(l).upper() for c in "EBWDI")))
check("active-tab box: sel points at a fleet LETTER in r3",
      saw(lambda l: l.startswith("F|") and 0 <= frame_sel(l) < len(frame_fleet(l))
          and frame_fleet(l)[frame_sel(l)].isalpha()))
check("unacked alerts show as LOWERCASE in the strip (blink); acked/calm upper",
      saw_after(idx_err, lambda l: l.startswith("F|")
                and any(c.islower() for c in frame_fleet(l))))

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
      bool(infra_navs) and frame_flash(infra_navs[-1]))

# ---- GO / ACK triage sweep -----------------------------------------------------
check("GO focuses the shown session (deep link session=sid-3 for infra)",
      any("session=sid-3" in l for l in focus_lines))
check("GO STAYS on the acked tab, no auto-switch (still infra)", go_stayed)
check("GO long (B|K) acknowledges WITHOUT focusing (no new open calls)",
      k_no_focus)
check("GO long (B|K) STAYS on the acked tab (still api)", k_stayed)
check("last unacked alert acked -> LED off (V|OFF)", k_led_off)

# ---- screen ownership (the 10s interaction window) -------------------------------
check("a new alert inside the interaction window does NOT steal the screen",
      pin_no_swap and bool(pin_frames))
check("...but the LED updates immediately (V|INPUT while pinned)", pin_led)

# ---- naming --------------------------------------------------------------------
check("sibling long names disambiguated (middle '~' squeeze)", uniq_names)

# ---- WYSIWYG: GO acts on EXACTLY the session on the glass (the user's bug) ------
check("GO on the shown head alert focuses exactly it (shown sibling -> sidA)",
      wysiwyg_head)
check("GO on a shown NON-head calm session focuses IT, not the head alert "
      "(webapp2 raised while infra is the head)",
      [c for (c, t) in go_ops] == ["focus"] if focused_webapp2 else False)
check("focusing a calm session STAYS on it (no jump to the queue head)",
      stayed_on_calm)

# ---- window-op invariants (the reason for the redesign) -------------------------
check("navigation sends ZERO window ops to wrapper ctrl sockets "
      "(browse + find loops, dwelling on ctrl-socket sessions)",
      nav_moved_nothing)
check("the daemon NEVER sends 'collapse' (raise-only, always; non-vacuous)",
      bool(all_ops) and all(c == "focus" for (c, t) in all_ops))

# ---- FOLLOW mode (double-click GO) ---------------------------------------------
check("FOLLOW starts OFF (no follow bit before the double-click)",
      follow_off_at_start)
check("double-click GO turns FOLLOW ON (follow bit set in frames)",
      follow_on_frame)
check("turning FOLLOW on raises the shown terminal immediately",
      any(c == "focus" for (c, t) in dbl_raised))
check("in FOLLOW, PREV/NEXT auto-raise the selected terminal (raise only)",
      follow_nav_raised)
check("double-click GO again turns FOLLOW OFF",
      follow_off_frame)
check("with FOLLOW off, navigation raises NOTHING",
      follow_off_no_raise)

# ---- stable tab order ----------------------------------------------------------
check("tab order is stable/alphabetical -- an error does NOT shuffle it to front",
      stable_order)

ok = all(checks)
print("\n  focus.log:", focus_lines or "(empty)")
print("  ctrl ops:", [(c) for (c, t) in all_ops] or "(none)")
print("  display frames:", len(display))
print("\n================", "ALL PASSED" if ok else "SOME FAILED", "================")
sys.exit(0 if ok else 1)
