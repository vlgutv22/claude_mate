#!/usr/bin/env python3
"""
Device-as-controller bridge: the Claude Mate hardware becomes a gamepad for a
web page running in a browser on the SAME Mac.

    device --USB/WiFi--> daemon --(this)--> http://127.0.0.1:8788 --> browser

A tiny threaded HTTP server on the loopback interface does two jobs:

    GET /            the static game from site/game/ (same origin as /events,
                     so there is no CORS dance and no mixed-content problem)
    GET /events      a Server-Sent Events stream of button presses:
                         data: {"btn":"P"}
                     codes are the device's existing P / N / G / K / M (/ F)
    GET /status      {"connected":bool,"grabbed":bool,"device":bool} so the
                     page can honestly say CONTROLLER: CLAUDE MATE vs KEYBOARD

GRAB is the whole design. `GET /events?grab=1` means "while I am connected the
device's buttons drive the GAME, not the session switcher". The daemon asks
`bridge.grabbed()` before acting on a button and skips its normal session
action when the answer is yes. Without ?grab=1 a client is a read-only mirror:
it sees every press and steals nothing.

A grab must never outlive the page that took it -- a browser tab closed at the
wrong moment must not leave the physical device permanently disconnected from
its real job. Three independent releases, weakest last:

    1. the stream loop notices EOF on the socket (clean close, ~1 s)
    2. a write to a dead/wedged peer fails or times out (WRITE_TIMEOUT_S)
    3. a stream is closed once it has been open for GRAB_MAX_S, whatever it
       claims. A live tab reconnects within `retry` and never notices; a wedged
       one simply expires. Heartbeats are written every ~15 s so a healthy
       client is never
       within a factor of two of that deadline

SAFETY: the listener is bound to 127.0.0.1 and a non-loopback host is coerced
back to it, loudly. This must not be reachable from the network -- it can move
the selection on someone's desk. Requests with a non-local Host or a foreign
Origin are refused, which closes the DNS-rebinding hole that local HTTP servers
otherwise leave open.

Stdlib only (http.server), Python 3.9 compatible, and importable on its own:
this module knows nothing about the daemon, and the daemon needs only three
calls -- push(), grabbed(), stop().
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import select
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_HOST = "127.0.0.1"       # LOOPBACK ONLY. See _safe_host().
DEFAULT_PORT = 8788
DEFAULT_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site", "game")

HEARTBEAT_S = 15.0        # ": keepalive" comment cadence on an idle stream
GRAB_TTL_S = 45.0         # kept for the constructor's signature; see GRAB_MAX_S

# THE HARD CAP, and the reason it replaced a staleness timeout. The old rule was
# "reap a client whose last successful write is older than grab_ttl", which
# cannot fire for the case it existed for: wfile.write() returns as soon as the
# bytes reach the kernel send buffer, so a socket that is open but never read
# refreshes last_write forever. Measured: a client that never reads still held
# the grab after 12 s with grab_ttl set to 2 s, and at the real settings a 13
# byte heartbeat needs ~1.7 days to fill a 128 KB buffer.
#
# An absolute lifetime does not depend on the peer telling us anything. The
# browser's EventSource reconnects automatically after `retry`, so a live page
# renews its grab without a flicker, while a wedged socket or a crashed tab is
# gone within the cap.
GRAB_MAX_S = 90.0
STREAM_TICK_S = 1.0       # stream loop wake-up: bounds disconnect detection
WRITE_TIMEOUT_S = 5.0     # a peer that will not drain fails instead of blocking
CLIENT_QUEUE_MAX = 64     # per-client backlog; overflow DROPS OLDEST
RETRY_MS = 1500           # EventSource reconnect hint

_LOCAL_HOSTS = {"localhost", "localhost.localdomain", "127.0.0.1", "::1",
                "0:0:0:0:0:0:0:1"}

_CLOSE = object()         # sentinel: unblocks a stream loop on stop()


def _default_log(msg: str) -> None:
    sys.stderr.write("[claude-mate %s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _safe_host(host: str, log: Callable[[str], None]) -> str:
    """Coerce anything that is not loopback back to 127.0.0.1.

    Deliberately not a fatal error and deliberately not obeyed: a typo or a
    copied --tcp-bind must not put a "press this button" endpoint on the LAN.
    """
    h = (host or "").strip()
    if h.lower() in _LOCAL_HOSTS:
        return h
    log("web bridge: refusing to bind %r -- this endpoint drives your device "
        "and stays on loopback; using %s" % (host, DEFAULT_HOST))
    return DEFAULT_HOST


def _host_is_local(value: Optional[str]) -> bool:
    """True for a Host/Origin authority that names this machine."""
    if not value:
        return False
    v = value.strip()
    if v.startswith("["):                      # [::1]:8788
        host = v[1:v.index("]")] if "]" in v else v[1:]
    else:
        host = v.split(":")[0]
    return host.lower() in _LOCAL_HOSTS


def _peer_gone(conn: socket.socket) -> bool:
    """Has the browser hung up? Cheap, non-blocking, never raises.

    An SSE client sends nothing after its request, so "readable" means either
    EOF (tab closed) or junk we do not care about. This is what turns a closed
    tab into a released grab within STREAM_TICK_S instead of one heartbeat.
    """
    try:
        readable, _, _ = select.select([conn], [], [], 0)
    except (OSError, ValueError):
        return True
    if not readable:
        return False
    try:
        return conn.recv(1, socket.MSG_PEEK) == b""
    except BlockingIOError:
        return False
    except (socket.timeout, InterruptedError):
        return False
    except OSError:
        return True


# --------------------------------------------------------------------------- #
# One connected browser
# --------------------------------------------------------------------------- #


class _Client:
    """A live /events stream. Owned by its handler thread; the fields the
    pusher touches (`q`, `alive`) are the only ones shared."""

    __slots__ = ("cid", "grab", "q", "alive", "opened", "last_write", "dropped",
                 "peer")

    def __init__(self, cid: int, grab: bool, peer: str, maxsize: int) -> None:
        self.cid = cid
        self.grab = bool(grab)
        self.peer = peer
        self.q: "queue.Queue[Any]" = queue.Queue(maxsize)
        self.alive = True
        self.opened = time.monotonic()
        self.last_write = time.monotonic()
        self.dropped = 0


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


class _BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler_cls, bridge: "WebBridge") -> None:
        self.bridge = bridge
        super().__init__(addr, handler_cls)

    def handle_error(self, request, client_address) -> None:
        # A browser closing an SSE stream is normal, not a traceback -- but a
        # real bug in the handler must not be swallowed with it.
        exc = sys.exc_info()[1]
        if isinstance(exc, (OSError, ConnectionError)):
            return
        self.bridge._log("web bridge: handler error from %s: %r"
                         % (client_address, exc))


class _Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeMateBridge/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30.0                 # cap idle keep-alive connections

    # http.server's default logging is one stderr line per asset; the daemon's
    # log is a signal channel, not an access log.
    def log_message(self, fmt: str, *args) -> None:
        pass

    def log_error(self, fmt: str, *args) -> None:
        pass

    @property
    def bridge(self) -> "WebBridge":
        return self.server.bridge          # type: ignore[attr-defined]

    # ---- routing ---------------------------------------------------------- #

    def do_GET(self) -> None:
        self._route(head_only=False)

    def do_HEAD(self) -> None:
        self._route(head_only=True)

    def _route(self, head_only: bool) -> None:
        # DNS-rebinding guard: a page on evil.example that resolves to
        # 127.0.0.1 would otherwise be able to grab the device. A request with
        # no Host at all is refused too -- it is not reachable from a browser,
        # and "absent" must not read as "allowed".
        host = self.headers.get("Host")
        if not host or not _host_is_local(host):
            self._fail(403, "non-local Host", head_only)
            return

        # CROSS-ORIGIN. Checking Origin only when PRESENT does not close this,
        # and that was the hole: cross-site subresource loads send no Origin and
        # their Host is 127.0.0.1, so <img src="http://127.0.0.1:8788/events?grab=1">
        # or a no-cors fetch on any page you happen to visit was admitted and
        # took the grab. CORS stops that page READING the stream, but reading is
        # not the attack -- holding the socket is, and while it is held the
        # device's buttons stop switching your sessions.
        #
        # Sec-Fetch-Site is sent by every browser that can make this request and
        # cannot be forged from script, so it is the guard that actually works.
        # Non-browser clients (curl, the test suite) send neither header and are
        # unaffected -- this is loopback-only either way.
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            self._fail(403, "cross-site request", head_only)
            return
        # NO Sec-Fetch-Dest blocklist here. The first attempt refused dest in
        # {image, script, style, ...} on the theory that the stream is neither --
        # and that 403'd the page's own <script src="engine.js">, whose dest IS
        # "script". Site is the discriminator; Dest is not, because a same-origin
        # subresource is exactly what this server exists to serve.
        origin = self.headers.get("Origin")
        if origin:
            try:
                if not _host_is_local(urllib.parse.urlsplit(origin).netloc):
                    self._fail(403, "cross-origin", head_only)
                    return
            except ValueError:
                self._fail(403, "bad Origin", head_only)
                return

        parsed = urllib.parse.urlsplit(self.path)
        # Absolute-form request targets ("GET http://host/x HTTP/1.1") are legal
        # and bypass a Host-only check, so the authority is vetted the same way.
        if parsed.netloc and not _host_is_local(parsed.netloc):
            self._fail(403, "non-local target", head_only)
            return
        path = parsed.path or "/"
        if path == "/events":
            if head_only:
                self._fail(405, "HEAD is not meaningful on a stream", head_only)
                return
            self._serve_events(urllib.parse.parse_qs(parsed.query))
            return
        if path == "/status":
            self._serve_status(head_only)
            return
        self._serve_static(path, head_only)

    # ---- /events ---------------------------------------------------------- #

    def _serve_events(self, query: Dict[str, List[str]]) -> None:
        bridge = self.bridge
        grab_vals = query.get("grab") or ["0"]
        grab = _truthy(grab_vals[0])
        peer = "%s:%s" % self.client_address[:2]
        client = bridge._register(grab, peer)
        try:
            self.close_connection = True       # a stream ends when it ends
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            # From here on the socket must never block us forever: a wedged tab
            # that stops draining is a dead client, not a stalled daemon.
            try:
                self.connection.settimeout(WRITE_TIMEOUT_S)
            except OSError:
                pass

            self._emit(client, "retry: %d\n\n" % RETRY_MS)
            hello = json.dumps({"grab": client.grab, "device": bridge.device_online()})
            self._emit(client, "event: hello\ndata: %s\n\n" % hello)

            last_beat = time.monotonic()
            while client.alive and not bridge.stopping():
                try:
                    item = client.q.get(timeout=STREAM_TICK_S)
                except queue.Empty:
                    item = None
                if item is _CLOSE:
                    break
                if item is not None:
                    self._emit(client, "data: %s\n\n" % item)
                now = time.monotonic()
                if now - client.opened >= GRAB_MAX_S:
                    break                    # hard cap; a live browser redials
                if now - last_beat >= bridge.heartbeat:
                    self._emit(client, ": keepalive\n\n")
                    last_beat = now
                if _peer_gone(self.connection):
                    break
        except (OSError, ValueError):
            pass                                # hung up mid-write: normal
        finally:
            bridge._unregister(client)

    def _emit(self, client: _Client, text: str) -> None:
        """Write one SSE chunk and record that the peer took it."""
        self.wfile.write(text.encode("utf-8"))
        try:
            self.wfile.flush()
        except AttributeError:                  # pragma: no cover
            pass
        client.last_write = time.monotonic()

    # ---- /status ---------------------------------------------------------- #

    def _serve_status(self, head_only: bool) -> None:
        body = json.dumps(self.bridge.status()).encode("utf-8")
        self._send(200, "application/json; charset=utf-8", body, head_only)

    # ---- static ----------------------------------------------------------- #

    def _serve_static(self, url_path: str, head_only: bool) -> None:
        root_real = os.path.realpath(self.bridge.root)
        rel = urllib.parse.unquote(url_path or "/")
        if "\x00" in rel:
            self._fail(400, "bad path", head_only)
            return
        if rel.endswith("/"):
            rel += "index.html"
        rel = rel.lstrip("/")                   # or join() would honour /etc/...
        if not rel:
            rel = "index.html"

        # Traversal guard: resolve symlinks and every ".." BEFORE deciding, then
        # require the result to live inside the root. Catches ../, %2e%2e%2f,
        # nested a/../../, and a symlink pointing out of the tree alike.
        target = os.path.realpath(os.path.join(root_real, rel))
        if target != root_real and not target.startswith(root_real + os.sep):
            self._fail(403, "forbidden", head_only)
            return

        if not os.path.isfile(target):
            if rel == "index.html":
                # No game installed yet: say so usefully instead of 404ing into
                # a dead end. The fallback page is a working button tester.
                self._send(200, "text/html; charset=utf-8",
                           _fallback_page(self.bridge), head_only)
                return
            self._fail(404, "not found", head_only)
            return

        ctype, _ = mimetypes.guess_type(target)
        if not ctype:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json"):
            ctype += "; charset=utf-8"
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError:
            self._fail(404, "not found", head_only)
            return
        self._send(200, ctype, body, head_only)

    # ---- plumbing --------------------------------------------------------- #

    def _send(self, status: int, ctype: str, body: bytes,
              head_only: bool = False) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
        except OSError:
            self.close_connection = True

    def _fail(self, status: int, why: str, head_only: bool = False) -> None:
        # head_only matters: _send() honours it, but _fail() used to drop it, so
        # every 4xx answer to a HEAD advertised Content-Length AND wrote the
        # body. Nothing closes the connection, so those bytes became the next
        # response's status line -- a keep-alive desync that shows up as
        # BadStatusLine on the FOLLOWING request, far from the cause.
        self._send(status, "text/plain; charset=utf-8",
                   ("%d %s\n" % (status, why)).encode("utf-8"), head_only)


def _fallback_page(bridge: "WebBridge") -> bytes:
    return ("""<!doctype html><meta charset="utf-8">
<title>Claude Mate controller bridge</title>
<style>
 body{font:15px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;
      background:#111;color:#eee;margin:0;padding:6vh 6vw}
 h1{font-size:1.2rem;color:#d97757;margin:0 0 1rem}
 code{background:#222;padding:.1em .4em;border-radius:4px}
 #btn{font-size:5rem;margin:.3em 0;min-height:1.2em}
 #st{color:#8a8}
</style>
<h1>Claude Mate &rarr; browser bridge</h1>
<p>The bridge is running, but there is no game at<br><code>"""
            + bridge.root.replace("&", "&amp;").replace("<", "&lt;")
            + """</code>.</p>
<p>Drop an <code>index.html</code> there and reload. Meanwhile, this page is a
live button tester &mdash; press a button on the device:</p>
<div id="btn">&middot;</div>
<div id="st">connecting&hellip;</div>
<script>
const b=document.getElementById('btn'),s=document.getElementById('st');
const es=new EventSource('/events');           // read-only: no grab taken here
es.onmessage=e=>{b.textContent=JSON.parse(e.data).btn;};
es.onopen=()=>s.textContent='connected (mirror only, buttons still switch sessions)';
es.onerror=()=>s.textContent='disconnected, retrying\\u2026';
</script>
""").encode("utf-8")


# --------------------------------------------------------------------------- #
# The bridge
# --------------------------------------------------------------------------- #


class WebBridge:
    """Loopback HTTP + SSE bridge between the device's buttons and a web page.

    Public surface, all thread-safe and none of it blocking:

        start()      -> bool     bind + serve (False if the port is unusable)
        stop()                   close the server, unblock every stream
        push(code)               fan a button code out to connected pages
        grabbed()    -> bool     is a page currently driving the buttons?
        connected()  -> bool     is any page listening at all?
        status()     -> dict     {connected, grabbed, device, clients}
    """

    def __init__(self,
                 root: Optional[str] = None,
                 host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT,
                 device: Optional[Callable[[], bool]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 queue_max: int = CLIENT_QUEUE_MAX,
                 grab_ttl: float = GRAB_TTL_S,
                 heartbeat: float = HEARTBEAT_S) -> None:
        self._log = log or _default_log
        self.root = os.path.abspath(root or DEFAULT_ROOT)
        self.host = _safe_host(host, self._log)
        self._port = int(port)
        self._device = device
        self.queue_max = max(1, int(queue_max))
        self.grab_ttl = float(grab_ttl)
        self.heartbeat = float(heartbeat)

        self._lock = threading.Lock()          # guards _clients / _next_id
        self._clients: Dict[int, _Client] = {}
        self._next_id = 0
        self._srv: Optional[_BridgeServer] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

    # ---- lifecycle -------------------------------------------------------- #

    def start(self) -> bool:
        """Bind and start serving. False if the port is unusable."""
        if self._srv is not None:
            return True
        self._stop_evt.clear()
        try:
            srv = _BridgeServer((self.host, self._port), _Handler, self)
        except OSError as exc:
            self._log("web bridge: cannot bind %s:%d: %s"
                      % (self.host, self._port, exc))
            return False
        self._srv = srv
        self._port = srv.server_address[1]
        t = threading.Thread(target=self._serve, name="web-bridge", daemon=True)
        t.start()
        self._thread = t
        self._log("web bridge on %s (game: %s)" % (self.url, self.root))
        if not os.path.isdir(self.root):
            self._log("web bridge: %s does not exist yet -- serving the "
                      "built-in button tester at /" % self.root)
        return True

    def _serve(self) -> None:
        try:
            self._srv.serve_forever(poll_interval=0.2)   # type: ignore[union-attr]
        except Exception as exc:                          # pragma: no cover
            if not self._stop_evt.is_set():
                self._log("web bridge: serve loop died: %s" % exc)

    def stop(self) -> None:
        """Close the listener and unblock every stream. Idempotent."""
        already = self._stop_evt.is_set()
        self._stop_evt.set()
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for c in clients:                       # release grabs immediately
            c.alive = False
            self._offer(c, _CLOSE)
        srv, self._srv = self._srv, None
        if srv is not None:
            try:
                srv.shutdown()                  # returns once serve_forever exits
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass
            self._log("web bridge stopped")
        elif not already:
            self._log("web bridge stopped")
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)

    def stopping(self) -> bool:
        return self._stop_evt.is_set()

    # ---- properties ------------------------------------------------------- #

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        host = "[%s]" % self.host if ":" in self.host else self.host
        return "http://%s:%d/" % (host, self._port)

    # ---- the device side -------------------------------------------------- #

    def push(self, code: str) -> None:
        """Fan a button code out to every connected page. NEVER blocks.

        Called on the ButtonReader thread, which is the device's only path to
        the daemon: a stalled browser tab that stopped draining its queue must
        cost a press, not the link. Overflow therefore drops the OLDEST event
        (a game wants the press you just made, not the one from ten seconds
        ago) and moves on.
        """
        if not isinstance(code, str):
            return
        code = code.strip()[:8]
        if not code or not all(32 <= ord(ch) < 127 for ch in code):
            return
        with self._lock:
            targets = list(self._clients.values())
        if not targets:
            return
        payload = json.dumps({"btn": code}, separators=(",", ":"))
        for c in targets:
            if c.alive:
                self._offer(c, payload)

    def _offer(self, client: _Client, item: Any) -> None:
        try:
            client.q.put_nowait(item)
            return
        except queue.Full:
            pass
        try:                                    # drop oldest, then retry once
            client.q.get_nowait()
            client.dropped += 1
        except queue.Empty:
            pass
        try:
            client.q.put_nowait(item)
        except queue.Full:                      # racing consumer refilled it
            client.dropped += 1

    def grabbed(self) -> bool:
        """Is a page currently driving the buttons?

        The daemon calls this on every press, so it is a lock + a walk of at
        most a handful of clients, and it can never raise.
        """
        with self._lock:
            return any(c.grab for c in self._live_locked())

    def connected(self) -> bool:
        with self._lock:
            return bool(self._live_locked())

    def clients(self) -> int:
        with self._lock:
            return len(self._live_locked())

    def device_online(self) -> bool:
        probe = self._device
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:
            return False

    def status(self) -> Dict[str, Any]:
        with self._lock:
            live = self._live_locked()
            connected = bool(live)
            grabbed = any(c.grab for c in live)
            count = len(live)
        # Outside the lock: the probe is the daemon's, with locks of its own.
        return {"connected": connected, "grabbed": grabbed,
                "device": self.device_online(), "clients": count}

    # ---- client registry -------------------------------------------------- #

    def _register(self, grab: bool, peer: str) -> _Client:
        with self._lock:
            self._next_id += 1
            client = _Client(self._next_id, grab, peer, self.queue_max)
            self._clients[client.cid] = client
        self._log("web bridge: page connected from %s (%s)"
                  % (peer, "GRAB -- buttons drive the game" if client.grab
                     else "mirror only"))
        return client

    def _unregister(self, client: _Client) -> None:
        client.alive = False
        with self._lock:
            existed = self._clients.pop(client.cid, None) is not None
        if existed:
            self._log("web bridge: page disconnected from %s%s%s"
                      % (client.peer,
                         " -- grab released" if client.grab else "",
                         " (%d events dropped)" % client.dropped
                         if client.dropped else ""))

    def _live_locked(self) -> List[_Client]:
        """Live clients, reaping the stale. Caller holds _lock.

        A client is stale when nothing has been written to it successfully for
        the hard cap. That is the last line of defence
        for the case the socket cannot tell us about: a tab that stopped
        reading but never closed. Without it a grab could outlive its page and
        the device would sit disconnected from its real job forever.
        """
        now = time.monotonic()
        live: List[_Client] = []
        dead: List[_Client] = []
        for c in self._clients.values():
            if not c.alive or (now - c.opened) > GRAB_MAX_S:
                dead.append(c)
            else:
                live.append(c)
        for c in dead:
            c.alive = False                     # its loop exits on the next tick
            self._clients.pop(c.cid, None)
            self._offer(c, _CLOSE)
        if dead:
            self._log("web bridge: reaped %d stale page(s) -- grab released"
                      % len(dead))
        return live


# --------------------------------------------------------------------------- #
# Standalone: python3 daemon/webbridge.py [--port N] [--root DIR]
# Serves the game and lets you fake presses with `curl -X ... ` -- handy for
# working on the page with no device attached.
# --------------------------------------------------------------------------- #

if __name__ == "__main__":                      # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    a = ap.parse_args()
    b = WebBridge(root=a.root, port=a.port, device=lambda: False)
    if not b.start():
        sys.exit(1)
    print("open %s -- type P/N/G/K/M + Enter here to fake a press, Ctrl-C to "
          "stop" % b.url, file=sys.stderr)
    try:
        for raw in sys.stdin:
            code = raw.strip()
            if code:
                b.push(code)
                print("pushed %s (grabbed=%s)" % (code, b.grabbed()),
                      file=sys.stderr)
    except KeyboardInterrupt:
        pass
    finally:
        b.stop()
