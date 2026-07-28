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
the V| LED lines, the STICKY selection (the screen never switches tabs on its
own -- alerts signal via LED + blinking fleet letters), and the focus URI.

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

# What a fake wrapper hands back for the mirror ("screen") command, keyed by
# session name. More rows than the device shows, so the daemon's tail-and-clip
# is exercised rather than assumed: only the LAST MIRROR_ROWS should appear,
# and PREV must scroll further back to reveal MIRROR-TOP.
FAKE_SCREEN = {
    "folA": (["MIRROR-TOP line %02d" % i for i in range(30)]
             + ["$ pytest -q", "47 passed in 3.2s", "MIRROR-BOTTOM", "", ""]),
}

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
                if data.startswith("screen"):
                    # The mirror read is NOT a window op, so it is deliberately
                    # kept out of ctrl_ops -- that list exists to prove the
                    # daemon never touches windows while navigating, and
                    # recording reads there would break the invariant it backs.
                    rows = FAKE_SCREEN.get(name, [])
                    conn.sendall(("\n".join(["SCREEN %d" % len(rows)] + rows
                                            + ["ok"]) + "\n").encode())
                else:
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

print("\n-- phase 3: a session starts waiting -> V|INPUT + fleet blink; the "
      "screen STAYS on webapp --")
idx_wait = mark()
feed("waiting|sid-3|infra"); time.sleep(1.5)
with display_lock:
    wait_frames = [l for l in display[idx_wait:] if l.startswith("F|")]
wait_no_steal = bool(wait_frames) and all(frame_subject(f) == "webapp"
                                          for f in wait_frames)
wait_blink = any("b" in frame_fleet(f) for f in wait_frames)

print("\n-- phase 4: an API error arrives -> V|ERROR; the screen still STAYS --")
idx_err = mark()
feed("error|sid-2|api"); time.sleep(1.5)
with display_lock:
    err_frames = [l for l in display[idx_err:] if l.startswith("F|")]
err_no_steal = bool(err_frames) and all(frame_subject(f) == "webapp"
                                        for f in err_frames)
err_blink = any("e" in frame_fleet(f) for f in err_frames)

print("\n-- phase 5: handshake H -> full resend (frame + re-armed LED loop) --")
idx_H = mark()
arduino_send("H"); time.sleep(1.5)

print("\n-- phase 6: PREV/NEXT browse the queue; no window ops, no acks --")
# Queue (stable alphabetical): api, infra, webapp. The screen still sits on
# webapp -- the alerts never moved it.
idx_nav = mark()
arduino_send("B|P"); time.sleep(0.8)      # up:   webapp -> infra
arduino_send("B|P"); time.sleep(0.8)      # up:   infra  -> api
arduino_send("B|N"); time.sleep(0.8)      # down: api    -> infra
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

print("\n-- phase 8b: a new alert must NEVER steal the screen (no "
      "auto-surface; LED updates immediately) --")
# The subject is wherever the user left it (api). A fresh waiting alert must
# move the LED (V|INPUT) but NOT the shown subject.
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
# them distinct. The screen must NOT jump to the new done alert; phase 10
# asserts it is still on api even after a long idle.
feed("done|sidA|webapp-backend-service-one")
feed("working|sidB|webapp-backend-service-two"); time.sleep(1.5)

print("\n-- phase 10: selection is STICKY across an idle stretch + WYSIWYG --")
# The old 10s idle auto-surface is GONE: even after a long pause the done
# alert must NOT be surfaced -- the screen stays where the user left it (api).
print("   (idling 11s to prove nothing auto-surfaces...)")
time.sleep(11.0)
with display_lock:
    shown10 = frame_subject([l for l in display if l.startswith("F|")][-1])
sticky_idle = (shown10 == "api")
# Navigate TO the done sibling: its name renders middle-squeezed ('~') yet GO
# must focus EXACTLY it (deep link session=sidA), never a recomputed target.
found_squeezed = False
for _ in range(10):
    with display_lock:
        cur = [l for l in display if l.startswith("F|")]
    s = frame_subject(cur[-1]) if cur else ""
    if "~" in s and s.endswith("-one"):
        found_squeezed = True
        break
    arduino_send("B|N"); time.sleep(0.5)
focus_before10 = [l.strip() for l in open(focuslog)] if os.path.exists(focuslog) else []
arduino_send("B|G"); time.sleep(1.5)
focus_after10 = [l.strip() for l in open(focuslog)] if os.path.exists(focuslog) else []
new_focus10 = focus_after10[len(focus_before10):]
wysiwyg_squeezed = (found_squeezed
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
feed(f"waiting|sid-3|infra|{ctrl_i}")   # infra alerts again; webapp2 is calm
time.sleep(1.2)
with ctrl_lock:
    ops_before_nav = len(ctrl_ops)
# Browse across the whole queue, dwelling longer than the old settle timer.
for ev in ("B|N", "B|N", "B|N", "B|P", "B|P"):
    arduino_send(ev); time.sleep(0.7)
time.sleep(1.0)
with ctrl_lock:
    nav_window_ops = ctrl_ops[ops_before_nav:]
# Navigate TO webapp2 (a CALM working session, while infra has a WAIT alert
# elsewhere). This reproduces the user's bug: GO must raise webapp2 (what's
# shown), never a recomputed alert target, and STAY on webapp2 afterward.
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
# folA carries an ACCOUNT so the FOLLOW play-marker reservation is observable:
# while FOLLOW is on, r1 must end 2 columns early (the marker's cells).
feed(f"working|sidFA|folA|{fa}|||acct1")
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
    # While FOLLOW is on, the daemon keeps r1's last 2 columns blank for the
    # play triangle: folA's frame must still END with its account, 2 short.
    fol_on = [l for l in display[idx_f:]
              if l.startswith("F|") and frame_follow(l)]
follow_r1_reserved = any(frame_r1(f).endswith("acct1")
                         and len(frame_r1(f)) <= 19 for f in fol_on)
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

print("\n-- phase 12b: B|F (ACK held) toggles FOLLOW with no click window --")
# The 4-button device toggles FOLLOW with a single unambiguous verb instead of
# a double-click. FOLLOW is OFF here (the second double-click above turned it
# off), so ONE B|F must turn it on AND raise the shown terminal -- exactly what
# the double-click does -- and a second B|F must turn it back off. Sending one
# press rather than two also proves the daemon is not just counting B|G pairs.
idx_fb = mark()
with ctrl_lock:
    ops_before_fb = len(ctrl_ops)
arduino_send("B|F"); time.sleep(1.0)
with display_lock:
    fb_on_frame = any(frame_follow(l) for l in display[idx_fb:]
                      if l.startswith("F|"))
with ctrl_lock:
    fb_raised = ctrl_ops[ops_before_fb:]
idx_fb_off = mark()
arduino_send("B|F"); time.sleep(1.0)
with display_lock:
    fb_off_frames = [l for l in display[idx_fb_off:] if l.startswith("F|")]
fb_off_frame = bool(fb_off_frames) and not frame_follow(fb_off_frames[-1])

print("\n-- phase 12c: MIRROR (B|M) shows the session's real terminal --")
# Park on folA, the only fake wrapper carrying screen content, so these
# assertions are about the mirror and not about which session happens to be
# selected. FOLLOW is off here, so navigating raises nothing.
def shown_subject():
    with display_lock:
        frames = [l for l in display if l.startswith("F|")]
    return frame_subject(frames[-1]) if frames else None

def nav_to(subject, max_presses=8):
    """PREV until `subject` is on the glass. Navigation WRAPS, so a fixed press
    count lands somewhere that depends on where the previous phase left off."""
    for _ in range(max_presses):
        if shown_subject() == subject:
            return True
        arduino_send("B|P"); time.sleep(0.5)
    return shown_subject() == subject

parked_on_folA = nav_to("folA")
with ctrl_lock:
    ops_before_mirror = len(ctrl_ops)
idx_m = mark()
arduino_send("B|M"); time.sleep(1.5)
with display_lock:
    m_lines = [l for l in display[idx_m:] if l.startswith("M|")]
mirror_titled = any(l.startswith("M|T|") for l in m_lines)
mirror_ended = any(l == "M|END" for l in m_lines)
# The fake screen is 35 rows for a 17-row view, so the daemon must show the
# TAIL: the newest output is what matters when you glance at a session.
mirror_tail = any("MIRROR-BOTTOM" in l for l in m_lines)
mirror_clipped = not any("MIRROR-TOP line 00" in l for l in m_lines)

# PREV must scroll the view, NOT move the selection.
idx_ms = mark()
for _ in range(8):
    arduino_send("B|P"); time.sleep(0.3)
time.sleep(1.2)
with display_lock:
    scroll_lines = [l for l in display[idx_ms:] if l.startswith("M|")]
mirror_scrolled = any("MIRROR-TOP" in l for l in scroll_lines)

idx_moff = mark()
arduino_send("B|M"); time.sleep(1.5)
with display_lock:
    off_lines = [l for l in display[idx_moff:] if l.startswith("M|")]
    after_off = [l for l in display[idx_moff:] if l.startswith("F|")]
mirror_closed = any(l == "M|OFF" for l in off_lines)
# Eight PREVs while mirroring must have scrolled, not navigated: the frame that
# comes back has to be the SAME session the view was opened on.
mirror_kept_subject = bool(after_off) and frame_subject(after_off[-1]) == "folA"
with ctrl_lock:
    mirror_ops = ctrl_ops[ops_before_mirror:]
mirror_no_window_ops = (len(mirror_ops) == 0)

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
check("a waiting alert does NOT steal the screen (stays on webapp)",
      wait_no_steal)
check("...it blinks its fleet letter instead (lowercase 'b')", wait_blink)
check("an error alert does NOT steal the screen either (still webapp)",
      err_no_steal)
check("...it blinks its fleet letter instead (lowercase 'e')", err_blink)
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
check("NEXT/PREV walk the queue (webapp -> infra -> api -> infra)",
      has_run(nav_subjects, ["infra", "api", "infra"]))
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

# ---- screen ownership (STICKY selection: the screen never moves itself) ---------
check("a new alert NEVER steals the screen (no auto-surface)",
      pin_no_swap and bool(pin_frames))
check("...but the LED updates immediately (V|INPUT while elsewhere)", pin_led)
check("selection survives an 11s idle stretch (done alert did NOT surface; "
      "still on api)", sticky_idle)

# ---- naming --------------------------------------------------------------------
check("sibling long names disambiguated (middle '~' squeeze)", found_squeezed)

# ---- WYSIWYG: GO acts on EXACTLY the session on the glass (the user's bug) ------
check("GO on the shown squeezed sibling focuses exactly it (-> sidA)",
      wysiwyg_squeezed)
check("GO on a shown CALM session focuses IT, not the urgent alert elsewhere "
      "(webapp2 raised while infra waits)",
      [c for (c, t) in go_ops] == ["focus"] if focused_webapp2 else False)
check("focusing a calm session STAYS on it (no jump elsewhere)",
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
check("FOLLOW reserves the play-marker cells: r1 ends 2 columns early with "
      "the account intact (<=19 chars, ends 'acct1')", follow_r1_reserved)
check("turning FOLLOW on raises the shown terminal immediately",
      any(c == "focus" for (c, t) in dbl_raised))
check("in FOLLOW, PREV/NEXT auto-raise the selected terminal (raise only)",
      follow_nav_raised)
check("double-click GO again turns FOLLOW OFF",
      follow_off_frame)
check("with FOLLOW off, navigation raises NOTHING",
      follow_off_no_raise)

# ---- FOLLOW mode via the 4-button device's dedicated verb (B|F) -----------------
check("B|F (ACK held) turns FOLLOW ON from a SINGLE press -- no click window",
      fb_on_frame)
check("B|F turning FOLLOW on raises the shown terminal, like the double-click",
      any(c == "focus" for (c, t) in fb_raised))
check("a second B|F turns FOLLOW OFF again",
      fb_off_frame)

# ---- MIRROR: the terminal view -------------------------------------------------
check("the mirror phase actually parked on folA (nav wraps, so this is "
      "asserted rather than assumed)", parked_on_folA)
check("B|M opens the mirror (title row + M|END terminator)",
      mirror_titled and mirror_ended)
check("the mirror shows the TAIL of a screen taller than the view "
      "(newest output visible)", mirror_tail)
check("...and clips the rest rather than sending all 35 rows",
      mirror_clipped)
check("PREV scrolls the mirror back into history (reveals MIRROR-TOP)",
      mirror_scrolled)
check("B|M again closes it (M|OFF)", mirror_closed)
check("scrolling the mirror did NOT move the selection (still folA)",
      mirror_kept_subject)
check("looking at a terminal NEVER raises it (zero window ops while mirrored)",
      mirror_no_window_ops)

# ---- stable tab order ----------------------------------------------------------
check("tab order is stable/alphabetical -- an error does NOT shuffle it to front",
      stable_order)

ok = all(checks)
print("\n  focus.log:", focus_lines or "(empty)")
print("  ctrl ops:", [(c) for (c, t) in all_ops] or "(none)")
print("  display frames:", len(display))
print("\n================", "ALL PASSED" if ok else "SOME FAILED", "================")
sys.exit(0 if ok else 1)
