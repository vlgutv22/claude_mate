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
    # Background agents (the reported bug): the live spinner ("· Hatching… (Ns · ↓
    # N tokens)") sits ABOVE the agent's transcript, which fills the bottom of the
    # screen. Keying "working" only off the bottom BUSY_TAIL_LINES read this busy
    # multi-agent session as IDLE (-> premature DONE). The whole-screen live-spinner
    # signal must catch it (faithful to the reported screenshot: a Hatching spinner
    # and a running "○ Explore" row, then the agent's streamed findings below).
    ("running subagents, live spinner above a long transcript (agents panel)",
     "● Agent \"Find tour UI\" finished · 3m 30s\n"
     "  bypass permissions on · esc to interrupt · ← for agents · ↓ to manage\n"
     "· Hatching… (5m 2s · ↓ 15.3k tokens)\n"
     "● main\n"
     "○ Explore  Scanning Genie ETL mappings.    3m 30s · ↓ 79.9k tokens\n"
     + "\n".join(f"  based on my exploration, line {i}" for i in range(14)) + "\n"
     + "  they render as header badges in the UI.", "working"),
    ("subagent hatching, spinner line pushed up by output below",
     "· Hatching… (5m 2s · ↓ 15.3k tokens)\n"
     + "\n".join(f"  agent output line {i}" for i in range(12)) + "\n"
     + "  final line of the streamed output", "working"),
    # Long-running work: an elapsed timer with an HOURS component ("1h 4m 30s") and
    # a k-abbreviated token count must still read working (both regexes handle h and
    # k/M now) -- else a >1h agent re-opens the original false-idle bug.
    ("long-running subagent, hours-format timer + k tokens",
     "· Hatching… (1h 4m 30s · ↓ 15.3k tokens)\n"
     "  Tip: /agents lets you spin up specialists.", "working"),
    # A bare k-abbreviated arrow meter in the bottom region (an agents row, no
    # parenthetical spinner) must read working -- the meter regex now allows k/M.
    ("agent row with k-abbreviated tokens in the bottom region",
     "● Running agents…\n"
     "○ Explore  Scanning ETL mappings   45s · ↓ 79.9k tokens", "working"),
    # Guard: once agents have FINISHED (no live spinner), the session is idle again.
    ("agents finished, no live spinner -> idle",
     "● Agent \"Find tour UI\" finished · 3m 30s\n"
     "● main · done\n"
     + "\n".join(f"  result summary line {i}" for i in range(8)) + "\n"
     + PROMPT, "idle"),
    # False-positive guards (from an adversarial probe): idle sessions whose visible
    # screen merely CONTAINS meter-like text must NOT read working. The live-spinner
    # regex requires a "verb…(timer…meter)" shape at a line start, which none of
    # these have.
    ("idle, a markdown TABLE pairs a duration and token column (benchmark)",
     "● Benchmark from BENCH.md:\n"
     "  | Model    | Wall time | Meter          |\n"
     "  | -------- | --------- | -------------- |\n"
     "  | Opus 4.8 | 2m 30s    | ↓ 79.9k tokens |\n"
     "  | Sonnet   | 45s       | ↓ 40.1k tokens |\n"
     + PROMPT, "idle"),
    ("idle, a bulleted note quotes a timer and a token meter",
     "● Here is what I learned about the detector:\n"
     "  - I ran the probe agent for 3m 30s and it logged ↓ 79.9k tokens total.\n"
     "  - The bottom-region scrape is what should govern the state.\n"
     + PROMPT, "idle"),
    ("idle, a FINISHED agent recap row keeps its token tally (hollow glyph)",
     "● I ran the exploration agents; summary:\n"
     "○ Explore ETL   done · 3m 30s · ↓ 79.9k tokens\n"
     "  It mapped every Genie source table to its Snowflake sink.\n"
     + PROMPT, "idle"),
    # ...and even a full spinner string QUOTED mid-sentence (scrolled above the
    # bottom region) stays idle: the new whole-screen signal rejects it because the
    # spinner is not at a line start, so writing prose ABOUT the meter doesn't self-
    # trigger. (Padding keeps the quote out of the bottom BUSY_TAIL_LINES so this
    # isolates the new line-anchored regex, not the pre-existing bottom-meter check.)
    ("idle, prose quotes a full spinner string mid-sentence (self-scrape)",
     "● The live meter reads \"✻ Forming… (14s · ↓ 610 tokens)\" while busy.\n"
     + "\n".join(f"  note line {i}" for i in range(12)) + "\n"
     + PROMPT, "idle"),
    # A spinner line reproduced verbatim on its OWN line (pasted bug report, README
    # blockquote, this repo's fixtures) is textually a live spinner -- the ONLY tell
    # is that it sits above Claude's idle prompt ("? for shortcuts"). These lock the
    # idle-prompt gate that suppresses the whole-screen spinner signal when idle.
    ("idle, a spinner FRAME pasted on its own line in a bug report",
     "● Got it -- here is the frozen status line from the bug report:\n"
     "✻ Forming… (14s · ↓ 610 tokens · thinking with xhigh effort)\n"
     "  I reproduced the freeze and traced it to the render loop.\n"
     + PROMPT, "idle"),
    ("idle, a README blockquote of the spinner",
     "● From the README \"How the status bar looks\":\n"
     "> Working… (3s · ↑ 429 tokens · esc to interrupt)\n"
     "  The blockquote above is the busy indicator.\n"
     + PROMPT, "idle"),
    ("idle, a summarised log line shaped like a spinner",
     "● Here is the deploy log you asked me to summarise:\n"
     "  Deploying… (30s · ↓ 5k tokens saved)\n"
     "  Release v2.4.0 is live on all regions.\n"
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
