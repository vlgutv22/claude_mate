#!/usr/bin/env python3
"""
Claude Mate daemon
==================

The "brain" of the Claude Mate USB hardware companion. It:

  1. Listens on a Unix-domain socket (CLAUDE_MATE_SOCK) for status lines emitted
     by Claude Code hooks: "<state>|<session_id>|<name>".
  2. Manages a single, continuously-open USB serial connection to the Arduino
     Nano. It auto-detects the port (/dev/cu.usbserial* then /dev/cu.usbmodem*),
     opens it once, keeps it open, and auto-reconnects if it disappears.
  3. Reads button events from the Arduino on a background thread:
        H        -> handshake; the daemon resends the full current state.
        B|1      -> FOCUS the currently displayed session.
        B|2      -> NEXT (advance the carousel, pausing auto-rotation ~10s).
  4. Drives a carousel that rotates through sessions (~3s/step, urgent-first)
     and sends one S card per step.
  5. Computes the overall status word (FREE/WIP/BLOCKED/WTF) and pushes it with
     "D|<WORD>", which tells the Arduino to rotate its stepper-driven status
     wheel to that word by the shortest path.
  6. Focuses a VS Code session via a deep link, with a window-raise fallback.

Only third-party dependency: pyserial.

Run:
    python3 daemon/claude_mate_daemon.py            # real hardware + hooks
    python3 daemon/claude_mate_daemon.py --mock     # demo with fake sessions

Config via environment variables:
    CLAUDE_MATE_PORT   serial device (default: autodetect)
    CLAUDE_MATE_SOCK   socket path   (default: /tmp/claude-mate.sock)
    CLAUDE_MATE_BAUD   serial baud   (default: 115200)
"""

from __future__ import annotations

import argparse
import glob
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover - friendly error if dependency missing
    sys.stderr.write(
        "[claude-mate] ERROR: pyserial is not installed. "
        "Run: pip install -r daemon/requirements.txt\n"
    )
    raise

# --------------------------------------------------------------------------- #
# Configuration / constants
# --------------------------------------------------------------------------- #

# Primary FOCUS deep link. Per VERIFIED FACTS, the documented URI handler is:
#   vscode://anthropic.claude-code/open?session={session_id}&prompt={prompt}
# NOTE: there is no *documented* URI to refocus an *already open* session panel;
# this opens/focuses the Claude chat for the given session. It is isolated here
# as a one-line constant so it is trivial to fix later if the handler changes.
FOCUS_URI_TEMPLATE = "vscode://anthropic.claude-code/open?session={session_id}"

# Default config (overridable via environment).
DEFAULT_SOCK = "/tmp/claude-mate.sock"
DEFAULT_BAUD = 115200

# Serial port autodetect order (globs).
PORT_GLOBS = ("/dev/cu.usbserial*", "/dev/cu.usbmodem*")

# Timings (seconds).
CAROUSEL_PERIOD = 3.0        # rotate one card every ~3s
NEXT_PAUSE = 10.0            # pause auto-rotation ~10s after a manual NEXT
PING_PERIOD = 15.0          # keepalive ping interval
RECONNECT_DELAY = 2.0       # wait between serial (re)connection attempts
SESSION_DONE_TTL = 120.0    # drop a 'done' session after this long with no update
SESSION_IDLE_TTL = 600.0    # drop any stale session after this long

# Card name length cap (the OLED is narrow).
NAME_MAX = 10

# State ordering for the carousel: most urgent first.
STATE_ORDER = {"error": 0, "waiting": 1, "working": 2, "done": 3, "idle": 4}

VALID_STATES = set(STATE_ORDER.keys())


def log(msg: str) -> None:
    """Clean, timestamped logging to stderr."""
    ts = time.strftime("%H:%M:%S")
    sys.stderr.write(f"[claude-mate {ts}] {msg}\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------- #
# Session model + thread-safe registry
# --------------------------------------------------------------------------- #


@dataclass
class Session:
    """One Claude Code session tracked by the daemon."""
    key: str                       # registry key (session_id or name)
    name: str                      # cwd basename, display name
    state: str = "idle"            # working | waiting | error | done | idle
    sid: str = ""                  # session_id (may be empty)
    cwd: str = ""                  # working directory, if known
    last_update_ts: float = field(default_factory=time.time)
    started_ts: Optional[float] = None  # when current 'working' turn began
    last_runtime: float = 0.0      # last completed turn duration (seconds)
    limit: str = "-"               # best-effort rate/usage string; "-" if unknown

    def runtime_seconds(self) -> float:
        """Runtime to display: live while working, else last completed turn."""
        if self.state == "working" and self.started_ts is not None:
            return max(0.0, time.time() - self.started_ts)
        return self.last_runtime


class Registry:
    """Thread-safe collection of sessions keyed by session_id (or name)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, Session] = {}

    def update(self, state: str, sid: str, name: str, cwd: str = "") -> None:
        """Apply a status update from a hook (or mock injector)."""
        if state not in VALID_STATES:
            log(f"ignoring update with invalid state: {state!r}")
            return
        key = sid if sid else (name if name else "unknown")
        now = time.time()
        with self._lock:
            sess = self._sessions.get(key)
            if sess is None:
                sess = Session(key=key, name=name or key)
                self._sessions[key] = sess
            # Update fields.
            if name:
                sess.name = name
            if sid:
                sess.sid = sid
            if cwd:
                sess.cwd = cwd
            prev_state = sess.state
            sess.last_update_ts = now

            # Manage runtime bookkeeping on state transitions.
            if state == "working":
                # Entering / continuing a turn: stamp start once.
                if prev_state != "working" or sess.started_ts is None:
                    sess.started_ts = now
            else:
                # Leaving 'working': record the completed turn duration.
                if prev_state == "working" and sess.started_ts is not None:
                    sess.last_runtime = max(0.0, now - sess.started_ts)
                sess.started_ts = None
            sess.state = state

    def remove(self, key: str) -> None:
        with self._lock:
            self._sessions.pop(key, None)

    def prune(self) -> None:
        """Drop stale/finished sessions so the carousel stays tidy."""
        now = time.time()
        with self._lock:
            dead: List[str] = []
            for key, s in self._sessions.items():
                age = now - s.last_update_ts
                if s.state == "idle" and age > SESSION_IDLE_TTL:
                    dead.append(key)
                elif s.state == "done" and age > SESSION_DONE_TTL:
                    dead.append(key)
                elif age > SESSION_IDLE_TTL:
                    dead.append(key)
            for key in dead:
                self._sessions.pop(key, None)
            if dead:
                log(f"pruned {len(dead)} stale session(s)")

    def ordered(self) -> List[Session]:
        """Return sessions sorted urgent-first, then by name for stability."""
        with self._lock:
            items = list(self._sessions.values())
        items.sort(key=lambda s: (STATE_ORDER.get(s.state, 99), s.name.lower()))
        return items

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def status_word(self) -> str:
        """Compute the overall status word for the wheel per the CONTRACT.

        Mapping (single source of truth), priority WTF > BLOCKED > WIP > FREE:
            WTF     <- at least one session in error
            BLOCKED <- at least one session waiting (Claude needs your input)
            WIP     <- at least one session working
            FREE    <- everything idle/done, or no sessions (also HOME position)
        """
        with self._lock:
            states = [s.state for s in self._sessions.values()]
        # WTF if anything errored (highest priority).
        if any(st == "error" for st in states):
            return "WTF"
        # BLOCKED if anything is waiting for the human.
        if any(st == "waiting" for st in states):
            return "BLOCKED"
        # WIP if anything is actively working.
        if any(st == "working" for st in states):
            return "WIP"
        # FREE otherwise (done/idle/empty).
        return "FREE"


# --------------------------------------------------------------------------- #
# Serial link: open once, keep open, auto-reconnect.
# --------------------------------------------------------------------------- #


class SerialLink:
    """Owns the single serial connection and serializes writes."""

    def __init__(self, port: Optional[str], baud: int) -> None:
        self._configured_port = port  # None => autodetect
        self._baud = baud
        self._ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()  # guards writes + (re)connect

    @staticmethod
    def autodetect() -> Optional[str]:
        """Find a likely Arduino device by glob priority."""
        for pattern in PORT_GLOBS:
            matches = sorted(glob.glob(pattern))
            if matches:
                return matches[0]
        return None

    def _resolve_port(self) -> Optional[str]:
        if self._configured_port:
            return self._configured_port if os.path.exists(self._configured_port) else None
        return self.autodetect()

    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def ensure_open(self) -> bool:
        """Try to (re)open the port. Returns True if open afterwards."""
        with self._lock:
            if self.is_open():
                return True
            port = self._resolve_port()
            if not port:
                return False
            try:
                self._ser = serial.Serial(
                    port=port,
                    baudrate=self._baud,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.5,        # read timeout
                    write_timeout=2.0,
                )
                log(f"serial opened on {port} @ {self._baud} 8N1")
                # Opening the port resets the Nano (~1.5s); it will emit H when
                # ready, prompting a full state resend. We do not block here.
                return True
            except (serial.SerialException, OSError) as exc:
                log(f"serial open failed on {port}: {exc}")
                self._close_locked()
                return False

    def _close_locked(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def write_line(self, line: str) -> bool:
        """Write one newline-terminated ASCII line. Returns success."""
        data = (line.rstrip("\n") + "\n").encode("ascii", errors="replace")
        with self._lock:
            if not self.is_open():
                return False
            try:
                self._ser.write(data)
                self._ser.flush()
                return True
            except (serial.SerialException, OSError) as exc:
                log(f"serial write failed ({line!r}): {exc}")
                self._close_locked()  # mark dead -> reconnect loop will recover
                return False

    def read_line(self) -> Optional[str]:
        """Read one line (blocking up to the read timeout). None on no-data/err."""
        # Note: we hold the lock only to grab the handle; readline itself can
        # block for the timeout, but writes use short bursts so contention is low.
        ser = self._ser
        if ser is None or not ser.is_open:
            return None
        try:
            raw = ser.readline()
        except (serial.SerialException, OSError) as exc:
            log(f"serial read failed: {exc}")
            self.close()
            return None
        if not raw:
            return None
        try:
            return raw.decode("ascii", errors="replace").strip()
        except Exception:
            return None


# --------------------------------------------------------------------------- #
# Display controller: builds protocol lines and tracks the current card.
# --------------------------------------------------------------------------- #


class Display:
    """Translates registry state into serial protocol lines."""

    def __init__(self, link: SerialLink, registry: Registry) -> None:
        self._link = link
        self._reg = registry
        self._lock = threading.Lock()
        self._last_word: Optional[str] = None
        self._current_index = 0          # carousel position within ordered list
        self._current_session: Optional[Session] = None  # last shown card's session

    # ---- dial (stepper status wheel) ------------------------------------- #

    def push_dial(self, force: bool = False) -> None:
        """Send "D|<WORD>" so the Arduino rotates the wheel; only on change."""
        word = self._reg.status_word()
        with self._lock:
            changed = (word != self._last_word)
            self._last_word = word
        if changed or force:
            self._link.write_line(f"D|{word}")

    # ---- cards ----------------------------------------------------------- #

    @staticmethod
    def _fmt_runtime(seconds: float) -> str:
        """Format seconds as mm:ss, or h:mm for >= 1 hour."""
        seconds = int(max(0, seconds))
        if seconds >= 3600:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            return f"{h}:{m:02d}"
        m = seconds // 60
        s = seconds % 60
        return f"{m:02d}:{s:02d}"

    def _send_card(self, sess: Session, idx: int, total: int) -> None:
        name = (sess.name or "?")[:NAME_MAX]
        # Sanitize: the | char and newlines would corrupt the protocol.
        name = name.replace("|", "/").replace("\n", " ").strip() or "?"
        runtime = self._fmt_runtime(sess.runtime_seconds())
        limit = (sess.limit or "-").replace("|", "/")[:6] or "-"
        line = f"S|{idx}|{total}|{name}|{sess.state}|{runtime}|{limit}"
        self._link.write_line(line)

    def show_index(self, index: int) -> None:
        """Show the card at ordered position `index` (wraps). Idle if empty."""
        sessions = self._reg.ordered()
        total = len(sessions)
        if total == 0:
            with self._lock:
                self._current_index = 0
                self._current_session = None
            self._link.write_line("I")
            self._link.write_line("D|FREE")
            return
        idx = index % total
        sess = sessions[idx]
        with self._lock:
            self._current_index = idx
            self._current_session = sess
        self._send_card(sess, idx + 1, total)

    def advance(self) -> None:
        """Move to the next card (used by carousel and NEXT button)."""
        with self._lock:
            nxt = self._current_index + 1
        self.show_index(nxt)

    def refresh_current(self) -> None:
        """Re-render whatever card is current (e.g. to update live runtime)."""
        with self._lock:
            idx = self._current_index
        self.show_index(idx)

    def resend_full_state(self) -> None:
        """On handshake (H): push dial (force) + current card from scratch."""
        log("handshake H -> resending full state")
        self.push_dial(force=True)
        self.refresh_current()

    def current_session(self) -> Optional[Session]:
        with self._lock:
            return self._current_session


# --------------------------------------------------------------------------- #
# FOCUS action
# --------------------------------------------------------------------------- #


def focus_session(sess: Optional[Session]) -> None:
    """
    Focus the VS Code session of the given card.

    Primary: open the documented deep link via macOS `open`.
    Fallback: raise the VS Code window for the workspace folder (open -a / code),
    used when the session id is unknown/stale or the primary fails.
    """
    if sess is None:
        log("FOCUS pressed but no current session")
        return

    log(f"FOCUS -> {sess.name} (sid={sess.sid or '?'}, cwd={sess.cwd or '?'})")

    # --- Primary: deep link -------------------------------------------------
    if sess.sid:
        uri = FOCUS_URI_TEMPLATE.format(
            session_id=urllib.parse.quote(sess.sid, safe="")
        )
        try:
            rc = subprocess.run(
                ["open", uri],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).returncode
            if rc == 0:
                return  # success
            log(f"deep-link open returned {rc}; using fallback")
        except Exception as exc:
            log(f"deep-link open failed: {exc}; using fallback")

    # --- Fallback: raise the VS Code window for the workspace ---------------
    _focus_fallback(sess)


def _focus_fallback(sess: Session) -> None:
    """Raise the VS Code window (best effort). Never raises."""
    cwd = sess.cwd
    # Prefer `code <cwd>` if the CLI is on PATH (focuses an existing window).
    if cwd and _which("code"):
        try:
            subprocess.run(
                ["code", cwd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return
        except Exception as exc:
            log(f"`code {cwd}` failed: {exc}")

    if cwd:
        try:
            subprocess.run(
                ["open", "-a", "Visual Studio Code", cwd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return
        except Exception as exc:
            log(f"`open -a VS Code {cwd}` failed: {exc}")

    # Last resort: just activate VS Code via AppleScript.
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Visual Studio Code" to activate'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception as exc:
        log(f"AppleScript activate failed: {exc}")


def _which(prog: str) -> bool:
    for p in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(p, prog)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return True
    return False


# --------------------------------------------------------------------------- #
# Background threads
# --------------------------------------------------------------------------- #


class SocketServer(threading.Thread):
    """Unix-domain socket server: parses '<state>|<session_id>|<name>' lines."""

    def __init__(self, sock_path: str, registry: Registry, on_update) -> None:
        super().__init__(name="socket-server", daemon=True)
        self._sock_path = sock_path
        self._reg = registry
        self._on_update = on_update
        self._stop = threading.Event()
        self._srv: Optional[socket.socket] = None

    def run(self) -> None:
        # Remove a stale socket file if present.
        try:
            if os.path.exists(self._sock_path):
                os.unlink(self._sock_path)
        except OSError as exc:
            log(f"could not remove stale socket {self._sock_path}: {exc}")

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(self._sock_path)
            os.chmod(self._sock_path, 0o666)  # let hooks (any user session) write
            srv.listen(16)
            srv.settimeout(0.5)
        except OSError as exc:
            log(f"FATAL: cannot bind socket {self._sock_path}: {exc}")
            return
        self._srv = srv
        log(f"socket listening on {self._sock_path}")

        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                self._handle_conn(conn)

        try:
            srv.close()
        finally:
            try:
                os.unlink(self._sock_path)
            except OSError:
                pass

    def _handle_conn(self, conn: socket.socket) -> None:
        conn.settimeout(1.0)
        buf = b""
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._process_line(line)
            # Process any trailing line without a newline.
            if buf:
                self._process_line(buf)
        except OSError:
            pass

    def _process_line(self, raw: bytes) -> None:
        try:
            line = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            return
        if not line:
            return
        # Expected: "<state>|<session_id>|<name>" ; tolerate extra trailing fields.
        parts = line.split("|")
        state = parts[0].strip() if len(parts) > 0 else ""
        sid = parts[1].strip() if len(parts) > 1 else ""
        name = parts[2].strip() if len(parts) > 2 else ""
        cwd = parts[3].strip() if len(parts) > 3 else ""  # optional extension
        if not state:
            log(f"ignoring malformed socket line: {line!r}")
            return
        if cwd and not name:
            name = os.path.basename(cwd.rstrip("/"))
        log(f"socket update: state={state} sid={sid or '-'} name={name or '-'}")
        self._reg.update(state, sid, name, cwd)
        self._on_update()

    def stop(self) -> None:
        self._stop.set()


class ButtonReader(threading.Thread):
    """Reads serial input and dispatches H / B|<n> events."""

    def __init__(self, link: SerialLink, display: Display) -> None:
        super().__init__(name="button-reader", daemon=True)
        self._link = link
        self._display = display
        self._stop = threading.Event()
        self.on_next = None  # callback set by the app (carousel pause)

    def run(self) -> None:
        while not self._stop.is_set():
            if not self._link.is_open():
                time.sleep(0.2)
                continue
            line = self._link.read_line()
            if not line:
                continue
            self._dispatch(line)

    def _dispatch(self, line: str) -> None:
        # Tolerate garbled / partial lines: only act on exact, known shapes.
        if line == "H":
            self._display.resend_full_state()
            return
        if line.startswith("B|"):
            parts = line.split("|")
            if len(parts) >= 2 and parts[1].isdigit():
                n = int(parts[1])
                if n == 1:
                    focus_session(self._display.current_session())
                elif n == 2:
                    log("NEXT button -> advancing carousel")
                    self._display.advance()
                    if self.on_next:
                        self.on_next()
                else:
                    log(f"unknown button index: {n}")
            return
        # Anything else: log at most as debug noise, ignore.
        log(f"ignoring serial line from Arduino: {line!r}")

    def stop(self) -> None:
        self._stop.set()


class SerialMaintainer(threading.Thread):
    """Keeps the serial port open; reconnects when it drops; sends pings."""

    def __init__(self, link: SerialLink, display: Display) -> None:
        super().__init__(name="serial-maintainer", daemon=True)
        self._link = link
        self._display = display
        self._stop = threading.Event()
        self._last_ping = 0.0

    def run(self) -> None:
        was_open = False
        while not self._stop.is_set():
            if not self._link.is_open():
                if was_open:
                    log("serial disconnected; will reconnect")
                    was_open = False
                if self._link.ensure_open():
                    was_open = True
                    # Give the Nano time to reset; it will send H which triggers
                    # a full resend. We also push state proactively as a safety net.
                    time.sleep(2.0)
                    self._display.resend_full_state()
                else:
                    self._stop.wait(RECONNECT_DELAY)
                    continue
            # Periodic keepalive ping.
            now = time.time()
            if now - self._last_ping >= PING_PERIOD:
                self._link.write_line("P")
                self._last_ping = now
            self._stop.wait(1.0)

    def stop(self) -> None:
        self._stop.set()


class Carousel(threading.Thread):
    """Rotates through cards ~every 3s and keeps the live runtime fresh."""

    def __init__(self, display: Display, registry: Registry) -> None:
        super().__init__(name="carousel", daemon=True)
        self._display = display
        self._reg = registry
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._paused_until = 0.0
        self._last_rotate = 0.0
        self._last_prune = 0.0

    def pause(self) -> None:
        """Pause auto-rotation for NEXT_PAUSE seconds (after a manual NEXT)."""
        with self._lock:
            self._paused_until = time.time() + NEXT_PAUSE

    def notify_change(self) -> None:
        """Called on any registry change: refresh dial + current card."""
        self._display.push_dial()
        self._display.refresh_current()

    def run(self) -> None:
        # Prime the display.
        self._display.show_index(0)
        self._display.push_dial(force=True)
        while not self._stop.is_set():
            now = time.time()

            # Periodic pruning of stale sessions.
            if now - self._last_prune >= 5.0:
                self._reg.prune()
                self._last_prune = now

            with self._lock:
                paused = now < self._paused_until

            if paused:
                # While paused, still keep the current card's runtime ticking.
                self._display.refresh_current()
                self._display.push_dial()
            else:
                if now - self._last_rotate >= CAROUSEL_PERIOD:
                    self._display.advance()
                    self._last_rotate = now
                else:
                    # Between rotations, refresh runtime/dial without advancing.
                    self._display.refresh_current()
                    self._display.push_dial()

            self._stop.wait(1.0)

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
# Mock injector (--mock)
# --------------------------------------------------------------------------- #


class MockInjector(threading.Thread):
    """Injects a few fake sessions cycling through states for demos."""

    def __init__(self, registry: Registry, on_update) -> None:
        super().__init__(name="mock-injector", daemon=True)
        self._reg = registry
        self._on_update = on_update
        self._stop = threading.Event()

    def run(self) -> None:
        # Three to four fake sessions in different working dirs.
        fakes = [
            ("sid-aaa", "webapp", "/Users/demo/webapp"),
            ("sid-bbb", "api", "/Users/demo/api"),
            ("sid-ccc", "infra", "/Users/demo/infra"),
            ("sid-ddd", "notes", "/Users/demo/notes"),
        ]
        # Cycle covers every state so the wheel visits all four words over time:
        #   working -> WIP, waiting -> BLOCKED, error -> WTF, done/idle -> FREE.
        cycle = ["working", "waiting", "error", "done", "idle"]
        log("MOCK mode: injecting fake sessions")
        # Seed initial states. These four exercise all four words at once
        # (WTF via 'error' > BLOCKED via 'waiting' > WIP via 'working' > FREE),
        # and the 'waiting' seed guarantees BLOCKED is reachable.
        seeds = ["working", "waiting", "error", "done"]
        for (sid, name, cwd), st in zip(fakes, seeds):
            self._reg.update(st, sid, name, cwd)
            # Set a fake limit on one of them to exercise the field.
        with self._reg._lock:  # set a demo limit string directly
            for s in self._reg._sessions.values():
                if s.name == "webapp":
                    s.limit = "71%"
        self._on_update()

        step = 0
        while not self._stop.is_set():
            # Advance one session's state every ~4s so the light/carousel move.
            sid, name, cwd = fakes[step % len(fakes)]
            new_state = cycle[(step // len(fakes)) % len(cycle)]
            self._reg.update(new_state, sid, name, cwd)
            self._on_update()
            step += 1
            self._stop.wait(4.0)

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
# Application wiring
# --------------------------------------------------------------------------- #


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Claude Mate daemon: bridges Claude Code hooks to the "
                    "Arduino status-wheel/OLED companion over USB serial.",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="inject fake sessions cycling through states for a no-Claude demo",
    )
    parser.add_argument(
        "--port",
        default=os.environ.get("CLAUDE_MATE_PORT") or None,
        help="serial device (default: autodetect /dev/cu.usbserial* then usbmodem*)",
    )
    parser.add_argument(
        "--sock",
        default=os.environ.get("CLAUDE_MATE_SOCK", DEFAULT_SOCK),
        help=f"Unix socket path (default: {DEFAULT_SOCK})",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=int(os.environ.get("CLAUDE_MATE_BAUD", DEFAULT_BAUD)),
        help=f"serial baud rate (default: {DEFAULT_BAUD})",
    )
    args = parser.parse_args(argv)

    log("starting Claude Mate daemon")
    log(f"  socket : {args.sock}")
    log(f"  port   : {args.port or 'autodetect'}")
    log(f"  baud   : {args.baud}")
    log(f"  mock   : {args.mock}")

    registry = Registry()
    link = SerialLink(args.port, args.baud)
    display = Display(link, registry)

    # The carousel owns "on change" behavior; the socket server and mock
    # injector call this whenever the registry mutates.
    carousel = Carousel(display, registry)

    def on_update() -> None:
        carousel.notify_change()

    socket_server = SocketServer(args.sock, registry, on_update)
    button_reader = ButtonReader(link, display)
    button_reader.on_next = carousel.pause
    maintainer = SerialMaintainer(link, display)

    threads: List[threading.Thread] = [
        socket_server,
        maintainer,
        button_reader,
        carousel,
    ]

    mock = None
    if args.mock:
        mock = MockInjector(registry, on_update)
        threads.append(mock)

    # Open the serial port up front (non-fatal if absent; maintainer retries).
    if not link.ensure_open():
        log("serial not available yet; will keep trying in the background")

    for t in threads:
        t.start()

    log("running. Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        log("shutting down...")
    finally:
        for t in threads:
            stop = getattr(t, "stop", None)
            if callable(stop):
                stop()
        # Best-effort: blank the display by sending idle (also homes the wheel).
        try:
            link.write_line("I")
            link.write_line("D|FREE")
        except Exception:
            pass
        link.close()
        # Give daemon threads a brief moment to wind down.
        time.sleep(0.3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
