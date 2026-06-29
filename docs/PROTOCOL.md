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
| `D\|<word>`                                           | Set the status word. `<word>` is one of `FREE`, `WIP`, `BLOCKED`, `WTF`. The Arduino draws the big word on the OLED **and** fires the vibration haptic **when the word changes** (WTF = 3 pulses, BLOCKED = 2 pulses, FREE = 1 short tick, WIP = silent). |
| `S\|<idx>\|<total>\|<name>\|<state>\|<runtime>\|<limit>` | Show one session card (the carousel step). See field table below. |
| `I`                                                   | Idle screen — no active sessions. The daemon also sends `D\|FREE` alongside it. |
| `P`                                                   | Ping / keepalive. The Arduino MAY ignore it, or reply with `H`. |

> The old `L|<color>` traffic-light command and the stepper status wheel have
> both been **removed**. The overall indicator is now the OLED word (drawn from
> `D|<word>`), with the vibration motor buzzing on each word change.

**`S` line fields:**

| Field     | Description |
|-----------|-------------|
| `idx`     | 1-based position in the carousel. |
| `total`   | Total number of sessions currently known. |
| `name`    | Session name (cwd basename), **up to 10 chars** — the **daemon truncates** before sending. |
| `state`   | One of: `working`, `waiting`, `error`, `done`, `idle`. |
| `runtime` | Like `03:21` (mm:ss) or `1:04` (h:mm). |
| `limit`   | Short string like `71%`, or `-` when unknown. |

Example:

```
S|1|3|claude-mat|error|03:21|71%
```

(idx 1 of 3, name truncated to 10 chars, state `error`, runtime `03:21`, limit `71%`.)

### 1b. Arduino → Daemon

| Line       | Meaning |
|------------|---------|
| `H`        | Hello / handshake, sent **once** right after boot/reset (and as the reply to `P`). On receiving `H` the daemon **re-sends the full current state** (word + current card). |
| `B\|1`     | **FOCUS** pressed (D2) — focus the current card's session's VS Code window. |
| `B\|2`     | **NEXT** pressed (D3) — advance the carousel to the next card. |
| `B\|3`     | **PREV** pressed (D4) — step the carousel to the previous card. |

**Reset note:** opening the USB serial port resets the Nano (~1.5 s). The `H`
handshake plus the daemon re-sending state on `H` is exactly what makes the
display recover correctly after every reconnect. After a reset the firmware
re-draws whatever word and card the daemon re-sends.

---

## 2. Socket message (hook → daemon)

**Transport:** Unix domain socket at `/tmp/claude-mate.sock` (override with
`CLAUDE_MATE_SOCK`).
**Framing:** one **newline-terminated** line per message.

```
<state>|<session_id>|<name>
```

| Field        | Description |
|--------------|-------------|
| `state`      | One of: `working`, `waiting`, `done`, `error`. |
| `session_id` | The Claude Code session id. **MAY be empty** if not provided. |
| `name`       | Basename of `cwd`. |

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

On a word change the Arduino redraws the big word on the OLED and fires the
vibration motor as a one-shot alert: **WTF = 3 pulses, BLOCKED = 2 pulses,
FREE = 1 short tick, WIP = silent**. Buttons and serial stay responsive
throughout.

### Carousel ordering (most urgent first)

```
error  ->  waiting  ->  working  ->  done  ->  idle
```

The daemon emits one `S` line per ~3 s step in this order. With zero sessions it
emits `I` + `D|FREE` instead.
