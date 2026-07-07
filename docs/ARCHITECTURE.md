# Claude Mate — Architecture

Claude Mate is a USB hardware companion for Claude Code. It is an Arduino Nano
driving a small I2C OLED, three buttons, and an **indication LED**. The daemon
keeps ONE **stable, alphabetically-ordered** **triage queue** of sessions (tabs
never shuffle as their states change) and the OLED shows ONE screen — the
*selected* session (the selection is **sticky**: only PREV/NEXT/GO ever move
it): four size-1 rows — the session name, the
state + time-in-state (+ the account the session runs as), the model + effort
(+ a remaining-limit chip, e.g. `5h82%` = 82% of the 5-hour window left), and a
whole-fleet letter strip (with the queue position). The LED blinks a
status-distinct pattern (driven by the daemon) for the worst unacknowledged
alert, looping until you acknowledge it. It shows the live status of one or
more Claude Code sessions running in the VS Code native extension (and/or the
terminal CLI) on a Mac, and lets you press a button to **raise** the window of
a session that needs your attention.

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
        |   - keeps ONE triage queue in STABLE order (alphabetical by
        |     name, tiebroken by session key) — tabs never shuffle
        |   - urgency (worst unacked: error > waiting > done, oldest
        |     first) is separate: drives the LED + fleet blink only
        |   - pre-renders ONE screen:  F|<flags>|<sel>|<r0>|<r1>|<r2>|<r3>
        |   - drives the LED:  V|<kind> for the worst unacked alert class
        v
USB serial  (115200 8N1, ASCII lines, '|' delimited, '\n' terminated)
        |
        v
Arduino Nano (ATmega328P)
        |
        +--> OLED SSD1306 128x32 (software I2C, addr 0x3C)  draws the one frame it
        |                                            was last sent: four size-1
        |                                            rows — name (flashing ~2.5 Hz
        |                                            while unacked), state+time
        |                                            +account, model+effort
        |                                            +remaining-limit, fleet strip
        +--> indication LED (D8)                    plays V|<kind>: START one-shot;
                                                     INPUT/DONE/ERROR loop until V|OFF

Buttons back (return path) -- layout PREV | GO | NEXT:
        Button press (PREV=D4, GO=D2, NEXT=D3)
        |   serial line: B|P PREV · B|N NEXT · B|G GO-short · B|K GO-long
        v
        Arduino  -->  serial  -->  daemon
        |
        +-- B|P PREV   : selection one step up the queue   (auto-repeats while held)
        +-- B|N NEXT   : selection one step down the queue (auto-repeats while held)
        +-- B|G GO     : single = ack + RAISE the shown session's window (stay);
        |                 double-click = toggle FOLLOW mode
        |                 best    : PTY wrapper ctrl socket ('focus' — raise only)
        |                 primary : macOS deep link  (open <FOCUS_URI_TEMPLATE>)
        |                 fallback: raise VS Code window for the workspace cwd
        +-- B|K GO-long: acknowledge WITHOUT raising anything
```

A handshake byte closes the loop after every reset. Opening the USB serial port
resets the Nano (~1.5 s), so on boot the Arduino emits `H`; the daemon answers
`H` by **re-sending the full current state** (the current frame + re-arming the
LED loop), and the OLED redraws it. This keeps the display correct across
reconnects without any persistent storage on the device.

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
 |  +----------------+   sock  |  | session registry      |  |  |
 |  | claude-status. +-------->|  |  (dict keyed by sid)  |  |  |
 |  |     sh (hook)  |  line   |  |  + the triage queue   |  |  |
 |  +----------------+         |  +-----------+-----------+  |  |
 |                             |              |              |  |
 |                             |   +----------+----------+   |  |
 |                             |   |                     |   |  |
 |                             |   v                     v   |  |
 |                             | +-----------+   +-----------+|  |
 |                             | | screen    |   | serial    ||  |
 |                             | | composer  |   | maintainer||  |
 |                             | | + 1 Hz    |   | + button  ||  |
 |                             | | ticker    |   | reader    ||  |
 |                             | +-----+-----+   +-----+-----+|  |
 |                             +-------|---------------^------+  |
 |                                     |               |         |
 +-------------------------------------|---------------|---------+
                                       | USB           | B|P / B|N / B|G / B|K
                             F|.. V|.. P | CDC          | H
                                       v               |
                              +-----------------------------------+
                              |        Arduino Nano (328P)        |
                              |                                   |
                              |  +--------+    +----------------+ |
                              |  | OLED   |    | indication LED | |
                              |  | 0x3C   |    | D8, V|<kind>:  | |
                              |  | one F| |    | START one-shot | |
                              |  | frame  |    | INPUT/DONE/ERR | |
                              |  |        |    | loop until OFF | |
                              |  +--------+    +----------------+ |
                              |   [PREV D4]  [GO D2]  [NEXT D3]   |
                              +-----------------------------------+
```

The daemon is the brain. The Arduino is a thin display + input device: it holds
no session model, makes no policy decisions, and simply renders the one frame
the daemon last sent and reports button presses. The only screens it draws on
its own are the boot **splash** (`MATE / starting...`) and the **LINK LOST**
screen (`NO LINK / waiting for daemon`) after ~30 s of daemon silence; on every
accepted press it also inverts the whole panel for ~80 ms as instant feedback.

---

## Triage-queue logic

There are **no UI modes** and no aggregate status word. The **triage queue** is a
**stable, alphabetically-ordered** list of sessions — tabs keep their position as
their states change and never shuffle under the user:

```
tab order:  STABLE — alphabetical by name (tiebreak: session key)
urgency:    worst UNACKNOWLEDGED alert (error > waiting > done, oldest first)
            → drives the LED loop class + the blinking fleet letters ONLY
```

Urgency is computed **separately** from the order. It decides only what the LED
blinks about and which fleet letters blink; the shown tab is **sticky** — the
screen never switches subject on its own. An already-acknowledged error never
hides a fresh waiting alert in the LED, but the tab order itself never
reorders.

An alert is born on the transition into `error`/`waiting`/`done` and dies in
exactly four ways — nothing else removes one, so none can be lost silently:

1. **`B|G` GO** — acknowledged + the window raised.
2. **`B|K` ACK** — acknowledged, no window op.
3. **Auto-resolve** — the session leaves the alert class on its own (the user
   answered in the terminal → `working`; a new turn started).
4. **TTL prune** — `done` sessions drop after 120 s without updates; anything
   drops after 600 s.

Acknowledging a `done` session turns it `idle`. **Flap suppression:** a session
re-entering the SAME alert class within 5 s of being acknowledged stays
acknowledged, so a bouncing detector cannot re-fire the LED the user just
silenced.

### The indication LED

The OLED is the **sole visual status**; the **indication LED on D8** is the
alert output, driven **entirely by the daemon** via `V|<kind>` lines — the
firmware just plays the pattern. The pattern is always the class of the
**worst unacknowledged alert** across all sessions (`ERROR` > `INPUT` >
`DONE`), so the LED loops exactly while something needs the human:

```
START   one long 1 s blink, then dark                 one-shot
INPUT   aggressive even blink (~2.8 Hz)               LOOPS until V|OFF
ERROR   super-aggressive fast strobe (~7 Hz)          LOOPS until V|OFF
DONE    cascade — 4 quick blinks, then a pause        LOOPS until V|OFF
OFF     LED off now                                   (sent when nothing is unacked)
```

The daemon (re)sends a loop kind only when the desired loop *changes*, so the
link is not spammed. As a failsafe the firmware stops any loop **and shows the
LINK LOST screen** if the daemon goes fully silent for ~30 s. See
[PROTOCOL.md](PROTOCOL.md) for the exact contract.

---

## Screen behaviour

The screen is **daemon-driven**: the Arduino only ever shows the single frame
it was last told to show via an `F|<flags>|<sel>|<r0>|<r1>|<r2>|<r3>` line.

- **What a frame shows:** four size-1 rows (≤ 21 chars each) — **r0** the
  selected session's name (full width, ~21 chars, inverting ~2.5 Hz while its
  alert is unacknowledged); **r1** the state (4-char tag
  `ERR `/`WAIT`/`DONE`/`WORK`/`IDLE` + time in that state, plus the session's
  account right-aligned); **r2** the best-fit model + effort plus the
  remaining-limit chip right-aligned (e.g. `5h82%`); **r3** the fleet strip (`pos/total` + one
  status letter per session in stable (alphabetical) order, space-separated —
  `E` error, `B` waiting, `W` working, `D` done, `I` idle; cut with a trailing
  `+` when it doesn't fit). An unacknowledged alert's letter is sent **lowercase**
  so the firmware **blinks** it — the acked/unacked state of every tab is
  visible in the strip.
- **Screen ownership:** the display NEVER changes subject on its own — only
  PREV/NEXT/GO move the selection (it falls back to the first tab only when
  the selected session ends). A GO/ACK **stays on the tab** it acted on — the
  device never auto-switches tabs.
- **Active-tab highlight / FOLLOW:** the shown session's fleet letter is drawn in
  a **wide (~11 px) filled rectangle centred on it** (a lit block, letter knocked
  out) so you can see which tab is on screen. Double-clicking GO toggles
  **FOLLOW** mode (a ► marker by the state row): PREV/NEXT then also raise the
  selected terminal, ~250 ms after the selection settles (raise only — never ack
  or collapse).
- **Navigation:** **PREV** / **NEXT** step the selection up/down the queue,
  wrap around the ends, and auto-repeat while held (400 ms to start, then
  5/s). Selection is tracked by session **key**, so the subject stays put as
  sessions come and go (only the `pos/total` number changes).
- **WYSIWYG:** GO/ACK act on exactly the session whose frame is on the glass —
  never a freshly recomputed most-urgent alert. So a press can only ever raise the
  terminal whose name the user is actually looking at.
- The daemon sends an `F` line whenever the rendered bytes change: immediately
  on any state change or button, and ~1/s while a displayed time ticks.
  Identical frames are not re-sent (except on handshake). With **zero
  sessions** it sends `F|0|-1|MATE|no sessions||`.

Long sibling names that collide at the row width are disambiguated with a
middle squeeze (first 9 + `~` + last 10, e.g. `webapp-ba~ervice-one`). The
exact line format is in [PROTOCOL.md](PROTOCOL.md).

---

## Focus mechanism

Focus is the single must-have action, and it is the **only window operation in
the entire system**: navigation NEVER touches macOS windows, and focus only
**raises/activates** — the daemon never collapses, resizes, or miniaturizes
anything (the wrapper's `collapse` verb still exists for compatibility, but the
daemon never sends it). When you press **GO** on a session, the daemon tries to
bring you to it:

1. **Best — the PTY wrapper's own terminal.** For wrapped sessions the daemon
   connects to the session's per-session control socket and sends the single
   verb `focus`; the wrapper raises + un-minimizes + activates *its own*
   terminal window. The wrapper replies in two stages — `go` on receipt
   (liveness), `ok` after the window op completed — and the daemon serializes
   focus work on a side thread, so consecutive GO presses raise windows in
   press order (a press still queued when a newer one arrives is dropped —
   last wins).

2. **Primary — VS Code deep link.** The daemon runs the macOS `open` command
   against a URI built from a configurable `FOCUS_URI_TEMPLATE` constant,
   formatted with the session id. Per the VERIFIED FACTS, the documented URI
   handler is:

   ```
   vscode://anthropic.claude-code/open?session={session_id}&prompt={prompt}
   ```

   This opens/links a Claude Code chat tab for the given session id. Keeping it
   behind a single constant means if the precise URI changes, it is a one-line
   fix.

3. **Fallback — raise the workspace window (always implemented).** When the
   session id is unknown/stale, or the primary `open` returns nonzero, the daemon
   raises VS Code for the session's workspace folder:
   `open -a "Visual Studio Code" <cwd>` (or `code <cwd>` if the CLI is on PATH),
   optionally activating "Visual Studio Code" via AppleScript.

A short GO also **acknowledges** the alert (raising the window = seen); a long
GO (~0.5 s, `B|K`) acknowledges *without* any window op.

---

## Robustness summary

- The daemon **keeps the serial port open continuously** to avoid resetting the
  Nano on every message.
- It **auto-detects** the port (`/dev/cu.usbserial*` then `/dev/cu.usbmodem*`)
  and **auto-reconnects** if the device disappears and returns; it never crashes
  on a missing port.
- Work is split across **threads with locks** (socket server, button reader,
  serial maintainer, 1 Hz ticker) — no busy-wait spinning; focus runs on
  serialized side threads so the button reader never blocks.
- The **hook always exits 0** and never blocks Claude; if the daemon/socket is
  down it silently no-ops with a short timeout.
- The **Arduino** tolerates partial/garbled lines, caps its input buffer
  (96 bytes), ignores malformed lines, and debounces buttons (~40 ms,
  immediate-fire). If the daemon goes silent for ~30 s it stops any LED loop
  and shows the **LINK LOST** screen instead of a frozen display.

---

## Limitations

These are deliberate, documented boundaries — not bugs:

- **Retry / resubmit is not supported in the GUI.** Re-running a failed turn is
  not reliably possible from outside the VS Code GUI, so Claude Mate does not
  attempt it. The hardware surfaces an `error` state as a flashing `ERR` frame
  (with the looping `V|ERROR` strobe) so a human can act, but the only button
  action toward a session is **GO** (raise its window).
- **Model/effort strings are best-effort.** The model/effort row is
  scraped from the live TUI by the PTY wrapper and stays empty for hook-only
  sessions or until scraped. The daemon **never fabricates** it; it fits
  whatever it reliably knows into the 21-char row.
- **Account + remaining limit come from the wrapper too.** The account is the
  wrapper's profile name (`default` for ~/.claude); the remaining-limit chip is
  polled from Anthropic's OAuth usage endpoint with the session's own token
  (read-only — refreshing credentials stays claude's job) and shows the more
  depleted of the 5-hour / weekly windows. Hook-only sessions show neither.
- **Focus depends on the deep link (hook-only sessions).** A wrapped session
  raises its own terminal via its control socket. Hook-only sessions use the
  VS Code deep link derived from the session id (VERIFIED FACTS:
  `vscode://anthropic.claude-code/open?session=...`). Per those same facts,
  **no documented URI pattern exists to programmatically focus an existing
  session inside an already-running chat panel** — the handler opens/links a chat
  tab. When the session id is unknown/stale or the deep link returns nonzero, the
  daemon falls back to **raising the VS Code window** for the workspace folder.
  Focus is therefore best-effort and may land you on the workspace rather than
  the precise chat in some cases.
- **No persistence on the device.** The Arduino holds no state; every reset
  relies on the `H` handshake and the daemon re-sending state. If the daemon is
  not running, the firmware shows the **LINK LOST** screen after ~30 s rather
  than stale data.
