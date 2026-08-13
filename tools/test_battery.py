#!/usr/bin/env python3
"""Compile the DEVICE battery gauge for the host and run it.

`firmware/claude_mate_s3/battery.h` holds the gauge's arithmetic: volts to
percent, the three-segment thresholds with their hysteresis, and the hold that
decides when the displayed level may move. CI compiles the firmware and never
runs it, which left every one of those checkable only by charging a cell,
flashing a board and watching a corner of the screen -- and that is how the
"nothing is wired to the divider" threshold came to sit at 3000 mV, a value only
a real, nearly-empty cell can produce, so the gauge erased itself exactly when
it mattered.

tools/batt_harness.cpp drives the real header, with no stubs and no copies; this
wrapper is what puts it in the `tools/test_*.py` glob that CI already runs.

Run:   python3 tools/test_battery.py         (needs a host C++17 compiler)
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tools" / "batt_harness.cpp"
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
        print("test_battery: SKIPPED - no host C++ compiler (set CXX)")
        return 0
    if not HARNESS.is_file():
        print("test_battery: FAIL - missing %s" % HARNESS)
        return 1

    print("test_battery: %s" % cxx)
    with tempfile.TemporaryDirectory() as td:
        exe = Path(td) / "batt"
        cmd = [
            cxx, "-std=c++17", "-O1", "-Wall",
            "-I", str(BOARD),
            str(HARNESS), "-o", str(exe),
        ]
        build = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
        if build.returncode != 0:
            print("test_battery: FAIL - harness did not compile")
            print(build.stderr.strip()[-4000:])
            return 1
        run = subprocess.run([str(exe)], capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        sys.stdout.write(run.stdout)
        if run.stderr.strip():
            sys.stderr.write(run.stderr)
        if run.returncode != 0:
            print("test_battery: FAIL")
            return 1
    print("test_battery: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
