# Claude Mate

A tiny USB hardware companion that shows the **live status of your Claude Code
sessions** on a small OLED, turns a **stepper-driven status wheel** to one of
four words, and lets you **jump straight to the session that needs you** with a
single button press.

Plug it into your Mac, run the daemon, drop in the Claude Code hooks, and the
device becomes an ambient, always-on status pane for every Claude Code session
you have open in VS Code (and/or the terminal CLI).

```
            ┌──────────────────────────────┐
            │  ▓▓ api-server   working ▓▓   │   ← live status card
            │     02:14            71%      │
            │  [1/3]            WIP         │   ← current wheel word (text echo)
            └──────────────────────────────┘
                     ⟳  ( WIP )                 ← stepper status wheel
              [ FOCUS ]   [ NEXT ]            ← two buttons

      wheel words:  FREE → WIP → BLOCKED → WTF   (escalation order)
```

---

## What it is

**Claude Mate** is an Arduino Nano + small I2C OLED + 2 buttons + a
**stepper-driven status wheel**, paired with a lightweight Python daemon on your
Mac and a set of Claude Code hooks. The hooks report session state changes; the
daemon keeps the model of all your sessions, drives the display, and acts on
button presses.

The single must-have action is **FOCUS**: press a button and the VS Code window
for the currently displayed session is raised so you can deal with it. Retrying
or resubmitting a turn is intentionally **out of scope** (see
[Limitations](#limitations)).

---

## Features

- **Live status carousel** — the daemon auto-rotates through all your active
  sessions (~every 3 s), showing one card at a time: project name, state,
  runtime, and a best-effort usage limit. Most urgent sessions are shown first.
- **Stepper status wheel** — a dial that physically rotates to one of four words,
  an at-a-glance system health signal (priority: **WTF > BLOCKED > WIP > FREE**):
  - **WTF** — at least one session is in error (StopFailure / API 5xx /
    overloaded / timeout).
  - **BLOCKED** — at least one session is waiting on your input (permission /
    question) and none errored.
  - **WIP** — something is working; nothing needs you right now.
  - **FREE** — all clear (idle/done sessions, or no sessions). This is the home
    position, set by a tab-on-wheel endstop and re-homed at boot.

  The words are 90° apart in escalation order so rotation = the situation
  escalating. Motion is non-blocking (AccelStepper). Two driver options sit
  behind a `#define`: **ULN2003 + 28BYJ-48** (default) or **A4988 + NEMA17**.
- **One-button FOCUS** — press FOCUS to raise the VS Code window of the session
  currently on screen, via a VS Code deep link with a window-raise fallback.
- **NEXT button** — advance the carousel immediately; auto-rotation pauses for
  ~10 s so you can read.
- **Robust by design** — keeps the serial port open continuously, auto-detects
  and auto-reconnects to the device, never crashes on a missing port, and the
  hooks never block or fail a Claude turn.
- **`--mock` demo mode** — run the whole display/wheel/carousel with fake
  sessions (cycling through all four words), no Claude and no hooks required.

---

## Architecture

```
   Claude Code (VS Code ext / CLI)
            │  hook events
            ▼
   ~/.claude/hooks/claude-status.sh
            │  "<state>|<session_id>|<name>\n"
            ▼
   Unix domain socket  /tmp/claude-mate.sock
            │
            ▼
   ┌───────────────────────────────────┐
   │   Python daemon (Mac)             │
   │   • session state model           │
   │   • status-word logic (D|<word>)  │
   │   • carousel rotation             │
   │   • FOCUS (deep link + fallback)  │
   └───────────────────────────────────┘
            │  USB serial 115200 8N1, "|"-delimited ASCII lines
            ▼
   ┌───────────────────────────────────┐
   │   Arduino Nano (ATmega328P)       │
   │   • SSD1306 128x32 OLED (I2C)     │
   │   • stepper status wheel          │
   │     (ULN2003/28BYJ-48 or A4988)   │
   │     homed to the D4 endstop       │
   │   • FOCUS / NEXT buttons          │
   └───────────────────────────────────┘
            │  buttons  "B|<n>\n"
            └──────────────► back to the daemon
```

Opening the USB serial port resets the Nano (~1.5 s). On boot the Arduino
re-homes the wheel and emits `H` (hello); the daemon responds by resending the
full current state (dial word + current card), so the display recovers cleanly
after any reconnect.

---

## Bill of Materials

| Qty | Part                                    | Notes                                  |
|-----|-----------------------------------------|----------------------------------------|
| 1   | Arduino Nano (ATmega328P)               | Any USB-serial Nano clone works        |
| 1   | SSD1306 0.91" 128×32 OLED, I2C           | Address `0x3C` (some boards `0x3D`); 0.96" 128×64 also works |
| 2   | Momentary push buttons                  | FOCUS and NEXT                         |
| 1   | Endstop microswitch                     | Home switch at `FREE` (D4, LOW=pressed)|
| 1   | Stepper motor                           | **28BYJ-48** (default) or **NEMA17**   |
| 1   | Stepper driver board                    | **ULN2003** (28BYJ-48) or **A4988** (NEMA17) |
| 1   | Wheel / dial face                       | FREE/WIP/BLOCKED/WTF + a home tab      |
| 1   | Piezo buzzer *(optional)*               | Chirps on transition into WTF          |
| —   | Jumper wires, breadboard / perfboard    |                                        |
| 1   | USB cable (to the Mac)                   | Data-capable, not charge-only          |

> Power the motor from the **USB 5 V rail** (28BYJ-48 ~240 mA) or an **external
> 12 V supply** (NEMA17/A4988) — **not** through the Nano's onboard regulator, to
> avoid brown-out resets. See [docs/WIRING.md](docs/WIRING.md).

Pinout summary (full details in [docs/WIRING.md](docs/WIRING.md)):

| Signal               | Pin       |
|----------------------|-----------|
| OLED SDA             | A4        |
| OLED SCL             | A5        |
| FOCUS button         | D2        |
| NEXT button          | D3        |
| ENDSTOP (home)       | D4        |
| Stepper IN1 / STEP   | D5        |
| Stepper IN2 / DIR    | D6        |
| Stepper IN3 / EN     | D7        |
| Stepper IN4          | D8 *(ULN2003 only)* |
| Buzzer (optional)    | D9        |

Buttons and the endstop use `INPUT_PULLUP` (other leg to GND; pressed = LOW). The
stepper sits on D5–D8 for ULN2003/28BYJ-48 or D5–D7 for A4988/NEMA17, picked with
a one-line `#define` in the firmware.

---

## Quick start

1. **Build & flash the firmware** onto the Arduino Nano. Install the firmware
   libraries via the Arduino Library Manager: **AccelStepper** (Mike McCauley,
   for non-blocking wheel motion), **Adafruit GFX**, and **Adafruit SSD1306**.
2. **Run the daemon** on your Mac:
   ```sh
   python3 daemon/claude_mate_daemon.py
   ```
   Try it with no hardware/Claude first:
   ```sh
   python3 daemon/claude_mate_daemon.py --mock
   ```
3. **Install the Claude Code hooks** so session events reach the daemon.

Step-by-step guides:

- 📦 **Install** — [docs/INSTALL.md](docs/INSTALL.md)
- 🔌 **Wiring** — [docs/WIRING.md](docs/WIRING.md)
- 📡 **Serial protocol** — [docs/PROTOCOL.md](docs/PROTOCOL.md)
- ✅ **Testing** — [docs/TESTING.md](docs/TESTING.md)

### Configuration

The daemon reads these environment variables (all optional):

| Variable            | Default          | Meaning                                   |
|---------------------|------------------|-------------------------------------------|
| `CLAUDE_MATE_PORT`  | autodetect       | Serial device. Autodetects `/dev/cu.usbserial*` then `/dev/cu.usbmodem*` |
| `CLAUDE_MATE_SOCK`  | `/tmp/claude-mate.sock` | Unix socket the hooks write to     |
| `CLAUDE_MATE_BAUD`  | `115200`         | Serial baud rate                          |

---

## How the status maps to your sessions

The daemon keeps one record per session (keyed by `session_id`, or by project
name if no id is provided). Each session is in one of these states:

| State     | Triggered by         | Meaning                                  |
|-----------|----------------------|------------------------------------------|
| `working` | UserPromptSubmit     | A turn is in progress                     |
| `waiting` | Notification         | Needs permission / Claude asked something |
| `error`   | StopFailure          | Turn ended on an API error (5xx/overloaded/timeout) |
| `done`    | Stop                 | Turn completed OK                         |
| `idle`    | Inactivity (TTL only)   | No active turn — no hook sets this        |

The status wheel is recomputed on every change (priority **WTF > BLOCKED > WIP >
FREE**):

- **WTF** if any session is in `error`.
- **BLOCKED** else if any session is `waiting`.
- **WIP** else if any session is `working`.
- **FREE** otherwise (all idle/done, or no sessions — the home position).

The daemon sends `D|<word>` to the Arduino whenever the word changes; the wheel
rotates to it by the shortest path. Carousel ordering, most urgent first:
`error` → `waiting` → `working` → `done` → `idle`.

---

## Repository layout

```
claude_mate/
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── SECURITY.md
├── .gitignore
├── .editorconfig
├── daemon/
│   └── claude_mate_daemon.py     # Python daemon (pyserial only)
├── firmware/
│   └── claude_mate/              # Arduino sketch (.ino + helpers)
├── hooks/
│   └── claude-status.sh          # installed to ~/.claude/hooks/
└── docs/
    ├── INSTALL.md
    ├── WIRING.md
    ├── PROTOCOL.md
    └── TESTING.md
```

---

## Limitations

- **Retry/resubmit is out of scope.** When a turn ends on an error, Claude Mate
  shows it (wheel to **WTF** + an `error` card) but does **not** offer a "retry"
  action. Reliably resubmitting a turn from outside the GUI is not feasible, so
  FOCUS — taking you to the session — is the intended response.
- **Limits are best-effort.** The usage/rate limit field is shown as a short
  string (e.g. `71%`) when it can be obtained, and as `-` when it cannot. Claude
  Mate never fabricates limit numbers; treat this as an extension point.
- **FOCUS relies on a VS Code deep link with a window-raise fallback.** The
  primary mechanism opens a VS Code deep link for the session id; if that is
  unknown/stale or returns nonzero, the daemon falls back to raising the VS Code
  window for the session's working directory (`open -a "Visual Studio Code"
  <cwd>` / `code <cwd>`). The exact deep-link URI lives behind a single config
  constant so it is trivial to update.
- **macOS-focused.** The daemon targets a Mac (serial device naming, `open`,
  VS Code activation). Other platforms would need port/focus adjustments.

---

## License

MIT — see [LICENSE](LICENSE). © 2026 Claude Mate contributors.
