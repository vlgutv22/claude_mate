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
# --yes / -y (or CLAUDE_MATE_ASSUME_YES=1) answers the one question this script
# asks, which is the difference between "one command" and "one command, then keep
# an eye out for a prompt". Opt-in on purpose: the question is whether to edit
# ~/.claude/settings.json, and a script that rewrites your editor config without
# being asked is not one to trust twice. It backs the file up either way.
ASSUME_YES="${CLAUDE_MATE_ASSUME_YES:-0}"
for arg in "$@"; do
    case "$arg" in
        -y|--yes) ASSUME_YES=1 ;;
        -h|--help)
            echo "usage: install.sh [--yes]"
            echo "  --yes   merge the hooks snippet into ~/.claude/settings.json"
            echo "          without asking (the file is backed up first)"
            exit 0 ;;
        *) echo "install.sh: unknown option $arg" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

HOOK_SRC="${REPO_DIR}/hooks/claude-status.sh"
SNIPPET_SRC="${REPO_DIR}/hooks/settings.snippet.json"

# Every runtime dependency this repo has, and the only list of them. The daemon
# needs pyserial (USB) and bleak (BLE); the PTY wrapper needs pyte. Adding one
# means editing a requirements file, never this script -- see step 5b.
REQ_FILES=("${REPO_DIR}/daemon/requirements.txt" "${REPO_DIR}/bin/requirements.txt")
REQ_ARGS=()
for _req in "${REQ_FILES[@]}"; do
    [[ -f "${_req}" ]] && REQ_ARGS+=(-r "${_req}")
done

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

# --- 1. Detect python3, then stop depending on it ---------------------------
# THE INTERPRETER AND ITS PACKAGES MUST TRAVEL TOGETHER. This step used to bake
# `command -v python3` -- typically /usr/local/bin/python3 -- straight into the
# LaunchAgent. That path is a symlink the python.org installer rewrites on every
# minor release, so the day 3.14 landed, a daemon that had worked for months
# began pointing at an interpreter with an empty site-packages. pyserial was
# still installed, in 3.12's site-packages, where nothing was looking. launchd's
# KeepAlive turned that into 1786 restarts and a 50 MB log.
#
# A virtualenv in the repo fixes it at the cause: .venv/bin/python3 is a symlink
# to a CONCRETE version, and the packages live beside it. A later Python release
# cannot move one without the other, and if the base interpreter is genuinely
# removed the failure is a loud "bad interpreter" rather than a silent
# ModuleNotFoundError about a package you know you installed.
BOOTSTRAP_PYTHON="$(command -v python3 || true)"
if [[ -z "${BOOTSTRAP_PYTHON}" ]]; then
    err "python3 not found on PATH. Install Python 3.9+ and re-run."
    exit 1
fi
info "Python: ${BOOTSTRAP_PYTHON}"

VENV_DIR="${REPO_DIR}/.venv"
PYTHON_BIN=""
if "${BOOTSTRAP_PYTHON}" -m venv --help >/dev/null 2>&1; then
    if [[ ! -x "${VENV_DIR}/bin/python3" ]]; then
        info "Creating a virtualenv for the daemon -> ${VENV_DIR}"
        # --upgrade-deps is 3.9+, but pointless here and slow; skip it.
        if ! "${BOOTSTRAP_PYTHON}" -m venv "${VENV_DIR}"; then
            warn "Could not create ${VENV_DIR}."
        fi
    fi
    [[ -x "${VENV_DIR}/bin/python3" ]] && PYTHON_BIN="${VENV_DIR}/bin/python3"
fi

if [[ -z "${PYTHON_BIN}" ]]; then
    # No venv module, or creation failed. Fall back to the system interpreter --
    # but to its RESOLVED path, never the floating symlink, for the reason above.
    PYTHON_BIN="$("${BOOTSTRAP_PYTHON}" -c \
        'import os,sys; print(os.path.realpath(sys.executable))' 2>/dev/null \
        || echo "${BOOTSTRAP_PYTHON}")"
    warn "Running without a virtualenv; pinned to ${PYTHON_BIN}"
fi
info "Daemon interpreter: ${PYTHON_BIN}"

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
        reply="n"
        if [[ "${ASSUME_YES}" = "1" ]]; then
            reply="y"
        elif [[ ! -t 0 ]]; then
            # Piped, and not told to assume yes. Touch nothing; just instruct.
            warn "Non-interactive shell: skipping automatic merge."
            warn "Re-run with --yes to merge it, or add the snippet above by hand."
        else
            read -r -p "Merge this snippet into ${CLAUDE_SETTINGS} now with jq? [y/N] " reply
        fi
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

# --- 5b. Python dependencies -------------------------------------------------
# pyserial is required for a USB device; bleak is required for BLE, which the
# plist above enables because every documented way to connect a device needs it.
# BEST EFFORT, never fatal: a managed or externally-managed Python will refuse
# the install, and a daemon without these still runs -- it just reports which
# transport it cannot offer. Saying so here beats a working install that
# silently cannot do the thing its last line tells you to do.
# DRIVEN BY THE MANIFESTS, NOT BY A HAND-KEPT LIST. This used to install
# `pyserial bleak` literally, which drifted twice: bleak sat commented out in
# daemon/requirements.txt so the daemon's own recovery message left BLE dead,
# and pyte -- bin/requirements.txt, the PTY wrapper's only dependency -- was
# never installed at all, so the wrapper silently fell through to unwrapped
# Claude and the device stopped receiving session state.
#
# NOT --user. That targets a per-user site directory that the venv does not even
# consult, and on the system-python fallback it creates a THIRD place for the
# same package to hide (3.12 framework, 3.14 framework, user-site) -- which is
# exactly the ambiguity that made the last failure take so long to read.
info "Python dependencies (${REQ_FILES[*]##*/})..."
pip_log="$(mktemp)"
if "${PYTHON_BIN}" -m pip install --quiet --disable-pip-version-check \
        "${REQ_ARGS[@]}" >"${pip_log}" 2>&1; then
    ok "Installed $("${PYTHON_BIN}" -m pip list --format=freeze 2>/dev/null \
        | grep -icE '^(pyserial|bleak|pyte)=' || echo '?') runtime dependencies"
else
    # SHOW PIP'S REASON. The old branch sent stderr to /dev/null and then said
    # "could not install them automatically", which is the least useful half of
    # what it knew: an externally-managed environment, a missing compiler and a
    # network failure all produced that one sentence.
    err "Dependency install failed. pip said:"
    sed 's/^/      /' "${pip_log}" >&2
    warn "The daemon will still start, but will report the transports it cannot"
    warn "offer. To retry by hand:"
    warn "  ${PYTHON_BIN} -m pip install ${REQ_ARGS[*]}"
fi
rm -f "${pip_log}"

# --- 6. Put the command-line tools on PATH ----------------------------------
# WHY THIS STEP EXISTS. `claude` is normally an alias straight into bin/, so
# nothing in this repo ever needed to be on PATH -- and the day a tool started
# telling people to run `claude-mate-connect --pair`, that instruction failed
# with "command not found" for the person who had followed every other
# instruction correctly. Symlinks, not copies, so a `git pull` updates them.
TOOL_DIR=""
for cand in /usr/local/bin "${HOME}/.local/bin"; do
    if [ -d "$cand" ] && [ -w "$cand" ]; then TOOL_DIR="$cand"; break; fi
done
if [ -n "$TOOL_DIR" ]; then
    for tool in claude-mate claude-mate-connect claude-mate-switch; do
        if [ -x "${REPO_DIR}/bin/${tool}" ]; then
            ln -sf "${REPO_DIR}/bin/${tool}" "${TOOL_DIR}/${tool}"
            ok "Linked ${tool} -> ${TOOL_DIR}"
        fi
    done
    case ":${PATH}:" in
        *":${TOOL_DIR}:"*) ;;
        *) warn "${TOOL_DIR} is not on your PATH; add it to use these by name" ;;
    esac
else
    warn "No writable dir on PATH (/usr/local/bin, ~/.local/bin) -- run the"
    warn "tools by path: ${REPO_DIR}/bin/claude-mate-connect"
fi

echo
ok "Claude Mate installed."
info "Daemon logs:"
info "  out: ${LOG_DIR}/claude-mate.out.log"
info "  err: ${LOG_DIR}/claude-mate.err.log"
info "Check status:  launchctl list | grep ${PLIST_LABEL}"
info "Device not linked? claude-mate-connect  (or press \`d\` at the account picker)"
info "Uninstall:     ${SCRIPT_DIR}/uninstall.sh"
