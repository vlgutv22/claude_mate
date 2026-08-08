#!/usr/bin/env python3
"""
Prove every SHIP IT level is PLAYABLE, not just parseable: gen_level.py already
rejects malformed headers, so this test checks the things a well-formed header
can still get wrong -- the things that actually shipped broken once.

Level 1's first draft passed every per-tile sanity check and still had ALL
SEVENTEEN pull requests unreachable, because horizontal reach was computed and
vertical reach never was (docs/GAME.md §2). The fix there was a breadth-first
search over standing positions; this file makes that search a permanent CI
gate, for every level at once:

  * the START tile is a real standing position
  * the FLAG column is solid the full playfield height -- an unmissable goal
  * every full-height hole is at most TWO columns wide (three is impossible
    by 3 px, which is worse than obviously impossible: it looks reachable)
  * bugs and product managers have floor under their WHOLE patrol and no wall
    inside it (a walker placed over a hole turns forever; one placed into a
    block clips through it)
  * a BFS using only the MEASURED jump budget -- one row of rise per hop,
    gaps of at most two tiles, drops of any depth -- reaches the win column
    and every single pull request

The movement rules are calibrated against level 1: it ships, people cross it,
so any rule set that cannot cross level 1 is wrong about the physics, not
right about the level.

Run:   python3 tools/test_levels.py          (stdlib only)
"""
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_level  # noqa: E402


def validate(level, label):
    errs = []
    grid = level["map"]
    rows, cols = level["rows"], level["cols"]

    def solid(c, r):
        return 0 <= c < cols and 0 <= r < rows and grid[r][c] != "."

    def openc(c, r):
        return not (0 <= c < cols and 0 <= r < rows) or grid[r][c] == "."

    # ---- the goal and the holes ------------------------------------------
    flag = level["flagCol"]
    for r in range(1, rows):
        if not solid(flag, r):
            errs.append("flag col %d is not solid at row %d" % (flag, r))

    run = 0
    for c in range(cols):
        hole = all(grid[r][c] == "." for r in range(rows))
        run = run + 1 if hole else 0
        if run > 2:
            errs.append("full-height gap wider than 2 tiles ending at col %d" % c)

    # ---- walkers need a floor and a clear band ---------------------------
    for kind in ("bugs", "pms"):
        for a in level[kind]:
            col, row, frm, to = a["col"], a["row"], a["from"], a["to"]
            if not frm <= col <= to:
                errs.append("%s@%d spawns outside its patrol %d..%d"
                            % (kind, col, frm, to))
            for c in range(frm, to + 1):
                if not solid(c, row + 1):
                    errs.append("%s@%d has no floor under col %d" % (kind, col, c))
                if solid(c, row):
                    errs.append("%s@%d has a wall inside its patrol at col %d"
                                % (kind, col, c))

    for a in level["prios"]:
        if not a["from"] <= a["row"] <= a["to"]:
            errs.append("prio@%d spawns outside its drift %d..%d"
                        % (a["col"], a["from"], a["to"]))

    for a in level["prs"]:
        if solid(a["col"], a["row"]):
            errs.append("PR@(%d,%d) is inside terrain" % (a["col"], a["row"]))

    # ---- BFS over standing positions -------------------------------------
    # A state (c, r) is standing ON solid tile (c, r), body in the open cell
    # above. Moves are the MEASURED arc from docs/GAME.md §2: walk; walk off
    # an edge and fall; hop ONE row up with up to 3 columns of drift over a
    # clear arc; jump a 1-2 tile gap flat, or across-and-down onto whatever
    # floor the landing column has. Nothing here can rise two rows or cross
    # three empty tiles, because the game cannot.
    sc, sr = level["start"]["col"], level["start"]["row"]
    if not (solid(sc, sr) and openc(sc, sr - 1)):
        errs.append("start (%d,%d) is not a standing position" % (sc, sr))
        return errs

    def fall_to(c, r):
        for rr in range(r, rows):
            if solid(c, rr):
                return (c, rr) if openc(c, rr - 1) else None
        return None  # out of the world

    seen = {(sc, sr)}
    q = deque([(sc, sr)])
    while q:
        c, r = q.popleft()
        cand = []
        for dc in (-1, 1):
            n = c + dc
            if solid(n, r) and openc(n, r - 1):
                cand.append((n, r))
            elif not solid(n, r) and openc(n, r - 1):
                t = fall_to(n, r)
                if t:
                    cand.append(t)
        if openc(c, r - 2):
            for dc in (-3, -2, -1, 1, 2, 3):
                n = c + dc
                if solid(n, r - 1) and openc(n, r - 2):
                    step = 1 if dc > 0 else -1
                    arc = range(c + step, n, step)
                    if all(openc(j, r - 2) and openc(j, r - 1) for j in arc):
                        cand.append((n, r - 1))
        for dc in (-3, -2, 2, 3):
            n = c + dc
            step = 1 if dc > 0 else -1
            path = range(c + step, n, step)
            if not all(openc(j, r - 1) and openc(j, r) for j in path):
                continue
            if solid(n, r) and openc(n, r - 1):
                cand.append((n, r))
            elif not solid(n, r - 1):
                t = fall_to(n, r)
                if t:
                    cand.append(t)
        for t in cand:
            if t not in seen:
                seen.add(t)
                q.append(t)

    win = cols - 2                      # the engine's own finish line
    if not any(c >= win for c, _ in seen):
        errs.append("win column %d is UNREACHABLE from the start" % win)

    for a in level["prs"]:
        pc, pr = a["col"], a["row"]
        ok = any(abs(c - pc) <= 1 and pr in (r - 1, r - 2, r - 3)
                 for c, r in seen)
        if not ok:
            errs.append("PR@(%d,%d) is UNREACHABLE" % (pc, pr))

    print("  %-20s %3d standing positions, %2d bugs, %d prios, %d PMs, "
          "%2d PRs, flag %d" % (label, len(seen), len(level["bugs"]),
                                len(level["prios"]), len(level["pms"]),
                                len(level["prs"]), flag))
    return errs


def main():
    failures = []
    print("test_levels: BFS-validating every level in gen_level.LEVELS")
    for header, prefix, _out in gen_level.LEVELS:
        level = gen_level.parse_header(header, prefix)
        name = level["name"].encode("ascii", "replace").decode()
        for e in validate(level, name):
            failures.append("%s: %s" % (name, e))
    if failures:
        print("test_levels: FAIL", file=sys.stderr)
        for f in failures:
            print("  - " + f, file=sys.stderr)
        return 1
    print("test_levels: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
