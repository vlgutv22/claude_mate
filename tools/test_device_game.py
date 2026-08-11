#!/usr/bin/env python3
"""Compile the DEVICE game engine for the host and run it.

`ship_it.h` is the firmware's copy of SHIP IT. CI compiles it for the ESP32 and
never runs it, which leaves two kinds of bug findable only by flashing a board
and looking at it: text that runs off a 320x172 panel, and numbers that drift
away from the web engine's. tools/devgame_harness.cpp stubs the display and NVS,
drives the real engine, and checks both -- this wrapper is what puts it in
`tools/test_*.py` so the existing CI glob picks it up.

The firmware itself is untouched by any of this: the stubs live here, and the
engine is included exactly as the sketch includes it.

Run:   python3 tools/test_device_game.py         (needs a host C++17 compiler)
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tools" / "devgame_harness.cpp"
STUB = REPO / "tools" / "devgame_stub"
GAME = REPO / "firmware" / "claude_mate_s3" / "game"
BOARD = REPO / "firmware" / "claude_mate_s3"


def find_compiler():
    # Respect CXX, then try the usual names. A machine with no host compiler is
    # not a failing build -- it is a machine that cannot run this test.
    for cand in (os.environ.get("CXX"), "c++", "g++", "clang++"):
        if cand and shutil.which(cand):
            return shutil.which(cand)
    return None


def main() -> int:
    cxx = find_compiler()
    if not cxx:
        print("test_device_game: SKIPPED - no host C++ compiler (set CXX)")
        return 0
    if not HARNESS.is_file():
        print("test_device_game: FAIL - missing %s" % HARNESS)
        return 1

    print("test_device_game: %s" % cxx)
    with tempfile.TemporaryDirectory() as td:
        exe = Path(td) / "devgame"
        cmd = [
            cxx, "-std=c++17", "-O1",
            # macOS marks sprintf deprecated; the firmware uses it deliberately
            # and this test is not the place to relitigate that.
            "-Wno-deprecated-declarations",
            "-I", str(STUB), "-I", str(BOARD), "-I", str(GAME),
            str(HARNESS), "-o", str(exe),
        ]
        build = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
        if build.returncode != 0:
            print("test_device_game: FAIL - the device engine did not compile "
                  "for the host")
            sys.stdout.write(build.stderr[-4000:])
            return 1
        if build.stderr.strip():
            sys.stdout.write(build.stderr[-2000:])

        run = subprocess.run([str(exe)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
        sys.stdout.write(run.stdout)
        if run.stderr.strip():
            sys.stdout.write(run.stderr)
        if run.returncode != 0:
            print("test_device_game: FAILED")
            return 1

    print("test_device_game: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
