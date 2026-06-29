# Claude Mate — Testing

Test **bottom-up**. Each rung isolates one layer so that when something breaks,
you know exactly where. Start at level 0 and only move up once the current rung
passes.

```
(5) real Claude hooks        <- highest: end-to-end with Claude Code
(4) tools/feed.sh fake hook
(3) daemon --mock
(2) serial protocol by hand
(1) selftest sketch
(0) port shows up            <- lowest: is the device even there?
```

> **The one gotcha that bites everyone — port is exclusive.**
> The Arduino IDE **Serial Monitor** and the **daemon** cannot both hold the
> serial port at the same time. Level 2 uses the Serial Monitor; levels 3–5 use
> the daemon. **Close the Serial Monitor before running the daemon, and stop the
> daemon (or its LaunchAgent) before opening the Serial Monitor.** A "port busy" /
> "resource busy" / "could not open port" error almost always means the other one
> still has it.

---

## Level −1 — Automated tests (no hardware, no Claude)

Two self-contained tests exercise the Mac side before you touch any hardware:

```sh
pip install pyserial                  # one-time, for the e2e test

python3 tools/test_e2e.py             # fake Arduino over a PTY: drives real
                                      # hook events through the socket and asserts
                                      # the status word (D|FREE / D|WIP /
                                      # D|BLOCKED / D|WTF), carousel cards, and the
                                      # FOCUS deep-link (nothing is launched; the
                                      # vibration motor lives only on the Arduino)

python3 tools/test_settings_merge.py  # proves the installer's settings.json merge
                                      # preserves your existing config and is idempotent
```

Both print a `PASS`/`FAIL` checklist and exit non-zero on failure, so they double
as CI smoke tests. They never touch your real `~/.claude` or serial ports.

---

## Level 0 — Port shows up

Plug the Nano into the Mac, then:

```sh
ls /dev/cu.*
```

You should see a `/dev/cu.usbserial*` or `/dev/cu.usbmodem*` entry. That is the
device the daemon will auto-detect.

- **Nothing appears?** Try another (data-capable) USB cable, another port, and
  confirm the board has power. Some clone Nanos need the **CH340** USB-serial
  driver installed on macOS.

Pass criterion: a `cu.*` device appears (and disappears when you unplug).

---

## Level 1 — Selftest sketch

Confirm the **hardware** works independently of the protocol. Flash the selftest
sketch (`firmware/selftest/selftest.ino`) and watch the board. On boot it now:

- **Cycles the word** through `FREE → WIP → BLOCKED → WTF` on the OLED every ~2 s,
  printing each one over serial, and **buzzes the vibration motor** on each change
  (WTF = 3 pulses, BLOCKED = 2, FREE = 1 short tick, WIP = silent) → display +
  motor driver OK.
- The **OLED** lights up and shows text + the current word → I2C + display OK
  (if blank, try address **0x3D**; if it's a 1.3" panel, you may need the
  **SH1106** driver — see [WIRING.md](WIRING.md)).
- Pressing **FOCUS (D2)**, **NEXT (D3)**, and **PREV (D4)** is printed over serial
  → buttons OK.

This validates OLED + vibration motor + buttons with **no Mac software**.

Pass criterion: the OLED cycles all four words, the motor buzzes the right pulse
count on each change, the screen shows text + word, and all three buttons respond.

---

## Level 2 — Serial protocol by hand (Arduino Serial Monitor)

Flash the **real firmware**. Open the Arduino IDE **Serial Monitor** at
**115200 baud**, line ending **Newline (`\n`)**.

1. On connect/reset you should see the board emit:

   ```
   H
   ```

2. Now **send** lines by hand and watch the display + motor. Set the word:

   ```
   D|WTF
   ```

   → the OLED shows the big word **WTF** and the motor buzzes **3 pulses** (this
   is a transition into WTF). Try `D|BLOCKED` (2 pulses), `D|FREE` (1 short
   tick), and `D|WIP` (silent) — each redraws the word, and the buzz fires only
   when the word actually changes.

3. Send a full session card:

   ```
   S|1|1|demo|error|03:21|71%
   ```

   → the OLED shows the card: name `demo`, state `error`, runtime `03:21`,
   limit `71%`, position `1/1`.

4. Send the idle screen and a ping:

   ```
   I
   P
   ```

   → idle screen; `P` may be ignored or answered with `H`.

5. Press the physical **FOCUS**, **NEXT**, and **PREV** buttons and confirm the
   Serial Monitor prints:

   ```
   B|1
   B|2
   B|3
   ```

Pass criterion: `H` on boot, cards render and the word + buzz update from typed
lines, button presses emit `B|1` / `B|2` / `B|3`.

> When done, **close the Serial Monitor** so the daemon can use the port.

---

## Level 3 — Daemon in mock mode

Run the daemon with fake sessions — no Claude, no hooks needed:

```sh
python3 $REPO/daemon/claude_mate_daemon.py --mock
```

`--mock` injects a few fake sessions that cycle through states (including a
`waiting` session, so all four words appear). Watch the real hardware:

- The **carousel** rotates through cards roughly every 3 seconds.
- The **status word** follows the logic: **WTF** when any session is `error`,
  **BLOCKED** else if any is `waiting`, **WIP** else if any is `working`, **FREE**
  otherwise. It should visit all four words as the mock states cycle, with the
  motor buzzing on each change.
- Press **NEXT** → the carousel advances immediately and pauses ~10 s.
- Press **PREV** → the carousel steps back one card and pauses ~10 s.
- Press **FOCUS** → the daemon attempts to focus that card's session (in mock,
  watch the daemon log for the focus call).

Also confirm robustness: **unplug** the Nano mid-run — the daemon should not
crash — then **replug**; it should reconnect and (via the `H` handshake) restore
the display.

Pass criterion: live carousel + correct status word + haptic + buttons handled +
survives an unplug/replug.

---

## Level 4 — Fake hook line via tools/feed.sh

Now test the **socket → daemon** path without Claude. Run the daemon **without**
`--mock` (so it listens on the socket), then feed it a fake hook line:

```sh
python3 $REPO/daemon/claude_mate_daemon.py    # in one terminal

$REPO/tools/feed.sh working abc123 demo        # in another
```

`tools/feed.sh` writes one newline-terminated line to `/tmp/claude-mate.sock` in
the socket format `<state>|<session_id>|<name>` (here:
`working|abc123|demo`). Try each state — `working`, `waiting`, `done`, `error` —
and watch the card and the status word update accordingly:

- `error` → **WTF** (3-pulse buzz).
- `waiting` (and nothing errored) → **BLOCKED** (2-pulse buzz).
- `working` (and nothing waiting/errored) → **WIP** (silent).
- `done` / nothing pending → **FREE** (1 short tick).

Pass criterion: feeding socket lines drives the card and the status word + haptic
per [PROTOCOL.md](PROTOCOL.md).

---

## Level 5 — Real Claude hooks (last)

Finally, exercise the whole pipeline with Claude Code. With the daemon running
and the hooks merged (see [INSTALL.md](INSTALL.md)):

1. Open a Claude Code session (VS Code extension or CLI) and **submit a prompt**
   (`UserPromptSubmit`) → word changes to **WIP** (silent), a card appears as
   `working`.
2. Trigger a **Notification** (e.g. a permission prompt) → word changes to
   **BLOCKED** (2-pulse buzz), card shows `waiting`.
3. Let a turn **complete** (`Stop`) → card shows `done`; word returns to
   **FREE** (1 tick) if nothing else needs you.
4. Cause an **API error** (`StopFailure`) → card shows `error`, word to **WTF**
   (3-pulse buzz). (`StopFailure` fires instead of `Stop` on API errors — they
   never both fire.)
5. Open multiple sessions and confirm the carousel orders them most-urgent-first
   (`error` → `waiting` → `working` → `done` → `idle`) and the OLED shows the
   highest-priority word (WTF > BLOCKED > WIP > FREE).
6. Press **FOCUS** on a card → VS Code focuses that session via the deep link, or
   raises the workspace window as a fallback (see the focus Limitations in
   [ARCHITECTURE.md](ARCHITECTURE.md)). Use **NEXT** / **PREV** to step cards.
7. End a session (`SessionEnd`) → it drops to `idle` / disappears; with zero
   sessions the display shows the idle screen and the word returns to **FREE**.

Pass criterion: real hook events move the cards and the status word correctly and
FOCUS brings up the right session.

---

## Quick troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| Daemon: "could not open port" / "resource busy" | Serial Monitor (or another daemon) still holds the port. Close it. |
| No device in `ls /dev/cu.*` | Bad cable, missing CH340 driver, or no power. |
| OLED blank | Wrong I2C address (try 0x3D) or SH1106 1.3" panel needs the SH1106 driver. |
| Display blanks ~1.5 s when daemon reconnects | Expected — USB open resets the Nano; the `H` handshake restores state. |
| Word never reaches BLOCKED on a question | Check the `Notification` hook is wired and the daemon is running. |
| Motor never buzzes | It buzzes only on a word *change*; confirm the word actually changed. Check the D5 driver wiring (module IN / NPN base via 1k / ULN2003 IN1), common ground, and the flyback diode (see [WIRING.md](WIRING.md)). |
| Motor buzzes weakly or not at all | D5 can't drive the motor directly — it needs the transistor/module/ULN2003 channel; verify the motor is on the 5 V rail, not a GPIO. |
| Buttons do nothing in the daemon | Confirm Level 2 first (`B|1`/`B|2`/`B|3` over serial), then check the daemon's button thread/logs. |
