#!/usr/bin/env bash
#
# Claude Mate uninstaller
# ----------------------------------------------------------------------------
# - Unloads + removes the macOS LaunchAgent
# - Removes the installed status hook
# - Leaves the user's ~/.claude/settings.json UNTOUCHED (we only tell you
#   which block to delete by hand, to avoid clobbering hand edits).
#
# Idempotent and safe to re-run.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CLAUDE_DIR="${HOME}/.claude"
CLAUDE_SETTINGS="${CLAUDE_DIR}/settings.json"
HOOK_DST="${CLAUDE_DIR}/hooks/claude-status.sh"

LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_LABEL="com.claudemate.daemon"
PLIST_DST="${LAUNCH_AGENTS_DIR}/${PLIST_LABEL}.plist"

info()  { printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

info "Claude Mate uninstaller"

# --- 1. Unload + remove the LaunchAgent -------------------------------------
if launchctl list "${PLIST_LABEL}" >/dev/null 2>&1; then
    info "Unloading LaunchAgent ${PLIST_LABEL}..."
    launchctl unload "${PLIST_DST}" 2>/dev/null || true
    # Fallback for newer launchd.
    uid="$(id -u)"
    launchctl bootout "gui/${uid}/${PLIST_LABEL}" 2>/dev/null || true
    ok "Unloaded ${PLIST_LABEL}"
else
    info "LaunchAgent ${PLIST_LABEL} not currently loaded."
fi

if [[ -f "${PLIST_DST}" ]]; then
    rm -f "${PLIST_DST}"
    ok "Removed ${PLIST_DST}"
else
    info "No plist at ${PLIST_DST} (already removed)."
fi

# --- 2. Remove the installed hook -------------------------------------------
if [[ -f "${HOOK_DST}" ]]; then
    rm -f "${HOOK_DST}"
    ok "Removed hook ${HOOK_DST}"
else
    info "No hook at ${HOOK_DST} (already removed)."
fi

# --- 3. settings.json — leave it alone, just instruct -----------------------
echo
warn "Your settings.json was NOT modified."
warn "If you no longer want the Claude Mate hooks, edit:"
warn "  ${CLAUDE_SETTINGS}"
warn "and delete any hook entries whose command points at:"
warn "  ${HOOK_DST}"
warn "(the UserPromptSubmit / Notification / Stop / StopFailure entries"
warn " that reference claude-status.sh)."
echo

ok "Claude Mate uninstalled."
info "Note: log files in ~/Library/Logs/claude-mate.*.log were left in place."
