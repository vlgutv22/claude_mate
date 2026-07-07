# Changelog

All notable changes to Claude Mate are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Claude Mate is pre-1.0, so entries are grouped by **date** rather than a
semantic version until the first tagged release; see
[`packaging/VERSION`](packaging/VERSION) for the current package version. Older
entries are kept as-written and describe the design **as it was on that date** —
they are the project's history, not the current behavior (which the
[README](README.md) always reflects).

## [Unreleased]

### 2026-07-07 — Sticky selection

- **The selection is now fully sticky.** The ~10 s idle auto-surface is gone:
  the screen **never** switches tabs on its own. Alerts elsewhere announce
  themselves via the LED and their blinking fleet letters; you browse with
  PREV/NEXT.
- **FOLLOW's ► marker no longer overdraws the account name.** While FOLLOW is
  on, the daemon keeps the last two columns of the state row blank (the
  right-aligned account shifts left), so the play triangle has its own space.

### 2026-07-06 — Account profiles + on-device account & remaining-limit

- **Account profiles.** The PTY wrapper can run different terminals under
  different Claude accounts by pointing each session at its own
  `CLAUDE_CONFIG_DIR`. Every subdirectory of `~/.claude-accounts` is a profile;
  an interactive start shows an opt-in picker listing each profile with the
  email logged into it. `--account <name>` / `CLAUDE_MATE_ACCOUNT` selects one
  non-interactively; an already-exported `CLAUDE_CONFIG_DIR` always wins. With
  no profile dirs, nothing changes.
- **Device shows the account + remaining limit.** Each wrapped session reports
  which account it runs as (right-aligned on the state row) and how much of that
  account's plan limit is left as a chip on the model+effort row (`5h82%` = 82%
  of the 5-hour window, `wk31%` = 31% of the week — the tighter one shows). The
  wrapper reads the session's own OAuth token and polls Anthropic's usage
  endpoint read-only; it never refreshes or rewrites credentials.

### 2026-07-05 — Interface rewrite: one screen, one queue, three buttons

The device interface was rewritten from scratch — **one screen, one queue,
three buttons**:

- **UI modes are gone.** No SCROLL/LIST toggle, no carousel, no detail card, no
  mode long-press. The daemon keeps ONE **stable, alphabetically-ordered** triage
  queue (tabs never shuffle; urgency is tracked separately, for the LED + idle
  auto-surface only) and pre-renders ONE screen — four size-1 rows (name ·
  state+time+account · model+effort+remaining-limit · position+fleet strip);
  the firmware is a dumb one-frame renderer.
- **Buttons are PREV / GO / NEXT everywhere.** GO short = acknowledge + raise
  the shown window (WYSIWYG); GO **double-click** = toggle FOLLOW mode; GO long
  = acknowledge only; PREV/NEXT auto-repeat while held. A GO/ACK **stays on the
  tab** it acted on (no auto-switch); after ~10 s idle the display auto-surfaces
  the most-urgent unacknowledged alert at its stable position (else the first
  tab). _(The idle auto-surface was later removed on 2026-07-07.)_
- **Navigation never touches windows** (except in FOLLOW mode, which raises
  only). The old terminal-follow preview (collapse/expand on every navigation)
  is gone; the daemon never sends `collapse`.
- **Serial protocol simplified.** Down: `F|<flags>|<sel>|<r0>|<r1>|<r2>|<r3>` +
  `V|<kind>` + `P`. Up: `H` + `B|P` / `B|N` / `B|G` / `B|K`. The old
  `D|` / `S|` / `T|` / `I` lines and `B|1`..`B|5` are gone.
- **Firmware additions:** a boot splash, a **LINK LOST** screen after ~30 s of
  daemon silence, and an ~80 ms whole-panel invert blip on every accepted
  press. LED semantics are unchanged (START one-shot; INPUT / ERROR / DONE loop
  until acknowledged; OFF).
- The hook, the PTY wrapper, the socket protocol, and the wiring are unchanged
  (button *roles* renamed: D4 MODE → PREV, D2 SUBMIT → GO).

### 2026-06-29 — OLED + PTY wrapper + acknowledge model

A big iteration day. Highlights:

- **Hardware redesign:** dropped the stepper-driven status wheel; the device is
  now a **0.91" 128×32 OLED + micro vibration motor + 3 buttons**. The OLED shows
  a per-session status card (state + live timer + acknowledge dot). _(The
  vibration motor was later replaced by an indication LED on D8.)_
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

[Unreleased]: https://github.com/vlgutv22/claude_mate/commits/main
