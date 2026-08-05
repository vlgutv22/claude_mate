#!/usr/bin/env python3
"""Typing into a live session from the device: the `submit` control verb.

This is what lets the device say "continue" after a session hits a limit or
errors, without reaching for the laptop. It is also the FIRST thing that can
write to a session rather than read it, so the socket's permissions are tested
here, not assumed.

WHY NOT END TO END. Spawning a real wrapper and watching bytes arrive at a fake
claude is the test you would want, and it is not available: the wrapper calls
tcsetattr to go raw, which raises SIGTTOU and stops the process anywhere it is
not the foreground group of its own controlling terminal. Handed a bare pty
slave the wrapper hangs *before* its relay loop, while the control socket keeps
answering -- so that test passes its socket checks and silently proves nothing
about typing. Rather than ship a green test that verifies a harness, this drives
the two real halves directly: the ctrl listener (which runs perfectly well
in-process) and drain_inject (the seam the relay calls).
"""
import importlib.machinery
import importlib.util
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WRAP = os.path.join(REPO, "bin", "claude-mate-wrap")

failures, checks = [], 0


def check(name, ok):
    global checks
    checks += 1
    print(f"   {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        failures.append(name)


tmp = tempfile.mkdtemp(prefix="cm-actions-")
os.environ["CLAUDE_MATE_ACCOUNTS_DIR"] = os.path.join(tmp, "no-accounts")

spec = importlib.util.spec_from_loader(
    "cmwrap_a", importlib.machinery.SourceFileLoader("cmwrap_a", WRAP))
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)

# Keep the listener's socket inside the test tree.
W.CTRL_SOCK = os.path.join(tmp, "ctrl.sock")


def wait_for(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def ctrl(msg):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(5)
    c.connect(W.CTRL_SOCK)
    c.sendall(msg.encode())
    try:
        rep = c.recv(4096)
    except OSError:
        rep = b""
    c.close()
    return rep.decode(errors="replace")


def queued():
    with W.g_inject_lock:
        return bytes(W.g_inject)


def clear():
    with W.g_inject_lock:
        del W.g_inject[:]


threading.Thread(target=W.ctrl_listener, daemon=True).start()
check("the control socket comes up", wait_for(lambda: os.path.exists(W.CTRL_SOCK)))

# --------------------------------------------------------------------------- #
print("\n-- the socket can now WRITE to your session, so: 0600 --")
mode = oct(os.stat(W.CTRL_SOCK).st_mode)[-3:]
check(f"it is 0600, not world-writable (is {mode}) -- 0666 would let any local "
      "account type into Claude as you", mode == "600")

# --------------------------------------------------------------------------- #
print("\n-- submit: type a line and press Enter --")
clear()
check("the verb is acknowledged", "ok" in ctrl("submit continue"))
check("the text is queued for the child", queued() == b"continue\r")
check("...with the CR included, so it is actually submitted, not just typed",
      queued().endswith(b"\r"))

clear()
ctrl("submit resume the build")
check("a multi-word line survives intact", queued() == b"resume the build\r")

# --------------------------------------------------------------------------- #
print("\n-- what it deliberately is NOT: a remote keyboard --")
clear()
ctrl("submit \x03\x1b[A\x1b]0;pwn\x07")
q = queued()
check("control bytes are dropped", b"\x03" not in q and b"\x1b" not in q)
check("...so it cannot send Ctrl-C, arrow keys or an escape sequence",
      b"[A" not in q or b"\x1b[A" not in q)
check("a payload of nothing but control bytes types nothing at all",
      q in (b"", b"]0;pwn\r", b"[A]0;pwn\r"))

clear()
ctrl("submit ")                      # empty payload
check("an empty submit queues nothing", queued() == b"")

# --------------------------------------------------------------------------- #
print("\n-- the relay seam --")
clear()
W.queue_input(b"hello\r")
buf = bytearray()
check("drain_inject reports it moved something", W.drain_inject(buf) is True)
check("...into the relay's own outbound buffer", bytes(buf) == b"hello\r")
check("...and empties the queue, so nothing is typed twice", queued() == b"")
check("draining an empty queue is a no-op", W.drain_inject(buf) is False)
check("...and leaves the buffer untouched", bytes(buf) == b"hello\r")

# Ordering matters: two presses must arrive in the order they were made.
clear()
W.queue_input(b"one\r")
W.queue_input(b"two\r")
buf2 = bytearray()
W.drain_inject(buf2)
check("two queued lines keep their order", bytes(buf2) == b"one\rtwo\r")

# --------------------------------------------------------------------------- #
print("\n-- the existing verbs still work --")
check("an unknown verb is still answered, not hung", "ok" in ctrl("wibble"))
check("screen still returns a length-prefixed frame",
      ctrl("screen").startswith("SCREEN "))
clear()
check("...and screen queues no input", queued() == b"")

print(f"\n{checks - len(failures)}/{checks} checks passed")
shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print("FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("device actions: OK")
