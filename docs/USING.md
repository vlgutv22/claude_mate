# Claude Mate — the manual, 0 → hero

Read it in order and you go from an empty machine to a working desk companion.
Every section stands alone if you already have the one before it.

**1.** [Install](#install-one-command) — one command · **2.** [The daemon](#the-daemon)
— what it is · **3.** [The CLI](#the-cli) — every command · **4.**
[The device](#the-device) — every button · **5.** [Connect one](#connecting-a-device)
· **6.** [When it is wrong](#when-something-is-wrong)

Two ways to drive the same thing: **the device** and **`claude-mate`** in a
terminal. They are not two implementations — a terminal command and a button
press arrive at the same code, so anything true of one is true of the other.
**You need no hardware for any of this except section 4 and 5**: the CLI is the
same interface.

---

## Install (one command)

```sh
git clone https://github.com/vlgutv22/claude_mate.git && cd claude_mate
./install/install.sh --yes
```

That does all of it: installs the status hook into `~/.claude/hooks/`, merges the
hooks block into `~/.claude/settings.json` (backing it up first), installs and
starts the **LaunchAgent** so the daemon runs at login, and puts `claude-mate`,
`claude-mate-connect` and `claude-mate-switch` on your `PATH`.

Leave off `--yes` and it asks before touching `settings.json` — that is the only
question it has. It is idempotent: re-run it after a `git pull`.

**Then feed it.** Either alias `claude` to the PTY wrapper, which is what gets
you model, effort, account and limit on the screen:

```sh
echo 'alias claude="'"$PWD"'/bin/claude-mate-wrap"' >> ~/.zshrc && exec zsh
```

…or rely on the hooks alone, which give you states but not the extras.

**Check it:**

```sh
claude-mate              # your sessions
claude-mate-connect      # every link, with a verdict
```

Uninstall with `./install/uninstall.sh` — it removes the agent, the hook and its
own symlinks, leaves your `settings.json` alone, and tells you which block to
delete.

---

## The daemon

One background process. It keeps the **triage queue** — every Claude Code
session, stable and alphabetical, so tabs never shuffle as their states change —
and renders one screen: the selected session over a whole-fleet letter strip.

It runs from a LaunchAgent, so it starts at login and restarts if it dies.

```sh
launchctl list | grep claudemate                       # is it up?
launchctl kickstart -k gui/$UID/com.claudemate.daemon  # restart it
tail -f ~/Library/Logs/claude-mate.err.log             # what it is doing
```

Configure it with environment variables in the plist
(`~/Library/LaunchAgents/com.claudemate.daemon.plist`) rather than flags — the
installer writes that file and there is a `CLAUDE_MATE_*` twin for every flag.
See the table in the [README](../README.md#configuration).

**States**, which are the whole point:

| | |
|---|---|
| `working` | a turn is running |
| `waiting` | it is asking you something — **needs you** |
| `error` | the turn failed — **needs you** |
| `done` | it finished — **needs you** |
| `idle` | nothing in flight |

The three that need you keep the LED going and the name flashing **until you
acknowledge them**. That is the only thing this project really does.

---

## The CLI

Four commands ship, and [CLI.md](CLI.md) is the full reference for all of them.
This page covers the one you use constantly.

### The app

```sh
claude-mate
```

On a terminal, with no arguments, you get an app rather than a printout:

```
 claude-mate   ● daemon running · 14:22:31

 SESSIONS
  ▸• api-server   working    1:23   Opus 5  xhigh  work
    web-ui        waiting    0:05   Opus 5         needs you
    docs          idle      12:40

  ▸  Start new session
     Start new session · skip permissions
     Device · link status and pairing
     Accounts · switch or review
     Quit

 ↑↓ move   ⏎ raise this session   a ack   f follow   m mirror   c continue
 r restart daemon   q quit
```

Arrow keys move the cursor, Enter chooses, `q` quits. On a session row, Enter
raises that session's terminal — the same thing the device's GO button does —
and `a` / `f` / `m` / `c` are acknowledge, follow, mirror and continue.

**Start new session** hands this terminal to `claude`, run through the wrapper
so the daemon sees it. The second entry adds `--dangerously-skip-permissions`;
it is a separate entry rather than a setting because that is a decision worth
naming out loud. Put anything else you want in `CLAUDE_MATE_NEW_ARGS`.

If the daemon is not running, the app says so and `r` starts it.

**Not an app on a pipe.** Redirect it and you get the plain print below, with no
colour and no escape codes — hooks, scripts and cron are unaffected.

### The plain print

```sh
claude-mate status              # the queue, once
claude-mate watch               # ...and keep printing it
```

```
   0  aladdin      idle      6:54  work 2  5h94%
   1  api-server   waiting   0:42  Opus 5  xhigh  work   <-- needs you
 > 2  claude_mate  working   6:55  xhigh  default  5h97%
```

`>` marks the row that is **on the device's glass** — the one a press acts on.
`<-- needs you` is an unacknowledged alert. The right-hand columns are model,
effort, account and how much of that account's plan limit is left.

### Every command

| Command | Button | What it does |
|---|---|---|
| `claude-mate` | — | print the queue and exit |
| `watch` | — | reprint it every second until Ctrl-C |
| `next` / `prev` | NEXT / PREV | step the selection (or scroll the mirror, if it is open) |
| `go` | GO | acknowledge **and raise that session's terminal window** |
| `ack` | GO, held | acknowledge only — leave your windows alone |
| `follow` | ACK, held | toggle FOLLOW: `next`/`prev` then also raise as they move |
| `mirror` | 4th, tapped | toggle the live terminal mirror on the device |
| `continue` | — | type `continue` into that session |
| `new-terminal` | — | open a terminal in that session's directory |
| `select <n\|name>` | *(none)* | point at a row instead of stepping to it |
| `accounts` | — | the saved logins |
| `accounts rm <n\|name>` | — | delete one |

**`select` is the only command the device has no equivalent for** — it walks
there with PREV/NEXT. It takes an index from the listing, or a name: exact first,
then a unique prefix, then a unique substring. **An ambiguous name is refused,
not guessed** — this moves what `go` acts on, and raising the wrong window is the
one mistake worth being pedantic about.

### It presses the same buttons

`claude-mate go` sends `press|G` to the daemon, which hands it to the **same**
`ButtonReader` that handles `B|G` off the wire. So every rule applies in both
places: PREV scrolls the mirror rather than moving the selection while the mirror
is open, GO closes the mirror before raising a real window, and a browser page
holding the gamepad grab swallows presses from either source.

There is no second implementation of GO to drift.

### Accounts

```sh
claude-mate accounts
```

```
  0  default        you@example.com
  1  work           you@work.example
  2  '\x1b'         someone@example.com
```

Odd names print as reprs, and that is not decoration: anything typed at the
account picker used to become a profile, and an **arrow key types an escape
sequence** — so real machines have a profile directory named `\033` that prints
as a blank row. The picker now *offers* to create a new name rather than assuming
it, but existing ones stay listed so you can see and remove them.

```sh
claude-mate accounts rm work     # or `rm 2` — an index, for names you cannot type
```

It shows you the name, the email and the directory, then asks you to **type the
name back**. A y/n on a destructive action is a reflex; typing the name is a
decision, and it also proves you are deleting the one you think you are. There is
no undo: the profile's login and every transcript under it go with it.

`default` is your `~/.claude` login rather than a profile, and this command will
not delete it.

For switching a live conversation to another account — and for remaining-limit
numbers — use `claude-mate-switch`.

> **Why deletion is not a daemon command.** The daemon's socket is `chmod 0600` — the user's own,
> which is all a hook needs, since hooks run in the user's own shells. It was
> `0666` until a review pointed out that this branch had grown it from
> state-updates-only into a command channel any local process could type into.
> Even at `0600`, deletion stays out: a hook firing a malformed line should not
> be able to remove a login. So `accounts rm` deletes in the CLI's own process
> and only asks the daemon to re-read afterwards.

---

## The device

Four buttons. **PREV · GO · NEXT**, and a 4th with three gestures.

| | |
|---|---|
| PREV / NEXT | step the queue (hold to repeat) |
| GO | acknowledge + raise that session's terminal |
| GO, double | toggle **FOLLOW** — a `►` appears, and PREV/NEXT then raise as they move |
| GO, held | acknowledge only |
| 4th, tap | the live **terminal mirror** of the selected session |
| 4th, double | the **menu** |
| 4th, held 2 s | sleep (any button wakes it) |

**The screen** shows the selected session's name, its state and time, model and
effort, account and remaining limit, and a letter per session across the fleet —
`E` error, `B` waiting, `W` working, `D` done, `I` idle. A letter **blinks**
while that session's alert is unacknowledged. The active tab's letter sits in a
filled box.

**The LED** plays a distinct rhythm per alert class and keeps playing until you
acknowledge. Colour is a second channel, not the only one — the rhythms differ
too, and the brightness levels differ, so it reads in sunlight and to a
red/green-colourblind eye.

### The menu (4th button, double-tap)

Four items: **CONDUCTOR** (back to triage), **SETTINGS**, **SHIP IT** (a
platformer that runs on the device), **SLEEP**.

**SETTINGS** is eight rows:

| Row | |
|---|---|
| BLE gamepad | the device becomes an ordinary Bluetooth HID gamepad, across reboots, until you turn it off |
| Sleep screen | off · 1m · 2m · 5m · 10m · 30m |
| Brightness | five steps, applied live |
| Alert LED | off · low · med · high — **off is genuinely dark** |
| Mac sound | an alert sound *on the Mac*, since the device has no speaker |
| Flip screen | applies on restart, and the row says so |
| About | which radio, whether it found the daemon, battery, boot cause, firmware |
| Factory reset | wipes Wi-Fi, token **and** settings — asks twice, the second a long press |

**Nothing to do with Wi-Fi is on that menu**, deliberately. A radio switch one
press away moved a cordless board onto Wi-Fi, and a board with no credentials
then rebooted into a setup portal that outranks the menu — hiding the row you
would have used to undo it. The switch lives behind a cable you have to
physically have: `claude-mate link <wifi|p2p|ble>`, or `I|WIFI` / `I|BLE` /
`I|P2P` typed at the USB console directly.

---

## Which radio carries the link

Three transports, one protocol. The daemon cannot tell them apart, and a device
can use exactly one at a time — the S3 has a single 2.4 GHz radio.

| | what it needs | when to use it |
|---|---|---|
| `ble` | nothing | the default. A cordless device on a desk, no network involved |
| `wifi` | the SSID and password of a network you and the Mac both trust | reach across a whole home or office |
| `p2p` | **nothing** | Wi-Fi's range and speed where there is no network you can or want to join |

Switch with the cable plugged in:

```sh
claude-mate link p2p        # the device reboots into it, ~3 s
```

### P2P: the device *is* the network

In `p2p` the ESP32-S3 stops being a client and becomes the access point. Your
Mac joins **the device's** network; the device reads the Mac's DHCP lease off
its own AP and dials the daemon on port 8787. From there it is the same nonce
handshake and the same line protocol as `wifi` — the daemon does not know the
difference.

Nothing else is on that segment: no router, no other clients, and no network
credentials stored on a keypad. That is the point of it — a guest SSID, a
corporate network you cannot put a device on, a hotel, a conference, or simply
not wanting a desk toy to hold your Wi-Fi password.

`claude-mate link p2p` prints the network name and password, and the device
shows both on its screen while it waits. Join it from the Mac once; macOS
remembers it and rejoins at every login, because unlike the setup portal the
P2P credentials are generated once and kept.

> **Your Mac has one Wi-Fi radio.** While it is on the device's network it is
> not on yours — no internet over Wi-Fi until you switch back, unless the Mac is
> also on Ethernet. This cannot be fixed from the device's side; it is the price
> of a link with no infrastructure in it. If the Mac needs to stay on your
> network, use `ble`.

`p2p` needs the daemon's TCP listener, which the shipped LaunchAgent already
enables (`CLAUDE_MATE_TCP=1`). The device must also already have the token — it
gets one automatically the first time it is plugged in.

---

## Connecting a device

An unprovisioned board comes up on **BLE**, advertising, with no token, and says
so on its screen. Give it the daemon's token in whichever of these suits:

**With a cable — nothing to do.** The daemon hands its token to any device that
appears on USB, so a board provisions itself a few seconds after being plugged
in.

**Cordless — one command and one button.**

```sh
claude-mate-connect --pair       # or press `d` at the account picker
```

The device shows **PAIR THIS DEVICE? / GO = ACCEPT**. Press GO. You get `PAIRED`
on the glass and `✓ PAIRED` / `✓ LINKED` in the terminal.

The device is the one that asks *you*, and that is what makes it safe on an open
radio: being in range gets an attacker a prompt on a screen they cannot reach and
nothing else. The token is sent only after the button, once, and the approval
dies with the connection. The cost, stated plainly: **the token crosses the air
in the clear inside that window** — the link is unencrypted by design, but a
sniffer listening at that second learns it. Pair away from hostile radio, or use
the cable.

> **The device will never appear in System Settings → Bluetooth, and that is
> correct.** macOS lists Classic and pairable devices; this is an unpaired GATT
> peripheral advertising in 200 ms bursts every 4 s, with no pairing step by
> design. Only a CoreBluetooth scan sees it, which is what the daemon does — its
> log is the only place the Mac can answer *"is it advertising?"*. (Gamepad mode
> is a real HID device and does appear there. Different role, same board.)

Full detail, both ends: [`firmware/README.md`](../firmware/README.md) and the
`E|` handshake in [`PROTOCOL.md`](PROTOCOL.md#enrolment).

---

## When something is wrong

**Start here.** It checks every link and prints the shortest fix for whichever
one is down:

```sh
claude-mate-connect
```

| Symptom | It is |
|---|---|
| everything reads `IDLE`, or nothing at all | the daemon is down, or nothing has posted yet — `launchctl list \| grep claudemate` |
| the device is dark | screen sleep. Any button wakes it |
| `no token: claude-mate-connect` on the glass | it has never been given a token — pair it, or plug it in |
| `x token rejected` | the two ends hold different tokens. Plug it in; the cable corrects it |
| `ble: start the daemon with --ble` | the device is fine and waiting; the daemon has BLE off |
| the daemon says `no device found yet` while the device says it is advertising | macOS Bluetooth permission for the daemon's Python, in **Privacy & Security → Bluetooth** |
| no model/effort/account on the screen | those come from the PTY wrapper — alias `claude` to `claude-mate-wrap` |
| the board is not on `/dev/cu.usbmodem*` | it is on battery, or asleep. A button wakes it |

**Rotating the token** — if it has been in a screenshot or a log:

```sh
rm ~/.config/claude-mate/token
launchctl kickstart -k gui/$UID/com.claudemate.daemon   # mints a new one
```

Then plug the device in, and the cable re-provisions it in seconds.
