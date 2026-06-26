# Claude Mate — Architecture

Claude Mate is a USB hardware companion for Claude Code. It is an Arduino Nano
driving a small I2C OLED, two buttons, and a 3-LED traffic light. It shows the
live status of one or more Claude Code sessions running in the VS Code native
extension (and/or the terminal CLI) on a Mac, and lets you press a button to
**focus** the VS Code window of a session that needs your attention.

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
        |   - recomputes traffic-light color on every change
        |   - drives the carousel (~1 card / 3 s)
        v
USB serial  (115200 8N1, ASCII lines, '|' delimited, '\n' terminated)
        |
        v
Arduino Nano (ATmega328P)
        |
        +--> OLED SSD1306 128x64 (I2C, addr 0x3C)   shows the current session card
        +--> 3 LEDs (G/Y/R via 220R)                the traffic light
        +--> optional piezo buzzer                  one chirp on transition into RED

Buttons back (return path):
        Button press (FOCUS=D2, NEXT=D3)
        |   serial line:  B|1  (FOCUS)  or  B|2  (NEXT)
        v
        Arduino  -->  serial  -->  daemon
        |
        +-- B|2 NEXT  : advance carousel immediately, pause auto-rotation ~10 s
        +-- B|1 FOCUS : focus the VS Code session of the currently displayed card
                          primary  : macOS deep link  (open <FOCUS_URI_TEMPLATE>)
                          fallback : raise VS Code window for the workspace cwd
```

A handshake byte closes the loop after every reset. Opening the USB serial port
resets the Nano (~1.5 s), so on boot the Arduino emits `H`; the daemon answers
`H` by **re-sending the full current state** (light + current card). This keeps
the display correct across reconnects without any persistent storage on the
device.

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
 |  |     sh (hook)  |  line   |  |  + traffic-light calc |  |  |
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
                                            | USB  | B|1 / B|2
                              L|.. S|.. I/P | CDC  | H
                                            v      |
                              +-----------------------------------+
                              |        Arduino Nano (328P)        |
                              |                                   |
                              |  +--------+   +----+ +----+ +----+|
                              |  | OLED   |   | G  | | Y  | | R  ||
                              |  | 0x3C   |   |LED | |LED | |LED ||
                              |  | I2C    |   |D5  | |D6  | |D7  ||
                              |  +--------+   +----+ +----+ +----+|
                              |                                   |
                              |  [FOCUS btn D2]  [NEXT btn D3]    |
                              |  (opt. buzzer D8)                 |
                              +-----------------------------------+
```

The daemon is the brain. The Arduino is a thin display + input device: it holds
no session model, makes no policy decisions, and simply renders whatever the
daemon last sent and reports button presses.

---

## Traffic-light logic

The traffic light reflects the **overall system state**, not a single session.
The daemon recomputes it on every state change and sends `L|<color>` to the
Arduino whenever the color changes.

```
RED     if ANY session is in {error, waiting}
        -> a human is needed: the queue of failures / questions
YELLOW  else if ANY session == working
        -> busy, but nothing needs you right now
GREEN   otherwise
        -> a free/done session and no problems, or everything idle
```

Priority order is strict: a single `error` or `waiting` session forces RED even
if other sessions are merely `working`. Only when there is nothing to act on and
nothing in progress does the light go GREEN.

The optional buzzer chirps **once** on the *transition into* RED (a rising-edge
alert), not continuously while RED — see [WIRING.md](WIRING.md).

---

## Carousel behaviour

The carousel is **daemon-driven**, not Arduino-driven. The Arduino only ever
shows the single card it was last told to show via an `S|...` line.

- The daemon auto-rotates through the known sessions, sending one `S` line per
  step roughly **every 3 seconds**.
- **Ordering — most urgent first:** `error` → `waiting` → `working` → `done` →
  `idle`. This matches the traffic-light priority so the most actionable session
  surfaces first.
- Pressing **NEXT** (`B|2`) advances to the next card immediately **and pauses
  auto-rotation for ~10 seconds**, giving you time to read a specific card
  before the rotation resumes.
- Pressing **FOCUS** (`B|1`) acts on the **currently displayed** card — it calls
  `focus()` for that session.
- If there are **zero sessions**, the daemon sends `I` (idle screen) together
  with `L|G` (green light) instead of any `S` card.

Each card carries: 1-based index, total count, truncated name (≤10 chars),
state, runtime (`mm:ss` or `h:mm`), and a best-effort limit string (`"-"` when
unknown). The exact line format is in [PROTOCOL.md](PROTOCOL.md).

---

## Focus mechanism

FOCUS is the single must-have action. When you press the FOCUS button on a card,
the daemon tries to bring you to that exact Claude Code session:

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
  attempt it. The hardware surfaces an `error` state and turns the light RED so a
  human can act, but the only button action toward a session is **FOCUS**.
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
