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
draws it. All ordering, selection, truncation and layout live in the daemon —
with one exception, the **workflow band** ([`J|`](#workflow-band)): for a
dynamic workflow the daemon sends numbers and a pre-rendered info string, and
each device draws the band in its own geometry.

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

While the shown session runs a **dynamic workflow** (Claude Code's Workflow
tool), the device shows the run itself in a **workflow band**: on the Nano it
takes r2's place — a bar and `2/5 2m39s 543k` — and model, effort and the chip
come back when it goes; on the S3 it sits under the fleet strip with the
workflow's name and a status word. It is display only: it never changes a
state, an acknowledgement or the LED. See [§1a](#workflow-band) and
[§3](#per-session-workflows).

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
| `F\|<flags>\|<sel>\|<r0>\|<r1>\|<r2>\|<r3>` | **The whole screen**, pre-rendered as four size-1 rows. See field table below. At least 7 fields; `r3` is the **last** field and may itself contain `\|`. A **transient notice** (why a press was declined, or which account a switch is heading for) rides in `r1`+`r2` for ~4 s: the daemon owns row rendering, so saying something new on the glass costs no firmware change and works on a device already flashed. `r0` and `r3` are left alone, so the message never loses the session it is about. |
| `J\|<st>\|<done>\|<failed>\|<total>\|<info>\|<name>` · `J\|-` | **The workflow band** for the session on the glass: a dynamic workflow's status, the counts that drive its bar, and an info string the daemon has already laid out. Drawn only while the `F\|` that follows carries flags **bit2**, and only from the `J\|` that `F\|` committed. `J\|-` clears it. Seven fields, frozen; grammar, caps and the write order in [The workflow band](#workflow-band) below. Older firmware ignores the line and the bit, and looks exactly as it did. |
| `V\|<kind>` | LED alert control (indication LED only; never touches the OLED). `<kind>` is `START`, `INPUT`, `DONE`, `ERROR`, or `OFF`. See **LED** below. |
| `L\|<i>\|<n>\|<name>\|<chip>` | **One saved account**, pushed on every handshake so the device's picker can show the logins you already have. Row 0 is always `default` (the `~/.claude` login, which is not a directory under the profiles root — omitting it let the device switch away from your main account with no way back). `i==0` resets the list, so a profile you deleted cannot leave a stale row behind. `<chip>` is that account's remaining-limit chip and may be empty: the names are free, the limits cost an HTTPS round trip each, so the daemon sends names immediately and re-sends enriched a moment later rather than blocking the handshake. Which account has headroom is the whole basis of the choice. Two consequences of that re-send, both load-bearing: the device must keep its **selection by name** across a rebuild (row numbers do not survive one, and the enriched push lands while a thumb is over GO), and the daemon must decode `B\|A<i>` against the list it actually **pushed** — it re-pushes within ~5 s of the profile set changing, but until then the glass is showing older rows and a fresh `listdir` would decode them to the wrong name. |
| `G\|2` | **BLE gamepad mode.** The device comes up as an ordinary Bluetooth HID gamepad (four buttons, no axes, physical order) and needs no daemon, no Wi-Fi and no loopback from then on — which is the only way a page served over **https** can be driven by this hardware, since it cannot reach `http://127.0.0.1`. Entering parks whichever radio was carrying the link, because the S3 has one 2.4 GHz radio and sharing it costs the link that matters. Reachable from the device itself at **SETTINGS → BLE gamepad** — the verb exists so the mode can also be entered, and tested, from the far end of a cable. Verified on hardware: advertises as `Claude Mate` with service `0x1812`, and `deinit` + Wi-Fi restart round-trips cleanly. **That last part holds only because Wi-Fi is a different stack** — BLE itself does *not* come back after a `deinit` in the same boot (`BLEDevice::init()` fails from then on), so on a device whose *link* is BLE, turning the gamepad off reboots rather than restarting the stack. See `firmware/README.md`. **`G\|2` now also sets the device's stored gamepad switch**, so it survives a reboot exactly as the menu toggle does — and consequently `G\|0` will *not* undo it (see below). |
| `G\|1` `G\|0` | **Controller mode** on / off. Sent by the daemon when a browser page takes or releases the grab on the `--web` bridge (see `daemon/webbridge.py`). While on, the device stops acting on its own buttons, reads the four pins raw and emits press/release **edges** (`B\|+P` …), draws a gamepad face **once** instead of per frame, and drops the backlight to a fraction of its normal duty. That last part is not decoration: a full flush is 110 KB over SPI, and doing it once a second to redraw a screen nobody is looking at — on battery, while the Mac has the buttons — is the overuse the mode exists to avoid. The device leaves on `G\|0`, or on a two-second hold of the 4th button if the Mac never sends one (a crashed tab, a killed daemon). An older firmware ignores `G\|` entirely and simply keeps its menu semantics. **The device can also be a gamepad by itself**, from **SETTINGS → BLE gamepad**, with no daemon involvement at all — so `G\|1` arriving while it is already a controller is a no-op rather than an error, and the daemon must not assume it is the only thing that puts the device there. <br><br>**`G\|0` releases only the mode the daemon opened.** Since the BLE gamepad became a persisted switch rather than an action, "on" is a standing instruction from the person holding the device, and a browser tab closing on the Mac must not revoke it. A device whose own switch is on stays a gamepad through `G\|0`; the two-second hold of the 4th button, or the settings row, is what turns it off. |
| `P` | Ping / keepalive, sent every ~15 s. The Arduino replies with `K` (NOT `H` — `H` means "I rebooted" and triggers a full resend + LED re-arm, which would restart the blink phase every ping). |

> **Adding a downlink verb? These letters are already taken, and not by this
> table.** On the S3, USB serial lines go to the **config console** first —
> `handleConfigLine()` — and only fall through to the protocol handler if it
> declines them. It owns `?`, `W`, `S`, `T`, `I`, `X`, `R`, `Y`, `Z`. A protocol verb
> that collides with one of those is **silently unreachable over USB** while
> working perfectly over TCP, so it survives every test that does not run on a
> cabled device. This is not hypothetical: controller mode first shipped as
> `X|1`, which the console answered with `refusing: send X|WIPE to clear
> config` — one lax compare away from wiping the Wi-Fi credentials and the
> token every time somebody opened the game. `tools/test_controller_mode.py`
> asserts the two sets stay disjoint.
>
> **The workflow band is `J|` for exactly this reason.** `W` was the obvious
> letter and is the worst one available: the console reads `W|<ssid>|<password>`
> and saves it, so a cabled S3 would have had its Wi-Fi credentials overwritten
> by a workflow's name. `T` (the token) and `S` (the daemon host) are the
> console's too. `J` is free on every path, in both directions.

**`F` line fields** — each row is ≤ 21 chars (size-1), drawn top to bottom:

| Field   | Description |
|---------|-------------|
| `flags` | Bitfield. **bit0**: invert **row 0** (the name) at ~2.5 Hz (an unacknowledged alert is on screen). **bit1**: FOLLOW mode is on (draw a ► marker by the state row). **bit2** (value 4): the **workflow band** is live for the shown session — draw it from the `J\|` this frame commits ([below](#workflow-band)). Set only when the shown session has a visible workflow and no notice holds `r1`/`r2`; never in the no-sessions frame. Older firmware reads only bit0 and bit1, and `flags` stays a single digit, so the frame is no longer than it was. |
| `sel`   | The character column **within `r3`** of the active tab's fleet letter (`-1` = none). The firmware fills a **wide** rectangle (~11 px) **centred** on that letter — a lit block with the letter knocked out. |
| `r0` (name) | Session name. The daemon truncates to the row width and, when two long sibling names collide, disambiguates with a middle squeeze (first 9 + `~` + last 10, e.g. `webapp-ba~ervice-one`). |
| `r1` (state) | `TAG  time` — the 4-char state tag (`ERR`/`WAIT`/`DONE`/`WORK`/`IDLE`) and the time in that state (mm:ss, or h:mm past an hour) — plus the session's **account** right-aligned (≥ 2-space gap, truncated to fit; omitted when unknown). |
| `r2` (meta) | Best-fit `model effort`, plus the **remaining-limit chip** (e.g. `5h82%`) right-aligned (≥ 2-space gap); the model+effort best-fit degrades into the room the chip leaves. Empty for hook-driven sessions with no scraped metadata. Sent the same with or without a workflow band: the Nano hides it while bit2 is set and draws the band there, the S3 keeps it, and older firmware just draws it. |
| `r3` (fleet) | `pos/total ` + one status **letter** per session in the stable (alphabetical) order, **space-separated**: `E` error, `B` waiting (blocked), `W` working, `D` done, `I` idle. An **unacknowledged alert's letter is sent LOWERCASE** — the firmware draws it uppercase but **blinks** it, so you can see which tabs still need acknowledging (they stop blinking as you ack them). When the strip does not fit, it is cut with a trailing `+`. `r3` is the last field (the firmware stops tokenizing at the 6th `\|` and takes the rest verbatim). |

The daemon sends an `F` line whenever the rendered bytes change: immediately on
any state change or button, and ~1/s while a displayed time ticks. Identical
frames are not re-sent — except on handshake, and while a workflow band is up:
then the `J|` and the `F|` that commits it go out together again whenever the
last pair is a second old, changed or not (below).

With no sessions the daemon sends `F|0|-1|MATE|no sessions||` (never with
bit2).

#### The workflow band — `J|` {#workflow-band}

While the shown session runs a dynamic workflow, the daemon sends it as numbers
plus one pre-rendered string, and each device draws the band in its own
geometry. It is the one place the firmware lays anything out — and even here the
daemon composes the text (`info`, the name); the device adds only a fixed
status word and decides where the bar ends.

```
J|<st>|<done>|<failed>|<total>|<info>|<name>     the band, for the session on the glass
J|-                                              no band: clear
```

Seven fields, **frozen**: nothing will ever be appended, which is what lets the
name be last. Mind the order — `done|failed|total` — which is not the socket
record's `done;total;failed` (§2). The firmware accepts exactly this, at most
77 bytes, and ignores anything else:

```
^J\|-$
^J\|[A-Z]\|\d{1,5}\|\d{1,5}\|\d{1,5}\|[^|]{0,14}\|[^|]{0,40}$
```

| Field | Description |
|-------|-------------|
| `st` | The workflow's status: `R` running · `E` running, an agent has failed · `P` paused on a usage limit · `D` completed · `F` ended without completing (failed or stopped — the screen cannot tell which). These are the workflow's letters, not the fleet strip's: an `E` here is a run that is still going, not a session error. The firmware accepts any `A`–`Z` and draws a letter it does not know neutrally — no word, the idle colour — so a status added later degrades instead of freezing the previous band. |
| `done` `failed` `total` | Up to 5 digits each, and they drive **only the bar**. The firmware saturates at 65535 and clamps `done ≤ total`, `failed ≤ total − done`. Counts unknown but the bar's percentage known → `done`=pct, `failed`=0, `total`=100. Nothing numeric known → `0\|0\|0`, and the firmware draws **no bar** at all, because an empty outline would read as 0 %. `failed` is 0 today: Claude's row does not show it. |
| `info` | ≤ 14 printable ASCII chars, no `\|`, composed by the daemon (below) and drawn right-aligned, verbatim. |
| `name` | ≤ 40 chars of ASCII: `\|` → `/`, `…` → `~`, any other non-ASCII → `?`, control characters removed. With `N` more workflows visible in that session it ends in ` +N`, inside the 40; a name cut to make room ends in `~`. The Nano never draws it. A `\|` cannot come from the daemon, so a name holding one is a line whose newline was lost with the next line spliced on — `J\|R\|1\|0\|5\|1/5\|deployF\|4\|4\|api…` — and the firmware rejects it rather than drawing half a frame as a name. |

**`info`** is built from up to four parts:

| Part | |
|---|---|
| COUNT | `done/total` — `3/5`; or `40%` when only the bar's percentage is known |
| CLK | the workflow's own clock, in Claude's units: `45s` under a minute, `2m39s` under an hour, `1h02m` under a day, then `1d03h` |
| TOK | tokens, compact: `999`, `1.2k`, `543k`, `1.4M`, `12M`, `1.2B` — rounded, and never `1000k` (that is `1.0M`) |
| PLUS | `+N` when `N` more workflows are visible |

It is the first of these that fits 14 characters, parts joined by single spaces
and missing ones skipped: COUNT CLK TOK PLUS · COUNT CLK PLUS · COUNT PLUS ·
COUNT CLK TOK · COUNT CLK · COUNT TOK · COUNT · CLK TOK · CLK · TOK. A paused
workflow with a usage-limit note shows the note instead (`til 3:30pm`). With
nothing at all to show — or nothing that fits 14 characters — a running one
(`R`/`E`) says `running`, followed by ` +N` when `N` more are visible
(`running +2`): the Nano draws no word for a running run, and without it r2
would be blank. A paused, finished or ended one gets just `+N`, or an empty
info, because both devices already draw `paused`, `done` or `ended` themselves
— the word again would read `paused … paused`. So a session whose footer reads
`◯ deployment-risk-sweep-3802  ▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱  0/5 · 2m39s · ↓ 542.8k tokens`
gets, on every device:

```
J|R|0|0|5|0/5 2m39s 543k|deployment-risk-sweep-3802
F|4|4|aladdin|WORK  03:09  claudeal|Opus 5.5 xhigh  5h82%|1/2 W I
```

and a second visible workflow in that session would make the info
`0/5 2m39s +1`.

**bit2 is the gate, `F|` is the commit.** The firmware parses a `J|` into a
staging copy and draws nothing; the next `F|` adopts it and renders, the way
`M|END` commits a mirror page. The staging copy outlives its commit, and a
refused `J|` leaves it as it was — so an `F|` whose own `J|` never arrived
commits the one before it. The write rules exist for that case. The daemon,
under the lock that composes the frame:

* while the band is up, writes the subject's `J|` and then the `F|` whenever it
  writes anything at all — a changed frame, a changed band, a forced resend —
  and, when nothing has changed, again once the last pair is **a second old**:
  the next call of the daemon's 1 Hz ticker, so every second or two. It writes
  that `F|` **even when its bytes are unchanged**, because it is the commit.
  Without the repeat, a band and a frame that both stand still — a paused or
  finished run under an `r1` past the hour, which moves once a minute — would
  leave a lost `J|` wrong for up to a minute;
* when the band moves to a different session — PREV/NEXT from one session's
  band to another's — writes `J|-` ahead of the new session's `J|`; likewise
  for the first band after the daemon starts, since a device that outlived the
  last daemon may still hold a `J|` it staged;
* when the band goes, writes `J|-` once, then frames as usual;
* on a forced resend (`H`, the serial port reopening, the mirror closing),
  writes `J|` — or `J|-` — and then `F|`.

So every frame with bit2 arrives right behind its `J|`, and a frame is always
drawn from a `J|` and the `F|` that committed it. A lost `J|` costs a second or
two of band at most — the next pair repairs it, the same *every frame is the
whole screen* rule the rest of the protocol lives by — and it never puts one
session's band under another session's name: after a press of NEXT, what a
lost `J|` leaves staged is the `J|-` written ahead of it, so that `F|` commits
no band rather than the last session's. Device sockets on TCP have Nagle off
(`TCP_NODELAY`), so an `F|` written right behind its `J|` is not held back
waiting for an ACK.

**Nothing in the band blinks**, on either device. A blink here means an
unacknowledged alert that GO clears, and a usage-limit pause can last until a
weekly reset with nothing to acknowledge. Workflow data never touches a
session's state, its acknowledgement or the LED ([§3](#per-session-workflows)),
and a running workflow does not keep the S3's screen awake.

**What each device draws** — the pixels and the four status words are the
firmware's, every other character is the daemon's:

* **Nano** — r2 is replaced while bit2 is set: `info` right-aligned, and to its
  left a bar outlined and filled `done/total` (`R`); the same bar 7 px shorter
  with a `!` after it (`E`); or the word `paused`, `done` or `ended` (`P`, `D`,
  `F`). No bar when `total` is 0; nothing for a letter it does not know. It never
  draws the name, and no word for a running (`R`/`E`) workflow. When bit2
  clears, r2 is model + effort + chip again.
* **S3** — two lines under the fleet strip, where the link-detail line is
  otherwise drawn, and only on the triage view (never over the menu, the
  mirror, the game, the gamepad face, pairing or the P2P screen): a bullet, the
  name and the status word (`running`, `paused`, `done`, `ended`); then a bar —
  one segment per agent when they fit (done in the status colour, failed in the
  error colour, pending as an outline), else a continuous bar — and `info` in
  the rows' size-2 type. Running is drawn in the WORK colour (with an
  error-coloured bullet for `E`), paused in WAIT's, done in DONE's, ended in
  ERR's, an unknown letter in IDLE's; a paused bullet is hollow. While the link
  is down its status replaces the name, dimmed, so the diagnosis stays on
  screen.

An already-flashed device ignores `J|` (an unknown verb) and bit2 (it reads
only bit0 and bit1), and keeps drawing r2: it looks exactly as it did.

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
| `H`   | Hello / handshake, sent **once** right after boot/reset. On receiving `H` the daemon **re-sends the full current state**: the shown session's workflow band (`J\|`, or `J\|-` when it has none) and then the frame, and it re-arms the LED loop. |
| `K`   | Keepalive ack — the reply to `P`. The daemon ignores it. |
| `B\|P` | **PREV** pressed (D4) — selection one step up the queue. Repeats while held. |
| `B\|N` | **NEXT** pressed (D3) — selection one step down the queue. Repeats while held. |
| `B\|G` | **GO** short press (D2). The firmware just emits `B\|G` on each short press; the **daemon** disambiguates a single press (after ~300 ms) from a double-click. A single press raises the terminal of the session **shown on the glass** (raise only), acknowledging its alert. A **double-click** (two `B\|G` within ~300 ms) toggles **FOLLOW** mode. |
| `B\|K` | **GO** long press (D2, held ≥ ~500 ms) — acknowledge the shown session's alert WITHOUT raising anything. No-op when nothing is unacknowledged. Also sent by a **short press of the ACK button** on a 4-button device: same verb, same effect, no daemon-side distinction. |
| `B\|F` | Toggle **FOLLOW** mode directly. Identical in effect to a GO double-click, but unambiguous: no 300 ms window to race. Turning it ON raises the shown terminal immediately, exactly as the double-click does. No stock firmware emits this today (the 4th button became MIRROR); it is kept because the daemon accepting it costs nothing and a device with a fifth switch would want it. |
| `B\|M` | **MIRROR** — open/close the terminal view for the session on the glass. Now reached from the ACTIONS sheet rather than directly from the 4th button. |
| `B\|C` | **Send "continue"** — type `continue` into the shown session and press Enter. The reason the ACTIONS sheet exists: a session that hit its 5-hour limit or took an API error is waiting for one word, and this is how you say it without turning back to the laptop. Wrapped sessions only — a hook-only session has no PTY to type into. The daemon relays it as `submit continue` on that session's control socket; the wrapper strips control bytes, so this can never become a remote keyboard. |
| `B\|T` | **New terminal** — open one in the shown session's directory and focus it. `open -a Terminal <cwd>`, which both opens and fronts, so no separate raise is needed. |
| `B\|A<i>` | **Switch to saved account `<i>`** — continue the shown conversation on one of the logins you already have. Switching does **not** mean signing in again: each is a profile directory under `~/.claude-accounts` holding its own credentials. `<i>` indexes the `L\|` list the daemon pushed, sorted, so both ends agree which name row 3 was — a drift there would switch you to the wrong account. The daemon sends `switch <name>` to the wrapper, which owns the claude process and so is the only thing that can replace it: it copies the transcript into that profile and re-execs with `--resume`. The name travels as **one argument, spaces and all** — a profile is a directory the user named, and `work 2` is an ordinary name for one. The wrapper answers `err <why>` and **keeps the session running** if it cannot honour the request — already on that account, no such profile, or nothing said yet so there is no transcript to carry. Every reason is checked before claude is asked to exit: a switch that fails after the session is gone costs you the session and gives nothing back. **The refusal goes on the glass**, not just into a log — the daemon holds it across the middle two rows for ~4 s (see `F\|`), because a press that quietly declines is indistinguishable from a button that does not work. Reasons are written to fit two 21-char rows for that reason. A different **organisation** is *not* among them: every account the picker can offer is a login the user installed and authenticated, so the domain difference is announced by the switcher and not used to refuse — a guard that fired on every pair of a real three-account fleet was an outage, not a safeguard. |
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

### 1d. The wireless handshake (TCP and BLE devices)

Before any of the above flows, a wireless device proves it knows the shared
token. **The same three lines on both radios**, in the same order, verified the
same way, against the same token — one handshake to understand, one secret to
provision, one thing to get wrong:

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

### 1d-bis. Enrolment (`E|`, BLE only) {#enrolment}

The handshake above assumes both ends already share a token. Getting one *onto*
a cordless device used to mean a cable it may be nowhere near, or a Wi-Fi access
point and a phone — for a BLE board, over a link that is already connected and
talking. `E|` is the verb that closes that gap.

| Direction | Line | Meaning |
|---|---|---|
| daemon → device | `E\|?` | may I enrol you? |
| device → daemon | `E\|OK` | a human pressed **GO** on the device |
| device → daemon | `E\|NO` | declined, already provisioned, or nobody was there |
| daemon → device | `E\|<token>` | the secret — **only ever after `E\|OK`** |
| device → daemon | `E\|SET` | adopted; challenge me again |

**The device asks its own human**, and that is the whole security argument. It
puts `PAIR?` on its screen and waits up to 45 s for a button; the daemon's side
is *armed*, never on, for three minutes at a time and disarmed by success or by
a refusal. So being in radio range gets an attacker a prompt on a screen they
cannot reach, and nothing else — and `E|<token>` is refused outright unless a
button was pressed in that same connection, because "has no token yet" must
never be permission by itself.

**What it costs**, stated plainly: the token crosses the air once, in the clear,
inside that window. The link is unencrypted by design (see the threat model
above), so this introduces no plaintext that was not already there — but a
sniffer listening at the second you press GO does learn the secret. Pair away
from hostile radio, or use a cable, which the daemon provisions over
automatically and with no window at all.

### 1e. Which pipe carries it

Three, and the bytes above are identical on all of them. Nothing in the protocol
changes with the transport; only who dials whom, and what the idle radio costs.

| | USB | TCP (`--tcp`) | BLE (`--ble`) |
|---|---|---|---|
| Who initiates | the daemon opens the port | the **device** dials the daemon, found over mDNS | the **Mac** scans; the device only advertises |
| Discovery | serial-port glob | `_claudemate._tcp` | the service UUID `c1a0de00-…-0001` |
| Idle cost | none (cabled) | an association held open continuously | **200 ms of advertising, then ~4 s silent**, repeating |
| Handshake | none (physical access is the trust) | §1d | §1d, identical |
| Framing | newline-delimited ASCII | newline-delimited ASCII | newline-delimited ASCII inside GATT writes/notifications — **a notification boundary is not a line boundary**, and both ends reassemble on newlines |

On BLE the device is the **peripheral** and exposes two characteristics, because
a GATT characteristic is not bidirectional: `…-0002` is written by the daemon
(everything in §1a), `…-0003` is notified by the device (everything in §1b).
Status is **never** encoded into the advertising payload — the status originates
on the host and the peripheral is the device, so the only thing it could
advertise about is itself.

**Wi-Fi and BLE are alternatives, never simultaneous:** the ESP32-S3 has one
2.4 GHz radio. Which one is a stored device setting (`claude-mate link`, or
`I|WIFI` / `I|BLE` / `I|P2P` over the cable), not a compile-time choice. `p2p`
is a third value of that same setting: Wi-Fi with the device as the access point
and the Mac as its only client, so the device dials a DHCP lease off its own AP
instead of an mDNS answer off a router. **Nothing above the socket changes** —
same nonce handshake, same frames — which is why the daemon has no notion of P2P
at all. The daemon can
serve both at once — a Nano on a cable, a Wi-Fi board and a BLE board all get
every frame — because on its side they are three transports, not three
protocols.

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
<state>|<session_id>|<name>[|<ctrl_sock>|<model>|<effort>|<account>|<limit>|<wf>]
```

| Field        | Description |
|--------------|-------------|
| `state`      | One of: `working`, `waiting`, `done`, `error` (or `idle`/`end` from the PTY wrapper). |
| `session_id` | The Claude Code session id. **MAY be empty** if not provided. |
| `name`       | Basename of `cwd`. The wrapper sanitises it like the meta fields — `\|` → `/`, CR/LF removed — because a `\|` in a directory name used to shift every field after it. |
| `ctrl_sock`  | (PTY wrapper) per-session control socket GO's `focus` connects to. Empty for hooks. |
| `model`      | (PTY wrapper) model in use, e.g. `Opus 4.8`. Empty until scraped / for hooks. |
| `effort`     | (PTY wrapper) effort level, e.g. `xhigh`. Empty until scraped / for hooks. |
| `account`    | (PTY wrapper) account/profile the session runs as, e.g. `work` or `default`. Empty for hooks. |
| `limit`      | (PTY wrapper) remaining-limit chip for that account, e.g. `5h82%` (82% of the 5-hour window left) or `wk31%` (31% of the week left) — whichever window is more depleted. Refreshed every `CLAUDE_MATE_USAGE_POLL_S` (default 120 s) from Anthropic's OAuth usage endpoint; empty until the first successful poll / for hooks. |
| `wf`         | (PTY wrapper) the session's **dynamic workflows**, read off Claude's footer: up to three records, or `-` for none. Empty = keep what the daemon has. See [Field 8](#wf-field) below. |

The hook path sends only the first three fields, and still does now that there
is a workflow band — so a hook-only session never shows one. The PTY wrapper
always sends all nine: the control socket, (once scraped) the model + effort,
(once known) the account + remaining limit, and `wf`. All trailing fields are
optional — the daemon defaults missing ones to empty, and an empty model,
effort, account or limit means *unchanged*, never *cleared*. Fields after the
ninth are ignored, so a later producer can append one without confusing this
daemon.

The hook is **fire-and-forget**: it connects with a short timeout, writes one
line, and exits **0** regardless of outcome. If the daemon/socket is down it
**silently no-ops**. The hook must never block or fail a Claude turn.

### Field 8 — `wf`, the session's workflows {#wf-field}

The PTY wrapper reads the row Claude Code draws under the prompt box for each
dynamic workflow (README, *Background work*) and sends what it sees:

```
wf  := '-'                              no workflows: clear
     | rec ('~' rec){0,2}               at most 3 records
rec := st ';' done ';' total ';' failed ';' pct ';' elapsed_s ';' tokens ';' note ';' name
```

An empty or missing field 8 means *keep what you have* — every older wrapper
sends that, and so does this one when its workflow code could not build the
field. `-` is the explicit *none*: the other fields are sticky when empty, so
without it a finished workflow could never be cleared. Anything else replaces
the session's whole set, unless no record in it is valid — then the set is
kept.

| Sub-field | Description |
|---|---|
| `st` | `R` running · `E` running, an agent has failed · `P` paused on a usage limit · `D` completed · `F` ended without completing (failed or stopped; the screen cannot tell which). The row's text says little of this: only a paused row says it in words — `⏸ … paused · usage limit resets …` — and the rest is the bullet's colour, plus whether the run's clock still moves. |
| `done` `total` `failed` | Decimal integers, or empty when unknown. Claude drops `done/total` first as the terminal narrows (below ~85 columns with a long name). `failed` is always empty today: the screen does not show it. |
| `pct` | 0–100, the progress fraction: from `done/total` when the row shows them, else read off the bar. Sent whenever the row has a bar. |
| `elapsed_s` | The run's own clock **now**, in seconds (below). Empty when no clock is on screen. |
| `tokens` | A plain integer: `542.8k` → `542800`. |
| `note` | Empty, or a compact usage-limit note: ASCII, ≤ 14 chars, no `;` `~` `\|`. |
| `name` | The **last** sub-field — a record is split with `split(';', 8)`, so a name may contain `;`. `\|` → `/`, `~` → `-`, control characters removed; ≤ 40 characters. |

The whole field is capped at **300 UTF-8 bytes** by leaving out whole records,
lowest priority first (the order in *Which records*, below), never part of one;
if not even one fits, the field is empty (keep).

**The usage-limit note** is the reset time, cut down to fit the band:

| A paused row reads | `note` |
|---|---|
| `paused · usage limit resets at 3:30pm` | `til 3:30pm` |
| `paused · usage limit resets at 3:30pm (Europe/Madrid)` | `til 3:30pm` |
| `paused · usage limit resets Oct 11, 3pm` | `til Oct 11 3pm` |
| `paused · usage limit` | `usage limit` |
| `paused` | *(empty)* |

The rule: take the text after `resets`, drop a leading `at ` and a trailing
`(…)` time zone, remove commas, collapse spaces and put `til ` in front; if
that is over 14 characters, drop the `til `; then cut to 14.

**Which records.** The wrapper's workflow watch decides: the rows on screen,
or the rows it saw last while they are out of sight. It *holds* them — finished
ones too — for as long as a menu or a dialog has the panel's place, and for
about **1.5 s** with Claude's prompt footer up but no row drawn, which is what a
frame Claude has only half redrawn looks like; a live run's rows for 20 s, since
a run always shows its outcome before its row goes. Only then does it send `-`.
A finished run sent as `-` and listed again a moment later would be a new
record to the daemon, its minute to linger started over
([§3](#per-session-workflows)), and the band would blink off and on every time
something covered it. Every line before a workflow was first seen carries `-`
too.

At most three records, chosen in this order:

1. a run the watch saw end in the last **10 s**, the most recent first — so an
   end reaches the daemon while it is still news, however many runs are still
   going beside it;
2. the running ones (`R`/`E`/`P`), in on-screen order — so a stack of finished
   rows cannot push one still running off the line;
3. the other finished ones, the most recently ended first, and last those it
   never saw end — rows that were already finished when it first read them.

The chosen records go out in on-screen (start-time) order. Rows past the third
are not on the line at all: the band follows at most three runs per session,
and its `+N` never says more than `+2`.

**`elapsed_s` is the clock now, not the clock last read.** Claude's clock text
ticks at its own resolution — every second for `2m39s`, every minute for
`1h02m`, every hour for `1d03h`. For a running row (`R`/`E`) the wrapper sends
the parsed clock plus the time since that text last changed, capped at one
tick; for a `P`/`D`/`F` row, or a clock that has stopped, the parsed value.
While a running row is held out of sight, its clock keeps advancing from its
last sighting.

**Cadence.** The wrapper re-sends when its state or meta fields change, and
that check now includes the workflows' *shape*: each record's `st`, `done`,
`total`, `failed`, `pct`, `note` and `name`, and how many records there are. A
shape change goes out at once. `elapsed_s` and `tokens` are not part of it: they
ride the existing 20 s keepalive, and the daemon runs the clock in between
([§3](#per-session-workflows)). There is no extra refresh timer, so the line
rate is what it was. With `CLAUDE_MATE_DEBUG` set, the snapshot header shows
`wf` as well.

**Fenced.** If the wrapper's workflow code fails, `wf` falls back to empty
(keep) and the state report still goes out: a workflow can cost the band, never
the session.

The uncoloured — running — row from [§1a](#workflow-band) reaches the socket
as:

```
working|3f9c1a2b|aladdin|/tmp/claude-mate-ctrl-3f9c1a2b.sock|Opus 5.5|xhigh|claudealaddin2|5h82%|R;0;5;;0;159;542800;;deployment-risk-sweep-3802
```

### The control socket (CLI → daemon) {#the-control-socket}

The same socket carries **commands**, not just session updates, and this is the
whole surface — six verbs. Anything unrecognised is parsed as a session update
above and dropped, so a typo is silent rather than an error.

| Line | Reply | Meaning |
|---|---|---|
| `queue` | JSON | the whole queue, as the device shows it — each entry with its visible `workflows` ([§3](#per-session-workflows)). Also the only liveness check there is |
| `accounts` | JSON | the saved logins and where they live |
| `accounts-refresh` | `ok` | re-read the accounts directory (after something changed it) |
| `press\|<code>` | `ok` | press a device button: `P N G K F M C T`, or `A<n>` to continue on saved account *n* |
| `select\|<n\|name>` | `ok <name>` | point the selection at a row |
| `pair` | `ok` | arm BLE enrolment for the next unprovisioned device |

**The button codes are the device's own**, and deliberately so: `press|G` is
handed to the same `ButtonReader` that handles `B|G` off the wire, so GO cannot
mean one thing to the device and another to the terminal.

There is **no** `status`, `ping` or `version` verb, and **no** delete. A hook in
any of your shells can write to this socket; a hook firing a malformed line
should not be able to remove a login, so `claude-mate accounts rm` does the
deletion in its own process and only asks the daemon to re-read afterwards. The
socket is mode 0600 — owner only.

Starting the daemon is not on here either, for the reason in
[CLI.md](CLI.md#starting-and-stopping-the-daemon): it goes through launchd, or
you get two of them fighting over one socket.

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

> **The hook does not forward the workflow itself.** It still sends three
> fields, so a `Stop` with a workflow in flight reports `working` and nothing
> more: the workflow band — the name, the progress, the status — needs the PTY
> wrapper ([field 8](#wf-field)).

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
| `workflows`      | The session's dynamic workflows from field 8 — up to three records, replaced whole. Display only; see [Per-session workflows](#per-session-workflows). |

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

### Per-session workflows {#per-session-workflows}

Each session keeps the records its latest field 8 listed, as one immutable tuple
replaced whole under the registry lock and read without it, the way the frame
reads every other field. An empty field 8 keeps the tuple, `-` empties it, and
a field with at least one valid record replaces it.

**Never a state.** Workflow data never changes a session's `state`,
`state_since` or acknowledgement, the one-shot LED kind an update returns, or
the LED. A workflow finishing is not an alert of its own: what keeps a session
`working` while its run is live is the wrapper's detection (*States* above), and
the DONE you acknowledge is still the session's, when it leaves `working` —
exactly as before.

**Merging.** A new list is matched to the old one by name, the k-th record of a
name to the previous k-th record of that name, and each record carries two
times of its own:

* an **anchor** for its clock — for `R`/`E` with a known `elapsed_s`, arrival
  time minus `elapsed_s`. While the record stays `R`/`E` the daemon keeps the
  earlier of the old and the new anchor, so the clock never steps back when a
  scrape lags; it starts afresh when the record enters `R`/`E` from `P`/`D`/`F`,
  or is new;
* **ended** — set when the record turns `D`/`F`, carried while it stays there,
  cleared if it goes back to `R`/`E`/`P`; and whether that end was **seen** —
  whether the line before listed the same record `R`/`E`/`P`. A record that
  first arrives already finished gets an ended time on arrival, for its linger,
  but no seen end.

It also remembers when a line last listed it, and whether that line came from a
wrapper — one with a non-empty `ctrl_sock`, which the hook never sends.

**Visibility** at a given moment — one rule, used by the frame, `J|`, the queue
JSON and the prune:

| Record | Visible while |
|---|---|
| `R` `E` `P`, listed by a wrapper | the last line that listed it is at most **90 s** old (`WF_STALE_S`). The wrapper's 20 s keepalive keeps a live one fresh; a wrapper that crashed does not |
| `R` `E` `P`, listed by a line with an empty `ctrl_sock` | always — until it is replaced or cleared, or the session itself is pruned |
| `D` `F` | at most **60 s** after it ended (`WF_LINGER_S`). Claude drops the row about 30 s after the end, so this is a backstop |

An expired `D`/`F` that its producer still lists stays as a **hidden
tombstone**. Without it, the next keepalive would bring the record back with a
fresh 60 s, and a finished run would flap on the device for as long as its row
stayed up. It goes when a line no longer lists it, on `-`, or with the session.
The prune that runs every 5 s drops stale `R`/`E`/`P` records.

**The primary record** — the one the device shows — is the most recently ended
visible `D`/`F` whose end the daemon saw, if that was within the last **10 s**
(`WF_SPOTLIGHT_S`), so a run finishing beside one still going is seen to finish;
else the first visible `R`/`E`/`P`; else the first visible `D`/`F`. A record that
arrives already finished — after a daemon restart, or a name Claude has cut
afresh on a resize — ended at a moment the daemon never saw, possibly long ago:
it lingers like any other, but never takes the band from a running one, which
would announce a stale run as just finished. Every other visible record counts
toward the band's `+N`.

**Two clocks.** `state_since`, and so r1, is time in *state*: it restarts at
every state change — approving a workflow agent's permission prompt is WAIT →
WORK — and for every session when the daemon restarts. The band's clock is the
*run's*: `now − anchor` for `R`/`E`, `elapsed_s` as sent for `P`/`D`/`F`, none
when unknown. It carries on through both, which is why the device writes it in
Claude's units (`12m30s`, `1h02m`) and never in r1's `mm:ss`.

**A run that is stopped, or fails, counts on for a while.** Its row turns red at
once, and red with a clock still moving is a run with a failed agent that goes
on (`E`), so the wrapper reports `F` only once the clock has stood still for
10 s — 70 s for a clock past the hour, which moves once a minute. Until then the
record is `E`, the anchor keeps the band's clock counting, and at `F` it shows
`elapsed_s` as sent: the time Claude froze, a step back by however long it
counted on. A run past the hour usually leaves the band first, when Claude
drops its row.

**Logging.** One daemon log line per start or finish — a name appears or goes,
or a record's status changes — such as
`workflow: aladdin "deployment-risk-sweep-3802" R -> D`. Never one per
keepalive.

**The queue JSON** (`queue`, and so `claude-mate status`) gives every entry a
`workflows` list — always present, `[]` when there are none — of its *visible*
records, each with `name`, `st` (the letter), `status` (the word: `running`,
`paused`, `done`, `ended`), `done`, `total`, `failed`, `pct`, `secs` (the
displayed clock in whole seconds, rounded down, or null), `tokens` and `note`.
The record the device shows comes first, then the rest in on-screen order.
