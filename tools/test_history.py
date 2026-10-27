#!/usr/bin/env python3
"""
The chat history, with NO hardware and NO real Claude: field 9 on the wire,
the daemon's record of every conversation a wrapped session ran, and the CLI
that lists, resumes and forgets those records.

It was asked for in these words: "now If I close chat its destroyed I want keep
chats untill user delete it to preevent crashes and reload to preevent context
loosing". So the promise under test is that a chat outlives its terminal, the
daemon, a crash and a reboot -- and goes when the user deletes it, and only
then. Review found that promise broken in more than one way before a line of it
was written, and each way has a section here:

  1. Field 9 is socket input, and a hook in any of the user's shells can write
     to that socket. The parser takes what it can use key by key and never
     raises; a path with a control character in it is dropped -- a NUL makes
     every os call on it raise, a newline forges a line in whatever prints it.
  2. An exception on the socket thread used to end that thread for good while
     the process lived on. launchd restarts a daemon that dies, not one that has
     stopped listening, so every report after it went nowhere, silently. A
     history fault must cost the history and nothing else.
  3. A record is created by a TURN, and only a session that ran one may say
     where the chat lives: a `--resume <id>` started in the wrong directory or
     under the wrong login is refused by claude before it runs anything, and it
     used to repoint the record at where the chat could not be found.
  4. A deleted record stays deleted. The session still running the chat
     reports every 20 s, and its next keepalive must not put it straight back.
  5. Claude Code deletes transcripts on its own -- cleanupPeriodDays, `claude
     purge`, a deleted profile. The daemon keeps a hard link beside the record,
     and resume puts the transcript back where `claude --resume` looks for it
     (for a deleted profile, once the profile is made again).
  6. Resume replays the launch flags through the wrapper's policy, in the
     chat's own directory and under its own login, and refuses whatever would
     start a SECOND claude on a chat that is open: two writers fork one
     transcript, and whichever branch a later resume follows, the other's turns
     are gone from the context. "Open" is looked at again right before the
     exec, counts a chat opened through claude's own picker by the session id
     its terminal found, and believes the terminals' files only in a directory
     no one else can write; a transcript written moments ago -- which
     something claude-mate cannot see may still be writing -- is a question.

In-process first: the daemon's parser, socket parser and HistoryWriter --
driven synchronously through note() and flush(), with an injected clock -- and
the CLI's resume with os.execve stubbed. Then real processes: a daemon fed by
the wrapper's own send_daemon(), killed with SIGKILL and started again; daemons
fed only the old line shapes, and --mock, which must keep nothing; and the CLI
run against nothing but the records, with no daemon at all.

Hermetic: HOME, TMPDIR, the socket and the accounts dir are temp, and
CLAUDE_CONFIG_DIR and CLAUDE_MATE_ACCOUNT are removed -- in this process too,
because the daemon module, the CLI and the wrapper are all loaded into it. The
real ~/.config/claude-mate/history is never read or written, and nothing here
talks to the real daemon's socket.

Run:   python3 tools/test_history.py      (needs pyserial)
"""
import atexit
import importlib.machinery
import importlib.util
import io
import itertools
import json
import os
import pty
import random
import re
import shutil
import site
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tty
import urllib.parse
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DAEMON = os.path.join(ROOT, "daemon", "claude_mate_daemon.py")
CLI = os.path.join(ROOT, "bin", "claude-mate")
WRAP = os.path.join(ROOT, "bin", "claude-mate-wrap")

try:
    import serial  # noqa: F401  (the daemon needs it; fail early & clearly)
except ImportError:
    print("SKIP: pyserial not installed. Run: pip install pyserial")
    sys.exit(0)

failures = []
checks = 0


def check(name, ok, detail=""):
    global checks
    checks += 1
    print(f"   {'ok  ' if ok else 'FAIL'}  {name}"
          + (f"   [{detail}]" if (detail and not ok) else ""))
    if not ok:
        failures.append(name)


def wait_for(pred, timeout=8.0, tick=0.05):
    end = time.time() + timeout
    while time.time() < end:
        val = pred()
        if val:
            return val
        time.sleep(tick)
    return False


# --------------------------------------------------------------------------- #
# The sandbox: this process's own environment, and so every child's
# --------------------------------------------------------------------------- #
# The daemon module, the CLI and the wrapper are loaded INTO this process, and
# each works out ~, the temp dir and the daemon's socket from the environment
# -- so the sandbox is set here, before any of them is imported. Two traps the
# suite has fallen into before: mkdtemp() has already asked
# tempfile.gettempdir(), which caches, so $TMPDIR alone is not enough; and the
# user site hangs off HOME, so a daemon whose pyserial lives there needs
# PYTHONUSERBASE carried across.
USERBASE = site.getuserbase()       # before HOME moves: it is worked out from it
tmp = tempfile.mkdtemp(prefix="cm-hist-")
HOME = os.path.join(tmp, "home")
TMPD = os.path.join(tmp, "tmpdir")
ACCTS = os.path.join(tmp, "accounts")
for _d in (HOME, TMPD, ACCTS):
    os.makedirs(_d)
NOBODY = os.path.join(tmp, "nobody.sock")   # a daemon socket nothing listens on
os.environ.update({
    "HOME": HOME, "TMPDIR": TMPD, "CLAUDE_MATE_ACCOUNTS_DIR": ACCTS,
    # NOT ONE LINE OF THIS FILE MAY REACH THE REAL DAEMON: it would take a test
    # line for a real session, and its writer would keep it in the real history.
    "CLAUDE_MATE_SOCK": NOBODY,
    "PYTHONUSERBASE": USERBASE, "PYTHONUNBUFFERED": "1"})
for _k in ("CLAUDE_CONFIG_DIR", "CLAUDE_MATE_ACCOUNT", "CLAUDE_MATE_TCP",
           "CLAUDE_MATE_BLE", "CLAUDE_MATE_WEB", "CLAUDE_MATE_SOUND",
           "CLAUDE_MATE_TOKEN", "CLAUDE_MATE_NEW_ARGS", "PYTHONIOENCODING",
           "PYTHONUTF8"):
    os.environ.pop(_k, None)
tempfile.tempdir = TMPD


def load(path, name):
    """Import a file by path -- the daemon, or an executable with no .py. Safe:
    each one keeps everything that starts a process behind its __main__."""
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod             # @dataclass resolves through sys.modules
    spec.loader.exec_module(mod)
    return mod


D = load(DAEMON, "cm_hist_daemon")
M = load(CLI, "cm_hist_cli")
W = load(WRAP, "cm_hist_wrap")
logged = []
D.log = lambda msg: logged.append(msg)  # the module's own calls land here
D.sfx_build_cache = lambda: {}          # no WAVs written by a test's Screen

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SNAP_KEYS = {"key", "name", "cwd", "conversation", "config_dir", "flags",
             "account", "turned", "turn_ts"}
U1 = "3f2a9c1e-7b4d-4c1a-9e8f-0123456789ab"
U2 = "8c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"
U3 = "0b9e8d7c-6f5a-4b3c-9d2e-1f0a9b8c7d6e"


def cid(tag):
    """A conversation id, a UUID as claude's are, one per test case."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "cm-hist/" + tag))


def mangle(path):
    """Claude Code's own name for a cwd's project directory."""
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def write_transcript(path, cwd, title="", pad=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "user", "cwd": cwd}) + "\n")
        for _ in range(pad):
            fh.write(json.dumps({"type": "assistant", "text": "x" * 100}) + "\n")
        if title:
            fh.write(json.dumps({"type": "ai-title", "aiTitle": title}) + "\n")
    return path


def transcript(base, conv, cwd, title="", project=None):
    """<base>/projects/<project, else the cwd mangled>/<conv>.jsonl, recording
    `cwd` the way claude does, in its first lines."""
    return write_transcript(os.path.join(base, "projects", project or mangle(cwd),
                                         conv + ".jsonl"), cwd, title)


def plant(home, conv, **fields):
    """A record as the daemon writes one, under <home>'s history."""
    rec = {"v": 1, "conversation": conv, "name": "proj", "cwd": "/nowhere",
           "config_dir": "", "account": "default", "flags": None,
           "last_active": time.time() - 3600, "ended": None}
    rec.update(fields)
    d = os.path.join(home, ".config", "claude-mate", "history")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, conv + ".json"), "w") as fh:
        json.dump(rec, fh)
    return rec


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def raw_of(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


# Every file lookup below is one of these: a check that meets a file the code
# under test failed to make must FAIL, and say which -- not end the run there.
def mode_of(path):
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def ino_of(path):
    try:
        return os.stat(path).st_ino
    except OSError:
        return None


def same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def is_regular(path):
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def session(srv, key):
    with srv._reg._lock:
        return srv._reg._sessions.get(key)


def talk(path, text, timeout=6.0):
    """Send `text` verbatim, read the answer to EOF. None if nobody listens."""
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(timeout)
    try:
        c.connect(path)
        c.sendall(text.encode())
        c.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            chunk = c.recv(65536)
            if not chunk:
                break
            buf += chunk
        return buf.decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        c.close()


def queue_of(path):
    """The daemon's queue, by session key, or None while nothing answers."""
    reply = talk(path, "queue\n", timeout=3.0)
    try:
        return {e["key"]: e for e in json.loads(reply)["queue"]}
    except (TypeError, ValueError, KeyError):
        return None


def spec_field(obj):
    """Field 9 for ANY object, encoded the way the spec writes the rule down
    (checked below to be the wrapper's own encoding, byte for byte)."""
    return urllib.parse.quote(json.dumps(obj, ensure_ascii=True,
                                         separators=(",", ":"), sort_keys=True),
                              safe="")


class Wrapper:
    """One wrapper process, as the daemon hears it: the wrapper's OWN
    send_daemon() and history_field(), run with this session's identity in the
    globals and the environment they read -- its SID, the directory it runs in,
    its login, its flags, the id once it has learned it -- and its own `end`
    latch, one per process like the real thing."""

    def __init__(self, sock, cwd, conv="", config="", flags=None,
                 account="default"):
        self.sock, self.cwd, self.conv = sock, cwd, conv
        self.config, self.flags, self.account = config, flags, account
        self.sid = os.urandom(4).hex()
        self.ended = False

    def _as(self, fn):
        saved = (W.DAEMON_SOCK, W.SID, W.NAME, W.CTRL_SOCK, dict(W.g_meta),
                 W.g_conv, W.g_resume_flags, W.g_ended, os.getcwd,
                 os.environ.get("CLAUDE_CONFIG_DIR"))
        W.DAEMON_SOCK, W.SID = self.sock or NOBODY, self.sid
        W.NAME = (os.path.basename(self.cwd) or "claude")[:20]
        W.CTRL_SOCK = os.path.join(tmp, f"ctrl-{self.sid}.sock")
        W.g_meta.update(model="Opus 4.8", effort="xhigh", account=self.account,
                        limit="", wf="-")
        W.g_conv, W.g_resume_flags, W.g_ended = self.conv, self.flags, self.ended
        os.getcwd = lambda: self.cwd        # what the wrapper's getcwd() says
        if self.config:
            os.environ["CLAUDE_CONFIG_DIR"] = self.config
        else:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        try:
            return fn()
        finally:
            self.ended = W.g_ended
            W.DAEMON_SOCK, W.SID, W.NAME, W.CTRL_SOCK = saved[:4]
            W.g_meta.clear()
            W.g_meta.update(saved[4])
            W.g_conv, W.g_resume_flags, W.g_ended = saved[5:8]
            os.getcwd = saved[8]
            if saved[9] is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = saved[9]

    def send(self, state):
        self._as(lambda: W.send_daemon(state))

    def field(self):
        return self._as(W.history_field)


def report(w, state="working"):
    """The bytes one report of `w` puts on the wire, read to EOF from a socket
    this test listens on -- the way the daemon reads it."""
    path = os.path.join(tmp, "wire.sock")
    if os.path.exists(path):
        os.unlink(path)
    lsn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    lsn.bind(path)
    lsn.listen(4)
    lsn.settimeout(3.0)
    try:
        w.sock = path
        w.send(state)
        try:
            conn, _ = lsn.accept()
        except OSError:
            return b""
        data = b""
        with conn:
            conn.settimeout(3.0)
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
        return data
    finally:
        lsn.close()


def line(state, sid, hist=None, name="proj", ctrl="/tmp/cm-hist-ctrl.sock"):
    """A wrapper line, nine fields -- ten with `hist`."""
    parts = [state, sid, name, ctrl, "Opus 4.8", "xhigh", "default", "", "-"]
    if hist is not None:
        parts.append(hist)
    return "|".join(parts).encode()


procs = []


def stop_daemon(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# Stopped at exit whatever happens in between: a test that dies on an
# unexpected exception must not leave a daemon running behind it.
atexit.register(lambda: [stop_daemon(p) for p in procs])

try:
    # ----------------------------------------------------------------------- #
    print("\n== the sandbox ==")
    check("everything loaded here resolves into the sandbox: the CLI's history "
          "and socket, the wrapper's socket and its context dir",
          M.history_dir().startswith(HOME + os.sep) and M.SOCK == NOBODY
          and W.DAEMON_SOCK == NOBODY and W.CTX_DIR.startswith(TMPD + os.sep))

    # ----------------------------------------------------------------------- #
    print("\n== field 9: the wrapper's own line, as the daemon reads it ==")
    WEIRD = "/tmp/a dir|with pipe;and semicolon"
    PROF_RAW = "/Users/me/.claude-accounts/work 2/"
    w = Wrapper(None, WEIRD, conv=U1, config=PROF_RAW,
                flags=["--model", "opus", "--name", "a|b;c"])
    raw = report(w)
    parts = raw.decode("utf-8", "replace").rstrip("\n").split("|")
    check("one line of ten fields: a '|' in a path or a flag is percent-encoded "
          "inside field 9, never a split",
          raw.endswith(b"\n") and raw.count(b"\n") == 1 and len(parts) == 10,
          ascii(raw[:90]))
    got = D.parse_history_field(parts[9]) if len(parts) == 10 else None
    check("...and the daemon's parser gets back exactly what the wrapper sent",
          got == {"id": U1, "cwd": WEIRD, "config": PROF_RAW,
                  "flags": ("--model", "opus", "--name", "a|b;c")}, ascii(got))
    check("the spec's encoding IS the wrapper's, byte for byte",
          len(parts) == 10 and parts[9] == spec_field(
              {"id": U1, "cwd": WEIRD, "config": PROF_RAW,
               "flags": ["--model", "opus", "--name", "a|b;c"]})
          and w.field() == parts[9])
    srv = D.SocketServer(os.path.join(tmp, "unused.sock"), D.Registry(),
                         on_update=lambda: None)
    srv._process_line(raw.rstrip(b"\n"))
    s = session(srv, w.sid)
    check("...and through the socket parser the session carries all four",
          s is not None and (s.conversation, s.cwd, s.config_dir, s.flags)
          == (U1, WEIRD, PROF_RAW, ("--model", "opus", "--name", "a|b;c")))
    w8 = Wrapper(None, "/tmp/caf\udce9 dir", conv=U1)
    parts = report(w8).decode("utf-8", "replace").rstrip("\n").split("|")
    got = D.parse_history_field(parts[9]) if len(parts) == 10 else None
    check("a directory whose name is not UTF-8 makes the trip whole: the same "
          "bytes come back out of it",
          bool(got) and os.fsencode(got["cwd"] or "") == b"/tmp/caf\xe9 dir",
          ascii(got))
    raw = report(Wrapper(None, WEIRD, conv=U1, flags=[]), "end")
    parts = raw.decode().rstrip("\n").split("|")
    check("`end` carries field 9 too: the id can first arrive on it",
          parts[0] == "end" and len(parts) == 10
          and (D.parse_history_field(parts[9]) or {}).get("id") == U1)

    # ----------------------------------------------------------------------- #
    print("\n== field 9: what the daemon accepts, key by key ==")
    P = D.parse_history_field

    def key_of(key, value):
        return (P(spec_field({key: value})) or {}).get(key, "MISSING")

    check("a whole field: all four keys, flags as a tuple",
          P(spec_field({"id": U1, "cwd": "/w/p", "config": "",
                        "flags": ["--model", "opus"]}))
          == {"id": U1, "cwd": "/w/p", "config": "",
              "flags": ("--model", "opus")})
    check("the dict always has the four keys, None for each one absent",
          P(spec_field({})) == {"id": None, "cwd": None, "config": None,
                                "flags": None})
    check("the id is lowercased (claude names the transcript in lowercase)",
          key_of("id", U1.upper()) == U1)
    bad_ids = ["not-a-uuid", U1 + "\n", U1[:-1], U1 + "0", " " + U1,
               U1.replace("-", ""), "", 42, None, [U1]]
    check("an id that is not a UUID is dropped -- and only the id: the rest "
          "of the field stands",
          all(P(spec_field({"id": b, "cwd": "/w"}))
              == {"id": None, "cwd": "/w", "config": None, "flags": None}
              for b in bad_ids))
    check("cwd: an absolute path is kept as it is -- spaces, '|' and all",
          key_of("cwd", "/a dir/with|pipe") == "/a dir/with|pipe")
    check("...a relative one is dropped, and so is ''",
          key_of("cwd", "rel/dir") is None and key_of("cwd", "") is None)
    check("...and so is one with a control character in it: NUL, TAB, CR, LF, "
          "ESC, DEL", all(key_of("cwd", "/a" + c + "b") is None
                          for c in "\x00\t\r\n\x1b\x7f"))
    check("...or one longer than PATH_MAX (4096)",
          key_of("cwd", "/" + "d" * 4095) == "/" + "d" * 4095
          and key_of("cwd", "/" + "d" * 4096) is None)
    check("...or one that is not a string",
          key_of("cwd", 5) is None and key_of("cwd", ["/a"]) is None)
    check("a directory name that is not UTF-8 (a lone surrogate, as json "
          "carries one) is kept, and fsencodes back to its bytes",
          os.fsencode(key_of("cwd", "/tmp/caf\udce9") or "")
          == b"/tmp/caf\xe9")
    check("config '' is the default login -- a value, not 'unknown'",
          key_of("config", "") == "")
    check("...a profile is kept VERBATIM, trailing slash and all (claude hashes "
          "the exact string into its Keychain entry)",
          key_of("config", PROF_RAW) == PROF_RAW)
    check("...a relative one, one with a control character, or a non-string: "
          "dropped", key_of("config", "work 2") is None
          and key_of("config", "/a\nb") is None
          and key_of("config", None) is None and key_of("config", 0) is None)
    check("flags: a list of strings, as a tuple; an empty list is a value too "
          "(started with none)",
          key_of("flags", ["--model", "opus"]) == ("--model", "opus")
          and key_of("flags", []) == ())
    check("...64 items pass, 65 do not",
          key_of("flags", ["-v"] * 64) == ("-v",) * 64
          and key_of("flags", ["-v"] * 65) is None)
    check("...4096 characters between them pass, 4097 do not",
          key_of("flags", ["x" * 4096]) == ("x" * 4096,)
          and key_of("flags", ["x" * 2048, "y" * 2049]) is None)
    check("...one item with a control character sinks the list",
          key_of("flags", ["--name", "a\tb"]) is None
          and key_of("flags", ["--name", "a\x00b"]) is None)
    check("...and so does anything that is not a list of strings",
          key_of("flags", "--model opus") is None
          and key_of("flags", [1, 2]) is None
          and key_of("flags", {"--model": "opus"}) is None)
    check("EACH KEY STANDS ALONE: flags over their budget cost the flags, never "
          "the id beside them",
          P(spec_field({"id": U1, "flags": ["-v"] * 65}))
          == {"id": U1, "cwd": None, "config": None, "flags": None})
    check("keys from a newer producer are ignored, not fatal",
          (P(spec_field({"id": U1, "pid": 4, "later": {"x": [1]}})) or {})
          .get("id") == U1)
    check("an empty field, or none at all, is None",
          P("") is None and P(None) is None and P("   ") is None)
    check("not JSON, half of it, or JSON that is not an object: None",
          all(P(x) is None for x in ("not json", "%7B", "%7B%22id%22", "null",
                                     spec_field([U1]), spec_field(U1),
                                     spec_field(5), spec_field(None))))

    def shape_ok(out):
        """What the socket thread relies on, whatever came in."""
        if out is None:
            return True
        if not isinstance(out, dict) or set(out) != {"id", "cwd", "config",
                                                     "flags"}:
            return False

        def path_ok(p):
            return (isinstance(p, str) and p.startswith("/") and len(p) <= 4096
                    and not any(ch < " " or ch == "\x7f" for ch in p))
        i, c, g, f = out["id"], out["cwd"], out["config"], out["flags"]
        return ((i is None or (isinstance(i, str)
                               and bool(UUID_RE.fullmatch(i))))
                and (c is None or path_ok(c))
                and (g is None or g == "" or path_ok(g))
                and (f is None or (isinstance(f, tuple) and len(f) <= 64
                                   and all(isinstance(x, str) for x in f))))

    rng = random.Random(20261027)
    pieces = ["%7B", "%7D", "%22", "%5B", "%5D", "%3A", "%2C", "%5C", "id",
              "cwd", "config", "flags", U1, "%2F", "/", "%00", "%0A", "%FF",
              "%E9", "%25", "u0000", "udce9", "null", "true", "1e999", "NaN",
              "-", '"', "{", "}", "[", "]", ":", ",", " ", "\n", "|", "\xe9",
              "\udce9", "\x00"]
    valid = spec_field({"id": U1, "cwd": "/w/p", "config": "",
                        "flags": ["--model", "opus"]})
    raised, wrong = [], []
    samples = []
    for n in range(4000):
        if n % 2:
            samples.append("".join(rng.choice(pieces)
                                   for _ in range(rng.randint(0, 30))))
            continue
        s = list(valid)
        for _ in range(rng.randint(1, 4)):
            pos = rng.randrange(len(s) + 1)
            if rng.random() < 0.5 and s:
                del s[min(pos, len(s) - 1)]
            else:
                s[pos:pos] = list(rng.choice(pieces))
        samples.append("".join(s))
    samples += [b"%7B%7D", 12, 3.5, ["x"], "%" * 70000,
                spec_field({"id": U1, "pad": "x" * 100000})]
    for s in samples:
        try:
            out = P(s)
        except Exception as exc:        # noqa: BLE001 -- exactly what is tested
            raised.append((ascii(s)[:40], repr(exc)))
            continue
        if not shape_ok(out):
            wrong.append(ascii(s)[:40])
    check(f"it NEVER raises, and whatever it returns has the shape the socket "
          f"thread relies on ({len(samples)} damaged, random and odd inputs)",
          not raised and not wrong, ascii((raised[:1], wrong[:1])))

    # ----------------------------------------------------------------------- #
    print("\n== field 9 on the socket: whole wrapper lines only, and sticky ==")
    H_FULL = spec_field({"id": U1, "cwd": "/w/proj", "config": "",
                         "flags": ["--model", "opus"]})
    H_NOID = spec_field({"cwd": "/w/proj", "config": "",
                         "flags": ["--model", "opus"]})
    srv = D.SocketServer(os.path.join(tmp, "unused.sock"), D.Registry(),
                         on_update=lambda: None)
    srv._process_line(line("working", "w1", H_FULL))
    s = session(srv, "w1")
    check("built without main() it has no writer, and still takes a whole "
          "wrapper line: conversation, directory, login and flags",
          srv.history is None and s is not None
          and (s.conversation, s.cwd, s.config_dir, s.flags)
          == (U1, "/w/proj", "", ("--model", "opus")))
    srv._process_line(line("working", "w2", H_FULL), complete=False)
    s = session(srv, "w2")
    check("a line cut short (no newline): its state counts, its field 9 does not",
          s is not None and s.state == "working" and s.conversation == ""
          and s.cwd == "" and s.config_dir is None and s.flags is None)
    srv._process_line(b"working|w3|proj||||||-|" + H_FULL.encode())
    s = session(srv, "w3")
    check("a hook's line (no ctrl socket) with a field 9 on it is a hook's "
          "line: field 9 ignored", s is not None and s.conversation == ""
          and s.cwd == "")
    srv._process_line(("working|" + U1 + "|proj").encode())
    check("a hook keyed by Claude's own id names no conversation to the history",
          getattr(session(srv, U1), "conversation", None) == "")
    srv._process_line(line("working", "w4"))
    check("a wrapper from before field 9 (nine fields) names none either",
          getattr(session(srv, "w4"), "conversation", None) == "")
    srv._process_line(line("idle", "w1", ""))
    s = session(srv, "w1")
    check("STICKY: a report with an empty field 9 changes none of them",
          s is not None and (s.conversation, s.cwd, s.config_dir, s.flags)
          == (U1, "/w/proj", "", ("--model", "opus")))
    srv._process_line(line("idle", "w1", spec_field(
        {"cwd": "/w/other", "config": "/p/acct", "flags": []})))
    s = session(srv, "w1")
    check("...a key that is there replaces, the rest stay -- and an empty flag "
          "list is a value", s is not None
          and (s.conversation, s.cwd, s.config_dir, s.flags)
          == (U1, "/w/other", "/p/acct", ()))
    srv._process_line(line("idle", "w1", spec_field({"config": ""})))
    check("...so is config '': back on the default login",
          getattr(session(srv, "w1"), "config_dir", None) == "")
    srv._process_line(line("idle", "t1", H_FULL))
    check("a session that has run no turn is not 'turned'",
          getattr(session(srv, "t1"), "turned", None) is False)
    srv._process_line(line("working", "t1", H_FULL))
    s = session(srv, "t1")
    ts = getattr(s, "turn_ts", 0.0)
    check("a transition into working is a turn: turned, and when it started",
          getattr(s, "turned", None) is True and ts > 0)
    srv._process_line(line("working", "t1", H_FULL))
    check("...a working keepalive is not another one",
          getattr(session(srv, "t1"), "turn_ts", None) == ts)
    reg = D.Registry()
    reg.update("idle", "r1", "p", now=100.0, conversation=U1)
    reg.update("waiting", "r1", "p", now=110.0)
    check("waiting is no turn",
          (reg.snapshot("r1") or {}).get("turned") is False)
    reg.update("working", "r1", "p", now=120.0)
    reg.update("idle", "r1", "p", now=130.0)
    reg.update("working", "r1", "p", now=140.0)
    snap_r = reg.snapshot("r1") or {}
    check("a later turn moves turn_ts on",
          snap_r.get("turned") is True and snap_r.get("turn_ts") == 140.0)
    check("Registry.snapshot() is one plain dict with exactly the keys the "
          "writer reads, and None for a session it does not have",
          set(snap_r) == SNAP_KEYS and reg.snapshot("nope") is None)
    gone = reg.remove("r1")
    check("Registry.remove() hands back what it removed, None for nothing",
          gone is not None and gone.conversation == U1
          and reg.remove("r1") is None)
    q = srv.queue_snapshot()["queue"]
    by = {e["key"]: e for e in q}
    check("every queue entry says which conversation it runs ('' unknown), "
          "beside the directory a wrapped session now fills",
          all("conversation" in e for e in q)
          and by.get("w1", {}).get("conversation") == U1
          and by.get("w1", {}).get("cwd") == "/w/other"
          and by.get("w3", {}).get("conversation") == ""
          and by.get(U1, {}).get("conversation") == "")

    # ----------------------------------------------------------------------- #
    print("\n== the history's two doors: a session line, and its `end` ==")

    class Spy:
        """A writer that only remembers what it was handed."""

        def __init__(self):
            self.got = []

        def note(self, snap, ended=None):
            self.got.append((dict(snap), ended))

    spy = Spy()
    srv = D.SocketServer(os.path.join(tmp, "unused.sock"), D.Registry(),
                         on_update=lambda: None)
    srv.history = spy
    srv._process_line(line("idle", "d1", H_NOID))
    srv._process_line(line("working", "d1", H_NOID))
    check("no conversation id yet: nothing for the history, even after a turn",
          spy.got == [])
    srv._process_line(line("working", "d1", H_FULL))
    first = spy.got[0][0] if spy.got else {}
    check("the report that names it hands over the session as it stands -- "
          "turned, since its turn ran before the id was known",
          len(spy.got) == 1 and spy.got[0][1] is None
          and set(first) == SNAP_KEYS and first["conversation"] == U1
          and first["turned"] is True and first["cwd"] == "/w/proj"
          and first["key"] == "d1")
    t0 = time.time()
    srv._process_line(line("end", "d1", H_FULL))
    last, ended = spy.got[-1] if spy.got else ({}, None)
    check("its `end` hands over what the session WAS, and when it ended",
          len(spy.got) == 2 and isinstance(ended, float)
          and abs(ended - t0) < 5 and last.get("conversation") == U1
          and last.get("turned") is True
          and last.get("flags") == ("--model", "opus")
          and session(srv, "d1") is None)
    srv._process_line(line("end", "d9", spec_field(
        {"id": U2, "cwd": "/w/x", "config": "/p/a", "flags": ["--effort", "x"]})))
    last, ended = spy.got[-1] if spy.got else ({}, None)
    check("an `end` from a session this daemon never saw (it restarted since) "
          "is taken from the end line alone -- turned False, so it may close a "
          "record but create or repoint none",
          len(spy.got) == 3 and ended is not None
          and last.get("conversation") == U2 and last.get("turned") is False
          and last.get("cwd") == "/w/x" and last.get("config_dir") == "/p/a")
    n = len(spy.got)
    srv._process_line(line("end", "d8"))
    srv._process_line(line("end", "d7", ""))
    check("...and without a field 9 it says nothing", len(spy.got) == n)
    srv._process_line(line("working", "d2", H_NOID))
    srv._process_line(line("end", "d2", spec_field({"id": U3})))
    last, ended = spy.got[-1] if spy.got else ({}, None)
    check("the id may arrive first ON the end line: the session's own turn "
          "still counts", len(spy.got) == n + 1
          and last.get("conversation") == U3 and last.get("turned") is True
          and ended is not None)
    n = len(spy.got)
    srv._process_line(("working|" + U1 + "|proj").encode())
    srv._process_line(line("working", "d3", H_FULL), complete=False)
    check("a hook's line, and a line cut short, hand over nothing",
          len(spy.got) == n)

    # ----------------------------------------------------------------------- #
    print("\n== a history fault costs the history, never the socket thread ==")

    class Boom:
        def note(self, snap, ended=None):
            raise RuntimeError("injected: note")

    class SnapBoom(D.Registry):
        def snapshot(self, key):
            raise TypeError("injected: snapshot")

    class UpdateBoom(D.Registry):
        def update(self, state, sid, name, *args, **kwargs):
            if sid == "bad":
                raise TypeError("injected: update")
            return super().update(state, sid, name, *args, **kwargs)

    def serve(reg, history, tag):
        path = os.path.join(tmp, tag + ".sock")
        server = D.SocketServer(path, reg, on_update=lambda: None)
        server.history = history
        server.start()
        wait_for(lambda: os.path.exists(path), 5)
        return server, path

    def states(path):
        q = queue_of(path)
        return None if q is None else {k: (e["state"], e["conversation"])
                                       for k, e in q.items()}

    D._fault_seen.clear()
    mark = len(logged)
    server, path = serve(D.Registry(), Boom(), "boom")
    try:
        talk(path, line("working", "b1", H_FULL).decode() + "\n")
        q1 = states(path)
        talk(path, line("waiting", "b1", H_FULL).decode() + "\n")
        q2 = states(path)
        talk(path, line("end", "b1", H_FULL).decode() + "\n")
        q3 = states(path)
        check("a writer whose note() raises: the line's state report still "
              "goes through", bool(q1) and q1.get("b1") == ("working", U1))
        check("...the next line still lands, and `queue` still answers",
              bool(q2) and (q2.get("b1") or ("",))[0] == "waiting")
        check("...an `end` still ends the session, on a thread still alive",
              q3 is not None and "b1" not in q3 and server.is_alive())
        faults = [m for m in logged[mark:] if m.startswith("history ")]
        check("...and the faults are logged once per kind, not once per line "
              "-- the first with its traceback",
              sum(m.startswith("history note failed") for m in faults) == 1
              and sum(m.startswith("history end failed") for m in faults) == 1
              and "Traceback" in faults[0], ascii(faults)[:200])
    finally:
        server.stop()
        server.join(3)
    server, path = serve(SnapBoom(), Spy(), "snapboom")
    try:
        talk(path, line("working", "s1", H_FULL).decode() + "\n")
        q1 = states(path)
        check("a registry whose snapshot() raises: the report lands all the same, "
              "and the thread lives", bool(q1) and q1.get("s1") == ("working", U1)
              and server.is_alive())
    finally:
        server.stop()
        server.join(3)
    mark = len(logged)
    server, path = serve(UpdateBoom(), None, "updboom")
    try:
        talk(path, "working|bad|proj|/c||||||-\n"
             + line("working", "good", H_FULL).decode() + "\n")
        q1 = states(path)
        check("a fault anywhere in a line costs THAT line: the next one on the "
              "same connection still lands, the thread lives",
              q1 is not None and "good" in q1 and "bad" not in q1
              and server.is_alive())
        check("...and it is logged",
              any(m.startswith("socket line failed") for m in logged[mark:]))
    finally:
        server.stop()
        server.join(3)

    # ----------------------------------------------------------------------- #
    print("\n== select|<key>: the exact row, whatever an index or name says ==")

    class RecLink:
        """A device link that takes every line."""

        def __init__(self):
            self.lines = []

        def write_line(self, text):
            self.lines.append(text)
            return True

    reg = D.Registry()
    reg.update("working", U1, "proj")                       # a hook's row
    reg.update("idle", "00000001", "proj", focus_ctrl="/c")  # all-digit SID
    scr = D.Screen(RecLink(), reg)
    order = [x.key for x in reg.queue()]
    got = scr.select_by("00000001")
    check("an all-digit session key selects THAT row, not the row at the index "
          "it spells", order == ["00000001", U1] and got == "proj"
          and scr._sel_key == "00000001", ascii(order))
    srv = D.SocketServer(os.path.join(tmp, "unused.sock"), reg,
                         on_update=lambda: None)
    srv.screen, srv.on_select = scr, scr.select_by
    reply = srv._process_line(("select|" + U1).encode())
    check("select|<key> over the socket: 'ok', and the row selected is that key",
          reply == "ok proj\n" and scr._sel_key == U1, ascii(reply))
    check("...where the name the two rows share is refused, as it always was, "
          "and an index still works", scr.select_by("proj") is None
          and scr.select_by("0") == "proj" and scr._sel_key == "00000001")

    # ----------------------------------------------------------------------- #
    print("\n== HistoryWriter: created by a turn, and repointed only by one ==")
    CHAT = os.path.join(tmp, "work", "chat-dir")
    CHAT2 = os.path.join(tmp, "work", "other-dir")
    for _d in (CHAT, CHAT2):
        os.makedirs(_d)
    ROOTS = os.path.join(tmp, "roots")

    def snap(conv, key="w1", cwd=CHAT, cfg="", flags=("--model", "opus"),
             account="default", turned=True, turn_ts=1000.0):
        return {"key": key, "name": (os.path.basename(cwd) or key)[:20],
                "cwd": cwd, "conversation": conv, "config_dir": cfg,
                "flags": flags, "account": account, "turned": turned,
                "turn_ts": turn_ts}

    def put(writer, s, ended=None):
        writer.note(s, ended=ended)
        writer.flush()

    def rec_at(root, conv):
        return read_json(os.path.join(root, conv + ".json"))

    def inode(root, conv):
        return ino_of(os.path.join(root, conv + ".json"))

    check("the snapshots below have exactly Registry.snapshot()'s keys",
          set(snap(U1)) == SNAP_KEYS)
    clock_a = [5000.0]
    rA = os.path.join(ROOTS, "a")
    wA = D.HistoryWriter(rA, clock=lambda: clock_a[0])
    XA = cid("a")
    mark = len(logged)
    put(wA, snap(XA, turned=False, turn_ts=0.0))
    check("no record before a turn: a session that ran none has nothing to resume",
          rec_at(rA, XA) is None)
    put(wA, snap(XA, turned=False, turn_ts=0.0), ended=900.0)
    check("...and no `end` creates one: not from a session that ran no turn here "
          "(one this daemon only heard of after a restart)",
          rec_at(rA, XA) is None)
    put(wA, snap(XA))
    want = {"v": 1, "conversation": XA, "name": "chat-dir", "cwd": CHAT,
            "config_dir": "", "account": "default", "flags": ["--model", "opus"],
            "last_active": 1000.0, "ended": None}
    check("its first turn writes the record: schema v1, every field",
          rec_at(rA, XA) == want, ascii(rec_at(rA, XA)))
    check("...0600, in a directory made 0700 (it names directories, a login "
          "and launch flags)", mode_of(os.path.join(rA, XA + ".json")) == 0o600
          and mode_of(rA) == 0o700)
    i0 = inode(rA, XA)
    other = snap(XA, key="w2", cwd=CHAT2, cfg="/p/elsewhere",
                 flags=("--model", "haiku"), account="other", turned=False,
                 turn_ts=0.0)
    put(wA, other)
    check("a session that ran NO turn cannot repoint it -- not the directory, "
          "the login or the flags: nothing is written",
          rec_at(rA, XA) == want and inode(rA, XA) == i0)
    put(wA, other, ended=1500.0)
    check("...its `end` closes the record, and changes nothing else",
          rec_at(rA, XA) == dict(want, ended=1500.0))
    PROF = os.path.join(ACCTS, "acct-w")
    s3 = snap(XA, key="w3", cwd=CHAT2, cfg=PROF, flags=("--effort", "high"),
              account="work", turn_ts=2000.0)
    put(wA, s3)
    want3 = dict(want, name="other-dir", cwd=CHAT2, config_dir=PROF,
                 account="work", flags=["--effort", "high"], last_active=2000.0,
                 ended=None)
    check("a later TURN reopens it (ended None) and moves it to where that "
          "session runs, as whom, with what", rec_at(rA, XA) == want3,
          ascii(rec_at(rA, XA)))
    i1 = inode(rA, XA)
    put(wA, s3)
    put(wA, dict(s3, turn_ts=1500.0))
    check("the same again, or an older turn: nothing new, so no write -- a "
          "keepalive is not a disk write",
          rec_at(rA, XA) == want3 and inode(rA, XA) == i1)
    put(wA, dict(s3, config_dir=None, account="", cwd="", name=""))
    check("an unknown login (None), an empty directory or account: no change",
          rec_at(rA, XA) == want3 and inode(rA, XA) == i1)
    put(wA, dict(s3, flags=()))
    check("...but an empty flag list is a value: started with none",
          (rec_at(rA, XA) or {}).get("flags") == [])
    put(wA, dict(s3, flags=()), ended=3000.0)
    check("its own `end` closes it", (rec_at(rA, XA) or {}).get("ended") == 3000.0)
    ours = [m for m in logged[mark:] if m.startswith("history: ")]
    check("ONE log line when a record is created, one per close -- never one "
          "per write", len(ours) == 3
          and ours.count(f"history: kept chat-dir {XA[:8]}") == 1
          and sum(m.endswith(f"{XA[:8]} closed") for m in ours) == 2,
          ascii(ours))
    rN = os.path.join(ROOTS, "n")
    wN = D.HistoryWriter(rN)
    XN = cid("n")
    mark = len(logged)
    wN.note(snap(XN, turn_ts=1000.0))
    wN.note(snap(XN, turn_ts=1100.0))
    wN.note(snap(XN, turn_ts=1100.0), ended=1150.0)
    # A LATE REPORT FROM THE SAME KEY: a ghost the socket re-registered after
    # its `end` (the wrapper's monitor was half way through a report). Folded
    # over the end, as a burst of snapshots is, the chat would never close.
    wN.note(snap(XN, turned=False, turn_ts=0.0))
    check("note() only queues -- it runs on the socket thread: not a file is "
          "touched before the flush", not os.path.exists(rN))
    wN.flush()
    ours = [m for m in logged[mark:] if XN[:8] in m]
    check("a burst lands in order -- kept, then closed -- and an `end` is never "
          "folded into a report that follows it from the same session",
          (rec_at(rN, XN) or {}).get("ended") == 1150.0
          and (rec_at(rN, XN) or {}).get("last_active") == 1100.0
          and len(ours) == 2 and "kept" in ours[0] and ours[1].endswith("closed"),
          ascii(ours))
    wN.note(snap(XN, key="w9", turn_ts=1200.0), ended=1250.0)
    wN.note(snap(XN, key="w10", turn_ts=1300.0))  # an account switch's next turn
    wN.flush()
    check("...and across sessions too: one closes, the next one's turn reopens",
          (rec_at(rN, XN) or {}).get("ended", 0) is None
          and (rec_at(rN, XN) or {}).get("last_active") == 1300.0
          and sum(m.endswith("closed") for m in logged[mark:]
                  if XN[:8] in m) == 2)

    print("\n== ...and a record the user deleted stays deleted ==")
    rB = M.history_dir()            # the CLI's own, so the CLI's own delete
    clock_b = [1100.0]
    wB = D.HistoryWriter(rB, clock=lambda: clock_b[0])
    XB = cid("b")
    put(wB, snap(XB))
    wB.note(snap(XB))                       # its next keepalive, already queued
    gone, errs = M.forget_chats([XB])
    wB.flush()
    check("deleted by the CLI with the session's next snapshot already queued: "
          "it does NOT come back", gone == [XB] and not errs
          and rec_at(rB, XB) is None)
    put(wB, snap(XB), ended=1200.0)
    check("...nor at that session's `end`", rec_at(rB, XB) is None)
    put(wB, snap(XB, turn_ts=1050.0))
    check("...nor for a turn that started before the record was last seen",
          rec_at(rB, XB) is None)
    put(wB, snap(XB, turn_ts=1150.0))
    check("...only a NEW turn -- the chat picked up again -- keeps it once more",
          (rec_at(rB, XB) or {}).get("last_active") == 1150.0)

    print("\n== ...and files it did not write: corrupt, mistyped, newer, odd ==")
    rC = os.path.join(ROOTS, "c")
    os.makedirs(rC)
    wC = D.HistoryWriter(rC)

    def plant_raw(conv, data):
        with open(os.path.join(rC, conv + ".json"), "wb") as fh:
            fh.write(data)

    XC = [cid(f"c{i}") for i in range(10)]
    plant_raw(XC[1], b"{not json")
    put(wC, snap(XC[1], turned=False, turn_ts=0.0))
    check("a record it cannot read is not touched by a session that ran no turn",
          raw_of(os.path.join(rC, XC[1] + ".json")) == b"{not json")
    put(wC, snap(XC[1]))
    r = rec_at(rC, XC[1]) or {}
    check("...and is written whole again by one that did",
          r.get("v") == 1 and r.get("conversation") == XC[1]
          and r.get("cwd") == CHAT)
    plant_raw(XC[2], json.dumps(
        {"v": 1, "conversation": XC[2], "cwd": 5, "name": ["x"],
         "flags": "--model", "last_active": "x", "ended": "y", "config_dir": 7,
         "account": None, "note": "mine"}).encode())
    put(wC, snap(XC[2]))
    r = rec_at(rC, XC[2]) or {}
    check("WRONG TYPE = ABSENT: every mistyped field comes from the turn, and a "
          "key it does not know is kept",
          r.get("cwd") == CHAT and r.get("name") == "chat-dir"
          and r.get("flags") == ["--model", "opus"]
          and r.get("last_active") == 1000.0 and r.get("ended", 0) is None
          and r.get("config_dir") == "" and r.get("account") == "default"
          and r.get("note") == "mine", ascii(r))
    plant_raw(XC[3], b"[1, 2]")
    put(wC, snap(XC[3]))
    check("a JSON list is no record either",
          (rec_at(rC, XC[3]) or {}).get("conversation") == XC[3])
    newer = json.dumps({"v": 2, "conversation": XC[4], "cwd": "/from/later",
                        "shape": "new"}).encode()
    plant_raw(XC[4], newer)
    mark = len(logged)
    put(wC, snap(XC[4]))
    put(wC, snap(XC[4], turn_ts=2000.0))
    check("a v2 record (a newer daemon's) is left byte for byte, never "
          "downgraded -- and that is said once",
          raw_of(os.path.join(rC, XC[4] + ".json")) == newer
          and sum("newer than this daemon" in m for m in logged[mark:]) == 1)
    plant_raw(XC[5], b'{"v": 1, "pad": "' + b"x" * 70000 + b'"}')
    put(wC, snap(XC[5]))
    r = rec_at(rC, XC[5]) or {}
    check("a file past 64 KB is not read as a record",
          "pad" not in r and r.get("conversation") == XC[5])
    os.mkfifo(os.path.join(rC, XC[6] + ".json"))
    t = threading.Thread(target=put, args=(wC, snap(XC[6])), daemon=True)
    t.start()
    t.join(5)
    check("a FIFO named like a record does not hang the writer, and is replaced "
          "by the record", not t.is_alive()
          and is_regular(os.path.join(rC, XC[6] + ".json"))
          and (rec_at(rC, XC[6]) or {}).get("conversation") == XC[6])
    outside = os.path.join(tmp, "outside.json")
    with open(outside, "wb") as fh:
        fh.write(b'{"v": 1, "keep": "me"}')
    os.symlink(outside, os.path.join(rC, XC[7] + ".json"))
    put(wC, snap(XC[7]))
    check("a symlink is replaced by the record, never written through",
          raw_of(outside) == b'{"v": 1, "keep": "me"}'
          and not os.path.islink(os.path.join(rC, XC[7] + ".json"))
          and (rec_at(rC, XC[7]) or {}).get("conversation") == XC[7])
    os.mkdir(os.path.join(rC, XC[8] + ".json"))
    mark = len(logged)
    wC.note(snap(XC[8]))
    wC.note(snap(XC[9]))
    wC.flush()
    check("a record that cannot be written costs that record: the next one in "
          "the same flush is written", rec_at(rC, XC[9]) is not None
          and os.path.isdir(os.path.join(rC, XC[8] + ".json")))
    check("...the failure is logged by name, and no temp file is left behind",
          any(m.startswith(f"history record {XC[8]} failed")
              for m in logged[mark:])
          and not [x for x in os.listdir(rC) if x.endswith(".tmp")])

    print("\n== ...the directory: 0700 whoever made it, back after rm -rf, swept ==")
    rD = os.path.join(ROOTS, "d")
    os.makedirs(rD)
    os.chmod(rD, 0o755)
    clock_d = [1000.0]
    wD = D.HistoryWriter(rD, clock=lambda: clock_d[0])
    XD1, XD2 = cid("d1"), cid("d2")
    put(wD, snap(XD1, turn_ts=900.0))
    check("a directory that was there 0755 is made 0700 by the write",
          mode_of(rD) == 0o700)
    shutil.rmtree(rD)
    put(wD, snap(XD2, turn_ts=900.0))
    check("rm -rf of the whole history: the next write brings the directory back, "
          "0700, and records are written again",
          os.path.isdir(rD) and mode_of(rD) == 0o700
          and rec_at(rD, XD2) is not None)
    put(wD, snap(XD1, turn_ts=900.0))
    check("...a chat it held went with it, and comes back only with a new turn",
          rec_at(rD, XD1) is None)
    put(wD, snap(XD1, turn_ts=1100.0))
    check("...like this one", rec_at(rD, XD1) is not None)
    rS = os.path.join(ROOTS, "s")
    os.makedirs(rS)
    stale = os.path.join(rS, f".{XD1}.abcd1234.tmp")
    fresh = os.path.join(rS, f".{XD1}.ef567890.tmp")
    lnk = os.path.join(rS, f".{XD1}.0badf00d.lnk")
    for p in (stale, fresh, lnk):
        with open(p, "w") as fh:
            fh.write("x")
    _old = time.time() - 700
    os.utime(stale, (_old, _old))
    put(D.HistoryWriter(rS), snap(cid("s1")))
    check("the first write of a process sweeps what a killed writer left: a temp "
          "record over 10 minutes old, and a temp link of any age",
          not os.path.exists(stale) and not os.path.exists(lnk))
    check("...but not a temp file young enough to be a write in flight",
          os.path.exists(fresh))

    print("\n== ...the transcript, kept beside the record ==")
    rE = os.path.join(ROOTS, "e")
    wE = D.HistoryWriter(rE)
    PG = os.path.join(ACCTS, "work [eu]*?")     # a glob pattern, as a name
    XE = [cid(f"e{i}") for i in range(7)]
    src = transcript(PG, XE[1], CHAT, title="t", project="-proj-e1")
    kept = os.path.join(rE, XE[1] + ".jsonl")
    put(wE, snap(XE[1], cfg=PG))
    check("the record keeps a hard link to the transcript -- found by listdir in "
          "a profile whose name is a glob pattern",
          os.path.isfile(kept) and same(src, kept)
          and (rec_at(rE, XE[1]) or {}).get("project") == "-proj-e1")
    body, kept_ino = raw_of(src), ino_of(kept)
    os.unlink(src)
    put(wE, snap(XE[1], cfg=PG, turn_ts=2000.0))
    check("Claude's copy deleted (cleanupPeriodDays, claude purge, a profile "
          "removed): the kept one IS the conversation, and is left alone",
          raw_of(kept) == body and kept_ino is not None
          and ino_of(kept) == kept_ino
          and (rec_at(rE, XE[1]) or {}).get("project") == "-proj-e1")
    with open(src + ".new", "wb") as fh:
        fh.write(body + b'{"type": "user"}\n')
    os.replace(src + ".new", src)
    put(wE, snap(XE[1], cfg=PG, turn_ts=3000.0))
    check("...and when Claude's copy is a new file, the kept link follows it",
          same(src, kept))
    src2 = transcript(os.path.join(HOME, ".claude"), XE[2], CHAT,
                      project="-proj-e2")
    put(wE, snap(XE[2], cfg=""))
    check("config '' is the default login: the transcript is looked for under "
          "~/.claude", os.path.isfile(os.path.join(rE, XE[2] + ".jsonl"))
          and same(src2, os.path.join(rE, XE[2] + ".jsonl")))
    transcript(os.path.join(HOME, ".claude"), XE[3], CHAT, project="-proj-e3")
    put(wE, snap(XE[3], cfg=None))
    check("an unknown login: the record is kept, nothing is linked (no guessing "
          "which login's transcript it is)", rec_at(rE, XE[3]) is not None
          and not os.path.exists(os.path.join(rE, XE[3] + ".jsonl"))
          and "project" not in (rec_at(rE, XE[3]) or {}))
    put(wE, snap(XE[4], cfg=""))
    check("no transcript anywhere yet: the record without a link",
          rec_at(rE, XE[4]) is not None
          and not os.path.exists(os.path.join(rE, XE[4] + ".jsonl")))
    rE2 = os.path.join(ROOTS, "e2")
    os.makedirs(rE2)
    wE2 = D.HistoryWriter(rE2)
    mark = len(logged)
    for x in XE[5:7]:
        transcript(os.path.join(HOME, ".claude"), x, CHAT, project="-p-" + x[:4])
        os.mkdir(os.path.join(rE2, x + ".jsonl"))     # where the link would go
        put(wE2, snap(x, cfg=""))
    check("a link that cannot be made (EXDEV, EPERM, ...) costs the link, never "
          "the record", all(rec_at(rE2, x) is not None for x in XE[5:7]))
    check("...said once, not once per record, and no temp link is left behind",
          sum("cannot keep a transcript" in m for m in logged[mark:]) == 1
          and not [x for x in os.listdir(rE2) if x.endswith(".lnk")])

    print("\n== ...the thread: debounced, and stop() drains ==")
    rT = os.path.join(ROOTS, "t")
    wT = D.HistoryWriter(rT)
    XT = cid("t")
    check("a daemon thread named 'history'", wT.name == "history" and wT.daemon)
    wT.start()
    wT.note(snap(XT))
    check("running, it writes a noted event within its debounce",
          bool(wait_for(lambda: rec_at(rT, XT), 5)))
    wT.note(snap(XT), ended=4242.0)
    wT.stop()
    wT.join(5)
    check("stop() right after an `end`: its own last flush writes it, then the "
          "thread exits", not wT.is_alive()
          and (rec_at(rT, XT) or {}).get("ended") == 4242.0)
    # THE LAST FLUSH IS THE THREAD'S OWN. An `end` that arrives while a flush is
    # half way through its batch, with stop() right behind it, is in no batch
    # yet; only the flush the thread makes on its way out can write it. Held
    # deterministically: the first write blocks until the end and the stop are
    # both in.
    wT2 = D.HistoryWriter(rT)
    XT2a, XT2b = cid("t2a"), cid("t2b")
    in_write, release = threading.Event(), threading.Event()
    real_write = wT2._write

    def held_write(conv, s, ended):
        if conv == XT2a:
            in_write.set()
            release.wait(5)
        return real_write(conv, s, ended)

    wT2._write = held_write
    wT2.start()
    wT2.note(snap(XT2a))
    in_write.wait(5)
    wT2.note(snap(XT2b), ended=77.0)
    wT2.stop()
    release.set()
    wT2.join(5)
    check("...and an `end` that lands while a flush is writing, stop() right "
          "behind it: written by the flush the thread makes on its way out",
          not wT2.is_alive() and (rec_at(rT, XT2b) or {}).get("ended") == 77.0)

    print("\n== ...and the CLI reads what it writes ==")
    rG = M.history_dir()
    XG = cid("g")
    tG = transcript(os.path.join(HOME, ".claude"), XG, CHAT,
                    title="from the writer")
    put(D.HistoryWriter(rG), snap(XG))
    r = next((x for x in M.load_history() if x["conversation"] == XG), None)
    check("load_history() reads the daemon's record field for field",
          r is not None and (r["name"], r["cwd"], r["config_dir"], r["account"],
                             r["flags"], r["last_active"], r["ended"],
                             r["project"])
          == ("chat-dir", CHAT, "", "default", ["--model", "opus"], 1000.0,
              None, mangle(CHAT)), ascii(r))
    check("...and finds both copies of its transcript, and the title in it",
          r is not None and M.chat_transcript(r) == tG
          and M.chat_kept(r) == os.path.join(rG, XG + ".jsonl")
          and M.chat_title(r) == "from the writer")
    XJ = [cid(f"j{i}") for i in range(5)]
    for fname, data in ((f".{XG}.abcd1234.tmp", b"{}"),
                        ("not-a-uuid.json", b'{"v": 1}'),
                        (XJ[1] + ".json", b"{broken"),
                        (XJ[2] + ".json", b"[1]"),
                        (XJ[3] + ".jsonl", b"{}"),
                        (XJ[4] + ".json", json.dumps(
                            {"v": 1, "name": 5, "cwd": "relative",
                             "flags": "x", "last_active": "x"}).encode())):
        with open(os.path.join(rG, fname), "wb") as fh:
            fh.write(data)
    recs = M.load_history()
    ids = [x["conversation"] for x in recs]
    check("only <uuid>.json files are records: temp files, kept transcripts, "
          "junk and corrupt files are skipped",
          XJ[1] not in ids and XJ[2] not in ids and XJ[3] not in ids
          and all(UUID_RE.fullmatch(x) for x in ids))
    r = next((x for x in recs if x["conversation"] == XJ[4]), None)
    check("...while a record with mistyped fields still lists (so it can be "
          "deleted), the bad fields blank", r is not None and r["name"] == ""
          and r["cwd"] == "" and r["flags"] is None and r["last_active"] == 0.0)
    check("...newest first",
          [x["last_active"] for x in recs]
          == sorted((x["last_active"] for x in recs), reverse=True))

    # ----------------------------------------------------------------------- #
    print("\n== resume, with the exec stubbed: where, as whom, with what ==")

    class TTYish(io.StringIO):
        def isatty(self):
            return True

    def resume(rec, mode="as-started", live=(), tty_in=True, tty_out=True,
               ask=None):
        """M.resume_chat with os.execve stubbed: (what it returned, the exec it
        made or None, what it printed). The cwd is read INSIDE the stub --
        resume chdirs first -- and put back in the finally, when there was one
        to put back (a check below runs it from a deleted directory). `ask`
        is passed only when given, so every older check calls it as before."""
        seen = {}

        def fake_execve(path, argv, env):
            seen.update(path=path, argv=list(argv), env=dict(env),
                        cwd=os.getcwd())
            raise SystemExit(0)

        try:
            back = os.getcwd()
        except OSError:
            back = None
        real = os.execve
        saved = (sys.stdin, sys.stdout)
        out = TTYish() if tty_out else io.StringIO()
        sys.stdin = TTYish() if tty_in else io.StringIO()
        sys.stdout = out
        os.execve = fake_execve
        got = None
        try:
            got = M.resume_chat(rec, mode,
                                live=None if live is None else set(live),
                                **({} if ask is None else {"ask": ask}))
        except SystemExit:
            pass
        except Exception as exc:          # noqa: BLE001 -- reported, not fatal
            got = f"RAISED {exc!r}"
        finally:
            os.execve = real
            sys.stdin, sys.stdout = saved
            if back:
                os.chdir(back)
        return got, (seen or None), ANSI.sub("", out.getvalue())

    def cli_resume(args, typed=""):
        """M._history_resume(args) on a pretend terminal, os.execve stubbed,
        `typed` what is typed at its prompts: (exit code, the exec or None,
        stdout, stderr)."""
        seen = {}

        def fake_execve(path, argv, env):
            seen.update(path=path, argv=list(argv), env=dict(env),
                        cwd=os.getcwd())
            raise SystemExit(0)

        back, real = os.getcwd(), os.execve
        saved = (sys.stdin, sys.stdout, sys.stderr)
        out, err = TTYish(), io.StringIO()
        sys.stdin, sys.stdout, sys.stderr = TTYish(typed), out, err
        os.execve = fake_execve
        rc = None
        try:
            rc = M._history_resume(list(args))
        except SystemExit as exc:
            rc = exc.code
        except Exception as exc:          # noqa: BLE001 -- reported, not fatal
            rc = f"RAISED {exc!r}"
        finally:
            os.execve = real
            sys.stdin, sys.stdout, sys.stderr = saved
            os.chdir(back)
        return rc, (seen or None), ANSI.sub("", out.getvalue()), err.getvalue()

    def aged(path, secs=3600.0):
        """`path`, last written `secs` ago."""
        t = time.time() - secs
        os.utime(path, (t, t))
        return path

    def dead_pid():
        """The pid of a process that has exited, and been reaped."""
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def rec_for(conv):
        return next((x for x in M.load_history() if x["conversation"] == conv),
                    None)

    RCHAT = os.path.join(tmp, "chats", "my proj")
    os.makedirs(RCHAT)
    with open(os.path.join(RCHAT, "s.json"), "w") as fh:
        fh.write("{}")                      # a settings FILE, relative to it
    _n = [0]

    def chat(flags=None, cfg="", cwd=RCHAT, project=None, base=None,
             with_transcript=True, kept=False):
        """A kept chat to resume, its transcript last written an hour ago --
        a chat written moments ago is a question of its own, checked below."""
        _n[0] += 1
        x = cid(f"resume-{_n[0]}")
        fields = {"name": os.path.basename(cwd), "cwd": cwd,
                  "config_dir": cfg, "flags": flags}
        if project:
            fields["project"] = project
        plant(HOME, x, **fields)
        if with_transcript:
            aged(transcript(base or cfg or os.path.join(HOME, ".claude"), x,
                            cwd, title="t"))
        if kept:
            aged(write_transcript(os.path.join(M.history_dir(), x + ".jsonl"),
                                  cwd, "kept"))
        return x

    def flags_of(ex):
        return ex["argv"][2:-2] if ex else None

    X1 = chat(["--model", "opus", "--dangerously-skip-permissions",
               "fix the tests", "-w", "feat", "--settings",
               '{"env": {"API_KEY": "sk-x"}}', "--settings", "s.json"])
    os.environ["CLAUDE_CONFIG_DIR"] = "/somewhere/else"   # inherited: must not win
    try:
        got, ex, out = resume(rec_for(X1))
    finally:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
    check("it execs the WRAPPER, so the resumed chat is tracked again",
          bool(ex) and ex["path"] == sys.executable
          and ex["argv"][:2] == [sys.executable, M.WRAP], ascii(got))
    check("...with the flags re-filtered by the wrapper's policy: the prompt "
          "(it would be SENT AGAIN), -w (a NEW worktree) and inline settings "
          "JSON (where tokens go) are dropped; model, permissions and a "
          "settings FILE stay",
          bool(ex) and ex["argv"][2:] == ["--model", "opus",
                                          "--dangerously-skip-permissions",
                                          "--settings", "s.json",
                                          "--resume", X1],
          ascii(ex and ex["argv"][2:]))
    check("...in the chat's own directory: claude finds the conversation "
          "through it", bool(ex)
          and os.path.realpath(ex["cwd"]) == os.path.realpath(RCHAT))
    check("...on the default login: an inherited CLAUDE_CONFIG_DIR is removed, "
          "and the account is named", bool(ex)
          and "CLAUDE_CONFIG_DIR" not in ex["env"]
          and ex["env"].get("CLAUDE_MATE_ACCOUNT") == "default")
    check("...and it says what it is doing",
          f"resuming: my proj in {RCHAT} as default" in out, ascii(out))
    got, ex, out = resume(rec_for(chat(["--dangerously-skip-permissions",
                                        "--model", "opus"])), "skip")
    check("skip, on a chat that already skipped: the flag once, never twice",
          flags_of(ex) == ["--dangerously-skip-permissions", "--model", "opus"],
          ascii(flags_of(ex)))
    got, ex, out = resume(rec_for(chat(
        ["--permission-mode", "plan", "--permission-mode=acceptEdits",
         "--model", "opus"])), "skip")
    check("skip: every --permission-mode goes, in both spellings, and the skip "
          "flag is added",
          flags_of(ex) == ["--model", "opus", "--dangerously-skip-permissions"],
          ascii(flags_of(ex)))
    got, ex, out = resume(rec_for(chat(["--restricted", "--model", "opus"])),
                          "skip")
    check("skip, on a chat started --restricted: refused, not contradicted "
          "(--restricted refuses bypass)",
          ex is None and "--restricted" in (got or ""), ascii(got))
    got, ex, out = resume(rec_for(chat(
        ["--dangerously-skip-permissions", "--permission-mode",
         "bypassPermissions", "--permission-mode=bypassPermissions",
         "--model", "opus"])), "ask")
    check("ask: every way of skipping goes -- the flag, and bypassPermissions "
          "in both spellings", flags_of(ex) == ["--model", "opus"],
          ascii(flags_of(ex)))
    got, ex, out = resume(rec_for(chat(["--permission-mode", "plan"])), "ask")
    check("...and a mode that asks is left as it was",
          flags_of(ex) == ["--permission-mode", "plan"], ascii(flags_of(ex)))
    # A FLAG'S VALUE IS NOT A FLAG. Filtered word by word, the session NAMED
    # "--dangerously-skip-permissions" lost its name in "ask" and left --name to
    # swallow --resume: claude would have started a NEW session with the id as
    # its prompt. And the menu labelled it as one that skipped permissions.
    NAMED = ["--name", "--dangerously-skip-permissions", "--model", "opus"]
    got, ex, out = resume(rec_for(chat(NAMED)), "ask")
    check("ask: a session NAMED --dangerously-skip-permissions keeps its name, "
          "and --resume <id> stays an argument of its own",
          flags_of(ex) == NAMED and bool(ex)
          and ex["argv"][-2] == "--resume", ascii(ex and ex["argv"][2:]))
    check("...and it is not taken for a chat that skipped permissions",
          not M.skips_permissions(NAMED)
          and M.skips_permissions(["--permission-mode", "bypassPermissions"])
          and M.skips_permissions(["--permission-mode=bypassPermissions"])
          and M.skips_permissions(["--dangerously-skip-permissions"]))
    got, ex, out = resume(rec_for(chat(NAMED)), "skip")
    check("skip: the name stays, and the skip flag is its own word",
          flags_of(ex) == NAMED + ["--dangerously-skip-permissions"],
          ascii(flags_of(ex)))
    XH = chat(["--resume", "0000dead-0000-4000-8000-00000000dead", "-p",
               "print this", "--fork-session"])
    got, ex, out = resume(rec_for(XH))
    check("a hand-edited record cannot steer the exec: another conversation, "
          "-p and --fork-session are filtered out again",
          bool(ex) and ex["argv"][2:] == ["--resume", XH],
          ascii(ex and ex["argv"][2:]))
    W2 = os.path.join(ACCTS, "work 2")
    got, ex, out = resume(rec_for(chat(["--model", "opus"], cfg=W2 + "/")))
    check("a profile login is set VERBATIM, trailing slash and all (claude "
          "hashes the exact string into its Keychain entry)",
          bool(ex) and ex["env"].get("CLAUDE_CONFIG_DIR") == W2 + "/"
          and ex["env"].get("CLAUDE_MATE_ACCOUNT") == "work 2", ascii(got))
    got, ex, out = resume(rec_for(chat(cfg=None)))
    check("login not recorded: the one the transcript is found under -- "
          "~/.claude is the default", bool(ex)
          and "CLAUDE_CONFIG_DIR" not in ex["env"]
          and ex["env"].get("CLAUDE_MATE_ACCOUNT") == "default", ascii(got))
    AX = os.path.join(ACCTS, "acct-x")
    got, ex, out = resume(rec_for(chat(cfg=None, base=AX)))
    check("...and a profile's is that profile",
          bool(ex) and ex["env"].get("CLAUDE_CONFIG_DIR") == AX
          and ex["env"].get("CLAUDE_MATE_ACCOUNT") == "acct-x", ascii(got))
    XK = chat(["--model", "opus"], with_transcript=False, kept=True)
    got, ex, out = resume(rec_for(XK))
    dst = os.path.join(HOME, ".claude", "projects", mangle(RCHAT), XK + ".jsonl")
    check("Claude's copy gone, the kept one there: put back where --resume looks "
          "(the cwd's project dir) -- the same inode -- then resumed",
          bool(ex) and os.path.isfile(dst)
          and same(dst, os.path.join(M.history_dir(),
                                                 XK + ".jsonl"))
          and os.path.realpath(ex["cwd"]) == os.path.realpath(RCHAT), ascii(got))
    XK2 = chat(with_transcript=False, kept=True, project="-where-it-was")
    got, ex, out = resume(rec_for(XK2))
    dst = os.path.join(HOME, ".claude", "projects", "-where-it-was",
                       XK2 + ".jsonl")
    check("...into the project directory it was last seen in, when the record "
          "knows it", bool(ex) and os.path.isfile(dst)
          and same(dst, os.path.join(M.history_dir(),
                                                 XK2 + ".jsonl")), ascii(got))
    XL = chat(["--model", "opus"])
    got, ex, out = resume(rec_for(XL), live={XL})
    check("REFUSED while it is open in a terminal: raise it instead",
          ex is None and "open in a terminal" in (got or ""), ascii(got))
    ctxd = os.path.join(TMPD, f"claude-mate-ctx-{os.getuid()}")
    os.makedirs(ctxd, mode=0o700, exist_ok=True)
    os.chmod(ctxd, 0o700)           # the wrapper's own mode, whatever the umask
    ctxf = os.path.join(ctxd, "ttys009.json")
    with open(ctxf, "w") as fh:
        json.dump({"pid": os.getpid(), "conversation": XL,
                   "updated_at": time.time()}, fh)
    try:
        got, ex, out = resume(rec_for(XL), live=None)
    finally:
        os.unlink(ctxf)
    check("...known from the terminal's own context file when no daemon "
          "answers: two claudes on one transcript fork it",
          ex is None and "open in a terminal" in (got or ""), ascii(got))
    # A KILLED TERMINAL'S ROW. kill -9 sends no `end`, so the daemon keeps the
    # row until it prunes it -- minutes in which the chat would be refused as
    # open. A row that has missed a few keepalives does not make a chat open.
    row = {"key": "deadbeef", "conversation": XL}
    check("a queue row still being heard from makes its chat open; one quiet "
          "past LIVE_REPORT_S does not; a daemon with no `seen` is believed",
          XL in M.live_conversations({"queue": [dict(row, seen=5)]})
          and XL not in M.live_conversations(
              {"queue": [dict(row, seen=M.LIVE_REPORT_S + 1)]})
          and XL in M.live_conversations({"queue": [row]}))
    got, ex, out = resume(rec_for(chat(
        cfg=os.path.join(ACCTS, "deleted-profile"), with_transcript=False)))
    check("REFUSED when its account profile is gone, naming it",
          ex is None and "deleted-profile" in (got or "")
          and "no longer exists" in (got or ""), ascii(got))
    XN2 = chat(with_transcript=False)
    got, ex, out = resume(rec_for(XN2))
    check("REFUSED with no transcript anywhere: where it looked, and what MAY "
          "have removed it", ex is None and f"no {XN2[:8]}" in (got or "")
          and os.path.join(HOME, ".claude", "projects") in (got or "")
          and "cleanupPeriodDays" in (got or "")
          and "claude purge" in (got or "") and "may" in (got or ""),
          ascii(got))
    got, ex, out = resume(rec_for(chat(cwd=os.path.join(tmp, "chats", "gone"))))
    check("REFUSED when its directory is gone: claude finds the conversation "
          "through it", ex is None and "no longer exists" in (got or "")
          and "claude finds the conversation through it" in (got or ""),
          ascii(got))
    got, ex, out = resume(rec_for(X1), tty_in=False)
    got2, ex2, out2 = resume(rec_for(X1), tty_out=False)
    check("REFUSED without a terminal on both ends: the wrapper would run "
          "claude unwrapped, untracked", ex is None and ex2 is None
          and "terminal on both ends" in (got or "")
          and "terminal on both ends" in (got2 or ""), ascii((got, got2)))

    # A PROFILE DELETED WITH ITS CHAT KEPT. The kept link is the whole
    # conversation, so the refusal says so -- and what brings it back -- where
    # it used to read as though the chat were lost with the profile.
    got, ex, out = resume(rec_for(chat(
        cfg=os.path.join(ACCTS, "gone-profile"), with_transcript=False,
        kept=True)))
    check("REFUSED when its profile is gone but the chat was kept: it says the "
          "conversation is kept, and to recreate the profile and log in",
          ex is None and "gone-profile" in (got or "")
          and "no longer exists" in (got or "")
          and "the conversation is kept; recreate the profile and log in to "
              "resume it as that account" in (got or ""), ascii(got))
    got, ex, out = resume(rec_for(chat(
        cfg=os.path.join(ACCTS, "gone-too"), with_transcript=False)))
    check("...and with nothing kept, it does not say the conversation is",
          ex is None and "gone-too" in (got or "")
          and "is kept" not in (got or ""), ascii(got))

    # OPENED THROUGH CLAUDE'S OWN PICKER (`claude --resume`, -r <name>,
    # --from-pr): the wrapper never learns the id for certain, so its context
    # file names no conversation -- and the chat read as closed while it ran,
    # and Resume started a second claude on it. The session id its transcript
    # scan found is there all the same, and for liveness that is enough.
    XP = chat(["--model", "opus"])
    with open(ctxf, "w") as fh:
        json.dump({"pid": os.getpid(), "conversation": "", "session_id": XP,
                   "transcript": "", "updated_at": time.time()}, fh)
    try:
        live_p = M.live_conversations(None)
        got, ex, out = resume(rec_for(XP), live=None)
    finally:
        os.unlink(ctxf)
    check("a chat opened through claude's own picker -- its terminal's file "
          "names no conversation, only the session id the scan found -- is "
          "open, and Resume refuses it", XP in live_p and ex is None
          and "open in a terminal" in (got or ""), ascii((sorted(live_p), got)))
    XQ = cid("picker-transcript")
    with open(ctxf, "w") as fh:
        json.dump({"pid": os.getpid(), "conversation": "", "session_id": "",
                   "transcript": os.path.join(HOME, ".claude", "projects",
                                              "-p", XQ + ".jsonl"),
                   "updated_at": time.time()}, fh)
    try:
        live_q = M.live_conversations(None)
    finally:
        os.unlink(ctxf)
    with open(ctxf, "w") as fh:
        json.dump({"pid": dead_pid(), "conversation": "", "session_id": XQ,
                   "updated_at": time.time()}, fh)
    try:
        live_d = M.live_conversations(None)
    finally:
        os.unlink(ctxf)
    check("...so is the <uuid>.jsonl its transcript names -- while a dead "
          "terminal's file names nothing open", XQ in live_q
          and XQ not in live_d, ascii((sorted(live_q), sorted(live_d))))

    # A TRANSCRIPT SOMETHING IS STILL WRITING, with no terminal claude-mate can
    # see on it: claude run without the wrapper, the VS Code panel, a picker
    # session before its first turn. None of them says which chat it has; the
    # fresh write is all there is to go on, so it is put to the person.
    XR = chat(["--model", "opus"])
    TR = M.chat_transcript(rec_for(XR))
    aged(TR, 10.0)
    asked = []
    got, ex, out = resume(rec_for(XR), ask=lambda q: asked.append(q) or False)
    check("a transcript written 10 s ago, no live terminal: the question is "
          "put -- when it was written, and what it may be that claude-mate "
          "cannot see -- and a no runs nothing", ex is None and got == ""
          and len(asked) == 1
          and re.search(r"its transcript was written 1\ds ago and no terminal "
                        r"claude-mate can see has it", asked[0] or "")
          and "the VS Code panel" in asked[0]
          and "resuming it twice writes one transcript from two places"
          in asked[0], ascii((got, asked)))
    got, ex, out = resume(rec_for(XR), ask=lambda q: True)
    check("...a yes resumes it", bool(ex) and ex["argv"][-2:] == ["--resume", XR],
          ascii(got))
    got, ex, out = resume(rec_for(XR))
    check("...and with nobody to ask, the question is the refusal",
          ex is None and (got or "").startswith("its transcript was written"),
          ascii(got))
    # getattr: a CLI without the constant fails these checks, not the file.
    aged(TR, getattr(M, "RECENT_WRITE_S", 120.0) + 60)
    asked = []
    got, ex, out = resume(rec_for(XR), ask=lambda q: asked.append(q) or False)
    check("...one last written longer ago than RECENT_WRITE_S is not asked "
          "about", bool(ex) and not asked, ascii((got, asked)))
    aged(TR, 10.0)
    rc, ex, out, err = cli_resume([XR[:8]], typed="no\n")
    check("the CLI puts it as a typed question: anything but `yes` is "
          "\"not resumed.\", exit 1, nothing run", rc == 1 and ex is None
          and "type yes to resume it anyway:" in out
          and "not resumed." in out, ascii((rc, out[-300:], err)))
    rc, ex, out, err = cli_resume([XR[:8]], typed="")
    check("...end of input there is 130", rc == 130 and ex is None,
          ascii((rc, err)))
    rc, ex, out, err = cli_resume([XR[:8]], typed="yes\n")
    check("...and `yes` resumes it", rc == 0 and bool(ex)
          and ex["argv"][-2:] == ["--resume", XR], ascii((rc, err)))

    # OPEN IS LOOKED AT RIGHT BEFORE THE EXEC. `history resume` with no
    # argument read liveness, then sat in its chooser for as long as the person
    # took -- and resumed a chat opened in another terminal meanwhile.
    XC = chat(["--model", "opus"])
    real_pick = M.pick

    def opened_meanwhile(title, items, **_kw):
        with open(ctxf, "w") as fh:
            json.dump({"pid": os.getpid(), "conversation": XC,
                       "updated_at": time.time()}, fh)
        return next((it.value for it in items if it.value[0] == "chat"
                     and it.value[1]["conversation"] == XC), None)

    M.pick = opened_meanwhile
    try:
        rc, ex, out, err = cli_resume([])
    finally:
        M.pick = real_pick
        if os.path.exists(ctxf):
            os.unlink(ctxf)
    check("`history resume` from its chooser: a chat opened in another "
          "terminal while the chooser was up is refused, not resumed twice",
          ex is None and "open in a terminal" in err, ascii((rc, err)))

    # BY NUMBER, WHICH CHAT FIRST. The number is the list as `claude-mate
    # history` printed it, and it reorders as chats run -- so a resume by index
    # says which chat it is now and waits for Enter; an id goes straight on. A
    # HOME of its own, so the indices are these two chats and no others.
    HOME_I = os.path.join(tmp, "home-index")
    CHAT_I = os.path.join(tmp, "chats", "index proj")
    os.makedirs(CHAT_I)
    I0, I1 = cid("index-0"), cid("index-1")
    home_was = os.environ["HOME"]
    os.environ["HOME"] = HOME_I
    try:
        for _conv, _ago, _title in ((I0, 60, "the newer chat"),
                                    (I1, 7200, "the older chat")):
            plant(HOME_I, _conv, name="index proj", cwd=CHAT_I,
                  last_active=time.time() - _ago)
            aged(transcript(os.path.join(HOME_I, ".claude"), _conv, CHAT_I,
                            title=_title))
        rc, ex, out, err = cli_resume(["1"])
        check("`history resume <n>` says which chat that is now -- short id, "
              "title, cwd -- and asks for Enter; end of input is 130 and runs "
              "nothing", rc == 130 and ex is None and I1[:8] in out
              and "the older chat" in out and CHAT_I in out
              and "press enter to resume it, ctrl-c to stop" in out,
              ascii((rc, out[-300:], err)))
        rc, ex, out, err = cli_resume(["1"], typed="\n")
        check("...Enter resumes that chat", rc == 0 and bool(ex)
              and ex["argv"][-2:] == ["--resume", I1], ascii((rc, err)))
        rc, ex, out, err = cli_resume(["1"], typed="n\n")
        check("...anything typed before Enter is not resumed, exit 1",
              rc == 1 and ex is None and "not resumed." in out,
              ascii((rc, out[-200:], err)))
        rc, ex, out, err = cli_resume([I1[:8]])
        check("...while an id cannot shift: resumed with no question", rc == 0
              and bool(ex) and ex["argv"][-2:] == ["--resume", I1]
              and "press enter" not in out, ascii((rc, out[-200:], err)))
    finally:
        os.environ["HOME"] = home_was

    # FROM A DIRECTORY DELETED UNDER THE SHELL. The wrapper names its session
    # after os.getcwd() as it loads, and that raises there: every resume was
    # refused as "cannot load the wrapper ... No such file or directory", and
    # the chat menu lost the flags it labels Resume by.
    XD = chat(["--dangerously-skip-permissions"])
    GONE = os.path.join(tmp, "deleted-cwd")
    os.makedirs(GONE)
    here = os.getcwd()
    del M._WRAPPER[:]               # loaded fresh, as a new CLI process would
    os.chdir(GONE)
    os.rmdir(GONE)
    try:
        items_d = M._flag_items(["--dangerously-skip-permissions"])
        got, ex, out = resume(rec_for(XD))
    finally:
        os.chdir(here)
    check("started in a directory that was deleted, the CLI still loads the "
          "wrapper: the menu keeps the chat's flags, and the resume goes "
          "ahead in the chat's own directory",
          items_d == [["--dangerously-skip-permissions"]] and bool(ex)
          and ex["argv"][2:] == ["--dangerously-skip-permissions", "--resume",
                                 XD]
          and os.path.realpath(ex["cwd"]) == os.path.realpath(RCHAT),
          ascii((items_d, got)))

    # THE CONTEXT FILES ARE ONLY AS TRUE AS THEIR DIRECTORY IS PRIVATE. Where
    # the temp dir is a shared /tmp, another user who made claude-mate-ctx-<uid>
    # first could plant files holding any chat "open" -- refused by rm, clear
    # and resume.
    XO = cid("ctx-trust")
    with open(ctxf, "w") as fh:
        json.dump({"pid": os.getpid(), "conversation": XO,
                   "updated_at": time.time()}, fh)
    try:
        open_private = XO in M.live_conversations(None)
        os.chmod(ctxd, 0o777)
        try:
            open_shared = XO in M.live_conversations(None)
        finally:
            os.chmod(ctxd, 0o700)
        os.rename(ctxd, ctxd + ".real")
        os.symlink(ctxd + ".real", ctxd)
        try:
            open_linked = XO in M.live_conversations(None)
        finally:
            os.unlink(ctxd)
            os.rename(ctxd + ".real", ctxd)
        try:
            by_owner = (M._read_regular(ctxf, uid=os.getuid()) is not None
                        and M._read_regular(ctxf, uid=os.getuid() + 1) is None)
        except TypeError:
            by_owner = False
    finally:
        os.unlink(ctxf)
    check("the terminals' context dir counts only while no one else can write "
          "it: group/world-writable, or a symlink in its place, and nothing in "
          "it makes a chat open", open_private and not open_shared
          and not open_linked, ascii((open_private, open_shared, open_linked)))
    check("...and in it, only a file this user owns is read", by_owner)

    # RAISE ITS TERMINAL GOES TO THE ROW STILL REPORTING. A terminal killed
    # outright keeps its row for minutes; the chat resumed since, in the same
    # directory, has a second one with the same name, and which sorts first is
    # a coin toss on their random keys. The dead row's sorts first here.
    XZ = cid("raise")
    said = []
    real_q, real_talk = M._queue_snapshot, M.talk

    def queue_of_rows(*rows):
        M._queue_snapshot = lambda timeout=2.0: {"queue": [
            dict(r, conversation=XZ, name="proj") for r in rows]}

    M.talk = lambda line_, timeout=6.0: said.append(line_) or "ok proj"
    try:
        queue_of_rows({"key": "0aaa0001", "seen": 120},
                      {"key": "fbbb0002", "seen": 3})
        note_a = M._raise_chat({"conversation": XZ})
        said_a = said[:]
        del said[:]
        queue_of_rows({"key": "0aaa0001", "seen": 30},
                      {"key": "fbbb0002", "seen": 3})
        M._raise_chat({"conversation": XZ})
        said_b = said[:]
        del said[:]
        queue_of_rows({"key": "0aaa0001", "seen": 120})
        note_c = M._raise_chat({"conversation": XZ})
        said_c = said[:]
    finally:
        M._queue_snapshot, M.talk = real_q, real_talk
    check("Raise its terminal, with a killed terminal's row first in the queue "
          "(quiet for 120 s) and the resumed chat's after it: GO goes to the "
          "one still reporting", said_a == ["select|fbbb0002", "press|G"]
          and "raised" in note_a, ascii((said_a, note_a)))
    check("...of two still reporting, the one heard from last",
          said_b == ["select|fbbb0002", "press|G"], ascii(said_b))
    check("...and with only the dead row left, nothing is selected and the "
          "note says its terminal has stopped reporting",
          said_c == [] and "stopped reporting" in note_c, ascii((said_c, note_c)))

    # ----------------------------------------------------------------------- #
    print("\n== resume_flags: a kept chat's flags survive a second filtering ==")
    rf_dir = os.path.join(tmp, "rf")
    os.makedirs(rf_dir)
    with open(os.path.join(rf_dir, "s.json"), "w") as fh:
        fh.write("{}")
    POOL = ["--model", "opus", "--dangerously-skip-permissions", "--add-dir",
            "/a", "--settings", "s.json", "--mcp-config", "fix it", "-w",
            "--name", "--", "--permission-mode=plan", "--resume"]

    def subsequence(part, whole):
        it = iter(whole)
        return all(any(x == y for y in it) for x in part)

    bad = []
    total = 0
    for _len in range(1, 4):
        for _argv in itertools.product(POOL, repeat=_len):
            total += 1
            once = W.resume_flags(list(_argv), cwd=rf_dir)
            if (W.resume_flags(once, cwd=rf_dir) != once
                    or not subsequence(once, _argv)):
                bad.append((list(_argv), once))
    check(f"filtering what it kept changes nothing, and it only keeps words of "
          f"the original, in order ({total} argvs) -- the record stores them "
          f"filtered, resume filters them again", not bad, ascii(bad[:1]))
    check("anything that is not an argv is no flags at all",
          W.resume_flags(None) == [] and W.resume_flags(["--model", 5]) == []
          and W.resume_flags(42) == [])

    # ----------------------------------------------------------------------- #
    # Real processes from here on
    # ----------------------------------------------------------------------- #
    stubs = os.path.join(tmp, "bin")
    os.makedirs(stubs)
    for prog in ("security", "curl", "open", "osascript", "afplay", "dns-sd"):
        with open(os.path.join(stubs, prog), "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(stubs, prog), 0o755)

    class Device:
        """A PTY standing in for the device's cable; what the daemon writes is
        read and dropped."""

        def __init__(self):
            self.master, self.slave = pty.openpty()
            tty.setraw(self.master)
            self.port = os.ttyname(self.slave)
            threading.Thread(target=self._drain, daemon=True).start()

        def _drain(self):
            while True:
                try:
                    if not os.read(self.master, 4096):
                        return
                except OSError:
                    return

    def start_daemon(home, sock, *args, accounts=None):
        """A real daemon with a HOME of its own (its history goes there), a PTY
        for a device and stubs for what it shells out to."""
        dev = Device()
        env = dict(os.environ)
        env.update({"HOME": home, "CLAUDE_MATE_SOCK": sock,
                    "CLAUDE_MATE_PORT": dev.port, "CLAUDE_MATE_BAUD": "115200",
                    "CLAUDE_MATE_ACCOUNTS_DIR":
                        accounts or os.path.join(tmp, "no-accounts"),
                    "PATH": stubs + os.pathsep + env.get("PATH", "")})
        proc = subprocess.Popen([sys.executable, DAEMON] + list(args), env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
        proc.cm_dev = dev
        dlog = []
        threading.Thread(target=lambda: [dlog.append(x.rstrip())
                                         for x in proc.stderr],
                         daemon=True).start()
        procs.append(proc)
        return proc, dlog

    def cli(*args, home, sock=NOBODY, tmpdir=None, stdin=None, env_extra=None):
        """The real CLI in a process of its own: (exit code, stdout, stderr).
        `stdin` is what gets typed at its prompt; None is /dev/null."""
        env = dict(os.environ)
        env.update({"HOME": home, "CLAUDE_MATE_SOCK": sock,
                    "TMPDIR": tmpdir or TMPD,
                    "CLAUDE_MATE_ACCOUNTS_DIR": os.path.join(tmp, "no-accounts")})
        env.update(env_extra or {})
        r = subprocess.run([sys.executable, CLI] + list(args), env=env, cwd=ROOT,
                           input=None if stdin is None else stdin.encode(),
                           stdin=subprocess.DEVNULL if stdin is None else None,
                           capture_output=True, timeout=30)
        return (r.returncode, r.stdout.decode("utf-8", "replace"),
                r.stderr.decode("utf-8", "replace"))

    def rows_of(out):
        """The list's rows, split into their columns."""
        return [re.split(r"\s{2,}", ln.strip()) for ln in out.splitlines()
                if ln[:3].strip().isdigit()]

    def row_for(out, conv):
        return next((r for r in rows_of(out) if len(r) > 1
                     and r[1] == conv[:8]), [])

    # --mock starts first and is looked at last: its demo seeds sessions at once.
    HOME_K = os.path.join(tmp, "home-mock")
    os.makedirs(HOME_K)
    SOCK_K = os.path.join(tmp, "k.sock")
    mock_proc, mock_log = start_daemon(HOME_K, SOCK_K, "--mock")

    print("\n== a real daemon: the record outlives a SIGKILL, and the daemon ==")
    HOME_R = os.path.join(tmp, "home-real")
    os.makedirs(HOME_R)
    SOCK_R = os.path.join(tmp, "r.sock")
    HIST_R = os.path.join(HOME_R, ".config", "claude-mate", "history")
    os.makedirs(os.path.join(tmp, "real chats", "proj|pipe"))
    CHAT_D = os.path.realpath(os.path.join(tmp, "real chats", "proj|pipe"))
    UR = cid("real")
    RP = os.path.join(HIST_R, UR + ".json")
    KEPT_R = os.path.join(HIST_R, UR + ".jsonl")
    tr_r = transcript(os.path.join(HOME_R, ".claude"), UR, CHAT_D,
                      title="keep chats until deleted")
    proc, dlog = start_daemon(HOME_R, SOCK_R)
    try:
        up = wait_for(lambda: queue_of(SOCK_R) is not None, 20)
        check("the daemon comes up, and with nothing named to it has no history "
              "directory: nothing is read or written at startup",
              bool(up) and not os.path.exists(HIST_R),
              ascii(" | ".join(dlog[-4:])))
        check("...and says where it keeps it",
              wait_for(lambda: any("history: ~/.config/claude-mate/history"
                                   in x for x in dlog), 5))
        w1 = Wrapper(SOCK_R, CHAT_D, flags=W.resume_flags(
            ["--model", "opus", "--dangerously-skip-permissions",
             "fix the tests"]))
        w1.send("idle")
        w1.send("working")          # the turn starts before the id is learned
        time.sleep(1.0)
        check("a turn with no conversation id yet keeps nothing",
              not os.path.exists(HIST_R))
        w1.conv = UR                # its transcript exists: the id rides along
        w1.send("working")
        rec = wait_for(lambda: read_json(RP), 8) or {}
        check("the report that names it keeps the chat: its id, directory, "
              "login, flags and account, not closed",
              rec.get("conversation") == UR
              and os.path.realpath(rec.get("cwd") or "/") == CHAT_D
              and rec.get("name") == "proj|pipe" and rec.get("config_dir") == ""
              and rec.get("flags") == ["--model", "opus",
                                       "--dangerously-skip-permissions"]
              and rec.get("account") == "default"
              and rec.get("ended", 0) is None
              and (rec.get("last_active") or 0) > 0, ascii(rec))
        check("...0600 in a 0700 directory", os.path.isfile(RP)
              and mode_of(RP) == 0o600 and mode_of(HIST_R) == 0o700)
        check("...with the transcript kept beside it: the same inode, no disk",
              os.path.isfile(KEPT_R) and same(tr_r, KEPT_R))
        e = (queue_of(SOCK_R) or {}).get(w1.sid, {})
        check("the queue says which conversation the row runs, and where",
              e.get("conversation") == UR
              and os.path.realpath(e.get("cwd") or "/") == CHAT_D, ascii(e))
        w1.send("idle")
        time.sleep(0.7)
        before = raw_of(RP)
        proc.kill()                 # SIGKILL: no finally, no last flush
        proc.wait(10)
        check("SIGKILL: the record is still there, whole -- and does not claim "
              "the chat was closed", raw_of(RP) == before and before is not None
              and (read_json(RP) or {}).get("ended", 0) is None)
        proc, dlog = start_daemon(HOME_R, SOCK_R)
        up = wait_for(lambda: queue_of(SOCK_R) is not None, 20)
        time.sleep(0.7)
        check("started again, the daemon leaves the record alone: not read at "
              "startup, not rewritten", bool(up) and raw_of(RP) == before)
        rc, out, err = cli("history", home=HOME_R, sock=SOCK_R)
        row = row_for(out, UR)
        check("`claude-mate history` lists it: interrupted, since nobody said "
              "it closed", rc == 0 and row[2:3] == ["interrupted"]
              and row[-1:] == ["keep chats until deleted"], ascii(row or out))
        w1.send("idle")             # its next keepalive: the new daemon learns it
        live = wait_for(lambda: (queue_of(SOCK_R) or {}).get(
            w1.sid, {}).get("conversation") == UR, 5)
        rc, out, err = cli("history", home=HOME_R, sock=SOCK_R)
        check("...open, once its wrapper reports to the new daemon (liveness "
              "from the daemon's queue)", bool(live)
              and row_for(out, UR)[2:3] == ["open"]
              and "daemon not running" not in out, ascii(out))
        time.sleep(0.7)
        check("...and that keepalive, from a session that ran no turn HERE, "
              "writes nothing", raw_of(RP) == before)
        talk(SOCK_R, f"done|{UR}|proj-pipe\n")    # the hook's row, same chat
        w1.send("end")
        closed = wait_for(lambda: (read_json(RP) or {}).get("ended"), 8)
        check("its `end` closes the record -- this daemon never saw it run a "
              "turn, and need not have", bool(closed))
        # THE LOG LINE TRAILS THE FILE: the writer logs after its rename, and
        # stderr reaches this test through a reader thread -- read at once,
        # a slow runner (CI's Python 3.9) saw the record closed and no line.
        def closed_lines():
            return [x for x in dlog if "history: proj|pipe" in x
                    and UR[:8] in x and x.endswith("closed")]
        wait_for(closed_lines, 5)
        check("...with one log line for it", len(closed_lines()) == 1,
              ascii([x for x in dlog if "history" in x]))
        rc, out, err = cli("history", home=HOME_R, sock=SOCK_R)
        check("...and the list says closed, though a hook's row for the same "
              "chat is still in the queue (hooks never send end)",
              row_for(out, UR)[2:3] == ["closed"]
              and UR in (queue_of(SOCK_R) or {}), ascii(out))
        w1.send("working")
        time.sleep(0.3)
        check("a report after `end` never leaves the wrapper: no ghost row",
              w1.sid not in (queue_of(SOCK_R) or {}))
        last = (read_json(RP) or {}).get("last_active") or 0
        w2 = Wrapper(SOCK_R, CHAT_D, conv=UR, flags=w1.flags)   # --resume <id>
        w2.send("idle")
        time.sleep(0.8)
        check("resumed, but before its first turn: still closed",
              (read_json(RP) or {}).get("ended") is not None)
        w2.send("working")
        reopened = wait_for(lambda: (read_json(RP) or {"ended": 0}).get(
            "ended", 0) is None, 8)
        check("...its first turn reopens it and moves last_active on",
              bool(reopened) and (read_json(RP) or {}).get("last_active", 0)
              > last)
        w2.send("end")
        wait_for(lambda: w2.sid not in (queue_of(SOCK_R) or {w2.sid: 1}), 5)
        proc.terminate()
        try:
            stopped = proc.wait(15) is not None
        except subprocess.TimeoutExpired:
            stopped = False
        check("stopped right after an `end`, the daemon still records it: the "
              "writer's own last flush", stopped
              and (read_json(RP) or {}).get("ended") is not None)
    finally:
        stop_daemon(proc)

    print("\n== ...while old line shapes, and --mock, keep nothing ==")
    HOME_O = os.path.join(tmp, "home-old")
    os.makedirs(HOME_O)
    SOCK_O = os.path.join(tmp, "o.sock")
    HIST_O = os.path.join(HOME_O, ".config", "claude-mate", "history")
    ACCTS_O = os.path.join(tmp, "accounts-old")
    os.makedirs(os.path.join(ACCTS_O, "work"))
    proc, dlog = start_daemon(HOME_O, SOCK_O, accounts=ACCTS_O)
    try:
        up = wait_for(lambda: queue_of(SOCK_O) is not None, 20)
        old = ["idle|abcd1234|proj|/tmp/c.sock|Opus 4.8|xhigh|default||-",
               "working|abcd1234|proj|/tmp/c.sock|Opus 4.8|xhigh|default||-",
               "idle|abcd1234|proj|/tmp/c.sock|Opus 4.8|xhigh|default||-",
               "end|abcd1234|proj|/tmp/c.sock|Opus 4.8|xhigh|default||-",
               f"working|{U1}|proj", f"done|{U1}|proj",
               "working|sid-1|proj", "idle|sid-1|proj"]
        for ln in old:
            talk(SOCK_O, ln + "\n")
        time.sleep(1.0)
        q = queue_of(SOCK_O) or {}
        check("a daemon fed only the old shapes -- nine-field wrapper lines, "
              "hooks, ids that are not UUIDs, a hook WITH Claude's id -- keeps "
              "no history: there is not even a directory",
              bool(up) and U1 in q and "sid-1" in q and not os.path.exists(HIST_O))
        talk(SOCK_O, "working|cut00001|proj|/tmp/c.sock|||default||-|"
             + spec_field({"id": U2, "cwd": CHAT, "config": "", "flags": []}))
        seen = wait_for(lambda: "cut00001" in (queue_of(SOCK_O) or {}), 5)
        time.sleep(1.0)
        e = (queue_of(SOCK_O) or {}).get("cut00001", {})
        check("a wrapper line cut short (no newline): its state lands, its "
              "field 9 does not -- no conversation, no record",
              bool(seen) and e.get("state") == "working"
              and e.get("conversation") == "" and not os.path.exists(HIST_O))
        # DELETING A PROFILE NO LONGER STRANDS ITS CHATS SILENTLY. The daemon is
        # only asked for the list of logins; the records are the CLI's HOME's.
        HOME_A = os.path.join(tmp, "home-accounts")
        os.makedirs(HOME_A)
        work = os.path.join(ACCTS_O, "work")
        plant(HOME_A, cid("acct1"), config_dir=work + "/")
        plant(HOME_A, cid("acct2"), config_dir=work)
        plant(HOME_A, cid("acct3"), config_dir="")
        rc, out, err = cli("accounts", "rm", "work", home=HOME_A, sock=SOCK_O)
        check("`accounts rm` says how many kept chats ran on that profile "
              "(compared by realpath, so a trailing slash counts) and that "
              "their kept transcripts stay",
              rc == 130 and "2 chats in History were on this account" in out
              and "kept transcripts stay until you delete them there" in out
              and os.path.isdir(work), ascii(out + err))
    finally:
        stop_daemon(proc)
    try:
        up = wait_for(lambda: queue_of(SOCK_K) is not None, 20)
        wk = Wrapper(SOCK_K, CHAT_D, conv=cid("mock"), flags=[])
        wk.send("idle")
        wk.send("working")
        time.sleep(1.5)
        qk = queue_of(SOCK_K) or {}
        check("--mock keeps no history: not its demo's sessions, and not even a "
              "real wrapper's turn", bool(up) and wk.sid in qk and len(qk) >= 2
              and not os.path.exists(os.path.join(HOME_K, ".config",
                                                  "claude-mate", "history")))
        check("...and says so when it starts",
              any("history: off (--mock)" in x for x in mock_log))
    finally:
        stop_daemon(mock_proc)

    # ----------------------------------------------------------------------- #
    print("\n== the CLI, with no daemon at all: list, rm, clear ==")
    HOME_C = os.path.join(tmp, "home-cli")
    TMPD_C = os.path.join(tmp, "tmpdir-cli")
    for _d in (HOME_C, TMPD_C):
        os.makedirs(_d)
    HIST_C = os.path.join(HOME_C, ".config", "claude-mate", "history")
    CTX_C = os.path.join(TMPD_C, f"claude-mate-ctx-{os.getuid()}")
    CLAUDE_C = os.path.join(HOME_C, ".claude")

    def ccli(*args, **kw):
        return cli(*args, home=HOME_C, tmpdir=TMPD_C, **kw)

    def ctx_file(conv, pid, age=0.0):
        os.makedirs(CTX_C, mode=0o700, exist_ok=True)
        os.chmod(CTX_C, 0o700)      # the wrapper's own mode, whatever the umask
        with open(os.path.join(CTX_C, "ttys001.json"), "w") as fh:
            json.dump({"tty": "/dev/ttys001", "pid": pid, "conversation": conv,
                       "updated_at": time.time() - age}, fh)

    def kept_c(conv):
        return os.path.exists(os.path.join(HIST_C, conv + ".json"))

    rc, out, err = ccli("history")
    check("nothing kept yet: says so, and how a chat comes to be kept",
          rc == 0 and "no chats kept yet. a chat is kept after its first turn "
          "in a wrapped session." in out, ascii(out + err))
    A = "abcd1111-1111-4111-8111-111111111111"
    B = "abcd2222-2222-4222-8222-222222222222"
    C = "12345678-3333-4333-8333-333333333333"
    CHAT_C = os.path.join(tmp, "cli chats", "alpha")
    now = time.time()
    plant(HOME_C, A, name="alpha", cwd=CHAT_C, account="work",
          last_active=now - 60)
    plant(HOME_C, B, name="beta", cwd=CHAT_C, account="default",
          last_active=now - 7200, ended=now - 7000)
    plant(HOME_C, C, name="gamma", cwd=CHAT_C, account="spare",
          last_active=now - 3 * 86400)
    trB = transcript(CLAUDE_C, B, CHAT_C, "second ▸ chat")
    transcript(CLAUDE_C, A, CHAT_C, "first chat")
    write_transcript(os.path.join(HIST_C, C + ".jsonl"), CHAT_C, "third chat",
                     pad=500)           # ~60 KB, and the only copy there is
    rc, out, err = ccli("history")
    rows = rows_of(out)
    check("one row per chat, newest first: index, short id, status, age, name, "
          "account, title -- the cursor glyph in a title made '>'",
          rc == 0 and rows == [
              ["0", A[:8], "interrupted", "1m", "alpha", "work", "first chat"],
              ["1", B[:8], "closed", "2h", "beta", "default", "second > chat"],
              ["2", C[:8], "interrupted", "3d", "gamma", "spare", "third chat"]],
          ascii(rows or out + err))
    check("...in the spec's columns",
          out.splitlines()[:1]
          == ["  0  abcd1111  interrupted   1m  alpha  work     first chat"],
          ascii(out.splitlines()[:1]))
    check("...under where it keeps them, how many, and how much exists "
          "nowhere else", f"{HIST_C} · 3 chats · 0.1 MB kept only here"
          in out, ascii(out))
    check("...that with no daemon, liveness came from the terminals' own files, "
          "and no colour on a pipe", "(daemon not running: liveness from the "
          "terminals' context files)" in out and "\x1b[" not in out)
    ctx_file(A, os.getpid())
    rc, out, err = ccli("history")
    check("a chat whose wrapper is alive and has written its context lately "
          "is open", row_for(out, A)[2:3] == ["open"], ascii(out))
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    ctx_file(A, dead.pid)
    rc, out, err = ccli("history")
    ok_dead = row_for(out, A)[2:3] == ["interrupted"]
    ctx_file(A, os.getpid(), age=1000.0)
    rc, out, err = ccli("history")
    check("...not when that process is gone, or its file has gone stale",
          ok_dead and row_for(out, A)[2:3] == ["interrupted"], ascii(out))
    ctx_file(A, os.getpid())
    rc, out, err = ccli("history", "rm", "0", stdin=A[:8] + "\n")
    check("rm refuses an open chat, even with its id typed back: it would come "
          "back on its next turn", rc != 0 and "still open" in out + err
          and kept_c(A), ascii(out + err))
    rc, out, err = ccli("history", "rm", "abcd")
    check("an id prefix two chats share is refused, naming both",
          rc != 0 and A[:8] in err and B[:8] in err and kept_c(A)
          and kept_c(B), ascii(err))
    rc, out, err = ccli("history", "rm", "ab")
    check("...and one under four characters is no id at all",
          rc != 0 and kept_c(A) and kept_c(B), ascii(err))
    rc, out, err = ccli("history", "rm", "1234", stdin="12345679\n")
    check("an all-digit argument past the list's end is an id prefix; a wrong "
          "id typed back deletes nothing",
          rc == 1 and C[:8] in out and "not deleted." in out and kept_c(C)
          and os.path.exists(os.path.join(HIST_C, C + ".jsonl")),
          ascii(out + err))
    check("...and the question says when the kept transcript is the ONLY copy",
          "this is the only copy of the conversation — there is no undo"
          in out, ascii(out))
    rc, out, err = ccli("history", "rm", "1234")
    check("...end of input at the prompt is 130, and deletes nothing",
          rc == 130 and kept_c(C))
    rc, out, err = ccli("history", "rm", "1234", stdin=C[:8] + "\n")
    check("...its short id typed back forgets the record AND its kept transcript",
          rc == 0 and not kept_c(C)
          and not os.path.exists(os.path.join(HIST_C, C + ".jsonl")),
          ascii(out + err))
    rc, out, err = ccli("history", "delete", B[:8], stdin=B[:8] + "\n")
    check("rm|remove|delete: Claude Code's own copy is said to stay, and stays",
          rc == 0 and not kept_c(B) and os.path.isfile(trB)
          and "Claude Code keeps its own copy of the transcript" in out,
          ascii(out + err))
    rc, out, err = ccli("history", "remove")
    check("no chat named, and no terminal for a chooser: a hint, not a guess",
          rc != 0 and "which chat" in err, ascii(err))
    rc, out, err = ccli("history", "resume", "0")
    check("`history resume` refuses with no terminal on both ends",
          rc != 0 and "terminal on both ends" in err, ascii(err))
    D1 = "dddd1111-4444-4444-8444-444444444444"
    D2 = "dddd2222-5555-4555-8555-555555555555"
    plant(HOME_C, D1, name="delta")
    plant(HOME_C, D2, name="delta")
    rc, out, err = ccli("history", "clear", stdin="yes\n")
    check("clear without the word typed: nothing deleted",
          rc == 1 and "not deleted." in out and kept_c(D1) and kept_c(D2),
          ascii(out + err))
    rc, out, err = ccli("history", "clear", stdin="clear\n")
    check("...with it: every chat that is not open is forgotten, the open one "
          "is kept and said to be", rc == 0 and not kept_c(D1)
          and not kept_c(D2) and kept_c(A) and "1 open chat stay" in out,
          ascii(out + err))
    rc, out, err = ccli("history", "clear")
    check("...and with only open chats left there is nothing to clear",
          rc == 0 and "nothing to clear" in out and kept_c(A), ascii(out + err))
    E = "eeee1111-6666-4666-8666-666666666666"
    weird = "/tmp/caf\udce9 dir"
    plant(HOME_C, E, name="caf\udce9", cwd=weird, last_active=time.time())
    transcript(CLAUDE_C, E, weird, "\x1b[31mred\x1b[0m alert")
    utf8 = {"LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}
    rc, out, err = ccli("history", env_extra=utf8)
    rc2, out2, err2 = ccli("history", "rm", E[:8], env_extra=utf8)
    check("a directory that is not UTF-8 and an ESC in a title: printed "
          "escaped, under a UTF-8 locale, with no traceback",
          rc == 0 and rc2 == 130 and E[:8] in out and "\\xdce9" in out2
          and "Traceback" not in err + err2 and "\x1b" not in out + out2,
          ascii((out2, err + err2))[:300])
    # A PIPE GETS EVERY CHARACTER, as the manual says: the 24- and 16-column
    # caps on a name and an account are for a terminal's width, and a script
    # matching on a long project name got it cut, with an ellipsis.
    HOME_P = os.path.join(tmp, "home-pipe")
    os.makedirs(HOME_P)
    LONG_NAME = "a-forty-character-project-directory-name"
    LONG_ACCT = "an-account-name-longer-than-16"
    plant(HOME_P, cid("pipe-long"), name=LONG_NAME, account=LONG_ACCT,
          last_active=time.time() - 60)
    plant(HOME_P, cid("pipe-short"), name="short", account="acct",
          last_active=time.time() - 120)
    rc, out, err = cli("history", home=HOME_P, tmpdir=TMPD_C)
    rows_p = out.splitlines()[:2]
    check("on a pipe the list cuts nothing: a 40-character name and a "
          "30-character account come out whole, and the columns still line up",
          rc == 0 and len(LONG_NAME) == 40 and len(rows_p) == 2
          and LONG_NAME in rows_p[0] and LONG_ACCT in rows_p[0]
          and "…" not in out
          and rows_p[0].find(LONG_ACCT) == rows_p[1].find("acct") > 0,
          ascii(out + err))
finally:
    for _p in procs:
        stop_daemon(_p)
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("history: OK")
