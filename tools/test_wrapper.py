#!/usr/bin/env python3
"""
Validate bin/claude-mate-wrap with NO real claude and NO hardware:
a staged fake-claude TUI drives the wrapper through working/waiting/error/idle,
and we assert the wrapper reports each state to the daemon socket, carries its
focus control-socket, deregisters on exit ('end'), and dispatches FOCUS
(osascript stubbed so nothing real is touched).

Then dynamic workflows: a fake claude draws two workflow rows under the prompt
box, ticks their clocks, completes one of them, and hides both. Every report
must carry nine fields; the rows must reach the daemon as field-8 records; a
run completing must be reported at once (its shape changed) while ticking
clocks are no reason to report; and '-' must follow only once WorkflowWatch's
hold for hidden rows is over (cut from 20 s to HOLD for the test), the
session staying working until then. Last, a run that has finished, its row
hidden by the '/' list for a moment and then shown again: it must stay
listed throughout, '-' only once Claude has dropped the row -- a '-' while
it was merely out of view made it a new run to the daemon when it came back.

Run:  python3 tools/test_wrapper.py     (needs pyte: pip install pyte)
"""
import os, pty, socket, threading, subprocess, sys, time, tempfile, textwrap, \
       re, fcntl, termios, struct, shutil

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAP = os.path.join(REPO, "bin", "claude-mate-wrap")

try:
    import pyte  # noqa: F401  (the wrapper needs it)
except ImportError:
    print("SKIP: pyte not installed. Run: pip install pyte")
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="cmw-")
sock = os.path.join(tmp, "d.sock")
osa_log = os.path.join(tmp, "osa.log")

received = []
def server():
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); srv.bind(sock); srv.listen(16)
    while True:
        try: conn, _ = srv.accept()
        except OSError: break
        with conn:
            d = conn.recv(512).decode(errors="ignore").strip()
        if d:
            received.append(d); print("  DAEMON <=", d)
threading.Thread(target=server, daemon=True).start()

stubdir = os.path.join(tmp, "bin"); os.makedirs(stubdir)
osa = os.path.join(stubdir, "osascript")
open(osa, "w").write(f'#!/bin/bash\necho called >> "{osa_log}"\nexit 0\n'); os.chmod(osa, 0o755)

fake = os.path.join(tmp, "fake-claude")
open(fake, "w").write(textwrap.dedent('''\
    #!/usr/bin/env python3
    import time, sys
    def screen(s):
        sys.stdout.write("\\x1b[2J\\x1b[H" + s + "\\n"); sys.stdout.flush()
    screen("welcome to claude")                          # idle
    time.sleep(0.7)
    screen("Generating... esc to interrupt")             # working
    time.sleep(0.8)
    screen("Do you want to proceed?\\n 1. Yes\\n 2. No")   # waiting
    time.sleep(0.8)
    screen("API Error: Overloaded (529). retrying")      # error
    time.sleep(0.8)
    screen("> ")                                         # idle
    time.sleep(0.8)
'''))
os.chmod(fake, 0o755)

m, s = pty.openpty()
def drain():
    while True:
        try: d = os.read(m, 4096)
        except OSError: break
        if not d: break
threading.Thread(target=drain, daemon=True).start()

# Hermetic: an EMPTY accounts dir (real profiles in ~/.claude-accounts would
# pop the interactive account picker and hang on the PTY), usage polling off
# (no keychain/network from a test run), a config dir and a temp dir of our
# own (the session tracker scans <config>/projects and writes under $TMPDIR).
cfg = os.path.join(tmp, "cfg"); os.makedirs(cfg)
env = dict(os.environ, CLAUDE_MATE_SOCK=sock, CLAUDE_REAL=fake,
           PATH=stubdir + os.pathsep + os.environ.get("PATH", ""),
           TERM_PROGRAM="iTerm.app",
           CLAUDE_MATE_ACCOUNTS_DIR=os.path.join(tmp, "no-accounts"),
           CLAUDE_MATE_USAGE_POLL_S="0", CLAUDE_CONFIG_DIR=cfg, TMPDIR=tmp)
env.pop("CLAUDE_MATE_DEBUG", None)
print("== launching wrapper around fake claude ==")
proc = subprocess.Popen([sys.executable, WRAP], stdin=s, stdout=s,
                        stderr=subprocess.PIPE, env=env)
os.close(s)

focus_sent = False
ack = b""
deadline = time.time() + 8
while time.time() < deadline and proc.poll() is None:
    if not focus_sent:
        for line in list(received):
            parts = line.split("|")
            if len(parts) >= 4 and parts[3].startswith("/tmp/claude-mate-ctrl-") and os.path.exists(parts[3]):
                try:
                    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(parts[3])
                    c.sendall(b"focus\n")
                    # Two-stage ack: 'go' on receipt, 'ok' after the window op
                    # (the daemon orders cross-session window ops by waiting).
                    c.settimeout(5.0)
                    try:
                        while b"ok" not in ack:
                            chunk = c.recv(16)
                            if not chunk: break
                            ack += chunk
                    except socket.timeout: pass
                    c.close(); focus_sent = True
                    print("  -> sent FOCUS to", parts[3], "ack:", ack)
                except OSError: pass
    time.sleep(0.1)
try: proc.wait(timeout=5)
except subprocess.TimeoutExpired: proc.kill()
time.sleep(0.3)

states = [l.split("|")[0] for l in received]
osa_called = os.path.exists(osa_log) and open(osa_log).read().strip() != ""
print("\n================ ASSERTIONS ================")
checks = []
def check(n, ok): checks.append(ok); print(f"  [{'PASS' if ok else 'FAIL'}] {n}")
check("registered on launch", len(received) > 0)
check("detected working", "working" in states)
check("detected waiting", "waiting" in states)
check("detected error", "error" in states)
check("detected idle", "idle" in states)
check("every report carried a ctrl socket",
      all(len(l.split("|")) >= 4 and l.split("|")[3].startswith("/tmp/claude-mate-ctrl-") for l in received))
check("sent 'end' on exit", "end" in states)
check("FOCUS reached wrapper (osascript ran)", osa_called)
check("ctrl 'focus' acked in two stages (go on receipt, ok after the op)",
      b"go" in ack and b"ok" in ack)
check("every report carries all nine fields, the last '-' (no workflow on screen)",
      all(len(l.split("|")) == 9 and l.split("|")[8] == "-" for l in received))
print("\n  state sequence:", states)

# --------------------------------------------------------------------------- #
# A dynamic workflow: its row on screen -> field 8, then '-' after the hold
# --------------------------------------------------------------------------- #
print("\n== a dynamic workflow: its row -> field 8, '-' once the hold is over ==")
HOLD = 3.0                     # WorkflowWatch.GONE_S -- 20 s in a real session
WF = "deployment-risk-sweep-3802"
BOX = "─" * 48
HINT = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"


def wf_frame(*rows):
    return "\n".join(["⏺ Launched the sweep; I will report back.", BOX, "❯ ", BOX,
                      HINT, ""] + list(rows))


GREEN, OFF = "\x1b[38;2;78;186;101m", "\x1b[0m"     # Claude's "success" bullet


def sweep(s):                  # the screenshot's run, its clock at 2m<s>s
    return f"  ◯ {WF}  {'▱' * 20}  0/5 · 2m{s:02d}s · ↓ 542.8k tokens"


NIGHTLY = f"  {GREEN}◯{OFF} nightly  ▰▰▰▰▰▰▰▰  5/5 · 3m44s · ↓ 1.2m tokens"
NIGHTLY_REC = "D;5;5;;100;224;1200000;;nightly"
SLASH = "\n".join(["⏺ Launched the sweep; I will report back.", BOX, "❯ /", BOX,
                   "  /workflows      Show dynamic workflow runs",
                   "  /tasks          List and manage background tasks"])

# (screen, seconds it stays up): nothing yet; two runs, their clocks ticking
# once a frame (which must not be a reason to report); the second one
# COMPLETES (green) while the first runs on -- a change of shape with no
# change of state, so only the shape can be what reports it; then the rows
# gone -- a menu took the panel's place -- for longer than the hold. Then the
# finished run alone, the '/' list over it, the run again, and Claude
# dropping its row.
FRAMES = ([(wf_frame(), 0.8)]
          + [(wf_frame(sweep(s), f"  ◯ nightly  ▰▰▰▰▰▰▱▱  4/5 · 3m{s:02d}s · ↓ 1.1m tokens"), 0.3)
             for s in range(39, 45)]
          + [(wf_frame(sweep(s), NIGHTLY), 0.3) for s in range(45, 48)]
          + [(wf_frame(), HOLD + 1.5)]
          + [(wf_frame(NIGHTLY), 0.8), (SLASH, 1.2), (wf_frame(NIGHTLY), 0.8), (wf_frame(), 2.5)])
SHOWN_S = 9 * 0.3              # how long the rows are on screen
LISTED_S = 0.8 + 1.2 + 0.8     # how long the finished run is shown or merely hidden
wf_fake = os.path.join(tmp, "fake-claude-wf")
with open(wf_fake, "w", encoding="utf-8") as fh:
    fh.write(textwrap.dedent(f'''\
        #!/usr/bin/env python3
        import sys, time
        for text, pause in {ascii(FRAMES)}:
            sys.stdout.buffer.write(("\\x1b[2J\\x1b[H" + text + "\\n").encode("utf-8"))
            sys.stdout.buffer.flush()
            time.sleep(pause)
    '''))
os.chmod(wf_fake, 0o755)

# The real wrapper with its 20 s hold for hidden rows cut to HOLD: loaded the
# way the unit tests load it, patched, and run as `claude` would run it.
launcher = os.path.join(tmp, "wrap-short-hold.py")
with open(launcher, "w", encoding="utf-8") as fh:
    fh.write(textwrap.dedent(f'''\
        import importlib.util
        from importlib.machinery import SourceFileLoader
        spec = importlib.util.spec_from_loader("cmw", SourceFileLoader("cmw", {ascii(WRAP)}))
        cmw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cmw)
        cmw.WorkflowWatch.GONE_S = {HOLD!r}
        cmw.main()
    '''))

wf_sock = os.path.join(tmp, "wf.sock")
wf_rx = []                     # (when it arrived, line)


def wf_server():
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); srv.bind(wf_sock); srv.listen(16)
    while True:
        try: conn, _ = srv.accept()
        except OSError: break
        data = b""
        with conn:
            conn.settimeout(2.0)
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk: break
                    data += chunk
            except OSError: pass
        line = data.decode(errors="replace").strip()
        if line:
            wf_rx.append((time.time(), line)); print("  DAEMON <=", line)


threading.Thread(target=wf_server, daemon=True).start()

m2, s2 = pty.openpty()
# Wide enough for the row: an 80-column default would wrap it, and a wrapped
# row is (correctly) not a workflow row at all.
fcntl.ioctl(s2, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))


def drain2():
    while True:
        try: d = os.read(m2, 4096)
        except OSError: break
        if not d: break


threading.Thread(target=drain2, daemon=True).start()
dbg_log = os.path.join(tmp, "debug.log")
env2 = dict(env, CLAUDE_MATE_SOCK=wf_sock, CLAUDE_REAL=wf_fake, CLAUDE_MATE_DEBUG=dbg_log)
p2 = subprocess.Popen([sys.executable, launcher], stdin=s2, stdout=s2,
                      stderr=subprocess.PIPE, env=env2)
os.close(s2)
try:
    _, err2 = p2.communicate(timeout=30)
except subprocess.TimeoutExpired:
    p2.kill(); _, err2 = p2.communicate()
time.sleep(0.3)
if err2.strip():
    print("  wrapper stderr:", err2.decode(errors="replace")[-400:])

lines2 = [d for _t, d in wf_rx]
parts2 = [d.split("|") for d in lines2]
REC = re.compile(r"^R;0;5;;0;(\d+);542800;;" + re.escape(WF)
                 + r"~R;4;5;;80;\d+;1100000;;nightly$")
DONE = re.compile(r"^R;0;5;;0;\d+;542800;;" + re.escape(WF)
                  + r"~D;5;5;;100;224;1200000;;nightly$")
first = next((i for i, p in enumerate(parts2) if len(p) == 9 and REC.match(p[8])), None)
done = next((i for i, p in enumerate(parts2) if len(p) == 9 and DONE.match(p[8])), None)
clear = (next((i for i in range(first + 1, len(parts2)) if parts2[i][8:] == ["-"]), None)
         if first is not None else None)


def reason(p):
    """What a report may go out for: its state, and field 8 without the
    clocks and the token counts (elapsed_s and tokens, sub-fields 5 and 6)."""
    recs = [] if p[8] in ("", "-") else [r.split(";", 8) for r in p[8].split("~")]
    return p[0], (tuple(tuple(r[:5] + r[7:]) for r in recs) if recs else p[8])


# From the first record to the clear, every report must have had a reason:
# a new state or a new shape. (A frame the PTY happens to hand over in two
# reads shows half a panel for a moment -- a real change of shape -- so this
# is asked of each report rather than by counting them.)
span = parts2[first:clear + 1] if first is not None and clear is not None else []
idle = [span[i] for i in range(1, len(span)) if reason(span[i]) == reason(span[i - 1])]
print("\n================ ASSERTIONS (workflow) ================")
check("every report carries exactly nine fields",
      bool(parts2) and all(len(p) == 9 for p in parts2))
check("...each short enough for one recv(512)", all(len(d.encode()) < 512 for d in lines2))
check("the launch report says '-': no workflow seen yet",
      bool(parts2) and parts2[0][8:] == ["-"])
secs = int(REC.match(parts2[first][8]).group(1)) if first is not None else -1
check(f"the rows reach the daemon as records, in panel order (clock {secs})",
      first is not None and 159 <= secs <= 166)
check("a run that completes while another runs on is reported at once, by its shape "
      "alone (the session stays working)",
      done is not None and parts2[done][0] == "working"
      and "working" in [p[0] for p in parts2[first:done]])
check(f"two ticking clocks are no reason to report: each of the {len(span)} reports "
      f"while they ran changed the state or the shape ({len(idle)} did not)",
      len(span) >= 3 and not idle)
check("'-' follows once the rows are gone for good", clear is not None)
gap = wf_rx[clear][0] - wf_rx[first][0] if clear is not None else 0.0
check(f"...but only after the hold ({gap:.1f}s after the first record, the rows went "
      f"{SHOWN_S:.1f}s after it)",
      # slack for a late monitor pass; with no hold at all the gap is SHOWN_S
      gap >= SHOWN_S + HOLD - 1.5)
held = [p[0] for p in parts2[first:clear]] if clear is not None else []
check("the session stays working while the hidden rows are held",
      "working" in held and "idle" not in held[held.index("working"):])
check("...and goes idle once the hold is over",
      clear is not None and "idle" in [p[0] for p in parts2[clear:]])
check("'end' carries nine fields too", any(p[0] == "end" and len(p) == 9 for p in parts2))
dbg_text = (open(dbg_log, encoding="utf-8", errors="replace").read()
            if os.path.exists(dbg_log) else "")
check("the CLAUDE_MATE_DEBUG header shows the field",
      "wf='R;0;5;;0;" in dbg_text and "wf='-'" in dbg_text)
alone = (next((i for i in range(clear + 1, len(parts2)) if parts2[i][8:] == [NIGHTLY_REC]), None)
         if clear is not None else None)
dropped = (next((i for i in range(alone + 1, len(parts2)) if parts2[i][8:] == ["-"]), None)
           if alone is not None else None)
check("a finished run alone on the panel is listed", alone is not None)
check("...and stays listed while the '/' list hides it: one '-', once Claude has dropped the row",
      dropped is not None
      and not any(p[8:] == [NIGHTLY_REC] for p in parts2[dropped + 1:])
      and wf_rx[dropped][0] - wf_rx[alone][0] >= LISTED_S - 0.8)
print("\n  states:", [p[0] for p in parts2])
ok = all(checks)
shutil.rmtree(tmp, ignore_errors=True)
print("\n================", "ALL PASSED" if ok else "SOME FAILED", "================")
sys.exit(0 if ok else 1)
