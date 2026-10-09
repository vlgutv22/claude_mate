#!/usr/bin/env python3
"""The terminal CLI, against a real daemon.

`claude-mate` exists so a terminal can do what the device does, and the whole
design rests on one claim: it presses the SAME buttons. `press|G` is handed to
the ButtonReader that handles `B|G` off the wire, so there is one implementation
of GO and two ways to reach it. These tests drive the socket the CLI drives and
check that claim holds -- that the queue it prints is the queue the device is
shown, in the same order, and that a press moves the same selection.

The other half is what the CLI must NOT do over that socket. It is chmod 0666,
because hooks in any of the user's shells post session updates to it, so every
local process can write to it. Reading state and pressing buttons survive a
stray write; deleting an account does not, and the test pins that the socket has
no way to ask for one.

No device and no hardware: a PTY stands in for the USB link, and sessions arrive
the way hooks send them.
"""
import json
import os
import pty
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tty

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DAEMON = os.path.join(ROOT, "daemon", "claude_mate_daemon.py")
CLI = os.path.join(ROOT, "bin", "claude-mate")

failures = []
checks = 0


def check(name, ok):
    global checks
    checks += 1
    print(f"   {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        failures.append(name)


def wait_for(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


tmp = tempfile.mkdtemp(prefix="cm-cli-")
SOCK = os.path.join(tmp, "cm.sock")
ACCTS = os.path.join(tmp, "accounts")
os.makedirs(os.path.join(ACCTS, "work"))
os.makedirs(os.path.join(ACCTS, "spare"))
with open(os.path.join(ACCTS, "work", ".claude.json"), "w") as fh:
    json.dump({"oauthAccount": {"emailAddress": "me@example.com"}}, fh)

master_fd, slave_fd = pty.openpty()
tty.setraw(master_fd)


def pty_drain():
    while True:
        try:
            if not os.read(master_fd, 4096):
                return
        except OSError:
            return


threading.Thread(target=pty_drain, daemon=True).start()

env = dict(os.environ)
env.update({
    "CLAUDE_MATE_SOCK": SOCK,
    "CLAUDE_MATE_PORT": os.ttyname(slave_fd),
    "CLAUDE_MATE_ACCOUNTS_DIR": ACCTS,
    "PYTHONUNBUFFERED": "1",
})
env.pop("CLAUDE_MATE_TCP", None)
env.pop("CLAUDE_MATE_BLE", None)

print("== starting a daemon (PTY device, no radios) ==")
proc = subprocess.Popen([sys.executable, DAEMON], env=env, cwd=ROOT,
                        stderr=subprocess.PIPE, text=True)
errs = []
threading.Thread(target=lambda: [errs.append(l) for l in proc.stderr],
                 daemon=True).start()


def talk(line, timeout=6.0):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(timeout)
    c.connect(SOCK)
    c.sendall((line + "\n").encode())
    c.shutdown(socket.SHUT_WR)
    buf = b""
    while True:
        chunk = c.recv(65536)
        if not chunk:
            break
        buf += chunk
    c.close()
    return buf.decode()


def feed(line):
    for _ in range(60):
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(SOCK)
            c.sendall((line + "\n").encode())
            c.close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("socket never came up")


def cli(*args):
    r = subprocess.run([sys.executable, CLI, *args], env=env, cwd=ROOT,
                       capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout + r.stderr


try:
    # Two sessions, in an order the alphabetical queue will REVERSE, so a CLI
    # that printed arrival order instead of queue order fails here.
    feed("working|s-zeta|zeta")
    feed("waiting|s-alpha|alpha")
    ok = wait_for(lambda: len(json.loads(talk("queue"))["queue"]) == 2)
    check("the daemon answers `queue` with the sessions it knows", ok)

    snap = json.loads(talk("queue"))
    names = [s["name"] for s in snap["queue"]]
    check("...in the queue's own stable alphabetical order, not arrival order",
          names == ["alpha", "zeta"])
    check("...carrying the fields the glass shows",
          all(k in snap["queue"][0]
              for k in ("state", "secs", "acked", "shown", "i")))
    check("...and an alert arrives unacknowledged, as it does on the device",
          any(not s["acked"] for s in snap["queue"]))
    # ALWAYS PRESENT, so a client can iterate it without asking first. A hook
    # line carries no workflows, so here every list is empty.
    check("...and a 'workflows' list on every entry, [] when it has none",
          all(s.get("workflows") == [] for s in snap["queue"]))

    # --- pressing the same buttons ------------------------------------------ #
    print("\n== a press from the terminal is a press ==")
    before = json.loads(talk("queue"))["shown"]
    check("...`press|N` is accepted", talk("press|N").strip() == "ok")
    moved = wait_for(lambda: json.loads(talk("queue"))["shown"] != before, 5.0)
    check("...and moves the selection the device would have moved", moved)

    check("FOLLOW is off to begin with",
          json.loads(talk("queue"))["follow"] is False)
    talk("press|F")
    check("...and `press|F` toggles the same FOLLOW the device toggles",
          wait_for(lambda: json.loads(talk("queue"))["follow"] is True, 5.0))
    talk("press|F")

    check("an unknown press is refused rather than guessed at",
          talk("press|").startswith("error:"))

    # --- select --------------------------------------------------------------#
    print("\n== select points at a row instead of stepping to it ==")
    check("by index", talk("select|0").strip() == "ok alpha")
    check("by name", talk("select|zeta").strip() == "ok zeta")
    check("by unique prefix", talk("select|al").strip() == "ok alpha")
    check("a name that matches nothing is an error, not a silent no-op",
          talk("select|nope").startswith("error:"))
    # AMBIGUITY IS A REFUSAL. This moves what GO acts on, and raising the wrong
    # window is the one mistake the feature must not make.
    feed("idle|s-alpha2|alpha-two")
    wait_for(lambda: len(json.loads(talk("queue"))["queue"]) == 3)
    # "alpha" is an EXACT name, so it must resolve -- that is not the ambiguous
    # case. As first written this check accepted either answer, which made it
    # unfailable: an `or` between "refused" and "resolved" covers every possible
    # reply. The ambiguous case needs a prefix that matches BOTH sessions.
    check("...an exact name still wins over a longer one sharing its prefix",
          talk("select|alpha").strip() == "ok alpha")
    check("...and a prefix matching two sessions is refused, not guessed",
          talk("select|alph").startswith("error:"))

    # --- accounts ----------------------------------------------------------- #
    print("\n== accounts ==")
    accts = json.loads(talk("accounts"))["accounts"]
    byname = {a["name"]: a for a in accts}
    check("the saved logins are listed", {"work", "spare"} <= set(byname))
    check("...'default' among them, since it is a login you can switch to",
          "default" in byname)
    check("...but marked unremovable: it is ~/.claude, not a profile",
          byname["default"]["removable"] is False)
    check("...and each carries the DIR the daemon means",
          byname["work"]["dir"] == os.path.join(ACCTS, "work"))

    # THE WHOLE POINT OF NOT PUTTING DELETION HERE. This socket is world
    # writable, so anything destructive on it would be destructive by any local
    # process. The CLI deletes in its own process instead.
    #
    # Checked against the SOURCE rather than by firing hopeful commands at the
    # socket: an unrecognised line there is not rejected, it is parsed as a
    # session update -- so `talk("wipe")` does not prove a wipe is impossible,
    # it just invents a session called "" in state "wipe". (Which is how the
    # first version of this test hung: it polluted the queue it went on to
    # assert about.)
    print("\n== nothing destructive is reachable over a 0666 socket ==")
    daemon_src = open(DAEMON, encoding="utf-8").read()
    start = daemon_src.index("def _process_line")
    body = daemon_src[start:daemon_src.index("\n    def ", start + 10)]
    check("the socket's command surface has no delete in it",
          not any(w in body for w in ("shutil.rmtree", "os.remove", "os.unlink",
                                      "os.rmdir")))
    # THIS LIST IS A LOCK, NOT AN OBSERVATION. Adding a verb should mean coming
    # here and saying why it is safe on a socket every one of the user's shells
    # can write to. `link` is here because it retunes the device's radio and
    # nothing else: it takes three fixed words, builds the `I|` line itself, and
    # deliberately cannot be talked into forwarding an arbitrary console line --
    # the same console also takes `T|<token>`, so a general relay would have
    # handed the device's shared secret to anything that could open the socket.
    check("...and the commands it does have are read-or-press only",
          sorted(re.findall(r'line == "([a-z-]+)"', body))
          == ["accounts", "accounts-refresh", "pair", "queue"]
          and sorted(re.findall(r'line\.startswith\("([a-z-]+)\|"\)', body))
          == ["link", "press", "select"])

    # The narrowness above is the security property, so pin it by behaviour too
    # and not only by reading the source.
    check("link| refuses a transport it does not know",
          talk("link|telepathy").startswith("error:"))
    check("...and refuses a console line dressed up as one",
          talk("link|T|stolen-token").startswith("error:"))
    for mode in ("wifi", "p2p", "ble"):
        check(f"...but accepts {mode}", not talk(f"link|{mode}")
              .startswith("error: link|"))
    # 0600, and this check used to assert 0666 -- it pinned the over-permission
    # as though it were a requirement. Hooks run in the user's own shells, same
    # uid, so owner-only always sufficed; 0666 only stopped being free when this
    # surface grew verbs that can type into a live session.
    check("...and the socket is owner-only, not world-writable",
          (os.stat(SOCK).st_mode & 0o777) == 0o600)

    # --- the CLI itself ----------------------------------------------------- #
    print("\n== the command a person actually types ==")
    rc, out = cli()
    check("`claude-mate` prints the queue", rc == 0 and "alpha" in out
          and "zeta" in out)
    check("...marking the row that is on the glass", ">" in out)

    # A dynamic workflow, the way the PTY wrapper reports one: field 8 of an
    # ordinary state line. The device shows it as a band; the terminal has the
    # room to print it in words, under the session it belongs to.
    feed("working|s-zeta|zeta||||||R;2;5;;40;75;542800;;deploy-sweep")
    ok = wait_for(lambda: any(
        s["workflows"] for s in json.loads(talk("queue"))["queue"]), 5.0)
    zeta = {s["name"]: s for s in json.loads(talk("queue"))["queue"]}["zeta"]
    check("a wrapper line's workflow reaches the queue JSON",
          ok and [w["name"] for w in zeta["workflows"]] == ["deploy-sweep"]
          and zeta["workflows"][0]["status"] == "running"
          and (zeta["workflows"][0]["done"], zeta["workflows"][0]["total"])
          == (2, 5))
    rc, out = cli("status")
    lines = out.splitlines()
    row = next((i for i, l in enumerate(lines)
                if "zeta" in l and "workflow" not in l), -1)
    sub = lines[row + 1] if 0 <= row < len(lines) - 1 else ""
    check("`claude-mate status` prints it on the line under its session",
          rc == 0 and "workflow deploy-sweep" in sub and "running" in sub
          and "2/5" in sub and "543k tokens" in sub)
    # The run's own clock, which carries on through every state change, in
    # Claude's units like the band -- never m:ss, which is r1's time in STATE
    # (the session row above it) and turns a ten-hour run into '622:55'.
    check("...its clock in Claude's units (1m15s), as the device's band "
          "writes it, not m:ss", bool(re.search(r"\b1m[12]\ds\b", sub))
          and not re.search(r"\b\d+:\d\d\b", sub))
    check("...indented, so it reads as part of that session",
          sub.startswith("        "))
    check("...and with no ANSI colour on a pipe", "\x1b[" not in out)
    rc, out = cli("accounts")
    check("`claude-mate accounts` lists them with their logins",
          rc == 0 and "work" in out and "me@example.com" in out)
    rc, out = cli("accounts", "rm", "default")
    check("...and refuses to delete 'default'",
          rc != 0 and "will not delete" in out)
    rc, out = cli("accounts", "rm", "nosuch")
    check("...and says so for an account that does not exist", rc != 0)
    rc, out = cli("link")
    check("`claude-mate link` explains the transports",
          rc == 0 and "p2p" in out and "wifi" in out and "ble" in out)
    rc, out = cli("link", "telepathy")
    check("...and rejects one that does not exist", rc != 0)
    rc, out = cli("--help")
    check("`--help` explains itself", rc == 0 and "press" in out.lower())

    # --- a failed start says WHY --------------------------------------------- #
    # THE REGRESSION THIS EXISTS FOR. On a real machine the daemon crash-looped
    # 1786 times on a one-line ModuleNotFoundError while `claude-mate daemon`
    # said only "nothing is answering on the socket yet" and pointed at the log
    # the answer was already sitting in. These are the two functions that turn
    # that into a message, so they are worth pinning even though the paths that
    # call them need a broken launchd job to reach.
    print("\n== a daemon that will not start explains itself ==")
    import importlib.machinery
    import importlib.util
    _ldr = importlib.machinery.SourceFileLoader("cm_cli_under_test", CLI)
    cm = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("cm_cli_under_test", _ldr))
    _ldr.exec_module(cm)

    crash = os.path.join(tmp, "crash.log")
    with open(crash, "w") as fh:
        fh.write("[claude-mate] starting\n"
                 "Traceback (most recent call last):\n"
                 '  File "daemon/claude_mate_daemon.py", line 95, in <module>\n'
                 "    import serial  # pyserial\n"
                 "ModuleNotFoundError: No module named 'serial'\n")
    said = cm.why_the_daemon_died(crash) or ""
    check("the traceback's exception reaches the message",
          "ModuleNotFoundError" in said and "serial" in said)
    check("...with the frames that led to it, not just the last line",
          "Traceback (most recent call last)" in said)

    plain = os.path.join(tmp, "plain.log")
    with open(plain, "w") as fh:
        fh.write("[claude-mate] ERROR: could not bind 8787\n"
                 "[claude-mate] running.\n")
    check("...and an ERROR line is found when there is no traceback",
          "could not bind 8787" in (cm.why_the_daemon_died(plain) or ""))
    check("...while a log that is not there is not an exception",
          cm.why_the_daemon_died(os.path.join(tmp, "absent.log")) is None)
    check("...nor is an empty one",
          cm.why_the_daemon_died(os.devnull) is None)

    # A CLI newer than the daemon it talks to: the queue has no 'workflows' key
    # at all. It must print the queue it was given, not a traceback.
    import contextlib
    import io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cm.render({"queue": [{"i": 0, "key": "k", "name": "elder",
                                  "state": "idle", "secs": 3, "model": "",
                                  "effort": "", "account": "", "limit": "",
                                  "cwd": "", "acked": True, "shown": True}],
                       "follow": False})
        rendered = buf.getvalue()
    except Exception as exc:                  # noqa: BLE001
        rendered = f"raised {exc!r}"
    check("render() prints an older daemon's queue (no 'workflows' key)",
          "elder" in rendered and "workflow" not in rendered)

    # ONE READING OF A RUN, on the glass and in the terminal. The CLI's clock
    # and token helpers are copies of the daemon's -- it cannot import a module
    # that needs pyserial -- so here they are held to the originals, at every
    # boundary either one has. (getattr: a CLI without them fails these checks,
    # not the rest of the file.)
    print("\n== a run's clock and tokens, written as the device's band does ==")
    _dspec = importlib.util.spec_from_file_location("cm_cli_daemon", DAEMON)
    dmod = importlib.util.module_from_spec(_dspec)
    sys.modules["cm_cli_daemon"] = dmod      # @dataclass resolves through it
    _dspec.loader.exec_module(dmod)
    clk, tok = getattr(cm, "wf_clock", None), getattr(cm, "wf_tokens", None)
    check("the run's clock: 45s, 2m39s, 1h02m, 1d03h -- Claude's units",
          clk is not None and [clk(s) for s in (45, 159, 3720, 97200)]
          == ["45s", "2m39s", "1h02m", "1d03h"])
    secs = sorted(set(list(range(0, 7300)) + list(range(0, 400000, 59))
                      + [86399, 86400, 97200, 10 ** 7, 10 ** 9]))
    check("...exactly the daemon's, at every boundary",
          clk is not None and all(clk(s) == dmod._wf_clock(s) for s in secs))
    check("tokens compact: 999, 1.2k, 543k (never 542.8k), 1.0M (never 1000k)",
          tok is not None and [tok(n) for n in (999, 1240, 542800, 999950)]
          == ["999", "1.2k", "543k", "1.0M"])
    toks = sorted(set([0, 999, 1000, 9949, 9950, 99_950, 542_800, 999_499,
                       999_950, 1_400_000, 9_960_000, 12_000_000, 999_600_000,
                       1_200_000_000, 10 ** 13]
                      + list(range(0, 3_000_000, 977))
                      + list(range(990_000_000, 1_010_000_000, 99_991))))
    check("...exactly the daemon's, at every boundary",
          tok is not None and all(tok(n) == dmod._wf_tokens(n) for n in toks))

    # The one crash launchd cannot narrate: no interpreter, so nothing is ever
    # written to the log why_the_daemon_died() would read.
    good = os.path.join(tmp, "good.plist")
    with open(good, "wb") as fh:
        import plistlib
        plistlib.dump({"ProgramArguments": ["/usr/bin/true", "x.py"]}, fh)
    check("the LaunchAgent's interpreter is readable",
          cm.daemon_interpreter(good) == "/usr/bin/true")
    check("...and an unreadable plist is None, not a crash",
          cm.daemon_interpreter(os.path.join(tmp, "absent.plist")) is None)
    rc, out = cli("nonsense")
    check("an unknown command is an error with a hint", rc != 0 and "help" in out)

    # A destructive command that is NOT confirmed must not delete.
    r = subprocess.run([sys.executable, CLI, "accounts", "rm", "spare"],
                       env=env, cwd=ROOT, input="wrong-name\n",
                       capture_output=True, text=True, timeout=30)
    check("`accounts rm` without the typed name deletes nothing",
          os.path.isdir(os.path.join(ACCTS, "spare"))
          and "not deleted" in r.stdout)
    r = subprocess.run([sys.executable, CLI, "accounts", "rm", "spare"],
                       env=env, cwd=ROOT, input="spare\n",
                       capture_output=True, text=True, timeout=30)
    check("...and deletes it when the name is typed back",
          not os.path.exists(os.path.join(ACCTS, "spare")))
    # --- the manual, pinned to the code ------------------------------------ #
    # Docs rot silently, and this set in particular: a command added to the CLI
    # and not to the manual is a command nobody finds, and one removed from the
    # CLI but left in the manual is worse -- someone types it and it fails.
    print("\n== the manual still describes what exists ==")
    using = open(os.path.join(ROOT, "docs", "USING.md"), encoding="utf-8").read()
    # The README carries the command list TOO, because it is the front door and
    # a list you have to follow a link to find is a list nobody finds. Two copies
    # is a rot risk taken deliberately -- so both are pinned, not just the one I
    # happened to write first.
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    cli_src = open(CLI, encoding="utf-8").read()
    verbs = sorted(re.findall(r'"([a-z-]+)": "[PNGKFMCT]"', cli_src))
    for doc, name in ((using, "docs/USING.md"), (readme, "README.md")):
        missing = [v for v in verbs
                   if f"`{v}`" not in doc and f"`claude-mate {v}`" not in doc
                   and f" {v} " not in doc]
        check(f"every button command is in {name} ({len(verbs)} of them)",
              not missing)
    check("...and the README lists the sibling commands beside it",
          "claude-mate-connect" in readme and "claude-mate-switch" in readme)
    check("...and puts the install where a reader lands, not below the fold",
          readme.index("./install/install.sh --yes")
          < readme.index("## What it is"))

    # THE SOCKET MODE, IN THE CODE AND IN EVERY PLACE THAT DESCRIBES IT. Three
    # documents explained the 0666 permission and the reasoning built on it, and
    # when the code became 0600 all three went on asserting the old value as
    # current -- including the CLI's own docstring. A number that appears in
    # prose four times and in code once is a number that will drift.
    daemon_src = open(DAEMON, encoding="utf-8").read()
    mode = re.search(r"os\.chmod\(self\._sock_path, (0o\d+)\)", daemon_src)
    check("the daemon's socket mode is discoverable and owner-only",
          mode and mode.group(1) == "0o600")
    stale = []
    for name, text in (("README.md", readme), ("docs/USING.md", using),
                       ("bin/claude-mate", cli_src)):
        # "is chmod 0666" as a CURRENT claim. Saying it *was* 0666 is history.
        if re.search(r"(is|are)\s+`?chmod 0666", text):
            stale.append(name)
    check("...and nothing still describes it as world-writable", not stale)
    for extra in ("select", "accounts", "watch"):
        check(f"...and `{extra}`", extra in using)
    check("...as is the one-command install, verbatim",
          "./install/install.sh --yes" in using)
    check("...and the installer really takes that flag",
          "--yes" in open(os.path.join(ROOT, "install", "install.sh"),
                          encoding="utf-8").read())
    # The claim the whole design rests on, in the docs as well as the code.
    check("...and the manual says a press is the same press",
          "same" in using and "ButtonReader" in using)

finally:
    # TEARDOWN IS BEST-EFFORT AND MUST NOT BE ABLE TO HANG. Every check above can
    # pass and the run still never finish: the daemon is being asked to stop
    # while it holds a PTY nobody is draining any more, and a test that wedges
    # after its last assertion looks exactly like a test that failed.
    print("   (stopping the daemon)")
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        print("   (it did not stop; killing)")
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    # NOT os.close(master_fd): a thread is blocked reading it, and closing an fd
    # out from under a blocked reader is not something to rely on. Process exit
    # closes it, and the reader is a daemon thread either way.
    print("   (cleaning up)")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("cli: OK")
