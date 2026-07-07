#!/usr/bin/env python3
"""
Regression test: terminal resize (SIGWINCH) MUST NOT hang the wrapper, and a
resize MUST still reach the child even when the session is idle.

Two failure modes are guarded:

  A. DEADLOCK UNDER LOAD. The old SIGWINCH handler took `screen_lock`. Python runs
     signal handlers on the MAIN thread, so a resize arriving while the main loop
     already held that lock inside `stream.feed(...)` self-deadlocked the whole
     session -- you had to kill it and lost all output. (Opening a new terminal
     tab raises the same SIGWINCH in many emulators, which is why "open a new tab"
     froze it too.) Here a fake claude floods output while we hammer SIGWINCH; the
     wrapper must keep running and exit cleanly within the timeout.

  B. IDLE RESIZE PROPAGATION. The fix moves the resize out of the handler into the
     main loop, which is normally blocked in select(). When the session is idle
     (no I/O) ONLY the signal self-pipe (set_wakeup_fd) can wake select() to apply
     the resize. A fake claude reports its window size on SIGWINCH; after it goes
     idle we send one resize and assert the child actually receives the new size.
     (This catches a regression that check A alone would miss -- heavy output wakes
     select() on its own, so check A passes even if the self-pipe is broken.)

Override the wrapper under test with CMW_WRAP=/path/to/old/claude-mate-wrap to
prove this test actually catches the regression.

Run:  python3 tools/test_wrapper_resize.py     (needs pyte: pip install pyte)
"""
import os, pty, socket, threading, subprocess, sys, time, tempfile, textwrap, \
       signal, fcntl, termios, struct

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAP = os.environ.get("CMW_WRAP", os.path.join(REPO, "bin", "claude-mate-wrap"))

try:
    import pyte  # noqa: F401  (the wrapper needs it)
except ImportError:
    print("SKIP: pyte not installed. Run: pip install pyte")
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="cmw-resize-")
sock = os.path.join(tmp, "d.sock")

received = []
rlock = threading.Lock()
def server():
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); srv.bind(sock); srv.listen(64)
    while True:
        try: conn, _ = srv.accept()
        except OSError: break
        with conn:
            d = conn.recv(512).decode(errors="ignore").strip()
        if d:
            with rlock:
                received.append(d)
threading.Thread(target=server, daemon=True).start()

# Fake claude:
#   phase 1 -- flood output for ~2.5s so the wrapper's main loop spends most of
#              its time inside `stream.feed(...)` holding screen_lock (the exact
#              window in which a mishandled SIGWINCH used to deadlock), with a
#              "working" signal in the live region so it keeps reporting.
#   phase 2 -- print IDLE_NOW and go quiet, so the wrapper blocks in select();
#              on SIGWINCH it echoes its current window size as CHILDSIZE r c.
IDLE_COLS, IDLE_ROWS = 113, 37           # distinctive size used only for the idle resize
fake = os.path.join(tmp, "fake-claude")
open(fake, "w").write(textwrap.dedent('''\
    #!/usr/bin/env python3
    import sys, time, signal, fcntl, termios, struct
    # Signal-safe: the handler only flips a flag (writing to buffered stdout from
    # a handler that interrupts the flood loop's own write() would crash with a
    # reentrant-call error). The main loop reports the size when idle.
    _winch = [False]
    def onwinch(signum, frame):
        _winch[0] = True
    signal.signal(signal.SIGWINCH, onwinch)
    line = "Generating... esc to interrupt  " + ("x" * 100) + "\\r\\n"
    end = time.monotonic() + 2.5
    while time.monotonic() < end:
        for _ in range(300):
            sys.stdout.write(line)
        sys.stdout.flush()
        time.sleep(0.004)
    _winch[0] = False
    sys.stdout.write("IDLE_NOW\\r\\n"); sys.stdout.flush()
    end = time.monotonic() + 3.0
    while time.monotonic() < end:
        if _winch[0]:
            _winch[0] = False
            d = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\\0" * 8)
            r, c, _, _ = struct.unpack("HHHH", d)
            sys.stdout.write("CHILDSIZE %d %d\\r\\n" % (r, c)); sys.stdout.flush()
        time.sleep(0.03)
'''))
os.chmod(fake, 0o755)

m, s = pty.openpty()
out = []
olock = threading.Lock()
def drain():
    while True:
        try: d = os.read(m, 65536)
        except OSError: break
        if not d: break
        with olock:
            out.append(d.decode(errors="ignore"))
threading.Thread(target=drain, daemon=True).start()

def drained():
    with olock:
        return "".join(out)

# Hermetic: empty accounts dir (a real profile would pop the account picker
# and hang on the PTY), usage polling off.
env = dict(os.environ, CLAUDE_MATE_SOCK=sock, CLAUDE_REAL=fake,
           CLAUDE_MATE_ACCOUNTS_DIR=os.path.join(tmp, "no-accounts"),
           CLAUDE_MATE_USAGE_POLL_S="0")
print(f"== launching wrapper ({os.path.basename(WRAP)}) ==")
proc = subprocess.Popen([sys.executable, WRAP], stdin=s, stdout=s,
                        stderr=subprocess.PIPE, env=env)
os.close(s)

def winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass

# --- Check A: hammer SIGWINCH during the output flood (deadlock window). The
# wrapper isn't this pty's controlling-tty owner, so we both flip the slave
# winsize (a genuine new size for get_winsize) AND deliver SIGWINCH directly.
def hammer():
    i = 0
    end = time.monotonic() + 2.3
    while time.monotonic() < end and proc.poll() is None:
        rows, cols = (40, 120) if i % 2 else (24, 80)
        winsize(m, rows, cols)
        try:
            proc.send_signal(signal.SIGWINCH)
        except Exception:
            break
        i += 1
        time.sleep(0.002)
    print(f"  check A: sent ~{i} SIGWINCH during flood")
hammer_t = threading.Thread(target=hammer)
hammer_t.start()

# --- Check B: wait until the child reports it has gone idle, let the wrapper
# settle into a blocked select(), then send ONE distinctive resize. Only the
# signal self-pipe can wake an idle select() to apply it.
idle_resize_ok = False
deadline = time.time() + 8
while time.time() < deadline and proc.poll() is None:
    if "IDLE_NOW" in drained():
        break
    time.sleep(0.05)
hammer_t.join(timeout=3)
time.sleep(0.5)                                  # let the wrapper block in select()
winsize(m, IDLE_ROWS, IDLE_COLS)
try:
    proc.send_signal(signal.SIGWINCH)
except Exception:
    pass
want = f"CHILDSIZE {IDLE_ROWS} {IDLE_COLS}"
deadline = time.time() + 3
while time.time() < deadline and proc.poll() is None:
    if want in drained():
        idle_resize_ok = True
        break
    time.sleep(0.05)
print(f"  check B: idle resize {'reached' if idle_resize_ok else 'did NOT reach'} the child ({want!r})")

# The wrapper must exit shortly after the fake claude finishes. A deadlocked
# SIGWINCH handler hangs the wrapper forever -> wait() times out.
hung = False
try:
    proc.wait(timeout=12)
except subprocess.TimeoutExpired:
    hung = True
    proc.kill()
time.sleep(0.3)

with rlock:
    states = [l.split("|")[0] for l in received]

print("\n================ ASSERTIONS ================")
checks = []
def check(n, ok): checks.append(ok); print(f"  [{'PASS' if ok else 'FAIL'}] {n}")
check("A: wrapper did NOT hang on resize storm (exited within timeout)", not hung)
check("A: kept reporting state through the resize storm", "working" in states)
check("B: idle resize reached the child via the signal self-pipe", idle_resize_ok)
check("deregistered cleanly on exit ('end')", (not hung) and "end" in states)
print("\n  states seen:", states[:14], "..." if len(states) > 14 else "")
ok = all(checks)
print("\n================", "ALL PASSED" if ok else "SOME FAILED", "================")
sys.exit(0 if ok else 1)
