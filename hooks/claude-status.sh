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
# CRITICAL CONTRACT:
#   This hook MUST NEVER block or fail a Claude turn. It swallows every error and
#   ALWAYS exits 0. For that reason we deliberately do NOT use `set -e`.
#
# Config (env, with sane defaults):
#   CLAUDE_MATE_SOCK   Unix socket path (default: /tmp/claude-mate.sock)
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

# --- Extract session_id and basename(cwd) robustly via python3 -------------
# python3 gives us proper JSON parsing. If python3 is missing or the JSON is
# malformed, we fall back to empty fields and still send what we have.
SID=""
NAME=""
if command -v python3 >/dev/null 2>&1; then
    # The python helper prints two lines: session_id then name (basename of cwd).
    # It swallows all its own errors and prints empty strings on failure.
    PARSED="$(
        printf '%s' "$PAYLOAD" | python3 -c '
import sys, json, os
sid = ""
name = ""
try:
    data = json.load(sys.stdin)
    if isinstance(data, dict):
        sid = data.get("session_id") or ""
        cwd = data.get("cwd") or ""
        if cwd:
            name = os.path.basename(os.path.normpath(cwd))
except Exception:
    # Any parse error -> emit empty fields; the hook must not fail.
    pass
# Strip the field separator/newlines from values so the wire format stays clean.
sid = str(sid).replace("|", "").replace("\n", "").replace("\r", "")
name = str(name).replace("|", "").replace("\n", "").replace("\r", "")
print(sid)
print(name)
' 2>/dev/null || true
    )"
    # Split the two lines into SID / NAME.
    SID="$(printf '%s\n' "$PARSED" | sed -n '1p' 2>/dev/null || true)"
    NAME="$(printf '%s\n' "$PARSED" | sed -n '2p' 2>/dev/null || true)"
fi

# Sanitize the state too (defensive; it comes from our own config but be safe).
STATE="$(printf '%s' "$STATE" | tr -d '|\n\r' 2>/dev/null || true)"

# Build the single wire line: <state>|<session_id>|<name>
LINE="${STATE}|${SID}|${NAME}"

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
