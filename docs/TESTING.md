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

> **Which device?** The ladder below is written for the Arduino Nano, the
> reference implementation. It applies unchanged to the ESP32-S3 except that you
> flash with [`firmware/flash_s3.sh`](../firmware/flash_s3.sh) rather than
> `arduino-cli upload` (that board needs three separate workarounds — see
> [`firmware/README.md`](../firmware/README.md)), and there is a fourth button.
>
> **The automated tests drive the daemon over a serial PTY, so the S3's TCP
> transport is the one path they do not cover.** A regression that only appears
> over Wi-Fi is possible and has happened: the terminal mirror first shipped
> sending rows that were correct over the wire but empty of content, which the
> serial-based suite could not have caught. When touching the wireless path,
> test against hardware — or against a throwaway client that performs the
> nonce/HMAC handshake and speaks the protocol directly.

## Level −1 — Automated tests (no hardware, no Claude)

Self-contained tests exercise the Mac side before you touch any hardware
(`tools/test_*.py` — CI runs the whole glob):

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
                                      # sent), plus the MIRROR view (B|M opens it,
                                      # the tail of a taller screen is shown, a
                                      # blank gap is collapsed rather than
                                      # windowed into, PREV scrolls WITHOUT moving
                                      # the selection, and mirroring a terminal
                                      # never raises it). Nothing is launched on
                                      # your Mac: open/code/osascript are stubbed.

python3 tools/test_settings_merge.py  # proves the installer's settings.json merge
                                      # preserves your existing config and is idempotent

python3 tools/test_detect.py          # the wrapper's screen scrape: which rendered
                                      # TUI frames read working/waiting/error/idle —
                                      # incl. the background-work tells that must
                                      # NOT read idle while work is in flight, and
                                      # must STOP reading working once it finishes
                                      # (real captured frames) (needs pyte)

python3 tools/test_workflow_rows.py   # a dynamic workflow's footer row: every
                                      # layout Claude draws (widths, Ghostty, paused,
                                      # selected) and the lines that only look like
                                      # one; its bullet colour in all six themes as
                                      # pyte reports it; ANSI frames through a real
                                      # pyte screen (running reads working, a
                                      # finished row lingering reads idle); the
                                      # frozen-clock and hidden-row memory, and the
                                      # prompt box told from a dialog however tall
                                      # the prompt in it; and the field-8 record
                                      # each row becomes on the wire — at most
                                      # three and 300 bytes: a run seen to end in
                                      # the last ten seconds first, then the running
                                      # ones, then the most recently ended, and four
                                      # rows whose runs end one after another, each
                                      # end reaching the wire once; a finished run
                                      # still listed behind a dialog and through a
                                      # half-drawn frame, '-' once Claude drops it;
                                      # the compact 'til 3:30pm' note, the clock
                                      # estimate, and a shape change sending at
                                      # once while the clock and tokens wait for
                                      # the keepalive (needs pyte)

python3 tools/test_workflow_daemon.py # the daemon's half of the workflow band, in
                                      # process with an injected clock, then the real
                                      # daemon over a PTY, then --mock: field 8 kept
                                      # on '', cleared on '-', kept when no record
                                      # parses; workflow data never touching a
                                      # state, an ack or the LED; a wrapper's record
                                      # stale after 90 s, a hook-shaped one never;
                                      # the 60 s linger and its tombstone (a
                                      # keepalive cannot bring a finished run back);
                                      # a clock that never steps back; the primary
                                      # record — the ten-second spotlight only for
                                      # an end the daemon saw, never for a run that
                                      # arrives finished — +N, and the 14-char info,
                                      # empty where the device draws the word
                                      # itself. Then the wire: a J| right before
                                      # every F| with bit2, that F| written as the
                                      # commit even when unchanged, and the pair
                                      # again once a second old; J|- once, and
                                      # ahead of another session's J|; a lost J|
                                      # costing a second of band and never
                                      # showing another session's;
                                      # no bit2 in the no-sessions frame or under a
                                      # notice, H re-sending both, every J| in the
                                      # firmware's grammar and every line ASCII and
                                      # short enough for the Nano; the queue JSON's
                                      # 'workflows'; and --mock walking the band
                                      # through every status. Then, with the device
                                      # unplugged, GO, ACK, CONTINUE and FOLLOW on the
                                      # selection and MIRROR opening nothing; and the
                                      # history writer dating a deletion by when it
                                      # last saw the record (claude -c picking a
                                      # deleted chat up again keeps it from its first
                                      # turn) and writing the record before its
                                      # transcript link (needs pyserial)

python3 tools/test_workflow_fw.py     # the firmware's half. First, with no compiler
                                      # needed: the two copies of workflow.h are
                                      # byte-identical and their caps match the
                                      # daemon's (info 14; the S3's name 40); in
                                      # both sketches a J| only stages and the F|
                                      # commits it; the S3 draws the band in
                                      # drawFrame() alone; neither blink trigger
                                      # knows a workflow exists; and every example
                                      # F| in the docs boxes its r1's fleet letter.
                                      # Then tools/wf_harness.cpp, built twice — the
                                      # Nano's WF_NAME_MAX=1 and the S3's default —
                                      # runs the real header: the J| grammar (a line
                                      # with the next F| spliced onto its name is
                                      # rejected, an unknown letter accepted), J|-,
                                      # 99999 -> 65535, clamping, bar fill and
                                      # segments, the Nano's r2 for every info
                                      # length 0-14, the S3 band inside y130-163 for
                                      # every status, and staging (a J| with no F|
                                      # after it changes nothing on screen), and
                                      # every J| the docs tell you to type,
                                      # accepted by both builds. That half SKIPs
                                      # without a C++ compiler, like
                                      # tools/test_battery.py

python3 tools/test_error_colour.py    # an error phrase counts only in Claude's error
                                      # colours above the prompt box: a reply that
                                      # MENTIONS "api error" reads idle, a real
                                      # warning-coloured API-error row (four themes /
                                      # colour depths) and the red retry line read
                                      # error, a limit notice in the chrome still does
                                      # (needs pyte)

python3 tools/test_hook_state.py      # the hook's wire line per Stop payload —
                                      # incl. "the turn ended but work is still in
                                      # flight" reporting working instead of done

python3 tools/test_controller_mode.py # what --web puts on the DEVICE link: G|1 when
                                      # a page takes the grab, G|0 on release (incl.
                                      # a crashed tab), and a re-assert on every
                                      # handshake so a device that connects mid-game
                                      # is told too. Ends with a STATIC check that
                                      # handleLine and handleConfigLine claim
                                      # disjoint verbs — the config console sees USB
                                      # lines first, so a collision is silently
                                      # unreachable over USB, which is exactly how
                                      # the first attempt at this verb (X|, already
                                      # X|WIPE) failed. Read it before adding one.

python3 tools/test_ble_link.py        # the BLE transport, both halves. Starts with
                                      # the CONTRACT — the service and characteristic
                                      # UUIDs live in two files, in two languages, and
                                      # a typo in one makes the device advertise
                                      # forever while the daemon scans forever, with
                                      # nothing on either side saying so. Then drives
                                      # the real BleLink against a FAKE bleak (a stub
                                      # module injected before the lazy import): the
                                      # nonce/HMAC handshake and both ways of failing
                                      # it, line reassembly (a notification boundary
                                      # is NOT a line boundary), and that handshake
                                      # lines never leak up to the dispatcher. Needs
                                      # no radio and no bleak install. Ends with
                                      # static checks that the firmware has ONE
                                      # gamepad row, that it is persisted, and that
                                      # I| landed on the config side of the verb split

python3 tools/test_history.py         # the chat history, against a temp HOME and
                                      # with no Claude: field 9 from the wrapper's
                                      # own line through the daemon's parser (a '|'
                                      # and a non-UTF-8 directory name come through
                                      # whole; a bad key costs only that key, a
                                      # control character its path; a line cut short
                                      # keeps its state and loses its field 9); the
                                      # socket thread outliving a history fault;
                                      # select|<key>; the writer's rules — a record
                                      # only after a turn, a deleted one back only
                                      # after a new turn, a session that never ran a
                                      # turn unable to repoint one, `end` closing it
                                      # and a turn reopening it, the kept hard link,
                                      # no write for a keepalive; resume with the
                                      # exec stubbed — where, as whom, with which
                                      # flags, and what it refuses: open in a
                                      # terminal (looked at again before the exec,
                                      # a picker's chat by the transcript its
                                      # terminal found, context files believed only
                                      # in a directory of yours), a deleted profile,
                                      # and a transcript written moments ago, which
                                      # is a question; by index, which chat it is
                                      # and Enter first; Raise skipping a killed
                                      # terminal's row; then a real daemon fed by
                                      # the wrapper's send_daemon(), SIGKILLed and
                                      # started again, old line shapes and --mock
                                      # keeping nothing, and the CLI listing (every
                                      # name whole on a pipe) and deleting with no
                                      # daemon at all, liveness from the terminals'
                                      # context files (needs pyserial)
```

Each prints a `PASS`/`FAIL` checklist and exits non-zero on failure, so they
double as CI smoke tests. They never touch your real `~/.claude`, the daemon
socket, or serial ports.

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
  position+fleet letters — the selftest predates the account/limit chips) through `ERR → WAIT → DONE → WORK → IDLE` on the OLED
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
   F|1|4|api-server|WAIT  0:42       work|Opus 4.8 xhigh  5h82%|1/3 B W D
   ```

   → the OLED shows the frame as four size-1 rows: the name `api-server` on r0
   **flashing** (`flags` bit0 = 1), the state + time (+ the account,
   right-aligned) on r1, the model + effort (+ the remaining-limit chip)
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

3. Draw a **workflow band**: a `J|` line, then a frame with bit2 (`flags` `4`):

   ```
   J|R|2|0|5|2/5 2m39s 543k|deployment-risk-sweep
   F|4|4|api-server|WORK  02:39      work|Opus 4.8 xhigh  5h82%|1/3 W B D
   ```

   → the `J|` alone changes nothing on screen: it is only staged, and the `F|`
   commits it. Then, on the Nano, r2 becomes a bar two-fifths full with
   `2/5 2m39s 543k` right-aligned; on the S3, under the fleet strip, the name
   and `running` in the WORK colour over five segments, two of them filled, and
   the same info. Send each of these on its own, exactly as it is, followed by
   the same `F|`:

   ```
   J|E|2|0|5|2/5 2m39s 543k|deployment-risk-sweep
   J|P|0|0|0|til 3:30pm|nightly-triage
   J|D|5|0|5|5/5 4m02s 1.2M|deployment-risk-sweep
   J|F|3|0|5|3/5 3m10s 801k|deployment-risk-sweep
   J|Q|1|0|5|1/5|future-status
   ```

   - `E`: a shorter bar and a `!` (Nano); a red bullet over the running bar (S3)
   - `P`: `paused` and no bar; a hollow amber bullet (S3)
   - `D`: `done`
   - `F`: `ended`
   - `Q`, a letter the firmware does not know: the info alone (Nano); no word,
     the IDLE colour (S3)

   Everything after the last `|` is the name, so a note typed after it becomes
   part of the name — and past 40 characters the firmware refuses the whole
   line, which leaves the previous status on screen as if the new one were
   drawn wrong.

   Nothing in the band blinks, whatever the status. Send the frame with `flags`
   `0` and the band goes — r2 is model + effort + chip again although no `J|-`
   was sent, because bit2 is the gate; `J|-` then a frame clears it for good. A
   firmware that predates the band ignores all of it and keeps drawing r2.

4. Send a ping:

   ```
   P
   ```

   → the board replies `K` (the keepalive ack; `H` is sent only on boot/reset).

5. Press the physical buttons and confirm the Serial Monitor prints:

   ```
   B|G     ← GO (D2), short press
   B|K     ← GO (D2), held ≥ ~0.5 s (long press)
   B|N     ← NEXT (D3); repeats ~5/s while held
   B|P     ← PREV (D4); repeats ~5/s while held
   ```

   Also confirm the panel does a quick ~80 ms invert blip on every press.

Pass criterion: `H` on boot, `F|` frames render (flash flag flashes the name
row), the LED patterns (`V|`, incl. the looping kinds stopped by `V|OFF`)
play from typed lines, LINK LOST appears after ~30 s of silence, a `J|` draws
nothing until an `F|` with bit2 commits it and then draws each status as above,
buttons emit `B|G`/`B|K`/`B|N`/`B|P` with auto-repeat on held PREV/NEXT.

> When done, **close the Serial Monitor** so the daemon can use the port.

---

## Level 3 — Daemon in mock mode

Run the daemon with fake sessions — no Claude, no hooks needed:

```sh
python3 $REPO/daemon/claude_mate_daemon.py --mock
```

`--mock` injects a few fake sessions that cycle through states (including a
`waiting` session, so all four words appear). Watch the real hardware:

- The **screen** stays on the tab you selected (it starts on the first tab
  and NEVER switches on its own); the shown session's name row **flashes**
  while its own alert is unacknowledged. Alerts on other tabs show as
  **blinking fleet letters** in the strip — the tab **order** stays
  alphabetical and never shuffles.
- The **LED** plays the pattern of the worst unacked class (`V|ERROR` strobe >
  `V|INPUT` blink > `V|DONE` cascade), looping until acknowledged; a calm
  `V|START` blink fires when a session starts working with nothing else pending.
- Press **NEXT** / **PREV** → the selection steps down/up the stable
  (alphabetical) order (wraps); the screen then stays where you put it —
  indefinitely.
- Press **GO** → the daemon acknowledges the session **shown on the glass**
  (WYSIWYG) and attempts to raise its window (in mock, watch the daemon log for
  the focus call); the display **stays on that tab** (no auto-switch).
- **Hold GO** (~0.5 s) → acknowledges WITHOUT any window op; the flashing and
  the LED loop stop, and the display stays on the tab. (Any remaining unacked
  alerts keep blinking their fleet letters and driving the LED — the view
  doesn't move.)
- **Double-click GO** → toggles FOLLOW mode (a ► marker appears); PREV/NEXT
  then also raise the selected terminal after it settles.
- **The workflow band** → the `api` session — the first tab, which the screen
  starts on — runs a workflow called `release-risk-sweep` that moves on at
  every mock step, every few seconds: running, its bar filling `0/5` → `5/5`
  and its tokens growing; then running with a failed agent (the Nano's `!`, the
  S3's red bullet); `paused` with `til 3:30pm`; `done`; `ended`; then gone, and
  round again. The band belongs to the session on the glass, so every other tab
  shows none; step back to `api` if you have moved off it. Nothing in it
  blinks, and none of it moves the LED. Mock re-sends the record at every step,
  so it never goes stale.

Also confirm robustness: **unplug** the Nano mid-run — the daemon should not
crash — then **replug**; it should reconnect and (via the `H` handshake) restore
the frame, with its workflow band, AND re-arm any active LED loop.

Pass criterion: queue-ordered display + correct LED class + buttons handled +
the workflow band cycling through every status on its tab + survives an
unplug/replug.

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

Then a **workflow**, the way the wrapper sends one — field 3 a control-socket
path (any non-empty one will do), fields 4–7 empty (empty means *keep*), field 8
one running record (`st;done;total;failed;pct;elapsed_s;tokens;note;name`). The
wrapper sends ten fields; these lines stop at the ninth and leave off field 9,
the conversation, which the daemon then takes as *unknown* — exactly what an
older wrapper sends:

```sh
$REPO/tools/feed.sh 'working|wf-demo|demo|/tmp/feed-demo.sock|||||R;2;5;;40;159;542800;;deployment-risk-sweep'
```

→ select `demo` and the band appears: `2/5 2m39s 543k`, its clock counting on
from 2m39s. Then try:

```sh
$REPO/tools/feed.sh 'working|wf-demo|demo|/tmp/feed-demo.sock|||||P;;;;;;;til 3:30pm;deployment-risk-sweep'
$REPO/tools/feed.sh 'working|wf-demo|demo|/tmp/feed-demo.sock|||||D;5;5;;100;312;1200000;;deployment-risk-sweep'
$REPO/tools/feed.sh 'working|wf-demo|demo|/tmp/feed-demo.sock|||||-'
```

— `paused` with `til 3:30pm`, then `done` (`5/5 5m12s 1.2M`), then no band.
Each change of status adds one `workflow:` line to the daemon log; sending the
same line again adds none.

**A record goes stale after 90 s without a refresh.** A field 3 that is not
empty makes the line a wrapper's, and a wrapper's running or paused record is
hidden 90 s after the last line that listed it — the real wrapper re-sends every
20 s, and feed.sh does not. So a band left alone vanishes after a minute and a
half; send the line again to bring it back. (A line with field 3 empty, the way
the hook sends it, has no such timeout: its record stays until replaced,
cleared with `-`, or the session is pruned.) A finished `D` record goes on its
own after at most 60 s either way.

**Leave field 9 off by hand.** A wrapper line carrying a conversation id, with
a `working` after it, puts that chat in your real history, at
`~/.config/claude-mate/history` — `claude-mate history rm` takes it out again.
`tools/test_history.py` drives that path against a temporary `HOME` instead.

Pass criterion: feeding socket lines drives the frame + LED per
[PROTOCOL.md](PROTOCOL.md), and a field-8 line puts up, changes and clears the
workflow band.

---

## Level 5 — Real Claude hooks (last)

Finally, exercise the whole pipeline with Claude Code. With the daemon running
and the hooks merged (see [INSTALL.md](INSTALL.md)):

1. Open a Claude Code session (VS Code extension or CLI) and **submit a prompt**
   (`UserPromptSubmit`) → a `WORK` frame appears and a one-shot `V|START`
   blink fires (if nothing else needs you).
2. Trigger a **Notification** (e.g. a permission prompt) → the `V|INPUT`
   blink **loops** and that session's fleet letter blinks until you navigate
   to it and GO / hold GO / answer in the terminal (shown, its `WAIT` frame
   flashes; the screen does not jump there on its own).
3. Let a turn **complete** (`Stop`) → a flashing `DONE` frame and a `V|DONE`
   cascade that **loops** until acknowledged.
4. Cause an **API error** (`StopFailure`) → a flashing `ERR` frame and a
   `V|ERROR` strobe that **loops** until acknowledged. (`StopFailure` fires
   instead of `Stop` on API errors — they never both fire.)
5. Open multiple sessions and confirm the fleet strip on the bottom row shows
   one letter per session in a stable alphabetical order (unacked alerts
   blink lowercase→uppercase), and that the shown tab never changes on its
   own.
6. Press **GO** on a shown session → its terminal window is **raised** (wrapper
   sessions) or VS Code focuses via the deep link / workspace fallback (see
   the focus Limitations in [ARCHITECTURE.md](ARCHITECTURE.md)). Confirm
   navigation with **NEXT**/**PREV** moves NO windows — ever.
7. End a session → it disappears; with zero sessions the display shows
   `MATE / no sessions`.

Pass criterion: real hook events drive the frames and LED correctly, GO raises
the right session, and navigation never touches a window.

**The workflow band needs the wrapper, not the hooks.** Run Claude through
`claude-mate-wrap`, have it start a dynamic workflow, and keep that session on
the glass: the band appears with the row under the prompt box, follows its
progress and clock, and is gone within a minute of the run's end. Worth seeing
once at full width and once in a terminal under ~85 columns (counts become a
percentage), and through a completion, an agent failure and a stop (`done`, the
`!`, `ended`). Open a menu or the run's own detail view while it runs: the band
holds, and the session stays `WORK`. Do the same in the half-minute a finished
run lingers: its band stays up, rather than blinking off and on. And stop a
run on purpose once: it reads `running` for about 10 s more, then `ended`, its
clock stepping back to where it stopped.

**So does the chat history.** In a wrapped session, run one turn and then quit
— `/exit`, or close the terminal window, which sends the wrapper a SIGHUP it
answers with `end`: `claude-mate history` lists the chat `closed` (the daemon's
log says `history: kept …` once and `… closed` once). `history resume` brings it
back in the same directory, as the same account, with the same permission mode
and model — asking first, while its transcript was written under two minutes
ago. Then `kill -9` a wrapped session's wrapper, the one way it cannot say
goodbye: within a minute the chat reads `interrupted` and resumes — its context
file's process is gone at once, and the daemon's row stops counting once it has
missed three keepalives — though the device lists the session until the TTL
prune. While a chat is open, stop the daemon:
`claude-mate history` still lists it `open`, from that terminal's context file,
and `history resume` refuses it. Last, `claude -c` in a directory that has an
older chat: once its first turn has run, `claude-mate history` lists the chat
it continued, `open`.

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
| No workflow band while a workflow runs | The band needs the PTY wrapper — a hook-only session never has one — and firmware that knows `J\|` (type one by hand, Level 2). It follows the session on the glass and steps aside for the ~4 s a notice is up; the S3 draws it on the main screen only, not under the menu or the mirror. If `claude-mate status` lists the workflow first under the `>` row, the daemon is sending it and the firmware is the gap. If it is under another row, the glass is on a different session: select that one. If it is not listed at all, the wrapper is not reading the row. |
