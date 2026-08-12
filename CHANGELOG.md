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

### 2026-08-12 — Pairing: one command, one button, no Wi-Fi anywhere

- **Added: `E|`, an enrolment handshake over BLE, and `claude-mate-connect
  --pair`.** Provisioning a cordless board meant a cable it was nowhere near, or
  a Wi-Fi access point, a phone and a 32-character secret typed by hand — over a
  BLE link that was *already connected and talking to the daemon*. Now: run the
  command (or press `d` at the account picker), the device puts **PAIR?** on its
  screen, press **GO**, done.
- **The device is the one that asks its human**, which is the whole security
  argument. Being in radio range gets an attacker a prompt on a screen they
  cannot reach and nothing else; `E|<token>` is refused outright unless a button
  was pressed in that same connection, because *"has no token yet"* must never
  be permission by itself. Approval is single-use and dies with the connection,
  the daemon's side is **armed for three minutes** rather than on, and a refusal
  disarms it so a declined device is not asked again on a loop. The cost is
  stated in `docs/PROTOCOL.md` rather than glossed: the token crosses the air
  once, in the clear, inside that window — the link is already unencrypted by
  design, but a sniffer listening at that second learns it.
- **Changed: every "no token" message names that route.** Two screens and the
  daemon's log had been pointing at the Wi-Fi portal, which is the long way
  round, and before that at a cable a cordless board may not have. They now all
  say `claude-mate-connect`, pinned together by a test — they had already
  drifted twice.

### 2026-08-12 — The cable provisions the radio, and the picker can say why it did not

- **Added: the daemon hands its token to any device that appears on USB.** *"How
  do I connect it after a factory reset"* had two honest answers — raise a portal
  from a phone, or type `T|<token>` down the cable — and both are work nobody
  should be asked to do, because the wiped device and the daemon that knows the
  secret are *already joined by a cable over which provisioning is the trusted
  path*. Every serial open now pushes the token. Measured on hardware: `no token`
  → `serial: handed the device this daemon's token` → `BLE device connected`, in
  **four seconds**, with nothing typed. Unconditional rather than conditional on
  "does it need one", because USB has no handshake to ask over (it is trusted by
  being physical) and the daemon's token is the authority — a device holding a
  different one cannot link, so overwriting is the repair. NVS skips a write
  whose value is unchanged, so the steady state costs nothing.
  `CLAUDE_MATE_NO_USB_PROVISION=1` turns it off.
- **Added: `claude-mate-connect`, and a `d) device` entry in the account
  picker.** That prompt is the only Claude Mate UI on the Mac, and until now it
  could only choose an account — so a device that would not link had its
  explanation in two places nobody at that prompt was looking: the device's own
  glass, and a log file. The command reports every link (daemon, token, cable,
  BLE) and prints the shortest fix for whichever is down, then the picker comes
  back. It also states the thing that reads as a fault and is not: **the device
  never appears in System Settings → Bluetooth**, because the status link is an
  unpaired GATT peripheral and the token handshake, not pairing, is what decides
  who may drive it.

### 2026-08-12 — The radio switch comes off the glass, the token screen moves to the front

- **Changed: no `Link` row in SETTINGS.** It was a plain toggle, so **one press
  moved a cordless board onto Wi-Fi** — and a board with no credentials then
  reboots into the setup portal, which outranks the menu and does not time out
  for a Wi-Fi device, so it hides the very row you would use to undo it. One
  press on the glass, and no way back without a cable. Found the way these
  things are always found: a board that had "stopped working over BLE" turned
  out to be sitting on Wi-Fi with no SSID, because the row had been pressed. The
  transport is not deleted — `I|WIFI` / `I|BLE` still work over USB, which keeps
  that switch behind a cable you have to actually have: if you can type `I|WIFI`
  you can type `I|BLE` back. Which radio is live, and whether it has found the
  daemon, is still on the glass in **SETTINGS → About**, which now colours the
  link green when the daemon is on the other end — the signal the `Link` row
  used to carry.
- **Changed: `Wi-Fi setup` becomes `Set token`, and moves to the top of
  SETTINGS.** Same portal behind it; a different name, because on a BLE device
  the only field that matters is the token and a row labelled *Wi-Fi setup* is
  a row nobody presses when a token is what they need. The value answers before
  you press it — `none`, in red, *is* the reason a freshly reset board is doing
  nothing. It is first because it is the row you need when nothing else works,
  and five rows are visible without scrolling.
- **Fixed: a factory-reset board with no cable attached could not be given a
  token at all.** For one commit this row was removed alongside `Link`, on the
  reasoning that both were Wi-Fi doors. They are not: the `Link` row *changes*
  the transport, while this one *provisions* the device, and on BLE it is the
  only on-glass route to a token. What remained was BOOT-held-at-power-on — an
  incantation nobody discovers, and useless advice to someone holding a cordless
  board that has just wiped itself. It is also not the trap `Link` was: on a BLE
  device the portal expires after five idle minutes and hands the glass back,
  and the token it takes reaches the running stack.
- **Changed: every "no token" message on the glass names that row** rather than
  a cable. `send T|<token> over USB` is not an instruction when the board is on
  its cell across the room; the status line, the NO LINK screen and the
  `A|NOTOKEN` note now all say `MENU > Set token`, and the daemon's log gives
  both routes. A test pins the three device-side strings together, because they
  drifted once already — the glass advertised a USB-only route while the only
  on-glass route had been deleted.
- **Fixed: a config write to an unprovisioned Wi-Fi device killed the setup
  portal.** `restart()` stopped the portal, saw no SSID and dropped to `OFF`. An
  unprovisioned Wi-Fi device *is* a device sitting in the portal — that is where
  `begin()` puts it — so `T|<token>`, typed at exactly the device that needs a
  token, tore down the only screen that could finish the job and left no link,
  no portal, and no way back but a reboot. It now does what `begin()` does:
  nothing to join means the portal.

### 2026-08-12 — A board with nothing in NVS can get itself a token

Five ways a factory-reset ESP32-S3 could not be provisioned at all, found by
wiping one and trying. Every one of them ends the same way: a device advertising
nothing and answering nothing, which no test on either side of the protocol
could have seen. `tools/test_ble_link.py` now pins all five statically
(43 → 57 checks).

- **Fixed: an unprovisioned board came up in a portal it could not satisfy.**
  Empty NVS defaulted the transport to Wi-Fi, so `begin()` found no SSID and
  raised the setup portal — which outranks every screen in `render()` and blocks
  the menu, including the **SETTINGS → Link** row that would have moved the
  device to BLE. The way out was reachable only from behind the thing blocking
  it. A board with nothing stored now comes up **on BLE**, the radio that needs
  nothing stored to work. A board with an SSID still comes up on Wi-Fi: an
  upgrade must not move a working device onto a different radio behind its
  owner's back.
- **Fixed: `T|<token>` reached NVS but not the running BLE stack.** The device
  went on answering `A|NOTOKEN` with the correct secret in flash beside it —
  indistinguishable, from either end, from a daemon rejecting a token that is
  right. Since BLE cannot be restarted in the same boot, the stack now takes the
  token live. The same hole existed on the **portal** path, where the page
  handler writes to NVS and has never heard of the stack, and that was the worse
  one: it is the only way a cordless board can be given a token with no cable.
- **Fixed: holding BOOT on a BLE board was a one-way door.** The portal only ever
  timed out on a device with credentials to fall back to, because for an
  unprovisioned Wi-Fi device the portal is all there is. A BLE device has a
  working link and no use for the screen, so it now expires after five idle
  minutes and hands the glass back — and expiring with no SSID powers the radio
  down rather than dialling an empty one, which would put the STA back up on the
  one radio the other transport is trying to use.
- **Fixed: `W|`, `S|` and `T|` restarted Wi-Fi on a BLE device.** Provisioning
  credentials for later would associate immediately, leaving both radios up —
  the exact contention one-transport-at-a-time exists to prevent. They are
  stored either way and applied only when Wi-Fi is the live link.
- **Fixed: the portal would not save a token without a network.** A blank
  network box was a `400 network required`, which is the right rule for the
  Wi-Fi flow and the wrong one on a device whose link is BLE, where the page is
  only ever opened for the token — it left "type a fake network name" as the way
  through. It now refuses only a submission that would save nothing, the
  network field says it is optional, and the dropdown offers **none**: a
  `<select>` always submits something, so a BLE user setting a token was
  silently storing whichever network happened to top the scan.
- **Changed: the screens a fresh board actually shows name the missing token
  first.** `ble: start the daemon with --ble` and `check it is running with
  --ble` are both true and both send you to the wrong half of the daemon's log
  when the real answer is that this device has no secret yet. The status line and
  the NO LINK screen now say `send T|<token> over USB`, and the `A|NOTOKEN` note
  no longer points a BLE user at a portal it never opens.

### 2026-08-11 — BLE as a transport, and one gamepad switch instead of two rows

- **Added: the line protocol over Bluetooth LE** (`firmware/claude_mate_s3/blelink.h`,
  `daemon/blelink.py`), as an alternative to Wi-Fi for the battery build.
  Nothing about the protocol changes — the same `F|`/`V|`/`M|`/`P` down and
  `H`/`K`/`B|` up, the same nonce/HMAC handshake, the same token — so nothing
  above the wire knows which pipe it got. What changes is the shape of the idle
  cost: Wi-Fi holds an association open all day for a device whose status
  changes are rare and bursty, while BLE **advertises for 200 ms and then goes
  quiet for four seconds**, repeating, until the daemon connects. Those figures
  are the ones [issue #21](https://github.com/vlgutv22/claude_mate/issues/21)
  asked for, credited there to Jack Jansen in the ESP8266/ESP32 group, who
  measured roughly 5% of normal consumption with that rhythm on his own battery
  devices. The roles are **inverted** from the TCP transport and deliberately:
  there the device dials out because the daemon's address is stable and a DHCP
  device's is not; here the Mac scans, because the side with mains power and a
  real scheduler should be the one doing the looking. The device consequently
  needs no daemon address at all — no mDNS browse, no host field in the portal.
- **Changed: `SETTINGS → Link` picks the radio, live, with no reboot** — and it
  is a stored byte rather than a compile-time `#define`, which is the part of
  #21 that makes the rest usable: moving a device on a desk between transports
  should not need a toolchain. `I|WIFI` / `I|BLE` does the same over the cable.
  The old stack always goes down before the new one comes up, because the S3 has
  **one 2.4 GHz radio** and even a few hundred milliseconds of overlap is the
  contention the whole arrangement exists to avoid. Holding BOOT at power-on
  still forces the Wi-Fi setup portal whichever transport is stored — an escape
  hatch that could be locked out by the setting you were trying to fix would not
  be one.
- **Changed: the two gamepad rows became one switch.** *Game controller* and
  *BLE gamepad* sat next to each other on a 172 px screen offering what reads as
  the same thing twice. They were not the same thing, but the difference was a
  *transport detail* — precisely the kind of thing a menu must not ask someone
  to hold in their head. The HID one wins on every axis that matters from the
  device's side (no daemon, no Wi-Fi, no loopback, and it is the only input path
  a page served over **https** can use at all), so **BLE gamepad** is now a
  single **on/off** row and the daemon-driven mode survives only where it
  belongs: as something the Mac asks for over the link (`G|1`), never as a menu
  item.
- **Changed: the gamepad switch is remembered, across a screen sleep and across
  a reboot.** It was an *action* before — somewhere the device went — and since
  "power off" here is deep sleep and waking re-runs `setup()` from the top,
  every sleep quietly handed a paired controller back to being a conductor. You
  found out mid-level, with the Mac still listing "Claude Mate" as a connected
  gamepad that had stopped sending anything. It is now a stored preference: on
  means the device **is** a gamepad until you say otherwise, off means it is the
  conductor and the daemon link comes back. Two consequences worth stating
  because both are deliberate: the switch is written to NVS **immediately**
  rather than on the usual 1.5 s deferred timer (a cell pulled in that window
  could otherwise leave the switch and the device disagreeing across a power
  cycle), and **`G|0` no longer revokes it** — a browser tab closing on the Mac
  must not override a standing instruction from the person holding the device.
  The two-second hold of the 4th button, or the row itself, is what turns it off.
- **Added: `docs/POWER.md`**, and it is deliberately incomplete. The issue asks
  for the measured idle draw documented next to the Wi-Fi number, and **neither
  number has been taken** — so the table says so in as many words, states the
  expectation *before* measuring so it can be checked, and gives the method in
  enough detail that two people's readings can be compared (meter in the battery
  lead, backlight off, both transports back to back on the same cell). A power
  page full of plausible figures nobody measured is worse than an empty one,
  because it stops anyone from measuring. It is also honest about the ceiling:
  stopping the advertiser cuts the *radio's* share, not the CPU's — this
  firmware never light-sleeps, because the display, the LED engine and the
  button poll all want the loop running — and the backlight remains the budget.
- **Measured: the BLE transport costs 9,008 bytes of flash and 2,848 of RAM**,
  against the same sketch built without it (1,320,866 → 1,329,874 B; 55,536 →
  58,384 B). That is cheap because the BLE stack was **already linked in** for
  the HID gamepad: the ~290 KB was paid for once and this is the second thing to
  use it. The sketch sits at 42% of the 3 MB app partition.
- **Added: `tools/test_ble_link.py`**, which starts with the check the compiler
  cannot do: the service and two characteristic UUIDs live in two files, in two
  languages, and a typo in one produces a device that advertises forever and a
  daemon that scans forever with **neither side reporting anything wrong**. The
  rest drives the real `BleLink` against a fake `bleak` injected before the lazy
  import — the handshake and both ways of failing it, and line reassembly, since
  a notification boundary is *not* a line boundary and the device can pack two
  lines into one notification or split one across two. No radio, and no `bleak`
  install, so CI runs it unchanged.
- **Changed: `bleak` is an optional dependency and stays optional.** It is
  imported inside `--ble`, never at module scope, so a daemon without it starts
  and runs exactly as before and reports one clear line instead of a traceback.
  The daemon's only hard third-party requirement is still pyserial, which is a
  promise the README makes.
- **Changed: `LinkHub` holds a list of wireless transports** rather than a named
  TCP one. A Nano on a cable, a Wi-Fi board and a BLE board can all be attached
  at once and every one of them gets every frame — the fan-out deliberately does
  not short-circuit, since an `or` chain would stop at the first transport that
  took the bytes and leave the second device showing a frame from a minute ago.
- **Fixed, on hardware, four ways the link could die and stay dead.** All four
  were invisible to the test suite and to CI, and all four were found by
  flashing a board and using it:
  - **BLE does not come back in the same boot.** `BLEDevice::deinit(true)` does
    not fully release the controller and every `init()` after it fails —
    deterministically. The first cut swapped the stacks in place, which worked
    exactly once per boot in each direction, so flipping SETTINGS → Link twice
    (what anyone does while looking at a new setting) left the device with **no
    link at all**: no advertising, no Wi-Fi, no retry, and `ble : OFF` visible
    only over a cable a cordless device is probably not attached to. Anything
    that takes the BLE stack down now **reboots** to bring it back, behind a
    screen that says which link it is switching to. The transport is in NVS, so
    three seconds is the whole cost. The same applies to turning the gamepad off
    on a BLE build — the pad and the link are two roles on one controller.
  - **A failed start was silent and permanent.** `blelink.h` now remembers it
    was asked to be up, retries, and when the retries are spent says so
    (`stuck()`) so the sketch can reboot rather than sit there being nothing.
  - **`I|BLE` could not restart a dead stack**, because `setTransport()`
    returned early when the value had not changed — so the one command that
    looked like it should fix it was a no-op.
  - **The daemon never noticed a device that vanished.** bleak's
    `disconnected_callback` did not fire when the board rebooted, and the daemon
    went on believing it was connected **for ninety minutes** — `has_clients()`
    true, frames written into nothing, and no scan ever started again. A
    transport whose recovery depends on one notification it does not control has
    no recovery, so the session loop now asks `is_connected` every tick and
    counts consecutive failed writes, which is the same thing `NetLink` has
    always done from the other direction. Recovery measured at ~6 s.
  `tools/test_ble_link.py` gained a regression test for the last of those, which
  also meant giving the fake client an `is_connected` the real one has. One of
  its own checks turned out to be environment-dependent — "bleak missing" passed
  only because bleak happened not to be installed, and quietly stopped testing
  anything the day it was; it now blocks the import outright.

### 2026-08-08 — SHIP IT level 2: M2 · SCAFFOLDING, and the product manager

- **Added: level 2 of SHIP IT**, playable in the web edition through a new
  MILESTONE row on the start screen. It unlocks once M1 has shipped on any
  tier — the row says so, and refuses until then — and the M1 finish screen
  announces the unlock. The sizing is arithmetic rather than
  taste: the crossing is 126 tiles against level 1's 105 — **20% longer**, so
  the same per-step drain prices a straight run at 1.2× the tier's walk cost
  (the start screen now quotes the real number per level) — and hazard density
  rises about **5%** (18 enemies and 10 gaps over 126 tiles, against 14 and 8
  over 105). Per-hit costs per tier are unchanged, so a chosen difficulty
  stays the difficulty chosen. The shape is the one `docs/GAME.md` §4 promised
  for M2: rising streaks — scaffolds, catwalks, perches and one pyramid that
  cannot be walked around.
- **Added: the product manager**, the game's third enemy and its most
  expensive. It patrols its floor at 0.7× the tier's enemy speed and *chases
  on sight* at 1.35×, capped at 1.2 px/step — deliberately below the 1.45
  walk, because a meeting you cannot outrun is not a mechanic, it is a wall.
  It cannot be stomped, and a collision costs the tier's priority-change
  penalty **plus 2½ days** (4½/4½/5/5½/5½ across the tiers), a 60 px shove
  and 90 frames of invulnerability. It gets a codex card like everyone else.
  Knockbacks are now swept against terrain instead of teleporting — the
  review found that a raw 46 px shove from the priority change at level 2's
  col 112 could pass through the pyramid's wall into its sealed hollow, a
  pit no jump can leave. It turns out to fix **level 1** too, which ships:
  at the priority changes on cols 31 and 93, 18 of the 172 standing positions
  that register a hit used to shove the player *inside* the staircase block
  behind them — from x=484 the old shove landed at 438, buried in terrain,
  where the swept one stops at 447 flush against the wall. That is the main
  walking floor, so it was the ordinary case rather than a corner of the map.
- **Added: `tools/test_levels.py`**, the reachability checker the design doc
  demanded after level 1's first draft shipped with all seventeen PRs
  unreachable: a breadth-first search over standing positions using only the
  measured jump budget, run in CI for every level, failing if the flag or any
  pull request cannot be reached.
- **Changed: `tools/gen_level.py` now generates every level**, not just the
  first. Each firmware header owns its geometry and actors; the palette and
  sprites are merged game-wide (the character lives in `level_01.h`, the
  product manager in `level_02.h`) and emitted into every generated module,
  with a duplicate definition anywhere being a hard error.
- **Changed: the device engine plays the campaign too**, not just level 1.
  `ship_it.h` was bound to level 1 at *compile* time — `LVL1_MAP` inside
  `solid()`, `LVL1_COLS` in the camera clamp, `LVL1_BUG_COUNT` as an array
  size — and now reads every level through a campaign table, so nothing in the
  simulation or the renderer knows which level it is playing and level 3 is a
  header plus one row. Actor arrays are sized to the worst level at compile
  time, so switching milestone never allocates; `ROWS` and `TILE` are still
  shared by every level and now `static_assert` it. The device gets the same
  MILESTONE row, the same lock, the product manager and its codex card, the
  per-level walk cost (M2 quotes 6 days where M1 quotes 5, on SPRINT), and the
  swept knockback. The start screen fits a fourth row by moving the title block
  up 6 px and tightening the row pitch from 32 to 27.
- A host harness compiles that device engine and drives it with the panel and
  NVS stubbed, which is how the layout of a screen CI cannot see is checked at
  all: 1840 drawn strings across every screen, both levels and all five tiers,
  none of them off the 320×172 panel and none colliding on its own row. It also
  pins the two engines to the same pixel — the swept shove lands on 447 on
  level 1 and 1775 on level 2 in the browser *and* in the firmware.

### 2026-07-31 — A battery indicator that stops lying, and a menu that responds

- **Changed: three segments instead of a battery percentage** (3 green, 2 amber,
  1 red). Reported from the bench: unplug a full cell and the display falls from
  100 % to 70–80 % within minutes. Every number was honest and the display was
  still a lie — 4200 mV is the *charger's* constant-voltage point rather than the
  cell's charge, a Li-ion just off charge sheds surface charge over tens of
  minutes, and at ~9 mV per percent the ADC noise on an unfiltered divider is
  worth whole points. A number invites you to trust its last digit. Behind the
  segments: 60 mV of hysteresis (fall only — rise on touch), a 45-second hold so
  no Wi-Fi burst or backlight step can move it, and the EMA slowed from 1/8 to
  1/16. The exact percentage and millivolts stay in `?` and on the About page,
  because diagnosis wants the number and a glance wants the shape.
- **Fixed: the menu was close to unusable with no daemon.** `TCPClient::connect()`
  and `MDNS.queryService()` both block, and both are called from the loop that
  polls the buttons — in DIALING that was roughly three quarters of every second
  with the firmware unable to read a pin, so a press *and* its release could land
  inside one window and be dropped rather than delayed. Reconnection is now held
  while a firmware-local screen is up, backs off 2 s → 30 s while the daemon is
  absent, and the dial timeout dropped 1500 → 600 ms.
- **Added: "Mac sound" in settings**, with a new `O|<KEY>|<value>` device →
  daemon verb to carry it. The device has no speaker, so this is the one
  preference whose effect happens on the Mac; it is re-sent on every connect
  because the daemon keeps no per-device state. Unknown keys are ignored on both
  sides, so old and new interoperate in either direction.
- **The settings page scrolls**, since five rows fit and there are now six.

### 2026-07-30 — Wi-Fi setup stops being a dead end, and the Mac can beep

- **Fixed: an empty token box erased the stored token.** The setup portal wrote
  it unconditionally, so the obvious thing to do on a return visit — fill in the
  Wi-Fi password and nothing else — silently wiped the shared secret, after
  which the device could never complete the handshake and reported only that it
  was not connected. Empty now means **keep**; clearing is a deliberate
  checkbox, or `X|WIPE`. The old docs warned "re-enter it every time", which was
  an admission the behaviour was wrong rather than a fix.
- **Fixed: every connection failure looked identical.** `drop(why)` discarded
  its reason (`(void)why;`), so a wrong token, a missing token, a daemon that
  is not listening and a lost network all presented as the device quietly
  cycling back to `dialing…`. The reason now shows on the bottom line for 20 s.
- **Changed: `--tcp` generates a token instead of refusing.** Failing closed was
  aimed at the right danger — an *unauthenticated* listener — but caught the
  wrong thing: the portal asks for a token, and the only way to have one was to
  already know to create the file by hand. It now writes a 32-byte secret to
  `~/.config/claude-mate/token` (0600, create-exclusive, never overwriting an
  existing file) and prints it for typing into the portal. It still refuses when
  a token can neither be read nor created, which is the real fail-closed case,
  and `test_net_link` pins both halves (23 → 28 checks).
- **Added: `--sound`** (off by default). Plays a macOS alert sound on the same
  transition that drives the LED — one truth, now three renderings: rhythm,
  colour, sound. One-shot, never looping: light is ignorable and a beep is not,
  and an alert that repeated until acknowledged would be a smoke alarm. The
  device itself stays silent — the ESP32-S3 has no DAC, and the board's 3.3 V
  rail is a linear LDO with the backlight on a resistor-limited MOSFET, so there
  is no switching node any firmware trick could make audible.

### 2026-07-30 — A menu on the 4th button, and a screen that turns itself off

**ESP32-S3 only, and nothing outside its firmware changed** — no protocol
change, no daemon change, and the Nano build untouched. All of this is
firmware-local because none of it is about a session: it is about the piece of
hardware, which the daemon has no business knowing.

- **Added: three gestures on the 4th button.** A tap still opens the terminal
  mirror in the (now named) **CONDUCTOR** view; a **double-tap** opens an
  on-device menu; the 2 s hold still means long sleep. The tap is now deferred
  by 300 ms, because a first tap is not yet knowably a single one — a cost paid
  by the least latency-sensitive of the three on purpose, since the mirror
  already waits a daemon round-trip. 300 ms is the daemon's own
  `DOUBLE_CLICK_S`, so both double-taps on the device want the same rhythm.
- **Added: a menu** — CONDUCTOR · SETTINGS · ABOUT · WI-FI · SLEEP, as a
  horizontal strip rather than a list, because a 320×172 panel gives a list a
  fifth of its width and runs it out of height at five rows. While the menu is
  up PREV/GO/NEXT are handled on the device and never emitted, so the queue
  cannot move while you are aiming at a row. **ABOUT** puts the serial `?`
  readout on the glass, which is the only place it can be read on a cordless
  device with no console attached, and **WI-FI** starts the setup portal —
  previously reachable only by holding BOOT through power-on or sending `Z`.
- **Added: screen sleep**, off after 1/2/5/10/30 min and only when nothing is
  waiting on you, judged from the frame the device already has (flashing name
  row, any lowercase fleet letter, or a *looping* LED — the daemon drives that
  from exactly "worst unacknowledged alert"). An alert turns it back on. A
  `working` fleet deliberately does not hold it on, and neither does `NO LINK`:
  a dead daemon must not be able to burn the cell flat, which is what would
  happen the moment you carried the device out of Wi-Fi range. Only the
  backlight goes off — `displayOff()` would save another milliamp and put the
  wake path one command from the dead-panel failure this firmware already warns
  about twice. Defaults to **off**, so nobody's screen starts going dark because
  they took an update.
- **Added: settings**, in their own NVS namespace so a factory reset can choose
  what it destroys — screen sleep, backlight (5 steps, non-linear in duty
  because equal duty steps feel like one huge jump then four identical ones),
  alert LED with a genuine **off** (a 7 Hz red strobe is right at a desk and
  wrong in a bedroom, and the alert still arrives through the name row), flip
  screen, and factory reset. Writes are deferred 1.5 s, so holding NEXT through
  the brightness steps costs one flash write instead of five.
- **A press on a dark screen only wakes, and is swallowed.** Phone convention,
  but for a specific reason: GO raises a terminal window, so obeying a press
  aimed at a screen you cannot read would occasionally yank you to the wrong
  session — the exact thing this device exists to prevent.

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
