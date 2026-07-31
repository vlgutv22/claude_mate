# SHIP IT — a game on the glass

> **Status: design proposal.** Nothing here is implemented. This document plus
> [`level_01.h`](../firmware/claude_mate_s3/game/level_01.h) exist so the concept
> can be judged before an engine is written.

A side-scroller for the ESP32-S3 companion. You are **Claude Mate**. Twelve
levels are twelve months of a project. You walk your own contribution graph from
the first day of a sprint to the milestone at the end of it, and **bugs eat your
schedule**. Ship before the deadline.

```
┌────────────────────────────────────────────────────────┐
│ M1 · KICKOFF                        ▓▓▓▓▓▓░░░░  14d    │  HUD: milestone + deadline
├────────────────────────────────────────────────────────┤
│         ▓▓                                             │
│   ▒▒    ▓▓        ░░░░                    ▒▒▒▒         │  the contribution graph
│   ▒▒         ▄▄                  ▄▄       ▒▒▒▒      ⚑  │  IS the terrain
│ ▒▒▒▒▒▒▒     ▒▒▒▒▒▒▒▒        ▒▒▒▒▒▒▒▒▒   ▒▒▒▒▒▒▒▒▒▒▒▒  │
│ ▓▓▓▓▓▓▓░░░░░▓▓▓▓▓▓▓▓░░░░░░░░▓▓▓▓▓▓▓▓▓░░░▓▓▓▓▓▓▓▓▓▓▓▓  │  ░ = a day you shipped
│ ████████     ████████        █████████   ████████████  │      nothing = a hole
└────────────────────────────────────────────────────────┘
   ▲  you        ▲ bug         ▲ gap                  ▲ milestone
```

---

## 1. The one rule that makes it work

**A commit is a floor tile. A day you shipped nothing is a hole.**

Everything follows from that. A streak is a bridge. A weekend off is a jump. A
heavy day is a tall block you can climb. The level *is* a contribution graph —
drawn in GitHub's own five-step green ramp, 7 rows for the days of the week,
columns marching right through the sprint — and it is also, unmodified, the
platformer's collision map.

That is why this idea fits this hardware. No tilesets, no art pipeline, no
sprite atlas: the terrain is **axis-aligned filled rectangles**, which is the one
thing a 40 MHz polled SPI panel draws quickly.

## 2. Deadline *is* health

Mario has lives and a separate countdown. Here they are the **same resource**,
and that is the design's second load-bearing decision.

You do not have three lives. You have **days until the milestone**. A bug does
not kill you — it costs you two days. Falling in a hole costs you a day and puts
you back at the last commit you stood on. Run out of days and the milestone
slips: that is the fail state, and it is the honest one, because in real work a
bug never kills you, it eats your schedule.

It also removes a whole UI: no lives counter, no separate timer. One number in
the corner, counting down, and everything in the world is a claim on it.

| Event | Cost / gain |
|---|---|
| A bug reaches you | **−1 day**, knockback, brief invulnerability |
| Fall in a hole | **−1 day**, respawn at the last tile you stood on |
| Stomp a bug | **+½ day** — fixing things buys schedule |
| A **priority change** reaches you | **−2 days** and shoved four tiles back; cannot be stomped |
| Merge a pull request | **+1 day**, and the screen says `PR MERGED` |
| Reach the milestone flag | level complete; unused days carry over |

**The vertical budget is ONE ROW per jump** — and missing this shipped a broken
level. The apex of a full jump is 35.3 px. Standing on row R and landing on row
R−k needs a rise of k×16:

| Climb | Need | Margin | |
|---|---|---|---|
| 1 row | 16 px | **+19.3 px** | comfortable |
| 2 rows | 32 px | **+3.3 px** | pixel-perfect — unreachable in practice |
| 3 rows | 48 px | −12.7 px | impossible |

Every raised ledge therefore needs **one step per row it rises**. Without those
steps a breadth-first search over standing positions found **all seventeen pull
requests unreachable** — the entire optional layer of the level was decoration.
Reported from play as *"why can't I jump so high?"*, which is exactly what a
3.3 px margin feels like.

The lesson generalises past this level: horizontal reach was computed carefully
from the start and vertical reach was never computed at all. Any level tool must
check both, and the reachability test has to be a real search from the spawn
point rather than a per-tile sanity check — a ledge always looks fine when you
only compare it to its own neighbours.

**The horizontal budget is two tiles, measured not chosen.** Walk 1.45 px/frame, jump
−4.75, gravity 0.30: a full jump is airborne 31 frames and covers 45 px, i.e.
**2.81 tiles**. A 1-tile gap clears with 29 px to spare, a 2-tile gap with 13 —
and a **3-tile gap is impossible by 3 px**, which is worse than obviously
impossible, because it looks reachable. Later levels buy wider crossings with a
platform in the middle, never with a bigger jump: clearing three tiles would
need 2.7 tiles of height on a 7-row playfield, which makes the character floaty
and every ceiling meaningless.

Carrying days over is what turns twelve levels into one arc rather than twelve
separate scores: a sloppy sprint 3 is still hurting you in sprint 9.

## 3. Controls

Exactly the four switches already soldered on. No new hardware.

| Button | In game |
|---|---|
| **PREV** | ← walk left |
| **GO** | ↑ jump — held longer jumps higher |
| **NEXT** | → walk right |
| **4th** ×1 | pause · save · exit to CONDUCTOR |
| **4th** ×2 | the device menu (as everywhere else) |
| **4th** ×3 | the game menu — level select, restart sprint, abandon run |

Three of those four need firmware that does not exist yet, and the reasons are
specific:

- **PREV/NEXT must be read as pin STATE, not as events.** `pollNavBtn` emits a
  press and then auto-repeats at 400 ms / 200 ms. A platformer needs "is it held
  down *this frame*", which is a raw `digitalRead`, and the existing debounce and
  repeat must both be bypassed in game mode.
- **The 4th button's tap is deferred `DBLCLICK_MS` (300 ms)** to watch for a
  double. That is correct for the mirror and fatal for an action button — which
  is exactly why the 4th button gets only non-action verbs here (pause, menus)
  and never jump.
- **The 4th button is also power-off at a 2 s hold.** Holding it during play
  would switch the device off mid-level. Game mode must suppress that, and then
  must offer some other way out — hence single-click = exit.

And, as with MENU and SETTINGS, **game mode swallows its buttons**: none of them
may reach the daemon, or every jump raises a terminal on the Mac.

### Coyote time and jump buffering are not polish — they are required

Found by playing the prototype, not by reading it. With a fixed jump arc and no
air control, the launch window for every gap in level 1 is about **13 px, roughly
eight frames**: jump one tile early and you land *in* the hole. Sweeping launch
positions, only one of five timings cleared the first gap, and the level's own
tutorial gap was the harshest jump in it.

The level data was fine. The *feel* was wrong, and the fix is the standard pair
rather than a bigger jump:

- **Coyote time** — you may still jump for ~6 frames after walking off an edge.
- **Jump buffering** — a jump pressed up to ~6 frames early fires the moment you
  land.

Twelve lines, and the same sweep then cleared **four of five** timings. Both must
be in the firmware engine from the first commit; retrofitting them means
re-tuning every level built without them.

## 4. The twelve levels

Twelve months of a release. Difficulty is not just "more bugs" — each level
changes one thing about the *shape* of the graph, because the graph is the level.

| # | Milestone | The sprint's shape | Introduces |
|---|---|---|---|
| 1 | **Kickoff** | dense, flat, forgiving | walking, one gap, one bug |
| 2 | **Scaffolding** | rising streaks | climbing, longer jumps |
| 3 | **First Light** | first real weekend gaps | run-up jumps |
| 4 | **Dogfood** | floating platforms | bugs that patrol above you |
| 5 | **Alpha** | sparse — a thin month | precision, few safe tiles |
| 6 | **Feature Freeze** | dense but bug-infested | crowds |
| 7 | **Hardening** | flaky tiles that blink out | timing |
| 8 | **Beta** | two viable routes | risk/reward: fast vs safe |
| 9 | **Release Candidate** | regressions respawn once | you cannot just clear it |
| 10 | **Code Freeze** | almost no green | the sparsest month |
| 11 | **Launch Week** | crunch: dense, dark, swarming | everything at once |
| 12 | **Ship It** | the whole year, compressed | a victory lap that can still fail |

Level 10 being the *emptiest* is deliberate. A code freeze means nobody is
committing, so there is almost nothing to stand on — the theme and the
difficulty curve point the same way, which is the test of whether a metaphor is
doing real work or just decorating.

## 5. Enemies

All of them are bugs. What differs is how they behave, and each is a real thing
that happens to a sprint.

| Enemy | Behaviour | Ships in |
|---|---|---|
| **Bug** | patrols a platform, turns at edges. Stompable, and stomping pays | L1 |
| **Priority change** | drifts vertically, denying a whole lane. **Cannot be stomped** — you do not fix a re-prioritisation by jumping on it. Rare: three, against eleven bugs | L1 |
| **Flaky test** | blinks in and out on a fixed cycle; harmless while absent | L7 |
| **Regression** | squashing it works — then it comes back once, angrier | L9 |
| **Merge conflict** | static, blocks a corridor; cannot be squashed, must be routed around | L8 |

**Every enemy type introduces itself.** The first time one comes on screen the
game freezes and shows a card: the sprite at 3×, its name, and exactly what a
collision costs. A player should never learn a rule by losing a day to it, and on
a device with no manual, no tooltips and no mouse-over, the card is the only
place that rule can live. Once per type, per run.

Stomping paying **+½ day** is what stops the safe line being the right line. Bugs
sit on the route you want, so "jump everything, touch nothing" is now worse than
engaging — the same claim the PR economy makes, one verb down.

## 6. What it looks like

**Terrain** uses GitHub's own ramp, so a screenshot of the game and a screenshot
of the graph are the same picture:

| Cell | Colour | Meaning |
|---|---|---|
| `0` | background | nothing shipped — a hole |
| `1` | `#9BE9A8` | a quiet day |
| `2` | `#40C463` | a normal day |
| `3` | `#30A14E` | a good day |
| `4` | `#216E39` | a heavy day |

**Claude Mate** is drawn from the project's own mascot — 14×12, terracotta
(`#D97757`, the accent colour this repo already uses everywhere). Two dark eye
slots, stubby arms, two legs. Squashes to 14×8 on landing, stretches to 12×14 at
the top of a jump; that squash-and-stretch is most of what makes a jump *feel*
like a jump, and it costs two rectangles.

**Bugs** are 10×8, dark red, with two legs and a pair of pale eyes — readable at
this size because they are the only round-ish thing on a screen made of squares.

## 7. Why this runs at all

The display is the constraint, and it is a hard one. The numbers were measured
for this repo already:

- The LCD sits on GPIO 40/41/42/45, which are **not** the S3's FSPI IOMUX pins,
  so the bus routes through the GPIO matrix and is capped at **40 MHz**.
- `Arduino_ESP32SPI` is **polled, not DMA** — it busy-waits a core.
- `Arduino_Canvas::flush()` ignores its argument and always pushes the whole
  110 KB buffer: **~28 ms**, i.e. a **~30 fps ceiling with zero game logic**.

A full-width scroller is the *worst* shape for that, because every pixel is dirty
every frame. Three things make it viable anyway, and they should be built in this
order:

1. **A short playfield.** Confine the world to a 320×112 band (7 rows × 16 px)
   with a static HUD above that is never re-pushed. That is 65 % of the pixels,
   ~18 ms, ~40 fps of headroom before logic.
2. **Tile-aligned scrolling.** The camera moves in whole 16 px steps, so a scroll
   is a memmove of the canvas plus one new 16 px column — not a redraw.
3. **Dirty rectangles for actors.** Claude Mate and the bugs are the only things
   that move within a tile. Redraw the tiles they left and the tiles they entered,
   nothing else.

If that is not enough, the escape hatch is the ST7789's **hardware scroll**
(`VSCRDEF`/`VSCRSADD`), which in this landscape rotation scrolls horizontally —
but the driver does not expose it, so it is a bench spike before anything is
designed around it, not a plan.

**The game loop must not block.** Longer than `LINK_WATCHDOG_MS` (30 s) and the
device flips to NO LINK; block at all and the transports stop draining and the
160-byte line buffers overflow. Like everything else here, it has to be a
`poll()`-shaped state machine.

**Memory is not a problem.** 8 MB PSRAM, 16 MB flash, and the sketch currently
uses 33 % of a 3 MB partition. A level is 7 × 60 bytes.

## 8. Sound

The device cannot make any, and will not without hardware: the ESP32-S3 has no
DAC, and this board's 3.3 V rail is a linear LDO with the backlight on a
resistor-limited MOSFET, so there is no switching node a firmware trick could
make audible.

But the daemon can, and the plumbing now exists. The `O|` verb added for the
sound toggle generalises to `O|SFX|<event>`, so a jump, a squash and a shipped
milestone can play through the Mac's speakers while you play on the device. That
is a genuinely nice property of a game on a networked device, and it costs one
new key on a verb that already ships.

## 9. Two things to decide before code

**The mascot is Anthropic's mark.** The pixel creature is the Claude Code logo.
This repo is CC BY-NC and already says "not affiliated with Anthropic", which
covers *reference* but is a weaker position for shipping it as a game character.
The safe alternative reads better anyway: a sprite of **the device itself** — a
little screen with legs, four key-caps for a face — which is unambiguously this
project's own and is funnier. Worth a decision before art is drawn twice.

**No Mario anything.** No Nintendo music, sprites, level layouts or names. Every
asset here is either original or GitHub's public colour ramp. The genre is not
ownable; the specific work is.

## 10. What exists so far

- This document.
- [`firmware/claude_mate_s3/game/level_01.h`](../firmware/claude_mate_s3/game/level_01.h)
  — level 1 as data, in the format an engine would consume, with the sprites and
  the palette.

Nothing is wired into the firmware. The next step, if the concept survives
review, is the engine described in §7 — playfield band, tile-aligned camera,
dirty-rect actors — plus the input changes in §3.
