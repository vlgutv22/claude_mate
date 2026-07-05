#!/usr/bin/env python3
"""
Claude Mate daemon
==================

The "brain" of the Claude Mate USB hardware companion. It:

  1. Listens on a Unix-domain socket (CLAUDE_MATE_SOCK) for status lines emitted
     by Claude Code hooks / the PTY wrapper: "<state>|<session_id>|<name>[|...]".
  2. Manages a single, continuously-open USB serial connection to the Arduino
     Nano. It auto-detects the port (/dev/cu.usbserial* then /dev/cu.usbmodem*),
     opens it once, keeps it open, and auto-reconnects if it disappears.
  3. Keeps ONE urgency-sorted triage queue of sessions
     (error > waiting > done > working > idle; unacknowledged before
     acknowledged inside a class; oldest event first) and renders ONE screen --
     the selected session (normally the queue head, i.e. the thing that needs
     the human most) as a pre-composed 4-row (all size-1) text frame:

         F|<flash>|<name>|<state+time>|<model+effort>|<pos + fleet letters>

     The firmware is a dumb renderer; ALL layout/ordering/selection lives here.
  4. Reads button events from the Arduino on a background thread. The buttons
     mean the same thing at all times (no modes):
        H     -> handshake; the daemon resends the full current state.
        B|P   -> PREV: selection one step up the queue (auto-repeats on hold).
        B|N   -> NEXT: selection one step down the queue (auto-repeats on hold).
        B|G   -> GO (short press): RAISE the terminal of the session SHOWN on
                 the glass (WYSIWYG -- always the name the user is looking at).
                 Raise/activate ONLY; the daemon never collapses, resizes, or
                 miniaturizes any window. An alert is acknowledged as it is
                 raised.
        B|K   -> GO (long press): acknowledge the shown session's alert WITHOUT
                 touching any window.
     After acknowledging an ALERT the selection snaps home to the next alert
     (N alerts = N presses); focusing a CALM session keeps it on the glass.
  5. Drives the indication LED via V|<KIND>: the pattern for the WORST
     unacknowledged alert class, looping until acknowledged (V|OFF).

Screen ownership rule: the display changes subject on its own ONLY when the
user is idle (no button press for HOME_AFTER_S). A GO/ACK acts on EXACTLY the
session whose frame is on the glass -- never a freshly recomputed head -- so a
press can only ever raise the terminal the user is actually looking at.

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
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

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

# Primary FOCUS deep link (hook-driven VS Code sessions). See focus_session().
FOCUS_URI_TEMPLATE = "vscode://anthropic.claude-code/open?session={session_id}"

# Default config (overridable via environment).
DEFAULT_SOCK = "/tmp/claude-mate.sock"
DEFAULT_BAUD = 115200

# Serial port autodetect order (globs).
PORT_GLOBS = ("/dev/cu.usbserial*", "/dev/cu.usbmodem*")

# Timings (seconds).
HOME_AFTER_S = 10.0          # no press for this long -> selection returns to
                             # the queue head; until then the screen NEVER
                             # changes subject on its own
REALERT_SUPPRESS_S = 5.0     # a session re-entering the SAME alert class this
                             # soon after being acknowledged stays acknowledged
                             # (absorbs detection flaps re-firing the LED)
WRAPPER_LIVE_TIMEOUT_S = 1.0  # a live wrapper acks receipt ('go') within ms; a
                              # wedged one (stopped process: the kernel accepts
                              # the connect into the backlog and buffers the
                              # send) costs only this per focus, not the
                              # full completion deadline
WRAPPER_ACK_TIMEOUT_S = 12.0  # overall deadline for the completion ack ('ok');
                              # must exceed the wrapper's worst-case focus op
                              # (~10s: tmux select-window/pane + osascript).
                              # Pre-ack wrappers close the socket at once (EOF),
                              # so the wait degrades to fire-and-forget.
PING_PERIOD = 15.0          # keepalive ping interval
RECONNECT_DELAY = 2.0       # wait between serial (re)connection attempts
SESSION_DONE_TTL = 120.0    # drop a 'done' session after this long with no update
SESSION_IDLE_TTL = 600.0    # drop any stale session after this long

# Screen text geometry (must match the firmware's fixed 4-row layout, all
# size-1: r0 name / r1 state+time / r2 model+effort / r3 position + fleet).
ROW_CHARS = 21              # size-1 rows are 21 chars wide (128 / 6px)

# 4-char state tag shown on the state row.
STATE_TAG = {"working": "WORK", "waiting": "WAIT", "error": "ERR",
             "done": "DONE", "idle": "IDLE"}

# One LETTER per session in the fleet strip (queue order), '|'-separated:
#   I idle  E error  W working (WIP)  D done  B waiting (blocked, needs input)
STATE_LETTER = {"error": "E", "waiting": "B", "working": "W",
                "done": "D", "idle": "I"}

# Triage priority, most urgent first. The queue sorts by
# (class rank, acknowledged?, event time): a fresh error outranks everything,
# unacknowledged before acknowledged inside a class, oldest first (FIFO triage).
STATE_ORDER = {"error": 0, "waiting": 1, "done": 2, "working": 3, "idle": 4}

VALID_STATES = set(STATE_ORDER.keys())

# Alert states nag the human until acknowledged (GO/ACK) or the state changes.
ALERT_STATES = {"error", "waiting", "done"}

# One-shot LED kind on a fresh transition INTO each alert state.
ALERT_KIND = {"error": "ERROR", "waiting": "INPUT", "done": "DONE"}

# Every "until acknowledged" alert plays as a CONTINUOUS firmware LED loop with
# its own distinct rhythm. The firmware repeats the pattern until V|OFF (sent
# when the last unacknowledged member of the class is acknowledged/cleared).
LOOP_KIND = {"error": "ERROR", "waiting": "INPUT", "done": "DONE"}

# Sentinel for "we have not told the firmware a loop state yet", so the first
# resolve always emits (V|OFF) and clears any stale loop left by a prior daemon.
_LOOP_UNSET = "\x00unset"


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
    state_since: float = field(default_factory=time.time)  # when current state began
    model: str = ""                # model in use, e.g. "Opus 4.8" (PTY wrapper)
    effort: str = ""               # effort level, e.g. "xhigh" (PTY wrapper)
    focus_ctrl: str = ""           # PTY-wrapper control socket for FOCUS (if any)
    acked: bool = True             # alert (done/waiting/error) seen by the human?
    last_ack_state: str = ""       # alert class most recently acknowledged...
    last_ack_ts: float = 0.0       # ...and when (re-alert flap suppression)

    def display_seconds(self) -> float:
        """The time shown on the info row: time in the current state.

        For 'working' that IS the live turn runtime; for an alert state it is
        the triage-critical number -- how long this has been waiting on the
        human; for 'idle' how long it has been sitting there.
        """
        return max(0.0, time.time() - self.state_since)

    def unacked_alert(self) -> bool:
        return self.state in ALERT_STATES and not self.acked


class Registry:
    """Thread-safe collection of sessions keyed by session_id (or name)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, Session] = {}

    def update(self, state: str, sid: str, name: str, cwd: str = "",
               focus_ctrl: str = "", model: str = "",
               effort: str = "") -> Optional[str]:
        """Apply a status update from a hook, the PTY wrapper, or the mock injector.

        Returns the one-shot LED KIND for this session's own transition
        (START/DONE/INPUT/ERROR), or None for a keepalive / silent change.

        Models "finished but not yet acknowledged": a turn ending (working ->
        idle) becomes 'done' and STAYS 'done' (alerting) until the user
        acknowledges it; later idle keepalives must not clear it.
        """
        if state not in VALID_STATES:
            log(f"ignoring update with invalid state: {state!r}")
            return None
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
            if focus_ctrl:
                sess.focus_ctrl = focus_ctrl
            if model:
                sess.model = model
            if effort:
                sess.effort = effort
            prev_state = sess.state
            sess.last_update_ts = now

            # Resolve the EFFECTIVE state under the done-until-acknowledged model.
            eff = state
            if state == "idle":
                if prev_state == "working":
                    eff = "done"                 # just finished -> needs ack
                elif prev_state == "done" and not sess.acked:
                    eff = "done"                 # keepalive while unacknowledged

            changed = (eff != prev_state)
            if changed:
                sess.state_since = now           # live "time in state" anchor
            sess.state = eff

            # Acknowledgment + one-shot LED kind on a real transition.
            haptic: Optional[str] = None
            if changed:
                if eff in ALERT_STATES:          # done/waiting/error: fresh alert
                    # Flap suppression: re-entering the SAME alert class right
                    # after the human acknowledged it stays acknowledged, so a
                    # bouncing detector can't re-fire the LED he just silenced.
                    if (sess.last_ack_state == eff
                            and (now - sess.last_ack_ts) < REALERT_SUPPRESS_S):
                        sess.acked = True
                    else:
                        sess.acked = False       # nag until acknowledged
                        haptic = ALERT_KIND[eff]
                elif eff == "working":
                    sess.acked = True
                    sess.last_ack_state = ""     # real forward progress: a
                    sess.last_ack_ts = 0.0       # future alert is NEW, not a flap
                    haptic = "START"             # job (re)started: calm tick
                else:                            # idle
                    sess.acked = True
            return haptic

    def remove(self, key: str) -> None:
        with self._lock:
            self._sessions.pop(key, None)

    def acknowledge(self, sess: Optional["Session"]) -> None:
        """GO/ACK pressed: mark this session's alert as seen. A finished (done)
        session becomes idle; a waiting/error session is silenced but keeps its
        state (it still shows, just stops nagging) until it changes on its own."""
        if sess is None:
            return
        with self._lock:
            if sess.state in ALERT_STATES:
                sess.last_ack_state = sess.state
                sess.last_ack_ts = time.time()
            sess.acked = True
            if sess.state == "done":
                sess.state = "idle"
                sess.state_since = time.time()

    def top_alert(self) -> Optional["Session"]:
        """The most urgent unacknowledged session needing the human (error >
        waiting > done), or None. Drives the LED loop class."""
        items = [s for s in self.queue() if s.unacked_alert()]
        return items[0] if items else None

    def prune(self) -> None:
        """Drop stale/finished sessions so the queue stays tidy."""
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

    def queue(self) -> List[Session]:
        """The triage queue: every UNacknowledged alert first (by urgency
        class, oldest first -- these are what still need the human), then
        everything else by class. This keeps the invariant that after each
        GO/ACK the queue head IS the next thing the LED is blinking about;
        an already-acknowledged error never hides a fresh waiting alert."""
        with self._lock:
            items = list(self._sessions.values())
        items.sort(key=lambda s: (0 if s.unacked_alert() else 1,
                                  STATE_ORDER.get(s.state, 99),
                                  s.state_since,
                                  s.name.lower()))
        return items

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


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
                # No flush(): tcdrain has no timeout bound, and a wedged port
                # would stall every caller holding the Screen lock. write() is
                # bounded by write_timeout and these lines are tiny; the kernel
                # drains them at line rate.
                self._ser.write(data)
                return True
            except (serial.SerialException, OSError) as exc:
                log(f"serial write failed ({line!r}): {exc}")
                self._close_locked()  # mark dead -> reconnect loop will recover
                return False

    def read_line(self) -> Optional[str]:
        """Read one line (blocking up to the read timeout). None on no-data/err."""
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
# Screen: composes the single frame and owns selection + LED state.
# --------------------------------------------------------------------------- #


def _fmt_time(seconds: float) -> str:
    """Format seconds as mm:ss, or h:mm for >= 1 hour. Always <= 5 chars."""
    seconds = int(max(0, seconds))
    if seconds >= 3600:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}:{m:02d}"
    m = seconds // 60
    s = seconds % 60
    return f"{m:02d}:{s:02d}"


def _sanitize(text: str) -> str:
    """Strip protocol-corrupting characters from a display string: the field
    separator and ALL C0 control chars + DEL (an embedded NUL or newline would
    otherwise wedge the firmware tokenizer on every frame for that subject).
    Non-ASCII is left to write_line's ascii/replace ('?'), which is 1-for-1 so
    field counts and length budgets hold."""
    text = (text or "").replace("|", "/")
    return "".join(ch if (ch >= " " and ch != "\x7f") else " " for ch in text)


def _fit_meta(model: str, effort: str, width: int) -> str:
    """Best-fit 'model effort' into `width` chars, degrading gracefully:
    full model+effort -> first-word model+effort -> effort -> model, truncated."""
    model = _sanitize(model).strip()
    effort = _sanitize(effort).strip()
    cands: List[str] = []
    if model and effort:
        cands = [f"{model} {effort}", f"{model.split()[0]} {effort}", effort]
    elif model:
        cands = [model, model.split()[0]]
    elif effort:
        cands = [effort]
    for c in cands:
        if len(c) <= width:
            return c
    return cands[-1][:width] if cands else ""


def _display_names(sessions: List[Session]) -> Dict[str, str]:
    """Map session key -> display name truncated to ROW_CHARS (the full size-1
    row now), disambiguating collisions (sibling dirs with a long common prefix)
    with a middle squeeze: first 9 chars + '~' + last 10 chars."""
    out: Dict[str, str] = {}
    plain: Dict[str, List[Session]] = {}
    for s in sessions:
        nm = _sanitize(s.name).strip() or "?"
        t = nm[:ROW_CHARS]
        plain.setdefault(t, []).append(s)
        out[s.key] = t
    for t, group in plain.items():
        if len(group) < 2:
            continue
        # Only a real collision if the FULL names differ (identical basenames
        # cannot be disambiguated by truncation at all).
        fulls = {g.name for g in group}
        if len(fulls) < 2:
            continue
        for g in group:
            nm = _sanitize(g.name).strip()
            if len(nm) > ROW_CHARS:
                out[g.key] = nm[:9] + "~" + nm[-10:]
    return out


class Screen:
    """Owns everything the device shows: the selection, the pre-rendered frame,
    and the LED loop tracker."""

    def __init__(self, link: SerialLink, registry: Registry) -> None:
        self._link = link
        self._reg = registry
        self._lock = threading.Lock()
        # Selection: a session KEY, or None meaning "the queue head". Tracked
        # by key so re-sorts never move the subject out from under the cursor.
        self._sel_key: Optional[str] = None
        self._last_press = 0.0
        # Last frame actually sent (dedup) + the key of the subject CURRENTLY
        # ON THE GLASS. A GO/ACK press acts on exactly this key (WYSIWYG), so
        # the raised terminal always matches the name the user is looking at.
        self._last_frame = ""
        self._shown_key: Optional[str] = None
        # LED loop tracker: what continuous loop the firmware is playing
        # ("ERROR"/"INPUT"/"DONE"/None); V| is (re)sent only when it changes.
        self._loop_kind: Optional[str] = _LOOP_UNSET

    # ---- selection -------------------------------------------------------- #

    def _interacting(self, now: float) -> bool:
        return (now - self._last_press) < HOME_AFTER_S

    def _subject(self, queue: List[Session]) -> Optional[Session]:
        """The session the screen shows: the selected key if it still exists,
        else the queue head."""
        if not queue:
            return None
        key = self._sel_key
        if key is not None:
            for s in queue:
                if s.key == key:
                    return s
        return queue[0]

    def nav(self, delta: int) -> None:
        """PREV/NEXT: move the selection by `delta` in queue order (wraps)."""
        now = time.time()
        with self._lock:
            queue = self._reg.queue()
            self._last_press = now
            if not queue:
                return
            cur = self._subject(queue)
            idx = 0
            for i, s in enumerate(queue):
                if cur is not None and s.key == cur.key:
                    idx = i
                    break
            nxt = queue[(idx + delta) % len(queue)]
            self._sel_key = nxt.key
        self.refresh()

    def snap_home(self) -> None:
        """After acknowledging an ALERT: surface the (new) queue head and PIN
        it, so the next alert to triage is on the glass and the screen does not
        drift on its own inside the interaction window."""
        with self._lock:
            queue = self._reg.queue()
            self._sel_key = queue[0].key if queue else None
        self.refresh()

    def stay_on(self, sess: Optional[Session]) -> None:
        """After focusing a CALM session (nothing to triage): keep it on the
        glass instead of jumping to some alert elsewhere -- the user asked to
        see this terminal, so the device stays on it."""
        if sess is None:
            return
        with self._lock:
            self._sel_key = sess.key
            self._last_press = time.time()
        self.refresh()

    def resolve_press_target(self) -> Optional[Session]:
        """The session a GO/ACK press applies to: EXACTLY the one whose frame is
        on the glass (WYSIWYG). Never a freshly recomputed head -- so a press
        can only ever act on the name the user is actually looking at. Falls
        back to the queue head only if the shown session has vanished. Also
        counts this press for the interaction window."""
        now = time.time()
        with self._lock:
            queue = self._reg.queue()
            self._last_press = now
            if self._shown_key is not None:
                for s in queue:
                    if s.key == self._shown_key:
                        return s
            return self._subject(queue)         # shown session gone -> head

    # ---- frame composition -------------------------------------------------- #

    def _compose(self, queue: List[Session],
                 subject: Optional[Session]) -> Tuple[str, Optional[str], bool]:
        """Build the F| line for the current state as four size-1 rows:
            r0  session name (flashes while its alert is unacknowledged)
            r1  state tag + time-in-state
            r2  model + effort
            r3  position + fleet strip (one '|'-separated status letter/session)
        Returns (line, subject_key, flash). The fleet field is LAST and may
        contain literal '|' (the firmware stops tokenizing at the 5th bar and
        takes the rest verbatim), which is what lets the strip use '|' as its
        visual divider."""
        if subject is None:
            return ("F|0|MATE|no sessions||", None, False)

        names = _display_names(queue)
        r0 = names.get(subject.key, "?")[:ROW_CHARS]

        tag = STATE_TAG.get(subject.state, "IDLE")
        t = _fmt_time(subject.display_seconds())
        r1 = f"{tag:<4}  {t}"[:ROW_CHARS]

        r2 = _fit_meta(subject.model, subject.effort, ROW_CHARS)

        pos = 1
        for i, s in enumerate(queue):
            if s.key == subject.key:
                pos = i + 1
                break
        head = f"{pos}/{len(queue)} "
        room = ROW_CHARS - len(head)
        strip = "|".join(STATE_LETTER.get(s.state, "I") for s in queue)
        if len(strip) > room:
            strip = strip[:max(0, room - 1)] + "+"
        r3 = head + strip

        flash = subject.unacked_alert()
        line = f"F|{1 if flash else 0}|{r0}|{r1}|{r2}|{r3}"
        return (line, subject.key, flash)

    def refresh(self, force: bool = False) -> None:
        """Re-compose the frame and send it if the bytes changed (or `force`).

        The queue snapshot is taken INSIDE the Screen lock so snapshot-time and
        commit-time are ordered -- a preempted caller can never commit a stale
        frame over a newer one (Screen -> Registry lock nesting is safe: the
        registry never takes the Screen lock)."""
        now = time.time()
        with self._lock:
            queue = self._reg.queue()
            if not self._interacting(now):
                # Idle: the subject is the live queue head.
                self._sel_key = None
            subject = self._subject(queue)
            if self._interacting(now) and subject is not None:
                # Interacting: PIN whatever is rendered, so the screen cannot
                # change subject on its own inside the interaction window
                # (covers a pinned session vanishing -> head fallback, and an
                # idle screen whose first press should anchor the subject).
                self._sel_key = subject.key
            line, key, flash = self._compose(queue, subject)
            changed = (line != self._last_frame)
            if changed or force:
                if self._link.write_line(line):
                    self._last_frame = line
                    self._shown_key = key

    # ---- LED ---------------------------------------------------------------- #

    def sync_led(self) -> None:
        """Single source of truth for the indicator LED. Reads the most-urgent
        unacknowledged alert and drives the firmware loop:

          * error / waiting / done -> a CONTINUOUS loop (V|ERROR / V|INPUT /
            V|DONE); the firmware repeats it until V|OFF.
          * nothing -> V|OFF, silence.

        The truth read, the dedup check, the write, and the tracker commit all
        happen under ONE lock hold, so concurrent callers can never invert the
        LED state with a stale snapshot. The commit is gated on a SUCCESSFUL
        write -- a failed write (port momentarily closed) leaves the tracker
        stale so the next tick retries."""
        with self._lock:
            top = self._reg.top_alert()
            kind = LOOP_KIND.get(top.state) if top is not None else None
            if kind == self._loop_kind:
                return
            line = f"V|{kind}" if kind else "V|OFF"
            if not self._link.write_line(line):
                return                        # keep tracker stale -> retry next tick
            self._loop_kind = kind
            if kind:
                log(f"LED: loop {kind} until acknowledged")

    def start_tick(self) -> None:
        """One-shot START blink (a job (re)started). The caller fires this only
        when nothing needs the human, so it never interrupts an alert loop."""
        self._link.write_line("V|START")

    # ---- lifecycle ------------------------------------------------------------ #

    def resend_full_state(self) -> None:
        """On handshake (H) / reconnect: push the current frame AND re-arm the
        LED from scratch.

        A handshake means the Nano just (re)booted (opening the port resets it,
        and a replug does too) LED-off, having lost any loop it was playing. If
        we kept our loop tracker, `_set_loop` would see the desired loop already
        "sent" and never re-emit it -- so an unacknowledged alert would go
        permanently silent after a routine replug. So we forget the tracker and
        re-drive the LED from the current alert state."""
        log("handshake H -> resending full state")
        with self._lock:
            self._loop_kind = _LOOP_UNSET      # firmware was reset -> forget it
        self.refresh(force=True)
        self.sync_led()

    def notify_change(self) -> None:
        """Called on any registry change: refresh frame + LED."""
        self.refresh()
        self.sync_led()


# --------------------------------------------------------------------------- #
# FOCUS action (raise/activate ONLY -- never collapse/resize/minimize)
# --------------------------------------------------------------------------- #


def wrapper_ctrl_send(ctrl: str, cmd: str) -> bool:
    """Send one command line ('focus') to a PTY wrapper's per-session control
    socket and WAIT for its ack. The wrapper replies in two stages -- 'go' the
    moment it accepts the command (liveness), 'ok' only after its window op
    (osascript) has COMPLETED -- so consecutive focuses apply in press order.
    The first read waits only WRAPPER_LIVE_TIMEOUT_S, so a wedged wrapper
    (stopped process whose socket the kernel still accepts) costs ~1s, while a
    live-but-slow op gets the full WRAPPER_ACK_TIMEOUT_S deadline. Pre-ack
    wrappers close the socket immediately (recv -> EOF), degrading to
    fire-and-forget. Returns success."""
    if not ctrl or not os.path.exists(ctrl):
        return False
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(2.0)
        c.connect(ctrl)
        c.sendall(cmd.encode("ascii") + b"\n")
        try:
            deadline = time.monotonic() + WRAPPER_ACK_TIMEOUT_S
            c.settimeout(WRAPPER_LIVE_TIMEOUT_S)   # a live wrapper 'go's in ms
            buf = b""
            while b"ok" not in buf:
                chunk = c.recv(16)
                if not chunk:          # EOF: pre-ack wrapper (or already done)
                    break
                buf += chunk
                c.settimeout(max(0.1, deadline - time.monotonic()))
        except socket.timeout:
            log(f"wrapper {cmd} ack timeout ({ctrl}); continuing")
        c.close()
        return True
    except OSError as exc:
        log(f"wrapper {cmd} failed ({ctrl}): {exc}")
        return False


def focus_session(sess: Optional[Session]) -> None:
    """
    Raise the terminal/editor window of the given session. RAISE ONLY.

    Best: ask the PTY wrapper to raise its own terminal window (un-minimizes +
    activates). Then: the documented VS Code deep link. Fallback: raise the
    VS Code window for the workspace folder.
    """
    if sess is None:
        log("GO pressed but no session to focus")
        return

    log(f"FOCUS -> {sess.name} (sid={sess.sid or '?'}, cwd={sess.cwd or '?'})")

    # --- Best: ask the PTY wrapper to raise its own terminal window ----------
    if sess.focus_ctrl:
        if wrapper_ctrl_send(sess.focus_ctrl, "focus"):
            return
        log("using focus fallback")

    # --- Primary: deep link (hook-based sessions only) ----------------------
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

    def __init__(self, sock_path: str, registry: Registry, on_update,
                 on_haptic=None) -> None:
        super().__init__(name="socket-server", daemon=True)
        self._sock_path = sock_path
        self._reg = registry
        self._on_update = on_update
        self._on_haptic = on_haptic   # called with a LED kind on session events
        self._stop_evt = threading.Event()  # NOT `_stop`: Thread.join() calls its own _stop()
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

        while not self._stop_evt.is_set():
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
            while not self._stop_evt.is_set():
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
        # Expected: "<state>|<session_id>|<name>|<ctrl_sock?>|<model?>|<effort?>".
        # The hook path sends only the first three fields; the PTY wrapper adds
        # the control socket and (when it has scraped them) the model + effort.
        # state may be "end" (from the PTY wrapper) to remove the session.
        parts = line.split("|")
        state = parts[0].strip() if len(parts) > 0 else ""
        sid = parts[1].strip() if len(parts) > 1 else ""
        name = parts[2].strip() if len(parts) > 2 else ""
        ctrl = parts[3].strip() if len(parts) > 3 else ""  # wrapper focus socket
        model = parts[4].strip() if len(parts) > 4 else ""
        effort = parts[5].strip() if len(parts) > 5 else ""
        if not state:
            log(f"ignoring malformed socket line: {line!r}")
            return
        if state == "end":
            key = sid if sid else name
            if key:
                self._reg.remove(key)
                log(f"session ended: {name or sid}")
                self._on_update()
            return
        log(f"socket update: state={state} sid={sid or '-'} name={name or '-'}")
        haptic = self._reg.update(state, sid, name, focus_ctrl=ctrl,
                                  model=model, effort=effort)
        self._on_update()
        if haptic and self._on_haptic:
            log(f"LED: {haptic} transition for {name or sid} ({state})")
            self._on_haptic(haptic)

    def stop(self) -> None:
        self._stop_evt.set()


class ButtonReader(threading.Thread):
    """Reads serial input and dispatches H / B|<x> events."""

    def __init__(self, link: SerialLink, screen: Screen) -> None:
        super().__init__(name="button-reader", daemon=True)
        self._link = link
        self._screen = screen
        self._stop_evt = threading.Event()  # NOT `_stop`: Thread.join() calls its own _stop()
        self.on_ack = None   # callback(sess): acknowledge a session's alert
        # FOCUS runs on side threads (it can block for seconds on sockets /
        # subprocesses). `_focus_serial` serializes them so two quick GOs
        # can't raise windows in finish-order instead of press-order, and
        # `_focus_gen` lets a newer press supersede one still waiting its turn
        # (last wins).
        self._focus_serial = threading.Lock()
        self._focus_gen_lock = threading.Lock()
        self._focus_gen = 0

    def run(self) -> None:
        while not self._stop_evt.is_set():
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
            self._screen.resend_full_state()
            return
        if line == "K":                          # keepalive ack to our P: no-op
            return
        if line.startswith("B|") and len(line) >= 3:
            ev = line[2]
            if ev == "P":                        # PREV: selection up the queue
                self._screen.nav(-1)
            elif ev == "N":                      # NEXT: selection down the queue
                self._screen.nav(+1)
            elif ev == "G":                      # GO short: acknowledge + raise
                self._go()
            elif ev == "K":                      # GO long: acknowledge only
                self._ack_only()
            else:
                log(f"unknown button event: {line!r}")
            return
        log(f"ignoring serial line from Arduino: {line!r}")

    def _go(self) -> None:
        """GO: raise the terminal of EXACTLY the session shown on the glass
        (WYSIWYG). The ack happens first so the LED/display react instantly,
        then the window is raised on a side thread -- the reader thread must
        keep consuming button events. Focus threads are SERIALIZED (press order
        == raise order) and a press still queued when a newer one arrives is
        dropped (last wins).

        Screen behaviour: if the shown session was an ALERT (something to
        triage), snap home so the next alert surfaces (n alerts = n presses).
        If it was a CALM session (the user just wanted its terminal), STAY on
        it -- do not jump to an alert elsewhere."""
        sess = self._screen.resolve_press_target()
        log(f"GO -> focus {sess.name if sess else '-'}")
        if sess is None:
            return
        was_alert = sess.unacked_alert()
        if self.on_ack:
            self.on_ack(sess)                    # raising the window = acknowledged
        if was_alert:
            self._screen.snap_home()             # advance to the next alert
        else:
            self._screen.stay_on(sess)           # keep the focused terminal shown
        with self._focus_gen_lock:
            self._focus_gen += 1
            gen = self._focus_gen

        def run() -> None:
            with self._focus_serial:             # one focus at a time
                with self._focus_gen_lock:
                    if gen != self._focus_gen:   # superseded while queued
                        return
                focus_session(sess)

        threading.Thread(target=run, name="focus", daemon=True).start()

    def _ack_only(self) -> None:
        """GO long-press: acknowledge the shown session's alert WITHOUT touching
        any window. A no-op when the shown session has nothing to acknowledge."""
        sess = self._screen.resolve_press_target()
        if sess is None or not sess.unacked_alert():
            log("ACK (long press): nothing to acknowledge")
            return
        log(f"ACK (long press) -> {sess.name} (no focus)")
        if self.on_ack:
            self.on_ack(sess)                    # silences the LED + re-renders
        self._screen.snap_home()                 # surface the next alert

    def stop(self) -> None:
        self._stop_evt.set()


class SerialMaintainer(threading.Thread):
    """Keeps the serial port open; reconnects when it drops; sends pings."""

    def __init__(self, link: SerialLink, screen: Screen) -> None:
        super().__init__(name="serial-maintainer", daemon=True)
        self._link = link
        self._screen = screen
        self._stop_evt = threading.Event()  # NOT `_stop`: Thread.join() calls its own _stop()
        self._last_ping = 0.0

    def run(self) -> None:
        was_open = False
        while not self._stop_evt.is_set():
            if not self._link.is_open():
                if was_open:
                    log("serial disconnected; will reconnect")
                    was_open = False
                if self._link.ensure_open():
                    was_open = True
                    # Give the Nano time to reset; it will send H which triggers
                    # a full resend. We also push state proactively as a safety net.
                    time.sleep(2.0)
                    self._screen.resend_full_state()
                else:
                    self._stop_evt.wait(RECONNECT_DELAY)
                    continue
            # Periodic keepalive ping.
            now = time.time()
            if now - self._last_ping >= PING_PERIOD:
                self._link.write_line("P")
                self._last_ping = now
            self._stop_evt.wait(1.0)

    def stop(self) -> None:
        self._stop_evt.set()


class Ticker(threading.Thread):
    """1 Hz housekeeping: prunes stale sessions, keeps the displayed times
    ticking (the frame is re-sent only when its bytes actually change), snaps
    the selection home after the interaction window, and keeps the LED honest."""

    def __init__(self, screen: Screen, registry: Registry) -> None:
        super().__init__(name="ticker", daemon=True)
        self._screen = screen
        self._reg = registry
        self._stop_evt = threading.Event()  # NOT `_stop`: Thread.join() calls its own _stop()
        self._last_prune = 0.0

    def run(self) -> None:
        # Prime the display.
        self._screen.refresh(force=True)
        while not self._stop_evt.is_set():
            now = time.time()
            if now - self._last_prune >= 5.0:
                self._reg.prune()
                self._last_prune = now
            self._screen.notify_change()
            self._stop_evt.wait(1.0)

    def stop(self) -> None:
        self._stop_evt.set()


# --------------------------------------------------------------------------- #
# Mock injector (--mock)
# --------------------------------------------------------------------------- #


class MockInjector(threading.Thread):
    """Injects a few fake sessions cycling through states for demos."""

    def __init__(self, registry: Registry, on_update) -> None:
        super().__init__(name="mock-injector", daemon=True)
        self._reg = registry
        self._on_update = on_update
        self._stop_evt = threading.Event()  # NOT `_stop`: Thread.join() calls its own _stop()

    def run(self) -> None:
        # Three to four fake sessions in different working dirs.
        fakes = [
            ("sid-aaa", "webapp", "/Users/demo/webapp"),
            ("sid-bbb", "api", "/Users/demo/api"),
            ("sid-ccc", "infra", "/Users/demo/infra"),
            ("sid-ddd", "notes", "/Users/demo/notes"),
        ]
        cycle = ["working", "waiting", "error", "done", "idle"]
        log("MOCK mode: injecting fake sessions")
        # Seed initial states covering every queue class at once.
        seeds = ["working", "waiting", "error", "done"]
        for (sid, name, cwd), st in zip(fakes, seeds):
            self._reg.update(st, sid, name, cwd)
        demo_meta = {
            "webapp": ("Opus 4.8", "xhigh"),
            "api":    ("Sonnet 4.6", "high"),
            "infra":  ("Haiku 4.5", "medium"),
            "notes":  ("Opus 4.8", "max"),
        }
        with self._reg._lock:  # set demo model / effort strings directly
            for s in self._reg._sessions.values():
                if s.name in demo_meta:
                    s.model, s.effort = demo_meta[s.name]
        self._on_update()

        step = 0
        while not self._stop_evt.is_set():
            # Advance one session's state every ~4s so the screen/LED move.
            sid, name, cwd = fakes[step % len(fakes)]
            new_state = cycle[(step // len(fakes)) % len(cycle)]
            self._reg.update(new_state, sid, name, cwd)
            self._on_update()
            step += 1
            self._stop_evt.wait(4.0)

    def stop(self) -> None:
        self._stop_evt.set()


# --------------------------------------------------------------------------- #
# Application wiring
# --------------------------------------------------------------------------- #


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Claude Mate daemon: bridges Claude Code hooks to the "
                    "Arduino triage-queue companion over USB serial.",
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
    screen = Screen(link, registry)

    def on_update() -> None:
        screen.notify_change()

    def on_ack(sess) -> None:
        registry.acknowledge(sess)
        screen.notify_change()

    def on_haptic(kind: str) -> None:
        """Immediate one-shots on a fresh transition. on_update() ran first, so
        the error/waiting/done continuous loops are already handled by
        update_led(); the only extra signal is the calm one-shot START blink,
        fired only when nothing louder needs the human."""
        if kind == "START" and registry.top_alert() is None:
            screen.start_tick()

    socket_server = SocketServer(args.sock, registry, on_update, on_haptic)
    button_reader = ButtonReader(link, screen)
    button_reader.on_ack = on_ack
    maintainer = SerialMaintainer(link, screen)
    ticker = Ticker(screen, registry)

    threads: List[threading.Thread] = [
        socket_server,
        maintainer,
        button_reader,
        ticker,
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

    # launchd stops us with SIGTERM: turn it into a clean SystemExit so the
    # finally below runs and the device is left in an honest state.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

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
        # Join the workers BEFORE the goodbye writes so a still-running ticker
        # can't clobber them (all loops wake within ~1 s).
        for t in threads:
            t.join(timeout=1.5)
        # Best-effort: silence the LED and leave an honest frame on the way out.
        try:
            link.write_line("V|OFF")
            link.write_line("F|0|MATE|daemon stopped||")
        except Exception:
            pass
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
