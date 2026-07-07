# Claude Mate — Protocol (source of truth)

This document is the **authoritative** description of every wire format in Claude
Mate. The daemon, the firmware, and the hook MUST agree with this file verbatim.
If code and this document disagree, the document wins until it is updated.

There are three interfaces:

1. **Serial protocol** — daemon ⇄ Arduino (USB CDC serial).
2. **Socket message** — hook / PTY wrapper → daemon (Unix domain socket).
3. **Session state model** — the triage queue the daemon keeps.

---

## 0. The interface model (one screen, one queue, three buttons)

The daemon keeps ONE **stable-ordered** list of sessions (alphabetical, so tabs
never shuffle) and renders ONE screen: the *selected* session. The firmware is
a dumb renderer: it holds exactly one pre-composed frame (four size-1 rows) and
draws it. All ordering, selection, truncation and layout live in the daemon.

```
+---------------------+
|api-server           |   r0: session name — flashes (inverts ~2.5 Hz) while
|WAIT  0:42       work|   r1: state tag + time-in-state    its alert is unacked
|Opus 4.8 xhigh  5h82%|       + the ACCOUNT the session runs as (right-aligned)
|2/6 E B W D I        |   r2: model + effort + REMAINING-LIMIT chip (right)
+---------------------+   r3: queue position + one status LETTER per session;
                              the active tab's letter sits in a filled rectangle
```

The **account** (`work`, `default`, …) is which Claude login the session runs
as (the PTY wrapper's `--account` profile). The **remaining-limit chip** shows
how much of that account's plan limit is left, for whichever window is more
depleted: `5h82%` = 82% of the 5-hour window remaining, `wk31%` = 31% of the
week left. Both come from the PTY wrapper and are empty (row right edges stay
blank) for hook-driven sessions.

The **active tab** (the session shown above) has its fleet letter drawn in a
**wide filled rectangle** in r3 — a lit block with the letter centred and
knocked out — so you can see at a glance which tab is on screen. Any tab with
an **unacknowledged alert** has its letter **blinking** in the strip, so you can
tell acked from unacked without hunting.

The three buttons mean the same thing at all times:

| Button (left→right) | Short press | Held / double |
|---|---|---|
| **PREV** | selection one step **up** the queue | auto-repeats (400 ms, then 5/s) |
| **GO**   | single: **RAISE** the terminal of the session **shown on the glass** (acknowledging its alert) | **double-click** (≤ 300 ms apart): toggle **FOLLOW** mode. Held ≥ 500 ms: acknowledge **without** raising. |
| **NEXT** | selection one step **down** the queue | auto-repeats (400 ms, then 5/s) |

**FOLLOW mode** (toggled by double-clicking GO; shown by a small ► marker by
the state row): while on, PREV/NEXT additionally **raise** the selected
session's terminal, ~250 ms after the selection settles (so holding to scroll
never raises the windows it passes over). Raise ONLY — never ack (ack stays on
GO long-press), never collapse.

**Window contract:** navigation touches macOS windows ONLY in FOLLOW mode, and
then only to **raise/activate** the selected terminal — the daemon never
collapses, resizes, or miniaturizes anything.

**WYSIWYG:** GO/ACK act on **exactly the session whose frame is on the glass** —
never a freshly recomputed most-urgent alert. So a press can only ever raise the
terminal whose name the user is actually looking at.

**No auto-switch, ever:** the selection is **sticky** — the screen only
changes subject when you navigate (PREV/NEXT), and a GO/ACK **stays on the tab
it acted on**. An alert on another tab announces itself through the LED and its
blinking fleet letter, never by stealing the screen. The tab **order** never
changes either.

---

## 1. Serial protocol (daemon ⇄ Arduino)

**Transport:** USB CDC serial.
**Baud:** `115200`.
**Framing:** `8N1` (8 data bits, no parity, 1 stop bit).
**Encoding:** ASCII lines, each terminated by a **single newline** (`\n`).
**Field separator:** the `|` character.

Lines that do not parse MUST be ignored by the receiver (the Arduino caps its
input buffer at 96 bytes and drops malformed/oversized lines).

### 1a. Daemon → Arduino

| Line | Meaning |
|------|---------|
| `F\|<flags>\|<sel>\|<r0>\|<r1>\|<r2>\|<r3>` | **The whole screen**, pre-rendered as four size-1 rows. See field table below. At least 7 fields; `r3` is the **last** field and may itself contain `\|`. |
| `V\|<kind>` | LED alert control (indication LED only; never touches the OLED). `<kind>` is `START`, `INPUT`, `DONE`, `ERROR`, or `OFF`. See **LED** below. |
| `P` | Ping / keepalive, sent every ~15 s. The Arduino replies with `K` (NOT `H` — `H` means "I rebooted" and triggers a full resend + LED re-arm, which would restart the blink phase every ping). |

**`F` line fields** — each row is ≤ 21 chars (size-1), drawn top to bottom:

| Field   | Description |
|---------|-------------|
| `flags` | Bitfield. **bit0**: invert **row 0** (the name) at ~2.5 Hz (an unacknowledged alert is on screen). **bit1**: FOLLOW mode is on (draw a ► marker by the state row). |
| `sel`   | The character column **within `r3`** of the active tab's fleet letter (`-1` = none). The firmware fills a **wide** rectangle (~11 px) **centred** on that letter — a lit block with the letter knocked out. |
| `r0` (name) | Session name. The daemon truncates to the row width and, when two long sibling names collide, disambiguates with a middle squeeze (first 9 + `~` + last 10, e.g. `webapp-ba~ervice-one`). |
| `r1` (state) | `TAG  time` — the 4-char state tag (`ERR`/`WAIT`/`DONE`/`WORK`/`IDLE`) and the time in that state (mm:ss, or h:mm past an hour) — plus the session's **account** right-aligned (≥ 2-space gap, truncated to fit; omitted when unknown). |
| `r2` (meta) | Best-fit `model effort`, plus the **remaining-limit chip** (e.g. `5h82%`) right-aligned (≥ 2-space gap); the model+effort best-fit degrades into the room the chip leaves. Empty for hook-driven sessions with no scraped metadata. |
| `r3` (fleet) | `pos/total ` + one status **letter** per session in the stable (alphabetical) order, **space-separated**: `E` error, `B` waiting (blocked), `W` working, `D` done, `I` idle. An **unacknowledged alert's letter is sent LOWERCASE** — the firmware draws it uppercase but **blinks** it, so you can see which tabs still need acknowledging (they stop blinking as you ack them). When the strip does not fit, it is cut with a trailing `+`. `r3` is the last field (the firmware stops tokenizing at the 6th `\|` and takes the rest verbatim). |

The daemon sends an `F` line whenever the rendered bytes change: immediately on
any state change or button, and ~1/s while a displayed time ticks. Identical
frames are not re-sent (except on handshake).

With no sessions the daemon sends `F|0|-1|MATE|no sessions||`.

#### LED — `V|<kind>`

The daemon owns the LED policy; the firmware just plays the pattern on D8. The
pattern is always the class of the **worst unacknowledged alert** across all
sessions (`ERROR` > `INPUT` > `DONE`), so the LED loops exactly while something
needs the human:

| `<kind>` | When the daemon sends it | Firmware pattern | Repeat |
|----------|--------------------------|------------------|--------|
| `START`  | A job (re)started, and nothing else needs you. | one long **1 s** blink, then dark. | one-shot |
| `INPUT`  | Worst unacked alert is `waiting` (Claude needs input). | aggressive even blink (~2.8 Hz). | **loops** until `V\|OFF` |
| `ERROR`  | Worst unacked alert is `error`. | super-aggressive fast strobe (~7 Hz). | **loops** until `V\|OFF` |
| `DONE`   | Worst unacked alert is `done` (finished turn). | cascade — 4 quick blinks, then a pause. | **loops** until `V\|OFF` |
| `OFF`    | No unacknowledged alerts remain. | LED off now. | — |

The daemon (re)sends the loop `<kind>` only when the desired loop *changes*, so
the link is not spammed. As a failsafe the firmware stops any loop **and shows
the LINK LOST screen** if the daemon goes fully silent for ~30 s (it pings `P`
every 15 s and refreshes `F` frames ~1/s, so this only trips when the daemon
has died). `OFF` (alias `STOP`) darkens the LED immediately.

#### Firmware-local screens

Two states the firmware draws on its own (the daemon never sends them):

* **Splash** — booted, no `F` frame yet: `MATE` / `starting...`.
* **LINK LOST** — no serial byte for ~30 s: `NO LINK` / `waiting for daemon`.
  Clears on the next complete parsed line.

#### Press feedback blip

On every accepted button edge the firmware inverts the whole panel for ~80 ms
(SSD1306 `0xA7`/`0xA6`) — instant "the device heard you" feedback with zero
framebuffer cost. Best-effort: the command is only sent while no chunked frame
transfer is open.

### 1b. Arduino → Daemon

The three buttons are, left→right, **PREV | GO | NEXT**. Debounce is ~40 ms
immediate-fire (an edge is accepted and emitted the same tick). PREV/NEXT emit
on the **press** edge and auto-repeat while held (400 ms to start, then one
event per 200 ms). GO distinguishes a short press (emit on release) from a long
press (emit once at 500 ms; the release is then swallowed).

| Line  | Meaning |
|-------|---------|
| `H`   | Hello / handshake, sent **once** right after boot/reset. On receiving `H` the daemon **re-sends the full current state** (frame + re-arms the LED loop). |
| `K`   | Keepalive ack — the reply to `P`. The daemon ignores it. |
| `B\|P` | **PREV** pressed (D4) — selection one step up the queue. Repeats while held. |
| `B\|N` | **NEXT** pressed (D3) — selection one step down the queue. Repeats while held. |
| `B\|G` | **GO** short press (D2). The firmware just emits `B\|G` on each short press; the **daemon** disambiguates a single press (after ~300 ms) from a double-click. A single press raises the terminal of the session **shown on the glass** (raise only), acknowledging its alert. A **double-click** (two `B\|G` within ~300 ms) toggles **FOLLOW** mode. |
| `B\|K` | **GO** long press (D2, held ≥ ~500 ms) — acknowledge the shown session's alert WITHOUT raising anything. No-op when nothing is unacknowledged. |

**Reset note:** opening the USB serial port resets the Nano (~1.5 s). The `H`
handshake plus the daemon re-sending state on `H` is exactly what makes the
display recover correctly after every reconnect. The daemon also forgets its
LED-loop tracker on `H`, so an unacknowledged alert's loop is re-armed after
every replug.

---

## 2. Socket message (hook / PTY wrapper → daemon)

**Transport:** Unix domain socket at `/tmp/claude-mate.sock` (override with
`CLAUDE_MATE_SOCK`).
**Framing:** one **newline-terminated** line per message.

```
<state>|<session_id>|<name>[|<ctrl_sock>|<model>|<effort>|<account>|<limit>]
```

| Field        | Description |
|--------------|-------------|
| `state`      | One of: `working`, `waiting`, `done`, `error` (or `idle`/`end` from the PTY wrapper). |
| `session_id` | The Claude Code session id. **MAY be empty** if not provided. |
| `name`       | Basename of `cwd`. |
| `ctrl_sock`  | (PTY wrapper) per-session control socket GO's `focus` connects to. Empty for hooks. |
| `model`      | (PTY wrapper) model in use, e.g. `Opus 4.8`. Empty until scraped / for hooks. |
| `effort`     | (PTY wrapper) effort level, e.g. `xhigh`. Empty until scraped / for hooks. |
| `account`    | (PTY wrapper) account/profile the session runs as, e.g. `work` or `default`. Empty for hooks. |
| `limit`      | (PTY wrapper) remaining-limit chip for that account, e.g. `5h82%` (82% of the 5-hour window left) or `wk31%` (31% of the week left) — whichever window is more depleted. Refreshed every `CLAUDE_MATE_USAGE_POLL_S` (default 120 s) from Anthropic's OAuth usage endpoint; empty until the first successful poll / for hooks. |

The hook path sends only the first three fields; the PTY wrapper appends the
control socket, (once scraped) the model + effort, and (once known) the
account + remaining limit. All trailing fields are optional — the daemon
defaults missing ones to empty.

The hook is **fire-and-forget**: it connects with a short timeout, writes one
line, and exits **0** regardless of outcome. If the daemon/socket is down it
**silently no-ops**. The hook must never block or fail a Claude turn.

### Wrapper control socket (daemon → wrapper)

The daemon connects to a session's `ctrl_sock` and sends **only one verb**:

* `focus` — raise + un-minimize + activate that session's terminal window.

The wrapper replies in two stages — `go` on receipt (liveness), `ok` after the
window op completed — so consecutive focuses apply in press order. Pre-ack
wrappers close the socket immediately (EOF), degrading to fire-and-forget.
The `collapse` verb still exists in the wrapper for compatibility but the
daemon **never sends it**.

### Hook event → state mapping

| Claude Code hook event | Socket `state` |
|------------------------|----------------|
| `UserPromptSubmit`     | `working`      |
| `Notification`         | `waiting`      |
| `Stop`                 | `done`         |
| `StopFailure`          | `error`        |

> **No `SessionEnd` hook is installed.** The daemon's `idle` state is **not**
> driven by any socket message — it is reached purely via the
> done-until-acknowledged model and TTL pruning (see §3).

---

## 3. Session state model (kept by the daemon)

The daemon keeps a dictionary of sessions, **keyed by `session_id`** (or by
`name` when the id is empty).

### Per-session fields

| Field            | Description |
|------------------|-------------|
| `name`           | cwd basename. |
| `state`          | Current state (see below). |
| `sid`            | Session id (`session_id`). |
| `cwd`            | Working directory, if known (used by the FOCUS fallback). |
| `last_update_ts` | Timestamp of the last update for this session. |
| `state_since`    | When the current state began — the displayed time is always *time in state* (for `working` that IS the live turn runtime; for an alert it is how long it has been waiting on the human). |
| `model`/`effort` | Best-effort strings scraped by the PTY wrapper. |
| `account`/`limit`| The account the session runs as + its remaining-limit chip (PTY wrapper; sticky once seen). |
| `focus_ctrl`     | The wrapper control socket path, if any. |
| `acked`          | Has the human seen this alert? |

### States

| State     | Entered when |
|-----------|--------------|
| `working` | `UserPromptSubmit` fired — a turn is in progress. |
| `waiting` | `Notification` fired — needs permission / Claude asked something. |
| `error`   | `StopFailure` fired — turn ended on an API error (5xx / overloaded / timeout). |
| `done`    | `Stop` fired — turn completed OK. Also `working` → `idle` becomes `done` (finished but not yet acknowledged) and STAYS `done` until acknowledged. |
| `idle`    | Acknowledging a `done` session, or inactivity/TTL pruning. **No hook/socket message sets `idle` directly into the model.** |

### Tab order (stable) vs. urgency (separate)

The display / navigation order is **stable — alphabetical by name** (then the
session key as a deterministic tiebreak). Tabs keep their position as their
states change; they never shuffle under the user.

**Urgency** is computed separately and drives only two things, never the tab
order:

```
worst UNACKNOWLEDGED alert  (error > waiting > done, oldest first)
```

* the **LED** loop class, and
* which **fleet letters blink** (unacked alerts are sent lowercase).

It never moves the selection: the shown tab is sticky.

### Ack lifecycle

An alert is born on the transition into `error`/`waiting`/`done`. It dies in
exactly four ways — nothing else removes one, so none can be lost silently:

1. **`B|G` GO** — acknowledged + the window raised.
2. **`B|K` ACK** — acknowledged, no window op.
3. **Auto-resolve** — the session leaves the alert class on its own (the user
   answered in the terminal → `working`; a new turn started).
4. **TTL prune** — `done` sessions drop after 120 s without updates; anything
   drops after 600 s.

Acknowledging a `done` session turns it `idle`. **Flap suppression:** a session
re-entering the SAME alert class within 5 s of being acknowledged stays
acknowledged (a bouncing detector cannot re-fire the LED the user just
silenced).

### Selection rules

* Tab order is **stable (alphabetical)** — tabs never change position as their
  states change. Selection is tracked by session **key**.
* The selection is **sticky**: the screen never changes subject on its own.
  Only when the selected session ends does it fall back to the first tab.
* GO/ACK act on the session **currently shown** (WYSIWYG) and **stay** on it —
  the device never auto-switches tabs.
* PREV/NEXT wrap around the ends.
* **FOLLOW** (double-click GO) makes PREV/NEXT also raise the selected terminal
  after the selection settles (~250 ms); double-click again to turn it off.
