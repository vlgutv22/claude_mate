#!/usr/bin/env bash
#
# Claude Mate — Claude Code status hook
# =====================================
# Invoked by Claude Code on hook events. Receives the hook JSON on stdin and the
# desired state as the first argument ($1), one of: working|waiting|done|error.
#
# It extracts session_id and basename(cwd) from the JSON and fires a single
# newline-terminated line to the Claude Mate daemon's Unix domain socket:
#
#     <state>|<session_id>|<name>
#
# NOT-DONE-YET RULE (Stop):
#   A turn can "end" with work still in flight — a dynamic workflow, background
#   agents, a backgrounded shell, a monitor, an MCP task — or with the session
#   parked on a one-shot wakeup it scheduled for itself (ScheduleWakeup, a /loop
#   tick, a one-shot cron). Claude Code says so in the Stop payload:
#
#     background_tasks[]  in-flight background work (running/pending)
#     session_crons[]     scheduled tasks that will wake this session later
#                         (recurring: false == a one-shot continuation)
#
#   Reporting 'done' there is a lie: the device buzzes DONE and shows IDLE while
#   the session keeps working, then goes quiet for the real finish. So when
#   either is non-empty we downgrade 'done' to 'working' and let the NEXT Stop
#   (the one that lands with nothing in flight) report the finish.
#
#   Recurring crons are deliberately NOT counted: a session that owns a daily
#   schedule is finished for now, and suppressing its DONE forever would hide it
#   from the device for good. Both fields are optional — a Claude Code build
#   that does not send them behaves exactly as before.
#
# CRITICAL CONTRACT:
#   This hook MUST NEVER block or fail a Claude turn. It swallows every error and
#   ALWAYS exits 0. For that reason we deliberately do NOT use `set -e`.
#
# Config (env, with sane defaults):
#   CLAUDE_MATE_SOCK      Unix socket path (default: /tmp/claude-mate.sock)
#   CLAUDE_MATE_DRY_RUN   if set to 1, print the wire line instead of sending it
#                         (used by tools/test_hook_state.py; handy for debugging
#                         a wiring by hand)
#
# Install: copy this file to ~/.claude/hooks/claude-status.sh and `chmod +x` it,
# then merge hooks/settings.snippet.json into ~/.claude/settings.json.

# NOTE: intentionally NO `set -e` / `set -u` / `set -o pipefail` here.
# Nothing this script does is allowed to abort the Claude turn.

# Desired state passed by the hook config (e.g. "working", "waiting", ...).
STATE="${1:-}"

# Socket path (override via env).
SOCK="${CLAUDE_MATE_SOCK:-/tmp/claude-mate.sock}"

# Read the hook JSON from stdin (may be empty if invoked oddly). Never block long.
PAYLOAD="$(cat 2>/dev/null || true)"

# --- Extract session_id, basename(cwd) and in-flight work via python3 ------
# python3 gives us proper JSON parsing. If python3 is missing or the JSON is
# malformed, we fall back to empty fields and still send what we have.
SID=""
NAME=""
INFLIGHT=0
if command -v python3 >/dev/null 2>&1; then
    # The python helper prints three lines: session_id, name (basename of cwd),
    # and the count of in-flight background work.
    # It swallows all its own errors and prints empty/zero on failure.
    PARSED="$(
        printf '%s' "$PAYLOAD" | python3 -c '
import sys, json, os
sid = ""
name = ""
inflight = 0
try:
    data = json.load(sys.stdin)
    if isinstance(data, dict):
        sid = data.get("session_id") or ""
        cwd = data.get("cwd") or ""
        if cwd:
            name = os.path.basename(os.path.normpath(cwd))
        # Work that outlives the turn (see NOT-DONE-YET RULE above). Both keys
        # are optional and only sent on Stop/SubagentStop.
        tasks = data.get("background_tasks")
        if isinstance(tasks, list):
            inflight += sum(1 for t in tasks if isinstance(t, dict))
        crons = data.get("session_crons")
        if isinstance(crons, list):
            # One-shot wakeups only: they are this turn continuing later. A
            # recurring cron is a schedule the session merely owns.
            inflight += sum(1 for c in crons
                            if isinstance(c, dict) and c.get("recurring") is False)
except Exception:
    # Any parse error -> emit empty fields; the hook must not fail.
    pass
# Strip the field separator/newlines from values so the wire format stays clean.
sid = str(sid).replace("|", "").replace("\n", "").replace("\r", "")
name = str(name).replace("|", "").replace("\n", "").replace("\r", "")
print(sid)
print(name)
print(inflight)
' 2>/dev/null || true
    )"
    # Split the three lines into SID / NAME / INFLIGHT.
    SID="$(printf '%s\n' "$PARSED" | sed -n '1p' 2>/dev/null || true)"
    NAME="$(printf '%s\n' "$PARSED" | sed -n '2p' 2>/dev/null || true)"
    INFLIGHT="$(printf '%s\n' "$PARSED" | sed -n '3p' 2>/dev/null || true)"
fi

# Sanitize the state too (defensive; it comes from our own config but be safe).
STATE="$(printf '%s' "$STATE" | tr -d '|\n\r' 2>/dev/null || true)"

# The turn ended but work did not: report the session as still working, so the
# device keeps showing it as busy instead of buzzing a premature DONE. Only
# 'done' is downgraded — 'waiting' and 'error' still need you either way.
case "$INFLIGHT" in
    ''|*[!0-9]*) INFLIGHT=0 ;;
esac
if [ "$STATE" = "done" ] && [ "$INFLIGHT" -gt 0 ]; then
    STATE="working"
fi

# Build the single wire line: <state>|<session_id>|<name>
LINE="${STATE}|${SID}|${NAME}"

# --- Dry run: print the line instead of sending it --------------------------
# Lets the test suite (and you, by hand) check what a given payload reports
# without a daemon, a socket or netcat in the picture.
if [ "${CLAUDE_MATE_DRY_RUN:-}" = "1" ]; then
    printf '%s\n' "$LINE"
    exit 0
fi

# --- Fire-and-forget to the daemon socket ----------------------------------
# Use `nc -U` with a short timeout so we never hang if the daemon is down.
# Everything is wrapped to swallow errors; a missing socket is a silent no-op.
if command -v nc >/dev/null 2>&1; then
    # -U  : Unix domain socket
    # -w1 : 1 second timeout (connect + I/O); keeps us fast and non-blocking
    # We pipe the line in and discard all output/errors.
    printf '%s\n' "$LINE" | nc -U -w1 "$SOCK" >/dev/null 2>&1 || true
fi

# ALWAYS succeed — never fail the Claude turn.
exit 0
