#!/usr/bin/env python3
"""Controller mode on the wire: the verb, and re-asserting it on a handshake.

Two bugs shipped in this feature and BOTH were caught by reading a daemon log
against real hardware, because nothing here was covered:

  1. The downlink verb was `X|`, which is already `X|WIPE` in the firmware's
     config console. `handleConfigLine()` sees USB serial lines BEFORE the
     protocol handler, so the device answered "refusing: send X|WIPE to clear
     config" and controller mode never engaged -- while the daemon cheerfully
     logged "controller mode: ON". Over TCP it would have reached the right
     handler, so it misfired only on the transport used for flashing.

  2. The daemon sent the verb only when the grab CHANGED, so a device that
     connected while a page was already holding it was never told, and sat in
     menu semantics wondering why the game felt laggy.

`test_webbridge.py` already proves the bridge's grab state is correct. What it
cannot see is what the DAEMON puts on the device link in response, which is
where both bugs lived -- the wiring is in `main()`. So this drives the real
daemon over a PTY, exactly as `test_e2e.py` does, and reads the bytes.

The last check is static and guards the whole CLASS of bug 1 rather than the
one instance: a protocol verb that collides with a config verb is unreachable
over USB, silently. It asserts the two switches stay disjoint.
"""
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import pty
import tty

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DAEMON = os.path.join(ROOT, "daemon", "claude_mate_daemon.py")
INO = os.path.join(ROOT, "firmware", "claude_mate_s3", "claude_mate_s3.ino")

failures = []
checks = 0


def check(name, ok):
    global checks
    checks += 1
    print(f"   {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        failures.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# --- fake device over a PTY ---------------------------------------------------
master_fd, slave_fd = pty.openpty()
slave_name = os.ttyname(slave_fd)
tty.setraw(master_fd)

wire = []                      # every line the daemon wrote TO the device
wire_lock = threading.Lock()


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
                with wire_lock:
                    wire.append(s)


threading.Thread(target=pty_reader, daemon=True).start()


def mark():
    """Index into the wire log, so 'after this point' is expressible."""
    with wire_lock:
        return len(wire)


def sent_after(idx, pred):
    with wire_lock:
        return any(pred(l) for l in wire[idx:])


def wait_for(pred, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def device_send(s):
    os.write(master_fd, (s + "\n").encode())


# --- the daemon, with --web on a private port ---------------------------------
tmp = tempfile.mkdtemp(prefix="cm-controller-")
sock_path = os.path.join(tmp, "cm.sock")
PORT = free_port()

env = dict(os.environ)
env["CLAUDE_MATE_PORT"] = slave_name
env["CLAUDE_MATE_SOCK"] = sock_path
env["CLAUDE_MATE_BAUD"] = "115200"
env["CLAUDE_MATE_ACCOUNTS_DIR"] = "/nonexistent-claude-mate-accounts"
env.pop("CLAUDE_MATE_WEB", None)

print(f"== starting daemon (fake port {slave_name}, web :{PORT}) ==")
proc = subprocess.Popen(
    [sys.executable, DAEMON, "--web", "--web-port", str(PORT),
     "--web-root", os.path.join(ROOT, "site", "game")],
    env=env, stderr=subprocess.PIPE, text=True)

daemon_log = []


def err_reader():
    for ln in proc.stderr:
        daemon_log.append(ln.rstrip())


threading.Thread(target=err_reader, daemon=True).start()


def logged(pred):
    return any(pred(l) for l in list(daemon_log))


class Page:
    """An EventSource that takes the grab, and can be closed abruptly."""

    def __init__(self, port, grab=True, linger0=False):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        if linger0:
            # The crashed-tab case: RST rather than FIN.
            import struct
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                 struct.pack("ii", 1, 0))
        path = "/events?grab=1" if grab else "/events"
        self.sock.sendall(
            (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
             "Accept: text/event-stream\r\n\r\n").encode())
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            try:
                if not self.sock.recv(4096):
                    break
            except OSError:
                break

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


try:
    # The daemon opens the port and the device says hello, as it does on boot.
    if not wait_for(lambda: proc.poll() is None
                    and logged(lambda l: "serial opened" in l), 12.0):
        print("daemon never opened the fake serial port:")
        print("\n".join(daemon_log[-15:]))
        sys.exit(1)
    device_send("H")
    wait_for(lambda: sent_after(0, lambda l: l.startswith("F|")), 6.0)

    print("\n-- taking the grab puts the device into controller mode --")
    idx = mark()
    page = Page(PORT)
    got_on = wait_for(lambda: sent_after(idx, lambda l: l == "G|1"))
    check("a ?grab=1 page sends G|1 to the device", got_on)
    check("...and the daemon says so in the log",
          wait_for(lambda: logged(lambda l: "controller mode: ON" in l)))

    print("\n-- a handshake while the grab is HELD re-asserts it (bug 2) --")
    # A device that reboots, or connects late, announces itself with H. The
    # grab did not change, so nothing else would tell it.
    idx = mark()
    device_send("H")
    check("H while grabbed re-sends G|1",
          wait_for(lambda: sent_after(idx, lambda l: l == "G|1")))
    check("...and does not send G|0 in the same breath",
          not sent_after(idx, lambda l: l == "G|0"))

    print("\n-- releasing the grab takes it back out --")
    idx = mark()
    page.close()
    check("closing the page sends G|0", wait_for(lambda: sent_after(
        idx, lambda l: l == "G|0"), 8.0))

    print("\n-- a handshake with NO grab must not arm controller mode --")
    idx = mark()
    device_send("H")
    time.sleep(1.5)
    check("H while ungrabbed never sends G|1",
          not sent_after(idx, lambda l: l == "G|1"))

    print("\n-- the abruptly-closed tab also releases (crashed-tab case) --")
    idx = mark()
    page2 = Page(PORT, linger0=True)
    wait_for(lambda: sent_after(idx, lambda l: l == "G|1"))
    idx = mark()
    page2.close()
    check("an RST-closed page still sends G|0", wait_for(lambda: sent_after(
        idx, lambda l: l == "G|0"), 8.0))

    print("\n-- the verb that collided is never on the wire (bug 1) --")
    with wire_lock:
        x_lines = [l for l in wire if l.startswith("X|")]
    check("the daemon never writes an X| line to the device", not x_lines)
    check("the device never answered the config console's refusal",
          not logged(lambda l: "refusing: send X|WIPE" in l))

finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

# --- static: the two switches must stay disjoint ------------------------------
# handleConfigLine() runs FIRST on USB serial lines, so any protocol verb that
# it also claims is unreachable over USB -- which is exactly how `X|` failed,
# and the compiler cannot see it because the switches are in different
# functions.
print("\n-- firmware: protocol verbs and config verbs do not collide --")


def case_labels(src, signature):
    """Case labels of the switch inside one function, by brace matching."""
    start = src.index(signature)
    i = src.index("{", start)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    body = src[i:j]
    return set(re.findall(r"case\s+'(.)'\s*:", body))


with open(INO, encoding="utf-8") as fh:
    src = fh.read()

proto = case_labels(src, "static void handleLine(char *line)")
config = case_labels(src, "static bool handleConfigLine(char *line)")
overlap = proto & config

print(f"   protocol verbs: {' '.join(sorted(proto))}")
print(f"   config verbs  : {' '.join(sorted(config))}")
check("handleLine and handleConfigLine claim disjoint verbs",
      not overlap)
if overlap:
    print(f"        COLLIDING: {' '.join(sorted(overlap))} -- unreachable over USB")
check("the gamepad verb G is a protocol verb", "G" in proto)
check("...and the config console does not claim it", "G" not in config)
check("X is still the config console's (wipe) verb", "X" in config)

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("controller mode: OK")
