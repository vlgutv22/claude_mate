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

The buttons mean the same thing at all times. The Nano build has **three**; the
ESP32-S3 build adds a fourth, and the first three are identical on both:

| Button (left→right) | Short press | Held / double |
|---|---|---|
| **PREV** | selection one step **up** the queue | auto-repeats (400 ms, then 5/s) |
| **GO**   | single: **RAISE** the terminal of the session **shown on the glass** (acknowledging its alert) | **double-click** (≤ 300 ms apart): toggle **FOLLOW** mode. Held ≥ 500 ms: acknowledge **without** raising. |
| **NEXT** | selection one step **down** the queue | auto-repeats (400 ms, then 5/s) |
| **MIRROR** *(4-button devices only)* | open/close a live view of that session's **actual terminal** | — (tap only) |

The 4th button is spent on the one thing three buttons could not do. Acknowledging
and FOLLOW were already reachable through GO's long-press and double-click, so
putting either on a switch would have bought a shortcut; showing the terminal was
not reachable at all. A 3-button device loses nothing — GO keeps all three
gestures — it simply cannot mirror.

**MIRROR mode.** While the view is open the daemon pulls the rendered TUI from
that session's PTY wrapper about once a second and pushes it as `M|` rows, and
**PREV/NEXT scroll the view instead of moving the selection**. That
reinterpretation happens in the DAEMON: the firmware emits the same `B|P`/`B|N`
either way and never learns a mode exists. GO closes the view and raises the real
window — looking at a copy is superseded by having the original. Polling stops
completely when the view closes, so an unwatched fleet costs nothing.

Only **wrapped** sessions can be mirrored: a hook-only session has no PTY for the
daemon to read, and says so rather than showing a blank screen that would look
like an idle session. Rows are **clipped, not reflowed** — a TUI's box drawing and
alignment carry meaning, and rewrapping turns it into noise.

**FOLLOW mode** (toggled by double-clicking GO, or by holding ACK on a
4-button device; shown by a small ► marker by the state row): while on,
PREV/NEXT additionally **raise** the selected session's terminal, ~250 ms after
the selection settles (so holding to scroll never raises the windows it passes
over). Raise ONLY — never ack (ack stays on GO long-press and the ACK button),
never collapse.

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
| `L\|<i>\|<n>\|<name>\|<chip>` | **One saved account**, pushed on every handshake so the device's picker can show the logins you already have. `i==0` resets the list, so a profile you deleted cannot leave a stale row behind. `<chip>` is that account's remaining-limit chip and may be empty: the names are free, the limits cost an HTTPS round trip each, so the daemon sends names immediately and re-sends enriched a moment later rather than blocking the handshake. Which account has headroom is the whole basis of the choice. |
| `G\|2` | **BLE gamepad mode.** The device comes up as an ordinary Bluetooth HID gamepad (four buttons, no axes, physical order) and needs no daemon, no Wi-Fi and no loopback from then on — which is the only way a page served over **https** can be driven by this hardware, since it cannot reach `http://127.0.0.1`. Entering parks the Wi-Fi link (`net.shutdown()`), because the S3 has one 2.4 GHz radio and sharing it costs the link that matters; `G\|0` leaves and gives the radio back. Reachable from the device itself at **SETTINGS → BLE gamepad** — the verb exists so the mode can also be entered, and tested, from the far end of a cable. Verified on hardware: advertises as `Claude Mate` with service `0x1812`, and `deinit` + Wi-Fi restart round-trips cleanly. |
| `G\|1` `G\|0` | **Controller mode** on / off. Sent by the daemon when a browser page takes or releases the grab on the `--web` bridge (see `daemon/webbridge.py`). While on, the device stops acting on its own buttons, reads the four pins raw and emits press/release **edges** (`B\|+P` …), draws a gamepad face **once** instead of per frame, and drops the backlight to a fraction of its normal duty. That last part is not decoration: a full flush is 110 KB over SPI, and doing it once a second to redraw a screen nobody is looking at — on battery, while the Mac has the buttons — is the overuse the mode exists to avoid. The device leaves on `G\|0`, or on a two-second hold of the 4th button if the Mac never sends one (a crashed tab, a killed daemon). An older firmware ignores `G\|` entirely and simply keeps its menu semantics. **The device can also enter this mode by itself**, from **SETTINGS → Game controller**, with no daemon involvement at all — so `G\|1` arriving while it is already in controller mode is a no-op rather than an error, and the daemon must not assume it is the only thing that puts the device there. |
| `P` | Ping / keepalive, sent every ~15 s. The Arduino replies with `K` (NOT `H` — `H` means "I rebooted" and triggers a full resend + LED re-arm, which would restart the blink phase every ping). |

> **Adding a downlink verb? These letters are already taken, and not by this
> table.** On the S3, USB serial lines go to the **config console** first —
> `handleConfigLine()` — and only fall through to the protocol handler if it
> declines them. It owns `?`, `W`, `S`, `T`, `X`, `R`, `Y`, `Z`. A protocol verb
> that collides with one of those is **silently unreachable over USB** while
> working perfectly over TCP, so it survives every test that does not run on a
> cabled device. This is not hypothetical: controller mode first shipped as
> `X|1`, which the console answered with `refusing: send X|WIPE to clear
> config` — one lax compare away from wiping the Wi-Fi credentials and the
> token every time somebody opened the game. `tools/test_controller_mode.py`
> asserts the two sets stay disjoint.

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

The buttons are, left→right, **PREV | GO | NEXT** (plus **ACK** on the
ESP32-S3's 4-button layout). Debounce is ~40 ms immediate-fire (an edge is
accepted and emitted the same tick). PREV/NEXT emit on the **press** edge and
auto-repeat while held (400 ms to start, then one event per 200 ms). GO and ACK
distinguish a short press (emit on release) from a long press (emit once at
500 ms; the release is then swallowed).

| Line  | Meaning |
|-------|---------|
| `H`   | Hello / handshake, sent **once** right after boot/reset. On receiving `H` the daemon **re-sends the full current state** (frame + re-arms the LED loop). |
| `K`   | Keepalive ack — the reply to `P`. The daemon ignores it. |
| `B\|P` | **PREV** pressed (D4) — selection one step up the queue. Repeats while held. |
| `B\|N` | **NEXT** pressed (D3) — selection one step down the queue. Repeats while held. |
| `B\|G` | **GO** short press (D2). The firmware just emits `B\|G` on each short press; the **daemon** disambiguates a single press (after ~300 ms) from a double-click. A single press raises the terminal of the session **shown on the glass** (raise only), acknowledging its alert. A **double-click** (two `B\|G` within ~300 ms) toggles **FOLLOW** mode. |
| `B\|K` | **GO** long press (D2, held ≥ ~500 ms) — acknowledge the shown session's alert WITHOUT raising anything. No-op when nothing is unacknowledged. Also sent by a **short press of the ACK button** on a 4-button device: same verb, same effect, no daemon-side distinction. |
| `B\|F` | Toggle **FOLLOW** mode directly. Identical in effect to a GO double-click, but unambiguous: no 300 ms window to race. Turning it ON raises the shown terminal immediately, exactly as the double-click does. No stock firmware emits this today (the 4th button became MIRROR); it is kept because the daemon accepting it costs nothing and a device with a fifth switch would want it. |
| `B\|M` | **MIRROR** — open/close the terminal view for the session on the glass. Now reached from the ACTIONS sheet rather than directly from the 4th button. |
| `B\|C` | **Send "continue"** — type `continue` into the shown session and press Enter. The reason the ACTIONS sheet exists: a session that hit its 5-hour limit or took an API error is waiting for one word, and this is how you say it without turning back to the laptop. Wrapped sessions only — a hook-only session has no PTY to type into. The daemon relays it as `submit continue` on that session's control socket; the wrapper strips control bytes, so this can never become a remote keyboard. |
| `B\|T` | **New terminal** — open one in the shown session's directory and focus it. `open -a Terminal <cwd>`, which both opens and fronts, so no separate raise is needed. |
| `B\|A<i>` | **Switch to saved account `<i>`** — continue the shown conversation on one of the logins you already have. Switching does **not** mean signing in again: each is a profile directory under `~/.claude-accounts` holding its own credentials. `<i>` indexes the `L\|` list the daemon pushed, sorted, so both ends agree which name row 3 was — a drift there would switch you to the wrong account. The daemon sends `switch <name>` to the wrapper, which owns the claude process and so is the only thing that can replace it: it copies the transcript into that profile and re-execs with `--resume`. |
| `B\|+P` `B\|-P` `B\|+G` `B\|-G` `B\|+N` `B\|-N` `B\|+M` `B\|-M` | Press / release **edges**, emitted **only in controller mode** (see `G\|` below). Debounce drops to 8 ms and there is no auto-repeat and no deferred tap, because a gamepad is asked a different question than a menu is: not "was it pressed" but "is it held down *this frame*". The ordinary verbs cannot answer it — 40 ms of debounce, then 400 ms before the first repeat, then one event per 200 ms, is a held direction that moves once, stalls for most of a second, then stutters. A daemon that does not know these codes must **ignore** them rather than treat `+` as the verb; acting on both edges would move the selection twice per press. |
| `O\|<KEY>\|<value>` | A device-set **option**. Unknown keys are ignored, so an older daemon and a newer firmware interoperate in both directions — the same rule the `B\|` verbs follow. Currently only `O\|SND\|0` / `O\|SND\|1`: mute or unmute the **macOS** alert sound. It crosses the link because it is the one device setting whose effect happens on the Mac — the device has no speaker, so a mute reached for on the device has to travel. Re-sent on every connect, since the daemon keeps no per-device state to remember it in. |

### 1c. Daemon → Arduino: the MIRROR view

Sent only while the view is open. Rows are pre-rendered and clipped to
**52 columns**, `17` rows, exactly like `F|` — the firmware never decides what
fits.

| Line | Meaning |
|------|---------|
| `M\|T\|<title>` | Header: the mirrored session's name and state. |
| `M\|<n>\|<text>` | Row `n` (0-based, `n < 17`). Text is the rest of the line VERBATIM; tabs are expanded, control bytes become spaces, and any literal `\|` is replaced with `¦` so it cannot be mistaken for a field separator. |
| `M\|END` | The full set has landed — draw it. Buffering until `END` means the view updates in ONE flush and never tears mid-refresh. |
| `M\|OFF` | Close the view; the daemon then re-sends the normal `F\|` frame. |

The daemon fetches the rows by sending `screen` to the session's PTY-wrapper
control socket (the same socket used for `focus`). The wrapper replies
length-prefixed — `SCREEN <n>`, then `n` rows, then `ok` — rather than using a
sentinel, because terminal output can contain any line, **including one that is
exactly `ok`**, which would truncate the mirror at an arbitrary point. A wrapper
predating the command answers a bare `ok`, which reads as "no preview".

### 1d. The wireless handshake (TCP devices only)

Before any of the above flows, a TCP device proves it knows the shared token:

| Direction | Line | Meaning |
|---|---|---|
| daemon → device | `C\|<nonce>` | 32 hex chars, fresh per connection |
| device → daemon | `A\|<mac>` | hex HMAC-SHA256(token, nonce) |
| device → daemon | `A\|NOTOKEN` | *"I have no token configured"* — see below |
| daemon → device | `A\|OK` | authenticated; the protocol above begins |
| daemon → device | `A\|NO` | rejected; the socket closes |

The token never crosses the wire and the nonce is fresh per connection, so a
sniffed handshake is worthless.

`A|NOTOKEN` exists because a device with no token used to simply hang up, which
reached the daemon as `bad handshake None` — indistinguishable from a crash, a
truncated read or a network fault, when the device knew the exact answer all
along. A cleared token is the commonest wireless failure there is (it is what
the setup portal used to cause), so it gets a line of its own and a daemon
message that says what to do about it. A daemon too old to recognise it reports
a rejected token, which is still more use than silence.

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
| `Stop`                 | `done` — or `working`, see below |
| `StopFailure`          | `error`        |

> **A `Stop` with work still in flight reports `working`, not `done`.** The
> `Stop` payload carries `background_tasks[]` (background work that is running
> or pending: dynamic workflows, background agents, backgrounded shells,
> monitors, MCP tasks) and `session_crons[]` (scheduled tasks that will wake the
> session later). Each `background_tasks[]` entry carries its own `status`
> (`running`, `pending`, …); entries in a **terminal** status are already over
> and are not counted, because the later `Stop` that would correct the downgrade
> may never arrive. An entry with no `status` at all *is* counted, so a build
> that omits the field behaves as before. When in-flight work remains — or a
> cron is a **one-shot** wakeup (`recurring: false`), i.e. this turn continuing later —
> the hook reports `working`, and the finish is reported by the next `Stop` that
> lands with nothing in flight. Recurring crons are not counted: a session that
> merely owns a daily schedule is finished for now, and suppressing its DONE
> forever would hide it from the device. Both fields are optional, so a Claude
> Code build that omits them behaves exactly as before.

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
| `working` | `UserPromptSubmit` fired — a turn is in progress. Also a turn that **ended with background work still in flight** (hook: `Stop` + `background_tasks`; PTY wrapper: the background-work tells on screen). |
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
