#!/usr/bin/env python3
"""Generate the web game's level data from the firmware level header.

The firmware headers are the single source of truth for level geometry, actor
spawns, the palette and the sprites. The browser prototype used to carry its
own copy of all of that, which is exactly the kind of duplication that drifts:
someone widens a gap in C, nobody widens it in JS, and the two engines quietly
stop being the same game.

    firmware/claude_mate_s3/game/level_01.h   ->   site/game/level_01.js
    firmware/claude_mate_s3/game/level_02.h   ->   site/game/level_02.js

Each header owns its own geometry and actors; the palette and the sprites are
game-wide and may be DEFINED in any one header (the character lives in
level_01.h, the product manager in level_02.h, where each first appears) but
are merged and emitted into EVERY generated module, so each level_NN.js is
self-contained and any level can draw any actor. Defining the same sprite or
palette entry in two headers is an error -- that is the drift this tool exists
to prevent.

Usage:
    python3 tools/gen_level.py            # write every site/game/level_NN.js
    python3 tools/gen_level.py --check    # exit 1 if any file on disk is stale

The parser is deliberately regex-based -- no C toolchain, no libclang, nothing
to install in CI -- and deliberately strict: every section it expects must be
present and self-consistent (a sprite's row count must match its _H define, the
map must match ROWS/COLS) or it raises instead of emitting plausible-looking
but wrong data.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GAME_DIR = REPO / "firmware" / "claude_mate_s3" / "game"
SITE_DIR = REPO / "site" / "game"
GENERATOR = "tools/gen_level.py"

# One row per level: the firmware header, the define prefix inside it, and the
# ES module it generates. Adding a level is adding a row.
LEVELS = [
    (GAME_DIR / "level_01.h", "LVL1", SITE_DIR / "level_01.js"),
    (GAME_DIR / "level_02.h", "LVL2", SITE_DIR / "level_02.js"),
]

# Kept for tools that import this module to parse level 1 on its own.
DEFAULT_INPUT = LEVELS[0][0]
DEFAULT_OUTPUT = LEVELS[0][2]


class LevelParseError(RuntimeError):
    """Raised when the header is not in the shape this generator understands."""


# --------------------------------------------------------------------------
# C source helpers
# --------------------------------------------------------------------------

def strip_comments(src: str) -> str:
    """Remove // and /* */ comments without touching string/char literals.

    The map rows sit next to /*Mon*/ style labels and the palette sits next to
    the hex values it was derived from; stripping first means no regex below
    ever has to care about either.
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in '"\'':
            quote = c
            out.append(c)
            i += 1
            closed = False
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == quote:
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                raise LevelParseError("unterminated string literal in header")
            continue
        if c == "/" and i + 1 < n:
            if src[i + 1] == "/":
                j = src.find("\n", i)
                i = n if j < 0 else j
                continue
            if src[i + 1] == "*":
                j = src.find("*/", i + 2)
                if j < 0:
                    raise LevelParseError("unterminated /* */ comment in header")
                out.append(" ")
                i = j + 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


_C_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "0": "\0",
    "\\": "\\", '"': '"', "'": "'", "a": "\a", "b": "\b", "f": "\f", "v": "\v",
}


def c_unescape(raw: str) -> str:
    """Decode the escapes a C string literal may carry (\\xB7 for the middle dot)."""
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        if raw[i] != "\\":
            out.append(raw[i])
            i += 1
            continue
        if i + 1 >= n:
            raise LevelParseError(f"trailing backslash in string literal: {raw!r}")
        nxt = raw[i + 1]
        if nxt == "x":
            m = re.match(r"[0-9a-fA-F]+", raw[i + 2:])
            if not m:
                raise LevelParseError(f"bad \\x escape in string literal: {raw!r}")
            out.append(chr(int(m.group(0), 16)))
            i += 2 + len(m.group(0))
            continue
        if nxt in _C_ESCAPES:
            out.append(_C_ESCAPES[nxt])
            i += 2
            continue
        raise LevelParseError(f"unsupported escape \\{nxt} in string literal: {raw!r}")
    return "".join(out)


def find_int_define(src: str, name: str, where: Path) -> int:
    m = re.search(r"^[ \t]*#define[ \t]+%s[ \t]+(-?\d+)\b" % re.escape(name),
                  src, re.MULTILINE)
    if not m:
        raise LevelParseError(f"{where}: missing `#define {name} <int>`")
    return int(m.group(1))


def find_array_block(src: str, decl_re: str, name: str, where: Path) -> str:
    """Return the text between `{` and the matching `};` of a declaration."""
    m = re.search(decl_re, src)
    if not m:
        raise LevelParseError(f"{where}: missing declaration of {name}")
    start = src.find("{", m.end() - 1)
    if start < 0:
        raise LevelParseError(f"{where}: {name} has no initialiser block")
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1:i]
    raise LevelParseError(f"{where}: unbalanced braces in {name}")


def string_literals(block: str) -> list[str]:
    return [c_unescape(m.group(1))
            for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', block)]


# --------------------------------------------------------------------------
# Header -> data
# --------------------------------------------------------------------------

SPRITES = [
    # js name,       C symbol prefix,  width define
    ("mate",         "MATE",           "MATE_W"),
    ("mateSquash",   "MATE_SQUASH",    "MATE_W"),
    ("mateStretch",  "MATE_STRETCH",   "MATE_W"),
    ("bug",          "BUG",            "BUG_W"),
    ("prio",         "PRIO",           "PRIO_W"),
    ("pm",           "PM",             "PM_W"),
    ("pr",           "PR",             "PR_W"),
]

# js key, C symbol suffix, required in every level header. Product managers
# arrive with level 2; a header without a PMS array simply fields none.
ACTOR_KINDS = [
    ("bugs",  "BUGS",  True),
    ("prios", "PRIOS", True),
    ("pms",   "PMS",   False),
    ("prs",   "PRS",   True),
]

PALETTE_KEYS = {
    "GH_L1": "l1", "GH_L2": "l2", "GH_L3": "l3", "GH_L4": "l4",
    "GH_BG": "bg", "GH_GRID": "grid", "GH_MATE": "mate", "GH_BUG": "bug",
    "GH_PRIO": "prio", "GH_PM": "pm", "GH_PR": "pr", "GH_TEXT": "text",
}


def parse_header(path: Path, prefix: str = "LVL1") -> dict:
    if not path.is_file():
        raise LevelParseError(f"input header not found: {path}")
    src = strip_comments(path.read_text(encoding="utf-8", errors="strict"))

    rows = find_int_define(src, f"{prefix}_ROWS", path)
    cols = find_int_define(src, f"{prefix}_COLS", path)
    tile = find_int_define(src, f"{prefix}_TILE", path)
    start_col = find_int_define(src, f"{prefix}_START_COL", path)
    start_row = find_int_define(src, f"{prefix}_START_ROW", path)
    flag_col = find_int_define(src, f"{prefix}_FLAG_COL", path)

    m = re.search(r'#define[ \t]+%s_NAME[ \t]+"((?:[^"\\]|\\.)*)"' % prefix, src)
    if not m:
        raise LevelParseError(f"{path}: missing `#define {prefix}_NAME \"...\"`")
    name = c_unescape(m.group(1))

    # ---- map -------------------------------------------------------------
    block = find_array_block(
        src, r"static\s+const\s+char\s+%s_MAP\s*\[[^\]]*\]\s*\[[^\]]*\]\s*=\s*\{" % prefix,
        f"{prefix}_MAP", path)
    grid = string_literals(block)
    if len(grid) != rows:
        raise LevelParseError(
            f"{path}: {prefix}_MAP has {len(grid)} rows but {prefix}_ROWS is {rows}")
    for i, row in enumerate(grid):
        if len(row) != cols:
            raise LevelParseError(
                f"{path}: {prefix}_MAP row {i} is {len(row)} chars but "
                f"{prefix}_COLS is {cols}")
        bad = sorted(set(row) - set(".1234"))
        if bad:
            raise LevelParseError(
                f"{path}: {prefix}_MAP row {i} has unknown tile(s) {bad!r}; "
                f"expected only '.' and '1'..'4'")

    # ---- actors ----------------------------------------------------------
    actors: dict[str, list[dict]] = {}
    for js_key, suffix, required in ACTOR_KINDS:
        sym = f"{prefix}_{suffix}"
        decl = r"static\s+const\s+GameSpawn\s+%s\s*\[\s*\]\s*=\s*\{" % sym
        if not re.search(decl, src):
            if required:
                raise LevelParseError(f"{path}: missing declaration of {sym}")
            actors[js_key] = []
            continue
        block = find_array_block(src, decl, sym, path)
        spawns = []
        for entry in re.finditer(r"\{([^{}]*)\}", block):
            nums = re.findall(r"-?\d+", entry.group(1))
            if len(nums) != 4:
                raise LevelParseError(
                    f"{path}: {sym} entry {{{entry.group(1).strip()}}} has "
                    f"{len(nums)} fields, expected 4 (col,row,from,to)")
            col, row, frm, to = (int(v) for v in nums)
            if not (0 <= col < cols) or not (0 <= row < rows):
                raise LevelParseError(
                    f"{path}: {sym} spawn at (col={col}, row={row}) is outside "
                    f"the {cols}x{rows} grid")
            spawns.append({"col": col, "row": row, "from": frm, "to": to})
        if required and not spawns:
            raise LevelParseError(f"{path}: {sym} is empty")
        actors[js_key] = spawns

    # ---- palette (whatever this header defines; merged later) ------------
    palette: dict[str, dict] = {}
    for m in re.finditer(
            r"#define[ \t]+(GH_\w+)[ \t]+RGB565\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", src):
        sym = m.group(1)
        r, g, b = (int(m.group(i)) for i in (2, 3, 4))
        for chan, val in (("r", r), ("g", g), ("b", b)):
            if not 0 <= val <= 255:
                raise LevelParseError(
                    f"{path}: {sym} channel {chan}={val} is out of 0..255")
        key = PALETTE_KEYS.get(sym)
        if key is None:
            raise LevelParseError(
                f"{path}: unknown palette macro {sym}; add it to PALETTE_KEYS "
                f"in {GENERATOR} so the web engine gets it too")
        palette[key] = {
            "sym": sym,
            "css": "#%02x%02x%02x" % (r, g, b),
            # what the panel actually receives once 5/6/5-packed
            "rgb565": ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3),
            "args": (r, g, b),
        }

    # ---- sprites (whatever this header defines; merged later) ------------
    sprites: dict[str, dict] = {}
    for js_name, sp_prefix, width_def in SPRITES:
        height_def = f"{sp_prefix}_H"
        decl = (r"static\s+const\s+char\s*\*\s*const\s+%s_BITS\s*\[[^\]]*\]\s*=\s*\{"
                % sp_prefix)
        if not re.search(decl, src):
            continue
        h = find_int_define(src, height_def, path)
        w = find_int_define(src, width_def, path)
        block = find_array_block(src, decl, f"{sp_prefix}_BITS", path)
        bits = string_literals(block)
        if len(bits) != h:
            raise LevelParseError(
                f"{path}: {sp_prefix}_BITS has {len(bits)} rows but {height_def} is {h}")
        for i, row in enumerate(bits):
            if len(row) != w:
                raise LevelParseError(
                    f"{path}: {sp_prefix}_BITS row {i} is {len(row)} px wide but "
                    f"{width_def} is {w}")
            bad = sorted(set(row) - set("01"))
            if bad:
                raise LevelParseError(
                    f"{path}: {sp_prefix}_BITS row {i} has non-bit character(s) {bad!r}")
        sprites[js_name] = {"w": w, "h": h, "bits": bits}

    return {
        "name": name, "rows": rows, "cols": cols, "tile": tile,
        "start": {"col": start_col, "row": start_row}, "flagCol": flag_col,
        "map": grid, "palette": palette, "sprites": sprites, **actors,
    }


def merge_assets(parsed: list) -> tuple:
    """Merge palette + sprites across headers; duplicates are drift, so raise."""
    palette: dict[str, dict] = {}
    sprites: dict[str, dict] = {}
    pal_home: dict[str, Path] = {}
    spr_home: dict[str, Path] = {}
    for path, level in parsed:
        for key, ent in level["palette"].items():
            if key in palette:
                raise LevelParseError(
                    f"{path}: palette entry {ent['sym']} is already defined in "
                    f"{pal_home[key]}; a colour must live in exactly one header")
            palette[key] = ent
            pal_home[key] = path
        for name, sp in level["sprites"].items():
            if name in sprites:
                raise LevelParseError(
                    f"{path}: sprite {name} is already defined in "
                    f"{spr_home[name]}; a sprite must live in exactly one header")
            sprites[name] = sp
            spr_home[name] = path
    missing = sorted(v for k, v in PALETTE_KEYS.items() if v not in palette)
    if missing:
        raise LevelParseError(
            f"palette is missing {', '.join(missing)} across all level headers")
    missing = [n for n, _, _ in SPRITES if n not in sprites]
    if missing:
        raise LevelParseError(
            f"sprites missing {', '.join(missing)} across all level headers")
    # canonical order, independent of which header carried what
    sprites = {n: sprites[n] for n, _, _ in SPRITES}
    return palette, sprites


# --------------------------------------------------------------------------
# Data -> JS
# --------------------------------------------------------------------------

def js(value) -> str:
    """A JS literal for a scalar. JSON strings are valid JS strings; keep the
    output pure ASCII so the file hashes the same on every platform."""
    return json.dumps(value, ensure_ascii=True)


def render_body(level: dict) -> str:
    L: list[str] = []
    add = L.append

    add("export const LEVEL = {")
    add("  name: %s," % js(level["name"]))
    add("  rows: %d," % level["rows"])
    add("  cols: %d," % level["cols"])
    add("  tile: %d," % level["tile"])
    add("  start: { col: %d, row: %d }," % (level["start"]["col"], level["start"]["row"]))
    add("  flagCol: %d," % level["flagCol"])
    add("")
    add("  // The terrain IS the collision map: '.' is a day nothing shipped and")
    add("  // you fall through it, '1'..'4' are shipping days you can stand on.")
    add("  map: [")
    for row in level["map"]:
        add("    %s," % js(row))
    add("  ],")
    add("")
    add("  // Actors: (col, row) spawn in the grid above. Bugs patrol columns")
    add("  // from..to; priority changes drift rows from..to; product managers")
    add("  // patrol columns from..to and chase on sight; PRs do not move.")
    for key, label in (("bugs", "bugs"), ("prios", "priority changes"),
                       ("pms", "product managers"), ("prs", "pull requests")):
        add("  %s: [ // %d %s" % (key, len(level[key]), label))
        for s in level[key]:
            add("    { col: %3d, row: %d, from: %3d, to: %3d }," %
                (s["col"], s["row"], s["from"], s["to"]))
        add("  ],")
        add("")
    add("  // Palette. The CSS hex below is the EXACT 8-bit argument the header")
    add("  // passes to RGB565(), not a round-trip back out of the packed word:")
    add("  // 5/6/5 throws away 3/2/3 bits per channel, so re-expanding would")
    add("  // shift every colour by a few counts for no reason. The rgb565 value")
    add("  // in each comment is what the firmware actually pushes to the LCD.")
    add("  palette: {")
    width = max(len(k) for k in level["palette"])
    for key, ent in level["palette"].items():
        r, g, b = ent["args"]
        add("    %-*s %s, // %s = RGB565(%3d, %3d, %3d) -> 0x%04X" %
            (width + 1, key + ":", js(ent["css"]), ent["sym"], r, g, b, ent["rgb565"]))
    add("  },")
    add("")
    add("  // Sprites keep the firmware's row-string form: one character per")
    add("  // pixel, one string per row, '1' painted and '0' transparent.")
    add("  sprites: {")
    for name, sp in level["sprites"].items():
        add("    %s: {" % name)
        add("      w: %d," % sp["w"])
        add("      h: %d," % sp["h"])
        add("      bits: [")
        for row in sp["bits"]:
            add("        %s," % js(row))
        add("      ],")
        add("    },")
    add("  },")
    add("};")
    add("")
    add("export default LEVEL;")
    add("")
    return "\n".join(L)


def render(level: dict, source: Path) -> str:
    body = render_body(level)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    try:
        rel_src = source.resolve().relative_to(REPO).as_posix()
    except ValueError:
        rel_src = source.as_posix()
    header = "\n".join([
        "// GENERATED FILE -- DO NOT EDIT BY HAND.",
        "//",
        "// Source:     %s" % rel_src,
        "// Assets:     palette + sprites merged from every level header",
        "// Generator:  %s" % GENERATOR,
        "// Regenerate: python3 %s" % GENERATOR,
        "// Verify:     python3 %s --check   (CI runs this)" % GENERATOR,
        "//",
        "// The firmware headers are the single source of truth for level",
        "// geometry, actors, palette and sprites. Edit a header and rerun the",
        "// generator; anything hand-edited here is lost on the next run, and CI",
        "// fails in the meantime so the two engines cannot drift apart.",
        "//",
        "// content-sha256: %s" % digest,
        "// (over everything below this comment block -- a changed body with an",
        "//  unchanged hash means someone edited the output by hand.)",
        "",
        "",
    ])
    return header + body


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def check(generated: str, out_path: Path) -> int:
    try:
        rel_out = out_path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        rel_out = out_path.as_posix()
    if not out_path.is_file():
        print(f"gen_level: {rel_out} does not exist -- run `python3 {GENERATOR}`",
              file=sys.stderr)
        return 1
    on_disk = out_path.read_text(encoding="utf-8")
    if on_disk == generated:
        print(f"gen_level: {rel_out} is up to date")
        return 0

    disk_lines = on_disk.splitlines(keepends=True)
    new_lines = generated.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        disk_lines, new_lines, fromfile=f"{rel_out} (on disk)",
        tofile=f"{rel_out} (regenerated)", n=2))
    added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    print(f"gen_level: {rel_out} is STALE -- {added} line(s) added, "
          f"{removed} line(s) removed versus the firmware header.", file=sys.stderr)
    limit = 60
    for line in diff[:limit]:
        sys.stderr.write(line if line.endswith("\n") else line + "\n")
    if len(diff) > limit:
        print(f"... {len(diff) - limit} more diff line(s) suppressed", file=sys.stderr)
    print(f"\nFix: run `python3 {GENERATOR}` and commit the result.", file=sys.stderr)
    return 1


def build_all() -> list:
    """Parse every level header, merge the shared assets, render every module.

    Returns [(output Path, level dict, generated text), ...].
    """
    parsed = [(hdr, parse_header(hdr, prefix)) for hdr, prefix, _ in LEVELS]
    palette, sprites = merge_assets(parsed)
    out = []
    for (hdr, prefix, dest), (_, level) in zip(LEVELS, parsed):
        level = {**level, "palette": palette, "sprites": sprites}
        out.append((dest, level, render(level, hdr)))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Generate the web game's level modules from the firmware headers.")
    ap.add_argument("--check", action="store_true",
                    help="regenerate in memory and exit 1 if any file on disk differs")
    args = ap.parse_args(argv)

    try:
        built = build_all()
    except LevelParseError as exc:
        print(f"gen_level: {exc}", file=sys.stderr)
        return 2

    if args.check:
        rc = 0
        for dest, _, generated in built:
            rc = max(rc, check(generated, dest))
        return rc

    for dest, level, generated in built:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(generated, encoding="utf-8")
        print("gen_level: wrote %s (%d bugs, %d priority changes, %d PMs, "
              "%d PRs, %dx%d map)" % (
                  dest.as_posix(), len(level["bugs"]), len(level["prios"]),
                  len(level["pms"]), len(level["prs"]),
                  level["rows"], level["cols"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
