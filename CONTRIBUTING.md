# Contributing to Claude Mate

Thanks for your interest in improving Claude Mate! This document covers how to
build the firmware, run the daemon, the coding conventions we follow, and how to
test your changes.

By participating you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

---

## Project layout

- `daemon/` — the Python daemon (`claude_mate_daemon.py`).
- `firmware/claude_mate/` — the Arduino Nano sketch.
- `hooks/claude-status.sh` — the Claude Code hook, installed to
  `~/.claude/hooks/`.
- `docs/` — install, wiring, protocol, and testing guides.

---

## Building the firmware

The firmware targets an **Arduino Nano (ATmega328P)** with an SSD1306 128×64
I2C OLED. You can build it with either the Arduino IDE or `arduino-cli`.

### Arduino IDE

1. Open `firmware/claude_mate/claude_mate.ino`.
2. Install the OLED driver library (e.g. Adafruit SSD1306 + Adafruit GFX) via
   the Library Manager.
3. Select **Tools → Board → Arduino Nano** and the correct processor
   (old/new bootloader as appropriate for your clone).
4. Select the serial port and click **Upload**.

### arduino-cli

```sh
arduino-cli compile --fqbn arduino:avr:nano firmware/claude_mate
arduino-cli upload  --fqbn arduino:avr:nano -p /dev/cu.usbserial-XXXX firmware/claude_mate
```

The optional buzzer is gated behind `#define ENABLE_BUZZER` — leave it commented
out if you did not wire a buzzer.

See [docs/WIRING.md](docs/WIRING.md) for the full pinout.

---

## Running the daemon

The daemon requires **Python 3.9+** and the single third-party dependency
**pyserial**.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install pyserial

# Run against real hardware (autodetects the serial port):
python3 daemon/claude_mate_daemon.py

# Run with fake sessions, no hardware and no Claude needed:
python3 daemon/claude_mate_daemon.py --mock
```

Configuration is via environment variables (all optional):

| Variable           | Default                  |
|--------------------|--------------------------|
| `CLAUDE_MATE_PORT` | autodetect               |
| `CLAUDE_MATE_SOCK` | `/tmp/claude-mate.sock`  |
| `CLAUDE_MATE_BAUD` | `115200`                 |

For end-to-end setup including the hooks, follow
[docs/INSTALL.md](docs/INSTALL.md).

---

## Coding conventions

### Python (daemon)

- **Python 3.9+ only.** Do not use syntax newer than 3.9.
- **The only allowed third-party dependency is `pyserial`.** Everything else
  must come from the standard library.
- Keep it clean, commented, and runnable with
  `python3 daemon/claude_mate_daemon.py`.
- Use **threads + locks** for the serial reader, socket server, and carousel.
  No busy-wait spinning.
- Keep the serial port open continuously; auto-detect and auto-reconnect; never
  crash on a missing port.
- The `--mock` flag must keep working — it is how reviewers demo the device.

### Arduino (firmware)

- Be **RAM-frugal**: prefer fixed `char` buffers, wrap string literals in the
  `F()` macro, and avoid heavy `String` churn inside `loop()`.
- Tolerate partial/garbled serial lines, cap the input buffer length, and ignore
  malformed lines.
- Debounce buttons (~200 ms).
- Emit `H` on boot so the daemon can resend full state after the USB reset.

### Bash (hooks & scripts)

- Start scripts with a proper shebang.
- Use `set -euo pipefail` **except in the hook**: the hook must **never** fail or
  block a Claude turn. It fires-and-forgets to the socket with a short timeout,
  swallows all errors, and **always exits 0**.

### General

- Follow [`.editorconfig`](.editorconfig): LF line endings, trailing newline,
  2-space indent for JSON/Markdown/`.ino`, 4-space indent for Python.
- Keep commits focused; describe the *why* in the message.

---

## Testing — the test ladder

Validate changes from the bottom up, the same ladder used in
[docs/TESTING.md](docs/TESTING.md):

1. **Daemon in `--mock` mode** — exercises the display/light/carousel logic with
   fake sessions and no hardware or Claude.
2. **Serial loopback / protocol** — verify the `|`-delimited lines the daemon
   emits and the `H` / `B|<n>` lines it consumes (see
   [docs/PROTOCOL.md](docs/PROTOCOL.md)).
3. **Firmware on the bench** — flash the Nano, confirm the OLED, the three LEDs,
   and both buttons respond.
4. **End-to-end** — run the daemon against real hardware, install the hooks, and
   drive real Claude Code sessions through `working → waiting → error → done`.

Please describe which rungs you tested in your pull request.

---

## Submitting changes

1. Fork and create a feature branch.
2. Make your change and run the relevant rungs of the test ladder.
3. Open a pull request describing what changed, why, and how you tested it.

We appreciate every contribution — thank you!
