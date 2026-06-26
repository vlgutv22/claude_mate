# Claude Mate — macOS installer (.pkg)

A native, double-click `.pkg` that installs the **Mac side** of Claude Mate
(daemon + hook + LaunchAgent). Firmware flashing stays separate
(see [../docs/INSTALL.md](../docs/INSTALL.md)).

## What the package does

| Step | Action | Where |
|------|--------|-------|
| payload | daemon + vendored `pyserial` + hook + firmware copy | `/Library/Application Support/ClaudeMate/` |
| postinstall | install hook | `~/.claude/hooks/claude-status.sh` |
| postinstall | merge hooks block (idempotent, preserves your settings) | `~/.claude/settings.json` |
| postinstall | instantiate + load the agent | `~/Library/LaunchAgents/com.claudemate.daemon.plist` |

`pyserial` is **vendored** into the payload (it's pure Python), exposed to the
daemon via `PYTHONPATH` in the LaunchAgent. So there is **no pip, no network,
and no compiled binary** — which keeps notarization to just the installer itself.

The daemon runs under `/usr/bin/python3` (provided by the Xcode Command Line
Tools, present on virtually all dev Macs; a fresh machine may prompt to install
them on first run).

## Build

```sh
./packaging/build-pkg.sh
```

Produces `packaging/dist/ClaudeMate-<ver>-unsigned.pkg`. For local testing,
**right-click the pkg in Finder → Open** to bypass Gatekeeper once.

## Sign + notarize (for distribution)

You need an Apple Developer account ($99/yr) with a **Developer ID Installer**
certificate in your keychain.

1. Create a notarytool keychain profile once:

   ```sh
   xcrun notarytool store-credentials "claude-mate-notary" \
       --apple-id "you@example.com" --team-id "TEAMID" \
       --password "<app-specific-password>"
   ```

2. Build, sign, notarize and staple in one go:

   ```sh
   export DEVELOPER_ID_INSTALLER="Developer ID Installer: Your Name (TEAMID)"
   export NOTARY_PROFILE="claude-mate-notary"
   ./packaging/build-pkg.sh
   ```

   Output: `packaging/dist/ClaudeMate-<ver>.pkg`, signed + notarized + stapled —
   double-clicks cleanly with no Gatekeeper or quarantine prompt.

Verify a finished installer:

```sh
pkgutil --check-signature packaging/dist/ClaudeMate-<ver>.pkg
spctl --assess --type install -vv packaging/dist/ClaudeMate-<ver>.pkg
xcrun stapler validate packaging/dist/ClaudeMate-<ver>.pkg
```

## Inspect without installing

```sh
pkgutil --expand packaging/dist/ClaudeMate-<ver>-unsigned.pkg /tmp/cm-expand
```

## Uninstall

```sh
./packaging/uninstall-pkg.sh
```

## Files

| File | Purpose |
|------|---------|
| `build-pkg.sh` | stage payload, vendor pyserial, `pkgbuild` + `productbuild`, optional sign/notarize |
| `scripts/preinstall` | stop a previously installed daemon (upgrade path) |
| `scripts/postinstall` | wire up the console user (hook, settings merge, agent load) |
| `distribution.xml.in` | productbuild distribution (title, license, welcome/conclusion, min OS 11) |
| `com.claudemate.daemon.plist.in` | LaunchAgent template with vendored `PYTHONPATH` |
| `resources/*.html` | installer welcome / conclusion screens |
| `uninstall-pkg.sh` | remove everything the pkg installed |

## Notes & limitations

- The postinstall targets the **GUI console user**, resolved via
  `stat -f%Su /dev/console` (with a `scutil` fallback) — not root. If you install
  while no one is logged into the GUI, it installs the files but skips the
  per-user wiring and logs that to `/tmp/claude-mate-install.log`.
- Installs for the **current** console user only (it's a personal device). For a
  truly multi-user system agent you'd ship the plist in `/Library/LaunchAgents/`
  with absolute log paths instead.
- Firmware is **not** flashed by the pkg — it's copied to the payload for
  reference only.
- **AppleDouble (`._*`) in the payload:** macOS stamps a SIP-protected
  `com.apple.provenance` xattr that `pkgbuild` encodes as `._*` files. A normal
  Terminal build is clean; some managed/sandboxed environments stamp it on every
  written file. It's harmless either way — `Installer.app` re-absorbs AppleDouble
  into xattrs on the target (no `._*` files end up installed) and notarization
  accepts them.
