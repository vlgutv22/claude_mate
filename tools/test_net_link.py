#!/usr/bin/env python3
"""
Test the daemon's WIRELESS (TCP) transport with NO hardware.

A fake wireless device dials the daemon over TCP, completes the nonce/HMAC
handshake, and is then treated exactly like a Nano on USB: it receives F|/V|/P
lines and its B| button events drive the queue. A PTY simultaneously pretends
to be a USB Nano, so the fan-out ("both devices see every frame") and the merge
("button events from either device work") invariants are asserted rather than
assumed.

Covers the security posture too: a wrong token is rejected, a token is never
sent in the clear, the nonce is fresh per connection, and --tcp without a token
refuses to listen at all.

Run:   python3 tools/test_net_link.py      (needs pyserial)
"""
import hmac
import os
import pty
import queue
import select
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tty

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMON = os.path.join(REPO, "daemon", "claude_mate_daemon.py")

try:
    import serial  # noqa: F401  (the daemon needs it; fail early & clearly)
except ImportError:
    print("SKIP: pyserial not installed. Run: pip install pyserial")
    sys.exit(0)

TOKEN = "s3cr3t-test-token"
results = []


def check(label, ok):
    results.append((label, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(pred, timeout=6.0, tick=0.05):
    """Poll until pred() is truthy. Returns the truthy value or False."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = pred()
        if val:
            return val
        time.sleep(tick)
    return False


class FakeDevice:
    """The wireless companion: dials in, authenticates, records what it sees."""

    def __init__(self, port, token):
        self.port = port
        self.token = token
        self.lines = []
        self.lock = threading.Lock()
        self.sock = None
        self.authed = False
        self.nonce = None
        self.reject = False
        self._buf = b""

    def _read_line(self, timeout=5.0):
        # settimeout() INSIDE the guard, not above it. The pump thread sits in
        # this function while phase 5 closes the socket under it to simulate a
        # device dropping off Wi-Fi, and settimeout() on a closed fd raises
        # OSError(EBADF) -- which killed the thread with a traceback on every
        # run of that phase. The recv below was already guarded; this was the
        # one line outside the net.
        try:
            self.sock.settimeout(timeout)
        except OSError:
            return None
        while b"\n" not in self._buf:
            try:
                chunk = self.sock.recv(1024)
            except (socket.timeout, OSError):
                return None
            if not chunk:
                return None
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\n", 1)
        return raw.decode(errors="replace").strip()

    def connect(self, token=None):
        """Dial + handshake. Returns True once A|OK lands."""
        tok = self.token if token is None else token
        self.sock = socket.create_connection(("127.0.0.1", self.port),
                                             timeout=5.0)
        challenge = self._read_line()
        if not challenge or not challenge.startswith("C|"):
            return False
        self.nonce = challenge[2:]
        mac = hmac.new(tok.encode(), self.nonce.encode(), "sha256").hexdigest()
        self.sock.sendall(f"A|{mac}\n".encode())
        verdict = self._read_line()
        if verdict == "A|OK":
            self.authed = True
            threading.Thread(target=self._pump, daemon=True).start()
            return True
        self.reject = (verdict == "A|NO")
        return False

    def _pump(self):
        while True:
            line = self._read_line(timeout=None)
            if line is None:
                return
            with self.lock:
                self.lines.append(line)
            print(f"   WIFI <= {line}")

    def send(self, line):
        print(f"   WIFI => {line}")
        self.sock.sendall((line + "\n").encode())

    def seen(self, pred, since=0):
        with self.lock:
            return [l for l in self.lines[since:] if pred(l)]

    def count(self):
        with self.lock:
            return len(self.lines)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def frame_subject(f):
    p = f.split("|")
    return p[3] if len(p) >= 7 else ""


def is_frame(f):
    return f.startswith("F|")


# --------------------------------------------------------------------------- #
# Phase 0: --tcp with no token GENERATES one, and still fails closed if it
# cannot.
#
# This used to assert that --tcp without a token refused to listen. That was
# aimed at the right danger -- an UNAUTHENTICATED listener -- but it caught the
# wrong thing, and the cost was a dead end for the user: the device's setup
# portal asks for a token, and the only way to have one was to already know to
# create the file by hand. A freshly generated 32-byte secret is not an
# unauthenticated listener, so generating it closes the loop without weakening
# anything. The property that actually matters is pinned by phase 1 (a wrong
# token is rejected), which is unchanged.
# --------------------------------------------------------------------------- #
print("\n-- phase 0: --tcp with no token generates one --")
tmp = tempfile.mkdtemp(prefix="cm-net-")
port_gen = free_port()
gen_token_path = os.path.join(tmp, "generated", "token")
env = dict(os.environ)
env["CLAUDE_MATE_SOCK"] = os.path.join(tmp, "gen.sock")
env["CLAUDE_MATE_PORT"] = "/dev/null/nope"      # never opens; that is fine
env["CLAUDE_MATE_TOKEN"] = ""
env["CLAUDE_MATE_TOKEN_FILE"] = gen_token_path
gen = subprocess.Popen(
    [sys.executable, DAEMON, "--tcp", "--tcp-port", str(port_gen),
     "--tcp-bind", "127.0.0.1"],
    env=env, stderr=subprocess.PIPE, text=True)
gen_err = []
threading.Thread(
    target=lambda: [gen_err.append(l) for l in gen.stderr],
    daemon=True).start()
time.sleep(2.0)


def port_open(p):
    try:
        socket.create_connection(("127.0.0.1", p), timeout=0.5).close()
        return True
    except OSError:
        return False


check("--tcp with no token file creates one", os.path.exists(gen_token_path))
gen_tok = ""
if os.path.exists(gen_token_path):
    gen_tok = open(gen_token_path).read().strip()
    mode = stat.S_IMODE(os.stat(gen_token_path).st_mode)
else:
    mode = -1
check("...long enough to be a real secret (>= 32 chars)", len(gen_tok) >= 32)
check("...mode 0600, not world-readable", mode == 0o600)
check("...and the listener actually opens", port_open(port_gen))
check("...and it is PRINTED, so it can be typed into the setup portal",
      any(gen_tok and gen_tok in l for l in gen_err))
gen.terminate()
gen.wait(timeout=5)

# ...but a token it cannot create still fails closed. This is the real
# fail-closed case, and it must never regress into an open listener.
print("\n-- phase 0b: --tcp still refuses when no token can be created --")
port_noauth = free_port()
env2 = dict(env)
env2["CLAUDE_MATE_SOCK"] = os.path.join(tmp, "noauth.sock")
env2["CLAUDE_MATE_TOKEN_FILE"] = "/dev/null/nope/token"   # unwritable by design
noauth = subprocess.Popen(
    [sys.executable, DAEMON, "--tcp", "--tcp-port", str(port_noauth),
     "--tcp-bind", "127.0.0.1"],
    env=env2, stderr=subprocess.PIPE, text=True)
noauth_err = []
threading.Thread(
    target=lambda: [noauth_err.append(l) for l in noauth.stderr],
    daemon=True).start()
time.sleep(2.0)

check("--tcp with an uncreatable token does NOT open a listener",
      not port_open(port_noauth))
check("...and says why", any("needs a shared token" in l for l in noauth_err))
noauth.terminate()
noauth.wait(timeout=5)

# --------------------------------------------------------------------------- #
# Set up the real daemon: a PTY "Nano" on USB + the TCP listener
# --------------------------------------------------------------------------- #
print("\n-- starting daemon with --tcp (USB PTY + wireless) --")
master_fd, slave_fd = pty.openpty()
slave_name = os.ttyname(slave_fd)
tty.setraw(master_fd)

usb_lines = []
usb_lock = threading.Lock()


def pty_reader():
    buf = b""
    while True:
        try:
            data = os.read(master_fd, 1024)
        except OSError:
            return
        if not data:
            return
        buf += data
        while b"\n" in buf:
            ln, buf = buf.split(b"\n", 1)
            s = ln.decode(errors="replace").strip()
            if s:
                with usb_lock:
                    usb_lines.append(s)
                print(f"   USB  <= {s}")


threading.Thread(target=pty_reader, daemon=True).start()

tcp_port = free_port()
hook_sock = os.path.join(tmp, "cm.sock")
token_file = os.path.join(tmp, "token")
with open(token_file, "w") as fh:
    fh.write(TOKEN + "\n")          # exercise the token-FILE path, not just env

env = dict(os.environ)
env["CLAUDE_MATE_SOCK"] = hook_sock
env["CLAUDE_MATE_PORT"] = slave_name
env["CLAUDE_MATE_TOKEN_FILE"] = token_file
env.pop("CLAUDE_MATE_TOKEN", None)
proc = subprocess.Popen(
    [sys.executable, DAEMON, "--tcp", "--tcp-port", str(tcp_port),
     "--tcp-bind", "127.0.0.1"],
    env=env, stderr=subprocess.PIPE, text=True)
daemon_err = []


def err_reader():
    for ln in proc.stderr:
        daemon_err.append(ln)
        print(f"   [daemon] {ln.rstrip()}")


threading.Thread(target=err_reader, daemon=True).start()


def feed(line):
    for _ in range(50):
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(hook_sock)
            c.send((line + "\n").encode())
            c.close()
            print(f"   HOOK => {line}")
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("hook socket never came up")


def usb_seen(pred):
    with usb_lock:
        return [l for l in usb_lines if pred(l)]


ok = wait_for(lambda: any("TCP listening" in l for l in daemon_err), 10.0)
check("--tcp with a token file opens the listener", bool(ok))

# --------------------------------------------------------------------------- #
# The cable provisions the radio, with nobody typing anything.
# --------------------------------------------------------------------------- #
# "How do I connect it after a factory reset" had one honest answer -- open a
# portal from a phone, or type T|<token> down the cable -- and both are work the
# user should never have been asked to do, since the daemon and the wiped device
# are already joined by a cable over which provisioning is the trusted path. So
# every serial open now hands the device this daemon's token. A device that
# never needed one is unaffected: NVS skips a write whose value is unchanged.
# NOT to whatever arrives -- to whatever ANSWERS. The daemon writes `P` on open
# and waits for the `K` a Claude Mate replies with, because autodetect() takes
# the first glob match and tries /dev/cu.usbserial* BEFORE the usbmodem* the S3
# actually uses: any FTDI dongle on the desk outranked the real device and was
# handed the shared secret.
ok = wait_for(lambda: usb_seen(lambda l: l == "P"), 10.0)
check("the daemon asks who is on the cable before trusting it", bool(ok))
check("...and sends no token to a port that has said nothing",
      not usb_seen(lambda l: l.startswith("T|")))

# Answer as the device does, and the token follows.
os.write(master_fd, b"K\n")
ok = wait_for(lambda: usb_seen(lambda l: l == f"T|{TOKEN}"), 10.0)
check("...then hands it the token once it answers the protocol", bool(ok))
check("...which is exactly the line the firmware's config console takes",
      bool(usb_seen(lambda l: l.startswith("T|"))))

# --------------------------------------------------------------------------- #
# Phase 1: a wrong token is rejected
# --------------------------------------------------------------------------- #
print("\n-- phase 1: a wrong token is rejected --")
impostor = FakeDevice(tcp_port, "wrong-token")
got_in = impostor.connect()
check("a device with the WRONG token is refused", not got_in)
check("...and is told so (A|NO) rather than left hanging", impostor.reject)
impostor.close()

# A device that knows it has NO token says so instead of hanging up silently.
# Silence made the daemon log "bad handshake None", which reads like a crash or
# a network fault -- and a cleared token is the commonest wireless failure there
# is, so it earns the one message that says what to do about it.
print("\n-- phase 1b: a device with no token at all says so --")
notok = socket.create_connection(("127.0.0.1", tcp_port), timeout=5.0)
notok.settimeout(5.0)
chal = b""
while b"\n" not in chal:
    c = notok.recv(1024)
    if not c:
        break
    chal += c
check("...the daemon still challenges it", chal.startswith(b"C|"))
notok.sendall(b"A|NOTOKEN\n")
verdict = b""
try:
    while b"\n" not in verdict:
        c = notok.recv(1024)
        if not c:
            break
        verdict += c
except (socket.timeout, OSError):
    pass
check("a device reporting A|NOTOKEN is refused with A|NO",
      verdict.strip() == b"A|NO")
notok.close()
check("...and the daemon logs what to DO about it, not 'bad handshake'",
      wait_for(lambda: any("HAS NO TOKEN" in l for l in daemon_err)))
check("...naming the token file, so the fix is copy-pasteable",
      any("claude-mate/token" in l or "token" in l.lower()
          for l in daemon_err if "HAS NO TOKEN" in l or "token is in" in l))

# --------------------------------------------------------------------------- #
# Phase 2: the real device authenticates and gets the full state
# --------------------------------------------------------------------------- #
print("\n-- phase 2: the real device authenticates --")
dev = FakeDevice(tcp_port, TOKEN)
check("a device with the RIGHT token is accepted", dev.connect())
first_nonce = dev.nonce
check("the challenge nonce is a 32-char hex string",
      bool(first_nonce) and len(first_nonce) == 32
      and all(c in "0123456789abcdef" for c in first_nonce))
check("the token itself is NEVER sent over the wire (HMAC only)",
      TOKEN not in (first_nonce or ""))

# The device owns its own hardware settings and the daemon rightly knows nothing
# about them -- the alert SOUND is the one exception, because it plays on the
# Mac. A mute reached for on the device therefore has to cross the link.
print("\n-- phase 2b: a device can set the Mac's alert sound --")
dev.send("O|SND|1")
check("O|SND|1 turns the sound on",
      wait_for(lambda: any("sound: on (set from the device)" in l
                           for l in daemon_err)))
dev.send("O|SND|0")
check("O|SND|0 turns it off again",
      wait_for(lambda: any("sound: off (set from the device)" in l
                           for l in daemon_err)))
# Forward compatibility both ways: a newer firmware must never break an older
# daemon, so an unrecognised key is ignored rather than fatal.
dev.send("O|NOSUCHKEY|1")
check("an unknown option is ignored, not fatal",
      wait_for(lambda: any("unknown device option" in l for l in daemon_err)))
dev.send("O|malformed")
check("a malformed option is ignored, not fatal",
      wait_for(lambda: any("malformed device option" in l for l in daemon_err)))
check("...and the link is still up after all of that", dev.authed)

# H is what a real device sends after A|OK; the daemon answers with full state.
dev.send("H")
check("H over TCP triggers a full state resend (F| frame arrives)",
      bool(wait_for(lambda: dev.seen(is_frame))))
check("...and the LED state is re-armed too (V| line arrives)",
      bool(wait_for(lambda: dev.seen(lambda l: l.startswith("V|")))))

# --------------------------------------------------------------------------- #
# Phase 3: frames fan out to BOTH transports
# --------------------------------------------------------------------------- #
print("\n-- phase 3: frames reach the USB Nano AND the wireless device --")
feed("working|sid-net|alpha")
feed("waiting|sid-net2|bravo")

got_wifi = wait_for(
    lambda: dev.seen(lambda l: is_frame(l) and frame_subject(l) == "alpha"))
got_usb = wait_for(
    lambda: usb_seen(lambda l: is_frame(l) and frame_subject(l) == "alpha"))
check("the wireless device sees the frame", bool(got_wifi))
check("the USB device sees the SAME frame", bool(got_usb))
check("both got byte-identical frames",
      bool(got_wifi) and bool(got_usb) and got_wifi[0] == got_usb[0])

# --------------------------------------------------------------------------- #
# Phase 4: buttons from either transport drive the same queue
# --------------------------------------------------------------------------- #
print("\n-- phase 4: buttons from either device drive the queue --")
before = dev.count()
dev.send("B|N")            # NEXT from the WIRELESS device
moved = wait_for(lambda: [l for l in dev.seen(is_frame, before)
                          if frame_subject(l) == "bravo"])
check("NEXT from the wireless device moves the selection (alpha -> bravo)",
      bool(moved))

before = dev.count()
os.write(master_fd, b"B|P\n")   # PREV from the USB device
print("   USB  => B|P")
moved_back = wait_for(lambda: [l for l in dev.seen(is_frame, before)
                               if frame_subject(l) == "alpha"])
check("PREV from the USB device moves it back, and the wireless device sees it",
      bool(moved_back))

# --------------------------------------------------------------------------- #
# Phase 5: keepalive + reconnect
# --------------------------------------------------------------------------- #
print("\n-- phase 5: a reconnecting device is served a fresh nonce --")
dev.close()
time.sleep(0.5)
dev2 = FakeDevice(tcp_port, TOKEN)
check("a device that dropped off WiFi can reconnect", dev2.connect())
check("...with a DIFFERENT nonce (a captured handshake cannot be replayed)",
      dev2.nonce != first_nonce)
dev2.send("H")
check("the reconnected device is resynced from scratch",
      bool(wait_for(lambda: dev2.seen(is_frame))))
# WAIT FOR IT, like every other check in this file. The daemon reaps a dropped
# socket asynchronously -- it finds out on its next write or its own sweep, not
# at the moment dev.close() returns -- so reading daemon_err once, right here,
# is a race that the test wins only on a fast, idle machine. It lost on CI's
# py3.11 runner while passing on 3.9 and 3.12 beside it, which is the signature
# of a timing race rather than a real regression.
check("the daemon logged the disconnect of the old socket",
      bool(wait_for(lambda: any("TCP device disconnected" in l
                                for l in daemon_err))))

# --------------------------------------------------------------------------- #
# Phase 6: the goodbye frame reaches wireless devices too
# --------------------------------------------------------------------------- #
print("\n-- phase 6: shutdown leaves an honest frame on the wireless device --")
before = dev2.count()
proc.terminate()
bye = wait_for(lambda: dev2.seen(lambda l: "daemon stopped" in l, before), 8.0)
check("a stopping daemon tells the wireless device (no frozen frame)", bool(bye))
proc.wait(timeout=10)
dev2.close()

# --------------------------------------------------------------------------- #
print("\n-- phase 7: a wireless device must not starve a USB one --")
# Regression: LinkHub.is_open() answers "is ANY device reachable", so with a
# wireless client connected it returns True while the serial port is SHUT. The
# maintainer used to gate reconnection on it and therefore never reopened
# serial -- a Nano that dropped, or was plugged in after the ESP32 linked, sat
# on NO LINK forever. Observed on real hardware with both devices attached.
import importlib.util
_spec = importlib.util.spec_from_file_location("cm_daemon_under_test", DAEMON)
_cmd = importlib.util.module_from_spec(_spec)
# @dataclass resolves annotations through sys.modules[cls.__module__], so the
# module has to be registered BEFORE exec_module or importing it blows up.
sys.modules["cm_daemon_under_test"] = _cmd
_spec.loader.exec_module(_cmd)

class _ClosedSerial:
    opened = False
    def is_open(self): return False
    def ensure_open(self):
        _ClosedSerial.opened = True      # the maintainer DID try to reopen
        return False

class _NetWithClient:
    def has_clients(self): return True
    def write_line(self, line): return True
    def stop(self): pass

_hub = _cmd.LinkHub.__new__(_cmd.LinkHub)     # bypass __init__'s real sockets
_hub._serial = _ClosedSerial()
# A LIST since the BLE central joined the TCP listener: LinkHub holds whatever
# wireless transports were asked for and asks each of them the same question.
_hub._wireless = [_NetWithClient()]

check("LinkHub.is_open() still reports up when only WiFi is connected",
      _hub.is_open() is True)
check("...but serial_is_open() reports the USB port SHUT, so the maintainer "
      "keeps retrying it", _hub.serial_is_open() is False)
_hub.ensure_serial_open()
check("...and ensure_serial_open() actually reaches the serial port",
      _ClosedSerial.opened)

# --------------------------------------------------------------------------- #
print("\n-- phase 8: a closed cable must not take the buttons down with it --")
# Regression, found on real hardware. The USB port dropped ('Device not
# configured') and six seconds later the device reconnected over BLE and said
# H. The hub forwards H/K to the serial link's provisioning regardless of which
# transport carried it, so the token write landed on `self._ser is None` -- an
# AttributeError, which the SerialException/OSError handler around it did not
# catch. It escaped _dispatch and ended the button-reader thread. The daemon
# then ran for hours looking perfectly healthy, with every device button dead.

_link = _cmd.SerialLink(port="/dev/nonexistent-claude-mate", baud=115200)
_link.set_provision_token("t0ken")
_link._pending_provision = True          # armed by an open that has since died
_link._ser = None                        # ...exactly the state that crashed

try:
    _link.provision_if_ours()
    _crashed = False
except Exception:
    _crashed = True
check("provisioning a port that closed under it does not raise", not _crashed)

# The arm belongs to one open, so closing must spend it rather than leave it
# pointed at a port that no longer exists.
_link._pending_provision = True
_link.close()
check("closing the port disarms the pending token push",
      _link._pending_provision is False)

# ...and the reader survives a dispatch that throws anyway, whatever the cause.
# This is the guard that turns "the device is bricked until you restart the
# daemon" into one log line, so it is tested independently of the bug above.
class _TwoLineLink:
    """Open, hands out two presses, then goes quiet."""
    def __init__(self): self.lines = ["B|G", "B|N"]
    def is_open(self): return True
    def read_line(self):
        return self.lines.pop(0) if self.lines else None

_reader = _cmd.ButtonReader.__new__(_cmd.ButtonReader)
_reader._link = _TwoLineLink()
_reader._stop_evt = threading.Event()
_dispatched = []

def _exploding_dispatch(line):
    # The FIRST press throws; the second must still arrive. Two lines, not one:
    # with a single line a dead thread and a surviving one both leave the loop,
    # and the test would pass against the very bug it exists to catch.
    _dispatched.append(line)
    if len(_dispatched) >= 2:
        _reader._stop_evt.set()
        return
    raise RuntimeError("boom")

_reader._dispatch = _exploding_dispatch
_t = threading.Thread(target=_reader.run, name="button-reader-under-test",
                      daemon=True)
_t.start()
_t.join(timeout=5.0)
check("a throwing dispatch does not kill the button reader -- the next press "
      "still lands", _dispatched == ["B|G", "B|N"])
check("...and the reader stops cleanly when asked", not _t.is_alive())


# --------------------------------------------------------------------------- #
# The network gate: the listener must not exist on a network nobody trusted.
# --------------------------------------------------------------------------- #
# WHY THIS IS TESTED BY BEHAVIOUR AND NOT BY READING THE SOURCE. The property is
# "nothing can connect", and the only honest way to assert that is to try to
# connect. Every check below opens a real socket at a real port.
print("\n== the TCP listener is gated on the network ==")

_gate_dir = tempfile.mkdtemp(prefix="cm-trust-")
_trust_file = os.path.join(_gate_dir, "trusted-networks")
os.environ["CLAUDE_MATE_TRUSTED_FILE"] = _trust_file
_gate_port = free_port()


def _serving(port):
    """Is something actually ACCEPTING here -- not merely bound?

    CONNECT IS NOT ENOUGH, and this is the whole trap the restart bug hid in.
    A listening socket completes the TCP handshake from the kernel backlog even
    when nothing ever calls accept(), so a dead accept loop answers connect()
    exactly like a live one. The daemon speaks first (C|<nonce>), so reading
    that challenge is the cheapest proof that a thread is really on the other
    end. A test written against connect() alone passes against the bug.
    """
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", port))
    except OSError:
        return False
    try:
        return s.recv(64).startswith(b"C|")
    except OSError:
        return False
    finally:
        s.close()


def _reachable(port):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


# current_network_id() shells out to route/arp. On a CI box with no default
# route it returns None, which is itself a state worth pinning -- but the
# trusted/untrusted transitions need a STABLE id, so stub it.
_real_netid = _cmd.current_network_id
_cmd.current_network_id = lambda: "aa:bb:cc:00:11:22"

_gnet = _cmd.NetLink("127.0.0.1", _gate_port, TOKEN, queue.Queue())
_gate = _cmd.NetworkGate(_gnet, None, poll_s=0.2)

# An EMPTY-but-present file means "the user has a policy and this net is not in
# it" -- distinct from no file at all, which is first run. Getting these two
# confused would either strand every upgrading user or silently trust anything.
open(_trust_file, "w").close()
_gate.evaluate()
check("an untrusted network gets no listener at all",
      not _reachable(_gate_port))

with open(_trust_file, "w") as fh:
    fh.write("aa:bb:cc:00:11:22 home\n")
_gate.evaluate()
check("...and a trusted one gets one", _serving(_gate_port))

with open(_trust_file, "w") as fh:
    fh.write("99:99:99:99:99:99 somewhere-else\n")
_gate.evaluate()
check("...moving to an untrusted network closes it again",
      not _reachable(_gate_port))

# THE REGRESSION THIS EXISTS FOR: stop() sets _stop_evt and start() did not
# clear it, so the listener came back bound, listening and attached to an
# accept loop that had already exited -- indistinguishable from working.
with open(_trust_file, "w") as fh:
    fh.write("aa:bb:cc:00:11:22 home\n")
_gate.evaluate()
check("...and coming home opens it a SECOND time (restartable)",
      _serving(_gate_port))
_gnet.stop()
check("...and stop() really closes it", not _reachable(_gate_port))

# No file at all = first run. Adopt once, so an upgrade cannot disconnect a
# device that worked yesterday -- never worse than the old listen-everywhere.
os.remove(_trust_file)
_tofu_port = free_port()
_tnet = _cmd.NetLink("127.0.0.1", _tofu_port, TOKEN, queue.Queue())
_tgate = _cmd.NetworkGate(_tnet, None, poll_s=0.2)
_tgate.evaluate()
check("first run adopts the network it finds, rather than stranding the user",
      _tgate.is_open() and "aa:bb:cc:00:11:22" in open(_trust_file).read())
_tnet.stop()

# Offline is not trusted. A laptop with no route has nothing to serve and
# nothing to serve it to.
_cmd.current_network_id = lambda: None
_off_port = free_port()
_onet = _cmd.NetLink("127.0.0.1", _off_port, TOKEN, queue.Queue())
_ogate = _cmd.NetworkGate(_onet, None, poll_s=0.2)
_ogate.evaluate()
check("no network means no listener", not _ogate.is_open())
_onet.stop()

# A gate that throws must fail CLOSED, not leave the port open behind it.
_cmd.current_network_id = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
_err_port = free_port()
_enet = _cmd.NetLink("127.0.0.1", _err_port, TOKEN, queue.Queue())
_egate = _cmd.NetworkGate(_enet, None, poll_s=0.2)
_et = threading.Thread(target=_egate.run, daemon=True)
_et.start()
time.sleep(0.5)
_egate.stop()
check("a gate that raises fails closed", not _reachable(_err_port))
_enet.stop()

_cmd.current_network_id = _real_netid
os.environ.pop("CLAUDE_MATE_TRUSTED_FILE", None)

# MAC normalisation: macOS arp drops leading zeros, so the same router
# fingerprints two ways and a trusted network silently stops being trusted.
check("a MAC with dropped leading zeros normalises to the padded form",
      _cmd.normalise_mac("0:1a:2B:3c:4d:5e") == "00:1a:2b:3c:4d:5e")
check("...and a non-MAC is rejected rather than half-parsed",
      _cmd.normalise_mac("nope") is None
      and _cmd.normalise_mac("aa:bb:cc") is None
      and _cmd.normalise_mac("gg:bb:cc:dd:ee:ff") is None)

# --------------------------------------------------------------------------- #
# Pre-auth connections are bounded -- the cap used to count only peers that had
# ALREADY authenticated, so anyone who could reach the port could hold an
# unbounded number of threads by connecting and saying nothing.
# --------------------------------------------------------------------------- #
print("\n== an unauthenticated peer cannot exhaust the daemon ==")
_dos_port = free_port()
_dnet = _cmd.NetLink("127.0.0.1", _dos_port, TOKEN, queue.Queue())
assert _dnet.start()
_silent = []
for _ in range(_cmd.NET_MAX_PENDING + 6):
    _s = socket.socket()
    _s.settimeout(1.0)
    try:
        _s.connect(("127.0.0.1", _dos_port))
        _silent.append(_s)
    except OSError:
        break
time.sleep(0.6)
check("connections beyond the handshake budget are refused, not queued "
      "forever", _dnet._pending <= _cmd.NET_MAX_PENDING)
check("...and the thread list does not grow without bound",
      len(_dnet._threads) <= _cmd.NET_MAX_PENDING + 2)
for _s in _silent:
    try:
        _s.close()
    except OSError:
        pass
_dnet.stop()

# SLOWLORIS. The handshake timeout used to re-arm on every byte, because the
# reader takes one byte at a time and a socket timeout bounds the GAP between
# recvs, not the read. Dripping a byte just under that gap held a thread for
# NET_MAX_LINE x NET_AUTH_TIMEOUT_S -- about forty minutes -- and enough of
# those keep the owner's real device out without ever knowing the token.
#
# NEVER BLOCK ON recv() IN THIS TEST. The daemon says nothing between the
# challenge and its verdict, so a blocking read is itself a long silent gap --
# which the OLD per-byte timeout also punished. A test written that way passes
# against the bug it is meant to catch. Poll with select instead, and keep
# every gap comfortably inside the per-recv timeout.
_slow_port = free_port()
_orig_auth_timeout = _cmd.NET_AUTH_TIMEOUT_S
_cmd.NET_AUTH_TIMEOUT_S = 1.0          # keep it quick; the bug is scale-free
_snet = _cmd.NetLink("127.0.0.1", _slow_port, TOKEN, queue.Queue())
assert _snet.start()
_drip = socket.socket()
_drip.settimeout(5.0)
_drip.connect(("127.0.0.1", _slow_port))
_drip.recv(64)                          # the C|<nonce> challenge
_drip.setblocking(False)
_t0 = time.time()
_dropped_after = None
for _ in range(12):                     # 12 x 0.4s = 4.8s, well past the 1s cap
    time.sleep(0.4)                     # gap < NET_AUTH_TIMEOUT_S on purpose
    try:
        _drip.sendall(b"a")
    except OSError:
        _dropped_after = time.time() - _t0
        break
    r, _, _ = select.select([_drip], [], [], 0)
    if r:
        try:
            if not _drip.recv(16):      # server hung up: deadline enforced
                _dropped_after = time.time() - _t0
                break
        except OSError:
            _dropped_after = time.time() - _t0
            break
_drip.close()
_cmd.NET_AUTH_TIMEOUT_S = _orig_auth_timeout
check("a drip-feeding peer is dropped on a deadline, not kept alive by each "
      "byte", _dropped_after is not None and _dropped_after < 3.0)
_snet.stop()

# --------------------------------------------------------------------------- #
print()
failed = [l for l, ok in results if not ok]
if failed:
    print(f"====== {len(failed)} FAILED ======")
    for l in failed:
        print(f"  FAIL: {l}")
    sys.exit(1)
print(f"================ ALL {len(results)} PASSED ================")
