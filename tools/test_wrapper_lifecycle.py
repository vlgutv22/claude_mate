#!/usr/bin/env python3
"""
Lifecycle / relay robustness tests for bin/claude-mate-wrap -- the "machine gun,
no excuses" hardening. No real claude, no hardware: a fake-claude PTY child is
driven through the failure modes the wrapper must survive.

Covers:
  #2  SIGTERM -> the wrapper runs the SAME cleanup as a clean exit (deregisters
      with 'end') and exits, instead of skipping teardown (old code: cleanup lived
      only in `finally`, which kill/logout bypass -> ghost session + dead tty).
  #3  A large paste while the child is slow to read round-trips fully without the
      relay deadlocking (non-blocking master_fd + pending-write buffer).
  #7  stdin EOF does NOT break the relay or busy-spin: the wrapper keeps relaying
      the child's output and exits cleanly when the CHILD exits.

Run:  python3 tools/test_wrapper_lifecycle.py     (needs pyte: pip install pyte)
"""
import os, pty, socket, threading, subprocess, sys, time, tempfile, textwrap, signal

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAP = os.path.join(REPO, "bin", "claude-mate-wrap")

try:
    import pyte  # noqa: F401  (the wrapper needs it)
except ImportError:
    print("SKIP: pyte not installed. Run: pip install pyte")
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="cmw-life-")
checks = []
def check(n, ok):
    checks.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}")

def daemon_server(path, sink):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path); srv.listen(16)
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        with conn:
            d = conn.recv(512).decode(errors="ignore").strip()
        if d:
            sink.append(d)

def write_fake(name, body):
    p = os.path.join(tmp, name)
    open(p, "w").write("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    os.chmod(p, 0o755)
    return p

def launch(fake, sock, split_io=False):
    """Run the wrapper around `fake`. Returns (proc, m_in, m_out).
    split_io=True gives the wrapper SEPARATE stdin/stdout ptys so the test can EOF
    its stdin (close m_in) while still reading its output (m_out)."""
    # Hermetic: empty accounts dir (a real profile would pop the account
    # picker and hang on the PTY), usage polling off.
    env = dict(os.environ, CLAUDE_MATE_SOCK=sock, CLAUDE_REAL=fake,
               TERM_PROGRAM="iTerm.app",
               CLAUDE_MATE_ACCOUNTS_DIR=os.path.join(tmp, "no-accounts"),
               CLAUDE_MATE_USAGE_POLL_S="0")
    if split_io:
        m_in, s_in = pty.openpty()
        m_out, s_out = pty.openpty()
        proc = subprocess.Popen([sys.executable, WRAP], stdin=s_in, stdout=s_out,
                                stderr=subprocess.DEVNULL, env=env)
        os.close(s_in); os.close(s_out)
        return proc, m_in, m_out
    m, s = pty.openpty()
    proc = subprocess.Popen([sys.executable, WRAP], stdin=s, stdout=s,
                            stderr=subprocess.DEVNULL, env=env)
    os.close(s)
    return proc, m, m

def drain(fd):
    """Keep reading a master fd so the wrapper's stdout never blocks (a real
    terminal always drains its output)."""
    def _d():
        while True:
            try:
                if not os.read(fd, 65536):
                    break
            except OSError:
                break
    threading.Thread(target=_d, daemon=True).start()


# --------------------------------------------------------------------------- #
# #2 -- SIGTERM triggers the cleanup (sends 'end') and exits.
# --------------------------------------------------------------------------- #
print("== #2: SIGTERM runs cleanup + deregisters ==")
sock2 = os.path.join(tmp, "d2.sock"); rx2 = []
threading.Thread(target=daemon_server, args=(sock2, rx2), daemon=True).start()
fake_sleep = write_fake("fake_sleep", '''
    import time, sys
    sys.stdout.write("\\x1b[2J\\x1b[Hready\\n"); sys.stdout.flush()
    time.sleep(30)                       # outlive the test; we kill it via the wrapper
''')
p2, m2, _ = launch(fake_sleep, sock2)
drain(m2)                                  # a real terminal always drains output
t0 = time.time()
while time.time() - t0 < 4 and not rx2:   # wait until registered
    time.sleep(0.05)
time.sleep(0.4)
p2.send_signal(signal.SIGTERM)
try:
    p2.wait(timeout=5); exited = True
except subprocess.TimeoutExpired:
    p2.kill(); exited = False
time.sleep(0.3)
try: os.close(m2)
except OSError: pass
states2 = [l.split("|")[0] for l in rx2]
check("wrapper exited promptly on SIGTERM", exited)
check("SIGTERM path deregistered with 'end'", "end" in states2)
print("    states:", states2)


# --------------------------------------------------------------------------- #
# #3 -- large paste round-trips without deadlocking the relay.
# --------------------------------------------------------------------------- #
print("\n== #3: large paste round-trips (non-blocking master_fd) ==")
sock3 = os.path.join(tmp, "d3.sock"); rx3 = []
threading.Thread(target=daemon_server, args=(sock3, rx3), daemon=True).start()
N = 200_000
fake_echo = write_fake("fake_echo", f'''
    import os, sys, tty, time
    try: tty.setraw(0)                    # no terminal echo -> clean byte count
    except Exception: pass
    sys.stdout.write("\\x1b[2J\\x1b[Hready\\n"); sys.stdout.flush()
    got = 0
    time.sleep(0.4)                       # be SLOW to start reading -> pressure the buffer
    while got < {N}:
        d = os.read(0, 4096)
        if not d: break
        got += len(d)
    sys.stdout.write("GOTALL %d\\n" % got); sys.stdout.flush()
    time.sleep(0.2)
''')
p3, m3, _ = launch(fake_echo, sock3)
# Wait for 'ready' then blast the paste.
buf = b""; t0 = time.time()
while time.time() - t0 < 4 and b"ready" not in buf:
    try: buf += os.read(m3, 4096)
    except OSError: break
payload = b"x" * N
def blast():
    try: os.write(m3, payload)
    except OSError: pass
threading.Thread(target=blast, daemon=True).start()
# Read until we see GOTALL or time out.
out = b""; t0 = time.time(); got_all = False
while time.time() - t0 < 8:
    try:
        chunk = os.read(m3, 65536)
    except OSError:
        break
    if not chunk:
        break
    out += chunk
    if b"GOTALL" in out:
        got_all = True
        break
gotn = -1
if got_all:
    try: gotn = int(out.split(b"GOTALL", 1)[1].split()[0])
    except Exception: gotn = -1
try: p3.wait(timeout=5)
except subprocess.TimeoutExpired: p3.kill()
try: os.close(m3)
except OSError: pass
check("relay did not deadlock on a large paste (child saw GOTALL)", got_all)
check(f"child received the full {N} bytes", gotn == N)
print("    child reported:", gotn, "bytes")


# --------------------------------------------------------------------------- #
# #7 -- stdin EOF: keep relaying, exit cleanly on CHILD exit, no busy-spin.
# --------------------------------------------------------------------------- #
print("\n== #7: stdin EOF is graceful (no spin, exits on child exit) ==")
sock7 = os.path.join(tmp, "d7.sock"); rx7 = []
threading.Thread(target=daemon_server, args=(sock7, rx7), daemon=True).start()
fake_after_eof = write_fake("fake_after_eof", '''
    import sys, time
    sys.stdout.write("\\x1b[2J\\x1b[Hready\\n"); sys.stdout.flush()
    time.sleep(1.5)                       # ... while the wrapper's stdin is at EOF
    sys.stdout.write("POSTEOF marker\\n"); sys.stdout.flush()
    time.sleep(0.3)
''')
p7, m_in, m_out = launch(fake_after_eof, sock7, split_io=True)
buf = b""; t0 = time.time()
while time.time() - t0 < 4 and b"ready" not in buf:
    try: buf += os.read(m_out, 4096)
    except OSError: break
os.close(m_in)                            # EOF on the wrapper's stdin
# Sample the wrapper's CPU time across the idle window; a busy-spin would burn a
# whole core. ps TIME is mm:ss.ss on macOS.
def cpu_secs(pid):
    try:
        t = subprocess.check_output(["ps", "-o", "time=", "-p", str(pid)]).decode().strip()
        m, s = t.split(":") if ":" in t else ("0", t)
        return int(m) * 60 + float(s)
    except Exception:
        return None
c0 = cpu_secs(p7.pid); time.sleep(1.3); c1 = cpu_secs(p7.pid)
post = b""; t0 = time.time()
while time.time() - t0 < 4:
    try: chunk = os.read(m_out, 4096)
    except OSError: break
    if not chunk: break
    post += chunk
    if b"POSTEOF" in post: break
try: p7.wait(timeout=5); exited7 = True
except subprocess.TimeoutExpired: p7.kill(); exited7 = False
time.sleep(0.3)
try: os.close(m_out)
except OSError: pass
states7 = [l.split("|")[0] for l in rx7]
check("kept relaying child output after stdin EOF (saw POSTEOF)", b"POSTEOF" in post)
check("exited cleanly after the child exited", exited7)
check("deregistered with 'end' after stdin EOF", "end" in states7)
if c0 is not None and c1 is not None:
    burned = c1 - c0
    check(f"no busy-spin while stdin at EOF (CPU +{burned:.2f}s over 1.3s wall)", burned < 0.7)
else:
    print("  [SKIP] CPU sampling unavailable on this platform")
print("    states:", states7)


# --------------------------------------------------------------------------- #
# #C -- relay breaks while the child is STILL ALIVE (our output terminal goes
# away): the wrapper must hang up the child and reap, NOT block forever in
# waitpid. Review finding: cleanup() now closes master_fd + the reap escalates to
# SIGKILL, so a broken-output session can't wedge.
# --------------------------------------------------------------------------- #
print("\n== #C: output terminal vanishes mid-relay -> no waitpid hang ==")
sockC = os.path.join(tmp, "dC.sock"); rxC = []
threading.Thread(target=daemon_server, args=(sockC, rxC), daemon=True).start()
fake_chatty = write_fake("fake_chatty", '''
    import sys, time
    sys.stdout.write("\\x1b[2J\\x1b[Hready\\n"); sys.stdout.flush()
    for i in range(300):                  # keep the relay actively writing for ~30s
        sys.stdout.write("line %d\\n" % i); sys.stdout.flush()
        time.sleep(0.1)
''')
pC, m_in, m_out = launch(fake_chatty, sockC, split_io=True)
buf = b""; t0 = time.time()
while time.time() - t0 < 4 and b"ready" not in buf:
    try: buf += os.read(m_out, 4096)
    except OSError: break
time.sleep(0.5)
os.close(m_out)                           # the wrapper's stdout target disappears
t0 = time.time()
try: pC.wait(timeout=10); exitedC = True   # must exit well under the child's ~30s
except subprocess.TimeoutExpired: pC.kill(); exitedC = False
took = time.time() - t0
time.sleep(0.3)
try: os.close(m_in)
except OSError: pass
statesC = [l.split("|")[0] for l in rxC]
check(f"wrapper did NOT hang in waitpid when output vanished (exited in {took:.1f}s)", exitedC)
check("deregistered with 'end' on the broken-output path", "end" in statesC)
print("    states:", statesC)


# --------------------------------------------------------------------------- #
# #6b -- pyte missing AND no real claude resolvable: exit cleanly (127), do NOT
# infinite re-exec. real_claude() now returns None and exec_real_claude() exits.
# --------------------------------------------------------------------------- #
print("\n== #6b: pyte missing + no real claude -> clean 127, no re-exec loop ==")
shadow = os.path.join(tmp, "shadow"); os.makedirs(os.path.join(shadow, "pyte"))
open(os.path.join(shadow, "pyte", "__init__.py"), "w").write('raise ImportError("forced")\n')
emptydir = os.path.join(tmp, "emptybin"); os.makedirs(emptydir)
# Hide every `claude` (PATH has none) and any versions dir (HOME=tmp), CLAUDE_REAL
# unset. python itself is launched via sys.executable (absolute), so it's fine.
env6 = dict(os.environ, PYTHONPATH=shadow, PATH=emptydir, HOME=tmp)
env6.pop("CLAUDE_REAL", None)
# subprocess timeout bounds it: a re-exec loop would never return -> TimeoutExpired.
try:
    r6 = subprocess.run([sys.executable, WRAP, "--x"], env=env6,
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=8)
    rc6, err6 = r6.returncode, r6.stderr.decode(errors="ignore")
except subprocess.TimeoutExpired:
    rc6, err6 = -1, "(timed out -> likely re-exec loop)"
check("exited 127 (clean not-found), no re-exec loop", rc6 == 127)
check("reported it could not locate claude", "could not locate" in err6 or "pyte not installed" in err6)
print("    rc=", rc6, "stderr tail:", err6.strip().splitlines()[-1] if err6.strip() else "(none)")


print("\n================", "ALL PASSED" if all(checks) else "SOME FAILED", "================")
sys.exit(0 if all(checks) else 1)
