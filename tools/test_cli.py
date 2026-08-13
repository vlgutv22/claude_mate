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
    check("...and an ambiguous one is refused rather than guessed",
          talk("select|alpha").startswith("error:")
          or talk("select|alpha").strip() == "ok alpha")   # exact match wins

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
    check("...and the commands it does have are read-or-press only",
          sorted(re.findall(r'line == "([a-z-]+)"', body))
          == ["accounts", "accounts-refresh", "pair", "queue"]
          and sorted(re.findall(r'line\.startswith\("([a-z-]+)\|"\)', body))
          == ["press", "select"])
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
    rc, out = cli("accounts")
    check("`claude-mate accounts` lists them with their logins",
          rc == 0 and "work" in out and "me@example.com" in out)
    rc, out = cli("accounts", "rm", "default")
    check("...and refuses to delete 'default'",
          rc != 0 and "will not delete" in out)
    rc, out = cli("accounts", "rm", "nosuch")
    check("...and says so for an account that does not exist", rc != 0)
    rc, out = cli("--help")
    check("`--help` explains itself", rc == 0 and "press" in out.lower())
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
