#!/usr/bin/env python3
"""
Test the DEVICE-AS-CONTROLLER bridge (daemon/webbridge.py) with NO hardware and
no browser: a real bridge is started on an ephemeral loopback port and driven
with raw sockets, exactly as a browser would drive it.

Covers the things that would hurt if they broke:

  * it binds LOOPBACK ONLY, and a non-loopback host is coerced back
  * it serves the static game, and REFUSES to serve anything outside the root
    (../, %2e%2e/, nested ../../.., and symlinks pointing out of the tree)
  * /status tells the page the truth about connected / grabbed / device
  * a button pushed from the (simulated) ButtonReader thread reaches an SSE
    client as `data: {"btn":"P"}`
  * GRAB is true only while a grabbing page is connected, and is released the
    moment it disconnects -- a closed tab must never leave the physical device
    permanently disconnected from its real job
  * a stale grab is reaped even if the socket never told us it died
  * a page that stops draining loses EVENTS, never the device: the queue drops
    OLDEST and push() never blocks
  * stop() unblocks every stream

Run:   python3 tools/test_webbridge.py       (stdlib only, no pyserial needed)
"""
import importlib.util
import json
import os
import socket
import shutil
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = os.path.join(REPO, "daemon", "webbridge.py")

_spec = importlib.util.spec_from_file_location("webbridge_under_test", MODULE)
wb = importlib.util.module_from_spec(_spec)
sys.modules["webbridge_under_test"] = wb
_spec.loader.exec_module(wb)

results = []


def check(label, ok):
    results.append((label, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def wait_for(pred, timeout=5.0, tick=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = pred()
        if val:
            return val
        time.sleep(tick)
    return False


def quiet(msg):
    print(f"   [bridge] {msg}")


# --------------------------------------------------------------------------- #
# Raw HTTP + SSE clients (no urllib: it would normalise the traversal paths we
# are specifically trying to smuggle past the server)
# --------------------------------------------------------------------------- #


def raw_get(port, path, method="GET", extra=None, host=None, timeout=5.0):
    """Returns (status, header_blob, body). Sends `path` byte-for-byte."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    req = f"{method} {path} HTTP/1.1\r\n"
    req += f"Host: {host or ('127.0.0.1:%d' % port)}\r\n"
    req += "Connection: close\r\n"
    for k, v in (extra or {}).items():
        req += f"{k}: {v}\r\n"
    req += "\r\n"
    s.sendall(req.encode())
    data = b""
    try:
        while True:
            chunk = s.recv(8192)
            if not chunk:
                break
            data += chunk
    except (socket.timeout, OSError):
        pass
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    try:
        status = int(head.split(b" ", 2)[1])
    except (IndexError, ValueError):
        status = 0
    return status, head.decode(errors="replace"), body


class SSEClient:
    """What EventSource does, in 40 lines."""

    def __init__(self, port, grab=False, consume=True):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        path = "/events?grab=1" if grab else "/events"
        self.sock.sendall(
            (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
             "Accept: text/event-stream\r\n\r\n").encode())
        self.buttons = []          # decoded {"btn": ...} payloads
        self.comments = 0          # ": keepalive" heartbeats
        self.hello = None
        self.headers = ""
        self.closed = False
        self.lock = threading.Lock()
        self._buf = b""
        if consume:
            threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            try:
                chunk = self.sock.recv(4096)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            self._buf += chunk
            if not self.headers and b"\r\n\r\n" in self._buf:
                head, _, self._buf = self._buf.partition(b"\r\n\r\n")
                self.headers = head.decode(errors="replace")
            while b"\n\n" in self._buf:
                block, self._buf = self._buf.split(b"\n\n", 1)
                self._frame(block.decode(errors="replace"))
        self.closed = True

    def _frame(self, block):
        name, data = "message", []
        for line in block.splitlines():
            if line.startswith(":"):
                with self.lock:
                    self.comments += 1
            elif line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if not data:
            return
        payload = "\n".join(data)
        try:
            obj = json.loads(payload)
        except ValueError:
            return
        with self.lock:
            if name == "hello":
                self.hello = obj
            elif "btn" in obj:
                self.buttons.append(obj["btn"])
                print(f"   SSE  <= {payload}")

    def got(self, code):
        with self.lock:
            return code in self.buttons

    def count(self):
        with self.lock:
            return len(self.buttons)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# A game root to serve, plus a secret OUTSIDE it to try to steal
# --------------------------------------------------------------------------- #
tmp = tempfile.mkdtemp(prefix="cm-web-")
root = os.path.join(tmp, "game")
os.makedirs(os.path.join(root, "sub"))
with open(os.path.join(root, "index.html"), "w") as fh:
    fh.write("<h1>CLAUDE-MATE-GAME-OK</h1>")
with open(os.path.join(root, "sub", "app.js"), "w") as fh:
    fh.write("console.log('asset-ok');")
with open(os.path.join(tmp, "secret.txt"), "w") as fh:
    fh.write("SUPERSECRET-DO-NOT-SERVE")
os.symlink(os.path.join(tmp, "secret.txt"), os.path.join(root, "escape.txt"))

device_up = [False]          # stands in for the daemon's link.is_open

# --------------------------------------------------------------------------- #
print("\n-- phase 0: it binds LOOPBACK ONLY --")
lan = wb.WebBridge(root=root, host="0.0.0.0", port=0, log=quiet)
check("a non-loopback bind address is coerced to 127.0.0.1",
      lan.host == "127.0.0.1")

bridge = wb.WebBridge(root=root, host="127.0.0.1", port=0,
                      device=lambda: device_up[0], log=quiet,
                      queue_max=8, heartbeat=0.4)
check("the bridge starts on an ephemeral port", bridge.start())
port = bridge.port
check(f"...and reports the port it actually got ({port})", port > 0)
check("...listening on 127.0.0.1, never 0.0.0.0",
      bridge._srv.server_address[0] == "127.0.0.1")

# --------------------------------------------------------------------------- #
print("\n-- phase 1: it serves the static game --")
st, head, body = raw_get(port, "/")
check("GET / serves index.html", st == 200 and b"CLAUDE-MATE-GAME-OK" in body)
st, head, body = raw_get(port, "/sub/app.js")
check("GET /sub/app.js serves the asset", st == 200 and b"asset-ok" in body)
check("...with a javascript content type", "javascript" in head.lower())
st, _, body = raw_get(port, "/nope.txt")
check("a missing file is a 404", st == 404)
st, head, body = raw_get(port, "/", method="HEAD")
check("HEAD / is 200 with no body", st == 200 and body == b"")

# --------------------------------------------------------------------------- #
print("\n-- phase 2: it refuses to serve anything outside the root --")
attacks = [
    "/../secret.txt",
    "/%2e%2e/secret.txt",
    "/sub/../../secret.txt",
    "/a/b/../../../secret.txt",
    "/..%2fsecret.txt",
    "/escape.txt",                       # symlink pointing out of the tree
]
for path in attacks:
    st, _, body = raw_get(port, path)
    leaked = b"SUPERSECRET" in body
    check(f"{path} is refused ({st}) and leaks nothing",
          st in (403, 404) and not leaked)
st, _, body = raw_get(port, "/etc/passwd")
check("/etc/passwd cannot escape the root either",
      st == 404 and b"root:" not in body)

print("\n-- phase 2b: a rebinding page cannot reach it --")
st, _, _ = raw_get(port, "/status", host="evil.example.com")
check("a request with a non-local Host is 403 (DNS-rebinding guard)", st == 403)
st, _, _ = raw_get(port, "/status", extra={"Origin": "http://evil.example.com"})
check("a cross-origin request is 403", st == 403)
st, _, _ = raw_get(port, "/status",
                   extra={"Origin": f"http://127.0.0.1:{port}"})
check("...but our own origin is fine", st == 200)
st, _, _ = raw_get(port, "http://evil.example.com/status", host=f"127.0.0.1:{port}")
check("an absolute-form target cannot smuggle past the Host check", st == 403)

# The hole this closes: checking Origin only when PRESENT admits every
# cross-site SUBRESOURCE load, because those send no Origin and their Host is
# 127.0.0.1. <img src="http://127.0.0.1:8788/events?grab=1"> on any page you
# happen to visit took the grab -- CORS stops that page READING the stream, but
# reading is not the attack. Holding the socket is, and while it is held the
# device's buttons stop switching your sessions.
st, _, _ = raw_get(port, "/status", extra={"Sec-Fetch-Site": "cross-site"})
check("a cross-site request with no Origin is 403", st == 403)
st, _, _ = raw_get(port, "/events?grab=1",
                   extra={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "iframe"})
check("...including an iframe reaching for the grab", st == 403)
check("...and it never took the grab", bridge.grabbed() is False)
st, _, _ = raw_get(port, "/events?grab=1",
                   extra={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "image"})
check("an <img> pointed at the stream is 403", st == 403)
# The regression that a Dest blocklist causes: the page's own module load sends
# Sec-Fetch-Dest: script, and refusing it 403s engine.js and the game never
# boots. Site is the discriminator; Dest is not.
st, _, _ = raw_get(port, "/engine.js",
                   extra={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Dest": "script"})
check("the page's own <script> load is served, not blocked as a subresource",
      st in (200, 404))
st, _, _ = raw_get(port, "/status", extra={"Sec-Fetch-Site": "same-origin"})
check("...but our own page is still fine", st == 200)
st, _, _ = raw_get(port, "/status", extra={"Sec-Fetch-Site": "none"})
check("...and a typed-in URL (Sec-Fetch-Site: none) is fine", st == 200)
# raw_get() treats host="" as "use the default", so this one goes out by hand:
# HTTP/1.0 with no Host header at all. "Absent" must not read as "allowed".
_s = socket.create_connection(("127.0.0.1", port), timeout=4)
_s.sendall(b"GET /status HTTP/1.0\r\n\r\n")
_d = b""
try:
    while True:
        _c2 = _s.recv(4096)
        if not _c2:
            break
        _d += _c2
except (socket.timeout, OSError):
    pass
_s.close()
check("a request with no Host at all is refused, not defaulted-open",
      b" 403 " in _d.split(b"\r\n")[0] or _d.split(b"\r\n")[0].endswith(b"403 Forbidden"))

# A body on a HEAD response is not merely wasteful: nothing closes the
# connection, so the stray bytes become the NEXT response's status line and the
# failure surfaces as BadStatusLine on an unrelated later request.
import http.client as _hc
_c = _hc.HTTPConnection("127.0.0.1", port, timeout=4)
_c.request("HEAD", "/definitely-not-here.txt")
_r = _c.getresponse(); _r.read()
check("HEAD on a missing file is 404", _r.status == 404)
try:
    _c.request("GET", "/")
    _r2 = _c.getresponse(); _r2.read()
    _ok = (_r2.status == 200)
except Exception:
    _ok = False
check("...and the connection is not desynced by a body on HEAD", _ok)
_c.close()

# --------------------------------------------------------------------------- #
print("\n-- phase 3: /status --")
st, head, body = raw_get(port, "/status")
js = json.loads(body)
check("GET /status is JSON", st == 200 and "json" in head.lower())
check("...nobody connected yet", js["connected"] is False)
check("...nothing grabbed", js["grabbed"] is False)
check("...and the device reads as OFFLINE while the link is shut",
      js["device"] is False)
device_up[0] = True
js = json.loads(raw_get(port, "/status")[2])
check("...it follows the real link: device true once it is open",
      js["device"] is True)

# --------------------------------------------------------------------------- #
print("\n-- phase 4: a pushed button reaches the page --")
mirror = SSEClient(port, grab=False)
check("an SSE client gets text/event-stream",
      bool(wait_for(lambda: "text/event-stream" in mirror.headers.lower())))
check("...and a hello frame telling it whether it grabbed",
      bool(wait_for(lambda: mirror.hello is not None))
      and mirror.hello.get("grab") is False)
check("...the bridge now reports a connected page", bridge.connected())

bridge.push("P")                       # <- the ButtonReader thread's call
check("a pushed P arrives as data: {\"btn\":\"P\"}",
      bool(wait_for(lambda: mirror.got("P"))))
for code in ("N", "G", "K", "M"):
    bridge.push(code)
check("every existing code (N/G/K/M) makes the trip",
      bool(wait_for(lambda: all(mirror.got(c) for c in "NGKM"))))
check("a keepalive comment is sent on an idle stream",
      bool(wait_for(lambda: mirror.comments > 0, 3.0)))

# --------------------------------------------------------------------------- #
print("\n-- phase 5: GRAB is true only while a grabbing page is connected --")
check("a MIRROR-only page does NOT grab the buttons", bridge.grabbed() is False)
js = json.loads(raw_get(port, "/status")[2])
check("...and /status agrees (connected, not grabbed)",
      js["connected"] is True and js["grabbed"] is False)

player = SSEClient(port, grab=True)
check("a ?grab=1 page takes the grab",
      bool(wait_for(lambda: bridge.grabbed())))
check("...its hello says so", bool(wait_for(lambda: player.hello is not None))
      and player.hello.get("grab") is True)
js = json.loads(raw_get(port, "/status")[2])
check("...and /status reports grabbed, so the page can say CONTROLLER: "
      "CLAUDE MATE", js["grabbed"] is True)
bridge.push("G")
check("the grabbing page receives buttons too",
      bool(wait_for(lambda: player.got("G"))))
check("...and the mirror still sees them (grab steals from the DAEMON, "
      "not from other pages)", bool(wait_for(lambda: mirror.got("G"))))

print("\n-- phase 6: the grab is released when the tab closes --")
player.close()
check("closing the grabbing page releases the grab",
      bool(wait_for(lambda: bridge.grabbed() is False, 6.0)))
check("...within a couple of seconds, not a heartbeat later",
      bridge.grabbed() is False)
check("...while the OTHER page stays connected", bridge.connected())
bridge.push("N")
check("...and the device keeps talking to the survivor",
      bool(wait_for(lambda: mirror.count() >= 6)))

print("\n-- phase 6b: a grab that dies WITHOUT closing hits the hard cap --")
# The case no socket can tell us about: a tab that stopped reading but never
# hung up. This used to be tested by back-dating last_write, which is why the
# broken version passed -- wfile.write() returns as soon as bytes reach the
# kernel send buffer, so a client that never reads refreshes last_write forever
# and the staleness reap could not fire for the one case it existed for.
# The cap does not ask the peer for anything: a stream is closed once it has
# been open for GRAB_MAX_S, and a live browser silently redials.
zombie = SSEClient(port, grab=True)
check("a second grabbing page takes the grab again",
      bool(wait_for(lambda: bridge.grabbed())))
with bridge._lock:
    for c in bridge._clients.values():
        if c.grab:
            c.opened = time.monotonic() - (wb.GRAB_MAX_S + 5.0)
check("a grab older than the hard cap is dropped",
      bridge.grabbed() is False)
check("...and its stream is torn down, not left dangling",
      bool(wait_for(lambda: zombie.closed, 4.0)))
zombie.close()
check("...the device is back to its real job (nothing grabbed)",
      bridge.grabbed() is False)

# --------------------------------------------------------------------------- #
print("\n-- phase 7: a stalled page loses EVENTS, never the device --")
# A client that never drains its queue: exactly a browser tab wedged behind a
# blocking script. The device's button path runs on the ButtonReader thread and
# must not care.
stalled = bridge._register(True, "stalled-tab")
depth = bridge.queue_max
for i in range(depth + 20):
    bridge.push("X%d" % i)
check(f"the per-client queue stays bounded at {depth}",
      stalled.q.qsize() == depth)
check("...and the overflow was counted as dropped", stalled.dropped == 20)
oldest = json.loads(stalled.q.get_nowait())["btn"]
check("...it dropped the OLDEST, keeping the press you just made",
      oldest == "X20")

t0 = time.monotonic()
for i in range(5000):
    bridge.push("P")
elapsed = time.monotonic() - t0
check(f"5000 pushes into a stalled client never block ({elapsed*1000:.0f} ms)",
      elapsed < 1.0)
bridge._unregister(stalled)
bridge.push("M")
check("...and a healthy page is still being served after all that",
      bool(wait_for(lambda: mirror.got("M"))))
check("push() with no clients at all is a no-op, not an error",
      bridge.push("P") is None)

# --------------------------------------------------------------------------- #
print("\n-- phase 8: clean shutdown --")
survivor = SSEClient(port, grab=True)
check("one last grabbing page", bool(wait_for(lambda: bridge.grabbed())))
bridge.stop()
check("stop() unblocks the open stream",
      bool(wait_for(lambda: survivor.closed, 5.0)))
check("...nothing is grabbed once the bridge is stopped",
      bridge.grabbed() is False)


def port_open(p):
    try:
        socket.create_connection(("127.0.0.1", p), timeout=0.5).close()
        return True
    except OSError:
        return False


check("...and the listener is closed", not wait_for(lambda: port_open(port), 1.0))
bridge.stop()
check("stop() is idempotent", True)
survivor.close()
mirror.close()

print("\n-- phase 9: a missing game root does not dead-end --")
gone = wb.WebBridge(root=os.path.join(tmp, "no-such-dir"), port=0, log=quiet)
check("the bridge starts even with no game installed", gone.start())
st, _, body = raw_get(gone.port, "/")
check("...and / serves the built-in button tester instead of a 404",
      st == 200 and b"bridge" in body.lower())
gone.stop()

busy = wb.WebBridge(root=root, port=0, log=quiet)
busy.start()
clash = wb.WebBridge(root=root, port=busy.port, log=quiet)
check("a port already in use fails soft (False), it does not crash the daemon",
      clash.start() is False)
busy.stop()

# --------------------------------------------------------------------------- #
shutil.rmtree(tmp, ignore_errors=True)
print()
failed = [l for l, ok in results if not ok]
if failed:
    print(f"====== {len(failed)} FAILED ======")
    for l in failed:
        print(f"  FAIL: {l}")
    sys.exit(1)
print(f"================ ALL {len(results)} PASSED ================")
