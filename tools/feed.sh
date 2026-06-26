#!/usr/bin/env bash
#
# feed.sh — tiny manual test helper for the Claude Mate daemon.
#
# Pipes a "<state>|<session_id>|<name>" line into the daemon's Unix socket so
# you can exercise the registry, status wheel and carousel without Claude Code
# hooks or real hardware.
#
# Usage:
#   tools/feed.sh "working|sid-123|webapp"
#   tools/feed.sh working sid-123 webapp        # fields as separate args
#   echo "error||api" | tools/feed.sh           # line on stdin
#
# Socket defaults to /tmp/claude-mate.sock (override with CLAUDE_MATE_SOCK).
#
# Requires `nc` with Unix-socket support (-U), which is standard on macOS.

set -euo pipefail

SOCK="${CLAUDE_MATE_SOCK:-/tmp/claude-mate.sock}"

# Build the line from arguments, or fall back to stdin.
if [ "$#" -eq 1 ]; then
  LINE="$1"
elif [ "$#" -ge 2 ]; then
  # Join all args with the '|' field separator: state sid name [cwd...]
  LINE="$1"
  shift
  for part in "$@"; do
    LINE="${LINE}|${part}"
  done
elif [ ! -t 0 ]; then
  # No args but stdin is a pipe/file: read one line from it.
  IFS= read -r LINE || true
else
  echo "usage: $(basename "$0") \"state|session_id|name\"" >&2
  echo "       $(basename "$0") state session_id name" >&2
  echo "       echo 'state|sid|name' | $(basename "$0")" >&2
  exit 2
fi

if [ -z "${LINE:-}" ]; then
  echo "feed.sh: empty input line" >&2
  exit 2
fi

if [ ! -S "$SOCK" ]; then
  echo "feed.sh: socket not found at $SOCK (is the daemon running?)" >&2
  exit 1
fi

# Fire the line into the socket.
#
# We pipe via stdin so nc closes the connection as soon as the line is sent.
# `-w1` caps the wait at 1s. The macOS (BSD) nc does NOT understand GNU's `-q`,
# so we deliberately avoid it; `-w1` is supported by both BSD and GNU netcat.
printf '%s\n' "$LINE" | nc -U -w1 "$SOCK"

echo "feed.sh: sent '$LINE' -> $SOCK" >&2
