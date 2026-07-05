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

python3 tools/test_e2e.py             # fake Arduino over a PTY + a fake PTY-wrapper
                                      # ctrl socket: drives real hook lines through
                                      # the socket, presses fake buttons (B|P/N/G/K),
                                      # and asserts ~30 checks — the
                                      # F|flash|r0|r1|r2|r3 frame protocol (four
                                      # size-1 rows + the packed letter fleet
                                      # strip), the V|<KIND> LED lines (incl.
                                      # handshake re-arm), the PREV/NEXT queue walk,
                                      # the GO/ACK triage sweep (GO focuses exactly
                                      # the shown session — WYSIWYG — then snaps to
                                      # the next alert; B|K acks without focusing;
                                      # last ack -> V|OFF), sibling-name
                                      # disambiguation, and the window-op invariants
                                      # (navigation sends ZERO window ops; GO sends
                                      # exactly one 'focus'; 'collapse' is never
                                      # sent). Nothing is launched on your Mac:
                                      # open/code/osascript are stubbed.

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

- **Cycles a 4-row demo frame** (name / state+time / model+effort /
  position+fleet letters) through `ERR → WAIT → DONE → WORK → IDLE` on the OLED
  every ~3 s in the real interface layout, printing each one over serial, and
  **plays the matching LED pattern on D8** on each change (ERROR strobe, INPUT
  blink, DONE cascade, START one-shot, dark on IDLE) → display + LED OK.
- The **OLED** lights up and shows the frame (the alert name row flashing) → software
  I2C (SDA A4 / SCL A3) + display OK (if blank, try address **0x3D**; if it's
  a 1.3" panel, you may need the **SH1106** driver — see [WIRING.md](WIRING.md)).
- Pressing **GO (D2)**, **NEXT (D3)**, and **PREV (D4)** is printed over serial
  → buttons OK. (The standalone selftest uses simple press events; the
  long-press / auto-repeat behavior lives in the real firmware, Level 2.)

This validates OLED + indication LED + buttons with **no Mac software**.

Pass criterion: the OLED cycles the demo frames in the real interface layout
(four size-1 rows — name / state+time / model+effort / position+fleet letters,
the alert name row flashing), the D8 LED
plays the matching pattern on each change (ERROR strobe / INPUT blink / DONE
cascade / START one-shot / dark on IDLE), and all three buttons print their
press lines (GO D2 / NEXT D3 / PREV D4).

---

## Level 2 — Serial protocol by hand (Arduino Serial Monitor)

Flash the **real firmware**. Open the Arduino IDE **Serial Monitor** at
**115200 baud**, line ending **Newline (`\n`)**.

1. On connect/reset you should see the board emit:

   ```
   H
   ```

2. Now **send** lines by hand and watch the display. Send a full frame
   (`F|<flags>|<sel>|<r0>|<r1>|<r2>|<r3>`):

   ```
   F|1|4|api-server|WAIT  0:42|Opus 4.8  xhigh|1/3 B W D
   ```

   → the OLED shows the frame as four size-1 rows: the name `api-server` on r0
   **flashing** (`flags` bit0 = 1), the state + time on r1, the model + effort
   on r2, and the position + space-separated fleet letters on r3 with the
   letter at column `4` (the `B`) in a **wide centred filled rectangle** (a lit
   block, letter knocked out). Add bit1 to `flags` (send `3` instead of `1`)
   and a ► FOLLOW marker appears by the state row; send `flags` `0` and the
   flashing stops. Send a letter lowercase (e.g. `1/3 b W D`) and it blinks
   (an unacked alert).

   Blink the LED with `V|<kind>` and watch each pattern:

   ```
   V|START     one long 1 s blink, then dark (one-shot)
   V|INPUT     aggressive even blink (~2.8 Hz) that LOOPS — V|OFF stops it
   V|ERROR     fast strobe (~7 Hz) that LOOPS — V|OFF stops it
   V|DONE      4-blink cascade + pause that LOOPS — V|OFF stops it
   V|OFF       stops any looping/playing pattern immediately
   ```

   Confirm the loops keep repeating until you send `V|OFF`, and that with no
   serial at all a loop self-stops after ~30 s **and the display switches to
   the LINK LOST screen** (the daemon-silence watchdog); any typed line brings
   the frame back.

3. Send a ping:

   ```
   P
   ```

   → the board replies `K` (the keepalive ack; `H` is sent only on boot/reset).

4. Press the physical buttons and confirm the Serial Monitor prints:

   ```
   B|G     ← GO (D2), short press
   B|K     ← GO (D2), held ≥ ~0.5 s (long press)
   B|N     ← NEXT (D3); repeats ~5/s while held
   B|P     ← PREV (D4); repeats ~5/s while held
   ```

   Also confirm the panel does a quick ~80 ms invert blip on every press.

Pass criterion: `H` on boot, `F|` frames render (flash flag flashes the name
row), the LED patterns (`V|`, incl. the looping kinds stopped by `V|OFF`)
play from typed lines, LINK LOST appears after ~30 s of silence, buttons emit
`B|G`/`B|K`/`B|N`/`B|P` with auto-repeat on held PREV/NEXT.

> When done, **close the Serial Monitor** so the daemon can use the port.

---

## Level 3 — Daemon in mock mode

Run the daemon with fake sessions — no Claude, no hooks needed:

```sh
python3 $REPO/daemon/claude_mate_daemon.py --mock
```

`--mock` injects a few fake sessions that cycle through states (including a
`waiting` session, so all four words appear). Watch the real hardware:

- The **screen** auto-surfaces the most-urgent unacked session (an unacked
  `error` first, then `waiting`, then `done`) at its stable position; its name
  row **flashes** while the alert is unacknowledged. As the mock states cycle,
  the surfaced subject follows the worst unacked alert — but the tab **order**
  (the fleet strip) stays alphabetical and never shuffles.
- The **LED** plays the pattern of the worst unacked class (`V|ERROR` strobe >
  `V|INPUT` blink > `V|DONE` cascade), looping until acknowledged; a calm
  `V|START` blink fires when a session starts working with nothing else pending.
- Press **NEXT** / **PREV** → the selection steps down/up the stable
  (alphabetical) order (wraps); the screen then stays where you put it for
  ~10 s after your last press.
- Press **GO** → the daemon acknowledges the session **shown on the glass**
  (WYSIWYG) and attempts to raise its window (in mock, watch the daemon log for
  the focus call); the display **stays on that tab** (no auto-switch).
- **Hold GO** (~0.5 s) → acknowledges WITHOUT any window op; the flashing and
  the LED loop stop, and the display stays on the tab. (After ~10 s idle the
  view auto-surfaces the next most-urgent unacked alert, at its stable position.)
- **Double-click GO** → toggles FOLLOW mode (a ► marker appears); PREV/NEXT
  then also raise the selected terminal after it settles.

Also confirm robustness: **unplug** the Nano mid-run — the daemon should not
crash — then **replug**; it should reconnect and (via the `H` handshake) restore
the frame AND re-arm any active LED loop.

Pass criterion: queue-ordered display + correct LED class + buttons handled +
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
and watch the frame and the LED update accordingly:

- `error` → `ERR` frame, name row flashing + a looping `V|ERROR` strobe
  (until you GO / hold GO / it clears).
- `waiting` → `WAIT` frame, flashing + a looping `V|INPUT` blink.
- `done` → `DONE` frame, flashing + a looping `V|DONE` cascade.
- `working` (nothing else pending) → `WORK` frame, steady + one-shot `V|START`.

Pass criterion: feeding socket lines drives the frame + LED per
[PROTOCOL.md](PROTOCOL.md).

---

## Level 5 — Real Claude hooks (last)

Finally, exercise the whole pipeline with Claude Code. With the daemon running
and the hooks merged (see [INSTALL.md](INSTALL.md)):

1. Open a Claude Code session (VS Code extension or CLI) and **submit a prompt**
   (`UserPromptSubmit`) → a `WORK` frame appears and a one-shot `V|START`
   blink fires (if nothing else needs you).
2. Trigger a **Notification** (e.g. a permission prompt) → the session
   auto-surfaces as a flashing `WAIT` frame and the `V|INPUT` blink **loops**
   until you GO / hold GO / answer in the terminal.
3. Let a turn **complete** (`Stop`) → a flashing `DONE` frame and a `V|DONE`
   cascade that **loops** until acknowledged.
4. Cause an **API error** (`StopFailure`) → a flashing `ERR` frame and a
   `V|ERROR` strobe that **loops** until acknowledged. (`StopFailure` fires
   instead of `Stop` on API errors — they never both fire.)
5. Open multiple sessions and confirm the queue orders them unacked-alerts
   first (`error` → `waiting` → `done`, oldest first), then the rest by class;
   the fleet strip on the bottom row shows one letter per session.
6. Press **GO** on a shown session → its terminal window is **raised** (wrapper
   sessions) or VS Code focuses via the deep link / workspace fallback (see
   the focus Limitations in [ARCHITECTURE.md](ARCHITECTURE.md)). Confirm
   navigation with **NEXT**/**PREV** moves NO windows — ever.
7. End a session → it disappears; with zero sessions the display shows
   `MATE / no sessions`.

Pass criterion: real hook events drive the frames and LED correctly, GO raises
the right session, and navigation never touches a window.

---

## Quick troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| Daemon: "could not open port" / "resource busy" | Serial Monitor (or another daemon) still holds the port. Close it. |
| No device in `ls /dev/cu.*` | Bad cable, missing CH340 driver, or no power. |
| OLED blank | Wrong I2C address (try 0x3D) or SH1106 1.3" panel needs the SH1106 driver. |
| Display blanks ~1.5 s when daemon reconnects | Expected — USB open resets the Nano; the `H` handshake restores state. |
| No WAIT frame on a question | Check the `Notification` hook is wired and the daemon is running. |
| LED never blinks | Alerts come from `V|<kind>`. Confirm a session actually transitioned (start/wait/done/error). Test the LED directly by typing `V|ERROR` in the Serial Monitor (Level 2). Check the D8 wiring (LED + series resistor to GND, see [WIRING.md](WIRING.md)). |
| LED blinks forever | It loops until acknowledged: GO (raise) or hold GO (ack only) sends `V|OFF`. A loop also self-stops ~30 s after the daemon goes silent (and LINK LOST appears). |
| Buttons do nothing in the daemon | Confirm Level 2 first (`B|G`/`B|N`/`B|P` over serial, `B|K` on a held GO), then check the daemon's button thread/logs. |
| Screen shows LINK LOST | The daemon has been silent ~30 s: check `launchctl list \| grep claudemate` and `~/Library/Logs/claude-mate.err.log`. Any daemon line clears the overlay. |
