#!/usr/bin/env bash
#
# Build (and optionally sign + notarize) the Claude Mate macOS installer .pkg.
#
#   ./packaging/build-pkg.sh
#
# Produces an UNSIGNED installer at packaging/dist/ClaudeMate-<ver>-unsigned.pkg
# that already works for local testing (right-click > Open to bypass Gatekeeper).
#
# To ship a notarized, double-click-clean installer, set these and re-run:
#
#   export DEVELOPER_ID_INSTALLER="Developer ID Installer: Your Name (TEAMID)"
#   export NOTARY_PROFILE="claude-mate-notary"   # a notarytool keychain profile
#   ./packaging/build-pkg.sh
#
# Create the notary profile once with:
#   xcrun notarytool store-credentials "claude-mate-notary" \
#       --apple-id "you@example.com" --team-id "TEAMID" --password "<app-specific-pw>"
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/.." && pwd)"
VER="$(tr -d '[:space:]' < "${HERE}/VERSION")"
DIST="${HERE}/dist"
IDENT="com.claudemate.pkg"

COMPONENT="${DIST}/ClaudeMate-component.pkg"
UNSIGNED="${DIST}/ClaudeMate-${VER}-unsigned.pkg"
SIGNED="${DIST}/ClaudeMate-${VER}.pkg"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

rm -rf "${DIST}"
mkdir -p "${DIST}"

# ---------------------------------------------------------------------------
# 1. Stage the payload root (everything installs under /Library).
# ---------------------------------------------------------------------------
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT
SUPPORT="${STAGE}/Library/Application Support/ClaudeMate"
mkdir -p "${SUPPORT}/daemon" "${SUPPORT}/hooks" "${SUPPORT}/firmware" "${SUPPORT}/vendor"

say "Staging payload"
cp "${REPO}/daemon/claude_mate_daemon.py" "${SUPPORT}/daemon/"
cp "${REPO}/daemon/requirements.txt"      "${SUPPORT}/daemon/"
install -m 0755 "${REPO}/hooks/claude-status.sh" "${SUPPORT}/hooks/claude-status.sh"
cp "${REPO}/hooks/settings.snippet.json"  "${SUPPORT}/hooks/"
cp "${HERE}/com.claudemate.daemon.plist.in" "${SUPPORT}/"
cp "${REPO}/LICENSE" "${SUPPORT}/"
cp -R "${REPO}/firmware/." "${SUPPORT}/firmware/"
printf '%s\n' "${VER}" > "${SUPPORT}/VERSION"

# ---------------------------------------------------------------------------
# 2. Vendor pyserial (pure Python -> portable, no pip at install time).
# ---------------------------------------------------------------------------
say "Vendoring pyserial"
python3 -m pip install --quiet --upgrade --target "${SUPPORT}/vendor" pyserial
# Drop pip metadata noise that we do not need to ship.
rm -rf "${SUPPORT}/vendor"/*.dist-info/RECORD 2>/dev/null || true

# Re-stage through ditto WITHOUT extended attributes / resource forks so stray
# xattrs (quarantine, etc.) do not become AppleDouble (._*) files in the payload.
# NOTE: the SIP-protected com.apple.provenance xattr cannot be stripped and some
# managed/sandboxed build environments stamp it on every written file; pkgbuild
# then encodes it as ._* entries. Those are harmless: macOS Installer re-absorbs
# AppleDouble back into xattrs on the target (no ._* files on the installed
# system) and notarization accepts them. A normal Terminal build is clean.
CLEAN="$(mktemp -d)"
ditto --noextattr --norsrc "${STAGE}" "${CLEAN}"
find "${CLEAN}" -name '._*' -delete 2>/dev/null || true
STAGE="${CLEAN}"

# ---------------------------------------------------------------------------
# 3. Build the component package (carries preinstall/postinstall scripts).
# ---------------------------------------------------------------------------
say "pkgbuild component"
chmod 0755 "${HERE}/scripts/preinstall" "${HERE}/scripts/postinstall"
pkgbuild \
    --root "${STAGE}" \
    --identifier "${IDENT}" \
    --version "${VER}" \
    --scripts "${HERE}/scripts" \
    --install-location "/" \
    "${COMPONENT}"

# ---------------------------------------------------------------------------
# 4. Build the product (distribution) package with the install UI.
# ---------------------------------------------------------------------------
say "productbuild distribution"
RES="$(mktemp -d)"
cp "${HERE}/resources/welcome.html" "${HERE}/resources/conclusion.html" "${RES}/"
cp "${REPO}/LICENSE" "${RES}/LICENSE.txt"
sed "s/__VERSION__/${VER}/g" "${HERE}/distribution.xml" > "${DIST}/distribution.xml" 2>/dev/null \
    || sed "s/__VERSION__/${VER}/g" "${HERE}/distribution.xml.in" > "${DIST}/distribution.xml"

productbuild \
    --distribution "${DIST}/distribution.xml" \
    --resources "${RES}" \
    --package-path "${DIST}" \
    "${UNSIGNED}"
rm -rf "${RES}"

say "Unsigned installer: ${UNSIGNED}"

# ---------------------------------------------------------------------------
# 5. Optional: sign + notarize + staple.
# ---------------------------------------------------------------------------
if [ -n "${DEVELOPER_ID_INSTALLER:-}" ]; then
    say "productsign (Developer ID Installer)"
    productsign --sign "${DEVELOPER_ID_INSTALLER}" "${UNSIGNED}" "${SIGNED}"
    pkgutil --check-signature "${SIGNED}" || true

    if [ -n "${NOTARY_PROFILE:-}" ]; then
        say "notarytool submit (this can take a few minutes)"
        xcrun notarytool submit "${SIGNED}" --keychain-profile "${NOTARY_PROFILE}" --wait
        say "stapler staple"
        xcrun stapler staple "${SIGNED}"
        xcrun stapler validate "${SIGNED}" || true
        say "Signed + notarized installer: ${SIGNED}"
    else
        say "Signed (not notarized; set NOTARY_PROFILE to notarize): ${SIGNED}"
    fi
else
    cat <<EOF

  Built UNSIGNED. To test locally:
      Right-click ${UNSIGNED##*/} in Finder > Open  (bypasses Gatekeeper once)

  To ship a notarized installer, set DEVELOPER_ID_INSTALLER (and NOTARY_PROFILE)
  and re-run. See the header of this script.

EOF
fi
