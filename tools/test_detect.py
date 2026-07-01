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
    # Self-scrape regression: a message that QUOTES the live meter verbatim (e.g.
    # design notes / research about the detector) scrolled up above the prompt must
    # NOT read working. The quote sits outside the bottom busy region.
    ("idle, a long message QUOTES the live meter verbatim (self-scrape)",
     "● Here is the saved detection research:\n"
     "  The spinner shows ↓ 610 tokens while it is busy.\n"
     "  Full form: (14s · ↓ 610 tokens · thinking with xhigh effort), esc to interrupt.\n"
     + "\n".join(f"  · detail line {i}" for i in range(10)) + "\n"
     + PROMPT, "idle"),
    ("permission prompt stays waiting",
     "Do you want to proceed?\n 1. Yes\n 2. No", "waiting"),
    ("api error stays error",
     "✗ API Error: Overloaded (529). retrying…", "error"),
    # #4 -- a content-prone error token (bare HTTP code / "connection error")
    # quoted in scrollback ABOVE the live region must NOT stick the session in
    # 'error'. Before the split, "connection error"/" 500" matched the whole
    # 20-line tail; a printed/old code then nagged every 5s forever (the keepalive
    # refreshes last_update_ts so prune never fires). Now they only match the
    # tight bottom region.
    ("idle, an OLD error code quoted in scrollback above the prompt (#4)",
     "● Earlier the API returned a connection error (500) but it recovered.\n"
     + "\n".join(f"  · followup line {i}" for i in range(12)) + "\n"
     + PROMPT, "idle"),
    # ...but a REAL error rendered on the live status line still reads error.
    ("real API error on the live status line still errors (#4 no regression)",
     "● The request failed.\n"
     "✗ API Error · connection error (529). retrying…", "error"),
    ("bare HTTP 500 on the bottom status line still errors via error_footer (#4)",
     "● Running request…\n"
     "  Request failed: 500 Internal Server Error", "error"),
    # Meter/code collision (review finding): a WORKING frame whose token count is
    # exactly an HTTP code (500/429/503/529) must read 'working', not 'error'.
    # Bare codes were dropped from error_footer AND busy_now is computed first.
    ("working meter showing '↓ 500 tokens' must NOT read error",
     "● Generating the response…\n"
     "✻ Forming… (10s · ↓ 500 tokens · thinking with xhigh effort)", "working"),
    ("working meter '↑ 429 tokens · esc to interrupt' must NOT read error",
     "✶ Working… (3s · ↑ 429 tokens · esc to interrupt)", "working"),
    # Background agents: the live agent-progress line ("○ <name> … Ns · ↓ N tokens")
    # or a "Hatching…" spinner sits ABOVE the agent's transcript, which fills the
    # bottom of the screen. Keying "working" only off the bottom BUSY_TAIL_LINES
    # read this busy multi-agent session as IDLE (-> premature DONE). The glyph-
    # prefixed live-line signal (matched over the whole screen) must catch it.
    ("running subagent, live agent line above a long transcript (agents panel)",
     "● Agent \"Find tour UI\" finished · 3m 30s\n"
     "  bypass permissions on · esc to interrupt · ← for agents · ↓ to manage\n"
     "● main\n"
     "○ Explore  Scanning Genie ETL mappings.    3m 30s · ↓ 79.9k tokens\n"
     + "\n".join(f"  based on my exploration, line {i}" for i in range(14)) + "\n"
     + "  they render as header badges in the UI.", "working"),
    ("subagent hatching, spinner line pushed up by output below",
     "· Hatching… (5m 2s · ↓ 15.3k tokens)\n"
     + "\n".join(f"  agent output line {i}" for i in range(12)) + "\n"
     + "  final line of the streamed output", "working"),
    # Guard: once agents have FINISHED (no live timer+meter line), the session is
    # idle again -- a finished-agent line ("● … finished · 3m 30s") has no ↑/↓
    # token meter and its glyph is a done marker, so it must NOT read working.
    ("agents finished, no live line -> idle",
     "● Agent \"Find tour UI\" finished · 3m 30s\n"
     "● main · done\n"
     + "\n".join(f"  result summary line {i}" for i in range(8)) + "\n"
     + PROMPT, "idle"),
]

fails = 0
for name, screen, expected in CASES:
    got, _src = detect_state(screen)
    ok = got == expected
    fails += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got!r}, want {expected!r}")

print("\n================", "ALL PASSED" if not fails else f"{fails} FAILED", "================")
sys.exit(1 if fails else 0)
