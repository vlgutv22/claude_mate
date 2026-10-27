#!/usr/bin/env bash
#
# Remove a pkg-installed Claude Mate. Run as the logged-in user (it will use
# sudo for the system payload). Leaves ~/.claude/settings.json untouched but
# tells you which hooks block to delete.
#
set -euo pipefail

SUPPORT="/Library/Application Support/ClaudeMate"
UID_NUM="$(id -u)"
PLIST="${HOME}/Library/LaunchAgents/com.claudemate.daemon.plist"

echo "==> Unloading LaunchAgent"
launchctl bootout "gui/${UID_NUM}" "${PLIST}" 2>/dev/null || launchctl unload "${PLIST}" 2>/dev/null || true
rm -f "${PLIST}"

echo "==> Removing hook"
rm -f "${HOME}/.claude/hooks/claude-status.sh"

echo "==> Removing system payload (needs sudo)"
sudo rm -rf "${SUPPORT}"

echo "==> Forgetting pkg receipt"
sudo pkgutil --forget com.claudemate.pkg 2>/dev/null || true

cat <<EOF

Done. One manual step left:
  Open ~/.claude/settings.json and delete the Claude Mate entries under "hooks"
  (the keys starting with "claudeMate"). Your other settings are untouched.
EOF

# The chat history is the user's, not the package's, so it stays -- and is
# said to stay, the way the hooks block is. A .pkg install on its own never
# writes one (it ships no wrapper, and only a wrapped session names its
# conversation), so this speaks up only when a wrapper from a checkout fed the
# daemon. Its .jsonl files may be the last copy of a conversation Claude Code
# has since cleaned up, which is why removing them is left to a person.
HISTORY_DIR="${HOME}/.config/claude-mate/history"
if [ -d "${HISTORY_DIR}" ]; then
    cat <<EOF

Your chat history was left in place:
  ${HISTORY_DIR}
  It may hold the only copy of a conversation. To delete it anyway:
  rm -r "${HISTORY_DIR}"
EOF
fi
