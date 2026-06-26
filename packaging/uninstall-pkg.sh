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
