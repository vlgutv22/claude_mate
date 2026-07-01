# Claude Mate — Protocol (source of truth)

This document is the **authoritative** description of every wire format in Claude
Mate. The daemon, the firmware, and the hook MUST agree with this file verbatim.
If code and this document disagree, the document wins until it is updated.

There are three interfaces:

1. **Serial protocol** — daemon ⇄ Arduino (USB CDC serial).
2. **Socket message** — hook → daemon (Unix domain socket).
3. **Session state model** — the in-memory model the daemon keeps.

---

## 1. Serial protocol (daemon ⇄ Arduino)

**Transport:** USB CDC serial.
**Baud:** `115200`.
**Framing:** `8N1` (8 data bits, no parity, 1 stop bit).
**Encoding:** ASCII lines, each terminated by a **single newline** (`\n`).
**Field separator:** the `|` character.

Lines that do not parse MUST be ignored by the receiver (the Arduino caps its
input buffer and drops malformed/oversized lines).

### 1a. Daemon → Arduino

| Line                                                  | Meaning |
|-------------------------------------------------------|---------|
| `D\|<word>`                                           | Set the status word. `<word>` is one of `FREE`, `WIP`, `BLOCKED`, `WTF`. The Arduino draws the big word on the OLED. **Visual only** — the word never buzzes on its own; all haptics come via `V\|`. |
| `S\|<idx>\|<total>\|<name>\|<state>\|<runtime>\|<limit>\|<ack>\|<model>\|<effort>` | Show one session card (the carousel step). See field table below. `ack`/`model`/`effort` are optional trailing fields — older daemons omit them and the firmware copes. |
| `V\|<kind>`                                           | Haptic control (motor only; never touches the OLED). `<kind>` is `START`, `INPUT`, `DONE`, `ERROR`, or `OFF`. See **Haptics** below. |
| `T\|<total>\|<sel>\|<row>\|<row>\|…`                  | **LIST-mode** frame (sent only in LIST mode). `<total>` = total tab count, `<sel>` = 0-based index of the highlighted tab (drives the scrollbar). Then up to **4** windowed rows, each `<name>;<status>;<hl>` where `status` is a short label shown in a left column (`WIP` working / `WAIT` waiting / `ERR` error / `DONE` done / `IDLE` idle) and `hl` is `1` for the highlighted row. Names are capped (`LIST_NAME_MAX`) so the whole line stays under the firmware's 96-char limit. |
| `I`                                                   | Idle screen — no active sessions. The daemon also sends `D\|FREE` alongside it. |
| `P`                                                   | Ping / keepalive. The Arduino MAY ignore it, or reply with `H`. |

> The old `L|<color>` traffic-light command and the stepper status wheel have
> both been **removed**. The overall indicator is now the OLED word (drawn from
> `D|<word>`, visual only); the vibration motor is driven **independently** by the
> daemon per session via `V|<kind>` (see **Haptics**).

#### Haptics — `V|<kind>`

The daemon owns **all** haptics and decides, per session, when to buzz; the
firmware just plays the pattern it is told. Amplitude (PWM duty) stays well below
full power — urgency reads as **rhythm**, not raw force.

| `<kind>` | When the daemon sends it | Firmware pattern | Repeat |
|----------|--------------------------|------------------|--------|
| `START`  | A job (re)started, and nothing else needs you. | 3 gentle 0.3 s pulses. | one-shot |
| `INPUT`  | A session started `waiting` (Claude needs input). | soft double-tap. | daemon re-taps every ~10 s until focused |
| `DONE`   | A turn finished (`done`, unacknowledged). | 5×0.2 s "heartbeat", gaps 0.2/0.4 s, then a rest. | **loops** until `V\|OFF` |
| `ERROR`  | A turn ended on an API error (`error`). | 0.4 s on / 0.2 s off. | **loops** until `V\|OFF` |
| `OFF`    | The alert was acknowledged (FOCUS) or cleared. | stops the motor now. | — |

`DONE` and `ERROR` are **continuous loops**: the firmware repeats the pattern on
its own until it receives `V|OFF` (sent on FOCUS/clear). The daemon (re)sends the
loop `<kind>` only when the desired loop *changes*, so the link is not spammed. As
a failsafe the firmware stops any loop if the daemon goes fully silent for ~30 s
(it pings `P` every 15 s and streams `S` cards ~1/s, so this only trips when the
daemon has died). `OFF` (alias `STOP`) silences the motor immediately.

**`S` line fields:**

| Field     | Description |
|-----------|-------------|
| `idx`     | 1-based position in the carousel. |
| `total`   | Total number of sessions currently known. |
| `name`    | Session name (cwd basename), **up to 20 chars** (`NAME_MAX`) — the **daemon truncates** before sending; the firmware buffer holds 20 + NUL. |
| `state`   | One of: `working`, `waiting`, `error`, `done`, `idle`. |
| `runtime` | Like `03:21` (mm:ss) or `1:04` (h:mm). |
| `limit`   | Short string like `71%`, or `-` when unknown. |
| `ack`     | `1` acknowledged / `0` not (optional). Alert states draw a top-left dot: filled+blinking = unacknowledged, hollow = acknowledged. |
| `model`   | Model in use, e.g. `Opus 4.8` (optional; PTY-wrapper sessions only, ≤12 chars). |
| `effort`  | Effort level, e.g. `xhigh` (optional; PTY-wrapper sessions only, ≤10 chars). |

When `model`/`effort` are present the firmware draws a small `model · effort`
row between the name and the big status word; when both are empty it keeps the
original two-line card. They are scraped from Claude's TUI by the PTY wrapper
(welcome-box header + the `◉ <effort>` pill) and cached per session.

Example:

```
S|1|3|claude-mat|error|03:21|-|0|Opus 4.8|xhigh
```

(idx 1 of 3, name `claude-mat`, state `error`, runtime `03:21`, no limit,
unacknowledged, model `Opus 4.8`, effort `xhigh`.)

### 1b. Arduino → Daemon

The three buttons are, left→right, **MODE | SUBMIT | NEXT**. Debounce is ~40 ms
(snappy taps); the MODE button additionally distinguishes a short press from a
long press (held ≥ ~500 ms).

| Line       | Meaning |
|------------|---------|
| `H`        | Hello / handshake, sent **once** right after boot/reset (and as the reply to `P`). On receiving `H` the daemon **re-sends the full current state** (word + current display + re-arms haptics). |
| `B\|1`     | **SUBMIT** pressed (D2) — focus/proceed to the selected tab (the current card in SCROLL mode, the highlighted row in LIST mode). |
| `B\|2`     | **NEXT** pressed (D3) — SCROLL: next card. LIST: move the highlight **down**. |
| `B\|3`     | **MODE** short-press (D4) — SCROLL: previous card. LIST: move the highlight **up**. |
| `B\|4`     | **MODE** long-press (D4, held ≥ ~500 ms) — **toggle** the UI mode (SCROLL ⇄ LIST). Fires once when the hold crosses the threshold; the release is then swallowed. |

**UI modes.** The daemon owns the mode. **SCROLL** is the carousel (auto-surface
the most-urgent tab + browse one card at a time via `S` frames). **LIST** shows a
scrolling list of *all* tabs via `T` frames; NEXT / MODE-short move the highlight,
SUBMIT focuses it. Haptics (`V|`) are unaffected by the mode.

**Reset note:** opening the USB serial port resets the Nano (~1.5 s). The `H`
handshake plus the daemon re-sending state on `H` is exactly what makes the
display recover correctly after every reconnect. After a reset the firmware
re-draws whatever word + display frame the daemon re-sends, and the daemon
re-arms any active haptic loop.

---

## 2. Socket message (hook → daemon)

**Transport:** Unix domain socket at `/tmp/claude-mate.sock` (override with
`CLAUDE_MATE_SOCK`).
**Framing:** one **newline-terminated** line per message.

```
<state>|<session_id>|<name>[|<ctrl_sock>|<model>|<effort>]
```

| Field        | Description |
|--------------|-------------|
| `state`      | One of: `working`, `waiting`, `done`, `error` (or `idle`/`end` from the PTY wrapper). |
| `session_id` | The Claude Code session id. **MAY be empty** if not provided. |
| `name`       | Basename of `cwd`. |
| `ctrl_sock`  | (PTY wrapper) per-session control socket FOCUS connects to. Empty for hooks. |
| `model`      | (PTY wrapper) model in use, e.g. `Opus 4.8`. Empty until scraped / for hooks. |
| `effort`     | (PTY wrapper) effort level, e.g. `xhigh`. Empty until scraped / for hooks. |

The hook path sends only the first three fields; the PTY wrapper appends the
control socket and (once scraped) the model + effort. All trailing fields are
optional — the daemon defaults missing ones to empty.

The hook is **fire-and-forget**: it connects with a short timeout, writes one
line, and exits **0** regardless of outcome. If the daemon/socket is down it
**silently no-ops**. The hook must never block or fail a Claude turn.

### Hook event → state mapping

The hook translates Claude Code hook events into the four socket states:

| Claude Code hook event | Socket `state` |
|------------------------|----------------|
| `UserPromptSubmit`     | `working`      |
| `Notification`         | `waiting`      |
| `Stop`                 | `done`         |
| `StopFailure`          | `error`        |

> **No `SessionEnd` hook is installed.** The canonical
> `hooks/settings.snippet.json` only wires the four events above. The daemon's
> `idle` state is **not** driven by any socket message — it is reached purely via
> inactivity/TTL pruning (see §3). A session never transitions to `idle` from a
> hook.

Notes from VERIFIED FACTS:

- `StopFailure` is a **distinct** event that fires when a turn ends on an API
  error (5xx, rate-limit 429, overloaded, timeout, `authentication_failed`,
  etc.). It fires **instead of** `Stop`; the two never fire on the same turn.
- Always-present hook stdin fields: `session_id`, `transcript_path`, `cwd`,
  `hook_event_name`. Event-specific fields vary (`error_type` on `StopFailure`,
  `source` on `SessionStart`, `tool_name`/`tool_input` on tool events, etc.).
- All these events fire in the VS Code extension with full parity to the CLI.

---

## 3. Session state model (kept by the daemon)

The daemon keeps a dictionary of sessions, **keyed by `session_id`**. If the
`session_id` is empty, the session is keyed by `name` instead.

### Per-session fields

| Field            | Description |
|------------------|-------------|
| `name`           | cwd basename. |
| `state`          | Current state (see below). |
| `sid`            | Session id (`session_id`). |
| `cwd`            | Working directory, if known (used by the FOCUS fallback). |
| `last_update_ts` | Timestamp of the last update for this session. |
| `started_ts`     | Set when the session **enters `working`**; used to compute live runtime. |
| `limit`          | Best-effort string, default `"-"`. |

### States

| State     | Entered when |
|-----------|--------------|
| `working` | `UserPromptSubmit` fired — a turn is in progress. |
| `waiting` | `Notification` fired — needs permission / Claude asked something. |
| `error`   | `StopFailure` fired — turn ended on an API error (5xx / overloaded / timeout). |
| `done`    | `Stop` fired — turn completed OK. |
| `idle`    | Inactivity only — the default initial state and where stale sessions land via TTL pruning. **No hook/socket message sets `idle`.** |

### Derived display values

- **Runtime:** while `working`, the displayed runtime is `now - started_ts`.
  Otherwise it is the duration of the **last completed turn**.
- **Limit:** best-effort. If rate/usage limits cannot be reliably obtained, show
  `"-"`. The daemon **MUST NOT fabricate** limit numbers — real limit reporting
  is a documented extension point.

### Status-word derivation (recomputed on every change)

The daemon maps the overall session state to one of **four words** and sends
`D|<word>` whenever the word changes. This is the **single source of truth** for
the OLED word and the haptic:

| Word      | Selected when                                                              |
|-----------|---------------------------------------------------------------------------|
| `WTF`     | ANY session in `error` (StopFailure / API 5xx / overloaded / timeout).    |
| `BLOCKED` | ELSE ANY session `waiting` (Claude needs your input) and none errored.    |
| `WIP`     | ELSE ANY session `working` and none blocked/errored.                      |
| `FREE`    | OTHERWISE — all idle/done, or no sessions.                                |

**Priority (strict):** `WTF` > `BLOCKED` > `WIP` > `FREE`. A single `error`
session forces `WTF` even if others are merely `working`.

On a word change the Arduino redraws the big word on the OLED — **visual only**.
The word does **not** buzz. Haptics are driven separately and per session via
`V|<kind>` (see **Haptics** in §1a): the daemon decides when a *specific* tab
started, finished, blocked, or errored, so a single tab's event is felt even when
the aggregate word does not change. Buttons and serial stay responsive throughout.

### Carousel ordering (most urgent first)

```
error  ->  waiting  ->  working  ->  done  ->  idle
```

The daemon emits one `S` line per ~3 s step in this order. With zero sessions it
emits `I` + `D|FREE` instead.
