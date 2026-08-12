# Claude Mate — Install

## The short version

```sh
git clone https://github.com/vlgutv22/claude_mate.git && cd claude_mate
./install/install.sh --yes
```

Steps 2, 3 and 4 below are what that does: it installs the status hook, merges
the hooks block into `~/.claude/settings.json` (backing it up first), installs
and starts the **LaunchAgent** so the daemon runs at login, and puts
`claude-mate`, `claude-mate-connect` and `claude-mate-switch` on your `PATH`.
Drop `--yes` and it asks before editing `settings.json`, which is the only
question it has. It is idempotent — re-run it after a `git pull`.

Then alias `claude` to the PTY wrapper (this is what gets model, effort, account
and remaining limit onto the screen):

```sh
echo 'alias claude="'"$PWD"'/bin/claude-mate-wrap"' >> ~/.zshrc && exec zsh
```

**You do not need hardware.** `claude-mate` in a terminal is the same interface
as the device — see [USING.md](USING.md), which is the manual for both.

Undo it all with `./install/uninstall.sh`.

The rest of this page is the **manual** route, and the firmware. Read it if you
are flashing a board, or if you want to know exactly what the installer touched.

---

## Prerequisites

A Mac, **Python 3.9+**, and — only if you are building hardware — `arduino-cli`
or the Arduino IDE plus your assembled device ([WIRING.md](WIRING.md)).

Python dependencies, all optional in the sense that the daemon degrades rather
than fails without them: **pyserial** (a USB device), **pyte** (the terminal
mirror and the PTY wrapper's state scraping), **bleak** (the BLE transport).

Put the project wherever you like — this guide refers to its root as `$REPO`
(e.g. `~/src/claude_mate`).

> **Two devices, and this page's step 1 is the Nano.** For the **ESP32-S3** —
> the cordless colour build — flashing is `./firmware/flash_s3.sh` and is
> documented in [`firmware/README.md`](../firmware/README.md), because
> `arduino-cli upload` cannot flash that board and the reasons are specific.
> Provisioning it needs nothing typed: plug it in and the daemon hands it a
> token, or run `claude-mate-connect --pair` and press GO.

---

## 1. Flash the firmware (Arduino IDE)

1. Open the firmware sketch in the **Arduino IDE** (under `$REPO/firmware/`).
2. **Tools → Board →** Arduino Nano. **Tools → Processor →** ATmega328P (try
   "ATmega328P (Old Bootloader)" if uploads fail).
3. **Tools → Port →** select the Nano's port (a `/dev/cu.usbserial*` or
   `/dev/cu.usbmodem*` device).
4. Install the **one required library** via **Sketch → Include Library → Manage
   Libraries…**:
   - **Adafruit GFX Library** — graphics primitives, and the only external
     dependency. The panel itself is driven by the bundled
     [`softssd1306.h`](../firmware/claude_mate/softssd1306.h), a software-I2C
     `Adafruit_GFX` subclass that puts SCL on **A3** (hardware SCL A5 was
     damaged) and leaves SDA on A4. **Adafruit SSD1306 and Adafruit BusIO are
     not needed** — CI compiles this sketch with Adafruit GFX alone.
   - If you have a **1.3" SH1106** panel instead, you will need its own driver
     (**Adafruit SH1106 / U8g2**) and a matching edit to the sketch — see the
     SSD1306-vs-SH1106 note in [WIRING.md](WIRING.md).
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
the OLED card, the status word + vibration haptic, and the carousel without
Claude or hooks.

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
   hooks. With the daemon running, submitting a prompt should show the word
   **WIP** (working) and a card; a `Notification` switches it to **BLOCKED** (and
   the motor gives a gentle per-session buzz as sessions start/wait/finish/error), etc.

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
