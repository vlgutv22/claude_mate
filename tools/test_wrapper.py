#!/usr/bin/env python3
"""
Validate bin/claude-mate-wrap with NO real claude and NO hardware:
a staged fake-claude TUI drives the wrapper through working/waiting/error/idle,
and we assert the wrapper reports each state to the daemon socket, carries its
focus control-socket, deregisters on exit ('end'), and dispatches FOCUS
(osascript stubbed so nothing real is touched).

Run:  python3 tools/test_wrapper.py     (needs pyte: pip install pyte)
"""
import os, pty, socket, threading, subprocess, sys, time, tempfile, textwrap

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

env = dict(os.environ, CLAUDE_MATE_SOCK=sock, CLAUDE_REAL=fake,
           PATH=stubdir + os.pathsep + os.environ.get("PATH", ""),
           TERM_PROGRAM="iTerm.app")
print("== launching wrapper around fake claude ==")
proc = subprocess.Popen([sys.executable, WRAP], stdin=s, stdout=s,
                        stderr=subprocess.PIPE, env=env)
os.close(s)

focus_sent = False
deadline = time.time() + 8
while time.time() < deadline and proc.poll() is None:
    if not focus_sent:
        for line in list(received):
            parts = line.split("|")
            if len(parts) >= 4 and parts[3].startswith("/tmp/claude-mate-ctrl-") and os.path.exists(parts[3]):
                try:
                    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(parts[3])
                    c.sendall(b"focus\n"); c.close(); focus_sent = True
                    print("  -> sent FOCUS to", parts[3])
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
print("\n  state sequence:", states)
ok = all(checks)
print("\n================", "ALL PASSED" if ok else "SOME FAILED", "================")
sys.exit(0 if ok else 1)
