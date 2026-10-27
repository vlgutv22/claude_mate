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
import contextlib
import importlib.machinery
import importlib.util
import io
import itertools
import json
import os
import re
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
# THE DEVELOPER'S OWN ACCOUNT MUST NOT REACH THIS TEST. select_account()
# consults CLAUDE_MATE_ACCOUNT before it ever offers the picker, so anyone who
# actually uses the feature -- exports it in their shell, which is the whole
# point of it existing -- got fourteen unexplained failures in the picker
# section and nowhere else. CI never has it set, so the suite was green there
# and red on the machine of the person most likely to be changing this file.
# CLAUDE_CONFIG_DIR is popped further down for the same reason; this is its
# pair, and it belongs up here with the rest of the sandbox.
os.environ.pop("CLAUDE_MATE_ACCOUNT", None)

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
print("\n-- ...and a conversation claude carries on without naming it --")

# -c, a bare --resume (claude's own picker), -r <name> and --from-pr carry on a
# conversation that is already there, and do not say which. The wrapper used
# to bet on the NEWEST transcript for -c, and to give up on the rest. But
# claude's --continue skips `claude -p` and SDK runs, /loop, --bg and teammate
# transcripts: the history kept a -p job under this terminal's directory and
# flags while the chat it really carried on went unrecorded -- and a chat open
# in a terminal, never named, read as closed, so History offered to resume it
# a second time. Now it is the ONE transcript that was there at launch, records
# this directory and has grown since, looked for after this session's own
# first turn; two such is no answer at all.
U1 = "11111111-1111-4111-8111-111111111111"
_carry = {a: W.conv_mode(list(a), W.session_argv(list(a))[1])
          for a in (("-c",), ("--continue",), ("--resume",), ("-r",),
                    ("-r", "my chat"), ("--resume=my-chat",), ("--from-pr",),
                    ("--from-pr", "123"), ("--from-pr=123",))}
check("-c, a bare --resume, -r <name> and --from-pr all carry on a conversation "
      "that is already there" + "".join(f"; {a} -> {m!r}" for a, m in _carry.items()
                                        if m != "carry"),
      set(_carry.values()) == {"carry"})
check("...while --fork-session makes a new one, whatever it forks",
      W.conv_mode(["-c", "--fork-session"], "") == "new"
      and W.conv_mode(["--resume", U1, "--fork-session"], "") == "new")
check("an id in hand is known; a plain start is new; a --session-id that is no "
      "UUID (claude refuses it) is nothing",
      W.conv_mode(["--resume", U1], U1) == "known" and W.conv_mode([], "") == "new"
      and W.conv_mode(["--session-id", "x"], "x") == "")

c_cfg = os.path.join(tmp, "carry-cfg")
c_cwd = os.path.realpath(tempfile.mkdtemp(prefix="carry-cwd-", dir=tmp))
c_dir = os.path.join(c_cfg, "projects", re.sub(r"[^a-zA-Z0-9]", "-", c_cwd))


def cid(n):
    return f"{n:08x}-0000-4000-8000-{n:012x}"


def carry(argv, there, grow=(), gone=(), turn=True):
    """learn_conversation for a session started with `argv`, in a project
    directory holding `there` at launch -- {id: (cwd recorded or None, entrypoint,
    seconds old)} -- after the transcripts in `grow` grew and those in `gone`
    went. Returns (id learned, mode left)."""
    shutil.rmtree(c_dir, ignore_errors=True)
    os.makedirs(c_dir)
    now = time.time()
    for n, (rec_cwd, entry, age) in there.items():
        path = os.path.join(c_dir, n + ".jsonl")
        head = ([{"type": "user", "cwd": rec_cwd, "sessionId": n, "entrypoint": entry}]
                if rec_cwd else [{"type": "summary", "summary": "s"}] * 12)
        with open(path, "w") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in head)
        os.utime(path, (now - age, now - age))
    W.g_conv, W.g_claude_sid, W.g_turn_seen = "", "", turn
    W.g_conv_mode = W.conv_mode(list(argv), W.session_argv(list(argv))[1])
    W.g_launch_transcripts = W.launch_transcripts(c_cfg, c_cwd)
    for n in grow:
        with open(os.path.join(c_dir, n + ".jsonl"), "a") as fh:
            fh.write(json.dumps({"type": "assistant", "cwd": there[n][0] or c_cwd,
                                 "sessionId": n}) + "\n")
    for n in gone:
        os.unlink(os.path.join(c_dir, n + ".jsonl"))
    W.learn_conversation(c_cfg, c_cwd, "")
    return W.g_conv, W.g_conv_mode


A, B, Y = cid(0xA), cid(0xB), cid(0xC)
_chat_and_job = {A: (c_cwd, "cli", 600), B: (c_cwd, "sdk-cli", 60)}
check("-c carries on the chat it continued, not the newer `claude -p` job beside it",
      carry(["-c"], _chat_and_job, grow=[A]) == (A, "carry"))
check("...and while that job is still writing, two have grown: nothing is named, "
      "never the job",
      carry(["-c"], _chat_and_job, grow=[A, B]) == ("", ""))
check("...nor anything before the session's own first turn: a file that grows "
      "then may be anybody's",
      carry(["-c"], _chat_and_job, grow=[A], turn=False) == ("", "carry"))
check("...and with nothing grown yet it waits, rather than give up",
      carry(["-c"], _chat_and_job) == ("", "carry"))
check("the picker's choice is learned the same way: the older chat it picked",
      carry(["--resume"], {A: (c_cwd, "cli", 900), Y: (c_cwd, "cli", 30)},
            grow=[A]) == (A, "carry"))
check("...and -r <name> too",
      carry(["-r", "my chat"], {A: (c_cwd, "cli", 900), Y: (c_cwd, "cli", 30)},
            grow=[A]) == (A, "carry"))
check("a transcript of ANOTHER directory whose name mangles like ours is not ours",
      carry(["-c"], {A: (c_cwd, "cli", 900),
                     Y: (c_cwd.replace("carry-cwd-", "carry_cwd_"), "cli", 30)},
            grow=[A, Y]) == (A, "carry"))
check("one that grew without saying where it belongs is not ruled out: with ours, "
      "two -- no answer",
      carry(["-c"], {A: (c_cwd, "cli", 900), Y: (None, "cli", 30)},
            grow=[A, Y]) == ("", ""))
check("a transcript deleted since launch (claude's own cleanup) costs nothing",
      carry(["-c"], {A: (c_cwd, "cli", 900), Y: (c_cwd, "cli", 86400 * 40)},
            grow=[A], gone=[Y]) == (A, "carry"))
W.g_conv, W.g_conv_mode, W.g_launch_transcripts, W.g_turn_seen = "", "", {}, False


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
check("a switch between your own accounts is allowed",
      W.switch_refusal("acct-b") == "")
# This used to be refused, and refusing it was the bug. Every account on the
# device's picker is one the user installed and logged into; which company each
# belongs to is not a trust boundary the daemon discovered. The check fired on
# every pair of a real three-account fleet -- so the feature could not do the
# one thing it exists for (the account you are on is out of limit) -- and said
# so only in a log file.
check("a move to another ORGANISATION is allowed too: it is your account as well",
      W.switch_refusal("acct-c") == "")
check("...so no refusal mentions organisations any more",
      "organisation" not in (W.switch_refusal("acct-b")
                             + W.switch_refusal("acct-c")))
check("--best is a request, not an account name",
      W.switch_refusal("--best") == "")

W.g_claude_sid = "never-started"
check("a session with nothing to carry is refused while it is still alive",
      "nothing to carry" in W.switch_refusal("acct-b"))
# Every refusal is shown on a 21-char-wide screen, two rows of it. One that does
# not fit arrives on the glass with its second half cut off, which is how the
# advice-bearing end of a sentence gets lost.
for _req in ("acct-a", "nosuch", "acct-b"):
    _why = W.switch_refusal(_req)
    check(f"the refusal {_why!r} ({len(_why)}c) fits the device's two rows",
          len(_why) <= 42)
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
# THE UMASK RACE. ctrl_listener narrows the process umask to 0o177 while it
# binds; a directory made by another thread in that moment is 0600 -- no x bit,
# nothing inside it can be opened. CI hit it. The next write must mend it.
os.chmod(os.path.dirname(p), 0o600)
real_getcwd, os.getcwd = os.getcwd, lambda: cwd
try:
    W.ctx_write("working", started)
finally:
    os.getcwd = real_getcwd
check("a context dir left 0600 by the umask race is put back to 0700, and "
      "the file is written again",
      oct(os.stat(os.path.dirname(p)).st_mode)[-3:] == "700"
      and json.load(open(p)).get("state") == "working")
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
# THE SWITCH KEEPS ITS OWN RULE, NOT THE HISTORY'S. It happens now, from a
# context file that already holds the whole argv, and stores nothing new -- so
# nothing the history leaves out of a record it keeps for days (inline JSON, a
# system prompt, whatever is past the daemon's size budget) may go missing on
# the way to another account. Through the history's rules a read-only agent
# came back as claude's default one (--agents dropped, --agent kept), Remote
# Control was gone, and a long --add-dir took the deny list after it along.
_agents = '{"reviewer":{"description":"d","prompt":"p","tools":["Read","Grep"]}}'
check("the switch keeps inline --agents beside the --agent it defines",
      S.resume_argv({"argv": ["--agents", _agents, "--agent", "reviewer"]}, "mine")
      == ["--agents", _agents, "--agent", "reviewer", "--resume", "mine"])
_inline = ["--append-system-prompt", "be terse", "--remote-control",
           "--settings", '{"permissions":{"deny":["Bash"]}}',
           "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"]
check("...and the system prompt, Remote Control and inline --settings/--mcp-config",
      S.resume_argv({"argv": _inline}, "mine") == _inline + ["--resume", "mine"])
_long = (["--add-dir"] + ["/x/" + "d" * 250] * 5
         + ["--disallowedTools", "Bash", "--model", "opus"])
check("...and every flag, however long the argv: a switch has no size budget",
      S.resume_argv({"argv": _long}, "mine") == _long + ["--resume", "mine"])


# --------------------------------------------------------------------------- #
print("\n-- resume_flags: what a resumed session comes back with --")

# The policy itself, in the wrapper, which the history replays a closed chat's
# flags through as well. It runs before claude starts and on every report, so
# it may never raise: [] for anything that is not an argv.
check("nothing in, nothing out", W.resume_flags([]) == [])
check("not an argv at all: [] rather than an exception",
      W.resume_flags(None) == [] and W.resume_flags([1, "--verbose"]) == []
      and W.resume_flags(object()) == [])
check("a value flag as the last word is dropped -- alone, it would swallow "
      "the --resume after it",
      W.resume_flags(["--verbose", "--model"]) == ["--verbose"])
check("--flag= with nothing after the '=' is still one whole word",
      W.resume_flags(["--model="]) == ["--model="])
check("booleans, values and --flag=value words are kept, in order",
      W.resume_flags(["--model", "opus", "--verbose", "--effort=max", "-n", "my chat"])
      == ["--model", "opus", "--verbose", "--effort=max", "-n", "my chat"])
check("an unknown flag goes, and takes its value with it",
      W.resume_flags(["--frobnicate", "x", "--verbose"]) == ["--verbose"])
check("everything after -- is a prompt, flag-shaped or not",
      W.resume_flags(["--verbose", "--", "--dangerously-skip-permissions"])
      == ["--verbose"])
# A required value is whatever claude's own parser takes it to be -- the next
# word, even one that starts with '-'. Reading it as a flag instead would hand
# a resumed session a permission bypass its original never had.
check("`--name --dangerously-skip-permissions` is a name, and stays one",
      W.resume_flags(["--name", "--dangerously-skip-permissions"])
      == ["--name", "--dangerously-skip-permissions"])
check("...and a dropped flag's flag-shaped value goes with it, never kept as a flag",
      W.resume_flags(["--system-prompt", "--dangerously-skip-permissions"]) == []
      and W.resume_flags(["--add-dir", "--dangerously-skip-permissions"])
      == ["--add-dir", "--dangerously-skip-permissions"])
check("every flag the policy drops goes with its value",
      all(W.resume_flags([f, "v", "--verbose"]) == ["--verbose"]
          for f in sorted(W.RESUME_FLAGS_DROP)))

rf_dir = os.path.join(tmp, "rf-cwd")
os.makedirs(rf_dir, exist_ok=True)
for _f in ("s.json", "m.json"):
    with open(os.path.join(rf_dir, _f), "w") as fh:
        fh.write("{}")
# Inline JSON is where tokens and API keys go, and a chat's record keeps its
# flags until someone deletes it. A FILE is a path, and survives.
check("--settings naming a file is kept, looked up in the session's directory",
      W.resume_flags(["--settings", "s.json"], cwd=rf_dir) == ["--settings", "s.json"]
      and W.resume_flags(["--settings=s.json"], cwd=rf_dir) == ["--settings=s.json"])
check("...and dropped where that file does not exist",
      W.resume_flags(["--settings", "s.json"], cwd=tmp) == [])
check("inline JSON in --settings or --agents is never kept",
      W.resume_flags(["--settings", '{"env":{"API_KEY":"sk-x"}}', "--verbose"],
                     cwd=rf_dir) == ["--verbose"]
      and W.resume_flags(["--agents", '{"r":{"prompt":"x"}}'], cwd=rf_dir) == [])
# AN AGENT WHOSE DEFINITION WENT. An interactive claude takes --agents only
# inline, so it never survives; an --agent naming one of its agents would come
# back with claude's default tools -- and no prompts under the bypass beside it.
check("an --agent whose inline --agents went keeps NO flags, the bypass included",
      W.resume_flags(["--agents", '{"ro":{"tools":["Read"]}}', "--agent", "ro",
                      "--dangerously-skip-permissions", "--model", "opus"],
                     cwd=rf_dir) == []
      and W.resume_flags(["--agents=" + '{"ro":{}}', "--agent=ro", "--verbose"],
                         cwd=rf_dir) == [])
check("...while inline --agents with no --agent, or an --agent alone, keep the rest",
      W.resume_flags(["--agents", '{"r":{}}', "--model", "opus"], cwd=rf_dir)
      == ["--model", "opus"]
      and W.resume_flags(["--agent", "reviewer", "--verbose"], cwd=rf_dir)
      == ["--agent", "reviewer", "--verbose"])
check("--mcp-config keeps only the values that are files",
      W.resume_flags(["--mcp-config", "m.json", '{"mcpServers":{}}', "--verbose"],
                     cwd=rf_dir) == ["--mcp-config", "m.json", "--verbose"])
check("...and goes entirely when none of them is",
      W.resume_flags(["--mcp-config", '{"mcpServers":{}}'], cwd=rf_dir) == [])
check("a variadic flag keeps every value up to the next flag",
      W.resume_flags(["--add-dir", "a", "b", "--allowedTools", "Bash(git *)", "Edit",
                      "--verbose"])
      == ["--add-dir", "a", "b", "--allowedTools", "Bash(git *)", "Edit", "--verbose"])
check("...but its --flag=value form is one value; a word after it is a prompt",
      W.resume_flags(["--allowedTools=Edit", "fix it"]) == ["--allowedTools=Edit"])

check(f"a value over {W.RESUME_VALUE_MAX} characters goes, with its flag",
      W.resume_flags(["--name", "x" * (W.RESUME_VALUE_MAX + 1), "--verbose"])
      == ["--verbose"]
      and W.resume_flags(["--name", "x" * W.RESUME_VALUE_MAX])
      == ["--name", "x" * W.RESUME_VALUE_MAX])
_big = ["--name", "a" * 250, "--model", "b" * 250, "--agent", "c" * 250,
        "--fallback-model", "d" * 250, "--effort", "max", "--verbose"]
_kept = W.resume_flags(_big)
check(f"past {W.RESUME_TOTAL_MAX} characters nothing more is added "
      f"({sum(map(len, _kept))} kept)",
      _kept == _big[:6] + ["--verbose"])
check("...except a boolean, which is always kept",
      _kept[-1] == "--verbose")
# WHAT THE DAEMON WILL TAKE. It refuses a flags list of more than
# HISTORY_FLAGS_MAX words, or with a control character in any of them -- the
# whole list, permission flags included -- so the wrapper never sends one.
_many = ["--add-dir", "d"] * 40 + ["--verbose"]
_cut = W.resume_flags(_many)
check(f"at most {W.RESUME_WORDS_MAX} words are kept, whole flags only "
      f"({len(_cut)} kept)",
      len(_cut) <= W.RESUME_WORDS_MAX and _cut == _many[:len(_cut)]
      and len(_cut) % 2 == 0)
check("a value with a control character goes, with its flag",
      W.resume_flags(["--name", "a\tb", "--model", "opus", "--verbose"])
      == ["--model", "opus", "--verbose"]
      and W.resume_flags(["--name=a\x1bb", "--verbose"]) == ["--verbose"])
# RESTRICTIONS ARE NEVER LOST. The booleans that skip every permission prompt
# are always kept, so a budget that dropped the deny list, plan mode or
# --restricted behind a long --add-dir resumed a chat freer than it ever ran:
# every prompt skipped, nothing denied. A restriction is kept past the size
# budget; one that still cannot be kept leaves no flags at all -- which drops
# the bypass too, the safe way to be wrong.
_restrict = getattr(W, "RESUME_FLAGS_RESTRICT", frozenset())
check("the restrictions are flags the policy keeps -- booleans, values or lists",
      bool(_restrict) and _restrict <= (W.RESUME_FLAGS_BOOL | W.RESUME_FLAGS_VALUE
                                        | W.RESUME_FLAGS_MANY))
_dirs = ["/Users/me/Projects/acme/monorepo/packages/service-%02d/src" % n
         for n in range(17)]
_deny = ["--disallowedTools", "Bash(rm:*)", "Bash(git push:*)", "WebFetch",
         "--permission-mode", "plan"]
_r = W.resume_flags(["--dangerously-skip-permissions", "--add-dir"] + _dirs + _deny)
check(f"a deny list and plan mode survive past {W.RESUME_TOTAL_MAX} characters, "
      f"behind a long --add-dir ({sum(map(len, _r))} kept)",
      _r == ["--dangerously-skip-permissions", "--add-dir"] + _dirs + _deny
      and sum(map(len, _r)) > W.RESUME_TOTAL_MAX)
check("...in the --flag=value spelling too, while the budget still drops the rest",
      W.resume_flags(["--add-dir"] + _dirs + ["--model", "m" * 60, "--tools=Read,Grep",
                                              "--permission-mode=plan"])
      == ["--add-dir"] + _dirs + ["--tools=Read,Grep", "--permission-mode=plan"])
_allow = ["Bash(npm run task-%02d:*)" % n for n in range(45)]
check("...and once the budget has run out, a deny list after it is still kept",
      W.resume_flags(["--dangerously-skip-permissions", "--allowedTools"] + _allow
                     + ["--disallowedTools", "Bash(rm:*)"])
      == ["--dangerously-skip-permissions", "--disallowedTools", "Bash(rm:*)"])
_short = ["T%d" % n for n in range(70)]
check(f"a restriction past the {W.RESUME_WORDS_MAX}-word cap leaves no flags at all: "
      f"no bypass either",
      W.resume_flags(["--dangerously-skip-permissions", "--allowedTools"] + _short
                     + ["--tools", "Read", "--disallowedTools", "Bash"]) == []
      and W.resume_flags(["--dangerously-skip-permissions", "--allowedTools"] + _short
                         + ["--restricted"]) == []
      and W.resume_flags(["--dangerously-skip-permissions", "--disallowedTools"]
                         + _short) == [])
check(f"...nor does one with a value over {W.RESUME_VALUE_MAX} characters, or a "
      f"control character in it",
      W.resume_flags(["--dangerously-skip-permissions", "--disallowedTools",
                      "Bash(" + "x" * W.RESUME_VALUE_MAX + ")"]) == []
      and W.resume_flags(["--dangerously-skip-permissions",
                          "--permission-mode=pl\tan"]) == [])
check("--permission-mode bypassPermissions passes the budget like the boolean "
      "bypass -- but losing it to the word cap only loses a bypass",
      W.resume_flags(["--add-dir"] + _dirs + ["--model", "m" * 60, "--permission-mode",
                                              "bypassPermissions"])
      == ["--add-dir"] + _dirs + ["--permission-mode", "bypassPermissions"]
      and W.resume_flags(["--verbose", "--allowedTools"] + _short
                         + ["--permission-mode", "bypassPermissions"]) == ["--verbose"])
# A SHORT-OPTION CLUSTER IS SEVERAL FLAGS, the way claude's parser reads it:
# -cn is -c then -n, and -n takes the next word whatever it looks like -- so
# `claude -cn --dangerously-skip-permissions` continues a session NAMED that,
# and skips nothing. Read as a flag, that name came back as the bypass.
check("`-cn --dangerously-skip-permissions` is a name: no bypass comes back",
      W.resume_flags(["-cn", "--dangerously-skip-permissions"]) == []
      and W.resume_flags(["-cpn", "--dangerously-skip-permissions"]) == []
      and W.resume_flags(["-pn", "--dangerously-skip-permissions", "hi"]) == [])
check("...a plain word after a cluster goes with it, and what follows is read as "
      "before", W.resume_flags(["-cn", "my chat", "--verbose"]) == ["--verbose"])
# Whether the word after a cluster is the cluster's value or a flag of its own
# depends on the letters: -nc is -n with the value "c", so after it come real
# flags. Dropping just that word moved the guess on to the next one.
check("...but a word after it that looks like a flag, `--` too, leaves no flags at "
      "all: claude may have read it either way",
      W.resume_flags(["-nc", "--model", "--dangerously-skip-permissions"]) == []
      and W.resume_flags(["--dangerously-skip-permissions", "-cn", "--",
                          "--restricted"]) == []
      and W.resume_flags(["-cn", "--permission-mode", "plan", "--verbose"]) == [])
# ITEMS, FOR WHATEVER HAS TO REASON ABOUT A FLAG: the app labels a resume by
# its permission mode and swaps the mode, and a word alone cannot say whether
# it is a flag or the value of the one before it.
check("resume_flag_items pairs every flag with its values",
      W.resume_flag_items(["--name", "--dangerously-skip-permissions",
                           "--add-dir", "a", "b", "--effort=max", "--verbose"])
      == [["--name", "--dangerously-skip-permissions"], ["--add-dir", "a", "b"],
          ["--effort=max"], ["--verbose"]])
check("...and resume_flags is exactly its words, in order",
      all(W.resume_flags(a) == [w for it in W.resume_flag_items(a) for w in it]
          for a in (_big, _many, ["--model", "x", "fix it", "--chrome"], [])))

# THIS LIST IS A LOCK, NOT AN OBSERVATION. Every option `claude --help` prints
# (Claude Code 2.1.311), with its argument as the help writes it: <x> takes one
# value, <x...> several, [x] may take one. Each one must be in EXACTLY ONE of
# the wrapper's sets -- kept as a boolean, a value, a file or a list, or
# dropped -- and with the argument shape the help gives it. When a Claude Code
# upgrade adds an option, paste the new --help lines here, and decide in the
# wrapper whether a resumed session should come back with it; until then this
# fails. An unclassified flag is not harmless: it is dropped on every resume,
# and nobody has said whether that is right.
HELP_2_1_311 = """
--add-dir <directories...>
--agent <agent>
--agents <json-or-file>
--allow-dangerously-skip-permissions
--allowedTools, --allowed-tools <tools...>
--append-system-prompt <prompt>
--autocompact <auto|tokens>
--ax-screen-reader
--bg, --background
--bare
--betas <betas...>
--brief
--chrome
--cloud [description|session_id|url]
-c, --continue
--dangerously-skip-permissions
-d, --debug [filter]
--debug-file <path>
--desktop
--disable-slash-commands
--disallowedTools, --disallowed-tools <tools...>
--effort <level>
--environment <environment_id>
--exclude-dynamic-system-prompt-sections
--fallback-model <model>
--file <specs...>
--fork-session
--forward-subagent-text
--from-pr [value]
-h, --help
--ide
--include-hook-events
--include-partial-messages
--input-format <format>
--json-schema <schema>
--max-budget-usd <amount>
--mcp-config <configs...>
--model <model>
-n, --name <name>
--no-chrome
--no-session-persistence
--output-format <format>
--permission-mode <mode>
--permission-prompts <target>
--plugin-dir <path>
--plugin-url <url>
-p, --print
--prompt-suggestions [value]
--remote-control [name]
--remote-control-session-name-prefix <prefix>
--replay-user-messages
--restricted
-r, --resume [value]
--safe-mode
--session-id <uuid>
--setting-sources <sources>
--settings <file-or-json>
--strict-mcp-config
--system-prompt <prompt>
--system-prompt-snapshot <on|off>
--teleport [session]
--tmux
--tools <tools...>
--verbose
-v, --version
-w, --worktree [name]
"""
HELP_ARGS = {}                      # flag -> '' none, 'one', 'many' or 'maybe'
for _line in HELP_2_1_311.strip().splitlines():
    _m = re.fullmatch(r"(-[\w-]+(?:, -[\w-]+)*)(?: (.+))?", _line)
    _arg = _m.group(2) or ""
    for _n in _m.group(1).split(", "):
        HELP_ARGS[_n] = ("" if not _arg else "maybe" if _arg.startswith("[")
                         else "many" if _arg.endswith("...>") else "one")
SETS = {"boolean": W.RESUME_FLAGS_BOOL, "value": W.RESUME_FLAGS_VALUE,
        "file": W.RESUME_FLAGS_FILE, "list": W.RESUME_FLAGS_MANY,
        "dropped": W.RESUME_FLAGS_DROP}
WANT = {"boolean": "", "value": "one", "file": "one", "list": "many"}
unclassified, misshapen = [], []
for _n, _arg in sorted(HELP_ARGS.items()):
    _in = [k for k, s in SETS.items() if _n in s]
    if len(_in) != 1:
        unclassified.append(f"{_n} ({' and '.join(_in) or 'nowhere'})")
    elif _in[0] == "dropped":
        # The word after a dropped flag that REQUIRES a value is that value,
        # whatever it looks like -- or it would come back as a flag.
        if (_n in W._DROP_TAKES_VALUE) != (_arg in ("one", "many")):
            misshapen.append(_n)
    elif _arg != WANT[_in[0]]:
        misshapen.append(_n)
check(f"every one of the {len(HELP_ARGS)} options in claude's --help is classified "
      f"exactly once" + (f": {unclassified}" if unclassified else ""),
      len(HELP_ARGS) == 77 and not unclassified)
check("...each with the argument the help gives it (a value read as a flag is a "
      "flag the original never had)" + (f": {misshapen}" if misshapen else ""),
      not misshapen and W._DROP_TAKES_VALUE <= W.RESUME_FLAGS_DROP)

# THE PROPERTY THAT MATTERS, over every short argv a pool of awkward words can
# make: claude, reading what is replayed plus `--resume SID`, must resume SID,
# see no prompt, and see no option or value the original launch did not have.
# parse() reads words the way claude's own parser (commander) does, by the
# argument shapes in the help above -- not by resume_flags' sets.


def parse(words):
    """claude's reading of `words`: (set of (flag, value), [prompt words])."""
    opts, prompt, i, many = set(), [], 0, None
    while i < len(words):
        w = words[i]
        i += 1
        flagish = len(w) > 1 and w.startswith("-")
        if many and not flagish:
            opts.add((many, w))
            continue
        many = None
        if w == "--":
            prompt += words[i:]
            break
        if not flagish:
            prompt.append(w)
            continue
        flag, eq, val = w.partition("=")
        if eq and w.startswith("--") and HELP_ARGS.get(flag):
            opts.add((flag, val))           # --flag=value: one value, no more
            continue
        kind = HELP_ARGS.get(w)
        short = "-" + w[1:2]
        if kind is None and len(w) > 2 and short != "--" and short in HELP_ARGS:
            # A cluster: -cn is -c, then -n. A letter that takes a value takes
            # the rest of the word as that value (-nc names the session "c").
            if HELP_ARGS[short]:
                opts.add((short, w[2:]))
            else:
                opts.add((short, None))
                words = words[:i] + ["-" + w[2:]] + words[i:]
            continue
        if kind in ("one", "many") and i < len(words):
            opts.add((w, words[i]))         # required: the next word, whatever it is
            i += 1
            many = w if kind == "many" else None
        elif kind == "maybe" and i < len(words) and not (
                len(words[i]) > 1 and words[i].startswith("-")):
            opts.add((w, words[i]))
            i += 1
        else:
            opts.add((w, None))
    return opts, prompt


POOL = ["--model", "opus", "--name", "--dangerously-skip-permissions", "--add-dir",
        "--mcp-config", "s.json", "--", "-w", "--system-prompt", "--model=x",
        "--resume", "fix it", "-cn", "-nc"]
bad = []
for _len in range(1, 5):
    for _argv in itertools.product(POOL, repeat=_len):
        _out = W.resume_flags(list(_argv), cwd=rf_dir)
        _was, _ = parse(list(_argv))
        _now, _prompt = parse(_out + ["--resume", "SID"])
        if (_prompt or ("--resume", "SID") not in _now
                or not (_now - {("--resume", "SID")}) <= _was):
            bad.append((list(_argv), _out))
check(f"replayed + `--resume SID` always resumes SID, with no prompt and nothing "
      f"the original lacked ({sum(len(POOL) ** k for k in range(1, 5))} argvs)"
      + (f": first of {len(bad)} {bad[0]}" if bad else ""),
      not bad)

# ...AND NEVER FREER THAN THE ORIGINAL. Every restriction claude read in the
# launch -- a tool list, a deny list, a permission mode short of bypass,
# --restricted -- it reads in the replay too, or nothing is replayed at all.
RPOOL = ["--tools", "Read", "--disallowedTools", "--permission-mode", "plan",
         "--restricted", "--dangerously-skip-permissions", "-cn", "-nc", "--name", "--"]
bad, _ran = [], 0
for _len in range(1, 5):
    for _argv in itertools.product(RPOOL, repeat=_len):
        _out = W.resume_flags(list(_argv))
        _was, _ = parse(list(_argv))
        if any(v is None and HELP_ARGS.get(f) in ("one", "many") for f, v in _was):
            continue                        # a value missing: claude never started
        _ran += 1
        _now, _prompt = parse(_out + ["--resume", "SID"])
        _lost = {(f, v) for f, v in _was if f in _restrict
                 and (f, v) != ("--permission-mode", "bypassPermissions")} - _now
        if (_prompt or ("--resume", "SID") not in _now
                or not (_now - {("--resume", "SID")}) <= _was or (_lost and _out)):
            bad.append((list(_argv), _out))
check(f"...and every restriction the original had is replayed, or no flag is "
      f"({_ran} argvs claude would start)"
      + (f": first of {len(bad)} {bad[0]}" if bad else ""),
      bool(_restrict) and not bad)

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

# The same move to a DIFFERENT-DOMAIN account, with yes=False and no terminal to
# type into. This is the exact shape the old code turned away ("this needs a
# typed confirmation and there is no terminal to type it in") -- which is also
# the shape every device-initiated switch has, since the wrapper re-execs the
# switcher after the session is already gone. It must reach the exec, and it
# must still SAY what it is doing.
cfg_e = mk_profile("acct-e", "me@other-org.test")
dest_e = {"name": "acct-e", "dir": cfg_e, "email": "me@other-org.test", "chip": ""}
seen_e, out_e = {}, io.StringIO()


def fake_execve_e(path, argv, env):
    seen_e.update(path=path, argv=argv, env=env)
    raise SystemExit(0)


back, os.execve = os.getcwd(), fake_execve_e
try:
    with contextlib.redirect_stdout(out_e):
        S.switch(dict(ctx_live, cwd=work_dir, argv=[]), dest_e, yes=False)
except SystemExit:
    pass
finally:
    os.execve = real_execve
    os.chdir(back)

check("a cross-organisation switch with nobody to ask now PROCEEDS",
      seen_e.get("env", {}).get("CLAUDE_MATE_ACCOUNT") == "acct-e")
check("...carrying the conversation with it",
      os.path.exists(os.path.join(cfg_e, "projects", mangled, "mine.jsonl")))
check("...and announcing the change of organisation rather than hiding it",
      "other-org.test" in out_e.getvalue()
      and "example.com" in out_e.getvalue())
check("...without ever asking for a typed confirmation",
      "type the target domain" not in out_e.getvalue())


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

# --------------------------------------------------------------------------- #
# The picker offers more than accounts.
# --------------------------------------------------------------------------- #
# This prompt is the only Claude Mate UI on the Mac, so it is where "my device
# will not connect" has to be answerable. It was not: the device said what was
# wrong on its own glass and the daemon said it in a log file, neither of which
# is in front of the person sitting here.
print("\n== the account picker can also connect a device ==")

os.environ.pop("CLAUDE_CONFIG_DIR", None)
connect_calls = []
W._run_connect = lambda: connect_calls.append(1)

answers = iter(["d", "0"])


def fake_input(prompt=""):
    """Answer the prompt, and ECHO it.

    The real input() writes its prompt to stdout; a fake that swallows it makes
    every "does it ASK before doing this" check silently unprovable -- which is
    how the first version of the new-profile test failed while the behaviour it
    tested was correct.
    """
    print(prompt, end="")
    return next(answers)


W.input = fake_input
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    W.select_account(None, True)
out = buf.getvalue()

check("the picker offers a device entry alongside the accounts",
      "d) device" in out)
check("...and `d` runs the connect tool", connect_calls == [1])
# The bug this guards is a picker that treats `d` as an account NAME and creates
# a profile called "d" -- which is what the first cut did, silently.
check("...without selecting anything, so the prompt comes back",
      out.count("select account") == 2)
check("...and no profile named 'd' was created",
      not os.path.isdir(os.path.join(tmp, "accounts", "d")))
check("...and the choice made afterwards is honoured (0 = default)",
      not os.environ.get("CLAUDE_CONFIG_DIR"))

# A NEW NAME CREATES A DIRECTORY, SO IT HAS TO BE ASKED FOR. Anything typed here
# used to become a profile -- and an ARROW KEY types an escape sequence, so a
# real accounts dir ended up holding a directory named `\033` (which prints as a
# blank row with somebody's email beside it) and another named `в` from a
# keyboard left in the wrong layout. Both were then logged into, because the
# picker had already committed and claude asked for a login.
os.environ.pop("CLAUDE_CONFIG_DIR", None)
for junk in ("\x1b", "\x1b[A", "\x01", "../evil", "a" * 40, "with/slash"):
    answers = iter([junk])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        W.select_account(None, True)
    made = os.path.isdir(os.path.join(tmp, "accounts", junk))
    check(f"a stray {junk!r} at the picker creates no profile",
          not made and not os.environ.get("CLAUDE_CONFIG_DIR")
          and "ignoring" in buf.getvalue())

# ...while a name someone plainly meant is offered, not assumed.
answers = iter(["brand-new", "n"])
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    W.select_account(None, True)
check("a plausible new name is OFFERED rather than created silently",
      "is new" in buf.getvalue()
      and not os.path.isdir(os.path.join(tmp, "accounts", "brand-new")))
check("...and declining leaves you on the default",
      not os.environ.get("CLAUDE_CONFIG_DIR"))

answers = iter(["brand-new", "y"])
with contextlib.redirect_stdout(io.StringIO()):
    W.select_account(None, True)
check("...and accepting creates it",
      os.environ.get("CLAUDE_CONFIG_DIR", "").endswith("brand-new"))

os.environ.pop("CLAUDE_CONFIG_DIR", None)
answers = iter(["acct-b"])
with contextlib.redirect_stdout(io.StringIO()):    # the menu again; not output
    W.select_account(None, True)
check("an EXISTING account name still selects it with no extra question",
      os.environ.get("CLAUDE_CONFIG_DIR", "").endswith("acct-b"))
del W.input

print(f"\n{checks - len(failures)}/{checks} checks passed")
shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print("FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("account switch: OK")
