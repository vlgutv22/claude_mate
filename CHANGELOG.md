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

### 2026-07-28 — Iteration 2: a cordless ESP32-S3 companion with a colour screen

**The Arduino Nano build is unchanged and still supported.** Both devices speak
the identical protocol and can be connected at the same time; everything new
sits behind verbs the Nano never sends.

- **Added: an ESP32-S3 Wi-Fi companion** (Waveshare ESP32-S3-LCD-1.47B) —
  172×320 colour LCD, four buttons, a WS2812 whose colour *and* rhythm encode
  the alert class, and Li-ion operation with an on-screen battery gauge. It
  reaches the daemon over TCP, discovered by mDNS and authenticated with a
  nonce/HMAC handshake in which the token never crosses the wire. Config lives
  in NVS and survives a reflash; an unprovisioned board raises a Wi-Fi setup
  portal. See [`firmware/README.md`](firmware/README.md).
- **Added: a live terminal mirror.** The fourth button opens the selected
  session's *actual terminal* on the device, refreshed ~1×/s, with PREV/NEXT
  scrolling it. It reuses the PTY wrapper's existing pyte mirror and the
  per-session control socket the daemon already used for `focus`, so there is no
  new channel and no cost when the view is closed. Wrapped sessions only — a
  hook-only session has no PTY and says so. Terminal contents cross the same
  **plaintext** TCP link as everything else, which is a materially larger
  exposure than a status string.
- **Added: `firmware/flash_s3.sh`.** `arduino-cli upload` cannot flash this
  board: its download mode ignores the DTR/RTS straps, the USB link stalls
  partway through any large transfer, and a hard reset lands back in the
  bootloader looking exactly like a dead board. The script works around all
  three and verifies every piece with a device-side hash.
- **Added: `firmware/README.md`**, which the S3 sketch had been citing while it
  did not exist. Covers both builds, board options, wiring, flashing and
  provisioning.
- **Added: power off.** Holding MIRROR for 2 s puts the device into deep sleep;
  a tap wakes it. "Off" is deep sleep rather than zero — the WS2812 has no
  shutdown pin and idles ~1 mA whenever the rail is up, which dwarfs the ~8 µA
  the S3 draws asleep, so expect roughly a month of standby on a 14500 and use a
  switch in the battery lead if you need true zero. Waking reboots; NVS config
  survives, and `?` reports which kind of boot it was.
- **Protocol:** new `M|` lines (daemon → device) carry the mirror; new `B|M`
  (mirror button) and `B|F` (direct FOLLOW toggle) are accepted from devices
  that have the buttons for them. Additive in a dispatch that already ignores
  unknown verbs, so old firmware and this daemon interoperate in both
  directions.
- **Fixed: a wireless device starved a USB one.** `LinkHub.is_open()` answers
  *"is any device reachable"*, and `SerialMaintainer` gated reconnection on
  exactly that — so once an ESP32 linked, a Nano that dropped (or was plugged in
  afterwards) was never reopened and sat on `NO LINK`. Fixing the gate alone
  would have broken the other case, because the retry path `continue`d past the
  keepalive ping; both are fixed, and `test_net_link` grew a phase pinning the
  invariant (20 → 23 checks).
- **Added: the printed enclosure for the S3** —
  [`assets/3d-model/claude_mate_s3.stl`](assets/3d-model/claude_mate_s3.stl),
  ≈ 55 × 43 × 23 mm, cut for the 1.47" LCD on a raised shelf, four **Kailh Choc
  low-profile (1350)** switches, USB-C, and a tube on the back that takes the
  14500 cell behind a two-screw base plate. New build photos in
  [`assets/photos/`](assets/photos/).
- **Docs:** the README opens on both devices and now carries a per-build bill of
  materials, an S3 pinout, the S3 screen sketch, the wireless-handshake flow and
  the terminal-mirror section; the landing page was rebuilt around iteration 2.

### 2026-07-26 — …and it leaves WIP again once that work is actually done

- **Fixed: the device stayed on WIP forever after the task finished.** The
  background-work tells added the day before are Claude's *transcript* text —
  printed once, never rewritten. A recap still reads `✻ Worked for 4s · 1 shell
  still running` long after that shell exited, and nothing scrolls it off while
  you are away from the keyboard, which is exactly when the device is all you
  can see. The daemon only alerts DONE on `working → idle`, so a session that
  never left `working` never buzzed at all.

  Measured on two 200-second recordings of live sessions, one frame per second:

  | Session | Work ends | Old detector | New detector |
  |---------|-----------|--------------|--------------|
  | backgrounded shell (`sleep 45`) | t=57s | `working` on all 143 remaining frames | `idle` from t=60s |
  | background agent (Explore) | t=26s | `working` on all 154 remaining frames | `idle` from t=26s |

  The fix is structural rather than a timeout: the three tells that live in the
  transcript (the `Waiting for N … to finish` banner, the turn recap's
  `· N … still running` suffix, the task panel's `N in background` counter) now
  count only while nothing Claude printed **after** them has superseded them.
  When the work really ends Claude says so on a new line — `⏺ Background command
  … completed (exit code 0)`, `⏺ Agent … finished · 9s` — so a tell expires on
  the very frame its work does. Turn-end furniture that legitimately follows one
  (the task panel, the `❋ recap:` line, a usage warning) expires nothing.

- **Fixed: a finished subagent kept the session on WIP for as long as the agents
  panel stayed up** (27s in the recording). Claude leaves the completed agent's
  row on screen with its final tally, `◯ Explore  Count .h files … 9s · ↓ 7.5k
  tokens`, and `↓ 7.5k tokens` is textually a live activity meter. The meter is
  now read only *above* the prompt box, which is where Claude's own foreground
  status line renders; the agents panel below it is excluded.

- **Fixed: the live in-flight chip is now read below the prompt box** rather
  than from "the last four non-empty lines". That is Claude's own chrome, so
  neither the conversation nor the text you are typing (`kill the 3 shells`) can
  reach it, and the chip is still found when an agents panel pushes the hint row
  further up.

- **Fixed: two shapes that fired on ordinary prose.** The banner now has to name
  the banner's own nouns, so Claude writing *"⏺ Waiting for 2 CI jobs to finish
  before I merge."* no longer pins the device (prose is permanent transcript, so
  that pin never lifted). The `N in background` counter now has to sit alone on
  its line, so a right-aligned table column reading `build   2 in background`
  does not match. The banner's phrases (`background agent`, `agent to finish`,
  `workflow to finish`, `tool to finish`) are gone from the tunable `busy` list
  in `patterns.json`: as bare substrings they carried neither the structural
  shape nor the freshness gate, and the structural regex already covers them.

- **Fixed (hook): a `Stop` payload listing an already-finished background task
  no longer downgrades DONE to `working`.** Each `background_tasks[]` entry
  carries its own `status` (verified against real payloads:
  `{"type":"subagent","status":"running",…}`), and Claude Code fires another
  `Stop` with an empty array once the work lands. Counting a terminal entry
  would suppress a DONE that no later event ever corrects. An entry with **no**
  status is still counted, so a build that omits the field behaves as before.

### 2026-07-25 — A turn that ends with background work no longer reads IDLE

- **Fixed: the device showed IDLE (and buzzed a premature DONE) while a
  background dynamic workflow, background agents, a backgrounded shell, a
  monitor or an MCP task were still running — or still queued.** Claude's reply
  finishes and the prompt comes back, but the session is not done; the wrapper
  only recognised one of the three tells Claude renders for that, and only in
  its unwrapped form.
- **PTY wrapper — three new background-work signals:**
  - the turn recap's suffix — `✻ Crunched for 1m 56s · 1 shell still running` —
    which covers the work the banner does not name;
  - the **live in-flight chip** in the hint row under the prompt box
    (`⏵⏵ bypass permissions on · 1 shell · ← for agents · ↓ to manage`) — the
    tell that also covers **queued** work and scheduled loops, and that keeps
    updating after the transcript lines scroll away. It is read only in the
    footer region, where conversation text cannot reach;
  - the same fact on Claude's compact status row (`N in background`).
  The `Waiting for N … to finish` banner is now also matched when it **wraps**
  in a narrow terminal. A to-do left `in progress` in the task panel is
  deliberately *not* a busy signal — it is a plan item, not a running task.
  Verified against frames captured from a live session: the exact idle frame
  that used to read `idle` now reads `working`.
- **Background work no longer masks "needs your input".** The generic
  question-picker footer is vetoed by the **foreground spinner only** — a
  session that keeps a shell or a workflow running still reports `waiting` when
  it asks you something.
- **Hooks — `Stop` now reports what Claude tells it.** The payload's
  `background_tasks` (running/pending background work) and one-shot
  `session_crons` (a `/loop` tick, a `ScheduleWakeup`, a one-shot cron — this
  turn continuing later) downgrade `done` to `working`; the finish is reported
  by the next `Stop` that lands with nothing in flight. Recurring crons are not
  counted, so a session that merely owns a daily schedule still reports DONE.
  Both fields are optional — older Claude Code builds behave as before.
- **Added `tools/test_hook_state.py`** (18 checks over the hook's wire line per
  payload, via a new `CLAUDE_MATE_DRY_RUN=1` that prints the line instead of
  sending it) and **15 new `tools/test_detect.py` cases**, including a frame
  captured verbatim from a live session, the input-precedence guards, and the
  false-positive guards (prose about work "still running" or "N in background",
  chip-shaped text in the conversation rather than the hint row, and the
  reported screenshot itself: an in-progress to-do with nothing actually in
  flight must stay IDLE).

### 2026-07-07 — Project goal, non-commercial license, enclosure + photos

- **Repositioned** around the actual goal: cutting the cognitive overload of
  orchestrating many Claude Code agent sessions at once — across different
  accounts and projects. Added a **Roadmap**: iteration 1 is Arduino + macOS
  (this repo); iteration 2 is a wireless **ESP32 Wi-Fi** remote (no USB tether).
- **License changed from MIT to CC BY-NC 4.0** (Creative Commons
  Attribution-NonCommercial). Personal use and contributions are welcome;
  **commercial use is prohibited**. Covers the software, firmware, hardware
  design, 3D model, photos, and docs.
- **Added `assets/`** — photos of the built device and a printable 3D enclosure
  model (`assets/3d-model/claude_mate_v2.3mf`). The README now leads with a hero
  photo and an *Enclosure & 3D model* section.

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
