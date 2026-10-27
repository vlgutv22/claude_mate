#!/usr/bin/env python3
"""
Validate bin/claude-mate-wrap with NO real claude and NO hardware:
a staged fake-claude TUI drives the wrapper through working/waiting/error/idle,
and we assert the wrapper reports each state to the daemon socket, carries its
focus control-socket, deregisters on exit ('end'), and dispatches FOCUS
(osascript stubbed so nothing real is touched).

Then dynamic workflows: a fake claude draws two workflow rows under the prompt
box, ticks their clocks, completes one of them, and hides both. Every report
must carry ten fields; the rows must reach the daemon as field-8 records; a
run completing must be reported at once (its shape changed) while ticking
clocks are no reason to report; and '-' must follow only once WorkflowWatch's
hold for hidden rows is over (cut from 20 s to HOLD for the test), the
session staying working until then. Last, a run that has finished, its row
hidden by the '/' list for a moment and then shown again: it must stay
listed throughout, '-' only once Claude has dropped the row -- a '-' while
it was merely out of view made it a new run to the daemon when it came back.

Last, field 9 -- the conversation the history keeps the chat under. With the
caps cache saying claude takes --session-id, the id the wrapper hands it must
come back in field 9 once the fake has written its transcript, in a report of
its own (nothing else changed), and in `end`; with a cold cache, the one new
transcript in the session's directory must name it within a second. Field 9
also carries the directory, the config dir and the flags a resume replays --
the opening prompt not among them -- and nothing may follow `end`. A session
started with -c carries on a transcript that was there before it: the one
that grows during its turn names it -- not the newer `claude -p` job beside
it -- and when another terminal's chat there grows too, nothing is named.

Run:  python3 tools/test_wrapper.py     (needs pyte: pip install pyte)
"""
import os, pty, socket, threading, subprocess, sys, time, tempfile, textwrap, \
       re, fcntl, termios, struct, shutil, json, urllib.parse

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

def read_line(conn):
    """One report, read the way the daemon reads it: up to its newline. A single
    recv(512) used to stand in for that, and silently cut any line longer than
    512 bytes -- which a report carrying a path and its flags (field 9) can be."""
    data = b""
    conn.settimeout(2.0)
    try:
        while b"\n" not in data:
            chunk = conn.recv(4096)
            if not chunk: break
            data += chunk
    except OSError: pass
    return data.decode(errors="ignore").strip()


received = []
def server():
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); srv.bind(sock); srv.listen(16)
    while True:
        try: conn, _ = srv.accept()
        except OSError: break
        with conn:
            d = read_line(conn)
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
check("every report carries all ten fields, field 8 '-' (no workflow on screen)",
      all(len(l.split("|")) == 10 and l.split("|")[8] == "-" for l in received))
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
first = next((i for i, p in enumerate(parts2) if len(p) == 10 and REC.match(p[8])), None)
done = next((i for i, p in enumerate(parts2) if len(p) == 10 and DONE.match(p[8])), None)
clear = (next((i for i in range(first + 1, len(parts2)) if parts2[i][8] == "-"), None)
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
check("every report carries exactly ten fields",
      bool(parts2) and all(len(p) == 10 for p in parts2))
# Fields 0-8 only: field 9 carries a path and flags, whose length is the
# machine's (a profile directory, the checkout's path), and the daemon reads a
# line to its newline anyway. The rest still has to stay small.
check("...fields 0-8 of each short enough for one recv(512)",
      all(len("|".join(p[:9]).encode()) < 512 for p in parts2))
check("the launch report says '-': no workflow seen yet",
      bool(parts2) and parts2[0][8] == "-")
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
check("'end' carries ten fields too", any(p[0] == "end" and len(p) == 10 for p in parts2))
dbg_text = (open(dbg_log, encoding="utf-8", errors="replace").read()
            if os.path.exists(dbg_log) else "")
check("the CLAUDE_MATE_DEBUG header shows the field",
      "wf='R;0;5;;0;" in dbg_text and "wf='-'" in dbg_text)
alone = (next((i for i in range(clear + 1, len(parts2)) if parts2[i][8] == NIGHTLY_REC), None)
         if clear is not None else None)
dropped = (next((i for i in range(alone + 1, len(parts2)) if parts2[i][8] == "-"), None)
           if alone is not None else None)
check("a finished run alone on the panel is listed", alone is not None)
check("...and stays listed while the '/' list hides it: one '-', once Claude has dropped the row",
      dropped is not None
      and not any(p[8] == NIGHTLY_REC for p in parts2[dropped + 1:])
      and wf_rx[dropped][0] - wf_rx[alone][0] >= LISTED_S - 0.8)
print("\n  states:", [p[0] for p in parts2])

# --------------------------------------------------------------------------- #
# Field 9: which conversation, once its transcript exists
# --------------------------------------------------------------------------- #
# The id reaches the daemon the two ways a real session gives it: handed to
# claude as --session-id (the caps cache says it can take one), or claude's
# own, recognised as the one transcript in this directory that is new since
# launch (a cold cache -- the first session after every Claude Code update).
# Each fake claude starts idle and writes its transcript a second in. The one
# handed an id stays idle throughout, so the report that names the conversation
# can only have gone out for the id. The cold-cache one runs a turn to do it,
# as a real first turn would: a claimed-by-discovery id waits for the session's
# OWN first turn, since until then any new transcript here is somebody else's
# -- which the third run is, a second terminal's conversation appearing before
# this session has said a word.
print("\n== field 9: a minted conversation id, and one found by its new transcript ==")
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CONV_FAKE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, re, sys, time, uuid
    if "--help" in sys.argv:          # the wrapper's caps probe: answer, write nothing
        print("  --session-id <uuid>  Use a specific session ID")
        sys.exit(0)
    def screen(s):
        sys.stdout.write("\\x1b[2J\\x1b[H" + s + "\\n"); sys.stdout.flush()
    argv, plan = sys.argv[1:], os.environ.get("FAKE_PLAN", "idle")
    cwd = os.getcwd()
    d = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects",
                     re.sub(r"[^a-zA-Z0-9]", "-", cwd))
    os.makedirs(d, exist_ok=True)
    def transcript(sid):
        with open(os.path.join(d, sid + ".jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "user", "cwd": cwd, "sessionId": sid}) + "\\n")
    def grow(sid):
        with open(os.path.join(d, sid + ".jsonl"), "a") as fh:
            fh.write(json.dumps({"type": "assistant", "cwd": cwd, "sessionId": sid}) + "\\n")
    screen("welcome to claude")
    if plan == "carry":               # -c: the chat it carries on was already there
        mine, other = os.environ["FAKE_CARRY"], os.environ.get("FAKE_OTHER", "")
        time.sleep(1.0)
        screen("Generating... esc to interrupt")       # our first turn
        at = time.time()
        for _ in range(3):
            if other:
                grow(other)           # another terminal's chat, at work meanwhile
            grow(mine)
            time.sleep(0.4)
        with open(__LOG__, "w") as fh:
            json.dump({"argv": argv, "sid": mine, "at": at}, fh)
        screen("welcome to claude")
        time.sleep(1.0)
        sys.exit(0)
    foreign = ""
    if plan == "foreign":             # another terminal's chat, before our first word
        time.sleep(0.5)
        foreign = str(uuid.uuid4())
        transcript(foreign)
    time.sleep(1.0)
    sid = (argv[argv.index("--session-id") + 1] if "--session-id" in argv
           else str(uuid.uuid4()))    # claude's own, when it was handed none
    if plan != "idle":
        screen("Generating... esc to interrupt")       # our first turn
    at = time.time()
    transcript(sid)
    with open(__LOG__, "w") as fh:
        json.dump({"argv": argv, "sid": sid, "at": at, "foreign": foreign}, fh)
    time.sleep(1.5)
    if plan != "idle":
        screen("welcome to claude")
    time.sleep(0.5)
''')


def drain_fd(fd):
    while True:
        try: d = os.read(fd, 4096)
        except OSError: break
        if not d: break


def hist(p):
    """Field 9 of a report, decoded: {} when it is empty or missing."""
    try: return json.loads(urllib.parse.unquote(p[9])) if len(p) > 9 and p[9] else {}
    except ValueError: return {"undecodable": p[9]}


def conv_run(tag, args, seed, plan="idle", there=(), fake_env=None):
    """The real wrapper around CONV_FAKE in a directory of its own. `there`:
    transcripts its project directory already holds at launch, as (id,
    entrypoint, seconds old). Returns the reports as (time, fields), what the
    fake logged, the work directory, and every 'conversation' the terminal's
    context file held while it ran."""
    work = os.path.join(tmp, "work-" + tag); os.makedirs(work)
    proj = os.path.join(cfg, "projects",
                        re.sub(r"[^a-zA-Z0-9]", "-", os.path.realpath(work)))
    for sid, entry, age in there:
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, sid + ".jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "user", "cwd": os.path.realpath(work),
                                 "sessionId": sid, "entrypoint": entry}) + "\n")
        when = time.time() - age
        os.utime(os.path.join(proj, sid + ".jsonl"), (when, when))
    fake_p = os.path.join(tmp, "fake-" + tag)
    log = os.path.join(tmp, "fake-" + tag + ".json")
    with open(fake_p, "w") as fh:
        fh.write(CONV_FAKE.replace("__LOG__", ascii(log)))
    os.chmod(fake_p, 0o755)
    ctx_dir = os.path.join(tmp, f"claude-mate-ctx-{os.getuid()}")
    if seed:                   # what the background probe leaves for the next session
        os.makedirs(ctx_dir, mode=0o700, exist_ok=True)
        caps = os.path.join(ctx_dir, "claude-caps.json")
        try: blob = json.load(open(caps))
        except (OSError, ValueError): blob = {}
        st = os.stat(fake_p)
        blob[f"{fake_p}:{int(st.st_mtime)}:{st.st_size}"] = True
        with open(caps, "w") as fh: json.dump(blob, fh)
    path, rx = os.path.join(tmp, tag + ".sock"), []

    def serve():
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); srv.bind(path); srv.listen(16)
        while True:
            try: conn, _ = srv.accept()
            except OSError: break
            with conn:
                line = read_line(conn)
            if line:
                rx.append((time.time(), line.split("|"))); print("  DAEMON <=", line[:160])
    threading.Thread(target=serve, daemon=True).start()
    m3, s3 = pty.openpty()
    ctx_file = os.path.join(ctx_dir, os.path.basename(os.ttyname(s3)) + ".json")
    threading.Thread(target=drain_fd, args=(m3,), daemon=True).start()
    p3 = subprocess.Popen([sys.executable, WRAP] + args, stdin=s3, stdout=s3,
                          stderr=subprocess.PIPE, cwd=work,
                          env=dict(env, CLAUDE_MATE_SOCK=path, CLAUDE_REAL=fake_p,
                                   FAKE_PLAN=plan, **(fake_env or {})))
    os.close(s3)
    seen, deadline = set(), time.time() + 20
    while p3.poll() is None and time.time() < deadline:
        try: seen.add(json.load(open(ctx_file)).get("conversation"))
        except (OSError, ValueError): pass
        time.sleep(0.05)
    try: _, err3 = p3.communicate(timeout=5)
    except subprocess.TimeoutExpired: p3.kill(); _, err3 = p3.communicate()
    time.sleep(0.3)
    if err3.strip():
        print("  wrapper stderr:", err3.decode(errors="replace")[-400:])
    try: logged = json.load(open(log))
    except (OSError, ValueError): logged = {}
    return rx, logged, work, seen


rx1, log1, work1, seen1 = conv_run(
    "known", ["--dangerously-skip-permissions", "--model", "opus", "fix the tests"], seed=True)
argv1 = log1.get("argv", [])
minted = argv1[argv1.index("--session-id") + 1] if "--session-id" in argv1 else ""
ids1 = [hist(p).get("id", "") for _t, p in rx1]
k1 = next((i for i, x in enumerate(ids1) if x), None)
print("\n================ ASSERTIONS (field 9) ================")
check("a cached answer: claude was handed --session-id <uuid>",
      bool(re.fullmatch(UUID, minted)))
check("the launch report names no conversation: it has no transcript yet",
      bool(ids1) and ids1[0] == "")
check("a report naming exactly that conversation follows once its transcript exists",
      k1 is not None and ids1[k1] == minted and rx1[k1][0] > log1.get("at", 0))
check("...sent for the id alone: fields 0-8 are the report before it, state and all",
      k1 is not None and k1 > 0 and rx1[k1][1][:9] == rx1[k1 - 1][1][:9])
check("...and every report after it still names it, 'end' last of all",
      k1 is not None and all(x == minted for x in ids1[k1:]) and rx1[-1][1][0] == "end")
check("nothing reaches the daemon after 'end'",
      [p[0] for _t, p in rx1].count("end") == 1 and rx1[-1][1][0] == "end")
h1 = hist(rx1[-1][1]) if rx1 else {}
check("field 9 says where and as whom: this directory, this config dir",
      os.path.realpath(h1.get("cwd", "")) == os.path.realpath(work1)
      and h1.get("config") == cfg)
check("...and what a resume replays: the flags, not the opening prompt",
      h1.get("flags") == ["--dangerously-skip-permissions", "--model", "opus"])
check("the terminal's context file names the conversation too, and only that one",
      minted in seen1 and seen1 <= {"", minted})

rx2, log2, work2, seen2 = conv_run("new", [], seed=False, plan="turn")
ids2 = [hist(p).get("id", "") for _t, p in rx2]
k2 = next((i for i, x in enumerate(ids2) if x), None)
gap2 = rx2[k2][0] - log2.get("at", 0) if k2 is not None else -1
check("a cold cache: no --session-id, so claude chose the id itself",
      "--session-id" not in log2.get("argv", ["--session-id"])
      and bool(re.fullmatch(UUID, log2.get("sid", ""))))
check(f"its one new transcript in this directory names the conversation, once "
      f"its first turn has run ({gap2:.2f}s after it was written)",
      k2 is not None and ids2[k2] == log2.get("sid") and 0 < gap2 < 1.5
      and rx2[k2][1][0] == "working")
check("...in a report sent for the id alone",
      k2 is not None and k2 > 0 and rx2[k2][1][:9] == rx2[k2 - 1][1][:9])
check("...that 'end' carries too, and nothing follows",
      bool(rx2) and rx2[-1][1][0] == "end" and ids2[-1] == log2.get("sid")
      and [p[0] for _t, p in rx2].count("end") == 1)

# ANOTHER TERMINAL'S NEW CHAT IS NEVER OURS. A second session in this directory
# starts a conversation of its own before this one has run a turn: looked at
# then, it would be the one new transcript here -- and the history would keep
# this terminal's directory, login and flags under the other chat's id. Looked
# at after our turn there are two, and two is a refusal to guess.
rx3, log3, work3, seen3 = conv_run("foreign", [], seed=False, plan="foreign")
ids3 = [hist(p).get("id", "") for _t, p in rx3]
check("a new transcript that appeared before this session's first turn is "
      "never claimed: two new ones, so no conversation is named at all",
      bool(re.fullmatch(UUID, log3.get("foreign", ""))) and bool(rx3)
      and not any(ids3) and seen3 <= {""}
      and "working" in [p[0] for _t, p in rx3])

# -c CARRIES ON A CHAT THAT WAS ALREADY THERE, and claude does not say which:
# its --continue skips `claude -p` and SDK runs, so the newest transcript in
# the directory -- a -p job here, newer than the chat -- is not the one it
# carries on. The one that grows during the session's own turn is.
CHAT, JOB = "0a0a0a0a-0000-4000-8000-00000000000a", "0b0b0b0b-0000-4000-8000-00000000000b"
rx4, log4, work4, seen4 = conv_run(
    "carry", ["-c"], seed=False, plan="carry",
    there=[(CHAT, "cli", 600), (JOB, "sdk-cli", 60)], fake_env={"FAKE_CARRY": CHAT})
ids4 = [hist(p).get("id", "") for _t, p in rx4]
k4 = next((i for i, x in enumerate(ids4) if x), None)
check("-c: the launch report names no conversation, before any turn",
      bool(ids4) and ids4[0] == "")
check("...the chat that grew during its turn names it -- not the newer -p job",
      k4 is not None and ids4[k4] == CHAT and rx4[k4][0] >= log4.get("at", 1e18))
check("...in every report from then on, 'end' last of all, and the context file too",
      k4 is not None and all(x == CHAT for x in ids4[k4:]) and rx4[-1][1][0] == "end"
      and JOB not in ids4 and CHAT in seen4 and seen4 <= {"", CHAT})

# ...AND TWO THAT GROW ARE NO ANSWER: another terminal at work on its own chat
# in this directory during the turn. Either could be ours.
OURS, THEIRS = "0c0c0c0c-0000-4000-8000-00000000000c", "0d0d0d0d-0000-4000-8000-00000000000d"
rx5, log5, work5, seen5 = conv_run(
    "carry-two", ["-c"], seed=False, plan="carry",
    there=[(OURS, "cli", 600), (THEIRS, "cli", 60)],
    fake_env={"FAKE_CARRY": OURS, "FAKE_OTHER": THEIRS})
ids5 = [hist(p).get("id", "") for _t, p in rx5]
check("-c with another chat there growing during the turn: no conversation is "
      "named at all", bool(rx5) and log5.get("sid") == OURS and not any(ids5)
      and seen5 <= {""} and "working" in [p[0] for _t, p in rx5])
ok = all(checks)
shutil.rmtree(tmp, ignore_errors=True)
print("\n================", "ALL PASSED" if ok else "SOME FAILED", "================")
sys.exit(0 if ok else 1)
