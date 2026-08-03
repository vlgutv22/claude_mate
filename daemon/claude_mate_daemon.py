#!/usr/bin/env python3
"""
Claude Mate daemon
==================

The "brain" of the Claude Mate USB hardware companion. It:

  1. Listens on a Unix-domain socket (CLAUDE_MATE_SOCK) for status lines emitted
     by Claude Code hooks / the PTY wrapper: "<state>|<session_id>|<name>[|...]".
  2. Manages the device link(s). By default that is a single, continuously-open
     USB serial connection to the Arduino Nano: it auto-detects the port
     (/dev/cu.usbserial* then /dev/cu.usbmodem*), opens it once, keeps it open,
     and auto-reconnects if it disappears. With --tcp it ALSO serves the exact
     same line protocol over TCP, so a wireless companion (the ESP32-S3 build)
     can drive the fleet from across the room. Frames fan out to every connected
     device; button events from any of them are merged into one stream, so a USB
     Nano and a WiFi device can be used side by side.
  3. Keeps ONE urgency-sorted triage queue of sessions
     (error > waiting > done > working > idle; unacknowledged before
     acknowledged inside a class; oldest event first) and renders ONE screen --
     the selected session (normally the queue head, i.e. the thing that needs
     the human most) as a pre-composed 4-row (all size-1) text frame:

         F|<flags>|<sel>|<name>|<state+time · account>|<model+effort · limit>
          |<pos + fleet letters>

     (account = which login the session runs as, limit = its remaining-limit
     chip, both right-aligned; empty for hook-driven sessions.) The firmware
     is a dumb renderer; ALL layout/ordering/selection lives here.
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
     A GO/ACK never auto-switches tabs -- it stays on the session it acted on.
     Double-clicking GO toggles FOLLOW mode: PREV/NEXT then also raise the
     selected terminal (raise only, after the selection settles).
  5. Drives the indication LED via V|<KIND>: the pattern for the WORST
     unacknowledged alert class, looping until acknowledged (V|OFF).

Screen ownership rule: the display NEVER changes subject on its own -- only
PREV/NEXT/GO move it. Alerts on other tabs signal through the LED and their
blinking fleet letters; the view stays where the user left it. A GO/ACK acts
on EXACTLY the session whose frame is on the glass -- never a freshly
recomputed head -- so a press can only ever raise the terminal the user is
actually looking at.

Only third-party dependency: pyserial.

Run:
    python3 daemon/claude_mate_daemon.py            # real hardware + hooks
    python3 daemon/claude_mate_daemon.py --mock     # demo with fake sessions

Config via environment variables:
    CLAUDE_MATE_PORT   serial device (default: autodetect)
    CLAUDE_MATE_SOCK   socket path   (default: /tmp/claude-mate.sock)
    CLAUDE_MATE_BAUD   serial baud   (default: 115200)
    CLAUDE_MATE_TCP    set to 1 to also serve the protocol over TCP
    CLAUDE_MATE_TCP_PORT / _TCP_BIND   listener port / bind address
    CLAUDE_MATE_TOKEN / _TOKEN_FILE    shared secret for TCP devices (required
                       for --tcp; the token itself never crosses the wire, see
                       the NetLink handshake)
"""

from __future__ import annotations

import argparse
import glob
import hmac
import os
import queue
import secrets
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import wave
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

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

# --- Wireless (TCP) transport ---------------------------------------------- #
# Opt-in (--tcp / CLAUDE_MATE_TCP=1). The device dials US: the daemon's address
# is stable, a DHCP device's is not.
DEFAULT_TCP_PORT = 8787
DEFAULT_TCP_BIND = "0.0.0.0"    # a remote device needs a routable address; the
                                # listener is token-gated (see NetLink)
DEFAULT_TOKEN_FILE = "~/.config/claude-mate/token"
NET_AUTH_TIMEOUT_S = 5.0    # a client must finish the handshake this fast
NET_MAX_LINE = 512          # drop over-long lines (the longest real one is ~94B)
NET_MAX_CLIENTS = 4         # bound the fan-out (a Nano + a few wireless devices)
MDNS_SERVICE = "_claudemate._tcp"   # advertised via macOS dns-sd so the device
MDNS_NAME = "Claude Mate"           # finds the daemon with zero configuration

# Timings (seconds).
DOUBLE_CLICK_S = 0.30        # two GO short-presses within this window = a
                             # double-click (toggles FOLLOW mode). A single GO
                             # is deferred this long to disambiguate.
# ---- MIRROR (the device's terminal view) ---------------------------------- #
# The colour screen fits 53x21 characters at size 1; the status bar takes the
# top band, leaving MIRROR_ROWS for content. Claude's TUI is 80-120 columns
# wide, so rows are CLIPPED, not reflowed -- wrapping a TUI whose box drawing
# and alignment carry meaning turns it into noise, and a clipped-but-truthful
# view is easier to read than a rewrapped one.
MIRROR_COLS = 52
MIRROR_ROWS = 17
MIRROR_POLL_S = 1.0          # refresh cadence while the view is open; polling
                             # stops entirely when it is closed, so an unwatched
                             # fleet costs nothing

FOLLOW_SETTLE_S = 0.25       # in FOLLOW mode, PREV/NEXT raise the selected
                             # terminal only after the selection settles this
                             # long -- so holding to scroll doesn't raise every
                             # window it passes over (raise ONLY, never collapse)
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

# ---- Alert sounds (macOS, opt-in) ------------------------------------------ #
# The device itself cannot make a sound: the ESP32-S3 has no DAC, and the
# Waveshare board's 3.3 V rail is a linear LDO with the backlight on a plain
# resistor-limited MOSFET -- there is no switching node whose current the
# firmware could modulate into anything audible. So until a piezo is soldered
# on, the Mac is the only speaker in the system.
#
# That is not purely a workaround. When you are AT the Mac it is the better
# channel: real speakers, distinguishable sounds, and it follows the system
# volume. It stops being enough exactly when the device is across the room on
# battery -- which is the case the piezo will eventually cover.
#
# Off by default. An audible alert on every state change is far more intrusive
# than a colour change, and that has to be opted into rather than inherited.
SOUND_DIR = "/System/Library/Sounds"
ALERT_SOUNDS = {
    "ERROR": "Basso.aiff",     # descending, unmistakably "something went wrong"
    "INPUT": "Glass.aiff",     # bright and short: a question, not a failure
    "DONE":  "Hero.aiff",      # rising, resolved
    "START": "Pop.aiff",       # a tick; only fires when nothing else is pending
}
SOUND_MIN_GAP_S = 2.0          # floor between sounds, whatever the state does

# SHIP IT, the game on the device, over `O|SFX|<code>`. The board has no DAC, no
# speaker and a backlight circuit with no inductor to abuse, so if the game is to
# make a noise at all it has to be this machine making it.
#
# These were macOS system alert sounds for one version, and that was wrong in a
# way that only shows up when you play: Basso and Glass are the vocabulary of an
# OS telling you something, not of a game. The browser prototype uses WebAudio
# square and sawtooth oscillators, and THAT is the sound the device should make
# -- so the recipes below are the prototype's literals, and the daemon renders
# them to WAV once at startup rather than reaching for a file someone at Apple
# designed for a different purpose.
#
#   (frequencies Hz), note seconds, waveform, peak gain
#
# A recipe of several frequencies is played as a SEQUENCE, one note per entry,
# each `dur` long -- that is what makes the merge an arpeggio and the ship a
# fanfare rather than a chord.
SFX_RECIPES = {
    "J": ((520, 760),                  0.055, "square",   0.050),  # jump
    "S": ((300, 150),                  0.050, "square",   0.050),  # stomped a bug
    "M": ((660, 880, 1320),            0.065, "square",   0.055),  # merged a PR
    "H": ((180, 120),                  0.090, "sawtooth", 0.050),  # a bug got you
    "R": ((180, 120),                  0.090, "sawtooth", 0.050),  # re-scoped
    "F": ((400, 260, 160),             0.060, "sawtooth", 0.050),  # fell
    "X": ((400, 260, 160),             0.060, "sawtooth", 0.050),  # slipped
    "W": ((523, 659, 784, 1047, 1319), 0.110, "square",   0.060),  # shipped
}
# R and X are not distinct sounds, and that is the prototype's choice rather
# than an oversight here: a priority change reads as a hit and a slipped
# milestone reads as a fall, which is what they are.

# The prototype's gains are WebAudio linear gain into a browser, and the same
# numbers rendered to a file and pushed through afplay land far below the system
# alerts they sit beside -- quiet enough to miss over a fan. Everything is scaled
# by this one factor, so the sounds keep their RELATIVE levels (the ship fanfare
# stays the loudest) while landing somewhere a person can hear.
SFX_GAIN     = 5.0
SFX_RATE     = 44100
SFX_ATTACK_S = 0.008           # the prototype's linear attack, then exp decay
SFX_FLOOR    = 0.0008          # ...to here, which is where its ramp ends

# Rendered WAVs live here. The version in the name is load-bearing: change a
# recipe without changing it and every machine keeps playing the old sound from
# cache forever.
SFX_CACHE_DIR = os.path.join(
    os.path.expanduser("~/Library/Caches/claude-mate"), "sfx-v1")

GAME_SOUND_MIN_GAP_S = 0.06

# A device with no token redials every couple of seconds; say what to do about
# it at most this often, or the advice buries itself.
NO_TOKEN_LOG_GAP_S = 60.0

# Sentinel for "we have not told the firmware a loop state yet", so the first
# resolve always emits (V|OFF) and clears any stale loop left by a prior daemon.
_LOOP_UNSET = "\x00unset"


def _sfx_render(freqs, dur: float, wave: str, vol: float) -> bytes:
    """One recipe -> 16-bit mono PCM, reproducing WebAudio's envelope exactly.

    The prototype gives each note its own oscillator and gain node, ramps the
    gain linearly 0 -> vol over 8 ms, then EXPONENTIALLY down to 0.0008 by the
    note's end. The exponential matters: a linear fade of a square wave at these
    durations sounds like a click with a tail, and the whole character of the
    thing is in that decay curve. WebAudio's exponentialRampToValueAtTime is
    v0 * (v1/v0)^(progress), which is what the pow() below is.

    Naive (non-band-limited) square and sawtooth, deliberately. WebAudio uses
    band-limited wavetables, so this aliases slightly where that does not -- at
    these frequencies the difference is inaudible, and the alternative is a
    Fourier synthesis nobody can check against the prototype by ear."""
    frames = bytearray()
    span   = max(dur - SFX_ATTACK_S, 1e-6)
    ratio  = SFX_FLOOR / vol
    peak   = min(vol * SFX_GAIN, 0.85)         # headroom against clipping
    for f in freqs:
        n = int(SFX_RATE * dur)
        for i in range(n):
            t = i / SFX_RATE
            if t < SFX_ATTACK_S:
                env = t / SFX_ATTACK_S
            else:
                env = pow(ratio, (t - SFX_ATTACK_S) / span)
            # Phase restarts per note, as it must: the prototype creates a new
            # oscillator for each one.
            ph = (f * t) % 1.0
            sample = (2.0 * ph - 1.0) if wave == "sawtooth" else (1.0 if ph < 0.5 else -1.0)
            v = int(max(-1.0, min(1.0, peak * env * sample)) * 32767)
            frames += struct.pack("<h", v)
    return bytes(frames)


def sfx_build_cache() -> dict:
    """Render every recipe to a WAV once and return {code: path}.

    Returns {} on any failure -- a game with no sound is a working game, and a
    daemon that will not start because it could not write a beep is not."""
    out = {}
    try:
        os.makedirs(SFX_CACHE_DIR, exist_ok=True)
        for code, (freqs, dur, wave_t, vol) in SFX_RECIPES.items():
            path = os.path.join(SFX_CACHE_DIR, f"{code}.wav")
            if not os.path.exists(path):
                pcm = _sfx_render(freqs, dur, wave_t, vol)
                # Written to a temp name and renamed, so a daemon killed
                # mid-render cannot leave a truncated WAV cached forever.
                tmp = path + ".part"
                with wave.open(tmp, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(SFX_RATE)
                    w.writeframes(pcm)
                os.replace(tmp, path)
            out[code] = path
    except Exception as exc:
        log(f"game sound: could not build the cache ({exc}); game is silent")
        return {}
    return out


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
    account: str = ""              # account the session runs as, e.g. "work" (PTY wrapper)
    limit: str = ""                # remaining-limit chip, e.g. "5h82%" (PTY wrapper)
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
               effort: str = "", account: str = "",
               limit: str = "") -> Optional[str]:
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
            if account:
                sess.account = account
            if limit:
                sess.limit = limit
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
        waiting > done, oldest first), or None. Drives the LED loop class.
        Sorted by URGENCY independently of the display order (which is a
        stable alphabetical order, see queue())."""
        with self._lock:
            items = [s for s in self._sessions.values() if s.unacked_alert()]
        items.sort(key=lambda s: (STATE_ORDER.get(s.state, 99), s.state_since))
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
        """The display/navigation order: STABLE, alphabetical by name (then key
        as a deterministic tiebreak). Tabs keep their position as their states
        change -- they never shuffle under the user. Urgency drives the LED
        separately (see top_alert()), not this order."""
        with self._lock:
            items = list(self._sessions.values())
        items.sort(key=lambda s: (s.name.lower(), s.key))
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

    # Same names LinkHub exposes, so the maintainer can drive either link type
    # without caring which transport it got.
    def serial_is_open(self) -> bool:
        return self.is_open()

    def ensure_serial_open(self) -> bool:
        return self.ensure_open()

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
# Wireless link: the same line protocol over TCP (opt-in).
# --------------------------------------------------------------------------- #


def load_token(explicit: Optional[str]) -> Optional[str]:
    """Resolve the shared secret: --token/env first, then the token file.

    A file keeps the secret out of the LaunchAgent plist (which is readable by
    every process on the machine); we require it to be non-empty but do not
    enforce permissions, since the user may deliberately share it.
    """
    if explicit:
        return explicit.strip() or None
    path = os.path.expanduser(os.environ.get("CLAUDE_MATE_TOKEN_FILE",
                                             DEFAULT_TOKEN_FILE))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def ensure_token(explicit: Optional[str]) -> Optional[str]:
    """load_token(), but MAKE one rather than refusing if there is none.

    --tcp used to fail closed on a missing token and fall back to USB-only,
    which was the right instinct aimed at the wrong problem. The danger being
    guarded against is an UNAUTHENTICATED listener; a freshly generated 32-byte
    secret is not that. What the old behaviour actually produced was a dead end:
    the device's setup portal asks for a token, and the only way to have one was
    to have already known to create the file by hand. Generating it here closes
    that loop -- run the daemon once, read the token off the terminal, type it
    into the portal.

    Written 0600, and only ever created; an existing file is never rewritten, so
    this cannot silently invalidate a device that is already provisioned.
    """
    tok = load_token(explicit)
    if tok:
        return tok
    if explicit:                       # they passed one and it was blank
        return None
    path = os.path.expanduser(os.environ.get("CLAUDE_MATE_TOKEN_FILE",
                                             DEFAULT_TOKEN_FILE))
    tok = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Create-exclusive: if another daemon raced us to it, read theirs rather
        # than clobbering a token a device may already be provisioned with.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(tok + "\n")
    except FileExistsError:
        return load_token(None)
    except OSError as exc:
        log(f"ERROR: could not create a token at {path}: {exc}")
        return None
    log("")
    log("  no shared token existed, so one was generated:")
    log("")
    log(f"      {tok}")
    log("")
    log(f"  saved to {path} (mode 0600).")
    log("  Type it into the device's wifi setup portal, in 'Shared token'.")
    log("")
    return tok


class NetLink:
    """Serves the daemon<->device line protocol to token-authenticated TCP peers.

    The device is the CLIENT: the daemon's address is stable and discoverable
    (mDNS), a DHCP device's is not, and dialling out means the device needs no
    inbound reachability at all.

    HANDSHAKE (the token never crosses the wire, so sniffing one session cannot
    reveal it and a captured handshake cannot be replayed against a later one):

        daemon -> device   C|<nonce>          32 hex chars, fresh per connection
        device -> daemon   A|<mac>            hex HMAC-SHA256(token, nonce)
        daemon -> device   A|OK               ...or A|NO and the socket closes

    After A|OK the socket carries exactly the bytes the serial link would: F|/V|/P
    down, H/K/B| up. The device sends H, which makes the daemon resend full state
    -- the same recovery path a Nano replug uses.

    Writes fan out to every authenticated client and reads from all of them merge
    into one queue, so a USB Nano and a wireless device work side by side. A
    device that vanishes without a FIN (walked out of WiFi range) is reaped the
    first time a frame or ping fails to send.

    THREAT MODEL: the payload itself is plaintext. Anyone who can sniff your LAN
    can read session names, models and states, and an on-path attacker could
    inject button events into an established connection. Use it on a network you
    trust; it is off unless you ask for it.
    """

    def __init__(self, bind: str, port: int, token: str,
                 rx: "queue.Queue[str]") -> None:
        self._bind = bind
        self._port = port
        self._token = token.encode("utf-8")
        self._rx = rx
        self._srv: Optional[socket.socket] = None
        self._lock = threading.Lock()           # guards _clients
        self._clients: List[socket.socket] = []
        self._stop_evt = threading.Event()
        self._threads: List[threading.Thread] = []
        self._no_token_logged = 0.0             # throttle for _log_no_token

    # ---- lifecycle -------------------------------------------------------- #

    def start(self) -> bool:
        """Bind + listen and start accepting. False if the port is unusable."""
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self._bind, self._port))
            srv.listen(NET_MAX_CLIENTS)
            srv.settimeout(0.5)                 # so the accept loop can stop
        except OSError as exc:
            log(f"TCP listen failed on {self._bind}:{self._port}: {exc}")
            return False
        self._srv = srv
        t = threading.Thread(target=self._accept_loop, name="net-accept",
                             daemon=True)
        t.start()
        self._threads.append(t)
        log(f"TCP listening on {self._bind}:{self._port} (token required)")
        return True

    def stop(self) -> None:
        self._stop_evt.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
            self._srv = None
        with self._lock:
            clients, self._clients = self._clients, []
        for c in clients:
            self._shutdown(c)

    @staticmethod
    def _shutdown(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # ---- accept + authenticate -------------------------------------------- #

    def _accept_loop(self) -> None:
        while not self._stop_evt.is_set():
            srv = self._srv
            if srv is None:
                return
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return                          # listener closed: we are done
            with self._lock:
                too_many = len(self._clients) >= NET_MAX_CLIENTS
            if too_many:
                log(f"TCP refused {addr[0]}: already serving {NET_MAX_CLIENTS}")
                self._shutdown(conn)
                continue
            t = threading.Thread(target=self._serve, args=(conn, addr),
                                 name="net-client", daemon=True)
            t.start()
            self._threads.append(t)

    def _authenticate(self, conn: socket.socket, peer: str) -> bool:
        """Nonce challenge / HMAC response. False = reject (caller closes)."""
        nonce = secrets.token_hex(16)
        expect = hmac.new(self._token, nonce.encode("ascii"),
                          "sha256").hexdigest()
        try:
            conn.settimeout(NET_AUTH_TIMEOUT_S)
            conn.sendall(f"C|{nonce}\n".encode("ascii"))
            reply = self._read_line_blocking(conn)
        except (OSError, socket.timeout):
            log(f"TCP {peer}: handshake timed out")
            return False
        if reply is None or not reply.startswith("A|"):
            log(f"TCP {peer}: bad handshake {reply!r}")
            return False
        # A device that knows it has no token says so, rather than hanging up and
        # leaving us to report "bad handshake None" -- which reads like a crash
        # or a network fault and sends you looking in the wrong place entirely.
        # It is the commonest wireless failure by far, because it is what a
        # cleared token looks like, so it gets the one message that says exactly
        # what to do about it.
        if reply.strip().upper() == "A|NOTOKEN":
            self._log_no_token(peer)
            try:
                conn.sendall(b"A|NO\n")
            except OSError:
                pass
            return False
        # compare_digest keeps the comparison time independent of how much of
        # the MAC an attacker guessed right.
        if not hmac.compare_digest(reply[2:].strip().lower(), expect):
            log(f"TCP {peer}: token rejected")
            try:
                conn.sendall(b"A|NO\n")
            except OSError:
                pass
            time.sleep(0.5)                     # take the shine off brute force
            return False
        try:
            conn.sendall(b"A|OK\n")
        except OSError:
            return False
        return True

    def _log_no_token(self, peer: str) -> None:
        """The actionable message, throttled.

        The device redials every couple of seconds, so an unthrottled message
        would bury the log in copies of itself -- which is its own kind of
        useless, and exactly what `bad handshake None` was already doing."""
        now = time.time()
        if now - self._no_token_logged < NO_TOKEN_LOG_GAP_S:
            return
        self._no_token_logged = now
        path = os.path.expanduser(os.environ.get("CLAUDE_MATE_TOKEN_FILE",
                                                 DEFAULT_TOKEN_FILE))
        log(f"TCP {peer}: THE DEVICE HAS NO TOKEN. Open its wifi setup portal "
            f"(4th button -> MENU -> WI-FI) and paste the token into 'Shared "
            f"token', or send T|<token> over USB.")
        log(f"    this daemon's token is in {path}")

    @staticmethod
    def _read_line_blocking(conn: socket.socket) -> Optional[str]:
        """One newline-terminated line, bounded by NET_MAX_LINE. None on EOF."""
        buf = bytearray()
        while len(buf) < NET_MAX_LINE:
            chunk = conn.recv(1)
            if not chunk:
                return None
            if chunk in (b"\n", b"\r"):
                if not buf:
                    continue                    # tolerate CRLF / blank lines
                break
            buf += chunk
        return buf.decode("ascii", errors="replace").strip()

    def _serve(self, conn: socket.socket, addr) -> None:
        peer = f"{addr[0]}:{addr[1]}"
        if not self._authenticate(conn, peer):
            self._shutdown(conn)
            return
        try:
            # Keepalives so a device that drops off WiFi without a FIN is
            # eventually reaped even if we happen never to write to it.
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            conn.settimeout(None)               # blocking reads from here on
        except OSError:
            pass
        with self._lock:
            self._clients.append(conn)
        log(f"TCP device connected: {peer}")
        try:
            while not self._stop_evt.is_set():
                try:
                    line = self._read_line_blocking(conn)
                except (OSError, socket.timeout):
                    break
                if line is None:
                    break                       # clean EOF
                if line:
                    self._rx.put(line)
        finally:
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            self._shutdown(conn)
            log(f"TCP device disconnected: {peer}")

    # ---- the link surface ------------------------------------------------- #

    def has_clients(self) -> bool:
        with self._lock:
            return bool(self._clients)

    def write_line(self, line: str) -> bool:
        """Fan out to every client. True if at least one took the bytes."""
        data = (line.rstrip("\n") + "\n").encode("ascii", errors="replace")
        with self._lock:
            targets = list(self._clients)
        if not targets:
            return False
        dead: List[socket.socket] = []
        ok = False
        for c in targets:
            try:
                c.sendall(data)
                ok = True
            except OSError:
                dead.append(c)
        if dead:
            with self._lock:
                for c in dead:
                    if c in self._clients:
                        self._clients.remove(c)
            for c in dead:
                self._shutdown(c)               # its reader thread unblocks
        return ok


class LinkHub:
    """One device link made of a USB serial port plus a TCP listener.

    Presents exactly the surface the rest of the daemon already used on
    SerialLink (is_open / ensure_open / write_line / read_line / close), so
    Screen, ButtonReader and SerialMaintainer are untouched.

    Frames go to BOTH transports; incoming lines from either arrive on one
    queue. A dedicated pump moves serial input onto that queue so a wireless
    button press is never stuck behind a blocking serial read.
    """

    def __init__(self, serial_link: SerialLink, net: NetLink,
                 rx: "queue.Queue[str]") -> None:
        self._serial = serial_link
        self._net = net
        self._rx = rx
        self._stop_evt = threading.Event()
        self._pump = threading.Thread(target=self._pump_serial,
                                      name="serial-rx-pump", daemon=True)
        self._pump.start()

    def _pump_serial(self) -> None:
        while not self._stop_evt.is_set():
            if not self._serial.is_open():
                self._stop_evt.wait(0.2)        # nothing to read; don't spin
                continue
            line = self._serial.read_line()     # bounded by the read timeout
            if line:
                self._rx.put(line)

    # ---- the link surface ------------------------------------------------- #

    def is_open(self) -> bool:
        """Is ANY device reachable? A wireless client counts."""
        return self._serial.is_open() or self._net.has_clients()

    def ensure_open(self) -> bool:
        """Try to (re)open serial; a connected wireless device also counts as
        up, so startup does not report "no device" when only WiFi is in use."""
        opened = self._serial.ensure_open()
        return opened or self._net.has_clients()

    # The USB port, asked about on its OWN terms. is_open()/ensure_open() above
    # answer "is anything reachable", which is right for startup but WRONG for
    # the maintainer: with a wireless device connected they return True while
    # the serial port is shut, so a Nano that drops -- or is plugged in later --
    # would never be reopened and would sit on NO LINK forever.
    def serial_is_open(self) -> bool:
        return self._serial.is_open()

    def ensure_serial_open(self) -> bool:
        return self._serial.ensure_open()

    def write_line(self, line: str) -> bool:
        # Deliberately not short-circuiting: every connected device must get
        # every frame, so both writes always run.
        wired = self._serial.write_line(line)
        wireless = self._net.write_line(line)
        return wired or wireless

    def read_line(self) -> Optional[str]:
        try:
            return self._rx.get(timeout=0.5)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._stop_evt.set()
        self._net.stop()
        self._serial.close()


# Anything the rest of the daemon needs from a device link.
Link = Union[SerialLink, LinkHub]


class MdnsAdvertiser:
    """Advertises the TCP listener as _claudemate._tcp via macOS `dns-sd`.

    Uses the system tool rather than a Bonjour library so the daemon keeps its
    single third-party dependency (pyserial). Best-effort: no dns-sd, no
    advertisement -- the device can still be pointed at a host by hand.
    """

    def __init__(self, port: int) -> None:
        self._port = port
        self._proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        if not _which("dns-sd"):
            log("dns-sd not found; skipping mDNS advertisement")
            return
        try:
            self._proc = subprocess.Popen(
                ["dns-sd", "-R", MDNS_NAME, MDNS_SERVICE, "local",
                 str(self._port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            log(f"advertising {MDNS_NAME} {MDNS_SERVICE} on port {self._port}")
        except OSError as exc:
            log(f"mDNS advertisement failed: {exc}")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


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

    def __init__(self, link: Link, registry: Registry,
                 sound: bool = False) -> None:
        self._link = link
        self._reg = registry
        self._lock = threading.Lock()
        # Selection: a session KEY, or None meaning "the queue head". Tracked
        # by key so re-sorts never move the subject out from under the cursor.
        # STICKY: only PREV/NEXT/GO (and the head fallback when the selected
        # session vanishes) ever move it -- the screen never switches tabs on
        # its own.
        self._sel_key: Optional[str] = None
        # Last frame actually sent (dedup) + the key of the subject CURRENTLY
        # ON THE GLASS. A GO/ACK press acts on exactly this key (WYSIWYG), so
        # the raised terminal always matches the name the user is looking at.
        self._last_frame = ""
        self._shown_key: Optional[str] = None
        # FOLLOW mode: when on, PREV/NEXT auto-raise the selected terminal. The
        # active tab's fleet letter is boxed either way; the box is FILLED while
        # following (the on-screen "switch"), an outline when not.
        self._follow = False
        # LED loop tracker: what continuous loop the firmware is playing
        # ("ERROR"/"INPUT"/"DONE"/None); V| is (re)sent only when it changes.
        self._loop_kind: Optional[str] = _LOOP_UNSET
        # Alert sound (macOS). Rides the SAME transition as the LED rather than
        # having a policy of its own -- one truth, three renderings (rhythm,
        # colour, sound), which is the rule the S3's colour LED already follows.
        self._sound = sound
        self._last_sound_at = 0.0
        self._last_sfx_at   = 0.0
        # Rendered lazily-but-once at construction: eight short WAVs, well
        # under a tenth of a second of work, and doing it here means the
        # first jump of the first run is not the one that pays for it.
        self._sfx = sfx_build_cache() if sys.platform == "darwin" else {}

    # ---- selection -------------------------------------------------------- #

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
        with self._lock:
            queue = self._reg.queue()
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

    def stay_on(self, sess: Optional[Session]) -> None:
        """After a GO/ACK: pin the selection to the session the press acted on
        (and redraw, e.g. so its flash stops) -- the device stays on it."""
        if sess is None:
            return
        with self._lock:
            self._sel_key = sess.key
        self.refresh()

    def toggle_follow(self) -> bool:
        """Flip FOLLOW mode. Returns the new state. Reached by a GO
        double-click, or by holding the 4-button device's ACK button."""
        with self._lock:
            self._follow = not self._follow
            state = self._follow
        self.refresh()          # redraw so the switch box fills/empties
        return state

    def is_follow(self) -> bool:
        with self._lock:
            return self._follow

    def current_shown(self) -> Optional[Session]:
        """The session whose frame is on the glass right now (for FOLLOW's
        auto-raise), or None."""
        with self._lock:
            key = self._shown_key
            if key is None:
                return None
            for s in self._reg.queue():
                if s.key == key:
                    return s
            return None

    def resolve_press_target(self) -> Optional[Session]:
        """The session a GO/ACK press applies to: EXACTLY the one whose frame is
        on the glass (WYSIWYG). Never a freshly recomputed head -- so a press
        can only ever act on the name the user is actually looking at. Falls
        back to the queue head only if the shown session has vanished."""
        with self._lock:
            queue = self._reg.queue()
            if self._shown_key is not None:
                for s in queue:
                    if s.key == self._shown_key:
                        return s
            return self._subject(queue)         # shown session gone -> head

    # ---- frame composition -------------------------------------------------- #

    def _compose(self, queue: List[Session],
                 subject: Optional[Session]) -> Tuple[str, Optional[str], bool]:
        """Build the F| line for the current state:
            F|<flags>|<sel>|<r0>|<r1>|<r2>|<r3>
        with four size-1 rows (r0 name / r1 state+time + account / r2
        model+effort + remaining-limit chip / r3 position + fleet letters).
        `flags` is a bitfield
        (bit0 = flash the name, bit1 = FOLLOW mode -> draw the play marker);
        `sel` is the character column WITHIN r3 of the active tab's fleet
        letter to box (-1 = none). Returns (line, subject_key, flash). r3 is
        LAST and may contain literal '|' (the firmware stops tokenizing at the
        6th bar and takes the rest verbatim), which is what lets the strip use
        '|' as its visual divider."""
        follow = self._follow
        if subject is None:
            return (f"F|{2 if follow else 0}|-1|MATE|no sessions||", None, False)

        names = _display_names(queue)
        r0 = names.get(subject.key, "?")[:ROW_CHARS]

        tag = STATE_TAG.get(subject.state, "IDLE")
        t = _fmt_time(subject.display_seconds())
        r1 = f"{tag:<4}  {t}"[:ROW_CHARS]
        # Account the session runs as (PTY wrapper), right-aligned on the
        # state row with a >= 2-space gap; truncated to what fits. FOLLOW's
        # play triangle is drawn over the last two columns of this row, so
        # those stay blank while following (the account shifts/truncates left
        # instead of being overdrawn).
        acct = _sanitize(subject.account).strip()
        width = ROW_CHARS - (2 if follow else 0)
        room = width - len(r1) - 2
        if acct and room >= 2:
            r1 += acct[:room].rjust(width - len(r1))

        # Remaining-limit chip (e.g. "5h82%"), right-aligned on the meta row;
        # model+effort best-fit into what the chip leaves (>= 2-space gap).
        lim = _sanitize(subject.limit).strip()[:8]
        if lim:
            left = _fit_meta(subject.model, subject.effort,
                             ROW_CHARS - len(lim) - 2)
            r2 = (left + lim.rjust(ROW_CHARS - len(left)))[:ROW_CHARS]
        else:
            r2 = _fit_meta(subject.model, subject.effort, ROW_CHARS)

        pos = 1
        for i, s in enumerate(queue):
            if s.key == subject.key:
                pos = i + 1
                break
        head = f"{pos}/{len(queue)} "
        room = ROW_CHARS - len(head)
        # One letter per session, SPACE-separated (no '|' bars -- the active-tab
        # square marks which is which; the space gives the highlight rectangle
        # room to centre the letter). An UNACKNOWLEDGED alert's letter is sent
        # LOWERCASE so the firmware BLINKS it -- you can see at a glance which
        # tabs still need acknowledging (they stop as you ack them).
        letters = []
        for s in queue:
            ltr = STATE_LETTER.get(s.state, "I")
            letters.append(ltr.lower() if s.unacked_alert() else ltr)
        strip = " ".join(letters)
        if len(strip) > room:
            strip = strip[:max(0, room - 1)] + "+"
        r3 = head + strip
        # Column of the active tab's letter within r3 (letters sit at even
        # strip offsets: 0,2,4,...; spaces at the odds). -1 if it fell past a
        # truncation.
        sel = len(head) + 2 * (pos - 1)
        if sel >= len(r3) or not r3[sel].isalpha():
            sel = -1

        flash = subject.unacked_alert()
        flags = (1 if flash else 0) | (2 if follow else 0)
        line = f"F|{flags}|{sel}|{r0}|{r1}|{r2}|{r3}"
        return (line, subject.key, flash)

    def refresh(self, force: bool = False) -> None:
        """Re-compose the frame and send it if the bytes changed (or `force`).

        The queue snapshot is taken INSIDE the Screen lock so snapshot-time and
        commit-time are ordered -- a preempted caller can never commit a stale
        frame over a newer one (Screen -> Registry lock nesting is safe: the
        registry never takes the Screen lock)."""
        with self._lock:
            queue = self._reg.queue()
            subject = self._subject(queue)
            if subject is not None:
                # The screen NEVER switches tabs on its own: anchor the
                # selection to whatever is rendered (the first tab when nothing
                # was ever selected, the head fallback after the selected
                # session vanished), so it stays put until the user navigates.
                # Alerts elsewhere signal via the LED and their blinking fleet
                # letters only.
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
            self._play_alert(kind)

    def set_sound(self, on: bool) -> None:
        """Enable/disable the macOS alert sound. Called from the device."""
        with self._lock:
            if self._sound == on:
                return
            self._sound = on
        log(f"sound: {'on' if on else 'off'} (set from the device)")

    def _play_alert(self, kind: Optional[str]) -> None:
        """Play one sound for an alert-class transition. Never blocks, never
        raises, and never becomes load-bearing.

        Deliberately ONE-SHOT, where the LED loops until acknowledged. Light is
        ignorable and a beep is not: a sound that repeated until you dealt with
        it would be a smoke alarm, and would get the whole feature switched off
        within a day. The LED carries the persistent state; sound only marks the
        moment it changed.

        Called with self._lock held, so it must not do anything that can wait --
        Popen returns as soon as the child is spawned, and the child is fully
        detached (nothing ever reaps it, so stdio goes to /dev/null and we let
        it exit on its own)."""
        if not (self._sound and kind):
            return
        if sys.platform != "darwin":
            return
        now = time.time()
        # A session flapping error -> waiting -> error would otherwise stack up
        # overlapping afplay processes. The LED's dedup does not cover this: the
        # kind genuinely changes each time, so the transition is real.
        if now - self._last_sound_at < SOUND_MIN_GAP_S:
            return
        name = ALERT_SOUNDS.get(kind)
        if not name:
            return
        path = os.path.join(SOUND_DIR, name)
        try:
            subprocess.Popen(["afplay", path],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
            self._last_sound_at = now
        except Exception as exc:
            # A missing afplay or a renamed system sound must never take the
            # daemon down or stall the alert path -- it is decoration.
            log(f"sound: {exc}")
            self._sound = False        # one complaint, then stop trying

    def play_sfx(self, code: str) -> None:
        """One game sound, from `O|SFX|<code>`. Same contract as _play_alert:
        never blocks, never raises, never becomes load-bearing.

        It keeps its OWN clock rather than sharing _last_sound_at. Sharing would
        break both directions -- the 2 s alert floor would swallow a level's
        worth of jumps, and a run of jumps would suppress a genuine ERROR alert
        that arrived mid-game, which is exactly the alert you would want to hear.
        """
        with self._lock:
            on = self._sound
        if not on or sys.platform != "darwin":
            return
        path = self._sfx.get(code)
        if not path:
            return
        now = time.time()
        if now - self._last_sfx_at < GAME_SOUND_MIN_GAP_S:
            return
        try:
            subprocess.Popen(["afplay", path],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
            self._last_sfx_at = now
        except Exception as exc:
            log(f"game sound: {exc}")

    def start_tick(self) -> None:
        """One-shot START blink (a job (re)started). The caller fires this only
        when nothing needs the human, so it never interrupts an alert loop."""
        self._link.write_line("V|START")
        with self._lock:
            self._play_alert("START")

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


def wrapper_ctrl_screen(ctrl: str) -> Optional[List[str]]:
    """Ask a PTY wrapper for its rendered TUI, for the device's mirror view.

    Length-prefixed ("SCREEN <n>", then n rows, then "ok") rather than read
    until a sentinel: terminal output can contain ANY line, including one that
    is exactly "ok", which would truncate the mirror at an arbitrary point.
    The wrapper closes the connection when done, so this reads to EOF.

    Returns the rows, or None when there is nothing to show -- no wrapper (a
    hook-only session), a dead one, or one predating the mirror command, which
    answers a bare "ok". None is rendered as "no preview", never as a blank
    screen that would look like a session sitting idle.
    """
    if not ctrl or not os.path.exists(ctrl):
        return None
    buf = b""
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(WRAPPER_LIVE_TIMEOUT_S)
        c.connect(ctrl)
        c.sendall(b"screen\n")
        deadline = time.monotonic() + WRAPPER_ACK_TIMEOUT_S
        while True:
            chunk = c.recv(8192)
            if not chunk:                      # EOF: the wrapper is done
                break
            buf += chunk
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            c.settimeout(remaining)
        c.close()
    except OSError:
        return None
    lines = buf.decode("utf-8", "replace").split("\n")
    if not lines or not lines[0].startswith("SCREEN "):
        return None                            # pre-mirror wrapper
    try:
        n = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return None
    return lines[1:1 + n]


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
        # Expected: "<state>|<session_id>|<name>|<ctrl_sock?>|<model?>|<effort?>
        # |<account?>|<limit?>". The hook path sends only the first three
        # fields; the PTY wrapper adds the control socket, the scraped model +
        # effort, the account it runs as, and the remaining-limit chip.
        # state may be "end" (from the PTY wrapper) to remove the session.
        parts = line.split("|")
        state = parts[0].strip() if len(parts) > 0 else ""
        sid = parts[1].strip() if len(parts) > 1 else ""
        name = parts[2].strip() if len(parts) > 2 else ""
        ctrl = parts[3].strip() if len(parts) > 3 else ""  # wrapper focus socket
        model = parts[4].strip() if len(parts) > 4 else ""
        effort = parts[5].strip() if len(parts) > 5 else ""
        account = parts[6].strip() if len(parts) > 6 else ""
        limit = parts[7].strip() if len(parts) > 7 else ""
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
                                  model=model, effort=effort,
                                  account=account, limit=limit)
        self._on_update()
        if haptic and self._on_haptic:
            log(f"LED: {haptic} transition for {name or sid} ({state})")
            self._on_haptic(haptic)

    def stop(self) -> None:
        self._stop_evt.set()


class ButtonReader(threading.Thread):
    """Reads serial input and dispatches H / B|<x> events."""

    def __init__(self, link: Link, screen: Screen) -> None:
        super().__init__(name="button-reader", daemon=True)
        self._link = link
        self._screen = screen
        self._stop_evt = threading.Event()  # NOT `_stop`: Thread.join() calls its own _stop()
        self.on_ack = None   # callback(sess): acknowledge a session's alert
        self.bridge = None   # optional WebBridge: device-as-controller (--web)
        # FOCUS runs on side threads (it can block for seconds on sockets /
        # subprocesses). `_focus_serial` serializes them so two quick GOs
        # can't raise windows in finish-order instead of press-order, and
        # `_focus_gen` lets a newer press supersede one still waiting its turn
        # (last wins).
        self._focus_serial = threading.Lock()
        self._focus_gen_lock = threading.Lock()
        self._focus_gen = 0
        # GO double-click: a single GO is deferred DOUBLE_CLICK_S to see if a
        # second GO follows (which toggles FOLLOW mode instead).
        self._go_lock = threading.Lock()
        self._go_timer: Optional[threading.Timer] = None
        self._go_gen = 0
        # FOLLOW-mode auto-raise: PREV/NEXT (re)arm this settle timer; when the
        # selection stops moving for FOLLOW_SETTLE_S the shown terminal is
        # raised (raise ONLY). Rapid scrolling never raises intermediate ones.
        self._follow_lock = threading.Lock()
        self._follow_timer: Optional[threading.Timer] = None
        self._follow_gen = 0
        # MIRROR: while open, a repeating timer pulls the shown session's
        # rendered TUI from its wrapper and pushes it to the device. Keyed by
        # session so the view follows one session even if the selection is
        # nudged, and torn down completely when closed -- no timer, no polling.
        self._mirror_lock = threading.Lock()
        self._mirror_key: Optional[str] = None
        self._mirror_scroll = 0
        self._mirror_timer: Optional[threading.Timer] = None
        self._mirror_warned = False   # log the "no screen" reason once per open

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
            code = line[2:].strip()
            ev = code[0]
            # Device-as-controller (--web): mirror every press to the browser,
            # and while a page holds the GRAB the buttons belong to the GAME.
            # One place covers P/N/G/K/M/F and anything added later; the return
            # is what stops a jump from also raising a terminal on the Mac.
            bridge = self.bridge
            if bridge is not None:
                bridge.push(code)
                if bridge.grabbed():
                    return
            # +P / -P are press and release EDGES, which only exist while the
            # device is in controller mode. They are meaningless to the session
            # switcher -- acting on them would move the selection twice per
            # press, once down and once up -- so they stop here.
            if code[0] in "+-":
                return
            # While the terminal view is open PREV/NEXT scroll it instead of
            # moving the selection. Reinterpreting here keeps the firmware a
            # dumb renderer -- it emits the same two verbs either way and never
            # has to know a mode exists.
            if ev == "P":                        # PREV: selection up / scroll up
                if self.mirror_open():
                    self._mirror_scroll_by(+1)   # +1 = further back in history
                else:
                    self._nav(-1)
            elif ev == "N":                      # NEXT: selection down / scroll
                if self.mirror_open():
                    self._mirror_scroll_by(-1)
                else:
                    self._nav(+1)
            elif ev == "M":                      # MIRROR button: toggle the view
                self._mirror_pressed()
            elif ev == "G":                      # GO short: focus / (dbl) follow
                # Raising the real window supersedes looking at a copy of it.
                self.mirror_close()
                self._go_pressed()
            elif ev == "K":                      # GO long / ACK button: ack only
                self._ack_only()
            elif ev == "F":                      # ACK button held: toggle FOLLOW
                self._follow_pressed()
            else:
                log(f"unknown button event: {line!r}")
            return
        # O| -- a device-set OPTION. The device owns its own hardware settings
        # (brightness, screen sleep, LED level) and the daemon rightly knows
        # nothing about them. The alert SOUND is the exception, because it plays
        # here: the device has no speaker, so a mute reached for on the device
        # has to travel. The device re-sends this on every connect, since the
        # daemon keeps no per-device state to remember it in.
        if line.startswith("O|"):
            self._device_option(line[2:])
            return
        log(f"ignoring serial line from Arduino: {line!r}")

    # ---- navigation (+ FOLLOW-mode auto-raise) --------------------------- #

    def _nav(self, delta: int) -> None:
        self._screen.nav(delta)
        if self._screen.is_follow():
            self._arm_follow_focus()

    def _arm_follow_focus(self) -> None:
        with self._follow_lock:
            self._follow_gen += 1
            gen = self._follow_gen
            if self._follow_timer is not None:
                self._follow_timer.cancel()
            t = threading.Timer(FOLLOW_SETTLE_S, self._follow_fire, args=(gen,))
            t.daemon = True
            self._follow_timer = t
            t.start()

    def _follow_fire(self, gen: int) -> None:
        """The selection settled in FOLLOW mode: raise the shown terminal
        (raise ONLY, no acknowledge -- ack stays on GO long-press)."""
        with self._follow_lock:
            if gen != self._follow_gen:          # superseded by a newer nav
                return
            self._follow_timer = None
        if not self._screen.is_follow():
            return
        sess = self._screen.current_shown()
        if sess is None:
            return
        log(f"follow -> raise {sess.name}")
        self._raise(sess)

    # ---- GO: single (focus+ack) vs double-click (toggle FOLLOW) ---------- #

    def _go_pressed(self) -> None:
        """GO short-press. A single press (after DOUBLE_CLICK_S with no second
        press) focuses+acks the shown session; a second press within the window
        instead toggles FOLLOW mode."""
        with self._go_lock:
            if self._go_timer is not None:       # second press -> double-click
                self._go_gen += 1
                self._go_timer.cancel()
                self._go_timer = None
                on = self._screen.toggle_follow()
                log(f"double-click GO -> FOLLOW {'ON' if on else 'OFF'}")
                if on:                           # turning it on raises now
                    sess = self._screen.current_shown()
                    if sess is not None:
                        self._raise(sess)
                return
            self._go_gen += 1
            gen = self._go_gen
            t = threading.Timer(DOUBLE_CLICK_S, self._go_single_fire, args=(gen,))
            t.daemon = True
            self._go_timer = t
            t.start()

    def _go_single_fire(self, gen: int) -> None:
        with self._go_lock:
            if gen != self._go_gen:              # a second press superseded it
                return
            self._go_timer = None
        self._go()

    # ---- MIRROR: the device's terminal view ------------------------------ #

    def mirror_open(self) -> bool:
        with self._mirror_lock:
            return self._mirror_key is not None

    def _mirror_pressed(self) -> None:
        """B|M -- toggle the terminal view for the session on the glass."""
        sess = self._screen.current_shown()
        with self._mirror_lock:
            if self._mirror_key is not None:      # open -> close
                self._mirror_close_locked()
                log("MIRROR off")
                return
            if sess is None:
                return
            self._mirror_key = sess.key
            self._mirror_scroll = 0
            self._mirror_warned = False
            # The ctrl socket is the whole story when a mirror comes up empty:
            # sessions are keyed by session id, so a restarted terminal is a
            # NEW session that merely shares a display name with the old one.
            # Logging which socket was chosen distinguishes "wrong session" from
            # "wrapper too old" without guesswork.
            log(f"MIRROR on -> {sess.name} sid={sess.sid[:8]} "
                f"ctrl={sess.focus_ctrl or '(none)'}")
        self._mirror_tick()                       # paint immediately, do not
                                                  # wait out the first interval

    def _mirror_close_locked(self) -> None:
        """Tear down the view. Caller holds _mirror_lock."""
        self._mirror_key = None
        self._mirror_scroll = 0
        if self._mirror_timer is not None:
            self._mirror_timer.cancel()
            self._mirror_timer = None
        self._link.write_line("M|OFF")            # hand the glass back to the
        self._screen.resend_full_state()          # normal frame

    def mirror_close(self) -> None:
        with self._mirror_lock:
            if self._mirror_key is not None:
                self._mirror_close_locked()

    def _mirror_scroll_by(self, delta: int) -> None:
        """PREV/NEXT scroll the view instead of moving the selection."""
        with self._mirror_lock:
            if self._mirror_key is None:
                return
            self._mirror_scroll = max(0, self._mirror_scroll + delta)
        self._mirror_tick()

    def _mirror_tick(self) -> None:
        """Fetch the wrapper's screen, push it, and re-arm."""
        with self._mirror_lock:
            key = self._mirror_key
            scroll = self._mirror_scroll
        if key is None:
            return
        # PREV/NEXT scroll rather than navigate while the view is open, so the
        # shown session cannot move under us; a mismatch means it ended or was
        # pruned, and the view has nothing left to show.
        sess = self._screen.current_shown()
        if sess is None or sess.key != key:
            self.mirror_close()
            return

        rows = wrapper_ctrl_screen(sess.focus_ctrl or "")
        if rows is None and not self._mirror_warned:
            log(f"MIRROR: no screen from {sess.name} "
                f"(ctrl={sess.focus_ctrl or '(none)'}) -- hook-only session, or "
                f"a wrapper started before the mirror command existed")
        if rows is None:
            body = ["", "  no preview for this session.", "",
                    "  only wrapped sessions mirror their",
                    "  terminal -- a hook-only session has",
                    "  no PTY for the daemon to read."]
        else:
            # COLLAPSE runs of blank rows to a single spacer before windowing.
            # A TUI is not a scrolling log: Claude's screen puts the banner and
            # conversation at the top, a wide blank gap in the middle, and the
            # input box at the bottom. Taking the last 17 of 30 raw rows lands
            # almost entirely in that gap -- observed as a mirror showing one
            # line of footer and nothing else. Interior whitespace is padding
            # for an 80x30 terminal and is pure waste on a 17-row window, so one
            # blank row is kept as a separator and the rest dropped. Single
            # blanks survive, so paragraph structure still reads.
            compact: List[str] = []
            blank_run = 0
            for r in rows:
                if r.strip():
                    blank_run = 0
                    compact.append(r)
                else:
                    blank_run += 1
                    if blank_run == 1:
                        compact.append("")
            while compact and not compact[-1].strip():
                compact.pop()
            while compact and not compact[0].strip():
                compact.pop(0)
            # Show the TAIL: on a conversation the newest exchange is what you
            # glanced at the device to see. Scroll walks back from there.
            first = max(0, len(compact) - MIRROR_ROWS)
            start = max(0, first - scroll)
            body = compact[start:start + MIRROR_ROWS]

        title = f"{sess.name} {sess.state}"[:MIRROR_COLS]
        if not self._mirror_warned:
            self._mirror_warned = True
            log(f"MIRROR: sending {len(body)} rows for {sess.name} "
                f"(fetched {'-' if rows is None else len(rows)})")
        self._link.write_line(f"M|T|{title}")
        for i in range(MIRROR_ROWS):
            text = body[i] if i < len(body) else ""
            # Tabs and control bytes would desync the fixed-width layout, and
            # '|' is the wire's field separator.
            text = text.expandtabs(8).replace("|", "¦")
            text = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in text)
            self._link.write_line(f"M|{i}|{text[:MIRROR_COLS]}")
        self._link.write_line("M|END")

        with self._mirror_lock:
            if self._mirror_key is None:          # closed while we were fetching
                return
            if self._mirror_timer is not None:
                self._mirror_timer.cancel()
            t = threading.Timer(MIRROR_POLL_S, self._mirror_tick)
            t.daemon = True
            self._mirror_timer = t
            t.start()

    def _follow_pressed(self) -> None:
        """B|F -- the 4-button device's dedicated FOLLOW toggle.

        Same effect as a GO double-click, without the guesswork: there is no
        DOUBLE_CLICK_S window to race, so a deliberate toggle can never be
        misread as two hurried raises (nor two hurried raises as a toggle).
        Held under _go_lock because that is the lock the double-click path
        toggles under -- otherwise a physical double-click and an F arriving
        together could interleave and land on the wrong final state.

        A GO press still waiting out its window is deliberately left alone: it
        was a separate intent and its raise is harmless here (raises serialize
        last-wins, and turning FOLLOW on raises the same session anyway).
        """
        with self._go_lock:
            on = self._screen.toggle_follow()
            log(f"FOLLOW button -> {'ON' if on else 'OFF'}")
            if on:                               # turning it on raises now,
                sess = self._screen.current_shown()   # same as the dbl-click
                if sess is not None:
                    self._raise(sess)

    def _raise(self, sess: Optional[Session]) -> None:
        """Raise a session's window on a serialized side thread (raise ONLY).
        Press order == raise order; a press still queued when a newer one
        arrives is dropped (last wins)."""
        if sess is None:
            return
        with self._focus_gen_lock:
            self._focus_gen += 1
            gen = self._focus_gen

        def run() -> None:
            with self._focus_serial:             # one raise at a time
                with self._focus_gen_lock:
                    if gen != self._focus_gen:   # superseded while queued
                        return
                focus_session(sess)

        threading.Thread(target=run, name="focus", daemon=True).start()

    def _go(self) -> None:
        """A confirmed single GO: raise the terminal of EXACTLY the session
        shown on the glass (WYSIWYG), acknowledge it, and STAY on it -- the
        device never auto-switches tabs."""
        sess = self._screen.resolve_press_target()
        log(f"GO -> focus {sess.name if sess else '-'}")
        if sess is None:
            return
        if self.on_ack:
            self.on_ack(sess)                    # raising the window = acknowledged
        self._screen.stay_on(sess)               # stay on the acted tab
        self._raise(sess)

    def _device_option(self, body: str) -> None:
        """`O|<KEY>|<value>` from a device. Unknown keys are ignored, so an
        older daemon and a newer firmware stay interoperable in both
        directions -- the same rule the B| verbs already follow."""
        parts = body.split("|")
        if len(parts) != 2:
            log(f"malformed device option: {body!r}")
            return
        key, val = parts[0].strip().upper(), parts[1].strip()
        if key == "SND":
            self._screen.set_sound(val == "1")
        elif key == "SFX":
            # Deliberately NOT logged: a level fires hundreds of these and the
            # log is something a person reads.
            self._screen.play_sfx(val[:1].upper())
        else:
            log(f"unknown device option {key!r} (ignored)")

    def _ack_only(self) -> None:
        """GO long-press: acknowledge the shown session's alert WITHOUT touching
        any window, and STAY on it (no auto-switch). A no-op when the shown
        session has nothing to acknowledge."""
        sess = self._screen.resolve_press_target()
        if sess is None or not sess.unacked_alert():
            log("ACK (long press): nothing to acknowledge")
            return
        log(f"ACK (long press) -> {sess.name} (no focus)")
        if self.on_ack:
            self.on_ack(sess)                    # silences the LED + re-renders
        self._screen.stay_on(sess)               # stay on the acked tab

    def stop(self) -> None:
        self._stop_evt.set()


class SerialMaintainer(threading.Thread):
    """Keeps the serial port open; reconnects when it drops; sends pings."""

    def __init__(self, link: Link, screen: Screen) -> None:
        super().__init__(name="serial-maintainer", daemon=True)
        self._link = link
        self._screen = screen
        self._stop_evt = threading.Event()  # NOT `_stop`: Thread.join() calls its own _stop()
        self._last_ping = 0.0

    def run(self) -> None:
        was_open = False
        last_try = 0.0
        while not self._stop_evt.is_set():
            # Ask about the USB port SPECIFICALLY, never "is any device up".
            # With a wireless device connected the latter is True while serial
            # is shut, so a Nano that drops -- or is plugged in after the ESP32
            # linked -- would never be reopened and would sit on NO LINK.
            if not self._link.serial_is_open():
                if was_open:
                    log("serial disconnected; will reconnect")
                    was_open = False
                if time.time() - last_try >= RECONNECT_DELAY:
                    last_try = time.time()
                    if self._link.ensure_serial_open():
                        was_open = True
                        # Give the Nano time to reset; it will send H which
                        # triggers a full resend. We also push state proactively
                        # as a safety net.
                        time.sleep(2.0)
                        self._screen.resend_full_state()
            # Ping REGARDLESS of the USB port. Retrying serial used to `continue`
            # past this, which would have silenced keepalives for a wireless-only
            # setup -- the very thing the old "any device is up" check was
            # papering over.
            now = time.time()
            if now - self._last_ping >= PING_PERIOD:
                self._link.write_line("P")
                self._last_ping = now
            self._stop_evt.wait(1.0)

    def stop(self) -> None:
        self._stop_evt.set()


class Ticker(threading.Thread):
    """1 Hz housekeeping: prunes stale sessions, keeps the displayed times
    ticking (the frame is re-sent only when its bytes actually change), and
    keeps the LED honest."""

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
            "webapp": ("Opus 4.8", "xhigh", "work", "5h82%"),
            "api":    ("Sonnet 4.6", "high", "default", "5h97%"),
            "infra":  ("Haiku 4.5", "medium", "default", "wk31%"),
            "notes":  ("Opus 4.8", "max", "work", "5h82%"),
        }
        with self._reg._lock:  # set demo model/effort/account/limit directly
            for s in self._reg._sessions.values():
                if s.name in demo_meta:
                    s.model, s.effort, s.account, s.limit = demo_meta[s.name]
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
                    "triage-queue companion over USB serial and, with --tcp, "
                    "over the network.",
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
    parser.add_argument(
        "--tcp",
        action="store_true",
        default=os.environ.get("CLAUDE_MATE_TCP", "") == "1",
        help="also serve the protocol over TCP for wireless devices "
             "(requires a shared token; off by default)",
    )
    parser.add_argument(
        "--tcp-port",
        type=int,
        default=int(os.environ.get("CLAUDE_MATE_TCP_PORT", DEFAULT_TCP_PORT)),
        help=f"TCP listener port (default: {DEFAULT_TCP_PORT})",
    )
    parser.add_argument(
        "--tcp-bind",
        default=os.environ.get("CLAUDE_MATE_TCP_BIND", DEFAULT_TCP_BIND),
        help=f"TCP bind address (default: {DEFAULT_TCP_BIND}; use 127.0.0.1 to "
             "keep it on this machine)",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        default=os.environ.get("CLAUDE_MATE_WEB", "") == "1",
        help="serve the browser game on 127.0.0.1 and let the device's buttons "
             "drive it while the page is open (off by default)",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=int(os.environ.get("CLAUDE_MATE_WEB_PORT", 8788)),
        help="loopback port for --web (default: 8788)",
    )
    parser.add_argument(
        "--web-root",
        default=os.environ.get("CLAUDE_MATE_WEB_ROOT") or None,
        help="directory served by --web (default: <repo>/site/game)",
    )
    parser.add_argument(
        "--sound",
        action="store_true",
        default=os.environ.get("CLAUDE_MATE_SOUND", "") == "1",
        help="play a macOS alert sound when the worst unacknowledged alert "
             "class changes (off by default; the device itself has no speaker)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CLAUDE_MATE_TOKEN") or None,
        help=f"shared secret wireless devices authenticate with (default: read "
             f"from {DEFAULT_TOKEN_FILE})",
    )
    args = parser.parse_args(argv)

    log("starting Claude Mate daemon")
    log(f"  socket : {args.sock}")
    log(f"  port   : {args.port or 'autodetect'}")
    log(f"  baud   : {args.baud}")
    log(f"  mock   : {args.mock}")

    registry = Registry()
    serial_link = SerialLink(args.port, args.baud)
    link: Link = serial_link
    net: Optional[NetLink] = None
    mdns: Optional[MdnsAdvertiser] = None

    # Wireless transport is opt-in AND fails closed: an unauthenticated listener
    # would let anyone on the network read session names and raise windows, so a
    # missing token disables it rather than weakening it.
    if args.tcp:
        token = ensure_token(args.token)
        if not token:
            log("ERROR: --tcp needs a shared token and one could not be created. "
                f"Set CLAUDE_MATE_TOKEN, pass --token, or write one to "
                f"{DEFAULT_TOKEN_FILE}. Continuing with USB serial only.")
        else:
            rx: "queue.Queue[str]" = queue.Queue()
            candidate = NetLink(args.tcp_bind, args.tcp_port, token, rx)
            if candidate.start():
                net = candidate
                link = LinkHub(serial_link, net, rx)
                mdns = MdnsAdvertiser(args.tcp_port)
                mdns.start()
        log(f"  tcp    : {args.tcp_bind}:{args.tcp_port} "
            f"({'on' if net else 'DISABLED'})")

    screen = Screen(link, registry, sound=bool(args.sound))
    log(f"  sound  : {'on' if args.sound else 'off'}")

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

    # Device-as-controller. Loopback only, opt-in, and entirely optional: an
    # import or bind failure costs the game, never the daemon. The import is
    # lazy and inside the branch because tools/test_net_link.py exec_modules
    # this file by path, where a module-level import would need daemon/ on
    # sys.path.
    web = None
    if args.web:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            from webbridge import WebBridge
        except Exception as exc:
            log(f"--web: cannot import webbridge.py: {exc}")
        else:
            candidate = WebBridge(root=args.web_root, port=args.web_port,
                                  device=link.is_open, log=log)

            def _controller(on: bool, _link=link) -> None:
                # Put the DEVICE into controller mode for as long as a page is
                # driving it. Without this the buttons keep their menu
                # semantics -- 40 ms debounce, then 400 ms before auto-repeat,
                # then one event every 200 ms -- which is right for a selection
                # list and unplayable as a gamepad. In controller mode the
                # firmware reads the pins raw and sends press/release edges.
                try:
                    _link.write_line("X|1" if on else "X|0")
                except Exception as exc:
                    log(f"controller mode: {exc}")
                log("device controller mode: %s" % ("ON" if on else "off"))

            candidate.on_grab = _controller
            if candidate.start():
                web = candidate
        log(f"  web    : {'on' if web else 'DISABLED'}")

    socket_server = SocketServer(args.sock, registry, on_update, on_haptic)
    button_reader = ButtonReader(link, screen)
    button_reader.on_ack = on_ack
    button_reader.bridge = web
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
        # This reaches wireless devices too, so a battery companion shows
        # "daemon stopped" instead of a frozen frame until its own watchdog trips.
        try:
            link.write_line("V|OFF")
            link.write_line("F|0|-1|MATE|daemon stopped||")
        except Exception:
            pass
        if web is not None:
            web.stop()
        if mdns is not None:
            mdns.stop()
        link.close()      # LinkHub.close() also stops the TCP listener
    return 0


if __name__ == "__main__":
    sys.exit(main())
