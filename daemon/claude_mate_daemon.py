#!/usr/bin/env python3
"""
Claude Mate daemon
==================

The "brain" of the Claude Mate USB hardware companion. It:

  1. Listens on a Unix-domain socket (CLAUDE_MATE_SOCK) for status lines emitted
     by Claude Code hooks / the PTY wrapper:

         <state>|<session_id>|<name>[|<ctrl_sock>|<model>|<effort>|<account>
          |<limit>|<wf>|<hist>]

     Hooks send the first three fields, the wrapper all ten. <wf> is the
     session's dynamic workflows: up to three records joined by '~', each
     st;done;total;failed;pct;elapsed_s;tokens;note;name with st one of
     R E P D F -- or '-' for none, or empty for "no change" (parse_wf).
     <hist> is one percent-encoded JSON object: the Claude Code conversation
     the session is running, its directory, its login and the launch flags a
     resume replays -- each key absent until known (parse_history_field).
     It is what the chat history keeps (HistoryWriter).
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
     is a dumb renderer; ALL layout/ordering/selection lives here -- with one
     exception, the workflow band. When the shown session has a workflow, the
     frame sets flags bit2 and is preceded by

         J|<st>|<done>|<failed>|<total>|<info>|<name>

     whose counts each device draws as a bar in its own geometry (info and
     name are still composed here); 'J|-' is sent once when there is none.
     The F commits the J: the firmware draws nothing from a J alone.
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
import errno
import glob
import hmac
import json
import os
import queue
import re
import secrets
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import wave
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, List, NamedTuple, Optional, Set,
                    Tuple, Union)

# --------------------------------------------------------------------------- #
# Crash-loop damage control -- BEFORE the first import that can fail
# --------------------------------------------------------------------------- #
# THIS RUNS FIRST ON PURPOSE. launchd opens our stderr (StandardErrorPath) and
# restarts us forever (KeepAlive), so a daemon that dies during import writes
# the same traceback every ThrottleInterval seconds with nothing to stop it. One
# machine reached 1786 restarts and a 50 MB log that way. A cap placed after the
# imports would never have run: the crash was `import serial` itself.
#
# TRUNCATE, DO NOT RENAME. launchd holds the fd, so renaming the file just means
# we keep writing to the same inode under its new name and the visible log stays
# empty forever. ftruncate on the fd we were handed is the only thing that works,
# and with O_APPEND the next write restarts at zero.
LOG_MAX_BYTES = int(os.environ.get("CLAUDE_MATE_LOG_MAX", 8 * 1024 * 1024))


def _cap_crash_log() -> None:
    """Keep a crash loop from eating the disk. Best effort, never fatal."""
    if LOG_MAX_BYTES <= 0:                      # opt out with CLAUDE_MATE_LOG_MAX=0
        return
    try:
        import stat
        st = os.fstat(2)
        if not stat.S_ISREG(st.st_mode) or st.st_size <= LOG_MAX_BYTES:
            return
        os.ftruncate(2, 0)
        os.lseek(2, 0, os.SEEK_SET)
        sys.stderr.write(
            f"[claude-mate] log passed {LOG_MAX_BYTES} bytes and was truncated. "
            f"If this keeps happening the daemon is crash-looping -- the error "
            f"is whatever follows this line.\n"
        )
    except Exception:                           # a pipe, a closed fd, a tty: fine
        pass


_cap_crash_log()

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover - friendly error if dependency missing
    # NAME THE INTERPRETER. The failure this message used to produce was
    # "pyserial is not installed" on a machine where pyserial *was* installed --
    # in the site-packages of the Python the LaunchAgent no longer runs, because
    # /usr/local/bin/python3 is a floating symlink that a python.org update
    # repointed at a fresh version. Without sys.executable in the message there
    # is nothing to tell those two situations apart.
    sys.stderr.write(
        "[claude-mate] ERROR: pyserial is not installed for this interpreter.\n"
        f"[claude-mate]   interpreter: {sys.executable}\n"
        f"[claude-mate]   fix:         {sys.executable} -m pip install -r "
        "daemon/requirements.txt\n"
        "[claude-mate] If you installed it before and it worked, a Python "
        "upgrade probably\n"
        "[claude-mate] moved the interpreter out from under the LaunchAgent. "
        "Re-run install/install.sh,\n"
        "[claude-mate] which pins the daemon to a virtualenv it owns.\n"
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
# ...and bound the peers that have NOT authenticated yet, which is the number an
# attacker controls. Generous next to one real device, trivial next to the
# thousands a laptop can open, and every slot is released within
# NET_AUTH_TIMEOUT_S whatever the peer does or does not send.
NET_MAX_PENDING = 8
MDNS_SERVICE = "_claudemate._tcp"   # advertised via macOS dns-sd so the device
# --- Which networks the listener is allowed to exist on -------------------- #
# THE LISTENER USED TO BE UNCONDITIONAL, and its own docstring explained why
# that was only ever half a design: "the payload itself is plaintext... use it
# on a network you trust". It said nothing about what happens when you carry the
# Mac somewhere else, and the answer was that port 8787 opened itself to a cafe
# exactly as readily as to your living room, and dns-sd announced it to everyone
# on the segment while it did.
#
# A NETWORK IS IDENTIFIED BY ITS GATEWAY'S MAC ADDRESS. Not by SSID: modern
# macOS redacts the SSID and BSSID from every API a daemon can reach without
# Location Services permission, which a LaunchAgent cannot raise a prompt for.
# Not by subnet either -- half the cafes in the world are 192.168.1.0/24, and so
# is half of everyone's house. The gateway's MAC is available from the routing
# table plus the ARP cache, needs no permission, no sudo and no dependency, and
# it actually distinguishes your router from a stranger's.
#
# BE HONEST ABOUT WHAT THIS IS. It is a strong practical control and NOT a
# cryptographic one: someone already on your LAN who knows your home gateway's
# MAC could forge it. It stops the daemon from offering itself to networks you
# have never seen; it does not turn a hostile LAN into a safe one. The plaintext
# threat model above still applies on any network you do trust.
TRUSTED_FILE = "~/.config/claude-mate/trusted-networks"
NET_GATE_POLL_S = 20.0      # how often to re-check which network we are on

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
NOTICE_S = 4.0              # how long a transient message holds the middle two
                            # rows. Long enough to read at arm's length after
                            # looking down at the press, short enough that the
                            # screen is never lying about a session for long.
PING_PERIOD = 15.0          # keepalive ping interval
RECONNECT_DELAY = 2.0       # wait between serial (re)connection attempts
SESSION_DONE_TTL = 120.0    # drop a 'done' session after this long with no update
SESSION_IDLE_TTL = 600.0    # drop any stale session after this long

# Screen text geometry (must match the firmware's fixed 4-row layout, all
# size-1: r0 name / r1 state+time / r2 model+effort / r3 position + fleet).
ROW_CHARS = 21              # size-1 rows are 21 chars wide (128 / 6px)

# ---- Workflows (Claude Code's dynamic workflows, read off its footer) ----- #
# The wrapper lists a session's workflows in field 8 of its line (parse_wf);
# the device draws the shown session's primary one as a band, from a J| line
# the frame commits (Screen._wf_view). Every knob is a module constant on
# purpose: there is nothing here a user should have to configure.
WF_INFO_CHARS = 14          # J| info: the firmware's WF_INFO_MAX (workflow.h)
WF_NAME_CHARS = 40          # J| name: the S3's WF_NAME_MAX (workflow.h)
WF_RECORDS = 3              # the wire's cap: field 8 lists at most this many
WF_STALE_S = 90.0           # a wrapper's running/paused record nobody has
                            # re-listed for this long is gone -- its 20 s
                            # keepalive missed four times over
WF_LINGER_S = 60.0          # a finished record stays visible this long...
WF_SPOTLIGHT_S = 10.0       # ...and takes the band from a running one for
                            # this long, so a run ending beside another one is
                            # seen to end rather than vanishing under it
WF_RESEND_S = 1.0           # while the band is up, its J+F pair goes out at
                            # least this often, changed or not: a lost J then
                            # costs a second or two (the 1 Hz Ticker's next
                            # call past this age), not the minute a frame at
                            # h:mm and a finished run's J can sit unchanged

# ---- Chat history (field 9 in, HistoryWriter out) ------------------------- #
# One record per Claude Code conversation a wrapped session ran, kept until the
# user deletes it -- in the CLI's own process, never over the socket. Next to
# the token and the trusted networks, NOT under ~/Library/Caches: macOS may
# purge a cache, and this is the one thing here whose whole job is to outlive
# the session, the daemon and the reboot. Resolved in main(), at startup.
HISTORY_DIR = "~/.config/claude-mate/history"
HISTORY_DEBOUNCE_S = 0.5    # a burst of reports -- a turn starting is a state
                            # change and a meta change in quick succession --
                            # costs one write, not one per line
HISTORY_JOIN_S = 5.0        # main() waits this long for the writer's final
                            # flush (the last `end`s) before exiting anyway
HISTORY_TMP_STALE_S = 600.0  # a temp file this old was left by a killed
                             # writer: a live one exists for milliseconds
HISTORY_READ_MAX = 65536    # a record is about 1 KB; past this it is not one
HISTORY_FIELD_MAX = 65536   # field 9 as received: 16x what the wrapper ever
                            # sends (it caps the field at 4096 bytes), a bound
                            # on work for the socket thread, not a format rule
HISTORY_PATH_MAX = 4096     # cwd / config dir in field 9: PATH_MAX
HISTORY_FLAGS_MAX = 64      # launch flags in field 9: at most this many...
HISTORY_FLAGS_CHARS = 4096  # ...and this many characters between them

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
# Workflows: what a session's dynamic workflows are doing
# --------------------------------------------------------------------------- #
# A WORKFLOW IS AN ATTRIBUTE OF A SESSION, NOT A STATE. The session stays WORK
# (or WAIT, or whatever it is) and the workflow rides beside it: nothing in
# here touches state, state_since, acked, the haptic return or the LED, so a
# run finishing can never buzz, and a stale record can never hold a session
# on anything.

_WF_STATES = "REPDF"        # R running, E running with a failed agent, P paused
                            # on a usage limit, D completed, F ended without
                            # completing (failed or stopped: the screen cannot
                            # tell which)
_WF_LIVE = "REP"            # still in flight
_WF_CLOCKED = "RE"          # ...with a clock that is still running
_WF_ENDED = "DF"
_WF_WORD = {"R": "running", "E": "running", "P": "paused", "D": "done",
            "F": "ended"}


class WfRec(NamedTuple):
    """One workflow, as the session's line last listed it. Immutable: a
    session's tuple of these is replaced whole under Registry._lock and read
    without it, like every other field _compose reads."""
    st: str                             # one of _WF_STATES
    done: Optional[int] = None          # agents finished      } None =
    total: Optional[int] = None         # agents in the run    } the screen
    failed: Optional[int] = None        # agents that failed   } did not say
    pct: Optional[int] = None           # the bar's fill, 0..100
    elapsed_s: Optional[int] = None     # the run's own clock when listed
    tokens: Optional[int] = None
    note: str = ""                      # a paused run's "til 3:30pm"
    name: str = ""
    live: bool = False                  # listed by a wrapper (ctrl socket set)
    seen_ts: float = 0.0                # the last line that listed it
    anchor: Optional[float] = None      # R/E: when its clock read zero
    ended_ts: Optional[float] = None    # D/F: when it was first seen ended
    ended_seen: bool = False            # D/F: and the end itself was seen --
                                        # the line before listed it R/E/P


def _wf_clean(text: str) -> str:
    """Control characters out, everything else kept. The name is stored as the
    session named it (the CLI prints it); the device's ASCII is made at render
    time, by _wf_ascii."""
    return "".join(ch for ch in (text or "")
                   if ch >= " " and not ("\x7f" <= ch <= "\x9f")).strip()


def _wf_num(text: str) -> Optional[int]:
    """A record's number: '' is unknown (None), ASCII digits are a value, and
    anything else raises -- a record with a garbled count is a malformed
    record, not one with a zero in it. Bounded so a hostile line cannot make
    int() do quadratic work."""
    text = text.strip()
    if not text:
        return None
    if len(text) > 18 or not (text.isascii() and text.isdigit()):
        raise ValueError(f"not a count: {text!r}")
    return int(text)


def parse_wf(text: Optional[str]) -> Optional[List[WfRec]]:
    """Field 8 of a session line -> its workflow records.

        wf  := '-' | rec ('~' rec){0,2}
        rec := st;done;total;failed;pct;elapsed_s;tokens;note;name

    Returns None for KEEP (no field, an empty one, or one with no valid record
    in it -- the same "empty means no change" rule every other trailing field
    follows), [] for CLEAR ('-'), and otherwise up to WF_RECORDS records with
    only the producer's fields set; Registry.update fills in the rest.

    A record with an unknown status letter or a malformed count is skipped,
    not guessed at: a newer producer's new letter must not draw as something
    it is not. The name is the LAST sub-field and is split off with
    maxsplit=8, so it may contain ';'."""
    text = (text or "").strip()
    if not text:
        return None
    if text == "-":
        return []
    out: List[WfRec] = []
    for raw in text.split("~"):
        parts = raw.split(";", 8)
        if len(parts) != 9:
            continue
        st = parts[0].strip()
        if len(st) != 1 or st not in _WF_STATES:
            continue
        try:
            done, total, failed, pct, elapsed, tokens = (
                _wf_num(p) for p in parts[1:7])
        except ValueError:
            continue
        out.append(WfRec(
            st=st, done=done, total=total, failed=failed,
            pct=None if pct is None else min(pct, 100),
            elapsed_s=elapsed, tokens=tokens,
            note=_wf_clean(parts[7])[:WF_INFO_CHARS],
            # A bound on memory, not a display rule: the producer sends <= 40
            # characters, and every renderer cuts to its own width.
            name=_wf_clean(parts[8])[:4 * WF_NAME_CHARS]))
        if len(out) == WF_RECORDS:
            break
    return out or None


def _wf_keyed(recs) -> List[Tuple[Tuple[str, int], WfRec]]:
    """[((name, k), rec)] where k counts earlier records of the same name.

    THE NAME ALONE IS NOT AN IDENTITY: two runs of one script are two
    workflows with one name, and each needs its own clock and its own linger.
    The k-th 'sweep' in this line is the k-th 'sweep' in the last one."""
    seen: Dict[str, int] = {}
    out = []
    for r in recs:
        k = seen.get(r.name, 0)
        seen[r.name] = k + 1
        out.append(((r.name, k), r))
    return out


def wf_merge(prev, recs: List[WfRec], now: float,
             live: bool) -> Tuple[WfRec, ...]:
    """The session's new tuple: `recs` as listed, carrying over what only the
    daemon knows about each one.

    ANCHOR (the clock of a running record). Every line re-lists the run's
    elapsed time, and the wrapper only reads it off the screen, which repaints
    once a second -- so a fresh `now - elapsed_s` jitters by up to a second
    from line to line. Keeping the EARLIEST anchor while a record stays R/E
    makes the shown clock monotonic: a lagging scrape can never step it back.
    Re-entering R/E from P/D/F, or appearing new, starts a fresh anchor.

    ENDED_TS (when a record was first seen D/F) is carried for as long as it
    stays D/F -- including after its linger has expired, which is what keeps
    a finished row Claude is still showing from reappearing for another
    minute on every keepalive.

    ENDED_SEEN says the END itself was seen: the same key was R/E/P in the
    last line, and is D/F in this one. Only such a record may take the
    spotlight (wf_primary). One that arrives already finished -- after a
    daemon restart, a fourth run the wire's three-record window kept off
    until another one ended, a name Claude cut afresh with '…' on a resize
    -- ended at a moment nobody here saw, possibly long ago, and announcing
    it as "just finished" would name the wrong run. It still lingers
    WF_LINGER_S from first sight, like any finished record."""
    old = dict(_wf_keyed(prev))
    out = []
    for key, r in _wf_keyed(recs):
        p = old.get(key)
        anchor = ended = None
        seen_end = False
        if r.st in _WF_CLOCKED:
            base = p.anchor if (p is not None and p.st in _WF_CLOCKED) else None
            if r.elapsed_s is not None:
                fresh = now - r.elapsed_s
                anchor = fresh if base is None else min(base, fresh)
            else:
                anchor = base       # a clock the screen dropped (it narrowed)
                                    # is still running
        elif r.st in _WF_ENDED:
            if p is not None and p.st in _WF_ENDED and p.ended_ts is not None:
                # Still the same ending (D -> F included): when it was first
                # seen, and whether the end itself was, carry over.
                ended, seen_end = p.ended_ts, p.ended_seen
            else:
                ended = now
                seen_end = p is not None and p.st in _WF_LIVE
        out.append(r._replace(live=live, seen_ts=now, anchor=anchor,
                              ended_ts=ended, ended_seen=seen_end))
    return tuple(out)


def _wf_changes(prev, new) -> List[Tuple[str, str, str]]:
    """(name, old st, new st) for every record that started, finished or
    changed status -- '-' for one that was not there. What the log says, so
    a keepalive that only moved a clock says nothing."""
    old = dict(_wf_keyed(prev))
    cur = dict(_wf_keyed(new))
    out = []
    for key, r in _wf_keyed(new):
        p = old.get(key)
        if p is None or p.st != r.st:
            out.append((r.name, p.st if p is not None else "-", r.st))
    for key, p in _wf_keyed(prev):
        if key not in cur:
            out.append((p.name, p.st, "-"))
    return out


def wf_visible(rec: WfRec, now: float) -> bool:
    """Whether a record is shown anywhere: the band, bit2, the queue JSON.

    Pure, and the only place this is decided, so the frame, its J, the CLI
    and prune() can never disagree about it.

      * R/E/P from a wrapper: until WF_STALE_S after the last line that listed
        it. A wrapper re-lists every 20 s, so a record that stops being listed
        without a '-' belongs to a wrapper that died.
      * R/E/P from anything else (a hook): no keepalive to go stale against,
        so it stands until a line replaces it, or its session goes.
      * D/F: for WF_LINGER_S after it was first seen ended. Past that it stays
        in the tuple as a hidden tombstone while the producer still lists it,
        so the next keepalive cannot resurrect it with a fresh ended_ts."""
    if rec.st in _WF_LIVE:
        return (not rec.live) or (now - rec.seen_ts <= WF_STALE_S)
    return rec.ended_ts is not None and now - rec.ended_ts <= WF_LINGER_S


def wf_elapsed(rec: WfRec, now: float) -> Optional[float]:
    """The run's clock as shown: still ticking for R/E (from its anchor, so
    the daemon needs no per-second line), frozen for P/D/F, None if the screen
    never showed one."""
    if rec.st in _WF_CLOCKED:
        return None if rec.anchor is None else max(0.0, now - rec.anchor)
    return None if rec.elapsed_s is None else float(rec.elapsed_s)


def wf_primary(recs, now: float) -> Tuple[Optional[WfRec], int]:
    """(the record the band shows, how many other visible records there are).

    A record that ENDED in the last WF_SPOTLIGHT_S takes the band first -- the
    most recently ended one -- or a run finishing beside a running one would
    be over before anyone saw it end. Only an end the daemon SAW counts
    (ended_seen): a record that turned up already finished has no moment of
    ending to announce, and spotlighting it would put a stale run over a live
    one as though it had just finished. Then the first one still in flight,
    in the producer's (on-screen) order; then the first finished one."""
    vis = [r for r in recs if wf_visible(r, now)]
    if not vis:
        return (None, 0)
    fresh = [r for r in vis if r.st in _WF_ENDED and r.ended_seen
             and r.ended_ts is not None and now - r.ended_ts <= WF_SPOTLIGHT_S]
    if fresh:
        pick = max(fresh, key=lambda r: r.ended_ts)
    else:
        going = [r for r in vis if r.st in _WF_LIVE]
        pick = going[0] if going else vis[0]
    return (pick, len(vis) - 1)


_wf_fault_count = [0]


def _wf_fault(where: str, exc: BaseException) -> None:
    """A workflow bug costs the workflow, never the frame or the session line.
    Every caller fences its workflow code and lands here, and this says so --
    a few times, with the traceback, and then stops repeating itself."""
    _wf_fault_count[0] += 1
    if _wf_fault_count[0] <= 5:
        log(f"workflow {where} failed ({type(exc).__name__}: {exc}); "
            f"carrying on without it\n{traceback.format_exc()}")


# --------------------------------------------------------------------------- #
# Faults that are contained -- and still heard about
# --------------------------------------------------------------------------- #
# NOT "FIVE, THEN SILENCE". _wf_fault's rule suits a parser that would print
# the same odd field forever. It is wrong where silence IS the failure: a
# socket line that faults once can fault on every line after it, and a history
# record that cannot be written stays unwritten -- the sixth failure is as much
# news as the first. So these are logged at most once a minute PER KEY (a
# record, the socket), each repeat with a count of what it skipped, and the
# traceback only the first time a key is heard from.
FAULT_LOG_GAP_S = 60.0

_fault_lock = threading.Lock()
_fault_seen: Dict[str, Tuple[float, int]] = {}  # key -> (last logged, skipped)


def _fault(key: str, what: str, exc: BaseException) -> None:
    """Log a contained fault, rate-limited per `key`. Call it from the
    `except` that caught `exc`: the traceback it prints is the one being
    handled. Takes no lock but its own and never raises into its caller."""
    try:
        now = time.time()
        with _fault_lock:
            prev = _fault_seen.get(key)
            if prev is not None and now - prev[0] < FAULT_LOG_GAP_S:
                _fault_seen[key] = (prev[0], prev[1] + 1)
                return
            if prev is None and len(_fault_seen) >= 256:
                _fault_seen.clear()                 # a bound, not a policy
            _fault_seen[key] = (now, 0)
        if prev is None:
            tail = "\n" + traceback.format_exc()
        else:
            tail = f" (again; {prev[1]} more since it was last logged)"
        log(f"{what} failed ({type(exc).__name__}: {exc}); carrying on{tail}")
    except Exception:
        pass


def _sock_fault(exc: BaseException) -> None:
    """One socket line failed: the line is lost, the socket thread is not
    (SocketServer._serve says why that matters)."""
    _fault("socket", "socket line", exc)


def _history_fault(where: str, exc: BaseException, key: str = "") -> None:
    """A history step failed. It costs the history -- that record, that
    event -- and never the state report, the frame or the thread."""
    _fault("history:" + (key or where), f"history {where}", exc)


# --------------------------------------------------------------------------- #
# Field 9: which conversation a wrapped session is running
# --------------------------------------------------------------------------- #
# The wrapper's key is its own 8-hex SID, new on every launch, every account
# switch and every resume; field 9 names the thing those all continue -- Claude
# Code's conversation id, the one `claude --resume` takes -- and where, and as
# whom, and how it was started. The chat history is built from nothing else.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _hist_text_ok(value: Any, limit: int) -> bool:
    """A str of at most `limit` characters with no C0 control and no DEL --
    a NUL in a path would make every os call on it raise ValueError, and a
    newline would forge a line in anything that prints it."""
    return (isinstance(value, str) and len(value) <= limit
            and not any(ch < " " or ch == "\x7f" for ch in value))


def _hist_path_ok(value: Any) -> bool:
    """An absolute path as the wrapper sends it. A lone surrogate passes on
    purpose: that is how json carries a directory name that is not UTF-8, and
    it has to come back as the same bytes for anything to find the directory."""
    return _hist_text_ok(value, HISTORY_PATH_MAX) and value.startswith("/")


def parse_history_field(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Field 9 of a wrapper line -> {"id", "cwd", "config", "flags"}, or None.

        hist := quote(json.dumps({"id": uuid, "cwd": path,
                                  "config": "" | path, "flags": [str, ...]}),
                      safe="")

    A key that is absent means "unknown / unchanged", and so does a key that
    fails its check: EACH KEY STANDS ALONE, so flags over their budget cost the
    flags and never the conversation id beside them. The dict always has all
    four keys, None for each that is absent or invalid; None for the whole
    field when it is empty or not a JSON object (a field from a newer producer
    included). The id is lowercased -- claude names the transcript in lowercase
    and the record is named after it; config "" is the default login.

    Pure, and it NEVER raises: it runs on the socket thread, inside the line
    whose state report must go through whatever this field holds."""
    try:
        text = (text or "").strip()
        if not text or len(text) > HISTORY_FIELD_MAX:
            return None
        obj = json.loads(urllib.parse.unquote(text))
        if not isinstance(obj, dict):
            return None
        out: Dict[str, Any] = {"id": None, "cwd": None, "config": None,
                               "flags": None}
        value = obj.get("id")
        if (isinstance(value, str) and value.isascii()
                and _UUID_RE.fullmatch(value.lower())):
            out["id"] = value.lower()
        value = obj.get("cwd")
        if _hist_path_ok(value):
            out["cwd"] = value
        value = obj.get("config")
        if value == "" or _hist_path_ok(value):
            out["config"] = value
        value = obj.get("flags")
        if (isinstance(value, list) and len(value) <= HISTORY_FLAGS_MAX
                and all(_hist_text_ok(f, HISTORY_FLAGS_CHARS) for f in value)
                and sum(len(f) for f in value) <= HISTORY_FLAGS_CHARS):
            out["flags"] = tuple(value)
        return out
    except Exception:
        return None


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
    # Its dynamic workflows (PTY wrapper), as last listed. Replaced WHOLE under
    # Registry._lock and read without it: one reference read of an immutable
    # tuple, which is how _compose reads every other field here.
    workflows: Tuple[WfRec, ...] = ()
    # What the chat history needs (field 9, wrapper lines only), sticky like
    # account and limit: a line that does not carry one changes nothing.
    conversation: str = ""         # Claude Code's id, the one --resume takes
    config_dir: Optional[str] = None   # the login: None unknown, "" the
                                       # default (~/.claude), else the path
    flags: Optional[Tuple[str, ...]] = None  # launch flags a resume replays
    # A TURN RAN, and when the latest one started: set on a transition INTO
    # working, never by a keepalive. The history creates a record only for a
    # conversation that ran a turn here, and takes its directory, login and
    # flags only from one -- a `--resume <id>` started in the wrong directory
    # or under the wrong login is refused by claude before it runs anything,
    # and must not repoint the record at where it could not be found.
    turned: bool = False
    turn_ts: float = 0.0

    def history_snapshot(self) -> Dict[str, Any]:
        """What the history writer needs of this session, as a plain dict it
        can keep after the session is gone (Registry.snapshot, and `end`)."""
        return {"key": self.key, "name": self.name, "cwd": self.cwd,
                "conversation": self.conversation,
                "config_dir": self.config_dir, "flags": self.flags,
                "account": self.account, "turned": self.turned,
                "turn_ts": self.turn_ts}

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

    @staticmethod
    def key_for(sid: str, name: str) -> str:
        """The key a session line is filed under: its id, else its name."""
        return sid if sid else (name if name else "unknown")

    def update(self, state: str, sid: str, name: str, cwd: str = "",
               focus_ctrl: str = "", model: str = "",
               effort: str = "", account: str = "",
               limit: str = "", wf: Optional[str] = None,
               now: Optional[float] = None, conversation: str = "",
               config_dir: Optional[str] = None,
               flags: Optional[Tuple[str, ...]] = None) -> Optional[str]:
        """Apply a status update from a hook, the PTY wrapper, or the mock injector.

        Returns the one-shot LED KIND for this session's own transition
        (START/DONE/INPUT/ERROR), or None for a keepalive / silent change.

        Models "finished but not yet acknowledged": a turn ending (working ->
        idle) becomes 'done' and STAYS 'done' (alerting) until the user
        acknowledges it; later idle keepalives must not clear it.

        `wf` is the line's field 8 (see parse_wf): None or '' keeps the
        session's workflows, '-' clears them, records replace them. It never
        touches anything else here -- not the state, not the ack, not the
        return value. `now` exists for tests; everything else passes nothing.

        `cwd`, `conversation`, `config_dir` and `flags` come from field 9 (see
        parse_history_field): "" or None keeps what the session has, a value
        replaces it -- config_dir "" is a value (the default login), and so
        is an empty flags tuple (started with none). Nothing else here reads
        them; the history takes them from Registry.snapshot().
        """
        if state not in VALID_STATES:
            log(f"ignoring update with invalid state: {state!r}")
            return None
        key = self.key_for(sid, name)
        now = time.time() if now is None else now
        # Parsed before the lock (it is pure) and fenced: a workflow field
        # nobody anticipated costs the workflows, never the state report.
        try:
            recs = parse_wf(wf)
        except Exception as exc:
            _wf_fault("parse", exc)
            recs = None
        changes: List[Tuple[str, str, str]] = []
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
            if conversation:
                sess.conversation = conversation
            if config_dir is not None:
                sess.config_dir = config_dir
            if flags is not None:
                sess.flags = flags
            if recs is not None:
                try:
                    new = (wf_merge(sess.workflows, recs, now,
                                    live=bool(focus_ctrl)) if recs else ())
                    changes = _wf_changes(sess.workflows, new)
                    sess.workflows = new
                except Exception as exc:
                    _wf_fault("update", exc)
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
                    sess.turned = True           # a turn started: the history
                    sess.turn_ts = now           # may keep this conversation
                    haptic = "START"             # job (re)started: calm tick
                else:                            # idle
                    sess.acked = True
        # ONE line per start or finish, never per keepalive: the per-line
        # 'socket update' log is already most of the log file, and a workflow
        # line every 20 s for a 28-minute median run would be the rest of it.
        for wname, was, now_st in changes:
            log(f'workflow: {sess.name} "{wname}" {was} -> {now_st}')
        return haptic

    def remove(self, key: str) -> Optional[Session]:
        """Drop a session and hand it back (None if it was not there): an
        `end` is recorded in the history from what the session WAS."""
        with self._lock:
            return self._sessions.pop(key, None)

    def snapshot(self, key: str) -> Optional[Dict[str, Any]]:
        """One session as the history needs it (Session.history_snapshot),
        copied under the lock so it is one moment, never half of an update."""
        with self._lock:
            sess = self._sessions.get(key)
            return sess.history_snapshot() if sess is not None else None

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

    def prune(self, now: Optional[float] = None) -> None:
        """Drop stale/finished sessions so the queue stays tidy -- and the
        running workflows of a wrapper that stopped listing them.

        Only stale R/E/P records go here. A finished record past its linger
        is a tombstone that stays for as long as its producer lists it (see
        wf_visible), so dropping it here would let the next keepalive bring it
        back as new."""
        now = time.time() if now is None else now
        stale: List[Tuple[str, WfRec]] = []
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
            for s in self._sessions.values():
                gone = [r for r in s.workflows
                        if r.st in _WF_LIVE and not wf_visible(r, now)]
                if gone:
                    s.workflows = tuple(r for r in s.workflows
                                        if not any(r is g for g in gone))
                    stale.extend((s.name, r) for r in gone)
        for sname, r in stale:
            log(f'workflow: {sname} "{r.name}" {r.st} -> - (not listed for '
                f'{int(now - r.seen_ts)} s)')

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
        self._provision_token: Optional[str] = None
        self._pending_provision = False   # armed on open, spent on the first K/H

    def set_provision_token(self, token: Optional[str]) -> None:
        """The secret to hand a device that arrives on the cable. See _provision().

        Set only when a wireless transport is up, because that is the only time
        there is a shared secret to share -- a USB-only daemon has nothing to
        provision the device FOR.
        """
        self._provision_token = token

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
                #
                # The token is NOT sent yet -- see provision_if_ours(). A `P`
                # asks whoever is on this port to identify itself; a Claude Mate
                # answers `K` (or `H` on boot), and only then does a secret go
                # down the wire.
                self._pending_provision = True
                self._write_locked("P")
                return True
            except (serial.SerialException, OSError) as exc:
                log(f"serial open failed on {port}: {exc}")
                self._close_locked()
                return False

    def provision_if_ours(self) -> None:
        """The device answered the protocol, so it is safe to give it the token.

        Called by the reader when a `K` or `H` arrives. THE HANDSHAKE IS THE
        POINT: autodetect() takes the first match of a glob, and PORT_GLOBS tries
        `/dev/cu.usbserial*` BEFORE the `/dev/cu.usbmodem*` the S3 actually
        enumerates as -- so any FTDI or CH340 dongle on the desk outranked the
        real device and was handed the shared secret, on every reconnect. It
        cannot answer `K`, so it no longer gets one.
        """
        with self._lock:
            if not self._pending_provision:
                return
            self._pending_provision = False
            self._provision_locked()

    def _write_locked(self, line: str) -> None:
        """Write with the lock already held. Failures are the caller's problem."""
        if not self.is_open():
            return
        try:
            self._ser.write((line + "\n").encode("ascii", errors="replace"))
        except (serial.SerialException, OSError):
            pass

    def _provision_locked(self) -> None:
        """Give the device on the cable this daemon's token, once per open.

        THE CABLE IS THE ANSWER TO "how do I connect it after a factory reset",
        and it should not involve the human at all. A wiped board comes up on
        BLE with no token; the daemon connects, gets A|NOTOKEN, and both ends
        then sit there knowing exactly what is wrong and doing nothing about it
        -- while a USB cable, over which provisioning is already the documented
        and trusted path, is plugged into the same two devices.

        Unconditional rather than conditional on "does it need one", because
        there is no way to ask over this link (USB has no handshake -- it is
        trusted by being physical) and because the daemon's token is the
        authority: a device holding a different one is a device that cannot
        link, so overwriting is the repair, not a side effect. NVS skips a write
        whose value is unchanged, so the steady state costs nothing.

        T| is a CONFIG verb, handled before the protocol on the firmware side,
        and the Nano ignores it as an unknown line -- so this is safe to send to
        whatever happens to be on the port.
        """
        token = self._provision_token
        if not token or os.environ.get("CLAUDE_MATE_NO_USB_PROVISION") == "1":
            return
        # THE PORT CAN CLOSE BETWEEN THE ARM AND THE WRITE, and it is not a rare
        # window: the arm is spent by a K/H reaching the ButtonReader, which the
        # hub forwards here from WHATEVER transport it arrived on -- so a device
        # that drops off the cable and says hello over the radio a few seconds
        # later lands exactly here with `self._ser` already None. That is an
        # AttributeError, not a SerialException, so the except below did not
        # catch it and it killed the reader thread. Observed in the wild: a
        # serial read failure at 21:37:42 and a BLE connect at 21:37:48 left the
        # daemon running with every device button dead.
        if not self.is_open():
            return
        try:
            self._ser.write(f"T|{token}\n".encode("ascii", errors="replace"))
        except (serial.SerialException, OSError) as exc:
            # Not fatal and not worth closing the port over: the link still
            # works, it just may not be able to authenticate over the radio.
            log(f"serial: could not hand the device a token: {exc}")
            return
        log("serial: handed the device this daemon's token (USB provisioning)")

    def _close_locked(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        # The arm belongs to the open that made it. Leaving it set means the
        # next K/H -- possibly from a device that is now on the radio, with no
        # cable at all -- tries to provision a port that is gone; ensure_open()
        # re-arms it if and when the cable comes back.
        self._pending_provision = False

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
        self._pending = 0                       # connections still handshaking
        self._no_token_logged = 0.0             # throttle for _log_no_token

    # ---- lifecycle -------------------------------------------------------- #

    def start(self) -> bool:
        """Bind + listen and start accepting. False if the port is unusable.

        RESTARTABLE, because NetworkGate closes the listener when the Mac moves
        to an untrusted network and opens it again when it comes home. stop()
        sets _stop_evt and nothing used to clear it, so the second start() built
        a listener and handed it to an accept loop whose very first `while not
        self._stop_evt.is_set()` was already false -- a socket bound, listening,
        and attached to nobody, which is worse than a failure because it looks
        exactly like success.
        """
        self._stop_evt.clear()
        # Drop the corpses of previous connections. This list is appended to
        # once per accepted connection and never pruned, so on a long-lived
        # daemon it is an unbounded list of dead Thread objects -- slow-motion
        # memory growth that anyone able to reach the port can drive.
        self._threads = [t for t in self._threads if t.is_alive()]
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
            # SHUTDOWN BEFORE CLOSE, and it is not belt-and-braces. close() on
            # a listening socket does not wake a thread already blocked in
            # accept() on Linux, and the kernel keeps the socket serving until
            # that thread lets go -- so "the listener is closed" was true of our
            # bookkeeping and false of the port for up to one accept timeout.
            # For a control whose whole job is to shut the door when the Mac
            # joins a network you do not trust, "closed in a moment" is not the
            # same promise as "closed". shutdown() wakes the accept immediately;
            # it is meaningless on a listening socket on some platforms, hence
            # the guard rather than a check.
            try:
                self._srv.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
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
            # THE CAP USED TO COUNT THE WRONG THING. _clients holds peers that
            # have ALREADY authenticated, so a peer that never finishes the
            # handshake was never counted -- and every accept span a thread that
            # lived for the full NET_AUTH_TIMEOUT_S. Anyone who could reach the
            # port could therefore hold an unbounded number of threads open by
            # connecting and saying nothing, without knowing the token at all.
            # Bounding connections IN THE HANDSHAKE is the check that matters;
            # the authenticated cap below stays as the fan-out bound it always
            # was.
            with self._lock:
                too_many = (len(self._clients) >= NET_MAX_CLIENTS
                            or self._pending >= NET_MAX_PENDING)
                if not too_many:
                    self._pending += 1
            if too_many:
                log(f"TCP refused {addr[0]}: {len(self._clients)} linked, "
                    f"{self._pending} in handshake")
                self._shutdown(conn)
                continue
            t = threading.Thread(target=self._serve, args=(conn, addr),
                                 name="net-client", daemon=True)
            t.start()
            with self._lock:
                self._threads = [x for x in self._threads if x.is_alive()]
                self._threads.append(t)

    def _authenticate(self, conn: socket.socket, peer: str) -> bool:
        """Nonce challenge / HMAC response. False = reject (caller closes)."""
        nonce = secrets.token_hex(16)
        expect = hmac.new(self._token, nonce.encode("ascii"),
                          "sha256").hexdigest()
        try:
            # One budget for the entire handshake, wall-clock, from before the
            # challenge goes out. See _read_line_blocking().
            deadline = time.monotonic() + NET_AUTH_TIMEOUT_S
            conn.settimeout(NET_AUTH_TIMEOUT_S)
            conn.sendall(f"C|{nonce}\n".encode("ascii"))
            reply = self._read_line_blocking(conn, deadline=deadline)
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
    def _read_line_blocking(conn: socket.socket,
                            deadline: Optional[float] = None) -> Optional[str]:
        """One newline-terminated line, bounded by NET_MAX_LINE. None on EOF.

        `deadline` is an absolute time.monotonic() value bounding the WHOLE
        line, and the handshake passes one.

        WHY A DEADLINE AND NOT JUST A SOCKET TIMEOUT. This reads one byte at a
        time, and a socket timeout re-arms on every successful recv -- so it
        bounds the gap between bytes, not the read. A peer dripping one byte
        every four seconds beat a five-second timeout indefinitely: 512 bytes
        of drip is roughly forty minutes of held thread, per connection, for an
        attacker who never has to know the token. Bounding how MANY such peers
        there can be (NET_MAX_PENDING) does not help if each one holds its slot
        for forty minutes -- together they simply keep the real device out.
        """
        buf = bytearray()
        while len(buf) < NET_MAX_LINE:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("handshake deadline exceeded")
                conn.settimeout(remaining)
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
        # try/finally around the handshake ALONE: the slot being released here
        # is the pre-auth one, and holding it for the lifetime of an established
        # link would mean four healthy devices exhausted the handshake budget
        # and nothing could ever connect again.
        try:
            ok = self._authenticate(conn, peer)
        finally:
            with self._lock:
                self._pending = max(0, self._pending - 1)
        if not ok:
            self._shutdown(conn)
            return
        try:
            # Keepalives so a device that drops off WiFi without a FIN is
            # eventually reaped even if we happen never to write to it.
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            conn.settimeout(None)               # blocking reads from here on
        except OSError:
            pass
        try:
            # NO NAGLE. A workflow band is a J| line written immediately before
            # the F| that commits it, and with Nagle on, that small F waits in
            # the kernel until the device ACKs the J -- which the ESP32's lwIP
            # delays by up to ~250 ms. Every frame would reach a Wi-Fi device
            # a quarter-second late. These are interactive, line-sized writes:
            # exactly what TCP_NODELAY exists for. Its own try, so a stack
            # without it cannot cost the settimeout above.
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
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
    """One device link made of a USB serial port plus any wireless transports.

    Presents exactly the surface the rest of the daemon already used on
    SerialLink (is_open / ensure_open / write_line / read_line / close), so
    Screen, ButtonReader and SerialMaintainer are untouched.

    Frames go to EVERY transport; incoming lines from any of them arrive on one
    queue. A dedicated pump moves serial input onto that queue so a wireless
    button press is never stuck behind a blocking serial read.

    The wireless ones are held as a list rather than as named fields because
    there are two of them now (a TCP listener and a BLE central) and there is
    nothing this class needs to know about either beyond has_clients /
    write_line / stop. Both are optional and either can be absent: --tcp alone,
    --ble alone, both, or a device on a cable and nothing else.
    """

    def __init__(self, serial_link: SerialLink,
                 wireless: List[Any],
                 rx: "queue.Queue[str]") -> None:
        self._serial = serial_link
        self._wireless = [w for w in wireless if w is not None]
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

    def _any_wireless(self) -> bool:
        return any(w.has_clients() for w in self._wireless)

    def is_open(self) -> bool:
        """Is ANY device reachable? A wireless client counts."""
        return self._serial.is_open() or self._any_wireless()

    def ensure_open(self) -> bool:
        """Try to (re)open serial; a connected wireless device also counts as
        up, so startup does not report "no device" when only the radio is in
        use."""
        opened = self._serial.ensure_open()
        return opened or self._any_wireless()

    # The USB port, asked about on its OWN terms. is_open()/ensure_open() above
    # answer "is anything reachable", which is right for startup but WRONG for
    # the maintainer: with a wireless device connected they return True while
    # the serial port is shut, so a Nano that drops -- or is plugged in later --
    # would never be reopened and would sit on NO LINK forever.
    def provision_if_ours(self) -> None:
        """Forward to the serial link. See SerialLink.provision_if_ours().

        The ButtonReader holds whatever `link` main() built -- this hub whenever
        a radio is enabled -- so a serial-only method that is not forwarded here
        is simply never called, silently. That is what happened: the token push
        waited for a `K` that reached a hub with no way to pass it on.
        """
        self._serial.provision_if_ours()

    def serial_is_open(self) -> bool:
        return self._serial.is_open()

    def ensure_serial_open(self) -> bool:
        return self._serial.ensure_open()

    def write_line(self, line: str) -> bool:
        # Deliberately not short-circuiting: every connected device must get
        # every frame, so every write runs. A `or` chain would stop at the first
        # transport that took the bytes and leave a second device showing a
        # frame from a minute ago.
        ok = self._serial.write_line(line)
        for w in self._wireless:
            ok = w.write_line(line) or ok
        return ok

    def read_line(self) -> Optional[str]:
        try:
            return self._rx.get(timeout=0.5)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._stop_evt.set()
        for w in self._wireless:
            w.stop()
        self._serial.close()


# Anything the rest of the daemon needs from a device link.
Link = Union[SerialLink, LinkHub]


def _run_quiet(argv: List[str], timeout: float = 3.0) -> str:
    """Run a read-only system tool and return stdout, or "" if anything fails."""
    try:
        out = subprocess.run(argv, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.decode("utf-8", "replace")


# PROC FIRST, THEN THE BSD TOOLS. This is a macOS product, but the daemon is
# plain Python that people run and test on Linux -- CI does, on every push --
# and `route -n get default` is a BSD-ism that Linux's route(8) does not
# understand. It failed the way an unparseable answer always does: silently, as
# "no gateway", which the gate correctly read as "unidentifiable network" and
# correctly refused to listen on. Correct, and completely useless: the wireless
# link simply never came up, with a security feature quietly holding the door.
# Reading /proc costs no subprocess at all, so it is also the faster path where
# it exists.
def _gateway_from_proc() -> Optional[str]:
    """Linux: the default route's gateway from /proc/net/route, or None."""
    try:
        with open("/proc/net/route", encoding="ascii") as fh:
            next(fh, None)                       # header
            for line in fh:
                parts = line.split()
                if len(parts) < 3 or parts[1] != "00000000":
                    continue                     # not the default route
                # Little-endian hex, so 010011AC is 172.17.0.1.
                raw = int(parts[2], 16)
                if not raw:
                    continue
                return ".".join(str((raw >> (8 * i)) & 0xFF) for i in range(4))
    except (OSError, ValueError):
        pass
    return None


def _mac_from_proc(gw: str) -> Optional[str]:
    """Linux: the gateway's MAC from /proc/net/arp, or None if not cached."""
    try:
        with open("/proc/net/arp", encoding="ascii") as fh:
            next(fh, None)                       # header
            for line in fh:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == gw:
                    return normalise_mac(parts[3])
    except OSError:
        pass
    return None


def default_gateway() -> Optional[str]:
    """The default route's gateway IP, or None when there is no route at all."""
    gw = _gateway_from_proc()
    if gw:
        return gw
    for line in _run_quiet(["route", "-n", "get", "default"]).splitlines():
        line = line.strip()
        if line.startswith("gateway:"):
            gw = line.split(":", 1)[1].strip()
            return gw or None
    return None


def current_network_id() -> Optional[str]:
    """A stable fingerprint for the network this Mac is on, or None if offline.

    The gateway's MAC address, normalised. See the note at TRUSTED_FILE for why
    it is the MAC and not the SSID or the subnet.
    """
    gw = default_gateway()
    if not gw:
        return None
    from_proc = _mac_from_proc(gw)
    if from_proc:
        return from_proc
    # `arp -n 192.168.68.1` -> "? (192.168.68.1) at 60:32:b1:1a:ee:40 on en0 ..."
    # ...or "? (192.168.68.1) -- no entry" when the cache is cold, which is not
    # an error: it happens for a second or two after joining, and the caller
    # simply re-checks on the next poll.
    out = _run_quiet(["arp", "-n", gw])
    if " at " not in out:
        return None
    mac = out.split(" at ", 1)[1].split()[0]
    return normalise_mac(mac)


def normalise_mac(mac: str) -> Optional[str]:
    """Lowercase, zero-padded, colon-separated -- or None if it is not a MAC.

    macOS `arp` drops leading zeros ("0:1a:2b:3c:4d:5e"), so the SAME network
    fingerprints two different ways depending on which octets happen to be
    small. Comparing the raw strings would quietly stop matching the file the
    user trusted, which presents as "my trusted network is not trusted any
    more" -- so normalise on the way in AND on the way out.
    """
    parts = mac.strip().lower().split(":")
    if len(parts) != 6:
        return None
    try:
        return ":".join(f"{int(p, 16):02x}" for p in parts)
    except ValueError:
        return None


def trusted_networks() -> Dict[str, str]:
    """Trusted gateway MAC -> the label the user gave it. Missing file = {}."""
    path = os.path.expanduser(os.environ.get("CLAUDE_MATE_TRUSTED_FILE",
                                             TRUSTED_FILE))
    out: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                mac, _, label = line.partition(" ")
                norm = normalise_mac(mac)
                if norm:
                    out[norm] = label.strip() or "(unnamed)"
    except OSError:
        pass
    return out


def trust_network(net_id: str, label: str) -> bool:
    """Add a network to the trusted file. True if it was written."""
    path = os.path.expanduser(os.environ.get("CLAUDE_MATE_TRUSTED_FILE",
                                             TRUSTED_FILE))
    norm = normalise_mac(net_id)
    if not norm:
        return False
    if norm in trusted_networks():
        return True
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 0600: this file decides who the daemon will talk to. It is not a
        # secret, but it is a policy, and a policy anything can append to is
        # not one.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"{norm} {label}\n")
        return True
    except OSError as exc:
        log(f"could not write {path}: {exc}")
        return False


class NetworkGate(threading.Thread):
    """Keeps the TCP listener and the mDNS advert off untrusted networks.

    Polls which network we are on and opens or closes the listener to match.
    Runs even when the answer never changes, because the interesting case is
    precisely the one nobody is watching: the laptop that was closed at home and
    opened somewhere else.

    TRUST ON FIRST USE, ONCE. An upgrade must not silently disconnect a device
    that worked yesterday, so if the trusted file does not exist yet the first
    network seen is adopted and loudly logged. That is never worse than the old
    behaviour -- which was to listen on every network without asking -- and from
    the second network onwards it is strictly better. `claude-mate trust --list`
    shows what was adopted and `--remove` undoes it.
    """

    def __init__(self, net: "NetLink", mdns: Optional["MdnsAdvertiser"],
                 poll_s: float = NET_GATE_POLL_S) -> None:
        super().__init__(name="net-gate", daemon=True)
        self._net = net
        self._mdns = mdns
        self._poll_s = poll_s
        self._stop_evt = threading.Event()
        self._open = False
        self._last_id: Optional[str] = ""      # "" = nothing evaluated yet

    def is_open(self) -> bool:
        return self._open

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.evaluate()
            except Exception as exc:                        # noqa: BLE001
                # A gate that crashes must fail CLOSED, and must say so. The
                # alternative -- a dead thread and a listener nobody is
                # re-evaluating -- is the one outcome worse than either state.
                log(f"network gate error ({exc}); closing the listener")
                self._apply(False, "gate error")
            self._stop_evt.wait(self._poll_s)

    def evaluate(self) -> None:
        net_id = current_network_id()
        if net_id is None:
            self._apply(False, "no network")
            self._last_id = None
            return
        known = trusted_networks()
        if not known and not os.path.exists(
                os.path.expanduser(os.environ.get("CLAUDE_MATE_TRUSTED_FILE",
                                                  TRUSTED_FILE))):
            if trust_network(net_id, "first-seen"):
                log(f"trusting the network you are on ({net_id}) -- first run. "
                    f"`claude-mate trust --list` to review, `--remove` to undo.")
                known = trusted_networks()
        if net_id in known:
            self._apply(True, f"{known[net_id]} ({net_id})")
        else:
            self._apply(False, f"untrusted network {net_id}")
        self._last_id = net_id

    def _apply(self, want_open: bool, why: str) -> None:
        if want_open == self._open:
            return
        self._open = want_open
        if want_open:
            if self._net.start():
                log(f"TCP listener open: {why}")
                if self._mdns:
                    self._mdns.start()
            else:
                self._open = False
        else:
            self._net.stop()
            if self._mdns:
                self._mdns.stop()
            log(f"TCP listener closed: {why}. "
                f"`claude-mate trust` to allow this network.")


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


def _wrap2(text: str) -> Tuple[str, str]:
    """Word-wrap a short message into the two 21-char rows a notice gets.

    Words first, because breaking a domain name across rows makes the thing you
    are being asked to notice unreadable. A word longer than a row is cut rather
    than allowed to swallow both -- overflow ends in an ellipsis so a truncated
    message never reads as a complete one that happens to be odd."""
    rows, i = ["", ""], 0
    for w in _sanitize(text).split():
        w = w[:ROW_CHARS]
        cand = f"{rows[i]} {w}".strip()
        if len(cand) <= ROW_CHARS:
            rows[i] = cand
        elif i == 0:
            i = 1
            rows[i] = w
        else:                                     # '+' = "and more", the same
            rows[1] = rows[1][:ROW_CHARS - 1] + "+"   # mark the fleet strip uses
            break
    return (rows[0], rows[1])


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


# ---- the workflow band: J|<st>|<done>|<failed>|<total>|<info>|<name> ------ #
# THE ONE PLACE THE DEVICES DO NOT SHARE A RENDERING. Everything else on the
# glass is a row the daemon composes and both firmwares print; the band carries
# numbers each device draws a bar from in its own geometry, plus two strings
# composed here so the firmware still never decides what a word says: `info`
# (what fits in 14 characters) and `name`. Both are ASCII before they leave,
# and neither can hold a '|' -- the firmware rejects any J with one in it.

def _wf_ascii(text: str) -> str:
    """A string the J| line can carry: '|' -> '/', the ellipsis Claude cuts
    names with -> '~', control characters dropped, anything else outside
    ASCII -> '?' (one for one, as the transports' own ascii/replace would)."""
    out = []
    for ch in text or "":
        if ch == "|":
            out.append("/")
        elif ch == "…":
            out.append("~")
        elif ch < " " or "\x7f" <= ch <= "\x9f":
            continue
        elif ch > "~":
            out.append("?")
        else:
            out.append(ch)
    return "".join(out)


def _wf_clock(seconds: float) -> str:
    """A run's clock in Claude's own units -- 45s, 2m39s, 1h02m, 1d03h -- so
    the band reads like the footer it came from, and so a ten-hour run never
    reads like the ten-minute "10:23" that r1's mm:ss would make of it."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86400}d{s % 86400 // 3600:02d}h"


def _wf_tokens(n: int) -> str:
    """Compact tokens: 999, 1.2k, 543k, 1.4M, 12M, 1.2B. Rounded half up in
    integers, and a value that rounds past its unit moves up one -- 999_950
    is "1.0M", never "1000k" (likewise 9_960 is "10k", not "10.0k")."""
    n = max(0, int(n))
    if n < 1000:
        return str(n)
    for div, unit in ((10 ** 3, "k"), (10 ** 6, "M"), (10 ** 9, "B")):
        tenths = (10 * n + div // 2) // div       # n / div to one decimal
        if tenths < 100:
            return f"{tenths // 10}.{tenths % 10}{unit}"
        whole = (n + div // 2) // div
        if whole < 1000 or unit == "B":
            return f"{whole}{unit}"
    return str(n)                                 # unreachable


def wf_info(rec: WfRec, extra: int, now: float) -> str:
    """The band's text, at most WF_INFO_CHARS ASCII characters.

    Parts: COUNT ("3/5", or "40%" when only the bar is known), CLK (the run's
    clock), TOK (tokens), PLUS ("+1": other workflows). The first candidate
    that fits wins, so a count is the last thing given up and "+N" outranks
    tokens -- on the Nano, which never shows the name, it is the only sign a
    second run exists. A paused run with a note says when it resumes instead.

    When nothing about the run fits (a hook's record, a paused row the
    terminal cut short), what stands in depends on who draws the word. A
    running run (R/E) gets its word: the Nano draws a bar for it and no word,
    and an empty info beside an empty bar would say nothing at all. A paused,
    finished or ended one gets only "+N", or nothing: both firmwares draw
    "paused"/"done"/"ended" themselves -- the Nano where the bar would be, the
    S3 on the band's first line -- and the word again in the info would read
    "paused ... paused"."""
    plus = f"+{extra}" if extra > 0 else ""
    note = _wf_ascii(rec.note).strip()[:WF_INFO_CHARS]
    if rec.st == "P" and note:
        return note
    if rec.done is not None and rec.total is not None:
        count = f"{rec.done}/{rec.total}"
    elif rec.pct is not None:
        count = f"{rec.pct}%"
    else:
        count = ""
    secs = wf_elapsed(rec, now)
    clk = "" if secs is None else _wf_clock(secs)
    tok = "" if rec.tokens is None else _wf_tokens(rec.tokens)
    if count or clk or tok:
        for parts in ((count, clk, tok, plus), (count, clk, plus),
                      (count, plus), (count, clk, tok), (count, clk),
                      (count, tok), (count,), (clk, tok), (clk,), (tok,)):
            text = " ".join(p for p in parts if p)
            if text and len(text) <= WF_INFO_CHARS:
                return text
    if rec.st not in _WF_CLOCKED:
        return plus if len(plus) <= WF_INFO_CHARS else ""
    word = _WF_WORD.get(rec.st, "")
    text = f"{word} {plus}".strip()
    return text if len(text) <= WF_INFO_CHARS else word[:WF_INFO_CHARS]


def _wf_j_name(name: str, extra: int) -> str:
    """The name the S3 prints, at most WF_NAME_CHARS. With other workflows
    visible it ends " +N" -- inside the 40, never past it -- and a name cut to
    make room ends in '~', the way Claude's own '…' arrives."""
    text = _wf_ascii(name).strip()
    tail = f" +{extra}" if extra > 0 else ""
    room = WF_NAME_CHARS - len(tail)
    if len(text) > room:
        text = text[:room - 1].rstrip() + "~"
    return text + tail


def wf_j_line(rec: WfRec, extra: int, now: float) -> str:
    """J|<st>|<done>|<failed>|<total>|<info>|<name> -- note the ORDER of the
    counts, which is not the socket's. They drive the bar and nothing else:
    with no counts but a bar fraction the bar is done=pct of total=100, and
    with neither it is 0|0|0, which both firmwares draw as no bar at all.
    Saturated at 65535, the firmware's own ceiling (and five digits)."""
    if rec.done is not None and rec.total is not None:
        nums = (rec.done, rec.failed or 0, rec.total)
    elif rec.pct is not None:
        nums = (rec.pct, 0, 100)
    else:
        nums = (0, 0, 0)
    counts = "|".join(str(min(max(0, int(v)), 65535)) for v in nums)
    return (f"J|{rec.st}|{counts}|{wf_info(rec, extra, now)}"
            f"|{_wf_j_name(rec.name, extra)}")


def wf_snapshot(recs, now: float) -> List[Dict[str, Any]]:
    """A session's visible workflows for the CLI: the band's own record FIRST
    (the spotlight rule needs times the JSON does not carry, so the order is
    how a client shows what the device shows), then the rest in the producer's
    order. 'status' is a word; 'st' tells a running-with-failures E from R."""
    vis = [r for r in recs if wf_visible(r, now)]
    primary, _extra = wf_primary(recs, now)
    if primary is not None:
        vis = [primary] + [r for r in vis if r is not primary]
    out = []
    for r in vis:
        secs = wf_elapsed(r, now)
        out.append({"name": r.name, "st": r.st,
                    "status": _WF_WORD.get(r.st, ""),
                    "done": r.done, "total": r.total, "failed": r.failed,
                    "pct": r.pct,
                    "secs": None if secs is None else int(secs),
                    "tokens": r.tokens, "note": r.note})
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
        # Both are forgotten while no device is reachable (see refresh).
        self._last_frame = ""
        self._shown_key: Optional[str] = None
        # Last J| line actually sent ("" = none yet), the session whose band
        # it was (None = none yet, or 'J|-' since), and when the last whole
        # J+F pair went out (monotonic). Read and written only by refresh(),
        # never by _compose -- see _wf_view for why.
        self._last_j = ""
        self._last_j_key: Optional[str] = None
        self._last_pair_at = float("-inf")
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
        self.on_handshake_extra = None   # re-assert edge-driven device state
        # A transient message across the middle two rows: (text, expires_at).
        # An ACTIONS-sheet verb can fail for ordinary reasons -- the session is
        # already on that account, it has said nothing worth carrying yet -- and
        # the wrapper answers every one of them in words. Those words used to go
        # to the log and nowhere else, which from arm's length is exactly what a
        # dead button looks like: the fix for "I pressed it and nothing
        # happened" is not to press harder, it is to say what happened. Rendered
        # by the daemon into the existing frame, so it costs no firmware change
        # and works on a device that is already flashed.
        self._notice: Optional[Tuple[str, float]] = None
        # Rendered lazily-but-once at construction: eight short WAVs, well
        # under a tenth of a second of work, and doing it here means the
        # first jump of the first run is not the one that pays for it.
        self._sfx = sfx_build_cache() if sys.platform == "darwin" else {}

    # ---- transient messages ----------------------------------------------- #

    def notice(self, text: str, secs: float = NOTICE_S) -> None:
        """Say something on the glass for a few seconds.

        Set OUTSIDE the lock-holding path and pushed immediately: a message
        about a press has to land while the finger is still near the button, not
        on whatever tick comes next. A timer forces one more refresh when it
        expires, otherwise a device with nothing else happening would keep the
        stale message until the next state change -- which, for an idle fleet at
        2am, could be hours."""
        with self._lock:
            self._notice = (text, time.monotonic() + secs)
        self.refresh(force=True)
        t = threading.Timer(secs + 0.05, self.refresh, kwargs={"force": True})
        t.daemon = True
        t.start()

    def _notice_rows(self) -> Optional[Tuple[str, str]]:
        """The live notice as two rows, or None. Expiry is checked HERE rather
        than by the timer, so a frame composed for any other reason after the
        deadline also drops it."""
        if self._notice is None:
            return None
        text, until = self._notice
        if time.monotonic() >= until:
            self._notice = None
            return None
        return _wrap2(text)

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

    def select_by(self, want: str) -> Optional[str]:
        """Select a session by key, queue index or name. Returns the name, or
        None.

        The device has no equivalent -- it walks there with PREV/NEXT -- but a
        terminal that can print the whole queue at once should be able to point
        at a row instead of stepping to it. The index is into the SAME queue the
        snapshot prints, so what you read is what you get.

        AN EXACT SESSION KEY FIRST. The queue JSON carries every row's key, and
        a client that already knows which row it means -- the app raising the
        terminal of a chat from its History -- says so by key: an index can
        shift under it between its read and this one, and a name is refused
        as ambiguous whenever two rows share one.

        Name matching is exact-first, then unique prefix, then unique substring.
        AMBIGUITY IS A REFUSAL rather than a guess: this moves what GO acts on,
        and picking the wrong session is the one mistake that costs you the
        window you were trying to raise.
        """
        want = want.strip()
        if not want:
            return None
        queue = self._reg.queue()
        if not queue:
            return None
        pick = next((s for s in queue if s.key == want), None)
        if pick is None and want.isdigit():
            i = int(want)
            if 0 <= i < len(queue):
                pick = queue[i]
        if pick is None:
            for pred in (lambda s: s.name == want,
                         lambda s: s.name.lower() == want.lower(),
                         lambda s: s.name.lower().startswith(want.lower()),
                         lambda s: want.lower() in s.name.lower()):
                hits = [s for s in queue if pred(s)]
                if len(hits) == 1:
                    pick = hits[0]
                    break
                if len(hits) > 1:
                    return None            # ambiguous: refuse, do not guess
        if pick is None:
            return None
        with self._lock:
            self._sel_key = pick.key
        self.refresh()
        return pick.name

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

    def _unlinked(self) -> bool:
        """No device is reachable, so there is no glass at all. Caller holds
        the lock.

        Fenced, like _wf_view: it is asked under the Screen lock on the
        ticker, the socket thread and the button reader, and an exception
        would end whichever one it was on. A link that cannot answer is taken
        as up -- all the screen ever assumed before it asked."""
        try:
            return not self._link.is_open()
        except Exception:
            return False

    def current_shown(self) -> Optional[Session]:
        """The session whose frame is on the glass right now, or None: what
        FOLLOW raises, CONTINUE types into and the queue marks as shown.

        With no device reachable it is the selection instead -- what the glass
        would show, and what a press from a terminal means (see refresh)."""
        with self._lock:
            queue = self._reg.queue()
            if self._unlinked():
                return self._subject(queue)
            key = self._shown_key
            if key is None:
                return None
            for s in queue:
                if s.key == key:
                    return s
            return None

    def resolve_press_target(self) -> Optional[Session]:
        """The session a GO/ACK press applies to: EXACTLY the one whose frame is
        on the glass (WYSIWYG). Never a freshly recomputed head -- so a press
        can only ever act on the name the user is actually looking at. Falls
        back to the selection (the head, if that has gone too) only if the
        shown session has vanished, or no device is reachable to show one."""
        with self._lock:
            queue = self._reg.queue()
            if self._shown_key is not None and not self._unlinked():
                for s in queue:
                    if s.key == self._shown_key:
                        return s
            return self._subject(queue)         # shown session gone -> head

    # ---- frame composition -------------------------------------------------- #

    @staticmethod
    def _wf_view(subject: Optional[Session], now: float) -> Tuple[str, bool]:
        """(the J| line for the subject's workflow band, whether it is up).

        Computed ONCE per refresh, with the same `now` the frame uses, and
        handed to _compose -- so bit2 and the J it commits are decided by one
        visibility check and can never disagree at a stale or linger
        boundary. Static: it reads the subject's immutable tuple and nothing on
        the Screen.

        Fenced. This runs under the Screen lock on the ticker, the socket
        thread and the button reader, and an exception escaping it would kill
        whichever one it was on; a workflow nobody anticipated must cost the
        band, never the frame."""
        if subject is None:
            return ("J|-", False)
        try:
            rec, extra = wf_primary(subject.workflows, now)
            if rec is None:
                return ("J|-", False)
            return (wf_j_line(rec, extra, now), True)
        except Exception as exc:
            _wf_fault("band", exc)
            return ("J|-", False)

    def _compose(self, queue: List[Session],
                 subject: Optional[Session],
                 wf_show: bool = False) -> Tuple[str, Optional[str], bool]:
        """Build the F| line for the current state:
            F|<flags>|<sel>|<r0>|<r1>|<r2>|<r3>
        with four size-1 rows (r0 name / r1 state+time + account / r2
        model+effort + remaining-limit chip / r3 position + fleet letters).
        `flags` is a bitfield
        (bit0 = flash the name, bit1 = FOLLOW mode -> draw the play marker,
        bit2 = the workflow band is live for this session -- `wf_show`, from
        _wf_view, unless a notice owns r1/r2);
        `sel` is the character column WITHIN r3 of the active tab's fleet
        letter to box (-1 = none). Returns (line, subject_key, flash). r3 is
        LAST and may contain literal '|' (the firmware stops tokenizing at the
        6th bar and takes the rest verbatim), which is what lets the strip use
        '|' as its visual divider.

        The band changes NOTHING else in the frame: r0..r3, sel, bit0 and bit1
        are byte-for-byte what they would be without it, and r2 keeps its
        model+effort+limit -- so firmware that predates the band (which reads
        only bits 0 and 1) draws exactly what it always did."""
        follow = self._follow
        note = self._notice_rows()
        if subject is None:
            if note:
                return (f"F|0|-1|MATE|{note[0]}|{note[1]}|", None, False)
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
        # A notice takes the middle two rows and leaves r0 and r3 alone: you
        # keep the name of the session it is ABOUT, and the fleet strip that
        # tells you nothing has moved underneath. Losing state+time and
        # model+effort for four seconds costs nothing; losing which session is
        # being talked about would make the message ambiguous.
        if note:
            r1, r2 = note
        elif wf_show:
            # ...and the band gives way to it too: on the Nano the band IS r2,
            # and a message about a press outranks a progress bar for the four
            # seconds it is up.
            flags |= 4
        line = f"F|{flags}|{sel}|{r0}|{r1}|{r2}|{r3}"
        return (line, subject.key, flash)

    def refresh(self, force: bool = False) -> None:
        """Re-compose the frame and send it if the bytes changed (or `force`).

        The queue snapshot is taken INSIDE the Screen lock so snapshot-time and
        commit-time are ordered -- a preempted caller can never commit a stale
        frame over a newer one (Screen -> Registry lock nesting is safe: the
        registry never takes the Screen lock).

        THE WORKFLOW BAND RIDES THE FRAME. Its J| line goes out immediately
        BEFORE the F that commits it, and the firmware only stages a J -- it
        draws nothing until the F arrives. So while the band is up, anything
        worth writing (a changed frame, a changed J, a forced resend) is
        written as the pair, and the F goes even when its own bytes are
        unchanged: it is the commit. That is also the loss recovery. A lost F
        is healed by the next one a second later, and pairing extends that to
        J -- a J that only went out when its bytes changed would leave a
        dropped one wrong for a whole linger or a whole usage-limit pause,
        because a finished run's J never changes. With no band, 'J|-' goes out
        once, ahead of the frame, and on every forced resend.

        "The next one" has to come, though, and a frame does not always move:
        r1 past an hour reads h:mm, and a paused or finished run's J is
        constant, so nothing changes for up to a minute. So the pair also goes
        out whenever the last one is WF_RESEND_S old -- the Ticker calls in
        once a second -- and a lost J or F costs a second or two, whatever is
        on the glass.

        And a band for a DIFFERENT session goes out over a cleared stage:
        'J|-', then its J, then the F. The firmware's staged J survives the
        commit, so if the new session's J were lost, its F would commit the
        PREVIOUS session's band under the new session's name. With the stage
        cleared first, a lost J means no band until the next pair instead. No
        J yet counts as different: a device that outlived the last daemon may
        still hold a J it staged."""
        with self._lock:
            if self._unlinked():
                # NO GLASS, NO STALE SUBJECT. _shown_key moves only when a
                # frame reaches a device, so once the device had gone -- a
                # cable pulled, a battery device asleep -- it went on naming
                # the last session drawn, and GO raised and acknowledged THAT
                # one: the app's History selected a chat's row by its key, was
                # told "ok", and its press raised another terminal. With
                # nothing linked the selection is the subject (current_shown,
                # resolve_press_target), and forgetting the last frame means
                # the first one after a reconnect goes out and is committed.
                # A write refused while the link is up clears nothing: the
                # old frame may still be on the glass, and a press must act
                # on what is there.
                self._shown_key = None
                self._last_frame = ""
            now = time.time()
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
            j_line, show = self._wf_view(subject, now)
            line, key, flash = self._compose(queue, subject, wf_show=show)
            changed = (line != self._last_frame)
            if show:
                sent_at = time.monotonic()
                other = key != self._last_j_key
                if not (changed or force or other or j_line != self._last_j
                        or sent_at - self._last_pair_at >= WF_RESEND_S):
                    return
                # Committed only on a successful write, like the frame: a J
                # nobody took is retried with its F on the next tick, and an F
                # is never sent without the J it commits. Another session's
                # band goes out over a cleared stage (see above).
                if other and self._last_j != "J|-":
                    if not self._link.write_line("J|-"):
                        return
                    self._last_j, self._last_j_key = "J|-", None
                if not self._link.write_line(j_line):
                    return
                self._last_j, self._last_j_key = j_line, key
                if self._link.write_line(line):
                    self._last_frame = line
                    self._shown_key = key
                    self._last_pair_at = sent_at
                return
            if force or self._last_j != "J|-":
                if self._link.write_line("J|-"):
                    self._last_j, self._last_j_key = "J|-", None
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
        # Controller mode is edge-driven -- the daemon sends G| only when the
        # grab CHANGES -- so a device that shows up while a page is already
        # holding it would never be told, and would sit in menu semantics
        # wondering why the game felt laggy. Same class of bug as the sound
        # preference, and the same fix: re-assert on every handshake.
        if self.on_handshake_extra is not None:
            try:
                self.on_handshake_extra()
            except Exception as exc:
                log(f"handshake extra: {exc}")

    def notify_change(self) -> None:
        """Called on any registry change: refresh frame + LED."""
        self.refresh()
        self.sync_led()


# --------------------------------------------------------------------------- #
# FOCUS action (raise/activate ONLY -- never collapse/resize/minimize)
# --------------------------------------------------------------------------- #


ACCOUNTS_DIR = os.path.expanduser(
    os.environ.get("CLAUDE_MATE_ACCOUNTS_DIR", "~/.claude-accounts"))
ACCT_MAX = 5                       # what the device's picker can show


def account_profiles(limit: int = ACCT_MAX) -> List[str]:
    """The SAVED logins, in a stable order both ends agree on.

    'default' first -- it is the ~/.claude login, usually the main one, and it
    is not a directory under the profiles root, so listing only that root left
    the device able to switch AWAY from your main account and never back to it.
    The rest are profile directories under ~/.claude-accounts, each holding its
    own credentials, sorted -- so the index the device sends back means the same
    name the daemon sent. An ordering that drifted between the two ends would
    switch you to the wrong account, which is the one mistake this must not make.
    """
    try:
        rest = sorted(d for d in os.listdir(ACCOUNTS_DIR)
                      if os.path.isdir(os.path.join(ACCOUNTS_DIR, d)))
    except OSError:
        rest = []
    names = ["default"] + rest
    return names[:limit] if limit else names


def account_dir(name: str) -> str:
    """A profile's config dir. 'default' is ~/.claude, which is NOT under the
    profiles root -- the same rule claude-mate-switch follows."""
    return (os.path.expanduser("~/.claude") if name == "default"
            else os.path.join(ACCOUNTS_DIR, name))


# What the device was ACTUALLY told, in order. The device answers with an index,
# so the only list that can decode it is the one it was sent -- not a fresh
# listdir at press time. Profiles appear and disappear while the device is
# linked (starting a session on a new name creates one), and a name sorting into
# the middle renumbers every row below it: the device would still be showing the
# old rows and the press would land on the neighbour. Switching to an account
# you did not choose is the one mistake this feature must not make.
_pushed_accounts: List[str] = []
_pushed_lock = threading.Lock()


def set_pushed_accounts(names: List[str]) -> None:
    with _pushed_lock:
        _pushed_accounts[:] = list(names)


def pushed_accounts() -> List[str]:
    with _pushed_lock:
        return list(_pushed_accounts)


_wrap_mod: Any = None


def account_headroom_chip(cfg_dir: str) -> str:
    """A profile's remaining-limit chip ('5h82%'), or '' if unknown.

    Borrowed from the wrapper rather than reimplemented: reading a profile's
    token means knowing that Claude Code keys the Keychain entry as
    "Claude Code-credentials-<sha256(cfg_dir)[:8]>", which was established by
    experiment and is exactly the sort of detail that rots when it is written
    down twice. Any failure is '' -- an unknown limit must never stop the
    picker listing the account.
    """
    global _wrap_mod
    if _wrap_mod is None:
        try:
            import importlib.machinery
            import importlib.util
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "bin", "claude-mate-wrap")
            spec = importlib.util.spec_from_loader(
                "cm_wrap_helpers",
                importlib.machinery.SourceFileLoader("cm_wrap_helpers", path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _wrap_mod = mod
        except Exception as exc:
            log(f"account usage unavailable: {exc}")
            _wrap_mod = False
    if not _wrap_mod:
        return ""
    try:
        tok = _wrap_mod._access_token(cfg_dir)
        if not tok:
            return ""
        return _wrap_mod._format_limit(_wrap_mod._fetch_usage(tok) or {})
    except Exception:
        return ""


def wrapper_ctrl_send(ctrl: str, cmd: str) -> bool:
    """wrapper_ctrl_call, for callers that only care whether it worked."""
    return wrapper_ctrl_call(ctrl, cmd)[0]


def wrapper_ctrl_call(ctrl: str, cmd: str) -> Tuple[bool, str]:
    """Send one command line ('focus') to a PTY wrapper's per-session control
    socket and WAIT for its ack. The wrapper replies in two stages -- 'go' the
    moment it accepts the command (liveness), 'ok' only after its window op
    (osascript) has COMPLETED -- so consecutive focuses apply in press order.
    The first read waits only WRAPPER_LIVE_TIMEOUT_S, so a wedged wrapper
    (stopped process whose socket the kernel still accepts) costs ~1s, while a
    live-but-slow op gets the full WRAPPER_ACK_TIMEOUT_S deadline. Pre-ack
    wrappers close the socket immediately (recv -> EOF), degrading to
    fire-and-forget.

    Returns (ok, why). `why` is the wrapper's own words when it refuses, and
    the caller is expected to SHOW them: a refusal that only reaches the log is
    indistinguishable, from where the user is standing, from a device that
    ignored the press."""
    if not ctrl or not os.path.exists(ctrl):
        return (False, "session is gone")
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(2.0)
        c.connect(ctrl)
        # utf-8, not ascii: a command can carry a profile name, and a profile is
        # a directory the user named. `.encode("ascii")` on one with an accent in
        # it raises UnicodeEncodeError -- not an OSError, so it escaped the
        # handler below and killed the calling thread instead of failing the send.
        c.sendall(cmd.encode("utf-8", "replace") + b"\n")
        buf = b""
        try:
            deadline = time.monotonic() + WRAPPER_ACK_TIMEOUT_S
            c.settimeout(WRAPPER_LIVE_TIMEOUT_S)   # a live wrapper 'go's in ms
            # A refusal is read to its NEWLINE, not to the first chunk. Stopping
            # at "buf starts with err" truncated the reason at whatever one recv
            # happened to return -- 64 bytes, mid-word ("...is another organis"),
            # which is fine for a log nobody reads and useless for a message we
            # now put on the glass. 512 to match the wrapper's own recv.
            while not (b"ok" in buf or (buf.startswith(b"err") and b"\n" in buf)):
                chunk = c.recv(512)
                if not chunk:          # EOF: pre-ack wrapper (or already done)
                    break
                buf += chunk
                c.settimeout(max(0.1, deadline - time.monotonic()))
        except socket.timeout:
            # Deliberately NOT a failure. `focus` uses the return value to
            # decide whether to run its own window-raising fallback, and a slow
            # ack from a wrapper that did raise the window would then raise it
            # twice. A timeout means "no answer yet", which is not "refused".
            log(f"wrapper {cmd} ack timeout ({ctrl}); continuing")
        finally:
            c.close()
        # A wrapper that REFUSES says so ('err <why>') rather than closing
        # silently, and the caller must not report the command as done: the
        # device would show a switch that never happened.
        if buf.startswith(b"err"):
            why = buf.decode("utf-8", "replace").strip()[4:] or "no reason given"
            log(f"wrapper refused {cmd!r}: {why}")
            return (False, why)
        return (True, "")
    except OSError as exc:
        log(f"wrapper {cmd} failed ({ctrl}): {exc}")
        return (False, "could not reach the session")


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
    """Unix-domain socket server: parses '<state>|<session_id>|<name>[|...]'
    lines (the wrapper's ten fields end with its workflows and the
    conversation it is running; see _process_line) and answers the CLI's few
    commands."""

    def __init__(self, sock_path: str, registry: Registry, on_update,
                 on_haptic=None) -> None:
        super().__init__(name="socket-server", daemon=True)
        self._sock_path = sock_path
        self._reg = registry
        self._on_update = on_update
        self._on_haptic = on_haptic   # called with a LED kind on session events
        # Set by main() when there is something to command. See _process_line:
        # this socket carries session updates, and now the controls that have to
        # come from a terminal rather than from a hook.
        self.on_pair_request = None
        self.on_press = None              # inject a device button code
        self.on_select = None             # move the selection; returns the name
        self.on_accounts_refresh = None   # re-read ~/.claude-accounts
        self.on_device_link = None        # switch the cabled device's transport
        self.screen = None                # for queue_snapshot()
        self.history = None               # HistoryWriter; unset under --mock
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
            # 0600, NOT 0666. Hooks run in the user's own shells -- same uid --
            # so owner-only has always been enough for them, and 0666 was
            # over-permission that cost nothing while this socket only carried
            # session updates. It stopped being free the moment it grew commands:
            # at 0666, ANY local account could `press|C` to type into the user's
            # live Claude session, `press|A0` to SIGTERM it, `pair` to arm an
            # over-the-air handout of the shared token, or `queue` to read every
            # session's working directory. The PTY wrapper's own control socket is
            # 0600 for exactly that reason, and this one was quietly undoing it by
            # proxy -- the daemon runs as the user and holds that 0600 socket.
            os.chmod(self._sock_path, 0o600)
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
                    self._serve(conn, line, complete=True)
            # A trailing piece with no newline is processed too -- but it may
            # be a line its sender gave up on half way (a sendall that timed out
            # stops at the socket buffer, 8 KB on macOS), so it is marked as
            # such: its state counts, its field 9 does not.
            if buf:
                self._serve(conn, buf, complete=False)
        except OSError:
            pass

    def _serve(self, conn: socket.socket, raw: bytes, complete: bool) -> None:
        """One line in, its answer (if any) out -- and A FAULT COSTS THE LINE,
        NEVER THE THREAD. Anything but an OSError used to end this thread for
        good while the process lived on: launchd restarts a daemon that dies,
        not one that has stopped listening, so every report after it went
        nowhere without a word. Each line is fenced, and the next one is read."""
        try:
            self._reply(conn, self._process_line(raw, complete))
        except Exception as exc:
            _sock_fault(exc)

    @staticmethod
    def _reply(conn: socket.socket, body: Optional[str]) -> None:
        """Write a command's answer back, if it had one.

        Session updates from hooks answer nothing and must keep costing nothing:
        they are fire-and-forget, the writer usually closes immediately, and a
        write into that closed socket is an ordinary EPIPE rather than a fault.
        """
        if not body:
            return
        try:
            conn.sendall(body.encode("utf-8", errors="replace"))
        except OSError:
            pass

    def _process_line(self, raw: bytes, complete: bool = True) -> None:
        try:
            line = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            return
        if not line:
            return
        # A COMMAND, not a session update. `pair` arms BLE enrolment for a few
        # minutes: the daemon owns the radio, so a separate process cannot do
        # this itself, and this socket is the one channel that already exists
        # between a terminal and the daemon. It is deliberately the ONLY command
        # here -- a hook can write to this socket, so anything that arrives on it
        # must be something a hook doing it by accident could not hurt, and the
        # worst an unwanted arming does is put a prompt on the device's screen
        # that nobody presses.
        if line == "pair":
            if self.on_pair_request:
                self.on_pair_request()
                log("pairing armed from the terminal")
                return "ok\n"
            log("pairing requested, but BLE is not running "
                "(start the daemon with --ble)")
            return "error: BLE is not running (start the daemon with --ble)\n"

        # ---- the CLI's command surface ------------------------------------- #
        # A terminal can now do what the device does, and does it by the SAME
        # route: `press|G` is dispatched through the ButtonReader that handles
        # `B|G` off the wire, so the CLI cannot drift into a second, subtly
        # different implementation of GO.
        #
        # WHAT MAY LIVE HERE. These verbs can type into the user's session
        # (`press|C`), stop it (`press|A<n>`), hand this daemon's token to a
        # peripheral over the air (`pair`), and read every session's working
        # directory (`queue`). That is not a surface to expose to every local
        # process, which is what the socket's old 0666 mode did -- it is 0600
        # now (see run()), so this is the user talking to their own daemon.
        #
        # Even so, DELETION IS NOT HERE. A hook in any of the user's shells can
        # write to this socket, and a hook firing a malformed line should not be
        # able to remove a login. `accounts rm` therefore deletes in the CLI's
        # own process and only asks the daemon to re-read afterwards.
        if line == "queue":
            return self._json(self.queue_snapshot())
        if line == "accounts":
            return self._json(self.accounts_snapshot())
        if line == "accounts-refresh":
            if self.on_accounts_refresh:
                self.on_accounts_refresh()
                return "ok\n"
            return "error: no account reporter\n"
        if line.startswith("press|"):
            code = line[6:].strip()
            if not code:
                return "error: press| needs a button code\n"
            if not self.on_press:
                return "error: no button reader\n"
            log(f"press {code!r} from the terminal")
            self.on_press(code)
            return "ok\n"
        if line.startswith("link|"):
            # Switch which radio the CABLED device uses. This exists because
            # there was no way to do it at all: the transport is set by the
            # firmware's `I|` console command, and the only route to that
            # console was a hand-run miniterm on /dev/cu.usbmodemN -- a port
            # this daemon holds open and reads from, non-exclusively, so the two
            # readers split the device's replies between them and the switch
            # that had worked looked like one that had not.
            #
            # A NARROW VERB, NOT A RELAY. The obvious shape here is "forward an
            # arbitrary config line", and it is the wrong one: the same console
            # takes `T|<token>`, so a general relay would let anything that can
            # write to this socket re-provision the device's shared secret. This
            # accepts three fixed words and builds the line itself.
            want = line[5:].strip().lower()
            if want not in ("auto", "cable", "wifi", "ble", "p2p"):
                return ("error: link| takes auto, cable, wifi, ble or p2p\n")
            if not self.on_device_link:
                return "error: no device on the cable\n"
            log(f"link {want!r} from the terminal")
            return "ok\n" if self.on_device_link(want) else \
                   "error: could not write to the device\n"
        if line.startswith("select|"):
            want = line[7:].strip()
            if not self.on_select:
                return "error: no screen\n"
            got = self.on_select(want)
            return f"ok {got}\n" if got else f"error: no session matching {want!r}\n"

        # Expected: "<state>|<session_id>|<name>|<ctrl_sock?>|<model?>|<effort?>
        # |<account?>|<limit?>|<wf?>|<hist?>". The hook path sends only the
        # first three fields; the PTY wrapper sends all ten: the control
        # socket, the scraped model + effort, the account it runs as, the
        # remaining-limit chip, the session's dynamic workflows (parse_wf) and
        # the conversation it is running (parse_history_field).
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
        # A PLAIN split, not split('|', 8). The workflow grammar already keeps
        # '|' out of field 8, so a maxsplit would buy nothing -- and it would
        # glue field 9 onto the last workflow's name. Fields past 9 are
        # ignored, as past 8 always were.
        wf = parts[8].strip() if len(parts) > 8 else ""
        if not state:
            log(f"ignoring malformed socket line: {line!r}")
            return
        # FIELD 9 ONLY FROM A WHOLE WRAPPER LINE. A hook never sends one, and
        # a line cut short (see _handle_conn) can end inside a path that still
        # reads as a path -- one directory up, say -- which the history would
        # then keep as where the chat lives.
        hist = None
        if complete and ctrl and len(parts) > 9:
            hist = parse_history_field(parts[9])
        if state == "end":
            key = sid if sid else name
            if key:
                sess = self._reg.remove(key)
                log(f"session ended: {name or sid}")
                self._on_update()
                self._history_end(sess, key, name, account, hist)
            return
        log(f"socket update: state={state} sid={sid or '-'} name={name or '-'}")
        h = hist or {}
        haptic = self._reg.update(state, sid, name, focus_ctrl=ctrl,
                                  model=model, effort=effort,
                                  account=account, limit=limit, wf=wf,
                                  cwd=h.get("cwd") or "",
                                  conversation=h.get("id") or "",
                                  config_dir=h.get("config"),
                                  flags=h.get("flags"))
        self._on_update()
        if haptic and self._on_haptic:
            log(f"LED: {haptic} transition for {name or sid} ({state})")
            self._on_haptic(haptic)
        self._history_seen(Registry.key_for(sid, name))

    # ---- the chat history's two doors -------------------------------------- #
    # BOTH FENCED, BOTH LAST. By the time either runs, the line has done
    # everything it is for -- the registry, the screen, the LED -- so a history
    # fault costs the history and nothing else. And neither touches a file:
    # note() only queues, and the HistoryWriter thread does the I/O.

    def _history_seen(self, key: str) -> None:
        """A session line: hand the writer the session as it now stands, if
        it is running a conversation the history can name."""
        try:
            if self.history is None:
                return
            snap = self._reg.snapshot(key)
            if snap is not None and snap["conversation"]:
                self.history.note(snap)
        except Exception as exc:
            _history_fault("note", exc)

    def _history_end(self, sess: Optional[Session], key: str, name: str,
                     account: str, hist: Optional[Dict[str, Any]]) -> None:
        """An `end`: the session is gone from the registry; tell the writer
        its conversation closed, from what the session was."""
        try:
            if self.history is None:
                return
            if sess is not None:
                snap = sess.history_snapshot()
            else:
                # UNKNOWN: the daemon restarted since this session last
                # reported, so all it knows is the end line. That may close a
                # record but never create or repoint one -- turned=False.
                snap = {"key": key, "name": name, "cwd": "",
                        "conversation": "", "config_dir": None,
                        "flags": None, "account": account, "turned": False,
                        "turn_ts": 0.0}
            # The end line's own field 9 on top: the id can arrive first on
            # it, when the transcript appeared after the last report.
            if hist is not None:
                if hist["id"]:
                    snap["conversation"] = hist["id"]
                if hist["cwd"]:
                    snap["cwd"] = hist["cwd"]
                if hist["config"] is not None:
                    snap["config_dir"] = hist["config"]
                if hist["flags"] is not None:
                    snap["flags"] = hist["flags"]
            if snap["conversation"]:
                self.history.note(snap, ended=time.time())
        except Exception as exc:
            _history_fault("end", exc)

    # ---- what the CLI reads ------------------------------------------------ #

    @staticmethod
    def _json(obj: Any) -> str:
        return json.dumps(obj) + "\n"

    def queue_snapshot(self) -> Dict[str, Any]:
        """The device's screen, as data.

        Built from the SAME Registry.queue() the frame is drawn from, so the
        order a terminal prints is the order PREV/NEXT steps through. A CLI that
        sorted its own way would make `select 3` mean two different things
        depending on which surface you happened to be looking at.
        """
        sel = shown = None
        follow = False
        scr = self.screen
        if scr is not None:
            sel_s = scr.resolve_press_target()
            shown_s = scr.current_shown()
            sel = sel_s.key if sel_s else None
            shown = shown_s.key if shown_s else None
            follow = scr.is_follow()
        out = []
        now = time.time()
        for i, sess in enumerate(self._reg.queue()):
            # ALWAYS a list, [] when there are none, so a client can iterate
            # it without asking -- and the same visibility the device uses,
            # so the terminal never lists a run the glass has stopped showing.
            try:
                wfs = wf_snapshot(sess.workflows, now)
            except Exception as exc:
                _wf_fault("queue", exc)
                wfs = []
            out.append({
                "i": i,
                "key": sess.key,
                "name": sess.name,
                "state": sess.state,
                "secs": round(sess.display_seconds()),
                "model": sess.model,
                "effort": sess.effort,
                "account": sess.account,
                "limit": sess.limit,
                "cwd": sess.cwd,
                "conversation": sess.conversation,
                # Seconds since its last line. A wrapper reports at least every
                # KEEPALIVE_S (20 s), so a row this quiet for a minute is a
                # terminal that died without an `end` (kill -9) -- the CLI does
                # not call a chat open on its word, though the row stays here
                # until prune() takes it.
                "seen": round(max(0.0, now - sess.last_update_ts)),
                "acked": sess.acked,
                "shown": sess.key == shown,
                "workflows": wfs,
            })
        return {"queue": out, "follow": follow, "shown": shown, "selected": sel}

    @staticmethod
    def accounts_snapshot() -> Dict[str, Any]:
        """The saved logins, in the order the device is shown them.

        `dir` is included because the CLI is what deletes one, and it should
        delete the path the daemon MEANS rather than one it rebuilt from a name
        and an assumption -- 'default' is ~/.claude and not under the profiles
        root at all, which is exactly the assumption that would go wrong.

        No limit chips: reading one costs a Keychain lookup and an HTTP call per
        account, fine on the device's slow poll and wrong for a command someone
        runs to see a list. claude-mate-switch already prints them.
        """
        out = []
        for name in account_profiles(limit=0):
            cfg = account_dir(name)
            out.append({"name": name, "dir": cfg,
                        "exists": os.path.isdir(cfg),
                        "removable": name != "default"})
        return {"accounts": out, "accounts_dir": ACCOUNTS_DIR}

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
            # THIS THREAD IS THE ONLY WAY IN FROM THE DEVICE, and until this
            # guard existed one unhandled exception ended it for the lifetime of
            # the daemon -- silently, as far as anything a user looks at goes.
            # Nothing else stops: sessions keep updating, LEDs keep firing, the
            # terminal keys and the web page keep working, the link still shows
            # connected. Only the buttons are dead, and the single traceback
            # explaining why has long since scrolled out of a log nobody reads
            # until something breaks. "The daemon is stuck" is what that looks
            # like from the couch. One bad line is worth a log entry, not the
            # whole input path.
            try:
                self._dispatch(line)
            except Exception:
                log(f"button dispatch failed for {line!r}:\n{traceback.format_exc()}")

    def press(self, code: str) -> None:
        """Act on a button code as though the device had sent it.

        Through _dispatch rather than by calling the handlers directly, and that
        is the whole point: every rule that applies to a real press applies here
        too -- PREV scrolling the mirror instead of moving the selection, GO
        closing the mirror first, a browser holding the grab swallowing the lot.
        A CLI that called _go_pressed() itself would be a second implementation
        of GO, and the two would drift the first time either changed.
        """
        self._dispatch(f"B|{code}")

    def _dispatch(self, line: str) -> None:
        # Tolerate garbled / partial lines: only act on exact, known shapes.
        # EITHER of these is proof that a Claude Mate is on the other end of the
        # cable, which is what the token push waits for. Cheap and idempotent:
        # the flag is armed once per serial open and spent on the first answer.
        if line in ("H", "K"):
            prov = getattr(self._link, "provision_if_ours", None)
            if prov:
                prov()
        if line == "H":
            # A DEVICE THAT JUST SAID HELLO HAS NO TERMINAL VIEW OPEN.
            #
            # The mirror is a daemon-side 1 s timer, and it was torn down by
            # exactly three things: another B|M, a B|G, or the shown session
            # disappearing. A device going away was not one of them. So any
            # restart that did not go through the device's own power-off --
            # a link restart, a gamepad toggle, a screen flip, `R`, a brownout,
            # or simply a BLE drop and reconnect -- left this timer running, and
            # the moment the link came back it pushed a title, seventeen rows
            # and M|END every second into a device that had never asked. The
            # firmware takes M|END as permission to show the view, and the view
            # sits in front of the status bar, so the device woke up in a
            # terminal nobody opened, with the battery chip and link glyph
            # hidden behind it, repainting the whole screen once a second and
            # holding the radio busy. On a battery build that is the single most
            # expensive thing the daemon can do to a device by accident.
            #
            # Fixed here rather than in the firmware deliberately: a device-side
            # "ignore mirrors I did not ask for" would leave the Mac polling a
            # wrapper and pushing nineteen lines a second into something that
            # throws them away, which costs the same radio and saves nothing.
            # ...and if it really did still have one open, it will say so
            # immediately: the device answers its own H with B|M when the view
            # survived on its side (a link flap rather than a reboot), which
            # re-opens this for the session on the glass. Closing first is what
            # makes that toggle unambiguous.
            #
            # _mirror_close_locked() resends the frame itself, so only push one
            # when there was nothing to hand the glass back from. Doing both
            # cost two full resends, two L| account bursts and two rounds of
            # per-profile HTTPS lookups on every reconnect -- self-inflicted, on
            # the very path this handler exists to make cheaper.
            if not self.mirror_close():
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
            elif ev == "C":                      # ACTIONS: type "continue"
                self._continue_pressed()
            elif ev == "T":                      # ACTIONS: new terminal, focused
                self._new_terminal_pressed()
            elif ev == "A":                      # ACTIONS: switch saved account
                self._switch_account_pressed(code[1:])
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
            # A VIEW NEEDS GLASS. With no device reachable current_shown() is
            # the selection, which is what a press that acts on a session
            # wants -- but a view opened into nothing would poll the wrapper
            # every second, for nobody, until something closed it.
            if sess is None or not self._link.is_open():
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

    def mirror_close(self) -> bool:
        """Close any open view. True if there was one.

        The return value exists for the handshake path: _mirror_close_locked()
        ends with its own resend_full_state(), so a caller that would otherwise
        resend needs to know whether that has already happened.
        """
        with self._mirror_lock:
            if self._mirror_key is None:
                return False
            self._mirror_close_locked()
            return True

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
        # LAST LOOK BEFORE WRITING. wrapper_ctrl_screen() above is a round trip
        # to another process, and a close landing during it -- the device's
        # M|OFF, or the H handler above -- would otherwise be overtaken by the
        # rows this tick already fetched. The device would set mirrorOn on the
        # M|END that followed the M|OFF and then sit on a frozen view with
        # nothing left to close it, because this tick does not re-arm.
        with self._mirror_lock:
            if self._mirror_key != key:
                return
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

    def _continue_pressed(self) -> None:
        """B|C -- type "continue" into the shown session and press Enter.

        The reason the ACTIONS menu exists. A session that hit its 5-hour limit
        or took an API error is sitting there waiting for a human to say one
        word, and the whole point of a desk device is not having to turn back to
        the laptop to say it. Goes to the session on the glass -- WYSIWYG, the
        same rule GO follows -- and only to a wrapped one, because a hook-only
        session has no PTY to type into.
        """
        sess = self._screen.current_shown()
        if sess is None:
            log("CONTINUE pressed but no session shown")
            self._screen.notice("continue: no session")
            return
        if not sess.focus_ctrl:
            log(f"CONTINUE -> {sess.name}: not a wrapped session, nothing to type into")
            self._screen.notice("continue: not a wrapped session")
            return
        ok, why = wrapper_ctrl_call(sess.focus_ctrl, "submit continue")
        log(f"CONTINUE -> {sess.name}: {'sent' if ok else 'FAILED'}")
        # Only on failure. A successful `continue` shows itself -- the session
        # goes from waiting to working within a second or two, on the same
        # screen -- and a notice would cover exactly that with older news.
        if not ok:
            self._screen.notice(why or "continue failed")

    def _new_terminal_pressed(self) -> None:
        """B|T -- open a new terminal, in the shown session's directory, and
        focus it. Off the reader thread: `open` can block for seconds while
        Terminal launches, and this thread must keep draining button events."""
        sess = self._screen.current_shown()
        cwd = (sess.cwd if sess and sess.cwd else os.path.expanduser("~"))

        def run() -> None:
            try:
                # `open -a Terminal <dir>` opens a new window already cd'd
                # there, and brings Terminal forward -- which IS the focus the
                # device asked for, so no separate raise is needed.
                subprocess.run(["open", "-a", "Terminal", cwd],
                               capture_output=True, timeout=20)
                log(f"NEW TERMINAL -> {cwd}")
            except Exception as exc:
                log(f"new terminal: {exc}")

        threading.Thread(target=run, name="new-terminal", daemon=True).start()

    def _switch_account_pressed(self, index: str = "") -> None:
        """B|A<i> -- continue the shown session on SAVED account number <i>.

        Switching accounts does NOT mean logging in again. Every one of these is
        a login you already have -- a profile directory under
        ~/.claude-accounts, holding its own credentials -- and the switch
        continues this conversation there. The index is decoded against the list
        the device was actually PUSHED (the L| lines it is showing), never
        against a fresh reading of the profiles directory: the two are read at
        different moments, and a profile created in between -- which happens
        simply by starting a session on a new name -- renumbers our list while
        the glass still shows the old rows, so row 3 would no longer be the name
        the device is highlighting.

        The wrapper does the work: it owns the claude process, so it is the only
        thing that can replace it.
        """
        sess = self._screen.current_shown()
        if sess is None:
            log("SWITCH pressed but no session shown")
            self._screen.notice("switch: no session")
            return
        if not sess.focus_ctrl:
            log(f"SWITCH -> {sess.name}: not a wrapped session")
            self._screen.notice("switch: not a wrapped session")
            return
        names = pushed_accounts()
        try:
            name = names[int(index)]
        except (ValueError, IndexError):
            log(f"SWITCH -> {sess.name}: index {index!r} names no account the "
                f"device was sent ({names or 'nothing pushed yet'})")
            self._screen.notice("switch: no such account")
            return

        def run() -> None:
            # The wrapper's own words, on the glass. Every refusal it can give
            # is a thing the person holding the device can act on -- they are
            # already on that account, the session has nothing to carry yet --
            # and none of them is worth making someone read a log to learn.
            ok, why = wrapper_ctrl_call(sess.focus_ctrl, f"switch {name}")
            log(f"SWITCH -> {sess.name}: continue on saved account "
                f"{name!r} ({'requested' if ok else 'FAILED'})")
            self._screen.notice(f"-> {name}" if ok else (why or "switch failed"))

        threading.Thread(target=run, name="switch-account", daemon=True).start()

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
    ticking (the frame is re-sent only when its bytes actually change -- or,
    while a workflow band is up, with its J once a second regardless, which
    is what bounds a lost J: see Screen.refresh), and keeps the LED honest."""

    def __init__(self, screen: Screen, registry: Registry,
                 on_slow_tick: Optional[Callable[[], None]] = None) -> None:
        super().__init__(name="ticker", daemon=True)
        self._screen = screen
        self._reg = registry
        self._slow = on_slow_tick        # every 5 s, alongside the prune
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
                if self._slow:
                    try:
                        self._slow()
                    except Exception as exc:   # housekeeping must never stop the tick
                        log(f"slow tick: {exc}")
            self._screen.notify_change()
            self._stop_evt.wait(1.0)

    def stop(self) -> None:
        self._stop_evt.set()


# --------------------------------------------------------------------------- #
# Chat history: one durable record per conversation
# --------------------------------------------------------------------------- #

# Every key a v1 record has, and what it holds until something is known.
# "project" is the one optional key: the directory under <login>/projects
# the transcript was last found in.
_HISTORY_DEFAULTS = (("name", ""), ("cwd", ""), ("config_dir", None),
                     ("account", ""), ("flags", None), ("last_active", 0.0),
                     ("ended", None))


def _history_time(value: Any) -> bool:
    """A JSON number usable as a time: not a bool (True is an int to Python),
    not NaN or an infinity (json.loads accepts both)."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and float("-inf") < value < float("inf"))


def _history_clean(obj: Any) -> Dict[str, Any]:
    """A parsed record with every known field of the wrong type dropped --
    WRONG TYPE = ABSENT -- so the merge never meets a list where it needs a
    number. A record is a file the user can edit by hand. Unknown keys stay
    as they are: a newer version wrote them, and a merge hands them back."""
    if not isinstance(obj, dict):
        return {}
    out = {}
    for key, value in obj.items():
        if key in ("conversation", "name", "cwd", "account", "project"):
            ok = isinstance(value, str)
        elif key == "config_dir":
            ok = value is None or isinstance(value, str)
        elif key == "flags":
            ok = value is None or (isinstance(value, list) and all(
                isinstance(f, str) for f in value))
        elif key == "last_active":
            ok = _history_time(value)
        elif key == "ended":
            ok = value is None or _history_time(value)
        elif key == "v":
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = True
        if ok:
            out[key] = value
    return out


def _locate_transcript(base: str, fname: str,
                       hint: Any = None) -> Optional[Tuple[str, str]]:
    """(project directory, path) of `fname` under <base>/projects/*/, or None.

    os.listdir, NEVER glob: profile names are whatever the user typed, and a
    '[' or a '*' in one turns the base into a pattern that matches nothing --
    a transcript reported missing while it sits right there. The record's own
    project is looked at first, so a write normally costs one stat."""
    projects = os.path.join(base, "projects")
    if (isinstance(hint, str) and hint and "/" not in hint
            and hint not in (".", "..")):
        path = os.path.join(projects, hint, fname)
        if os.path.isfile(path):
            return hint, path
    try:
        names = sorted(os.listdir(projects))
    except (FileNotFoundError, NotADirectoryError):
        return None
    for name in names:
        path = os.path.join(projects, name, fname)
        if os.path.isfile(path):
            return name, path
    return None


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class HistoryWriter(threading.Thread):
    """Keeps a record of every conversation a wrapped session ran, in
    <root>/<id>.json, with a hard link to its transcript beside it,
    <root>/<id>.jsonl -- until the user deletes them.

    WHY A RECORD OUTLIVES ITS SESSION. Closing a chat used to destroy it as
    far as anything here knew: the session left the queue, and which
    conversation it was, where and as whom went with it. A crash or a reboot
    did the same. A record is written while the chat runs and survives close,
    prune, a daemon restart or crash, and a reboot, so it can be resumed where
    and as it was. Deleting one is the user's, in the CLI's own process --
    never over the socket, where a hook's malformed line could reach it.

    WHY THE HARD LINK. Claude Code deletes transcripts on its own --
    cleanupPeriodDays (30 days by default), `claude purge`, a profile removed
    with `claude-mate accounts rm` -- and a record whose transcript is gone
    keeps only the address of a chat, not the chat. A second name for the
    same inode keeps the conversation for as long as the record exists, and
    costs no disk while Claude still has its copy.

    THREADING. note() runs on the socket thread and does NO I/O: it queues the
    event under a lock that guards nothing else and wakes this thread, which
    gathers a burst for HISTORY_DEBOUNCE_S and writes holding no daemon lock.
    Nothing is read or written at startup -- a fault there would be a launchd
    restart every five seconds -- so the first event is the first I/O. stop()
    only asks: the thread does the final flush itself, and main() stops it
    after the socket server is joined, so every last `end` is in that flush.

    THE RULES, per event, are in _write, numbered as the design numbers them.
    Each event is fenced on its own: a record that cannot be written costs that
    record, and is logged once a minute for as long as it keeps failing."""

    def __init__(self, root: str,
                 clock: Callable[[], float] = time.time) -> None:
        super().__init__(name="history", daemon=True)
        self._root = root
        self._clock = clock           # injectable for tests; real time here
        self._lock = threading.Lock()         # _pending and _wake, nothing else
        self._flush_lock = threading.Lock()   # one flush at a time
        self._pending: Dict[str, List[Tuple[Dict[str, Any],
                                            Optional[float]]]] = {}
        self._wake = threading.Event()
        self._stop_evt = threading.Event()  # NOT `_stop`: Thread.join() calls its own _stop()
        # Everything below belongs to the flush, under _flush_lock.
        self._written: Set[str] = set()       # records this process wrote
        self._last: Dict[str, Dict[str, Any]] = {}   # ...as it last wrote them
        self._present: Dict[str, float] = {}  # when each was last seen there
        self._deleted: Dict[str, float] = {}  # deleted ones, dated (rule 2)
        self._swept = False
        self._newer: Set[str] = set()         # v > 1 records already logged
        self._link_logged = False

    # ---- the socket thread's side ------------------------------------------ #

    def note(self, snap: Optional[Dict[str, Any]],
             ended: Optional[float] = None) -> None:
        """Queue one event: a session as it stands (`ended` None) or as it
        ended (`ended` = when). NO I/O -- the socket thread is the only way in
        for every hook -- and None or a snapshot with no conversation is a
        no-op, so a caller never has to check first.

        A snapshot REPLACES a trailing snapshot of the same session -- a
        burst of keepalives is one write -- but never an end, and never across
        one: an `end` followed by a session that resumed the same chat (an
        account switch) must land as the close and then the reopening."""
        conv = (snap or {}).get("conversation")
        if not conv:
            return
        with self._lock:
            events = self._pending.setdefault(conv, [])
            if (ended is None and events and events[-1][1] is None
                    and events[-1][0].get("key") == snap.get("key")):
                events[-1] = (snap, None)
            else:
                events.append((snap, ended))
            self._wake.set()

    def stop(self) -> None:
        """Ask the thread to finish. It flushes once more on its way out."""
        self._stop_evt.set()
        self._wake.set()

    # ---- the writer's side ------------------------------------------------- #

    def run(self) -> None:
        while not self._stop_evt.is_set():
            self._wake.wait()
            self._stop_evt.wait(HISTORY_DEBOUNCE_S)   # cut short by stop()
            self._flush_fenced()
        self._flush_fenced()        # stopped: whatever arrived since lands now

    def _flush_fenced(self) -> None:
        try:
            self.flush()
        except Exception as exc:                # the thread must outlive it
            _history_fault("flush", exc)

    def flush(self) -> None:
        """Write everything queued so far. The writer thread's, and the tests'.

        The wake Event is cleared IN THE SAME LOCK as the swap: cleared after
        it, a note() landing in between would be in neither this batch nor a
        wake-up, and could sit unwritten until some later chat said anything."""
        with self._flush_lock:
            with self._lock:
                batch, self._pending = self._pending, {}
                self._wake.clear()
            for conv, events in batch.items():
                for snap, ended in events:
                    try:
                        self._write(conv, snap, ended)
                    except Exception as exc:
                        _history_fault(f"record {conv}", exc, key=conv)

    def _write(self, conv: str, snap: Dict[str, Any],
               ended: Optional[float]) -> None:
        """One event for conversation `conv`, through rules 1-8."""
        root = self._root
        # 1. THE DIRECTORY, ON EVERY WRITE. A user who `rm -rf`s it gets it back
        # on the next write, instead of a history that stopped without a word
        # until the daemon restarted. 0700 whatever made it: a record names
        # directories, a login and launch flags.
        os.makedirs(root, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
        if not self._swept:
            self._swept = True
            self._sweep()
        path = os.path.join(root, conv + ".json")
        # 2. DELETED UNDER US. A record this process wrote that is not there
        # now was deleted by the user, and the session still reporting must
        # not put it straight back. DATED BY WHEN IT WAS LAST SEEN, not by
        # when this noticed it gone. A turn begun before the deletion still
        # cannot bring it back: its start was a change, written within the
        # debounce by a write that saw the record -- and the CLI will not
        # delete an open chat at all. But `claude -c` names its chat only
        # once its first turn is under way, so dated by the noticing, that
        # turn -- begun after the deletion -- read as older, and the chat it
        # picked up again was not kept.
        rec = self._read(path) if os.path.isfile(path) else None
        exists = rec is not None
        if exists:
            self._present[conv] = self._clock()
        elif conv in self._written:
            self._deleted[conv] = self._present.pop(conv, self._clock())
            self._written.discard(conv)
            self._last.pop(conv, None)
        turned = bool(snap.get("turned"))
        turn_ts = float(snap.get("turn_ts") or 0.0)
        if not exists:
            # 3. CREATED ONLY BY A TURN -- after a deletion, by a NEW one,
            # begun since the record was last seen. A session that never ran
            # a turn has nothing to resume, and an `end` from a session this
            # daemon never saw knows too little to start a record with.
            if not (turned and turn_ts > self._deleted.get(conv, 0.0)):
                return
            rec = {}
        elif isinstance(rec.get("v"), int) and rec["v"] > 1:
            # 4. (read in _read) NEVER DOWNGRADE a record a newer version
            # wrote: rewriting it as v1 would drop what it knows and this
            # does not.
            if conv not in self._newer:
                self._newer.add(conv)
                log(f"history: {conv} is a v{rec['v']} record, newer than "
                    f"this daemon; leaving it as it is")
            return
        # 5. MERGE into what is there; keys this version does not know stay.
        merged = dict(rec)
        merged["v"] = 1
        merged["conversation"] = conv
        if turned:
            cwd = snap.get("cwd") or ""
            if cwd:
                merged["cwd"] = cwd
            name = os.path.basename(cwd) or snap.get("name") or ""
            if name:
                merged["name"] = name
            if snap.get("config_dir") is not None:
                merged["config_dir"] = snap["config_dir"]
            if snap.get("account"):
                merged["account"] = snap["account"]
            if snap.get("flags") is not None:
                merged["flags"] = list(snap["flags"])
            merged["last_active"] = max(rec.get("last_active", 0.0), turn_ts)
            merged["ended"] = ended         # None reopens a closed record
        elif ended is None:
            return      # NO TURN, NO SAY: only an end may touch the record
        else:
            merged["ended"] = ended
        for key, default in _HISTORY_DEFAULTS:
            merged.setdefault(key, default)
        # 6. KEEP THE TRANSCRIPT beside the record: found here, so the record
        # names the project it is in, and linked below, once the record is.
        src = self._find_transcript(conv, merged)
        # 7. NOTHING NEW, NO WRITE: a keepalive is not a disk write.
        if not (exists and merged == self._last.get(conv)):
            # 8. IN WHOLE OR NOT AT ALL.
            self._replace(path, merged, conv)
            self._written.add(conv)
            self._last[conv] = merged
            self._present[conv] = self._clock()
            # One line per record created and per record closed -- never per
            # write, like the workflow start/finish lines.
            label = f"{merged['name'] or '?'} {conv[:8]}"
            if not exists:
                log(f"history: kept {label}"
                    + (" (closed)" if ended is not None else ""))
            elif ended is not None:
                log(f"history: {label} closed")
        # 6, the link. THE RECORD BEFORE ITS LINK. A writer killed between the
        # two, or a record that could not be written (the raise skips this),
        # leaves a record without its link, which the next write heals --
        # never a link without a record: a transcript nothing lists or
        # deletes, holding its disk long after Claude dropped its own copy.
        # An unchanged record gets it too: Claude may have replaced its copy
        # with a new file, and the link follows it.
        if src is not None:
            self._keep(conv, src)

    @staticmethod
    def _read(path: str) -> Optional[Dict[str, Any]]:
        """Rule 4: the record at `path`, read SAFELY -- None if it is not
        there (deleted between the check and the open, too).

        O_NOFOLLOW, O_NONBLOCK, a regular-file check on the open descriptor
        and a size cap: it sits in a directory the user can write to, and a
        FIFO there would block this thread for good, a symlink to /dev/zero or
        a huge file would be read whole. Anything that is not a JSON object --
        a symlink included, which is replaced and never written through --
        reads as {}, an empty record the merge then fills in properly."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return {}
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return {}
            data = b""
            while len(data) <= HISTORY_READ_MAX:
                chunk = os.read(fd, HISTORY_READ_MAX + 1 - len(data))
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(fd)
        if len(data) > HISTORY_READ_MAX:
            return {}
        try:
            return _history_clean(json.loads(data.decode("utf-8")))
        except Exception:
            return {}

    def _find_transcript(self, conv: str,
                         merged: Dict[str, Any]) -> Optional[str]:
        """Rule 6, the lookup: where the transcript is now, or None -- with
        the project directory it is in noted in `merged`, so the record about
        to be written names it. Not found: the record keeps what it had."""
        cfg = merged.get("config_dir")
        if cfg is None or (cfg and not cfg.startswith("/")):
            return None         # which login is unknown: nowhere to look
        base = os.path.expanduser("~/.claude") if cfg == "" else cfg
        try:
            hit = _locate_transcript(base, conv + ".jsonl",
                                     merged.get("project"))
        except (OSError, ValueError) as exc:
            self._cannot_keep(exc)
            return None
        if hit is None:
            return None
        project, src = hit
        merged["project"] = project
        return src

    def _keep(self, conv: str, src: str) -> None:
        """Rule 6, the link: hard-link the transcript at `src` beside the
        record, once the record is written (see _write). A transcript that
        was not found is never linked, so an existing kept link is left alone
        -- that is the point of keeping it."""
        dst = os.path.join(self._root, conv + ".jsonl")
        try:
            if _same_file(src, dst):
                return
            tmp = os.path.join(self._root,
                               f".{conv}.{secrets.token_hex(4)}.lnk")
            os.link(src, tmp)
            try:
                os.replace(tmp, dst)
            except OSError:
                _unlink_quietly(tmp)
                raise
        except (OSError, ValueError) as exc:
            self._cannot_keep(exc)

    def _cannot_keep(self, exc: Exception) -> None:
        # EXDEV (the history and the transcripts on different volumes),
        # EPERM, ... The record is still worth writing without it.
        if not self._link_logged:
            self._link_logged = True
            log(f"history: cannot keep a transcript beside its record "
                f"({type(exc).__name__}: {exc}); records are still kept, "
                f"transcripts only by Claude Code. Said once.")

    def _replace(self, path: str, merged: Dict[str, Any], conv: str) -> None:
        """Rule 8: a temp file in the same directory, synced, renamed over the
        record, then the directory synced so the rename survives a power cut
        too. Any failure removes the temp file and is raised."""
        text = json.dumps(merged, indent=1, sort_keys=True,
                          ensure_ascii=True) + "\n"
        fd, tmp = tempfile.mkstemp(dir=self._root, prefix="." + conv + ".",
                                   suffix=".tmp")
        done = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            done = True
        finally:
            if not done:
                _unlink_quietly(tmp)
        try:
            dfd = os.open(self._root, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass                # best effort: not every filesystem allows it

    def _sweep(self) -> None:
        """Rule 1, once per process: what a writer killed mid-write left.

        A temp record older than HISTORY_TMP_STALE_S goes. A temp LINK goes
        whatever its age: it is one more name for a transcript -- a chat the
        user may since have deleted, kept alive by a file nothing lists -- and
        its mtime is the transcript's, not its own, so age says nothing about
        it. Only this thread makes one, and only between two calls in _keep,
        so none of this process's is in flight while this runs."""
        now = self._clock()
        try:
            names = os.listdir(self._root)
        except OSError:
            return
        for name in names:
            if not name.startswith("."):
                continue
            path = os.path.join(self._root, name)
            try:
                if name.endswith(".lnk") or (
                        name.endswith(".tmp") and
                        now - os.lstat(path).st_mtime > HISTORY_TMP_STALE_S):
                    os.unlink(path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Mock injector (--mock)
# --------------------------------------------------------------------------- #


class MockInjector(threading.Thread):
    """Injects a few fake sessions cycling through states for demos."""

    # THE WORKFLOW BAND'S DEMO. One session carries a run that walks through
    # every rendering the band has, one mock step (4 s) at a time: running
    # with the count advancing 0..5, running with a failed agent, paused on a
    # usage limit, done, ended -- then none, and round again. (st, done,
    # failed); None is '-'.
    WF_DEMO_NAME = "release-risk-sweep"
    WF_DEMO_TOTAL = 5
    WF_DEMO = (("R", 0, None), ("R", 1, None), ("R", 2, None), ("R", 3, None),
               ("R", 4, None), ("R", 5, None), ("E", 4, 1), ("P", 4, None),
               ("D", 5, None), ("F", 3, 2), None)

    @classmethod
    def demo_wf(cls, step: int) -> str:
        """Field 8 for mock step `step`, exactly as a wrapper would send it.

        The clock advances 4 s a step while the run is going and then holds,
        as a finished run's does; tokens grow. A running record's clock is
        anchored by the daemon, so between steps it ticks on its own."""
        i = step % len(cls.WF_DEMO)
        spec = cls.WF_DEMO[i]
        if spec is None:
            return "-"
        st, done, failed = spec
        total = cls.WF_DEMO_TOTAL
        lived = min(i, 6)                       # the run stops at its E step
        pct = "" if st == "P" else str(100 * done // total)   # P: no bar
        note = "til 3:30pm" if st == "P" else ""
        return ";".join((st, str(done), str(total),
                         "" if failed is None else str(failed), pct,
                         str(37 + 4 * lived), str(18400 + 97300 * lived),
                         note, cls.WF_DEMO_NAME))

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
        # Seed initial states covering every queue class at once -- in QUEUE
        # (alphabetical) order. The ticker is already running and anchors the
        # selection to whatever it renders, so a tick landing between two seeds
        # pins the screen to the first one; seeded in this order that is always
        # the head, api, which is where the demo workflow is.
        seeds = ["working", "waiting", "error", "done"]
        for (sid, name, cwd), st in sorted(zip(fakes, seeds),
                                           key=lambda x: x[0][1]):
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

        # The workflow rides on api: alphabetically first, so it is the queue
        # head and therefore what a freshly started --mock device shows.
        demo_sid, demo_name, demo_cwd = fakes[1]
        # What each session last SAID, which is what a wrapper re-sends with
        # every keepalive.
        said = {sid: st for (sid, _n, _c), st in zip(fakes, seeds)}
        step = 0
        while not self._stop_evt.is_set():
            # Advance one session's state every ~4s so the screen/LED move.
            sid, name, cwd = fakes[step % len(fakes)]
            new_state = cycle[(step // len(fakes)) % len(cycle)]
            said[sid] = new_state
            # EVERY step re-lists the demo workflow, through Registry.update
            # and the parser like a real line, so it is exercised end to end
            # and never goes stale.
            wf = self.demo_wf(step)
            if sid == demo_sid:
                self._reg.update(new_state, sid, name, cwd, wf=wf)
            else:
                self._reg.update(new_state, sid, name, cwd)
                # A keepalive repeats what it said -- except that a finished
                # turn is said as "idle" (the daemon is what makes it DONE
                # until acknowledged); repeating "done" would re-raise an
                # alert the user had just acknowledged, every four seconds.
                st = said[demo_sid]
                self._reg.update("idle" if st == "done" else st, demo_sid,
                                 demo_name, demo_cwd, wf=wf)
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
        "--trust-any-network",
        action="store_true",
        default=os.environ.get("CLAUDE_MATE_TRUST_ANY", "") == "1",
        help="open the TCP listener on every network, not only trusted ones "
             "(the pre-gate behaviour; see `claude-mate trust`)",
    )
    parser.add_argument(
        "--ble",
        action="store_true",
        default=os.environ.get("CLAUDE_MATE_BLE", "") == "1",
        help="also serve the protocol over Bluetooth LE for battery devices "
             "(requires a shared token and the 'bleak' package; off by default)",
    )
    parser.add_argument(
        "--ble-address",
        default=os.environ.get("CLAUDE_MATE_BLE_ADDRESS") or None,
        help="connect to this BLE address instead of scanning for the service "
             "(macOS reports its own per-host UUIDs, not MAC addresses)",
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
    ble: Optional[Any] = None
    mdns: Optional[MdnsAdvertiser] = None
    net_gate: Optional[NetworkGate] = None

    # Both wireless transports are opt-in AND fail closed: an unauthenticated
    # link would let anyone in range read session names and raise windows, so a
    # missing token disables it rather than weakening it. They share the token,
    # the queue and the protocol -- the only difference is which radio.
    if args.tcp or args.ble:
        token = ensure_token(args.token)
        rx: "queue.Queue[str]" = queue.Queue()
        if not token:
            flags = " / ".join(f for f, on in (("--tcp", args.tcp),
                                               ("--ble", args.ble)) if on)
            log(f"ERROR: {flags} needs a shared token and one could not be "
                f"created. Set CLAUDE_MATE_TOKEN, pass --token, or write one "
                f"to {DEFAULT_TOKEN_FILE}. Continuing with USB serial only.")
        else:
            # The cable can now provision the radio. See SerialLink._provision().
            serial_link.set_provision_token(token)
            if args.tcp:
                # BUILT HERE, OPENED BY THE GATE. The listener's existence is
                # now a function of which network the Mac is on, and that answer
                # changes while the daemon runs -- a laptop closed at home and
                # opened in a cafe never restarts anything. NetworkGate owns
                # start()/stop() from here on; LinkHub gets the object either
                # way, and a closed listener simply has no clients to write to.
                net = NetLink(args.tcp_bind, args.tcp_port, token, rx)
                mdns = MdnsAdvertiser(args.tcp_port)
                if args.trust_any_network:
                    if net.start():
                        mdns.start()
                    else:
                        net = None
                else:
                    net_gate = NetworkGate(net, mdns)
                    net_gate.evaluate()   # settle it before the banner prints
                    net_gate.start()
            if args.ble:
                # Imported here, not at module scope: bleak is optional, and
                # tools/test_net_link.py exec_modules this file by path, where a
                # module-level `from blelink import ...` would need daemon/ on
                # sys.path. Same reason webbridge is imported lazily below.
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                try:
                    from blelink import BleLink
                    candidate_ble = BleLink(token, rx,
                                            address=args.ble_address, log=log)
                    if candidate_ble.start():
                        ble = candidate_ble
                except Exception as exc:                # noqa: BLE001
                    log(f"BLE transport unavailable: {exc}")
            if net or ble:
                link = LinkHub(serial_link, [net, ble], rx)
        if args.tcp:
            # Say which of the three states this is, because "on" and "waiting
            # for a network you trust" are very different things to read at
            # 2 a.m. when a device will not link.
            if net is None:
                where = "DISABLED"
            elif net_gate is not None:
                where = "on" if net_gate.is_open() else "closed (untrusted net)"
            else:
                where = "on (gate off)"
            log(f"  tcp    : {args.tcp_bind}:{args.tcp_port} ({where})")
        if args.ble:
            log(f"  ble    : {'scanning' if ble else 'DISABLED'}"
                f"{' for ' + args.ble_address if args.ble_address else ''}")

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
    # ---- the saved-account list the device's picker shows ------------------ #
    # Pushed on every handshake. Switching accounts on the device means picking
    # one of these -- logins that already exist, each with its own credentials
    # -- never signing in again, so the device must know their names.
    _handshake_extra: List[Any] = []

    def send_accounts(with_chips: bool = False) -> None:
        names = account_profiles()
        if not names:
            return
        # Say what will not fit rather than quietly showing the first five: a
        # picker that silently omits the account you were looking for reads as
        # "that login is gone".
        hidden = len(account_profiles(0)) - len(names)
        if hidden > 0:
            log(f"accounts: {hidden} more not shown -- the picker holds {ACCT_MAX}")
        chips = [""] * len(names)
        if with_chips:
            for i, n in enumerate(names):
                # "" -- not the expanded path -- is how the wrapper spells "the
                # default login". Claude Code suffixes the Keychain service with
                # a hash of $CLAUDE_CONFIG_DIR only for profiles; ~/.claude's
                # entry has no suffix, so passing its path finds no token and
                # the account lists with no limit -- the one number the choice
                # is actually made on.
                chips[i] = account_headroom_chip("" if n == "default"
                                                 else account_dir(n))
        sent = 0
        for i, n in enumerate(names):
            try:
                # The RETURN VALUE, not just the absence of an exception: a
                # closed port answers False rather than raising, and counting
                # that as sent would leave us decoding the device's presses
                # against a list it never received.
                if not link.write_line(f"L|{i}|{len(names)}|{n[:14]}|{chips[i]}"):
                    break
                sent = i + 1
            except Exception as exc:
                log(f"accounts: {exc}")
                break
        # Only what actually went out: a burst cut short leaves the device
        # holding the rows it got, and that prefix is what its indices mean.
        set_pushed_accounts(names[:sent])
        if not sent:
            log("saved accounts: nothing reached the device (link down)")
            return
        log("saved accounts -> device: " +
            ", ".join(f"{i}:{n}{(' ' + chips[i]) if chips[i] else ''}"
                      for i, n in enumerate(names[:sent])))

    _last_push: List[List[str]] = [[]]        # the list this daemon last aimed at

    def push_accounts() -> None:
        """Names first, then chips once the network has answered.

        The names are free and the device needs them immediately; the remaining
        limits cost one HTTPS round trip per profile, which must not sit in the
        handshake path. So: send what we know, then send it again enriched.
        Which account has headroom is the whole basis of the choice, so it is
        worth the second push."""
        _last_push[0] = account_profiles()
        send_accounts(False)

        def enrich() -> None:
            send_accounts(True)

        threading.Thread(target=enrich, name="acct-usage", daemon=True).start()

    _handshake_extra.append(push_accounts)

    def refresh_accounts() -> None:
        """Re-push when the saved logins have changed under us.

        The handshake is the only other push, and profiles come and go while the
        device stays linked -- picking a new name at the wrapper's account prompt
        creates one. Since the device answers with an INDEX, a list that drifts
        here and not there does not merely look stale, it decodes to the wrong
        name. Cheap: one listdir, and a push only on a real difference.

        Compared against what was last SENT AT, not what reached the device.
        While the device is unplugged nothing reaches it, and a comparison
        against "what it received" would find a difference every five seconds
        forever -- each one costing an HTTPS usage call per profile, for a
        picker nobody can see. A reconnect re-pushes from the handshake anyway."""
        if account_profiles() != _last_push[0]:
            push_accounts()

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
                    _link.write_line("G|1" if on else "G|0")
                except Exception as exc:
                    log(f"controller mode: {exc}")
                log("device controller mode: %s" % ("ON" if on else "off"))

            candidate.on_grab = _controller
            _handshake_extra.append(
                lambda _b=candidate: _controller(_b.grabbed()))
            if candidate.start():
                web = candidate
        log(f"  web    : {'on' if web else 'DISABLED'}")

    # Everything the device needs re-asserted when it says hello. A list rather
    # than one callback because these are independent: the account list must be
    # pushed whether or not --web is on, and controller mode only exists when it
    # is. Edge-driven state that is never re-asserted is correct only while
    # nothing reconnects, which is not a property this device has.
    def _on_handshake() -> None:
        for fn in _handshake_extra:
            try:
                fn()
            except Exception as exc:
                log(f"handshake extra: {exc}")

    screen.on_handshake_extra = _on_handshake

    socket_server = SocketServer(args.sock, registry, on_update, on_haptic)
    # `pair` over the socket reaches the radio. Left unset when BLE is not
    # running, so the command answers "BLE is not running" rather than silently
    # doing nothing -- the failure a terminal cannot otherwise see.
    if ble is not None:
        socket_server.on_pair_request = ble.arm_pairing
    # The CLI's hooks. `press` goes through the SAME _dispatch that handles a
    # B| line off the wire, so `claude-mate go` and pressing GO on the device
    # cannot mean two different things -- there is one implementation of GO and
    # the terminal is just another way to reach it.
    socket_server.screen = screen
    socket_server.on_press = lambda code: button_reader.press(code)
    socket_server.on_select = screen.select_by
    socket_server.on_accounts_refresh = refresh_accounts
    # SERIAL ONLY, NEVER THE HUB. link.write_line() fans a line out to every
    # connected device, and `I|` is the one line that must not be broadcast:
    # sending it to the fleet would move a Wi-Fi device and a BLE device onto
    # whichever transport the person at the cable happened to pick, rebooting
    # both. The device being reconfigured is by definition the one on the cable.
    socket_server.on_device_link = \
        lambda mode: serial_link.write_line(f"I|{mode.upper()}")
    # THE CHAT HISTORY -- but never the demo's. --mock's sessions are fake,
    # and its MockInjector writes the registry directly, past the socket and
    # field 9: nothing it does is a chat anyone had, and the history is a
    # promise about chats you had. Nothing is read or written here; the
    # writer's first I/O is the first conversation a wrapper names.
    history: Optional[HistoryWriter] = None
    if args.mock:
        log("  history: off (--mock)")
    else:
        history = HistoryWriter(os.path.expanduser(HISTORY_DIR))
        socket_server.history = history
        log(f"  history: {HISTORY_DIR}")
    button_reader = ButtonReader(link, screen)
    button_reader.on_ack = on_ack
    button_reader.bridge = web
    maintainer = SerialMaintainer(link, screen)
    ticker = Ticker(screen, registry, on_slow_tick=refresh_accounts)

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
    # Not in `threads`: it is stopped on its own, last -- see the finally.
    if history is not None:
        history.start()

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
        link.close()      # LinkHub.close() also stops every wireless transport
        # THE HISTORY LAST. Every thread that can hand it an event -- the
        # socket server above all, whose final `end`s are the ones that say a
        # chat closed rather than broke -- has been stopped and joined above,
        # so the writer's own last flush has everything there will be.
        # Stopped with the others, a note landing after that flush would be
        # lost, and a chat that closed cleanly would read as interrupted.
        if history is not None:
            history.stop()
            history.join(timeout=HISTORY_JOIN_S)
    return 0


if __name__ == "__main__":
    sys.exit(main())
