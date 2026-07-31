# Contributing to Claude Mate

Thanks for your interest in improving Claude Mate! This document covers how to
build the firmware, run the daemon, the coding conventions we follow, and how to
test your changes.

By participating you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

> **License of contributions.** Claude Mate is licensed **CC BY-NC 4.0**
> (Creative Commons Attribution-NonCommercial — see [LICENSE](LICENSE)).
> Contributions are welcome for **personal, non-commercial** use.
> **Commercial use of the project is not permitted.**
>
> **Contributors must also agree to the [CLA](CLA.md)** — one sentence in your
> first pull request covers everything you contribute afterwards. You keep the
> copyright in your own work; the agreement grants the project a licence broad
> enough that it can be relicensed later.
>
> That last part is the reason it exists. Saying only "contributions are offered
> under the same licence" leaves every contributor owning their patch under
> CC BY-NC, which means the project could **never** change licence or be
> published commercially without tracking down every person who ever landed a
> line — including the ones who have changed address or moved on. It is a
> one-way door, and it closes silently on the first merged pull request.

---

## Project layout

- `daemon/` — the Python daemon (`claude_mate_daemon.py`).
- `firmware/claude_mate/` — the Arduino Nano sketch (iteration 1).
- `firmware/claude_mate_s3/` — the ESP32-S3 Wi-Fi sketch (iteration 2).
- `firmware/flash_s3.sh` — build + flash for the S3; `arduino-cli upload`
  cannot flash that board. See [`firmware/README.md`](firmware/README.md).
- `hooks/claude-status.sh` — the Claude Code hook, installed to
  `~/.claude/hooks/`.
- `docs/` — install, wiring, protocol, and testing guides.

Both devices speak the **same protocol** and may be connected simultaneously, so
a change to the daemon or the wrapper has to keep working for the Nano. Anything
the Nano cannot do (colour, the terminal mirror) belongs behind a verb it simply
never sends.

---

## Building the firmware

The firmware targets an **Arduino Nano (ATmega328P)** with an SSD1306 0.91"
128×32 I2C OLED. You can build it with either the Arduino IDE or `arduino-cli`.

### Arduino IDE

1. Open `firmware/claude_mate/claude_mate.ino`.
2. Install the **Adafruit GFX** library via the Library Manager — it is the
   only external dependency (the SSD1306 is driven by the bundled
   `softssd1306.h`, a software-I2C `Adafruit_GFX` subclass).
3. Select **Tools → Board → Arduino Nano** and the correct processor
   (old/new bootloader as appropriate for your clone).
4. Select the serial port and click **Upload**.

### arduino-cli

```sh
arduino-cli compile --fqbn arduino:avr:nano firmware/claude_mate
arduino-cli upload  --fqbn arduino:avr:nano -p /dev/cu.usbserial-XXXX firmware/claude_mate
```

The only firmware library dependency is **Adafruit GFX**; the SSD1306 itself is
driven by the bundled `softssd1306.h` (a software-I2C `Adafruit_GFX` subclass —
the Nano's hardware-I2C SCL pin was damaged, so SCL is bit-banged on A3; see the
header comment). There is no stepper and no vibration motor: the sole alert
output is the **indication LED on D8**, driven with plain `digitalWrite` timing.

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
- Use **threads + locks** for the serial reader, the socket server, and the
  render/refresh loop. No busy-wait spinning.
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

1. **Daemon in `--mock` mode** — exercises the triage-queue, screen-rendering,
   and LED-policy logic with fake sessions and no hardware or Claude.
2. **Serial loopback / protocol** — verify the `|`-delimited lines the daemon
   emits (`F|…`, `V|…`, `M|…`, `P`) and the `H` / `B|P` / `B|N` / `B|G` /
   `B|K` / `B|M` lines it consumes (see
   [docs/PROTOCOL.md](docs/PROTOCOL.md)).
3. **Firmware on the bench** — flash the device, confirm the display renders the
   frame the daemon sends, the indication LED plays the alert patterns
   (START / INPUT / DONE / ERROR), and every button responds: **PREV / GO /
   NEXT** on the Nano, plus **MIRROR** on the ESP32-S3.
4. **End-to-end** — run the daemon against real hardware, install the hooks, and
   drive real Claude Code sessions through `working → waiting → error → done`.

`tools/test_e2e.py` drives rungs 1–2 over a **serial** PTY. The ESP32-S3's
**TCP** transport is not covered by it, so changes to anything the wireless path
touches want a check against real hardware — or a throwaway client that performs
the nonce/HMAC handshake and speaks the protocol directly. A bug that only
showed up over TCP is exactly how the terminal mirror shipped broken once.

Please describe which rungs you tested in your pull request.

---

## Submitting changes

1. Fork and create a feature branch.
2. Make your change and run the relevant rungs of the test ladder.
3. Open a pull request describing what changed, why, and how you tested it.

We appreciate every contribution — thank you!
