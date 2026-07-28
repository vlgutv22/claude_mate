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
import socket
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
        self.sock.settimeout(timeout)
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
# Phase 0: --tcp with NO token must refuse to listen (fail closed)
# --------------------------------------------------------------------------- #
print("\n-- phase 0: --tcp without a token refuses to listen --")
tmp = tempfile.mkdtemp(prefix="cm-net-")
port_noauth = free_port()
env = dict(os.environ)
env["CLAUDE_MATE_SOCK"] = os.path.join(tmp, "noauth.sock")
env["CLAUDE_MATE_PORT"] = "/dev/null/nope"      # never opens; that is fine
env["CLAUDE_MATE_TOKEN"] = ""
env["CLAUDE_MATE_TOKEN_FILE"] = os.path.join(tmp, "does-not-exist")
noauth = subprocess.Popen(
    [sys.executable, DAEMON, "--tcp", "--tcp-port", str(port_noauth),
     "--tcp-bind", "127.0.0.1"],
    env=env, stderr=subprocess.PIPE, text=True)
noauth_err = []
threading.Thread(
    target=lambda: [noauth_err.append(l) for l in noauth.stderr],
    daemon=True).start()
time.sleep(2.0)


def port_closed():
    try:
        socket.create_connection(("127.0.0.1", port_noauth), timeout=0.5).close()
        return False
    except OSError:
        return True


check("--tcp without a token does NOT open a listener", port_closed())
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
# Phase 1: a wrong token is rejected
# --------------------------------------------------------------------------- #
print("\n-- phase 1: a wrong token is rejected --")
impostor = FakeDevice(tcp_port, "wrong-token")
got_in = impostor.connect()
check("a device with the WRONG token is refused", not got_in)
check("...and is told so (A|NO) rather than left hanging", impostor.reject)
impostor.close()

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
check("the daemon logged the disconnect of the old socket",
      any("TCP device disconnected" in l for l in daemon_err))

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
print()
failed = [l for l, ok in results if not ok]
if failed:
    print(f"====== {len(failed)} FAILED ======")
    for l in failed:
        print(f"  FAIL: {l}")
    sys.exit(1)
print(f"================ ALL {len(results)} PASSED ================")
