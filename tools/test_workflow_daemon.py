#!/usr/bin/env python3
"""
Workflow status in the daemon, with NO hardware: the model, the band's J| line,
and the frame that commits it.

The wrapper lists a session's dynamic workflows in field 8 of its line. The
daemon keeps them beside the session -- never AS its state -- and draws the
shown session's primary one as a band: a J| line immediately before the F| that
commits it, with flags bit2 set. Three things in that sentence shipped wrong in
review before any of it was written, and each has a section here:

  1. A finished run Claude keeps showing must not flap on the device. Its linger
     clock lives on the record, so dropping the record at the end of the linger
     let the next keepalive bring it back as new -- visible a minute, hidden ten
     seconds, visible again, forever. It stays as a hidden tombstone instead.
  2. The run's clock must never step backwards. Every line re-lists elapsed
     time read off a screen that repaints once a second, so it jitters; the
     daemon keeps the EARLIEST anchor while the run keeps running.
  3. A J that only went out when its own bytes changed would leave a lost one
     wrong for a whole linger, because a finished run's J never changes. So J
     goes out before EVERY F that has bit2, and the F is the commit.

And three more that came out of reviewing the code once it was written:

  4. The spotlight is for a run seen to END. A finished record that merely
     appears -- brought back by the wire's three-record window, or renamed by
     a narrower terminal -- ended at a moment nobody saw.
  5. "Heals at the next F" needs a next F. On a frame that does not move (r1
     at h:mm, a paused or finished band) that took up to a minute, so the pair
     goes out every second while the band is up; and a different session's
     band goes out over a cleared stage, so a lost J means no band, never the
     previous session's.
  6. P/D/F infos never repeat the status word: both devices draw it already.

In-process first (an injected `now`, so nothing here waits out 90 s), then the
real daemon over a PTY, then `--mock` -- which runs alongside the rest, since
its demo needs ~25 s to get from "running" to anything else.

Last, a section of its own that only borrows the in-process daemon: three
fixes from reviewing the chat history -- what a press acts on once the device
has gone, when a deleted record counts as deleted, and that a record goes
down before the link to its transcript.

Hermetic: temp sockets and HOME, a PTY for the device, and stubs on PATH for
everything the daemon might shell out to (the Keychain lookup behind the
account chips, curl, open, osascript), so a handshake reads no real login and
makes no network call.

Run:   python3 tools/test_workflow_daemon.py      (needs pyserial)
"""
import atexit
import errno
import importlib.util
import json
import os
import pty
import re
import shutil
import site
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tty
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DAEMON = os.path.join(ROOT, "daemon", "claude_mate_daemon.py")

try:
    import serial  # noqa: F401  (the daemon needs it; fail early & clearly)
except ImportError:
    print("SKIP: pyserial not installed. Run: pip install pyserial")
    sys.exit(0)

failures = []
checks = 0


def check(name, ok, detail=""):
    global checks
    checks += 1
    print(f"   {'ok  ' if ok else 'FAIL'}  {name}"
          + (f"   [{detail}]" if (detail and not ok) else ""))
    if not ok:
        failures.append(name)


def wait_for(pred, timeout=8.0, tick=0.05):
    end = time.time() + timeout
    while time.time() < end:
        val = pred()
        if val:
            return val
        time.sleep(tick)
    return False


# The accepted grammar, verbatim from the spec. The firmware drops anything else.
J_RE = re.compile(r"^J\|(-|[A-Z]\|\d{1,5}\|\d{1,5}\|\d{1,5}\|[^|]{0,14}"
                  r"\|[^|]{0,40})$", re.ASCII)


def j_ok(line):
    return (bool(J_RE.match(line)) and line.isascii()
            and len(line.encode()) <= 95)


def flags(f):
    p = f.split("|")
    return int(p[1]) if len(p) >= 7 and p[1].isdigit() else 0


def subject(f):
    p = f.split("|")
    return p[3] if len(p) >= 7 else ""


def jf(lines):
    """The J| and F| lines only, in order: pings, LED and account lines from
    other threads may land between a J and its F on the wire, and the
    firmware does not care -- it stages the J until an F arrives."""
    return [l for l in lines if l.startswith("J|") or l.startswith("F|")]


def unpaired(lines):
    """F lines with bit2 whose nearest J/F neighbour before them is not a J
    for a workflow. Empty means every band frame was committed from a J."""
    seq = jf(lines)
    bad = []
    for i, l in enumerate(seq):
        if l.startswith("F|") and flags(l) & 4:
            if i == 0 or not seq[i - 1].startswith("J|") or seq[i - 1] == "J|-":
                bad.append(l)
    return bad


tmp = tempfile.mkdtemp(prefix="cm-wf-")

# --------------------------------------------------------------------------- #
# The environment every daemon here runs in
# --------------------------------------------------------------------------- #
stubs = os.path.join(tmp, "bin")
os.makedirs(stubs)
for prog in ("security", "curl", "open", "osascript", "afplay", "dns-sd"):
    p = os.path.join(stubs, prog)
    with open(p, "w") as fh:
        fh.write("#!/bin/sh\nexit 1\n")
    os.chmod(p, 0o755)
home = os.path.join(tmp, "home")
os.makedirs(home)


def daemon_env(port, sock):
    env = dict(os.environ)
    for k in ("CLAUDE_MATE_TCP", "CLAUDE_MATE_BLE", "CLAUDE_MATE_WEB",
              "CLAUDE_MATE_SOUND", "CLAUDE_MATE_TOKEN"):
        env.pop(k, None)
    env.update({
        "CLAUDE_MATE_PORT": port,
        "CLAUDE_MATE_SOCK": sock,
        "CLAUDE_MATE_BAUD": "115200",
        "CLAUDE_MATE_ACCOUNTS_DIR": os.path.join(tmp, "no-accounts"),
        # A HOME of its own, so neither the account-chip lookup nor the sound
        # cache touches the real one -- with the user site kept importable,
        # in case that is where pyserial lives.
        "HOME": home,
        "PYTHONUSERBASE": site.getuserbase(),
        "PATH": stubs + os.pathsep + env.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
    })
    return env


class FakeDevice:
    """A PTY standing in for the device: everything the daemon writes, in order."""

    def __init__(self):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.master)
        self.port = os.ttyname(self.slave)
        self.lines = []
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = b""
        while True:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\n" in buf:
                ln, buf = buf.split(b"\n", 1)
                s = ln.decode("ascii", "replace").strip()
                if s:
                    with self.lock:
                        self.lines.append(s)

    def send(self, s):
        os.write(self.master, (s + "\n").encode())

    def mark(self):
        with self.lock:
            return len(self.lines)

    def since(self, i=0):
        with self.lock:
            return list(self.lines[i:])


def start_daemon(dev, sock, *args):
    proc = subprocess.Popen([sys.executable, DAEMON, *args],
                            env=daemon_env(dev.port, sock),
                            stderr=subprocess.PIPE, text=True)
    log = []
    threading.Thread(target=lambda: [log.append(l.rstrip())
                                     for l in proc.stderr],
                     daemon=True).start()
    return proc, log


def stop_daemon(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# --------------------------------------------------------------------------- #
# --mock starts first: it needs ~25 s of 4 s steps to get past "running"
# --------------------------------------------------------------------------- #
mock_dev = FakeDevice()
mock_started = time.time()
mock_proc, mock_log = start_daemon(mock_dev, os.path.join(tmp, "mock.sock"),
                                   "--mock")
# Stopped at exit whatever happens in between: a test that dies on an
# unexpected exception must not leave a daemon running behind it.
atexit.register(stop_daemon, mock_proc)

# --------------------------------------------------------------------------- #
# The daemon, in process
# --------------------------------------------------------------------------- #
_spec = importlib.util.spec_from_file_location("cm_wf_daemon", DAEMON)
D = importlib.util.module_from_spec(_spec)
sys.modules["cm_wf_daemon"] = D        # @dataclass resolves through sys.modules
_spec.loader.exec_module(D)

logged = []
_real_log = D.log
D.log = lambda msg: logged.append(msg)  # the module's own calls land here


def wf_logs():
    return [m for m in logged if m.startswith("workflow:")]


T0 = 1_000_000.0                        # an injected clock, far from real time


def reg_with(wf, ctrl="/tmp/cm-wf-test-ctrl.sock", state="working", now=T0,
             reg=None):
    reg = reg or D.Registry()
    reg.update(state, "s1", "proj", focus_ctrl=ctrl, wf=wf, now=now)
    return reg


def recs(reg, key="s1"):
    with reg._lock:
        return reg._sessions[key].workflows


print("\n== the constants other parts of the feature are built against ==")
check("WF_INFO_CHARS is 14 (the firmware's WF_INFO_MAX)", D.WF_INFO_CHARS == 14)
check("WF_NAME_CHARS is 40 (the S3's WF_NAME_MAX)", D.WF_NAME_CHARS == 40)
check("WF_STALE_S 90, WF_LINGER_S 60, WF_SPOTLIGHT_S 10",
      (D.WF_STALE_S, D.WF_LINGER_S, D.WF_SPOTLIGHT_S) == (90, 60, 10))
# getattr: a daemon without it fails this check, not the rest of the file.
check("WF_RESEND_S 1: while the band is up its pair goes out every second",
      getattr(D, "WF_RESEND_S", None) == 1.0)

# --------------------------------------------------------------------------- #
print("\n== field 8: keep, clear, replace ==")
check("no field at all is KEEP (None)", D.parse_wf(None) is None)
check("an empty field is KEEP too, like every other trailing field",
      D.parse_wf("") is None and D.parse_wf("   ") is None)
check("'-' is CLEAR ([])", D.parse_wf("-") == [])
one = D.parse_wf("R;0;5;;0;159;542800;;deployment-risk-sweep-3802")
check("the screenshot's row parses to one record with every number",
      one is not None and len(one) == 1
      and (one[0].st, one[0].done, one[0].total, one[0].failed, one[0].pct,
           one[0].elapsed_s, one[0].tokens, one[0].name)
      == ("R", 0, 5, None, 0, 159, 542800, "deployment-risk-sweep-3802"))
check("an unknown status letter is skipped, not drawn as something else",
      D.parse_wf("X;1;2;;;;;;n") is None)
check("...and so is a record with a garbled count, or too few sub-fields",
      D.parse_wf("R;one;2;;;;;;n") is None and D.parse_wf("R;1;2") is None
      and D.parse_wf("R;-1;2;;;;;;n") is None)
mix = D.parse_wf("Q;1;2;;;;;;bad~D;5;5;;100;60;10;;good~R;x;;;;;;;worse")
check("a mix keeps only the valid records",
      mix is not None and [r.name for r in mix] == ["good"])
check("the name is the LAST sub-field, so it may contain ';'",
      D.parse_wf("R;;;;;;;;a;b;c")[0].name == "a;b;c")
check("at most three records are kept",
      len(D.parse_wf("~".join(f"R;;;;;;;;n{i}" for i in range(5)))) == 3)
check("a bar past 100% is held at 100", D.parse_wf("R;;;;250;;;;n")[0].pct == 100)

reg = reg_with("R;1;5;;20;60;100;;sweep")
reg.update("working", "s1", "proj", wf=None, now=T0 + 1)
check("an 8-field line (no field 8) keeps the workflows", len(recs(reg)) == 1)
reg.update("working", "s1", "proj", wf="", now=T0 + 2)
check("an empty field 8 keeps them", len(recs(reg)) == 1)
reg.update("working", "s1", "proj", wf="Z;1;2;;;;;;nope", now=T0 + 3)
check("a field with no valid record keeps them",
      [r.name for r in recs(reg)] == ["sweep"])
reg.update("working", "s1", "proj", wf="-", now=T0 + 4)
check("'-' clears them", recs(reg) == ())

# Through the socket parser, the way a wrapper line actually arrives.
srv = D.SocketServer(os.path.join(tmp, "unused.sock"), D.Registry(),
                     on_update=lambda: None)
srv._process_line(b"working|s9|nine|/tmp/c.sock|m|e|a|l|"
                  b"R;1;5;0;20;60;100;;sweep|a-field-from-the-future")
nine = recs(srv._reg, "s9")
check("a field 9 from a newer producer is ignored, not glued onto the name",
      len(nine) == 1 and nine[0].name == "sweep")
check("...and a line with a ctrl socket marks its records live (a wrapper)",
      nine[0].live is True)
srv._process_line(b"working|s8|eight||||||R;;;;;;;;hooked")
check("...while one without is not (a hook: nothing to go stale against)",
      recs(srv._reg, "s8")[0].live is False)

# --------------------------------------------------------------------------- #
print("\n== workflow data never touches the session's state ==")
reg = D.Registry()
reg.update("working", "s1", "proj", focus_ctrl="/x", now=T0)
reg.update("idle", "s1", "proj", focus_ctrl="/x", now=T0 + 5)  # -> done, unacked
with reg._lock:
    s = reg._sessions["s1"]
    before = (s.state, s.acked, s.state_since, s.last_ack_state, s.last_ack_ts)
got = reg.update("idle", "s1", "proj", focus_ctrl="/x",
                 wf="D;5;5;;100;300;9000;;sweep", now=T0 + 9)
with reg._lock:
    after = (s.state, s.acked, s.state_since, s.last_ack_state, s.last_ack_ts)
check("a finished run on an unacknowledged DONE leaves it DONE and unacked",
      before == after and before[0] == "done" and before[1] is False,
      f"{before} -> {after}")
check("...and returns no haptic (no LED, no sound, no V| line)", got is None)
got = reg.update("idle", "s1", "proj", focus_ctrl="/x",
                 wf="R;0;3;;0;5;10;;again", now=T0 + 12)
check("...nor does a new run starting on it", got is None and s.state == "done")

# --------------------------------------------------------------------------- #
print("\n== staleness is the PRODUCER's, not the record's ==")
# A wrapper's R row with no clock (a narrow terminal) and a paused row (which
# never shows one) are exactly the records an "elapsed known" rule missed.
reg = reg_with("R;;;;40;;;;narrow~P;;;;;;;til 3:30pm;paused")
live = recs(reg)
check("a live record is visible up to WF_STALE_S after it was last listed",
      all(D.wf_visible(r, T0 + 90) for r in live))
check("...and hidden after, clock or no clock",
      not any(D.wf_visible(r, T0 + 91) for r in live))
hook = reg_with("R;;;;;;;;hooked", ctrl="")
check("a hook's record (no ctrl socket) is still visible at 300 s",
      D.wf_visible(recs(hook)[0], T0 + 300))
logged.clear()
reg.prune(now=T0 + 95)
check("prune() drops the stale running and paused records",
      recs(reg) == ())
check("...saying so once per record",
      len(wf_logs()) == 2 and all("-> -" in m for m in wf_logs()),
      repr(wf_logs()))
hook.prune(now=T0 + 300)
check("...and leaves a hook's record alone", len(recs(hook)) == 1)

# --------------------------------------------------------------------------- #
print("\n== a finished run lingers once, then stays gone ==")
reg = D.Registry()
seen_visible = []
for t in range(0, 151, 5):
    reg.update("working", "s1", "proj", focus_ctrl="/x",
               wf="D;5;5;;100;300;9000;;sweep", now=T0 + t)
    seen_visible.append((t, D.wf_visible(recs(reg)[0], T0 + t)))
check("a D record re-listed every 5 s is visible for its first 60 s",
      all(v for t, v in seen_visible if t <= 60))
check("...and NOT resurrected by the keepalives after (65..150 s)",
      not any(v for t, v in seen_visible if t >= 65), repr(seen_visible))
check("...because it stays as a tombstone carrying its first ended_ts",
      recs(reg)[0].ended_ts == T0)
reg.prune(now=T0 + 200)
check("prune() never drops a tombstone (that would let it come back)",
      len(recs(reg)) == 1)
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf="R;0;2;;0;1;1;;other", now=T0 + 155)
check("...it leaves once a line no longer lists it",
      [r.name for r in recs(reg)] == ["other"])
reg = reg_with("D;5;5;;100;300;9000;;sweep")
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf="F;4;5;1;80;300;9000;;sweep", now=T0 + 30)
check("D -> F keeps the original ended_ts (the linger does not restart)",
      recs(reg)[0].ended_ts == T0)
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf="R;4;5;;80;301;9000;;sweep", now=T0 + 31)
check("...and going back to running clears it", recs(reg)[0].ended_ts is None)

# --------------------------------------------------------------------------- #
print("\n== the clock never steps backwards ==")
reg = D.Registry()
shown = []
sends = {0: 120, 5: 124, 10: 130, 15: 130, 20: 130}   # 124: a 1 s scrape lag;
for t in range(0, 26):                                # 130 x3: a stale re-send
    if t in sends:
        reg.update("working", "s1", "proj", focus_ctrl="/x",
                   wf=f"R;1;5;;20;{sends[t]};100;;sweep", now=T0 + t)
    shown.append(D.wf_elapsed(recs(reg)[0], T0 + t))
check("sampled every second, the displayed elapsed never decreases",
      all(b >= a for a, b in zip(shown, shown[1:])), repr(shown))
check("...and ticks on its own between lines (no per-second socket line)",
      shown[3] == 123.0 and shown[25] == 145.0, repr(shown))
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf="P;1;5;;;150;100;til 3:30pm;sweep", now=T0 + 30)
check("a paused run shows its frozen clock",
      D.wf_elapsed(recs(reg)[0], T0 + 90) == 150.0)
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf="R;1;5;;20;100;100;;sweep", now=T0 + 40)
check("re-entering R from P starts a fresh anchor (no min with the old one)",
      D.wf_elapsed(recs(reg)[0], T0 + 40) == 100.0)
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf="R;1;5;;20;;100;;sweep", now=T0 + 45)
check("a clock the screen stopped showing keeps running from its anchor",
      D.wf_elapsed(recs(reg)[0], T0 + 50) == 110.0)

reg = reg_with("R;0;5;;0;10;1;;twin~R;0;5;;0;500;1;;twin")
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf="R;0;5;;0;15;1;;twin~D;5;5;;100;505;1;;twin", now=T0 + 5)
a, b = recs(reg)
check("two runs with ONE name keep their own anchors (the k-th of that name)",
      D.wf_elapsed(a, T0 + 5) == 15.0 and b.ended_ts == T0 + 5,
      f"{D.wf_elapsed(a, T0 + 5)} {b.ended_ts}")

# --------------------------------------------------------------------------- #
print("\n== which record the band shows ==")
reg = reg_with("D;5;5;;100;60;1;;old~R;1;5;;20;30;1;;going~P;;;;;;;;wait")
first, extra = D.wf_primary(recs(reg), T0 + 11)
check("past the spotlight, the first record still in flight takes the band",
      first.name == "going" and extra == 2, f"{first.name} +{extra}")
first, extra = D.wf_primary(recs(reg), T0)
check("...and inside it too, when the finished one was never seen running: "
      "nobody saw it end, so it may have ended long ago",
      first.name == "going" and extra == 2, f"{first.name} +{extra}")
check("...though it lingers like any finished run, WF_LINGER_S from first "
      "sight", D.wf_visible(recs(reg)[0], T0 + 60)
      and not D.wf_visible(recs(reg)[0], T0 + 61))
reg = reg_with("R;1;5;;20;30;1;;a~R;2;5;;40;30;1;;b")
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf="R;1;5;;20;35;1;;a~D;5;5;;100;35;1;;b", now=T0 + 5)
p0, _ = D.wf_primary(recs(reg), T0 + 5)
p1, _ = D.wf_primary(recs(reg), T0 + 15)
p2, _ = D.wf_primary(recs(reg), T0 + 16)
check("a run that just ended takes the band for WF_SPOTLIGHT_S...",
      p0.name == "b" and p1.name == "b")
check("...then hands it back to the one still running", p2.name == "a")
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf="R;1;5;;20;37;1;;a~F;4;5;;80;35;1;;b", now=T0 + 7)
p0, _ = D.wf_primary(recs(reg), T0 + 15)
p1, _ = D.wf_primary(recs(reg), T0 + 16)
check("...a D that turns F is the same ending: same spotlight, same 10 s",
      p0.name == "b" and p1.name == "a", f"{p0.name} {p1.name}")

# THE SPOTLIGHT NAMES THE RUN THAT JUST ENDED, or nothing. Four runs, three on
# the wire, running ones first -- the order the wrapper first sent them in:
# when alpha ends, three running runs outrank it and it drops off the wire
# unseen; when bravo ends, alpha comes back -- finished twenty seconds ago, and
# new to the daemon. Spotlighting what was merely new announced alpha as just
# done, and bravo never. Whatever order a producer picks, a record the daemon
# never saw running has no ending it saw.
reg = reg_with("R;2;5;;40;300;1;;alpha~R;2;5;;40;200;1;;bravo"
               "~R;1;5;;20;100;1;;charlie")
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf="R;2;5;;40;220;1;;bravo~R;1;5;;20;120;1;;charlie"
              "~R;1;5;;20;70;1;;xray", now=T0 + 20)
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf="D;5;5;;100;320;1;;alpha~R;1;5;;20;140;1;;charlie"
              "~R;1;5;;20;90;1;;xray", now=T0 + 40)
p, extra = D.wf_primary(recs(reg), T0 + 40)
check("a finished run the wire left off and then brought back is not "
      "announced as just ended: the band stays on a running one",
      p.name == "charlie" and extra == 2, f"{p.name} +{extra}")
# ...and the same for a name Claude cuts afresh when the terminal narrows: a
# new key, first seen finished.
cut = "release-risk-sweep-with-a-l…"
reg = reg_with(f"R;3;5;;60;200;1;;{cut}~R;1;5;;20;50;1;;triage")
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf=f"D;5;5;;100;205;1;;{cut}~R;1;5;;20;55;1;;triage", now=T0 + 5)
p0, _ = D.wf_primary(recs(reg), T0 + 5)
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf="D;5;5;;100;205;1;;release-risk…~R;1;5;;20;75;1;;triage",
           now=T0 + 25)
p1, _ = D.wf_primary(recs(reg), T0 + 25)
check("a run seen to end takes the spotlight once...", p0.name == cut, p0.name)
check("...and not again when a narrower terminal re-cuts its name",
      p1.name == "triage", p1.name)
reg = reg_with("D;5;5;;100;60;1;;x~F;3;5;2;60;61;1;;y")
p, extra = D.wf_primary(recs(reg), T0 + 30)
check("with only finished runs, the first of them", p.name == "x" and extra == 1)
check("with nothing visible, nothing", D.wf_primary(recs(reg), T0 + 61)
      == (None, 0))

# --------------------------------------------------------------------------- #
print("\n== the info string ==")


def info(wf, extra=0, at=0.0):
    r = recs(reg_with(wf))[0]
    return D.wf_info(r, extra, T0 + at)


check("count, Claude's clock, tokens", info("R;0;5;;0;159;542800;;n")
      == "0/5 2m39s 543k", info("R;0;5;;0;159;542800;;n"))
check("...and '+N' outranks the tokens when it does not all fit",
      info("R;0;5;;0;159;542800;;n", extra=1) == "0/5 2m39s +1")
check("the bar's percent stands in for an unknown count",
      info("R;;;;40;100;;;n") == "40% 1m40s")
check("an hour-long clock reads 1h02m, not a mm:ss that looks like minutes",
      info("R;3;12;;25;3720;;;n") == "3/12 1h02m")
check("...and a day-long one 1d03h", info("R;;;;;97200;;;n") == "1d03h")
check("big counts give up the clock before the count",
      info("R;412;912;;45;37375;23065249;;n") == "412/912 10h22m"
      and info("R;412;912;;45;37375;23065249;;n", extra=2) == "412/912 +2")
check("a paused run with a note shows the note",
      info("P;2;9;;;600;90000;til 3:30pm;n") == "til 3:30pm")
# WHO DRAWS THE WORD decides what stands in for nothing. The Nano draws a bar
# and no word for a running run, so an R/E info with nothing else in it says
# 'running'. Both devices draw 'paused', 'done' and 'ended' themselves -- the
# Nano where the bar would be, the S3 on the band's first line -- so a P/D/F
# info that said it too put "paused ... paused" on the glass.
check("nothing known about a running run (a hook's record): its status word",
      info("R;;;;;;;;n") == "running" and info("E;;;;;;;;n") == "running")
check("...plus the others, when there are some", info("R;;;;;;;;n", extra=2)
      == "running +2")
bare = [info(f"{st};;;;;;;;n") for st in "PDF"]
check("...but a paused one without a note, a done or an ended one says "
      "nothing: the device draws that word itself", bare == ["", "", ""],
      repr(bare))
bare = [info(f"{st};;;;;;;;n", extra=2) for st in "PDF"]
check("...only the others, when there are some", bare == ["+2"] * 3, repr(bare))
bare = D.wf_j_line(recs(reg_with("P;;;;;;;;nightly-triage"))[0], 0, T0)
check("...and a J with an empty info is one the firmware accepts",
      bare == "J|P|0|0|0||nightly-triage" and j_ok(bare), bare)
check("a frozen clock for a finished run", info("D;5;5;;100;300;9000;;n", at=30)
      == "5/5 5m00s 9.0k")
check("every info is ASCII and at most 14", all(
    len(info(w, e)) <= 14 and info(w, e).isascii()
    for w in ("R;65535;65535;;100;999999999;999999999999;;n",
              "P;1;2;;;;;til Oct 11 3:30pm (Europe/Madrid);n",
              "P;;;;;;;résumé|à 3h;n", "E;123456789;987654321;;;;;;n")
    for e in (0, 2)))

print("\n== compact tokens, at every boundary ==")
for n, want in ((0, "0"), (999, "999"), (1000, "1.0k"), (9949, "9.9k"),
                (9950, "10k"), (99_950, "100k"), (542_800, "543k"),
                (999_499, "999k"), (999_950, "1.0M"), (1_400_000, "1.4M"),
                (9_960_000, "10M"), (12_000_000, "12M"),
                (999_600_000, "1.0B"), (1_200_000_000, "1.2B")):
    check(f"{n:>13,} -> {want}", D._wf_tokens(n) == want, D._wf_tokens(n))
check("nothing ever reads '1000k' or '1000M'",
      not any(D._wf_tokens(n).startswith("1000")
              for n in range(999_000, 1_001_000, 7)))

# --------------------------------------------------------------------------- #
print("\n== the name, and the whole J| line ==")
check("non-ASCII becomes '?', the ellipsis '~', a bar '/', control chars go",
      D._wf_j_name("café|x…\x07y", 0) == "caf?/x~y")
nm = D._wf_j_name("n" * 40, 2)
check("' +N' sits INSIDE the 40, and a name cut for it ends in '~'",
      len(nm) == 40 and nm.endswith("~ +2"), nm)
check("a short name keeps all of itself", D._wf_j_name("sweep", 1) == "sweep +1")

r = recs(reg_with("E;3;7;1;42;60;100;;order"))[0]
line = D.wf_j_line(r, 0, T0)
check("J is done|FAILED|total -- not the socket's order (3/7, 1 failed)",
      line.startswith("J|E|3|1|7|"), line)
check("only a percent known: the bar is pct of 100",
      D.wf_j_line(recs(reg_with("R;;;;40;;;;p"))[0], 0, T0)
      .startswith("J|R|40|0|100|"))
check("nothing numeric known: 0|0|0, which both firmwares draw as no bar",
      D.wf_j_line(recs(reg_with("R;;;;;;;;p"))[0], 0, T0)
      .startswith("J|R|0|0|0|running|"))

# Adversarial: a wf string handed straight to Registry.update can carry what
# the socket's own '|' split never could -- a bar inside a name.
evil = ("R;99999999;99999999;99999999;100;999999999;999999999999;;"
        + "名前|…" * 30
        + "~E;1;2;;50;10;100;;b|c\x1b[31m~D;5;5;;100;60;10;;" + "é" * 80)
reg = reg_with(evil)
lines = []
for t in (0, 5, 30, 61, 200):
    for r in recs(reg):
        if D.wf_visible(r, T0 + t):
            p, extra = D.wf_primary(recs(reg), T0 + t)
            lines.append(D.wf_j_line(p, extra, T0 + t))
            lines.append(D.wf_j_line(r, 2, T0 + t))
check("adversarial records still make J lines in the firmware's grammar",
      lines and all(j_ok(l) for l in lines),
      repr([l for l in lines if not j_ok(l)][:2]))
check("...at most 77 bytes, under the Nano's 95",
      max(len(l) for l in lines) <= 77, str(max(len(l) for l in lines)))
check("...with huge counts saturated at the firmware's 65535",
      any(l.startswith("J|R|65535|65535|65535|") for l in lines))
check("...and three records naming ' +2' within the 40",
      any(l.endswith(" +2") for l in lines))

# --------------------------------------------------------------------------- #
print("\n== the log: one line per start or finish, never per keepalive ==")
logged.clear()
reg = D.Registry()
for t in range(0, 100):                 # 100 keepalives, only numbers moving
    reg.update("working", "s1", "proj", focus_ctrl="/x",
               wf=f"R;{t % 5};5;;{t};{60 + t};{1000 + 37 * t};;sweep",
               now=T0 + t)
check("a run appearing is ONE line", wf_logs() == ['workflow: proj "sweep" - -> R'],
      repr(wf_logs()))
logged.clear()
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf="D;5;5;;100;200;5000;;sweep", now=T0 + 101)
reg.update("working", "s1", "proj", focus_ctrl="/x",
           wf="D;5;5;;100;200;5000;;sweep", now=T0 + 120)
check("R -> D is exactly one more, and its keepalive none",
      wf_logs() == ['workflow: proj "sweep" R -> D'], repr(wf_logs()))
logged.clear()
reg.update("working", "s1", "proj", focus_ctrl="/x", wf="-", now=T0 + 130)
check("...and its leaving one more", wf_logs() == ['workflow: proj "sweep" D -> -'])

# --------------------------------------------------------------------------- #
# The frame. Under a fixed clock, so a second boundary cannot make two frames
# composed back to back differ for a reason that is not under test.
# --------------------------------------------------------------------------- #


class _Clock:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def strftime(self, *a):
        return time.strftime(*a)

    def sleep(self, s):
        pass


clock = _Clock(T0)
D.time = clock                          # the module's `time`, not the real one

print("\n== _compose keeps its contract, and sets only bit2 ==")
scr = object.__new__(D.Screen)          # exactly what test_device_actions does
scr._follow, scr._notice = False, None
check("_compose([], None) is still a 3-tuple with the no-sessions frame",
      scr._compose([], None) == ("F|0|-1|MATE|no sessions||", None, False))
check("...and 'no sessions' NEVER has bit2, asked for or not",
      scr._compose([], None, wf_show=True)[0] == "F|0|-1|MATE|no sessions||")
scr._follow = True
check("...with FOLLOW on either", flags(scr._compose([], None, wf_show=True)[0])
      == 2)
scr._follow = False

reg = D.Registry()
reg.update("working", "sa", "alpha", focus_ctrl="/x", model="Opus 4.8",
           effort="xhigh", account="work", limit="5h82%", now=T0)
q = reg.queue()
s = q[0]
plain = scr._compose(q, s)
check("a session's frame is byte-identical to today's (pinned)",
      plain == ("F|0|4|alpha|WORK  00:00      work|Opus 4.8 xhigh  5h82%|1/1 W",
                "sa", False), repr(plain))
reg.update("working", "sa", "alpha", focus_ctrl="/x",
           wf="R;1;5;;20;60;100;;sweep", now=T0)
check("...and stays so with workflows on it, until the band is asked for",
      scr._compose(q, s) == plain)
band = scr._compose(q, s, wf_show=True)
p0, p1 = plain[0].split("|", 6), band[0].split("|", 6)
check("wf_show sets bit2 and nothing else: sel, r0..r3 unchanged",
      flags(band[0]) == 4 and p0[2:] == p1[2:] and band[1:] == plain[1:])
reg.update("idle", "sa", "alpha", now=T0)             # -> DONE, flashing
scr._follow = True
fl = flags(scr._compose(reg.queue(), s, wf_show=True)[0])
check("bit0 (flash) and bit1 (FOLLOW) ride alongside it untouched", fl == 7, fl)
scr._follow = False
scr._notice = ("already on work 2", T0 + 4)
noted = scr._compose(q, s, wf_show=True)
check("a notice in r1/r2 clears bit2 (on the Nano the band IS r2)",
      flags(noted[0]) & 4 == 0 and "already on work 2" in noted[0])
scr._notice = None

print("\n== _wf_view: one visibility check for bit2 and its J ==")
check("no subject: J|- and no band", D.Screen._wf_view(None, T0) == ("J|-", False))
reg = reg_with("R;1;5;;20;60;100;;sweep")
s = reg.queue()[0]
j, show = D.Screen._wf_view(s, T0 + 90)
check("a live record at the stale boundary: the band and its J together",
      show and j.startswith("J|R|1|0|5|"))
check("...one second past it: neither", D.Screen._wf_view(s, T0 + 91)
      == ("J|-", False))
bad = D.Session(key="b", name="b")
bad.workflows = ("not a record",)
logged.clear()
check("a workflow the code did not anticipate costs the band, not the frame",
      D.Screen._wf_view(bad, T0) == ("J|-", False)
      and any("failed" in m for m in logged))

# --------------------------------------------------------------------------- #
print("\n== refresh(): J before every F that has bit2; J|- once ==")


class RecLink:
    """A device link that remembers every line, and can be made to refuse."""

    def __init__(self):
        self.lines, self.ok = [], True

    def write_line(self, line):
        if not self.ok:
            return False
        self.lines.append(line)
        return True

    def take(self):
        out, self.lines = self.lines, []
        return out


D.sfx_build_cache = lambda: {}          # no WAVs written by a test's Screen
link = RecLink()
reg = D.Registry()
reg.update("working", "sa", "alpha", focus_ctrl="/x",
           wf="R;1;5;;20;60;12000;;sweep", now=T0)
reg.update("working", "sb", "bravo", focus_ctrl="/y", now=T0)
scr = D.Screen(link, reg)
scr.refresh()
got = link.take()
check("the first band goes out over a cleared stage -- 'J|-', its J, then F "
      "with bit2 -- since a device that outlived the last daemon may hold a J",
      len(got) == 3 and got[0] == "J|-"
      and got[1] == "J|R|1|0|5|1/5 1m00s 12k|sweep"
      and got[2].startswith("F|4|") and subject(got[2]) == "alpha", repr(got))
scr.refresh()
check("nothing changed: nothing is written", link.take() == [])
clock.t += 1
scr.refresh()
got = link.take()
check("a tick: the pair again (the clock moved in both)",
      len(got) == 2 and got[0].startswith("J|") and "1m01s" in got[0]
      and got[1].startswith("F|4|"))
reg.update("working", "sa", "alpha", focus_ctrl="/x",
           wf="R;2;5;;40;61;12000;;sweep", now=clock.t)
scr.refresh()
got2 = link.take()
check("a J that changed under an unchanged frame still goes out WITH its F "
      "(the F is the commit, even with the same bytes)",
      len(got2) == 2 and got2[0].startswith("J|R|2|0|5|")
      and got2[1] == got[1], repr(got2))
scr.refresh(force=True)
got3 = link.take()
check("force (a handshake): J then F, both re-sent unchanged",
      got3 == got2, repr(got3))

scr.nav(+1)                             # alpha -> bravo
got = link.take()
check("moving to a session with no run: 'J|-' then its F, without bit2",
      len(got) == 2 and got[0] == "J|-" and subject(got[1]) == "bravo"
      and flags(got[1]) & 4 == 0, repr(got))
clock.t += 1
scr.refresh()
got = link.take()
check("...and only once: the next tick is the F alone",
      len(got) == 1 and got[0].startswith("F|0|"), repr(got))
scr.refresh(force=True)
check("...though a handshake re-sends it ahead of the frame",
      [l[:3] for l in link.take()] == ["J|-", "F|0"])
scr.nav(-1)                             # back to alpha
got = link.take()
check("moving back: its J, then its F with bit2",
      len(got) == 2 and got[0].startswith("J|R|2|0|5|")
      and flags(got[1]) == 4)

link.ok = False
clock.t += 1
scr.refresh()
check("a J the link refused is not committed...", scr._last_j == got[0])
check("...and no F goes out without the J it commits", link.lines == [])
link.ok = True
scr.refresh()
got = link.take()
check("the next tick sends the pair", len(got) == 2 and got[0].startswith("J|")
      and got[1].startswith("F|4|"))

# A finished run's J never changes. It must still precede every F.
reg.update("working", "sa", "alpha", focus_ctrl="/x",
           wf="D;5;5;;100;300;99000;;sweep", now=clock.t)
seen = []
for _ in range(5):
    clock.t += 1
    scr.refresh()
    seen += link.take()
js = [l for l in seen if l.startswith("J|")]
check("a finished run: its unchanging J goes out before every ticking F",
      len(js) == 5 and len(set(js)) == 1 and not unpaired(seen)
      and js[0] == "J|D|5|0|5|5/5 5m00s 99k|sweep", repr(seen[:2]))
clock.t += D.WF_LINGER_S
scr.refresh()
got = link.take()
check("past its linger the band comes down: 'J|-', then F without bit2",
      [l[:3] for l in got] == ["J|-", "F|0"], repr(got))
scr._notice = ("hello there", clock.t + 4)
reg.update("working", "sa", "alpha", focus_ctrl="/x",
           wf="R;1;5;;20;60;12000;;sweep", now=clock.t)
scr.refresh()
got = link.take()
check("under a notice the J still leads, and the F says the band is down",
      len(got) == 2 and got[0].startswith("J|R|") and flags(got[1]) & 4 == 0)
scr._notice = None

# --------------------------------------------------------------------------- #
print("\n== a lost J costs a second, and never shows another session's band ==")
# A J can be lost while the F behind it arrives: a BLE write is "done" once it
# is queued, and a UART can overrun. The firmware's staged J survives a commit,
# so the F then commits whatever was staged before. These run the daemon's
# lines through the firmware's own rule (workflow.h): a J| is staged and draws
# nothing, 'J|-' stages a clear, a lost J leaves the stage as it was, and every
# F| commits the stage -- drawn iff that F has bit2 and the stage is a band.
# On frames that do NOT move: both sessions two hours in their state, so r1
# reads h:mm, and paused bands, whose J is constant. That is where a lost J
# used to stay wrong for up to a minute.


def glass(lines):
    """[(the F's r0, the J it committed -- None if no band)] per F|."""
    staged, out = None, []
    for l in lines:
        if l == "J|-":
            staged = None
        elif l.startswith("J|"):
            staged = l
        elif l.startswith("F|"):
            out.append((subject(l), staged if flags(l) & 4 else None))
    return out


class LossyLink(RecLink):
    """Says yes to every line and loses the ones `drop` picks, as a radio or
    an overrun UART does. `lines` is what arrived; `wrote` what was sent."""

    def __init__(self):
        super().__init__()
        self.wrote, self.drop = [], (lambda line: False)

    def write_line(self, line):
        self.wrote.append(line)
        return True if self.drop(line) else super().write_line(line)


P_API = "P;;;;;3125;542800;til 3:30pm;sweep-api"
P_ZETA = "P;;;;;615;12000;til Oct 11 3pm;triage-zeta"
clock.t += 1000                          # clear of everything above
link = LossyLink()
reg = D.Registry()
for sid, name, wf in (("sa", "api", P_API), ("sz", "zeta", P_ZETA),
                      ("su", "zulu", None)):
    reg.update("working", sid, name, focus_ctrl="/x", now=clock.t - 7200)
    reg.update("working", sid, name, focus_ctrl="/x", wf=wf, now=clock.t)
scr = D.Screen(link, reg)
scr.refresh()
api_j = next((l for l in link.lines if l.startswith("J|P|")), "")
check("api's paused band is on the glass", glass(link.lines)[-1:]
      == [("api", api_j)] and api_j.endswith("|sweep-api"), repr(link.lines))
check("...and its frame, at h:mm, does not move from second to second",
      "WORK  2:00" in link.lines[-1], link.lines[-1])
link.wrote = []
link.drop = lambda line: line.startswith("J|") and line != "J|-"
scr.nav(+1)                              # NEXT: api -> zeta, zeta's J is lost
check("NEXT to another session's band: 'J|-' first, then its J, then its F",
      [l[:3] for l in link.wrote] == ["J|-", "J|P", "F|4"]
      and link.wrote[1].endswith("|triage-zeta"), repr(link.wrote))
check("...so the lost J leaves zeta with NO band, never api's under its name",
      glass(link.lines)[-1] == ("zeta", None), repr(glass(link.lines)[-1:]))
link.drop = lambda line: False
link.wrote = []
clock.t += 0.5
scr.refresh()
check("half a second on, nothing has moved and nothing is sent",
      link.wrote == [], repr(link.wrote))
clock.t += 0.5
scr.refresh()
zeta_j = link.wrote[0] if link.wrote else ""
check("a second on, the pair goes again though nothing changed...",
      [l[:3] for l in link.wrote] == ["J|P", "F|4"]
      and zeta_j.endswith("|triage-zeta"), repr(link.wrote))
check("...and zeta's own band is on the glass", glass(link.lines)[-1]
      == ("zeta", zeta_j), repr(glass(link.lines)[-1:]))

# The same session, its band changing under an unchanged frame, the new J lost.
link.drop = lambda line: line.startswith("J|D|")
reg.update("working", "sz", "zeta", focus_ctrl="/x",
           wf="D;5;5;;100;640;15000;;triage-zeta", now=clock.t)
scr.refresh()
check("a lost J for a finished run: the F commits the stale paused one...",
      glass(link.lines)[-1] == ("zeta", zeta_j), repr(glass(link.lines)[-1:]))
link.drop = lambda line: False
clock.t += 1
scr.refresh()
check("...for a second, not until the frame next moves",
      glass(link.lines)[-1][1] == "J|D|5|0|5|5/5 10m40s 15k|triage-zeta",
      repr(glass(link.lines)[-1:]))
check("...and the J and F of that pair are the same bytes the daemon kept",
      scr._last_j == "J|D|5|0|5|5/5 10m40s 15k|triage-zeta"
      and scr._last_frame == link.lines[-1])

link.wrote = []
scr.nav(+1)                              # zeta -> zulu, which has no run
clock.t += 1
scr.refresh()
clock.t += 1
scr.refresh()
check("with no band up, the frame alone, and only when it changes: 'J|-' and "
      "the F once, nothing after", [l[:3] for l in link.wrote]
      == ["J|-", "F|0"], repr(link.wrote))
link.wrote = []
scr.nav(-2)                              # zulu -> api, whose band is up again
check("from no band to a band: its J and F, no second 'J|-'",
      [l[:3] for l in link.wrote] == ["J|P", "F|4"], repr(link.wrote))

# --------------------------------------------------------------------------- #
print("\n== the queue JSON ==")
srv = D.SocketServer(os.path.join(tmp, "unused2.sock"), D.Registry(),
                     on_update=lambda: None)
srv._reg.update("working", "s1", "one", focus_ctrl="/x",
                wf="R;1;5;;20;60;100;;a~R;4;5;;80;98;7;;b", now=clock.t)
srv._reg.update("working", "s1", "one", focus_ctrl="/x",
                wf="R;1;5;;20;60;100;;a~D;5;5;;100;99;7;;b", now=clock.t)
srv._reg.update("working", "s2", "two", now=clock.t)
srv._reg.update("working", "s3", "three", focus_ctrl="/x",
                wf="D;5;5;;100;99;7;;was~R;1;5;;20;60;100;;is", now=clock.t)
snap = srv.queue_snapshot()
byname = {e["name"]: e for e in snap["queue"]}
check("every entry has 'workflows', a list -- [] when there are none",
      all(isinstance(e.get("workflows"), list) for e in snap["queue"])
      and byname["two"]["workflows"] == [])
wfs = byname["one"]["workflows"]
check("...with the documented keys",
      wfs and set(wfs[0]) == {"name", "st", "status", "done", "total", "failed",
                              "pct", "secs", "tokens", "note"})
check("...the band's own record first (here the run that just ended)",
      [w["name"] for w in wfs] == ["b", "a"], repr([w["name"] for w in wfs]))
check("...and the running one first where the finished one was never seen "
      "running", [w["name"] for w in byname["three"]["workflows"]]
      == ["is", "was"], repr(byname["three"]["workflows"]))
check("...with a status WORD, and an int clock",
      wfs[0]["status"] == "done" and wfs[1]["status"] == "running"
      and wfs[1]["secs"] == 60)
clock.t += 61
check("...and what the device stopped showing, the JSON stops listing",
      [w["name"] for w in byname["one"]["workflows"]] != []
      and [w["name"] for w in {e["name"]: e for e in srv.queue_snapshot()
                                ["queue"]}["one"]["workflows"]] == ["a"])
check("...all of it JSON-serialisable",
      bool(json.dumps(srv.queue_snapshot())))

# --------------------------------------------------------------------------- #
print("\n== the --mock demo walks every rendering ==")
cycle = [D.MockInjector.demo_wf(i) for i in range(len(D.MockInjector.WF_DEMO))]
parsed = [D.parse_wf(w) for w in cycle]
check("every step is a valid field 8 (or '-')",
      all(p for p in parsed[:-1]) and parsed[-1] == [])
check("...cycling R -> E -> P -> D -> F -> '-'",
      [p[0].st for p in parsed[:-1]] == list("RRRRRREPDF"))
check("...with the count advancing 0..5 and tokens growing while it runs",
      [p[0].done for p in parsed[:6]] == [0, 1, 2, 3, 4, 5]
      and all(parsed[i][0].tokens < parsed[i + 1][0].tokens for i in range(5)))
check("...and the paused step carrying its note",
      parsed[7][0].note == "til 3:30pm")

D.time = time                           # the real clock back
D.log = _real_log

# --------------------------------------------------------------------------- #
# The real daemon, over a PTY
# --------------------------------------------------------------------------- #
print("\n== end to end: wrapper lines in, J| and F| out ==")
dev = FakeDevice()
sock = os.path.join(tmp, "cm.sock")
proc, dlog = start_daemon(dev, sock)
ctrl_a = os.path.join(tmp, "ctrl-alpha.sock")      # a path is enough: nothing
ctrl_b = os.path.join(tmp, "ctrl-bravo.sock")      # here raises a window


def feed(line):
    for _ in range(80):
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(sock)
            c.sendall((line + "\n").encode())
            c.close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("the daemon's socket never came up")


def talk(line):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(5)
    c.connect(sock)
    c.sendall((line + "\n").encode())
    c.shutdown(socket.SHUT_WR)
    buf = b""
    while True:
        chunk = c.recv(65536)
        if not chunk:
            break
        buf += chunk
    c.close()
    return buf.decode()


def first_pair(lines, j_prefix="J|"):
    """The first J/F pair in `lines` whose F has bit2 and whose J starts with
    `j_prefix` -- a tick of the old state can land after a mark and before
    the line under test is processed, so the pair is found, not assumed."""
    seq = jf(lines)
    for i in range(1, len(seq)):
        if seq[i].startswith("F|") and flags(seq[i]) & 4 \
                and seq[i - 1].startswith(j_prefix) and seq[i - 1] != "J|-":
            return seq[i - 1], seq[i]
    return None


def wf_lines(i=0):
    return [l for l in dlog[i:] if "workflow:" in l]


try:
    wait_for(lambda: any("socket listening" in l for l in dlog), 10)
    wait_for(lambda: any(l.startswith("F|") for l in dev.since()), 10)
    feed(f"working|sid-a|alpha|{ctrl_a}|Opus 4.8|xhigh|work|5h82%|"
         f"R;1;5;;20;60;12000;;sweep")
    feed(f"working|sid-b|bravo|{ctrl_b}|Sonnet 4.6|high|work|5h82%")
    pair = wait_for(lambda: first_pair(dev.since()))
    check("the shown session's run reaches the device as J, then F with bit2",
          bool(pair) and pair[0].startswith("J|R|1|0|5|")
          and pair[0].endswith("|sweep") and subject(pair[1]) == "alpha",
          repr(pair))
    check("...the frame otherwise exactly as it would be (r2 keeps the model)",
          bool(pair) and pair[1].split("|")[5] == "Opus 4.8 xhigh  5h82%")

    i_nav = dev.mark()
    dev.send("B|N")                                  # alpha -> bravo
    time.sleep(2.6)                                  # >= 2 ticks of bravo
    after = jf(dev.since(i_nav))
    # A tick of alpha's may land between the mark and the press; what matters
    # is what leads bravo's first frame.
    at = after.index("J|-") if "J|-" in after else -1
    check("moving to a session with no run sends 'J|-' first...",
          at >= 0 and at + 1 < len(after) and subject(after[at + 1]) == "bravo"
          and not any(subject(l) == "bravo" for l in after[:at]),
          repr(after[:4]))
    check("...exactly once, while its frames keep ticking",
          after.count("J|-") == 1
          and sum(1 for l in after if l.startswith("F|")) >= 2, repr(after))
    check("...and none of bravo's frames has bit2",
          not any(flags(l) & 4 for l in after if subject(l) == "bravo"))

    i_back = dev.mark()
    dev.send("B|P")                                  # back to alpha
    pair = wait_for(lambda: first_pair(dev.since(i_back)), 4)
    check("moving back: its J, then its F with bit2",
          bool(pair) and pair[0].startswith("J|R|")
          and subject(pair[1]) == "alpha")

    i_h = dev.mark()
    dev.send("H")
    pair = wait_for(lambda: first_pair(dev.since(i_h)), 4)
    check("H (a device that just booted) gets J then F again",
          bool(pair) and pair[0].startswith("J|R|"), repr(pair))
    check("...from the full resend", bool(wait_for(
        lambda: any("handshake H" in l for l in dlog), 4)))

    feed(f"working|sid-a|alpha|{ctrl_a}||||")         # an old 8-field keepalive
    time.sleep(0.4)
    q = json.loads(talk("queue"))
    a = {e["name"]: e for e in q["queue"]}.get("alpha", {})
    awf = a.get("workflows") or [{}]
    check("an 8-field line from an older wrapper keeps the run",
          [w.get("name") for w in awf] == ["sweep"])
    check("...and the queue JSON lists it", awf[0].get("st") == "R"
          and awf[0].get("done") == 1 and awf[0].get("total") == 5)

    n_logs = len(wf_lines())
    for k in range(3):                               # keepalives: numbers move
        feed(f"working|sid-a|alpha|{ctrl_a}|||||"
             f"R;{1 + k % 2};5;;20;{61 + k};{12000 + k};;sweep")
    i_d = dev.mark()
    feed(f"working|sid-a|alpha|{ctrl_a}|||||D;5;5;;100;70;20000;;sweep")
    pair = wait_for(lambda: first_pair(dev.since(i_d), "J|D|"), 4)
    check("a finished run: J|D, then F with bit2 (it lingers)",
          bool(pair) and pair[0] == "J|D|5|0|5|5/5 1m10s 20k|sweep",
          repr(pair))
    time.sleep(0.5)
    new_logs = wf_lines()[n_logs:]
    check("the log gained exactly one line for it -- none for the keepalives",
          len(new_logs) == 1 and new_logs[0].endswith('alpha "sweep" R -> D'),
          repr(new_logs))

    feed(f"working|sid-c|charlie|{ctrl_a}|m|e|a|l|"
         f"R;1;2;;50;5;10;;fwd|a-field-from-the-future")
    time.sleep(0.4)
    c = {e["name"]: e for e in json.loads(talk("queue"))["queue"]}.get(
        "charlie", {})
    check("a 10-field line: field 9 is ignored, the name is exactly the name",
          [w.get("name") for w in c.get("workflows") or []] == ["fwd"])

    time.sleep(1.2)
    every = dev.since()
    js = [l for l in every if l.startswith("J|")]
    check("every J the device received is in the firmware's grammar",
          js and all(j_ok(l) for l in js), repr([l for l in js if not j_ok(l)]))
    check("every F with bit2 was committed from a J right before it",
          not unpaired(every), repr(unpaired(every)[:2]))
    check("every line the device received is ASCII and fits the Nano",
          all(l.isascii() and len(l) <= 95 for l in every))
finally:
    stop_daemon(proc)

# --------------------------------------------------------------------------- #
print("\n== --mock shows the band, and walks it ==")
try:
    def mock_states():
        return [l.split("|")[1] for l in mock_dev.since()
                if l.startswith("J|") and l != "J|-"]

    # Bounded: six running steps of 4 s each before the first E, so ~25 s
    # from the mock's start; 60 s is room for a slow CI box, not a target.
    walked = wait_for(lambda: (lambda st: "R" in st and any(
        x in st[st.index("R"):] for x in "EPDF"))(mock_states()),
        max(1.0, mock_started + 60 - time.time()), tick=0.25)
    sts = mock_states()
    check("--mock puts a running workflow on the device (J|R...)", "R" in sts)
    check("...and then walks it on to another status within a minute",
          bool(walked), repr(sorted(set(sts))))
    seen = mock_dev.since()
    check("...with the band up on the frames it commits",
          any(l.startswith("F|") and flags(l) & 4 for l in seen))
    check("...every one of them preceded by its J",
          not unpaired(seen), repr(unpaired(seen)[:2]))
    check("...and every J in the grammar",
          all(j_ok(l) for l in seen if l.startswith("J|")))
finally:
    stop_daemon(mock_proc)

# =========================================================================== #
# REVIEW FIXES FROM THE CHAT HISTORY, in process. Not about workflows: they sit
# here because this file already loads the daemon with its log and its clock
# in the test's hands. Each check below that is marked (regression) failed
# against the code before its fix.
# =========================================================================== #
D.log = lambda msg: logged.append(msg)
D.time = clock                          # frames that do not tick under a test
clock.t = T0 + 100_000

print("\n== no glass: with no device linked, a press acts on the selection ==")
# GO and ACK act on the session whose frame is on the glass, and CONTINUE and
# FOLLOW on the one shown there. That key only moved when a frame reached a
# device, so once the device had gone -- a cable pulled, a battery device
# asleep -- it went on naming the last session drawn, and History's "Raise its
# terminal" (select|<key>, then press|G) raised and acknowledged THAT one.


class GlassLink:
    """A device link that can be unplugged (`up`), refuse a write while it is
    plugged in (`ok`), or find the cable gone on its next write (`dies`), as a
    serial write does when the device has been pulled."""

    def __init__(self):
        self.up, self.ok, self.dies, self.lines = True, True, False, []

    def is_open(self):
        return self.up

    def write_line(self, line):
        if self.dies:
            self.up = self.dies = False
        if not (self.up and self.ok):
            return False
        self.lines.append(line)
        return True


def frames(lines):
    return [l for l in lines if l.startswith("F|")]


KA, KB = "aaaa1111", "bbbb2222"
glink, greg = GlassLink(), D.Registry()
greg.update("waiting", KA, "alpha", focus_ctrl="/ctl-a")   # nobody has seen it
greg.update("idle", KB, "bravo", focus_ctrl="/ctl-b")
gscr = D.Screen(glink, greg)
gsrv = D.SocketServer(os.path.join(tmp, "unused3.sock"), greg,
                      on_update=lambda: None)
gsrv.screen, gsrv.on_select = gscr, gscr.select_by


def acts_on():
    """(the key GO and ACK act on, the key CONTINUE and FOLLOW act on)."""
    t, s = gscr.resolve_press_target(), gscr.current_shown()
    return (t.key if t else None, s.key if s else None)


def select(key):
    return gsrv._process_line(("select|" + key).encode())


gscr.refresh(force=True)
check("linked: alpha is on the glass, and every press means alpha",
      acts_on() == (KA, KA) and subject(frames(glink.lines)[-1]) == "alpha")
glink.ok = False
check("linked, one write refused: select|<bravo's key> answers 'ok bravo'...",
      select(KB) == "ok bravo\n")
check("...yet a press still means alpha, which the glass still shows: a "
      "refused write is not a device gone", acts_on() == (KA, KA),
      repr(acts_on()))
glink.ok = True
gscr.refresh()
check("...until bravo's frame lands", acts_on() == (KB, KB))

gscr.select_by(KA)
glink.up = False                        # the cable out, or the device asleep
check("unplugged: select|<bravo's key> still answers 'ok bravo'...",
      select(KB) == "ok bravo\n")
check("(regression) ...and GO, ACK and CONTINUE mean bravo, the selection -- "
      "not alpha, the last name drawn on glass nobody can see",
      acts_on() == (KB, KB), repr(acts_on()))
gsnap = gsrv.queue_snapshot()
check("(regression) ...which the queue marks as both shown and selected",
      (gsnap["shown"], gsnap["selected"]) == (KB, KB),
      repr((gsnap["shown"], gsnap["selected"])))

raised, ctrl_calls = [], []


def fake_ctrl_call(ctrl, cmd):
    ctrl_calls.append((ctrl, cmd))
    return True, ""


greader = D.ButtonReader(glink, gscr)
greader.on_ack = lambda s: (greg.acknowledge(s), gscr.notify_change())
greader._raise = lambda s: raised.append(s.key if s else None)
_real_ctrl_call = D.wrapper_ctrl_call
D.wrapper_ctrl_call = fake_ctrl_call
try:
    greader._go()
    alpha = next(s for s in greg.queue() if s.key == KA)
    check("(regression) GO raises bravo's terminal and stays on bravo, and "
          "alpha's alert is still unseen", raised == [KB]
          and gscr._sel_key == KB and alpha.unacked_alert(), repr(raised))
    greader._continue_pressed()
    check("(regression) CONTINUE types into bravo",
          ctrl_calls == [("/ctl-b", "submit continue")], repr(ctrl_calls))
    del raised[:]
    greader._follow_pressed()           # FOLLOW on raises the shown one...
    greader._follow_pressed()           # ...and off again raises nothing
    check("(regression) turning FOLLOW on raises bravo", raised == [KB],
          repr(raised))
    greader._mirror_pressed()
    check("(regression) MIRROR opens no view: a view needs glass, and one "
          "opened into nothing would poll the wrapper every second for nobody",
          not greader.mirror_open())
finally:
    greader.mirror_close()
    D.wrapper_ctrl_call = _real_ctrl_call

glink.up, glink.lines = True, []        # plugged back in
gscr.refresh()
check("plugged back in: the next frame goes out unforced and is what a press "
      "means", [subject(f) for f in frames(glink.lines)] == ["bravo"]
      and acts_on() == (KB, KB), repr(glink.lines))
glink.up = False
gscr.refresh()                          # a tick while it is out
glink.up, glink.lines = True, []
gscr.refresh()
check("(regression) ...even when it is the very frame last sent: a device "
      "that came back is told what is on it, and the press follows",
      [subject(f) for f in frames(glink.lines)] == ["bravo"]
      and gscr._shown_key == KB, repr(glink.lines))

gscr.select_by(KA)                      # alpha on the glass again, linked
glink.dies = True
check("the write that finds the cable gone: select|<bravo's key> answers "
      "'ok bravo'", select(KB) == "ok bravo\n" and not glink.up)
check("(regression) ...and a press means bravo at once, before any tick "
      "has noticed the device is gone", acts_on() == (KB, KB),
      repr(acts_on()))
glink.up = True

# --------------------------------------------------------------------------- #
# The chat history's writer, against a temp root and a fake transcript tree
# under a temp HOME -- `~/.claude` is where the default login's transcripts
# are looked for, and this must never be the real one.
htmp = tempfile.mkdtemp(prefix="cm-wf-hist-")
_real_home = os.environ.get("HOME")
os.environ["HOME"] = os.path.join(htmp, "home")
try:
    PROJ = os.path.join(htmp, "proj")
    TREE = os.path.join(htmp, "home", ".claude", "projects", "-proj")
    os.makedirs(PROJ)
    os.makedirs(TREE)

    def transcript(conv):
        path = os.path.join(TREE, conv + ".jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "user", "cwd": PROJ}) + "\n")
        return path

    def hsnap(conv, key="w1", turn_ts=1000.0):
        """A wrapped session that ran a turn, as Registry.snapshot() has it."""
        return {"key": key, "name": "proj", "cwd": PROJ, "conversation": conv,
                "config_dir": "", "flags": (), "account": "default",
                "turned": True, "turn_ts": turn_ts}

    def hput(writer, s, ended=None):
        writer.note(s, ended=ended)
        writer.flush()

    def record(root, conv):
        try:
            with open(os.path.join(root, conv + ".json")) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def kept(root, conv, src):
        dst = os.path.join(root, conv + ".jsonl")
        return os.path.exists(dst) and os.path.samefile(src, dst)

    def forget(root, conv):
        """What the CLI's `history rm` does: the link first, then the record."""
        for ext in (".jsonl", ".json"):
            try:
                os.unlink(os.path.join(root, conv + ext))
            except FileNotFoundError:
                pass

    print("\n== a deleted record is dated by when it was last seen ==")
    hroot = os.path.join(htmp, "history")
    hnow = [1000.5]
    hw = D.HistoryWriter(hroot, clock=lambda: hnow[0])
    CX = "3f2a9c1e-7b4d-4c1a-9e8f-0123456789ab"
    CY = "5b6c7d8e-1f2a-4b3c-8d4e-5f6a7b8c9d0e"
    tx, ty = transcript(CX), transcript(CY)
    hput(hw, hsnap(CX))                  # its turn at 1000, written at 1000.5
    hnow[0] = 1010.0
    hput(hw, hsnap(CX), ended=1010.0)    # closed: last seen there at 1010
    forget(hroot, CX)                    # deleted at, say, 1020
    hnow[0] = 1030.75                    # noticed at 1030.75, by the report of
    hput(hw, hsnap(CX, key="w2", turn_ts=1030.0))    # a turn begun at 1030
    rx = record(hroot, CX) or {}
    check("(regression) a turn begun after the deletion, but named only once "
          "it was under way (claude -c), keeps the chat again -- though the "
          "writer noticed the deletion after that turn had begun",
          rx.get("last_active") == 1030.0 and rx.get("ended") is None
          and kept(hroot, CX, tx), repr(rx))

    hnow[0] = 1000.5
    hput(hw, hsnap(CY))
    hnow[0] = 1015.5                     # a turn begun at 1015 is a change,
    hput(hw, hsnap(CY, turn_ts=1015.0))  # written within the debounce
    forget(hroot, CY)                    # deleted at, say, 1020
    hnow[0] = 1030.75                    # ...noticed at 1030.75, by that
    hput(hw, hsnap(CY, turn_ts=1015.0))  # turn's next report, then its end
    hput(hw, hsnap(CY, turn_ts=1015.0), ended=1031.0)
    check("a turn begun before the deletion cannot bring it back -- a write "
          "that saw the record noted its start -- nor can its end",
          record(hroot, CY) is None
          and not os.path.exists(os.path.join(hroot, CY + ".jsonl")))
    hnow[0] = 1041.0
    hput(hw, hsnap(CY, key="w3", turn_ts=1040.0))
    check("...only a turn begun after it does",
          (record(hroot, CY) or {}).get("last_active") == 1040.0
          and kept(hroot, CY, ty))

    print("\n== ...and through the socket, the way claude -c reports it ==")

    def wline(state, sid, **hist):
        """A wrapper's ten-field line; field 9 names the chat once `id` is
        in it -- for claude -c, only after its first turn has begun."""
        hist.update(cwd=PROJ, config="", flags=[])
        field9 = urllib.parse.quote(json.dumps(
            hist, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            safe="")
        return "|".join([state, sid, "proj", "/tmp/cm-wf-ctl-" + sid, "", "",
                         "default", "", "", field9]).encode()

    zroot = os.path.join(htmp, "history-z")
    zsrv = D.SocketServer(os.path.join(htmp, "z.sock"), D.Registry(),
                          on_update=lambda: None)
    zw = D.HistoryWriter(zroot, clock=lambda: clock.t)
    zsrv.history = zw
    CZ = "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"
    tz = transcript(CZ)
    for t, ln in ((5000.0, wline("idle", "aaaa0001")),
                  (5001.0, wline("working", "aaaa0001")),
                  (5001.25, wline("working", "aaaa0001", id=CZ))):
        clock.t = t
        zsrv._process_line(ln)
    clock.t = 5001.75
    zw.flush()
    check("a first session's turn keeps the chat",
          (record(zroot, CZ) or {}).get("last_active") == 5001.0)
    clock.t = 5011.0
    zsrv._process_line(wline("end", "aaaa0001", id=CZ))
    clock.t = 5011.5
    zw.flush()
    forget(zroot, CZ)                    # closed, then deleted at 5020
    for t, ln in ((5030.0, wline("idle", "bbbb0002")),      # claude -c: no id
                  (5040.0, wline("working", "bbbb0002")),   # its first turn
                  (5040.25, wline("working", "bbbb0002", id=CZ))):
        clock.t = t
        zsrv._process_line(ln)
    clock.t = 5040.75
    zw.flush()
    rz = record(zroot, CZ) or {}
    check("(regression) closed, deleted, then picked up again with claude -c: "
          "its first turn keeps the chat again, transcript linked beside it",
          rz.get("last_active") == 5040.0 and rz.get("ended") is None
          and kept(zroot, CZ, tz), repr(rz))
    clock.t = 5051.0
    zsrv._process_line(wline("end", "bbbb0002", id=CZ))
    clock.t = 5051.5
    zw.flush()
    check("...and its end closes it",
          (record(zroot, CZ) or {}).get("ended") == 5051.0)

    print("\n== the record goes down before the link to its transcript ==")
    kroot = os.path.join(htmp, "history-k")
    kw = D.HistoryWriter(kroot, clock=lambda: 2000.0)
    CN = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    tn = transcript(CN)

    def no_space(path, merged, conv):
        raise OSError(errno.ENOSPC, "No space left on device")

    kw._replace = no_space        # the record's write fails, or dies half way
    mark = len(logged)
    hput(kw, hsnap(CN))
    check("(regression) a NEW record whose write fails leaves no link to its "
          "transcript: nothing would list it, delete it or let go of its disk",
          os.listdir(kroot) == [], repr(os.listdir(kroot)))
    check("...and the failure is logged, by the record's name",
          any(CN in m and "failed" in m for m in logged[mark:]))
    del kw._replace
    hput(kw, hsnap(CN))
    rn = record(kroot, CN) or {}
    check("the next write puts the record down and then the link, the record "
          "naming the project it found the transcript in",
          rn.get("project") == "-proj" and kept(kroot, CN, tn), repr(rn))
    ino = os.stat(os.path.join(kroot, CN + ".json")).st_ino
    with open(tn + ".new", "w") as fh:   # Claude rewrites its copy: a new
        fh.write(json.dumps({"type": "user", "cwd": PROJ}) + "\n{}\n")
    os.replace(tn + ".new", tn)          # file, a new inode
    hput(kw, hsnap(CN))
    check("an unchanged record is not rewritten, and its link still follows "
          "Claude's new copy", kept(kroot, CN, tn)
          and os.stat(os.path.join(kroot, CN + ".json")).st_ino == ino)
finally:
    if _real_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _real_home
    shutil.rmtree(htmp, ignore_errors=True)
    D.time = time
    D.log = _real_log

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("workflow daemon: OK")
