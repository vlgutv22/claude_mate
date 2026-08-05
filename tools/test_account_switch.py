#!/usr/bin/env python3
"""Carrying a conversation to another account: the tracker and the switch.

An account is a $CLAUDE_CONFIG_DIR, read once at startup, so switching can only
mean starting claude again elsewhere -- and the conversation only survives
because a transcript is a file that can be copied into the target profile's
projects/ tree. Everything below tests the two halves of that: the wrapper
recording WHICH transcript belongs to this terminal, and the switch putting it
in the right place under a different profile.

No claude, no network, no hardware. Profiles, config dirs and transcripts are
all fabricated in a temp tree.
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WRAP = os.path.join(REPO, "bin", "claude-mate-wrap")
SWITCH = os.path.join(REPO, "bin", "claude-mate-switch")

failures, checks = [], 0


def check(name, ok):
    global checks
    checks += 1
    print(f"   {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        failures.append(name)


def load(path, name):
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tmp = tempfile.mkdtemp(prefix="cm-switch-")
os.environ["CLAUDE_MATE_ACCOUNTS_DIR"] = os.path.join(tmp, "accounts")
os.environ["TMPDIR"] = os.path.join(tmp, "tmpdir")
os.makedirs(os.environ["TMPDIR"], exist_ok=True)

W = load(WRAP, "cmwrap_t")

# Setting $TMPDIR is NOT enough and this test learned it the hard way: the
# mkdtemp() above already called tempfile.gettempdir(), which caches, so the
# wrapper's CTX_DIR resolved to the REAL temp dir and this test wrote a context
# file into the user's system temp -- outside the tree it cleans up. Redirect
# the module cache too, and pin CTX_DIR (already computed at import) by hand.
tempfile.tempdir = os.environ["TMPDIR"]
W.CTX_DIR = os.path.join(os.environ["TMPDIR"], f"claude-mate-ctx-{os.getuid()}")


def mk_transcript(cfg, projdir, sid, when=None, body='{"x":1}\n'):
    d = os.path.join(cfg, "projects", projdir)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, sid + ".jsonl")
    with open(p, "w") as fh:
        fh.write(body)
    if when:
        os.utime(p, (when, when))
    return p


# --------------------------------------------------------------------------- #
print("\n-- find_transcript: which file is THIS session's --")

cfg_a = os.path.join(tmp, "accounts", "acct-a")
cwd = "/Users/someone/Projects/thing"
mangled = cwd.replace("/", "-")

old = mk_transcript(cfg_a, mangled, "old-session", when=time.time() - 10_000)
check("a transcript older than our start is NOT adopted",
      W.find_transcript(cfg_a, cwd, since=time.time() - 60) == "")

started = time.time() - 30
mine = mk_transcript(cfg_a, mangled, "mine", when=time.time() - 5)
check("the newest transcript since we started IS adopted",
      W.find_transcript(cfg_a, cwd, since=started) == mine)

# A session in a DIFFERENT directory, newer than ours. The cwd's own directory
# must win -- otherwise switching in one terminal would carry off the
# conversation belonging to another terminal in another project.
other = mk_transcript(cfg_a, "-Users-someone-Projects-other", "elsewhere",
                      when=time.time() - 1)
check("a newer transcript in ANOTHER project does not win",
      W.find_transcript(cfg_a, cwd, since=started) == mine)

check("a config dir with no projects tree yields ''",
      W.find_transcript(os.path.join(tmp, "nope"), cwd, since=0) == "")


# --------------------------------------------------------------------------- #
print("\n-- the tracker file: per terminal, private, atomic, disposable --")

W.MY_TTY = "/dev/ttys042"
os.environ["CLAUDE_CONFIG_DIR"] = cfg_a
real_getcwd, os.getcwd = os.getcwd, lambda: cwd
try:
    W.ctx_write("working", started)
finally:
    os.getcwd = real_getcwd

p = W.ctx_path()
check("the file is named for the terminal, not the user",
      os.path.basename(p) == "ttys042.json")
check("...and lives under the temp dir, so a reboot clears it",
      p.startswith(tempfile.gettempdir()))
check("the file is private (0600)", oct(os.stat(p).st_mode)[-3:] == "600")
check("its directory is private (0700)",
      oct(os.stat(os.path.dirname(p)).st_mode)[-3:] == "700")
check("no .tmp scratch file is left behind",
      not [f for f in os.listdir(os.path.dirname(p)) if f.endswith(".tmp")])

ctx = json.load(open(p))
check("it records the transcript it found", ctx["transcript"] == mine)
check("...and derives the session id from the filename",
      ctx["session_id"] == "mine")
check("it records the account", ctx["account"] == "acct-a")
check("it records the cwd", ctx["cwd"] == cwd)
check("it records the state", ctx["state"] == "working")

# Two terminals, two files -- a switch in one must not move the other's work.
W.MY_TTY = "/dev/ttys043"
real_getcwd, os.getcwd = os.getcwd, lambda: cwd
try:
    W.ctx_write("idle", started)
finally:
    os.getcwd = real_getcwd
check("a second terminal gets its OWN context file",
      os.path.exists(W.ctx_path()) and W.ctx_path() != p)
W.ctx_clear()
check("...and clearing one leaves the other alone",
      not os.path.exists(W.ctx_path()) and os.path.exists(p))

W.MY_TTY = "/dev/ttys042"
W.ctx_clear()
check("cleanup removes the context file", not os.path.exists(p))
W.ctx_clear()
check("clearing twice is not an error", True)


# --------------------------------------------------------------------------- #
print("\n-- the switch itself --")

# Two logged-in profiles, same owner, plus one in another org.
def mk_profile(name, email):
    d = os.path.join(tmp, "accounts", name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, ".claude.json"), "w") as fh:
        json.dump({"oauthAccount": {"emailAddress": email}}, fh)
    return d


mk_profile("acct-a", "me@example.com")
cfg_b = mk_profile("acct-b", "me@example.com")
cfg_c = mk_profile("acct-c", "me@other-org.test")

S = load(SWITCH, "cmswitch_t")

dest_b = {"name": "acct-b", "dir": cfg_b, "email": "me@example.com", "chip": "5h90%"}
ctx_live = {"tty": "/dev/ttys042", "cwd": cwd, "account": "acct-a",
            "config_dir": cfg_a, "transcript": mine, "session_id": "mine",
            "state": "working", "updated_at": time.time()}

check("the target path keeps the SOURCE directory name",
      S.target_transcript(ctx_live, cfg_b) ==
      os.path.join(cfg_b, "projects", mangled, "mine.jsonl"))

check("domain_of pulls the org out of an email",
      S.domain_of("a@b.co") == "b.co" and S.domain_of("") == "")

# A stale tracker must not offer to migrate a conversation that has ended.
os.makedirs(W.CTX_DIR, mode=0o700, exist_ok=True)
S.W.MY_TTY = "/dev/ttys044"
with open(S.W.ctx_path(), "w") as fh:
    json.dump(dict(ctx_live, updated_at=time.time() - 99_999), fh)
check("the CLI's own context dir is inside the test tree, not system temp",
      S.W.CTX_DIR.startswith(tmp))
check("a stale context is rejected, not resumed", S.read_context() is None)
with open(S.W.ctx_path(), "w") as fh:
    json.dump(ctx_live, fh)
check("a fresh context is accepted", (S.read_context() or {}).get("session_id") == "mine")


# --------------------------------------------------------------------------- #
print("\n-- end to end, through the real CLI --")

env = dict(os.environ, CLAUDE_MATE_ACCOUNTS_DIR=os.path.join(tmp, "accounts"),
           TMPDIR=os.environ["TMPDIR"], CLAUDE_CONFIG_DIR=cfg_a,
           CLAUDE_MATE_USAGE_POLL_S="0")


def run(args, tty="/dev/ttys045", ctx=None):
    """Drive the CLI with a context file planted for `tty`."""
    e = dict(env)
    key = os.path.basename(tty)
    d = os.path.join(os.environ["TMPDIR"], f"claude-mate-ctx-{os.getuid()}")
    os.makedirs(d, mode=0o700, exist_ok=True)
    if ctx is not None:
        with open(os.path.join(d, key + ".json"), "w") as fh:
            json.dump(ctx, fh)
    # The CLI keys off the tty it is attached to; the test has none, so name
    # the planted context explicitly -- the same --tty a user needs to act on a
    # session running in a different pane.
    return subprocess.run([sys.executable, SWITCH, "--tty", key] + args, env=e,
                          capture_output=True, text=True, timeout=60)


r = run(["--dry-run", "acct-b"], ctx=ctx_live)
landed = os.path.join(cfg_b, "projects", mangled, "mine.jsonl")
check("--dry-run copies nothing", not os.path.exists(landed))
check("--dry-run still says where it would go",
      "acct-b" in r.stdout and "mine" in r.stdout)

r = run(["acct-a"], ctx=ctx_live)
check("switching to the account you are already on is refused",
      r.returncode != 0 and "already on" in (r.stdout + r.stderr))

r = run(["nosuch"], ctx=ctx_live)
check("an unknown account is refused, and lists the real ones",
      r.returncode != 0 and "acct-b" in (r.stdout + r.stderr))

r = run(["acct-b"], ctx=dict(ctx_live, transcript="", session_id=""))
check("a terminal with no transcript yet is told so, not crashed",
      r.returncode != 0 and "no transcript" in (r.stdout + r.stderr).lower())

r = run(["acct-b"], tty="/dev/ttys046")     # no context planted
check("a terminal with no live session is told so",
      r.returncode != 0 and "no live wrapped session" in (r.stdout + r.stderr))

# Cross-organisation: must warn, and must not proceed unprompted.
r = run(["--dry-run", "acct-c"], ctx=ctx_live)
check("a cross-organisation move is called out by domain",
      "other-org.test" in r.stdout and "different" in r.stdout.lower())

print(f"\n{checks - len(failures)}/{checks} checks passed")
shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print("FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("account switch: OK")
