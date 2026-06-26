# Claude Mate — Install

End-to-end setup, in order:

1. [Flash the firmware](#1-flash-the-firmware-arduino-ide)
2. [Run the daemon](#2-run-the-daemon)
3. [Merge the hooks snippet](#3-merge-the-hooks-snippet)
4. [Install the LaunchAgent](#4-install-the-launchagent-auto-start)

Prerequisites: a Mac, **Python 3.9+**, the **Arduino IDE**, and your assembled
hardware (see [WIRING.md](WIRING.md)). The only third-party Python dependency is
**pyserial**.

The project lives at `/Users/vlgutv/Projects/claude_mate` (referred to below as
`$REPO`).

---

## 1. Flash the firmware (Arduino IDE)

1. Open the firmware sketch in the **Arduino IDE** (under `$REPO/firmware/`).
2. **Tools → Board →** Arduino Nano. **Tools → Processor →** ATmega328P (try
   "ATmega328P (Old Bootloader)" if uploads fail).
3. **Tools → Port →** select the Nano's port (a `/dev/cu.usbserial*` or
   `/dev/cu.usbmodem*` device).
4. Install the **required libraries** via **Sketch → Include Library → Manage
   Libraries…**:
   - **Adafruit SSD1306** (the OLED driver) — or **Adafruit SH1106 / U8g2** if
     you have a **1.3"** SH1106 panel (see the SSD1306-vs-SH1106 note in
     [WIRING.md](WIRING.md)).
   - **Adafruit GFX Library** (graphics primitives Adafruit SSD1306 depends on).
   - **Adafruit BusIO** (I2C/SPI helper pulled in by the above).
5. Confirm the I2C address in the sketch is **0x3C** (change to **0x3D** if your
   panel uses the alternate address).
6. **Upload**. After upload, the board resets and emits `H` on boot.

> If you installed the **10 µF RESET→GND cap** from the WIRING caveat, **remove
> it before uploading** — it blocks the bootloader — then re-install it.

**Smoke test:** open the IDE **Serial Monitor** at **115200 baud**. You should
see `H` shortly after a reset. (See [TESTING.md](TESTING.md) for the full
ladder.) Then **close the Serial Monitor** — it holds the port and the daemon
cannot open it at the same time.

---

## 2. Run the daemon

The daemon needs Python 3.9+ and pyserial:

```sh
python3 -m pip install --user pyserial
```

Run it directly:

```sh
python3 $REPO/daemon/claude_mate_daemon.py
```

It auto-detects the serial port (`/dev/cu.usbserial*` then `/dev/cu.usbmodem*`),
opens it once, and keeps it open. Leave it running.

**Optional environment variables** (sane defaults shown):

| Variable           | Default      | Meaning |
|--------------------|--------------|---------|
| `CLAUDE_MATE_PORT` | autodetect   | Serial device path (skip autodetect). |
| `CLAUDE_MATE_SOCK` | `/tmp/claude-mate.sock` | Unix socket the hook writes to. |
| `CLAUDE_MATE_BAUD` | `115200`     | Serial baud rate. |

**Mock mode (no hardware needed for the display logic, no Claude needed):**

```sh
python3 $REPO/daemon/claude_mate_daemon.py --mock
```

`--mock` injects a few fake sessions that cycle through states so you can demo
the OLED card, the traffic light, and the carousel without Claude or hooks.

---

## 3. Merge the hooks snippet

The hook script is **fire-and-forget** and always exits 0, so it can never block
or break a Claude turn.

1. Make sure the hook script is installed and executable at the path Claude Code
   expects:

   ```sh
   mkdir -p ~/.claude/hooks
   cp $REPO/hooks/claude-status.sh ~/.claude/hooks/claude-status.sh
   chmod +x ~/.claude/hooks/claude-status.sh
   ```

2. Merge the hooks block into your Claude Code settings. Settings live at
   (VERIFIED FACTS):
   - **User:** `~/.claude/settings.json`
   - **Project:** `.claude/settings.json`
   - **Local:** `.claude/settings.local.json`

   Add (merge into any existing `"hooks"` object) the events Claude Mate cares
   about — `UserPromptSubmit`, `Notification`, `Stop`, and `StopFailure` — each
   pointing at the script with the matching state as its argument. The canonical
   snippet ships at `$REPO/hooks/settings.snippet.json` and is exactly what
   `install/install.sh` merges; merge its `hooks` object, which looks like:

   ```json
   {
     "hooks": {
       "claudeMateWorking": {
         "type": "shell",
         "events": ["UserPromptSubmit"],
         "command": "~/.claude/hooks/claude-status.sh working"
       },
       "claudeMateWaiting": {
         "type": "shell",
         "events": ["Notification"],
         "command": "~/.claude/hooks/claude-status.sh waiting"
       },
       "claudeMateDone": {
         "type": "shell",
         "events": ["Stop"],
         "command": "~/.claude/hooks/claude-status.sh done"
       },
       "claudeMateError": {
         "type": "shell",
         "events": ["StopFailure"],
         "command": "~/.claude/hooks/claude-status.sh error"
       }
     }
   }
   ```

   (Hooks-block shape per VERIFIED FACTS: each hook has `type`, `events`,
   `command`. `StopFailure` is a distinct event that fires instead of `Stop` on
   API errors; its `claudeMateError` entry is optional and can be omitted on
   builds without `StopFailure`.) There is **no `SessionEnd` hook**: the daemon's
   `idle` state is derived purely from inactivity (TTL pruning), not from a hook
   event — see [PROTOCOL.md](PROTOCOL.md).

3. Restart / reload Claude Code (or start a new session) so it picks up the
   hooks. With the daemon running, submitting a prompt should turn the light
   **YELLOW** (working) and show a card; a `Notification` turns it **RED**, etc.

---

## 4. Install the LaunchAgent (auto-start)

To keep the daemon running and restart it on login/crash, install the macOS
**LaunchAgent**.

The simplest path is to run the installer, which templates the plist (filling in
your repo path, `python3`, and `$HOME`) and loads it for you:

```sh
$REPO/install/install.sh
```

To do it by hand instead, copy and load the plist (note the label has **no
hyphen** between "claude" and "mate"):

```sh
cp $REPO/install/com.claudemate.daemon.plist \
   ~/Library/LaunchAgents/com.claudemate.daemon.plist

launchctl load ~/Library/LaunchAgents/com.claudemate.daemon.plist
```

To stop / unload it:

```sh
launchctl unload ~/Library/LaunchAgents/com.claudemate.daemon.plist
```

The LaunchAgent runs the same `daemon/claude_mate_daemon.py`. The daemon tolerates
the device being absent at launch and auto-reconnects when you plug the Nano in,
so it is safe to keep loaded all the time.

> **Port conflict reminder:** only one program can hold the serial port. If the
> Arduino IDE **Serial Monitor** is open, the daemon (or LaunchAgent) cannot open
> the port — and vice versa. Close one before starting the other.

---

## Verifying the install

Follow [TESTING.md](TESTING.md) from the bottom up: confirm the port appears in
`ls /dev/cu.*`, run the selftest, poke the protocol by hand, run the daemon in
`--mock`, feed a fake hook line, and finally exercise real Claude hooks.
