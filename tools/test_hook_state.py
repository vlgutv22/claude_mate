#!/usr/bin/env python3
"""
Unit tests for hooks/claude-status.sh — the state line it reports to the daemon.

Regression focus: the NOT-DONE-YET rule. A Claude turn can "end" while a dynamic
workflow, background agents, a backgrounded shell, a monitor or an MCP task keep
running, or while the session is parked on a one-shot wakeup it scheduled for
itself. Claude Code says so in the Stop payload (`background_tasks`,
`session_crons`); reporting 'done' there buzzes the device and shows IDLE while
the session works on. These cases lock in that such a Stop reports 'working'
instead — and that a genuinely finished turn still reports 'done'.

The hook is run for real (bash + its python3 helper) with CLAUDE_MATE_DRY_RUN=1,
which prints the wire line instead of opening the daemon socket — no daemon, no
socket and no netcat involved.

Run:  python3 tools/test_hook_state.py
"""
import json, os, shutil, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, "hooks", "claude-status.sh")

if not shutil.which("bash"):
    print("SKIP: bash not available")
    sys.exit(0)
if not shutil.which("python3"):                    # the hook's JSON parser
    print("SKIP: python3 not on PATH for the hook")
    sys.exit(0)


def run(state, payload):
    """Run the hook with `payload` on stdin; return its wire line."""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    env = dict(os.environ, CLAUDE_MATE_DRY_RUN="1")
    p = subprocess.run(["bash", HOOK, state], input=body, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=20)
    # The hook must never fail a turn, whatever it is fed.
    assert p.returncode == 0, f"hook exited {p.returncode}: {p.stderr!r}"
    return p.stdout.strip()


BASE = {"session_id": "abc123", "cwd": "/Users/me/projects/claude_mate",
        "hook_event_name": "Stop", "stop_hook_active": False}


def stop(**extra):
    d = dict(BASE)
    d.update(extra)
    return d


TASK = {"id": "t1", "type": "workflow", "status": "running",
        "description": "milestone burn"}
CRON_ONCE = {"id": "c1", "schedule": "30 14 25 7 *", "recurring": False,
             "prompt": "continue the milestone"}
CRON_RECUR = {"id": "c2", "schedule": "0 9 * * 1-5", "recurring": True,
              "prompt": "morning triage"}

CASES = [
    # name, state, payload, expected wire line
    ("plain Stop, nothing in flight -> done",
     "done", stop(), "done|abc123|claude_mate"),
    ("Stop with empty background_tasks -> done",
     "done", stop(background_tasks=[], session_crons=[]),
     "done|abc123|claude_mate"),
    ("older Claude Code (fields absent) -> done, unchanged behaviour",
     "done", {"session_id": "abc123", "cwd": "/tmp/claude_mate"},
     "done|abc123|claude_mate"),

    ("Stop while a dynamic workflow runs -> working (the reported bug)",
     "done", stop(background_tasks=[TASK]), "working|abc123|claude_mate"),
    ("Stop with a PENDING (queued) background task -> working",
     "done", stop(background_tasks=[dict(TASK, status="pending")]),
     "working|abc123|claude_mate"),
    ("Stop with several background tasks -> working",
     "done", stop(background_tasks=[TASK, dict(TASK, id="t2", type="shell")]),
     "working|abc123|claude_mate"),
    ("Stop parked on a one-shot wakeup (/loop tick) -> working",
     "done", stop(background_tasks=[], session_crons=[CRON_ONCE]),
     "working|abc123|claude_mate"),
    ("Stop owning only a RECURRING cron -> done (a schedule, not this turn)",
     "done", stop(background_tasks=[], session_crons=[CRON_RECUR]),
     "done|abc123|claude_mate"),
    ("Stop with a recurring cron AND live work -> working",
     "done", stop(background_tasks=[TASK], session_crons=[CRON_RECUR]),
     "working|abc123|claude_mate"),

    # A task that is already OVER must not hold the session on WIP (#14). Each
    # entry carries its own status -- captured from a real Stop payload:
    #   {"id":"aab0d0a9...","type":"subagent","status":"running",
    #    "description":"Count .h files","agent_type":"Explore"}
    # Counting a terminal one would downgrade this Stop to 'working' with no
    # later Stop to correct it, and the device would never buzz DONE again.
    ("Stop listing a COMPLETED background task -> done",
     "done", stop(background_tasks=[dict(TASK, status="completed")]),
     "done|abc123|claude_mate"),
    ("Stop listing a FAILED background task -> done",
     "done", stop(background_tasks=[dict(TASK, status="failed")]),
     "done|abc123|claude_mate"),
    ("Stop with one finished and one still running -> working",
     "done", stop(background_tasks=[dict(TASK, status="completed"),
                                    dict(TASK, id="t2", status="running")]),
     "working|abc123|claude_mate"),
    ("a task with NO status is still counted (forward-compatible)",
     "done", stop(background_tasks=[{"id": "t9", "type": "shell"}]),
     "working|abc123|claude_mate"),

    # Only 'done' is downgraded: the other states need you regardless of what
    # else is running, and 'working' is already what we would send.
    ("waiting stays waiting even with work in flight",
     "waiting", stop(background_tasks=[TASK]), "waiting|abc123|claude_mate"),
    ("error stays error even with work in flight",
     "error", stop(background_tasks=[TASK]), "error|abc123|claude_mate"),
    ("UserPromptSubmit still reports working",
     "working", {"session_id": "abc123", "cwd": "/tmp/claude_mate"},
     "working|abc123|claude_mate"),

    # Robustness: the hook must survive anything and still emit a clean line.
    ("malformed JSON -> empty fields, still reports",
     "done", "{not json", "done||"),
    ("empty payload -> empty fields, still reports",
     "done", "", "done||"),
    ("background_tasks of the wrong type is ignored",
     "done", stop(background_tasks="lots"), "done|abc123|claude_mate"),
    ("junk entries in the arrays are ignored",
     "done", stop(background_tasks=["nope", None], session_crons=["nope"]),
     "done|abc123|claude_mate"),
    ("a cron without 'recurring' is not treated as one-shot",
     "done", stop(session_crons=[{"id": "c3", "schedule": "* * * * *"}]),
     "done|abc123|claude_mate"),
    ("field separators in the payload cannot corrupt the wire line",
     "done", {"session_id": "a|b", "cwd": "/tmp/we|ird"}, "done|ab|weird"),
]

fails = 0
for name, state, payload, expected in CASES:
    try:
        got = run(state, payload)
    except Exception as e:                          # noqa: BLE001 - report, don't abort
        got = f"<error: {e}>"
    ok = got == expected
    fails += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got!r}, want {expected!r}")

print("\n================", "ALL PASSED" if not fails else f"{fails} FAILED", "================")
sys.exit(1 if fails else 0)
