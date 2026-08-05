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
print("\n-- ...for a REAL cwd, whose name claude mangles more than the test did --")

# The bug this section exists for. The old search preferred the directory named
# cwd.replace("/", "-") -- but claude mangles "_" and "." to "-" as well, so for
# any path containing one (/Users/x/Projects/claude_mate) the preferred name
# never existed, the search fell through to "newest .jsonl anywhere", and a
# switch walked off with an unrelated project's conversation. Observed in the
# field: a terminal in claude_mate offering to carry a 7 MB conversation that
# belonged to another terminal in another project.
cfg_u = os.path.join(tmp, "accounts", "acct-u")
u_cwd = "/Users/someone/Projects/claude_mate"
u_dir = "-Users-someone-Projects-claude-mate"          # what claude really uses


def body(cwd_value, sid="s"):
    """A transcript that says where it belongs, as claude's really do."""
    return (json.dumps({"type": "mode", "sessionId": sid}) + "\n"
            + json.dumps({"type": "user", "cwd": cwd_value, "sessionId": sid}) + "\n")


u_started = time.time() - 60
u_mine = mk_transcript(cfg_u, u_dir, "mine-u", when=time.time() - 20,
                       body=body(u_cwd, "mine-u"))
u_other = mk_transcript(cfg_u, "-Users-someone-Projects-aladdin", "theirs",
                        when=time.time() - 1,
                        body=body("/Users/someone/Projects/aladdin", "theirs"))
check("an underscore in the path no longer hides our own project directory",
      W.find_transcript(cfg_u, u_cwd, since=u_started) == u_mine)
check("...so a busier conversation in another project is not adopted",
      W.find_transcript(cfg_u, u_cwd, since=u_started) != u_other)
check("both manglings of a cwd are recognised",
      W.project_dir_names(u_cwd) == {u_cwd.replace("/", "-"), u_dir})

# Same mangled name, different directory: /a/b_c and /a/b-c collide. The file
# says which one it is, and that answer beats the filename.
collide = mk_transcript(cfg_u, u_dir, "collided", when=time.time() - 2,
                        body=body("/Users/someone/Projects/claude-mate", "collided"))
check("a transcript that records a DIFFERENT cwd is rejected",
      W.find_transcript(cfg_u, u_cwd, since=u_started) == u_mine)
check("transcript_cwd reads the directory out of the file",
      W.transcript_cwd(collide) == "/Users/someone/Projects/claude-mate"
      and W.transcript_cwd(os.path.join(tmp, "nope.jsonl")) == "")


# --------------------------------------------------------------------------- #
print("\n-- ...or, better, no search at all: the id we gave claude --")

# The real fix. A session that was told its own id does not have to recognise
# its transcript by mtime, by directory name, or at all -- the file is the one
# with that name, wherever the mangling put it, from the first turn onward.
old_but_ours = mk_transcript(cfg_u, u_dir, "known-id",
                             when=time.time() - 99_999, body=body(u_cwd, "known-id"))
check("a known session id finds its transcript regardless of age",
      W.find_transcript(cfg_u, u_cwd, since=time.time(), sid="known-id") == old_but_ours)
check("...and regardless of what else is newer",
      W.find_transcript(cfg_u, u_cwd, since=0, sid="known-id") == old_but_ours)
check("an id with no file yet is not faked",
      W.find_transcript(cfg_u, u_cwd, since=0, sid="not-started") == "")

_real_claude, _cached = W.real_claude, W.claude_caps_cached
W.real_claude = lambda: "/nonexistent/claude"
W.claude_caps_cached = lambda _b: True
argv, sid = W.session_argv(["--dangerously-skip-permissions"])
check("a fresh session is given an id, and we keep it",
      sid and argv == ["--dangerously-skip-permissions", "--session-id", sid])
check("...a real uuid, not a short hex tag", len(sid) == 36 and sid.count("-") == 4)
check("--resume <id> already names the conversation",
      W.session_argv(["--resume", "abc-123"]) == (["--resume", "abc-123"], "abc-123"))
check("--session-id from the user is respected, not doubled",
      W.session_argv(["--session-id", "u-1"]) == (["--session-id", "u-1"], "u-1"))
check("--continue leaves the id to claude, and asks for nothing",
      W.session_argv(["-c"]) == (["-c"], ""))
check("a bare --resume likewise",
      W.session_argv(["--resume"]) == (["--resume"], ""))
check("--fork-session mints an id we are not told, so we claim none",
      W.session_argv(["--resume", "abc-123", "--fork-session"])[1] == "")
W.claude_caps_cached = lambda _b: False
check("a claude too old for --session-id is left alone, not broken",
      W.session_argv(["-p"]) == (["-p"], ""))
W.claude_caps_cached = lambda _b: None
check("an unasked claude is left alone too -- the scan covers that session",
      W.session_argv(["-p"]) == (["-p"], ""))
W.real_claude, W.claude_caps_cached = _real_claude, _cached

# Whether a claude HAS the flag is remembered, never waited for: the only way to
# ask is to run it, and nothing may sit in front of a terminal you just opened.
fake = os.path.join(tmp, "fake-claude")
with open(fake, "w") as fh:
    fh.write("#!/bin/sh\necho '  --session-id <uuid>  Use a specific session ID'\n")
os.chmod(fake, 0o755)
check("a claude nobody has asked about yet has no remembered answer",
      W.claude_caps_cached(fake) is None)

slow = os.path.join(tmp, "slow-claude")
with open(slow, "w") as fh:
    fh.write("#!/bin/sh\nsleep 30\n")
os.chmod(slow, 0o755)
t0 = time.time()
W.session_argv([])                       # real_claude() won't find `slow`, but
elapsed = time.time() - t0               # nothing here may run a binary at all
check("...and starting a session does not wait to find out (no probe on the "
      "startup path)", elapsed < 1.0)

check("the probe reads the answer out of --help",
      W.probe_session_id_support(fake) is True)
check("...and remembers it", W.claude_caps_cached(fake) is True)
old_stat = os.stat(fake)
swapped = "#!/bin/sh\necho '  --resume <id>'\n"   # no flag; same size, same mtime
with open(fake, "w") as fh:
    fh.write(swapped + "#" * (old_stat.st_size - len(swapped)))
os.utime(fake, (old_stat.st_atime, old_stat.st_mtime))
check("...without asking again while the binary is the same",
      W.claude_caps_cached(fake) is True)
plain = os.path.join(tmp, "plain-claude")
with open(plain, "w") as fh:
    fh.write("#!/bin/sh\necho '  --resume <id>'\n")
os.chmod(plain, 0o755)
check("a claude without the flag is remembered as such, not assumed",
      W.probe_session_id_support(plain) is False
      and W.claude_caps_cached(plain) is False)
check("a binary that cannot be probed remembers nothing",
      W.probe_session_id_support(os.path.join(tmp, "no-such-claude")) is False
      and W.claude_caps_cached(fake) is True)      # the good answer survives


# --------------------------------------------------------------------------- #
print("\n-- the request from the device: an account name is ONE argument --")

# "work 2" is an ordinary name for a profile directory, and it used to be split
# into two arguments on its way to the switcher, which rejected the second --
# after claude had already been killed to make room for the switch.
check("a name with a space stays one argument",
      W.switch_argv("work 2") == ["work 2"])
check("a plain name is unchanged", W.switch_argv("work") == ["work"])
check("a flag request is still a list of words",
      W.switch_argv("--best") == ["--best"])
check("surrounding whitespace is not an argument",
      W.switch_argv("  work 2  ") == ["work 2"])


# --------------------------------------------------------------------------- #
print("\n-- ...and what the wrapper refuses BEFORE it kills claude for it --")


def mk_profile(name, email):
    """A logged-in profile: a directory with an account recorded in it."""
    d = os.path.join(tmp, "accounts", name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, ".claude.json"), "w") as fh:
        json.dump({"oauthAccount": {"emailAddress": email}}, fh)
    return d


cfg_a = mk_profile("acct-a", "me@example.com")
cfg_b = mk_profile("acct-b", "me@example.com")
cfg_c = mk_profile("acct-c", "me@other-org.test")
W.ACCOUNTS_DIR = os.path.join(tmp, "accounts")
os.environ["CLAUDE_CONFIG_DIR"] = cfg_a
W.g_claude_sid, W.g_started_at = "mine", 0.0
# The session's own transcript, so "is there anything to carry" answers yes.
mk_transcript(cfg_a, mangled, "mine", when=time.time() - 5)

check("a switch to the account you are already on is refused, not performed",
      "already on" in W.switch_refusal("acct-a"))
check("...an account that does not exist likewise",
      "no such account" in W.switch_refusal("nosuch"))
check("...and a move to ANOTHER ORGANISATION most of all",
      "other-org.test" in W.switch_refusal("acct-c"))
check("...naming where to do that deliberately",
      "claude-mate-switch" in W.switch_refusal("acct-c"))
check("a switch between your own accounts is allowed",
      W.switch_refusal("acct-b") == "")
check("--best is a request, not an account name",
      W.switch_refusal("--best") == "")

W.g_claude_sid = "never-started"
check("a session with nothing to carry is refused while it is still alive",
      "nothing to carry" in W.switch_refusal("acct-b"))
W.g_claude_sid = "mine"


# --------------------------------------------------------------------------- #
print("\n-- the tracker file: per terminal, private, atomic, disposable --")

W.MY_TTY = "/dev/ttys042"
os.environ["CLAUDE_CONFIG_DIR"] = cfg_a
W.g_user_argv = ["--dangerously-skip-permissions"]
W.g_claude_sid = ""                  # this section is about the fallback scan
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
# The flags too: a switch that resumes without them gives you the conversation
# back in a session that behaves differently from the one you were in.
check("it records the flags the session was started with",
      ctx["argv"] == ["--dangerously-skip-permissions"])

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

# acct-a / acct-b (same owner) and acct-c (another org) were built above.
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

# What the resumed session is started with. Everything the user chose survives;
# anything naming a DIFFERENT conversation is replaced by the one we carried.
check("the session's own flags survive the switch",
      S.resume_argv({"argv": ["--dangerously-skip-permissions", "--model", "opus"]},
                    "mine") ==
      ["--dangerously-skip-permissions", "--model", "opus", "--resume", "mine"])
check("...and a flag naming another conversation does not",
      S.resume_argv({"argv": ["--resume", "old-id", "--verbose"]}, "mine") ==
      ["--verbose", "--resume", "mine"])
check("...including the ones that take no value",
      S.resume_argv({"argv": ["-c", "--verbose"]}, "mine") ==
      ["--verbose", "--resume", "mine"])
check("...and the --flag=value spelling",
      S.resume_argv({"argv": ["--session-id=old", "--verbose"]}, "mine") ==
      ["--verbose", "--resume", "mine"])
check("a context from an older wrapper (no argv) still resumes",
      S.resume_argv({}, "mine") == ["--resume", "mine"])

# The move itself, with the exec stubbed: it must land in the conversation's own
# directory. `claude --resume <id>` looks for the transcript under the project
# directory derived from the CWD, so a resume started anywhere else says "No
# conversation found with session ID" while the copy sits one directory away --
# seen in the field, from a terminal that had cd'd elsewhere.
work_dir = os.path.join(tmp, "work-dir")
os.makedirs(work_dir, exist_ok=True)
# Its own destination profile: this one really copies, and the --dry-run check
# further down means nothing if another test has already put the file there.
cfg_d = mk_profile("acct-d", "me@example.com")
dest_d = {"name": "acct-d", "dir": cfg_d, "email": "me@example.com", "chip": "5h90%"}
seen = {}


def fake_execve(path, argv, env):
    seen.update(path=path, argv=argv, env=env, cwd=os.getcwd())
    raise SystemExit(0)


back, real_execve = os.getcwd(), os.execve
os.execve = fake_execve
try:
    S.switch(dict(ctx_live, cwd=work_dir,
                  argv=["--dangerously-skip-permissions"]), dest_d, yes=True)
except SystemExit:
    pass
finally:
    os.execve = real_execve
    os.chdir(back)

check("the switch resumes in the conversation's own directory",
      os.path.realpath(seen.get("cwd", "")) == os.path.realpath(work_dir))
check("...through the wrapper, so the new session is still tracked",
      seen.get("argv", [])[1] == SWITCH.replace("claude-mate-switch",
                                                "claude-mate-wrap"))
check("...resuming the conversation it just copied, with its own flags",
      seen.get("argv", [])[2:] ==
      ["--dangerously-skip-permissions", "--resume", "mine"])
check("...as the target account, with the picker skipped",
      seen.get("env", {}).get("CLAUDE_CONFIG_DIR") == cfg_d
      and seen.get("env", {}).get("CLAUDE_MATE_ACCOUNT") == "acct-d")
check("...and the transcript really landed there",
      os.path.exists(os.path.join(cfg_d, "projects", mangled, "mine.jsonl")))


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
