#!/usr/bin/env python3
"""Check the DEVICE half of the workflow band, then run it on the host.

The band is the one thing on either screen the firmware lays out itself: the
daemon sends numbers (J|) and each device draws them in its own geometry. Two
kinds of check, in this order:

  1. PURE PYTHON, no compiler needed, so they run on every machine:
     - workflow.h exists twice -- arduino-cli cannot include a header from
       outside the sketch folder -- and the two copies are byte-identical;
     - its caps are the daemon's: WF_INFO_MAX == WF_INFO_CHARS (14), and the
       S3's WF_NAME_MAX == WF_NAME_CHARS (40). A daemon that sends a 15th
       character gets every J| refused, and the band silently never appears;
     - the Nano keeps one character of the name (RAM), and the longest legal
       J| fits both devices' line buffers;
     - in both sketches J| only STAGES (no redraw) and F| commits it, the
       band is drawn only from the S3's drawFrame(), and the blink triggers
       still know nothing about workflows (a blink means "acknowledge me");
     - every example F| in the docs boxes the fleet letter of its own r1.
  2. THE HARNESS, if there is a C++ compiler: tools/wf_harness.cpp is built
     twice -- with -DWF_NAME_MAX=1 as the Nano builds workflow.h, and with the
     default as the S3 does -- and run: the grammar, saturation, clamping, the
     fill against Claude Code's own arithmetic, segments, the Nano's r2 for
     every info length, the S3 band's extents for every status, and staging.
     Then each build's --parse mode takes every J| the docs tell you to type
     by hand (docs/TESTING.md Level 2, the protocol's examples): a refused
     one leaves the previous band up, and looks like a firmware bug.

Run:   python3 tools/test_workflow_fw.py      (part 2 needs a host C++17 compiler)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NANO_DIR = REPO / "firmware" / "claude_mate"
S3_DIR = REPO / "firmware" / "claude_mate_s3"
NANO_INO = NANO_DIR / "claude_mate.ino"
S3_INO = S3_DIR / "claude_mate_s3.ino"
BOARD = S3_DIR / "board_s3.h"
WF_NANO = NANO_DIR / "workflow.h"
WF_S3 = S3_DIR / "workflow.h"
DAEMON = REPO / "daemon" / "claude_mate_daemon.py"
HARNESS = REPO / "tools" / "wf_harness.cpp"

failures = []
checks = 0


def check(name, ok, detail=""):
    global checks
    checks += 1
    if ok:
        print("  ok    %s" % name)
    else:
        failures.append(name)
        print("  FAIL  %s%s" % (name, (" -- " + detail) if detail else ""))


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def c_define(src, name):
    """The value of `#define NAME <value>` (the first one), or None."""
    m = re.search(r"^\s*#define\s+%s\s+([^\s/]+)" % re.escape(name), src, re.M)
    return m.group(1) if m else None


def py_const(src, name):
    """A module-level integer constant of the daemon, or None."""
    m = re.search(r"^%s\s*(?::[^=\n]+)?=\s*(\d+)\b" % re.escape(name), src, re.M)
    return int(m.group(1)) if m else None


def fn_body(src, signature):
    """The body of the function whose definition starts with `signature`,
    by brace matching -- the switch inside it is what the checks read."""
    start = src.index(signature)
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    return src[i:]


def case_body(body, letter):
    """One `case 'X':` arm of a switch, up to the next case or default."""
    m = re.search(r"case\s+'%s'\s*:(.*?)(?=\n\s*case\s+'|\n\s*default\s*:)"
                  % re.escape(letter), body, re.S)
    return m.group(1) if m else None


def block_after(src, anchor):
    """The brace-matched block that follows `anchor` (e.g. the blink `if`)."""
    start = src.index(anchor)
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    return src[start:]


def pure_checks():
    print("workflow.h: one header, two sketch folders")
    have = WF_NANO.is_file() and WF_S3.is_file()
    check("firmware/claude_mate/workflow.h and firmware/claude_mate_s3/"
          "workflow.h both exist", have)
    if not have:
        return
    same = WF_NANO.read_bytes() == WF_S3.read_bytes()
    check("...and are byte-identical (edit one, copy it over the other)", same,
          "" if same else "run: cp firmware/claude_mate/workflow.h "
          "firmware/claude_mate_s3/workflow.h  (or the other way round)")

    wf = read(WF_S3)
    nano = read(NANO_INO)
    s3 = read(S3_INO)
    board = read(BOARD)
    daemon = read(DAEMON)

    print("the caps the firmware and the daemon share")
    info_max = c_define(wf, "WF_INFO_MAX")
    name_wire = c_define(wf, "WF_NAME_WIRE")
    check("WF_INFO_MAX is 14", info_max == "14", "got %r" % info_max)
    check("WF_NAME_WIRE (what the wire carries) is 40", name_wire == "40",
          "got %r" % name_wire)

    # The S3 keeps the whole name: it must not override WF_NAME_MAX, so the
    # default -- the wire's cap -- is what it compiles with.
    m = re.search(r"#ifndef\s+WF_NAME_MAX\s*\n\s*#define\s+WF_NAME_MAX\s+(\w+)",
                  wf)
    default = m.group(1) if m else None
    if default == "WF_NAME_WIRE":
        default = name_wire
    s3_override = [c_define(read(p), "WF_NAME_MAX")
                   for p in sorted(S3_DIR.glob("*.ino")) + sorted(S3_DIR.glob("*.h"))
                   if p.name != "workflow.h"]
    s3_override = [v for v in s3_override if v is not None]
    s3_name = s3_override[0] if s3_override else default
    check("the S3's WF_NAME_MAX is 40 (the default, not overridden)",
          s3_name == "40", "got %r" % s3_name)

    for const, fw_value, what in (("WF_INFO_CHARS", info_max, "WF_INFO_MAX"),
                                  ("WF_NAME_CHARS", s3_name,
                                   "the S3's WF_NAME_MAX")):
        got = py_const(daemon, const)
        if got is None:
            # Loud on purpose: without the constant nothing pins the daemon's
            # truncation to the firmware's grammar, and a mismatch shows up
            # only as a band that never appears.
            check("the daemon defines %s (to compare with %s)" % (const, what),
                  False, "daemon/claude_mate_daemon.py has no module-level "
                  "`%s = <int>`" % const)
        else:
            check("the daemon's %s (%d) == %s (%s)" % (const, got, what,
                                                        fw_value),
                  str(got) == fw_value)

    # The Nano never draws the name; keeping all 40 would cost 78 bytes of
    # RAM it does not have to spare. It must say so BEFORE the include.
    m = re.search(r"#define\s+WF_NAME_MAX\s+(\d+)\s*\n(?:\s*//[^\n]*\n)*"
                  r"\s*#include\s+\"workflow\.h\"", nano)
    check("the Nano defines WF_NAME_MAX 1 right before including workflow.h",
          bool(m) and m.group(1) == "1")

    worst = (len("J|R|") + 3 * len("65535|") + int(info_max or 0) + len("|")
             + int(name_wire or 0))
    nano_line = int(c_define(nano, "LINE_MAX") or 0)
    s3_line = int(c_define(board, "LINE_MAX") or 0)
    check("the longest legal J| (%d bytes) fits the Nano's line buffer (%d)"
          % (worst, nano_line - 1), worst == 77 and worst <= nano_line - 1)
    check("...and the S3's (%d)" % (s3_line - 1), worst <= s3_line - 1)

    print("J| stages, F| commits -- in both sketches")
    for label, src, sig in (
            ("Nano", nano, "static void handleLine(char *line)"),
            ("S3", s3, "static void handleLine(char *line)")):
        body = fn_body(src, sig)
        j = case_body(body, "J")
        f = case_body(body, "F")
        check("%s: handleLine has a J case that stages it" % label,
              j is not None and "wfStage(" in j)
        check("%s: ...and the J case never asks for a redraw" % label,
              j is not None and "requestRender" not in j)
        ok_f = (f is not None and re.search(r"frameWf\s*=\s*flags\s*&\s*4", f)
                and "wfCommit(" in f and "requestRender()" in f
                and f.index("wfCommit(") < f.index("requestRender()"))
        check("%s: F reads flags bit2 and commits the staged J| before "
              "redrawing" % label, bool(ok_f))

    config = fn_body(s3, "static bool handleConfigLine(char *line)")
    check("S3: the USB config console does not claim J",
          not re.search(r"case\s+'J'\s*:", config))

    print("where the band is drawn, and what it never does")
    calls = [m.start() for m in re.finditer(r"\bwfDrawBand\(", s3)]
    frame = fn_body(s3, "static void drawFrame()")
    check("S3: the band is drawn in exactly one place, drawFrame() -- never "
          "over the menu, mirror, pad, game or pairing screens",
          len(calls) == 1 and "wfDrawBand(" in frame)
    check("S3: ...and the link-detail line is left out only while it is up",
          re.search(r"drawFooter\(accent,\s*!band\)", frame) is not None)
    attn = fn_body(s3, "static bool needsAttention()")
    check("S3: a workflow does not keep the screen awake (needsAttention)",
          not re.search(r"\bwf\b|frameWf", attn))
    for label, src, anchor in (("Nano", nano, "if (nb != gBlinkOn)"),
                               ("S3", s3, "if (nb != blinkOn)")):
        blink = block_after(src, anchor)
        check("%s: the blink re-render trigger knows nothing of workflows "
              "(a blink means: acknowledge me)" % label,
              not re.search(r"\bwf\b|frameWf|Wf", blink))

    print("the frames the docs tell you to type")

    def table(name):
        return dict(re.findall(r'"(\w+)": "(\w+)"', re.search(
            r"^%s = \{[^}]*\}" % name, daemon, re.M).group()))
    letter = table("STATE_LETTER")
    letters = {tag: letter.get(state) for state, tag in table("STATE_TAG").items()}
    frames = [(path, ln) for path, ln in doc_wire_lines() if ln.startswith("F|")]
    check("the docs show example F| frames", len(frames) >= 3,
          "found %d" % len(frames))
    for path, ln in frames:
        f = ln.split("|", 6)
        if len(f) != 7 or not f[2].lstrip("-").isdigit():
            check("%s: %s is a whole frame" % (path, ln), False)
            continue
        sel, r1, r3 = int(f[2]), f[4], f[6]
        if sel < 0:
            continue                          # nothing boxed
        tag = r1.split()[0] if r1.split() else ""
        boxed = r3[sel] if sel < len(r3) else ""
        check("%s: %s boxes its r1's fleet letter (%s -> %s)"
              % (path, ln[:24] + "...", tag, letters.get(tag)),
              boxed.upper() == (letters.get(tag) or "?"),
              "column %d of %r is %r" % (sel, r3, boxed))


# The hand-typed lines: Level 2 of docs/TESTING.md and the examples beside the
# protocol. A J| with a note typed after it is refused whole and leaves the
# previous band up -- which reads as the firmware drawing the new one wrong --
# and a frame that boxes the wrong letter sends someone looking for a bug in
# the fleet strip. Blocks with a '<placeholder>' in them are grammar, not lines.
DOC_SOURCES = (REPO / "docs" / "TESTING.md", REPO / "docs" / "PROTOCOL.md",
               REPO / "firmware" / "README.md")


def doc_wire_lines():
    out = []
    for path in DOC_SOURCES:
        block, fenced = [], False
        for raw in read(path).splitlines():
            if raw.strip().startswith("```"):
                if fenced and not any("<" in b for b in block):
                    out += [(path.relative_to(REPO).as_posix(), b) for b in block
                            if b.startswith(("J|", "F|"))]
                block, fenced = [], not fenced
            elif fenced:
                block.append(raw.strip())
    return out


def find_compiler():
    # Respect CXX, then try the usual names. A machine with no host compiler is
    # not a failing build -- it is a machine that cannot run the second half.
    for cand in (os.environ.get("CXX"), "c++", "g++", "clang++"):
        if cand and shutil.which(cand):
            return shutil.which(cand)
    return None


def run_harness(cxx):
    builds = (("the Nano's build (-DWF_NAME_MAX=1)", ["-DWF_NAME_MAX=1"]),
              ("the S3's build (default WF_NAME_MAX)", []))
    with tempfile.TemporaryDirectory() as td:
        for i, (label, extra) in enumerate(builds):
            print("\nharness: %s" % label)
            exe = Path(td) / ("wf%d" % i)
            cmd = [cxx, "-std=c++17", "-O1", "-Wall", "-Wextra", *extra,
                   "-I", str(S3_DIR), str(HARNESS), "-o", str(exe)]
            build = subprocess.run(cmd, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
            if build.returncode != 0:
                check("%s compiles" % label, False)
                print(build.stderr.strip()[-4000:])
                continue
            if build.stderr.strip():
                # Shown, not fatal: host compilers disagree about warnings,
                # and CI's g++ is not this machine's clang.
                sys.stdout.write(build.stderr[-2000:])
            run = subprocess.run([str(exe)], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
            sys.stdout.write(run.stdout)
            if run.stderr.strip():
                sys.stdout.write(run.stderr)
            check("%s passes" % label, run.returncode == 0)

            lines = [(p, ln) for p, ln in doc_wire_lines() if ln.startswith("J|")]
            parse = subprocess.run([str(exe), "--parse"], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   input="".join(ln + "\n" for _p, ln in lines))
            verdicts = parse.stdout.splitlines()
            check("%s: the docs show J| lines to type (%d)" % (label, len(lines)),
                  len(lines) >= 5 and len(verdicts) == len(lines))
            for (path, ln), v in zip(lines, verdicts):
                check("%s accepts %s's %s" % (label, path, ln), v.startswith("1 "))


def main():
    pure_checks()
    skipped = False
    if not HARNESS.is_file():
        check("tools/wf_harness.cpp exists", False)
    else:
        cxx = find_compiler()
        if not cxx:
            skipped = True
            print("\ntest_workflow_fw: harness SKIPPED - no host C++ compiler "
                  "(set CXX)")
        else:
            print("\ntest_workflow_fw: %s" % cxx)
            run_harness(cxx)

    print("\n%d/%d checks passed" % (checks - len(failures), checks))
    if failures:
        print("test_workflow_fw: FAIL")
        for f in failures:
            print("  - %s" % f)
        return 1
    print("test_workflow_fw: PASS%s"
          % (" (the C++ harness was SKIPPED)" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
