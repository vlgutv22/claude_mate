#!/usr/bin/env python3
"""
Unit tests for bin/claude-mate-wrap's detect_state(), the screen-scrape that
maps Claude's rendered TUI to working|waiting|error|idle.

Regression focus: the EXTENDED-THINKING spinner. Claude Code shows
"✻ Forming… (14s · ↓ 610 tokens · thinking with xhigh effort)" while it is busy
thinking, and the "esc to interrupt" hint rotates out for a "Tip:" line -- so a
detector that keys "working" off "esc to interrupt" alone reads thinking as
IDLE, and the daemon then fires a premature DONE buzz mid-turn. These cases lock
in that the live activity meter is recognised as working.

Run:  python3 tools/test_detect.py     (needs pyte: pip install pyte)
"""
import os, sys, importlib.util
from importlib.machinery import SourceFileLoader

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAP = os.path.join(REPO, "bin", "claude-mate-wrap")

# The wrapper has no ".py" suffix, so give importlib an explicit source loader.
loader = SourceFileLoader("cmw", WRAP)
spec = importlib.util.spec_from_loader("cmw", loader)
cmw = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(cmw)
except ImportError as e:                       # pyte missing -> match suite convention
    print(f"SKIP: could not import wrapper ({e}). Run: pip install pyte")
    sys.exit(0)

detect_state = cmw.detect_state

# A realistic idle bottom-of-screen, used as the scrollback above live frames so
# we also prove the live region is what governs (not stale prompt chrome).
PROMPT = "╭─────────╮\n│ >       │\n╰─────────╯\n  ? for shortcuts"

CASES = [
    # name, screen, expected_state
    ("thinking spinner (Forming, no 'esc to interrupt')",
     "● I'll start by reading the issue.\n"
     "✻ Forming… (14s · ↓ 610 tokens · thinking with xhigh effort)\n"
     "  Tip: Use /agents to optimize specific tasks.", "working"),
    ("thinking spinner, different verb + elapsed in minutes",
     "✻ Herding… (2m 3s · ↑ 1,204 tokens · thinking with high effort)\n"
     "  Tip: Press up to edit your previous messages.", "working"),
    ("generating with the classic interrupt hint",
     "✶ Generating… (8s · ↑ 320 tokens · esc to interrupt)", "working"),
    ("running a tool (meter, hint rotated to a tip)",
     "● Running 2 shell commands…\n"
     "✻ Working… (5s · ↓ 88 tokens)\n"
     "  Tip: /agents lets you spin up specialists.", "working"),
    ("idle prompt -- must NOT read working",
     PROMPT, "idle"),
    ("idle, conversation merely MENTIONS tokens (no live meter)",
     "● The model used a few hundred tokens for that step.\n" + PROMPT, "idle"),
    ("permission prompt stays waiting",
     "Do you want to proceed?\n 1. Yes\n 2. No", "waiting"),
    ("api error stays error",
     "✗ API Error: Overloaded (529). retrying…", "error"),
]

fails = 0
for name, screen, expected in CASES:
    got, _src = detect_state(screen)
    ok = got == expected
    fails += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got!r}, want {expected!r}")

print("\n================", "ALL PASSED" if not fails else f"{fails} FAILED", "================")
sys.exit(1 if fails else 0)
