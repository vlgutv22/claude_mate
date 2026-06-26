#!/usr/bin/env bash
#
# Claude Mate installer
# ----------------------------------------------------------------------------
# - Installs the Claude Code status hook into ~/.claude/hooks/
# - Shows (and optionally merges) the settings.json hooks snippet
# - Installs + loads the macOS LaunchAgent that runs the daemon at login
#
# Idempotent and safe to re-run.
#
set -euo pipefail

# --- Resolve paths ----------------------------------------------------------
# Directory of this script (install/), and the repo root one level up.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

HOOK_SRC="${REPO_DIR}/hooks/claude-status.sh"
SNIPPET_SRC="${REPO_DIR}/hooks/settings.snippet.json"

CLAUDE_DIR="${HOME}/.claude"
CLAUDE_HOOKS_DIR="${CLAUDE_DIR}/hooks"
CLAUDE_SETTINGS="${CLAUDE_DIR}/settings.json"
HOOK_DST="${CLAUDE_HOOKS_DIR}/claude-status.sh"

PLIST_TEMPLATE="${SCRIPT_DIR}/com.claudemate.daemon.plist"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_LABEL="com.claudemate.daemon"
PLIST_DST="${LAUNCH_AGENTS_DIR}/${PLIST_LABEL}.plist"

LOG_DIR="${HOME}/Library/Logs"

# --- Pretty printing helpers ------------------------------------------------
info()  { printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
err()   { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; }

info "Claude Mate installer"
info "Repo:   ${REPO_DIR}"

# --- 1. Detect python3 ------------------------------------------------------
PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
    err "python3 not found on PATH. Install Python 3.9+ and re-run."
    exit 1
fi
info "Python: ${PYTHON_BIN}"

# --- 2. Install the hook ----------------------------------------------------
if [[ ! -f "${HOOK_SRC}" ]]; then
    err "Hook source not found: ${HOOK_SRC}"
    err "Make sure the repo is complete before running install.sh."
    exit 1
fi

mkdir -p "${CLAUDE_HOOKS_DIR}"
cp "${HOOK_SRC}" "${HOOK_DST}"
chmod +x "${HOOK_DST}"
ok "Installed hook -> ${HOOK_DST}"

# --- 3. Ensure log directory exists -----------------------------------------
mkdir -p "${LOG_DIR}"

# --- 4. settings.json hooks snippet -----------------------------------------
# We never blindly overwrite the user's settings.json. We either offer a jq
# merge (with a backup) or print the snippet and instructions.
echo
info "Claude Code hooks configuration"
echo  "----------------------------------------------------------------------"

if [[ -f "${SNIPPET_SRC}" ]]; then
    echo "Add the following 'hooks' block to ${CLAUDE_SETTINGS}:"
    echo
    cat "${SNIPPET_SRC}"
    echo

    if command -v jq >/dev/null 2>&1; then
        # jq is available -> offer to merge automatically.
        if [[ ! -t 0 ]]; then
            # Non-interactive run (e.g. piped). Don't touch settings; just instruct.
            warn "Non-interactive shell: skipping automatic merge."
            warn "Run install.sh in a terminal to merge automatically, or merge the snippet above by hand."
        else
            read -r -p "Merge this snippet into ${CLAUDE_SETTINGS} now with jq? [y/N] " reply
            case "${reply}" in
                [yY]|[yY][eE][sS])
                    if [[ -f "${CLAUDE_SETTINGS}" ]]; then
                        backup="${CLAUDE_SETTINGS}.bak.$(date +%Y%m%d%H%M%S)"
                        cp "${CLAUDE_SETTINGS}" "${backup}"
                        ok "Backed up existing settings -> ${backup}"
                    else
                        echo '{}' > "${CLAUDE_SETTINGS}"
                    fi
                    tmp="$(mktemp)"
                    # Deep-merge: snippet's hooks take precedence on key collisions.
                    if jq -s '.[0] * .[1]' "${CLAUDE_SETTINGS}" "${SNIPPET_SRC}" > "${tmp}"; then
                        mv "${tmp}" "${CLAUDE_SETTINGS}"
                        ok "Merged hooks into ${CLAUDE_SETTINGS}"
                    else
                        rm -f "${tmp}"
                        err "jq merge failed; your settings.json was NOT modified."
                        warn "Merge the snippet above into ${CLAUDE_SETTINGS} manually."
                    fi
                    ;;
                *)
                    info "Skipping automatic merge. Add the snippet above manually."
                    ;;
            esac
        fi
    else
        warn "jq not found. Add the snippet above into ${CLAUDE_SETTINGS} manually."
        warn "Tip: 'brew install jq' to enable automatic merging next time."
    fi
else
    warn "Snippet not found at ${SNIPPET_SRC}."
    warn "You must add a 'hooks' block to ${CLAUDE_SETTINGS} that runs:"
    warn "  ${HOOK_DST}"
    warn "on UserPromptSubmit, Notification, Stop, and StopFailure."
fi
echo  "----------------------------------------------------------------------"
echo

# --- 5. Install the LaunchAgent ---------------------------------------------
if [[ ! -f "${PLIST_TEMPLATE}" ]]; then
    err "Plist template not found: ${PLIST_TEMPLATE}"
    exit 1
fi

mkdir -p "${LAUNCH_AGENTS_DIR}"

# Unload any previously loaded agent so we can cleanly replace it (idempotent).
if launchctl list "${PLIST_LABEL}" >/dev/null 2>&1; then
    info "Unloading existing LaunchAgent before reinstall..."
    launchctl unload "${PLIST_DST}" 2>/dev/null || true
fi

# Substitute placeholders. Use a temp file then move into place atomically.
# '|' is used as the sed delimiter because paths contain '/'.
tmp_plist="$(mktemp)"
sed \
    -e "s|__REPO__|${REPO_DIR}|g" \
    -e "s|__PYTHON__|${PYTHON_BIN}|g" \
    -e "s|__HOME__|${HOME}|g" \
    "${PLIST_TEMPLATE}" > "${tmp_plist}"
mv "${tmp_plist}" "${PLIST_DST}"
ok "Installed LaunchAgent -> ${PLIST_DST}"

# Load (start) the agent. 'load -w' marks it enabled and starts it now.
if launchctl load -w "${PLIST_DST}" 2>/dev/null; then
    ok "Loaded LaunchAgent ${PLIST_LABEL}"
else
    warn "launchctl load reported an issue; trying to bootstrap..."
    # Fallback for newer launchd that prefers bootstrap.
    uid="$(id -u)"
    launchctl bootstrap "gui/${uid}" "${PLIST_DST}" 2>/dev/null || true
fi

echo
ok "Claude Mate installed."
info "Daemon logs:"
info "  out: ${LOG_DIR}/claude-mate.out.log"
info "  err: ${LOG_DIR}/claude-mate.err.log"
info "Check status:  launchctl list | grep ${PLIST_LABEL}"
info "Uninstall:     ${SCRIPT_DIR}/uninstall.sh"
