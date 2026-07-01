# Claude Mate — Architecture

Claude Mate is a USB hardware companion for Claude Code. It is an Arduino Nano
driving a small I2C OLED, three buttons, and a **micro vibration motor**. The
OLED shows one of four big status words (FREE / WIP / BLOCKED / WTF) over a
session-detail line, and the motor buzzes a per-session haptic alert (driven by
the daemon) when a session needs you. It shows the live status of one or more Claude Code sessions running in
the VS Code native extension (and/or the terminal CLI) on a Mac, and lets you
press a button to **focus** the VS Code window of a session that needs your
attention.

Retry / resubmit is intentionally **out of scope** (see [Limitations](#limitations)).
**Focus is the must-have action.**

---

## Data flow

The end-to-end flow is one-directional for *status* (Claude → hardware) and a
short return path for *buttons* (hardware → action):

```
Claude Code hook event (UserPromptSubmit, Notification, Stop, StopFailure)
        |
        v
~/.claude/hooks/claude-status.sh        (fire-and-forget, always exits 0)
        |   one newline-terminated line:  <state>|<session_id>|<name>
        v
Unix domain socket  /tmp/claude-mate.sock
        |
        v
Python daemon  (daemon/claude_mate_daemon.py)
        |   - keeps session-state model keyed by session_id
        |   - recomputes the status word (FREE/WIP/BLOCKED/WTF) on every change
        |   - drives the carousel (~1 card / 3 s)
        v
USB serial  (115200 8N1, ASCII lines, '|' delimited, '\n' terminated)
        |
        v
Arduino Nano (ATmega328P)
        |
        +--> OLED SSD1306 128x32 (I2C, addr 0x3C)   shows the current session card
        |                                            + the current word (FREE/WIP/
        |                                            BLOCKED/WTF) as the big status
        +--> micro vibration motor (D5)             per-session haptic via V|<kind>:
        |                                            START/INPUT one-shot, DONE/ERROR loop

Buttons back (return path) -- layout MODE | SUBMIT | NEXT:
        Button press (SUBMIT=D2, NEXT=D3, MODE=D4)
        |   serial line: B|1 SUBMIT · B|2 NEXT · B|3 MODE-short · B|4 MODE-long
        v
        Arduino  -->  serial  -->  daemon
        |
        +-- B|2 NEXT   : SCROLL -> next card (pause auto-show ~10 s) ; LIST -> highlight down
        +-- B|3 MODE(short): SCROLL -> previous card (pause ~10 s)   ; LIST -> highlight up
        +-- B|4 MODE(long) : toggle UI mode (SCROLL <-> LIST)
        +-- B|1 SUBMIT : focus the VS Code session of the selected tab
                          (current card in SCROLL, highlighted row in LIST)
                          primary  : macOS deep link  (open <FOCUS_URI_TEMPLATE>)
                          fallback : raise VS Code window for the workspace cwd
```

A handshake byte closes the loop after every reset. Opening the USB serial port
resets the Nano (~1.5 s), so on boot the Arduino emits `H`; the daemon answers
`H` by **re-sending the full current state** (word + current card), and the OLED
redraws it. This keeps the display correct across reconnects without any
persistent storage on the device.

---

## Component diagram (ASCII)

```
 +-------------------------------------------------------------+
 |                          macOS host                         |
 |                                                             |
 |  +----------------+        +-----------------------------+  |
 |  |  Claude Code   |        |   Python daemon (always-on) |  |
 |  |  (VS Code ext  |        |                             |  |
 |  |   or CLI)      |        |  +-----------------------+  |  |
 |  |                |        |  | socket server thread  |  |  |
 |  |  hook events   |        |  |  /tmp/claude-mate.sock|  |  |
 |  +-------+--------+        |  +-----------+-----------+  |  |
 |          |                 |              |              |  |
 |          | runs            |              v              |  |
 |          v                 |  +-----------------------+  |  |
 |  +----------------+   sock  |  |  session-state model  |  |  |
 |  | claude-status. +-------->|  |  (dict keyed by sid)  |  |  |
 |  |     sh (hook)  |  line   |  |  + status-word calc   |  |  |
 |  +----------------+         |  +-----------+-----------+  |  |
 |                             |              |              |  |
 |                             |   +----------+----------+   |  |
 |                             |   |                     |   |  |
 |                             |   v                     v   |  |
 |                             | +-----------+   +-----------+|  |
 |                             | | carousel  |   | serial    ||  |
 |                             | | thread    |   | I/O +     ||  |
 |                             | | (~3 s)    |   | button    ||  |
 |                             | +-----+-----+   | reader    ||  |
 |                             |       |         | thread    ||  |
 |                             |       +----+----+-----------+|  |
 |                             +------------|------^----------+  |
 |                                          |      |             |
 +------------------------------------------|------|-------------+
                                            | USB  | B|1 / B|2 / B|3 / B|4
                          D|.. S|.. T|.. I/P | CDC  | H
                                            v      |
                              +-----------------------------------+
                              |        Arduino Nano (328P)        |
                              |                                   |
                              |  +--------+    +----------------+ |
                              |  | OLED   |    | vibration motor| |
                              |  | 0x3C   |    | D5, V|<kind>:  | |
                              |  | I2C    |    | START/INPUT 1shot| |
                              |  | FREE/  |    | DONE/ERROR loop| |
                              |  | WIP/.. |    | OFF = stop     | |
                              |  +--------+    +----------------+ |
                              |  [MODE D4] [SUBMIT D2] [NEXT D3]  |
                              +-----------------------------------+
```

The daemon is the brain. The Arduino is a thin display + input device: it holds
no session model, makes no policy decisions, and simply renders whatever the
daemon last sent and reports button presses.

---

## Status-word logic

The status word reflects the **overall system state**, not a single session. The
daemon recomputes a **word** on every state change and sends `D|<word>` to the
Arduino whenever the word changes; the Arduino draws it on the OLED (visual only).
Haptics are separate — driven per session via `V|<kind>` (see the OLED word +
vibration haptic section above).

```
WTF      if ANY session is in {error}
         -> something blew up: StopFailure / API 5xx / overloaded / timeout
BLOCKED  else if ANY session == waiting
         -> Claude needs your input (a permission/question)
WIP      else if ANY session == working
         -> busy, but nothing needs you right now
FREE     otherwise
         -> all idle/done, or no sessions
```

Priority order is strict: **WTF > BLOCKED > WIP > FREE**. A single `error`
session forces WTF even if others are merely `working`. Only when there is nothing
to act on and nothing in progress does the word return to FREE.

### OLED word + vibration haptic

The OLED is the **sole visual status**: it renders the big word
(FREE / WIP / BLOCKED / WTF) over the session-detail line. There is no wheel, no
dial, no homing, and no endstop.

The **micro vibration motor on D5** is a haptic alert driven **entirely by the
daemon, per session**, via `V|<kind>` lines — *not* by the word. The word (`D|`)
is visual only. The daemon decides when a specific tab started, is waiting,
finished, or errored, and sends the matching kind; the firmware just plays it at
a **graduated-but-soft** PWM amplitude (urgency reads as rhythm, not force):

```
START   3 gentle 0.3s ticks                          one-shot
INPUT   soft double-tap                               re-tapped ~every 10s
DONE    5×0.2s heartbeat (gaps 0.2/0.4s), then rest   LOOPS until V|OFF
ERROR   0.4s on / 0.2s off alarm                      LOOPS until V|OFF
OFF     stop the motor                                (sent on FOCUS / clear)
```

`DONE` and `ERROR` loop in the firmware until the daemon sends `V|OFF` (on FOCUS
or when the alert clears); a firmware watchdog also stops any loop if the daemon
goes silent for ~30 s. See [PROTOCOL.md](PROTOCOL.md) for the exact contract.

The motor pulls more current than a GPIO can source, so D5 drives it through a
small switch: a 3-pin vibro module, an NPN transistor (1 kΩ base resistor +
1N4148 flyback diode), or a spare ULN2003 channel. It is powered from the USB
5 V rail with common grounds — see [WIRING.md](WIRING.md).

---

## Carousel behaviour

The carousel is **daemon-driven**, not Arduino-driven. The Arduino only ever
shows the single card it was last told to show via an `S|...` line.

- The daemon auto-rotates through the known sessions, sending one `S` line per
  step roughly **every 3 seconds**.
- **Ordering — most urgent first:** `error` → `waiting` → `working` → `done` →
  `idle`. This matches the status-word priority (WTF → BLOCKED → WIP → FREE) so
  the most actionable session surfaces first.
- Pressing **NEXT** (`B|2`) advances to the next card immediately **and pauses
  auto-rotation for ~10 seconds**, giving you time to read a specific card
  before the rotation resumes.
- Pressing **MODE** short (`B|3`) steps to the previous card and likewise pauses
  auto-rotation for ~10 seconds. A **long** press (`B|4`) instead toggles into
  **LIST mode** — a scrolling list of *all* tabs (`T` frames), where NEXT /
  MODE-short move the highlight and SUBMIT focuses it; long-press again returns
  to the scroll carousel.
- Pressing **SUBMIT** (`B|1`) acts on the **selected** tab (the currently
  displayed card in SCROLL, the highlighted row in LIST) — it calls `focus()`
  for that session.
- If there are **zero sessions**, the daemon sends `I` (idle screen) together
  with `D|FREE` instead of any `S`/`T` frame.

Each card carries: 1-based index, total count, truncated name (≤10 chars),
state, runtime (`mm:ss` or `h:mm`), and a best-effort limit string (`"-"` when
unknown). The exact line format is in [PROTOCOL.md](PROTOCOL.md).

---

## Focus mechanism

Focus is the single must-have action. When you press the **SUBMIT** button on a
card, the daemon tries to bring you to that exact Claude Code session:

1. **Primary — VS Code deep link.** The daemon runs the macOS `open` command
   against a URI built from a configurable `FOCUS_URI_TEMPLATE` constant,
   formatted with the session id. Per the VERIFIED FACTS, the documented URI
   handler is:

   ```
   vscode://anthropic.claude-code/open?session={session_id}&prompt={prompt}
   ```

   This opens/links a Claude Code chat tab for the given session id. Keeping it
   behind a single constant means if the precise URI changes, it is a one-line
   fix.

2. **Fallback — raise the workspace window (always implemented).** When the
   session id is unknown/stale, or the primary `open` returns nonzero, the daemon
   raises VS Code for the session's workspace folder:
   `open -a "Visual Studio Code" <cwd>` (or `code <cwd>` if the CLI is on PATH),
   optionally activating "Visual Studio Code" via AppleScript.

---

## Robustness summary

- The daemon **keeps the serial port open continuously** to avoid resetting the
  Nano on every message.
- It **auto-detects** the port (`/dev/cu.usbserial*` then `/dev/cu.usbmodem*`)
  and **auto-reconnects** if the device disappears and returns; it never crashes
  on a missing port.
- Work is split across **threads with locks** (socket server, button reader,
  carousel) — no busy-wait spinning.
- The **hook always exits 0** and never blocks Claude; if the daemon/socket is
  down it silently no-ops with a short timeout.
- The **Arduino** tolerates partial/garbled lines, caps its input buffer, ignores
  malformed lines, and debounces buttons (~200 ms).

---

## Limitations

These are deliberate, documented boundaries — not bugs:

- **Retry / resubmit is not supported in the GUI.** Re-running a failed turn is
  not reliably possible from outside the VS Code GUI, so Claude Mate does not
  attempt it. The hardware surfaces an `error` state and shows **WTF** (with the
  looping `V|ERROR` alarm haptic) so a human can act, but the only button action
  toward a session is **FOCUS**.
- **Limits are best-effort.** The `limit` field is a short display string and
  defaults to `"-"`. If the daemon cannot reliably obtain rate/usage limits, it
  shows `"-"` and **never fabricates** numbers. Real limit reporting is an
  extension point, not a guarantee.
- **Focus depends on the deep link.** Primary focus uses the VS Code deep link
  derived from the session id (VERIFIED FACTS:
  `vscode://anthropic.claude-code/open?session=...`). Per those same facts,
  **no documented URI pattern exists to programmatically focus an existing
  session inside an already-running chat panel** — the handler opens/links a chat
  tab. When the session id is unknown/stale or the deep link returns nonzero, the
  daemon falls back to **raising the VS Code window** for the workspace folder.
  Focus is therefore best-effort and may land you on the workspace rather than
  the precise chat in some cases.
- **No persistence on the device.** The Arduino holds no state; every reset
  relies on the `H` handshake and the daemon re-sending state. If the daemon is
  not running, the display is stale/blank.
