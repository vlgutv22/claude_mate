# Claude Mate

A tiny USB hardware companion that shows the **live status of your Claude Code
sessions** on a small OLED, **buzzes a micro vibration motor** the moment one of
them needs you — finishes, gets blocked, or errors — and lets you **jump
straight to the session that needs you** with a single button press.

Plug it into your Mac, run the daemon, and feed it either via the Claude Code
**hooks** or via the new **PTY wrapper** (`claude-mate-wrap`). The device becomes
an ambient, always-on status pane for every Claude Code session you have open —
in VS Code, the terminal CLI, iTerm2, tmux, anywhere.

```
        ┌────────────────────────────────┐
        │ ● api-server              2/4   │   ← ack dot · session name · card index
        │ Opus 4.8 · xhigh                │   ← model · effort (PTY-wrapper sessions)
        │ WAIT  04:12                     │   ← big STATE word · live time-in-state
        └────────────────────────────────┘
              ((•)) vibration motor             ← buzzes per session, when IT needs you
          [ MODE ]   [ SUBMIT ]   [ NEXT ]     ← three buttons
   MODE: short = prev / list-up · long = switch SCROLL↔LIST   SUBMIT: short = focus tab · long = acknowledge   NEXT: next / list-down

   the dot:   ● blinking = unacknowledged (needs you)   ○ hollow = acknowledged (focused)
   the buzz:  START 3 soft ticks · INPUT gentle tap · DONE heartbeat-loop · ERROR alarm-loop  (loops/re-taps until you SUBMIT)
```

---

## What it is

**Claude Mate** is an Arduino Nano + 0.91" I2C OLED + 3 buttons + a **micro
vibration motor**, paired with a lightweight Python daemon on your Mac.

The daemon keeps a model of every Claude Code session, **auto-surfaces the one
that most needs you**, drives the OLED card, and **buzzes the motor per session**
— so a single tab finishing, blocking, or erroring is *felt* even while the rest
of your fleet keeps working. It keeps nagging (gently) until you deal with it.

The single must-have action is **FOCUS**: press a button and the window for the
displayed session is raised so you can deal with it — the integrated VS Code
panel *or* the actual terminal that session is running in (matched by TTY).
Retrying or resubmitting a turn is intentionally **out of scope** (see
[Limitations](#limitations)).

---

## Two ways to feed it

You can drive the daemon from **either or both** of these — mix freely:

| Path | What it is | What it can see |
|------|-----------|-----------------|
| **(a) Claude Code hooks** | `hooks/claude-status.sh`, wired to `UserPromptSubmit` / `Notification` / `Stop` / `StopFailure`. Fire-and-forget; never blocks or fails a turn. | Turn boundaries: started, needs-input, finished OK, finished on error. Works in the VS Code extension and the CLI. |
| **(b) PTY wrapper** `bin/claude-mate-wrap` | Run `claude` *through* a wrapper (`alias claude=claude-mate-wrap`). It forks a pseudo-terminal, relays stdin/stdout transparently, and mirrors the TUI into a headless terminal emulator to read the **live screen**. | Everything on screen the hooks can't report: the spinner, **API errors/retries**, **permission prompts**, interactive option-pickers, and *"Waiting for N dynamic workflow to finish"* — i.e. it knows a session is **still busy after the turn "ends"**. |

The wrapper is the more capable feed (true live state, plus terminal focus); the
hooks are the zero-dependency feed. Use whichever fits each session.

---

## Features

- **Auto-surface the urgent tab** — the daemon shows the single most urgent
  session that needs you, ordered **error → waiting → done → working → idle**.
  There is **no blind auto-carousel**; the screen holds the thing you should
  look at. **NEXT** and **MODE (short)** step the same urgent-first order manually
  and pause auto-surfacing for ~10 s so you can browse.
- **Two UI modes** — a long-press of **MODE** toggles between **SCROLL** (the
  one-card carousel above) and **LIST** (a scrolling list of *all* tabs, each with
  a status label — `WIP`/`WAIT`/`ERR`/`DONE`/`IDLE` — and its name). In LIST,
  **NEXT** / **MODE-short** move the highlight, **SUBMIT** focuses the
  highlighted tab (double-click opens/closes its **detail** card), and a
  **long-press of SUBMIT acknowledges** the selected tab's alert without
  focusing it. Buttons fire instantly (edge-accepted debounce; ~500 ms hold =
  long-press).
- **Per-session haptics** — the motor buzzes for *that session's own*
  transition, not just an aggregate change:
  - **START** — a job (re)started → three gentle 0.3 s ticks (one-shot).
  - **INPUT** — a session is waiting on you → a soft double-tap, re-tapped
    gently every ~10 s until you focus it.
  - **DONE** — a turn finished → a 5-pulse "heartbeat" (0.2 s pulses, gaps
    alternating 0.2 / 0.4 s), **looping** until you focus it.
  - **ERROR** — a turn ended on an API error → a 0.4 s-on / 0.2 s-off alarm,
    **looping** until you focus it.

  Amplitude is PWM duty kept **graduated but soft** — Start/Done gentlest, the
  error alarm firmest but never full power — so urgency reads as *rhythm and
  repetition*, never a jarring jolt.
- **Loop / re-tap until acknowledged** — the DONE and ERROR alerts **loop
  continuously** in the firmware, and the waiting alert re-taps every ~10 s, until
  you **FOCUS** the session (which sends `V|OFF` to silence the motor) or it
  changes on its own. If the daemon dies mid-alert the firmware stops the loop on
  its own after ~30 s of silence.
- **"Finished but not seen" model** — when a turn ends, the session becomes
  **done** and *stays* done (alerting, with a blinking dot) until you focus it;
  later idle keepalives don't silently clear it. Focusing acknowledges it.
- **Acknowledge dot on the card** — alert states (done / waiting / error) carry a
  dot top-left: **filled + blinking = unacknowledged**, **hollow ring =
  acknowledged**. At a glance you know whether you've seen it.
- **Live time-in-state** — the card's timer counts up live for every state
  except `done`: an idle/blocked/errored tab shows *how long it has been sitting
  there*, while a finished tab shows the completed turn's frozen duration.
- **One-button FOCUS** — raises the session's window: first the **PTY wrapper's
  own terminal** (iTerm2 / Terminal.app / VS Code / Ghostty / Warp / tmux,
  matched by TTY), else a **VS Code deep link**, else the VS Code window for the
  workspace folder.
- **Hot-reloadable detection** — the wrapper's state patterns live in
  [`patterns.json`](patterns.json) and reload live (~0.25 s, no restart), so you
  can tune what counts as error/waiting/busy without touching code.
- **Robust by design** — the daemon keeps the serial port open continuously,
  auto-detects and auto-reconnects to the device, never crashes on a missing
  port; the hooks never block a turn; the wrapper falls back to running `claude`
  directly if anything is wrong.
- **`--mock` demo mode** — run the whole display with fake sessions cycling
  through every state, no Claude and no hardware required.

---

## Architecture

```
   Claude Code session
   ├─ (a) hooks ─ ~/.claude/hooks/claude-status.sh
   │                 "<state>|<sid>|<name>\n"
   │
   └─ (b) PTY wrapper ─ bin/claude-mate-wrap  (alias claude=claude-mate-wrap)
                     "<state>|<sid>|<name>|<ctrl_sock>\n"   + screen-scrapes the live TUI
                            │
                            ▼
              Unix domain socket  /tmp/claude-mate.sock
                            │
                            ▼
   ┌──────────────────────────────────────────────┐
   │   Python daemon (Mac)                        │
   │   • session model + "done-until-acknowledged"│
   │   • status word     D|<FREE/WIP/BLOCKED/WTF> │   (overall health / priority)
   │   • per-session buzz V|<START/INPUT/DONE/ERR>│   loops + V|OFF on focus
   │   • auto-surface the most-urgent session     │
   │   • FOCUS: wrapper ctrl-sock → VS Code link  │
   └──────────────────────────────────────────────┘
                            │  USB serial 115200 8N1, "|"-delimited ASCII
                            ▼
   ┌──────────────────────────────────────────────┐
   │   Arduino Nano (ATmega328P)                  │
   │   • SSD1306 128x32 OLED — status card: ack    │
   │     dot · name · idx · model·effort · STATE   │
   │   • micro vibration motor (D5) plays V|<KIND>│
   │   • SUBMIT/NEXT/MODE buttons → B|1..B|5       │
   └──────────────────────────────────────────────┘
                            │  H (hello on boot), B|<n> (buttons)
                            └──────────────► back to the daemon
```

Two control flows worth calling out:

- **FOCUS round-trip.** Each wrapped session opens a per-session control socket
  (`/tmp/claude-mate-ctrl-<id>.sock`) and tells the daemon about it. On a FOCUS
  press the daemon connects to that socket and sends `focus`; the wrapper raises
  *its own* terminal window using the right method for `$TERM_PROGRAM` (iTerm2
  and Terminal.app match the exact tab by TTY). Hook-only sessions fall back to a
  VS Code deep link / window raise.
- **Reset recovery.** Opening the USB serial port resets the Nano (~1.5 s). On
  boot the Arduino emits `H`; the daemon responds by re-sending the full current
  state (status word + current card), so the display recovers cleanly after any
  reconnect.

> The aggregate **FREE / WIP / BLOCKED / WTF** word (priority **WTF > BLOCKED >
> WIP > FREE**) is the daemon's overall health-and-priority model: it decides
> which session is surfaced and how it buzzes. The OLED's *big text* is the
> surfaced session's own **state** (WORKING / WAITING / ERROR / DONE / IDLE).

---

## Haptics & the acknowledge model

Haptics are driven **entirely by the daemon** via `V|<KIND>` lines — the
firmware just plays the pattern; the status word (`D|`) is visual only and never
buzzes on its own. The daemon decides, **per session**, when to buzz and how it
repeats. Amplitude (PWM duty) is **graduated but soft** — urgency reads as
rhythm, not raw force:

| Event (per session) | `V|` kind | Pattern | Repeat until focused |
|---------------------|-----------|---------|----------------------|
| Job (re)started     | `START`   | 3 gentle 0.3 s ticks | — (one-shot) |
| Waiting on you      | `INPUT`   | soft double-tap | re-taps every **~10 s** |
| Turn finished       | `DONE`    | 5×0.2 s heartbeat (gaps 0.2/0.4 s) | **loops** continuously |
| Error / retry       | `ERROR`   | 0.4 s on / 0.2 s off alarm | **loops** continuously |

`DONE` and `ERROR` **loop** in the firmware until the daemon sends `V|OFF`;
`INPUT` re-taps on a timer. A turn ending (`working → idle`) becomes **done** and
keeps looping until you acknowledge the session — focusing it (**SUBMIT**
short-press) or silencing it in place (**SUBMIT** long-press) acknowledges it
(sending `V|OFF` to silence the motor: a done tab becomes idle; a waiting/error
tab goes quiet but keeps its state until it changes). The OLED's ack dot mirrors
this: blinking while unacknowledged, hollow once seen. If the daemon ever dies
mid-alert, the firmware stops the loop on its own after ~30 s of serial silence.

---

## Bill of Materials

| Qty | Part                                    | Notes                                  |
|-----|-----------------------------------------|----------------------------------------|
| 1   | Arduino Nano (ATmega328P)               | Any USB-serial Nano clone works        |
| 1   | SSD1306 0.91" 128×32 OLED, I2C           | Address `0x3C` (some boards `0x3D`); 0.96" 128×64 also works |
| 3   | Momentary push buttons                  | MODE, SUBMIT, NEXT                     |
| 1   | Micro vibration motor                   | coin/pager type; haptic alert on D5    |
| 1   | NPN transistor + ~1 kΩ + 1N4148 diode   | D5 driver — **or** a 3-pin vibro module / a spare ULN2003 channel instead |
| —   | Jumper wires, breadboard / perfboard    |                                        |
| 1   | USB cable (to the Mac)                   | Data-capable, not charge-only          |

> The vibration motor is tiny (tens of mA) so the **USB 5 V rail powers it
> fine** — no brown-out concern. Don't drive it straight off D5: use a vibro
> module, an NPN transistor (1 kΩ base + 1N4148 flyback), or a ULN2003 channel,
> and keep all grounds common. See [docs/WIRING.md](docs/WIRING.md).

Pinout summary (full details in [docs/WIRING.md](docs/WIRING.md)):

| Signal               | Pin       | Notes                                  |
|----------------------|-----------|----------------------------------------|
| OLED SDA             | A4        | I2C                                    |
| OLED SCL             | A5        | I2C                                    |
| SUBMIT button        | D2        | `INPUT_PULLUP`, emits `B|1` short (focus) / `B|5` long (acknowledge) |
| NEXT button          | D3        | `INPUT_PULLUP`, emits `B|2` (next / highlight down) |
| MODE button          | D4        | `INPUT_PULLUP`, emits `B|3` short / `B|4` long (prev / mode toggle) |
| Vibration motor drive| D5        | PWM-capable; never drive the motor directly |

The three buttons use `INPUT_PULLUP` (other leg to GND; pressed = LOW). D5 drives
the vibration motor through a module / NPN+1k+1N4148 / ULN2003 channel. D5 is
PWM-capable, which is how the firmware keeps the patterns gentle (amplitude well
below full power).

---

## Quick start

1. **Build & flash the firmware** (`firmware/claude_mate/claude_mate.ino`) onto
   the Arduino Nano. Install the libraries via the Arduino Library Manager:
   **Adafruit GFX** and **Adafruit SSD1306** (the OLED is the only thing that
   needs a library).
2. **Run the daemon** on your Mac:
   ```sh
   python3 daemon/claude_mate_daemon.py
   ```
   Try it with no hardware/Claude first:
   ```sh
   python3 daemon/claude_mate_daemon.py --mock
   ```
3. **Feed it.** Pick either (or both):
   - **Hooks:** install the Claude Code hooks so session events reach the daemon.
   - **PTY wrapper:** `pip install pyte`, then run Claude through the wrapper:
     ```sh
     alias claude="$PWD/bin/claude-mate-wrap"
     claude            # use Claude exactly as normal — now with live state + FOCUS
     ```
     The wrapper is safe to install as a global `claude` shim: non-interactive
     (`claude -p …`, pipes, CI) execs the real binary, and it locates the real
     `claude` even when every `claude` on `PATH` is your own shim.

Step-by-step guides:

- 📦 **Install** — [docs/INSTALL.md](docs/INSTALL.md)
- 🔌 **Wiring** — [docs/WIRING.md](docs/WIRING.md)
- 📡 **Serial protocol** — [docs/PROTOCOL.md](docs/PROTOCOL.md)
- ✅ **Testing** — [docs/TESTING.md](docs/TESTING.md)

### Configuration

**Daemon** environment variables (all optional):

| Variable            | Default          | Meaning                                   |
|---------------------|------------------|-------------------------------------------|
| `CLAUDE_MATE_PORT`  | autodetect       | Serial device. Autodetects `/dev/cu.usbserial*` then `/dev/cu.usbmodem*` |
| `CLAUDE_MATE_SOCK`  | `/tmp/claude-mate.sock` | Unix socket the hooks/wrapper write to |
| `CLAUDE_MATE_BAUD`  | `115200`         | Serial baud rate                          |

**PTY wrapper** environment variables:

| Variable               | Default               | Meaning                                |
|------------------------|-----------------------|----------------------------------------|
| `CLAUDE_MATE_SOCK`     | `/tmp/claude-mate.sock` | Daemon socket to report state to     |
| `CLAUDE_MATE_PATTERNS` | `<repo>/patterns.json`  | Detection patterns file (hot-reloaded) |
| `CLAUDE_MATE_DEBUG`    | unset                 | If set to a path, append screen + state snapshots there (debugging) |
| `CLAUDE_REAL`          | autodetect            | Explicit path to the real `claude` binary |

**Detection tuning** lives in [`patterns.json`](patterns.json) — case-insensitive
substrings matched against the rendered TUI, grouped as `error` / `waiting` /
`waiting_footer` / `busy` (precedence: error > waiting > waiting_footer > busy >
idle). Matching is scoped to Claude's **live status region** (the bottom ~20
non-empty lines) for error/waiting/busy, and to the **footer** (~4 lines) for the
generic picker phrases — so the same keywords in scrollback, logs, code, or
conversation above don't false-trigger. The footer phrases are limited to
`to select` / `to navigate` (a real question picker), and a footer-only `waiting`
must **persist a couple of seconds** before it reports — so a config dialog you
open and dismiss (the `/effort` or `/model` slider, whose footer is
`… · Esc to cancel`) no longer buzzes the instant you open it.

---

## How the status maps to your sessions

The daemon keeps one record per session (keyed by `session_id`, or by name if no
id is provided). Each session is in one of these states:

| State     | Triggered by                          | Meaning                                  |
|-----------|---------------------------------------|------------------------------------------|
| `working` | `UserPromptSubmit` / wrapper "busy"   | A turn is in progress (or a background workflow is still running) |
| `waiting` | `Notification` / wrapper prompt+picker | Needs permission, a question, or a menu choice |
| `error`   | `StopFailure` / wrapper API-error      | Turn ended on an API error (5xx / overloaded / timeout / usage limit) |
| `done`    | `Stop` (then held until acknowledged)  | Turn completed OK — keeps alerting until focused |
| `idle`    | Inactivity (TTL) / acknowledged done   | No active turn                            |

The daemon recomputes the overall **status word** on every change (priority
**WTF > BLOCKED > WIP > FREE**) and sends `D|<word>`:

- **WTF** if any session is in `error`.
- **BLOCKED** else if any session is `waiting`.
- **WIP** else if any session is `working`.
- **FREE** otherwise (all idle/done, or no sessions).

That word is the **priority model**: it decides which session gets auto-surfaced
and which buzz fires. Auto-surface and navigation order, most urgent first:
`error → waiting → done → working → idle`. The OLED shows that surfaced session's
card; with zero sessions it shows the `IDLE` screen.

---

## Repository layout

```
claude_mate/
├── README.md
├── LICENSE · CONTRIBUTING.md · CODE_OF_CONDUCT.md · SECURITY.md
├── patterns.json                  # hot-reloadable state-detection tuning
├── bin/
│   └── claude-mate-wrap           # PTY wrapper: live state + terminal FOCUS (pyte)
├── daemon/
│   └── claude_mate_daemon.py      # Python daemon (pyserial only)
├── firmware/
│   ├── claude_mate/               # Arduino sketch (OLED card + vibration motor)
│   └── selftest/                  # hardware self-test sketch
├── hooks/
│   ├── claude-status.sh           # installed to ~/.claude/hooks/
│   └── settings.snippet.json      # the four hook wirings
├── install/                       # install.sh / uninstall.sh / LaunchAgent plist
├── packaging/                     # notarizable macOS .pkg installer
├── tools/                         # feed.sh + e2e / wrapper / settings-merge tests
└── docs/
    ├── INSTALL.md · WIRING.md · PROTOCOL.md · TESTING.md · ARCHITECTURE.md
```

---

## Limitations

- **Retry/resubmit is out of scope.** When a turn ends on an error, Claude Mate
  shows it (`error` card + the looping 0.4/0.2 s alarm buzz until you focus it)
  but does **not** offer a "retry" action. Reliably resubmitting a turn from outside the
  GUI is not feasible, so FOCUS — taking you to the session — is the intended
  response.
- **Limits are best-effort.** The usage/rate-limit field is shown as a short
  string (e.g. `71%`) when it can be obtained, and as `-` when it cannot. Claude
  Mate never fabricates limit numbers; treat this as an extension point.
- **FOCUS targets the wrapper's terminal first, then VS Code.** A wrapped session
  raises its own terminal window (matched by TTY) reliably. Hook-only sessions
  fall back to a VS Code deep link, then to raising the VS Code window for the
  workspace folder — the exact deep-link URI lives behind a single config
  constant so it is trivial to update.
- **Detection is screen-scrape-based (wrapper).** State is inferred from the
  rendered TUI using `patterns.json`. It is scoped to the live status region to
  avoid false positives, but unusual terminal themes/locales may need a pattern
  tweak — which you can do live, no restart.
- **macOS-focused.** The daemon and wrapper target a Mac (serial device naming,
  `open`, AppleScript focus). Other platforms would need port/focus adjustments.

---

## What changed (2026-06-29)

A big iteration day. Highlights:

- **Hardware redesign:** dropped the stepper-driven status wheel; the device is
  now a **0.91" 128×32 OLED + micro vibration motor + 3 buttons**. The OLED shows
  a per-session status card (state + live timer + acknowledge dot).
- **New PTY wrapper** (`bin/claude-mate-wrap`): wrap `claude` to read its **live
  TUI state** (errors, prompts, pickers, background-workflow "still busy") and to
  raise the **exact terminal** on FOCUS (by TTY). Safe to install as a global
  `claude` shim.
- **Per-session haptics + acknowledge model:** the motor buzzes for *each
  session's own* start/finish/block/error (`V|<KIND>`). The DONE and ERROR alerts
  **loop** in the firmware (waiting re-taps every ~10 s) until you FOCUS, which
  sends `V|OFF`; a finished turn stays "done" until seen. The OLED carries a
  blinking/hollow ack dot.
- **No blind auto-carousel:** the screen auto-surfaces the single most-urgent
  unacknowledged tab; NEXT/PREV browse manually and pause auto-surface ~10 s.
- **Tighter detection:** `patterns.json` (hot-reloadable), state matching scoped
  to the live status region (bottom ~20 lines) + footer-only picker phrases,
  option-pickers treated as **waiting**, and `usage limit reached` treated as
  **error**.
- **Live time-in-state** timer; gentle **looping** DONE/ERROR haptics (soft
  heartbeat / alarm) that stop on FOCUS via `V|OFF`; assorted reliability fixes
  (loop-idempotent sends, daemon-silence watchdog, handshake resend, nav-pause order).

---

## License

MIT — see [LICENSE](LICENSE). © 2026 Claude Mate contributors.
