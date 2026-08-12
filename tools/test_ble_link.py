#!/usr/bin/env python3
"""The BLE transport, on both sides of a contract neither side can see.

The daemon's BLE central and the firmware's BLE peripheral agree on four
strings -- a service UUID, two characteristic UUIDs and a device name -- that
live in two files, in two languages, that no compiler or linter reads together.
Get one wrong and NOTHING says so: the device advertises forever, the daemon
scans forever, and both look healthy. That failure is the reason this file
starts with a contract check rather than a behaviour one.

The rest drives the real `BleLink` against a fake `bleak` -- a stub module
injected into sys.modules before the lazy import runs -- because the parts worth
testing are the parts that are not about radios:

  * the nonce/HMAC handshake, including both ways of failing it, which is the
    only thing standing between "in radio range" and "may drive this device";
  * line reassembly out of notifications, where a notification boundary is NOT
    a line boundary -- the device can pack two lines into one notification and
    split one line across two, and a reader that assumes otherwise corrupts the
    mirror in a way that looks like a rendering bug;
  * that handshake lines are consumed by the handshake and never leak up to the
    daemon's ButtonReader as unknown verbs.

Finally a few static assertions about the firmware, guarding the invariants that
the menu rework depends on: ONE gamepad row, persisted, and a config verb that
does not collide with the protocol's.
"""
import hmac
import os
import queue
import re
import subprocess
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DAEMON_DIR = os.path.join(ROOT, "daemon")
FW = os.path.join(ROOT, "firmware", "claude_mate_s3")
INO = os.path.join(FW, "claude_mate_s3.ino")

failures = []
checks = 0


def check(name, ok):
    global checks
    checks += 1
    print(f"   {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        failures.append(name)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# 1. The contract: the two halves must name the same service.
# --------------------------------------------------------------------------- #
print("== the firmware and the daemon agree on the service ==")

fw_ble = read(os.path.join(FW, "blelink.h"))
py_ble = read(os.path.join(DAEMON_DIR, "blelink.py"))
ino = read(INO)


def fw_define(name, src=None):
    m = re.search(rf'#define\s+{name}\s+"([^"]+)"', src if src else fw_ble)
    return m.group(1) if m else None


def py_const(name):
    m = re.search(rf'^{name}\s*=\s*"([^"]+)"', py_ble, re.M)
    return m.group(1) if m else None


for fw_name, py_name in (("BLE_SVC_UUID", "BLE_SVC_UUID"),
                         ("BLE_RX_UUID", "BLE_RX_UUID"),
                         ("BLE_TX_UUID", "BLE_TX_UUID")):
    a, b = fw_define(fw_name), py_const(py_name)
    check(f"{fw_name} matches ({a})", a is not None and a == b)

# The advertised name is the sketch's, and it is deliberately the SAME name the
# HID gamepad advertises: one device, one entry in the Bluetooth pane, and never
# both roles at once.
check("the device name matches the sketch's DEVICE_BLE_NAME",
      fw_define("DEVICE_BLE_NAME", ino) == py_const("BLE_DEVICE_NAME"))
check("...and the HID gamepad advertises under that same name",
      f'blepad.begin(DEVICE_BLE_NAME)' in ino)

# The duty cycle from the issue. Asserted rather than admired: these two numbers
# are the entire power argument for this transport, and a later "just make it a
# bit snappier" that quietly halves the off time should have to change a test
# that says why it exists.
adv_on = re.search(r"#define\s+BLE_ADV_ON_MS\s+(\d+)", fw_ble)
adv_off = re.search(r"#define\s+BLE_ADV_OFF_MS\s+(\d+)", fw_ble)
check("advertising bursts are 200 ms", adv_on and adv_on.group(1) == "200")
check("...and the gap between them is 4 s",
      adv_off and adv_off.group(1) == "4000")
# The daemon's scan must outlast one whole silent stretch or it can land
# entirely inside the gap and report a device that is right there as absent.
scan = re.search(r"^BLE_SCAN_S\s*=\s*([\d.]+)", py_ble, re.M)
check("the daemon's scan window outlasts a full off-period",
      scan and float(scan.group(1)) > int(adv_off.group(1)) / 1000.0)


# --------------------------------------------------------------------------- #
# 2. A fake bleak, so the handshake and the framing can be driven for real.
# --------------------------------------------------------------------------- #
TOKEN = "a-shared-token"


class FakeAdv:
    def __init__(self, uuids):
        self.service_uuids = uuids


class FakeDevice:
    def __init__(self, address="00:11:22:33:44:55"):
        self.address = address


class FakeScanner:
    """Answers a scan with a device whose advertisement carries our UUID.

    The filter the daemon passes in is CALLED, not bypassed: a filter that looks
    at the wrong field would otherwise pass this test and find nothing in a real
    room.
    """
    seen_filter = False

    @staticmethod
    async def find_device_by_filter(fn, timeout=0):
        FakeScanner.seen_filter = True
        adv = FakeAdv([FakeScanner.advertised])
        return FakeDevice() if fn(FakeDevice(), adv) else None

    @staticmethod
    async def find_device_by_address(address, timeout=0):
        return FakeDevice(address)


class FakeClient:
    """The device end: answers the challenge, records what it was sent.

    `token` is what THIS fake device believes the shared secret is, which is how
    the rejection paths are exercised -- a device with the wrong token is not a
    special code path on the daemon side, it is the ordinary one arriving at a
    different answer.
    """
    token = TOKEN
    mode = "normal"            # or "notoken"
    written = []               # every line the daemon sent us
    instances = []

    def __init__(self, device, disconnected_callback=None):
        self.device = device
        self._on_disc = disconnected_callback
        self._notify = None
        # The real client has this, and the session loop now asks it every tick
        # rather than trusting the disconnect callback -- see `vanish()`.
        self.is_connected = True
        FakeClient.instances.append(self)

    def vanish(self):
        """The device goes away WITHOUT the callback firing.

        Not a hypothetical: observed on hardware, where the board rebooted three
        times and the daemon went on believing it was connected for an hour and
        a half, writing frames into nothing and never scanning again. The
        callback is simply not something a transport can depend on.
        """
        self.is_connected = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def start_notify(self, uuid, cb):
        self._notify = cb

    async def write_gatt_char(self, uuid, data, response=False):
        for raw in data.decode("ascii").splitlines():
            if not raw:
                continue
            FakeClient.written.append(raw)
            if raw.startswith("C|"):
                if FakeClient.mode == "notoken":
                    self.notify("A|NOTOKEN")
                    continue
                mac = hmac.new(FakeClient.token.encode(), raw[2:].encode(),
                               "sha256").hexdigest()
                self.notify(f"A|{mac}")

    # ---- the device speaking, in whatever chunks we like ------------------- #
    def notify(self, text):
        self.raw(text.encode("ascii") + b"\n")

    def raw(self, data):
        if self._notify:
            self._notify(None, bytearray(data))


def install_fake_bleak(advertised):
    FakeScanner.advertised = advertised
    FakeScanner.seen_filter = False
    FakeClient.written = []
    FakeClient.instances = []
    mod = types.ModuleType("bleak")
    mod.BleakScanner = FakeScanner
    mod.BleakClient = FakeClient
    sys.modules["bleak"] = mod


def wait_for(pred, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


sys.path.insert(0, DAEMON_DIR)
install_fake_bleak(py_const("BLE_SVC_UUID"))
import blelink  # noqa: E402


def new_link(token=TOKEN):
    logs = []
    rx = queue.Queue()
    link = blelink.BleLink(token, rx, log=logs.append)
    return link, rx, logs


# --------------------------------------------------------------------------- #
print("\n== the handshake, and what it lets through ==")
FakeClient.token = TOKEN
FakeClient.mode = "normal"
link, rx, logs = new_link()
try:
    check("start() succeeds when bleak is importable", link.start() is True)
    check("the device is found and authenticated",
          wait_for(lambda: link.has_clients()))
    check("...by a filter that actually inspected the advertisement",
          FakeScanner.seen_filter)
    check("the daemon sent a fresh nonce challenge",
          any(l.startswith("C|") and len(l) == 34 for l in FakeClient.written))
    check("...and confirmed with A|OK", "A|OK" in FakeClient.written)

    # The handshake's own lines must never reach the daemon's dispatcher: an A|
    # arriving there is an unknown verb, logged as noise, while the handshake
    # sits waiting for the line it was already handed.
    check("no handshake line leaked onto the daemon's queue", rx.empty())

    dev = FakeClient.instances[-1]
    dev.notify("H")
    check("a line from the device arrives on the shared queue",
          wait_for(lambda: not rx.empty()) and rx.get() == "H")

    check("write_line() reaches the device",
          link.write_line("F|0|-1|api|WAIT 0:42|Opus 5|2/6") and
          wait_for(lambda: FakeClient.written[-1].startswith("F|")))

    print("\n== a notification boundary is not a line boundary ==")
    dev.raw(b"B|+P\nB|-P\n")            # two lines, one notification
    check("two lines in one notification arrive as two",
          wait_for(lambda: rx.qsize() >= 2) and
          [rx.get(), rx.get()] == ["B|+P", "B|-P"])
    dev.raw(b"B|+")                      # one line, two notifications
    dev.raw(b"G\n")
    check("one line split across two notifications arrives whole",
          wait_for(lambda: not rx.empty()) and rx.get() == "B|+G")

    print("\n== a device that vanishes WITHOUT the callback is still noticed ==")
    # The bug this guards: bleak's disconnected_callback did not fire when the
    # board rebooted, so the daemon believed it was connected for 90 minutes --
    # has_clients() true, frames written into nothing, and no scan ever started
    # again. Recovery cannot depend on one notification the transport does not
    # control, so the session loop asks is_connected every tick as well.
    dev.vanish()
    check("has_clients() goes false without any disconnect callback",
          wait_for(lambda: not link.has_clients(), 5.0))
    check("...and write_line() stops claiming to have sent anything",
          link.write_line("F|0|-1|x|y|z|w") is False)
    check("...and it goes back to scanning rather than sitting on a dead link",
          wait_for(lambda: len(FakeClient.instances) > 1, 8.0))
finally:
    link.stop()

# --------------------------------------------------------------------------- #
print("\n== a device with the wrong token never attaches ==")
install_fake_bleak(py_const("BLE_SVC_UUID"))
FakeClient.token = "not-the-same-token"
FakeClient.mode = "normal"
link, rx, logs = new_link()
try:
    link.start()
    check("the daemon says the token was rejected",
          wait_for(lambda: any("token rejected" in l for l in logs)))
    check("...and the link never comes up", not link.has_clients())
    check("...and it told the device so, rather than just hanging up",
          "A|NO" in FakeClient.written)
finally:
    link.stop()

# --------------------------------------------------------------------------- #
print("\n== a device with NO token gets the message that says what to do ==")
install_fake_bleak(py_const("BLE_SVC_UUID"))
FakeClient.token = TOKEN
FakeClient.mode = "notoken"
link, rx, logs = new_link()
try:
    link.start()
    check("A|NOTOKEN produces the actionable message, not 'bad handshake'",
          wait_for(lambda: any("HAS NO TOKEN" in l for l in logs)))
    check("...and the link never comes up", not link.has_clients())
finally:
    link.stop()

# --------------------------------------------------------------------------- #
print("\n== a device advertising something else is not ours ==")
install_fake_bleak("0000feed-0000-1000-8000-00805f9b34fb")
FakeClient.token = TOKEN
FakeClient.mode = "normal"
link, rx, logs = new_link()
try:
    link.start()
    time.sleep(1.0)
    check("a foreign service UUID is never connected to",
          not link.has_clients() and not FakeClient.written)
finally:
    link.stop()

# --------------------------------------------------------------------------- #
print("\n== bleak missing costs the BLE link and nothing else ==")
# `sys.modules["bleak"] = None` makes `import bleak` raise ImportError outright.
# Merely POPPING the stub is not enough and quietly stopped testing anything the
# day bleak got installed on the machine running this: the import fell straight
# through to the real package and start() succeeded. A test that passes because
# of what happens to be installed is not a test.
saved = sys.modules.get("bleak")
sys.modules["bleak"] = None
link, rx, logs = new_link()
check("start() returns False rather than raising", link.start() is False)
check("...and says how to fix it",
      any("pip install bleak" in l for l in logs))
if saved is not None:
    sys.modules["bleak"] = saved
else:
    del sys.modules["bleak"]

# --------------------------------------------------------------------------- #
print("\n== the daemon accepts --ble and survives having no token ==")
env = dict(os.environ)
env["CLAUDE_MATE_ACCOUNTS_DIR"] = "/nonexistent-claude-mate-accounts"
out = subprocess.run([sys.executable,
                      os.path.join(DAEMON_DIR, "claude_mate_daemon.py"),
                      "--help"],
                     capture_output=True, text=True, env=env, timeout=60)
check("--ble is a documented flag", "--ble" in out.stdout)
check("--ble-address is a documented flag", "--ble-address" in out.stdout)

# --------------------------------------------------------------------------- #
# 3. Firmware invariants the menu rework depends on.
# --------------------------------------------------------------------------- #
print("\n== the firmware has ONE gamepad row, and it remembers ==")

m = re.search(r"enum SetRow : uint8_t \{(.*?)\}", ino, re.S)
rows = [r.strip() for r in re.split(r"[,\s]+", m.group(1)) if r.strip()]
check("SR_BLE is gone -- the two rows became one",
      "SR_BLE" not in rows)
check("SR_PAD is still there, and is the gamepad row", "SR_PAD" in rows)
check("...and a Link row chooses the transport", "SR_LINK" in rows)
check('the row is labelled "BLE gamepad"',
      re.search(r'case SR_PAD:\s*\n\s*label = "BLE gamepad";', ino))
check('no row is labelled "Game controller" any more',
      not re.search(r'label\s*=\s*"Game controller"', ino))
check("the row shows on/off rather than an arrow",
      re.search(r'case SR_PAD:.*?value = cfg\.pad\(\) \? "on" : "off";',
                ino, re.S))

settings = read(os.path.join(FW, "settings.h"))
check("the gamepad is persisted to NVS", 'p.putBool("pad"' in settings)
check("...and read back at boot", 'p.getBool("pad"' in settings)
check("...and written EAGERLY, not on the deferred timer, so a power cut "
      "cannot leave the switch and the device disagreeing",
      re.search(r"void setPad\(bool on\) \{.*?write\(\);", settings, re.S))
check("boot restores the gamepad", re.search(
    r"if \(cfg\.pad\(\)\) enterPad\(true, PAD_BLE\);", ino))

# handleConfigLine() runs FIRST on USB lines, so a verb both switches claim is
# silently unreachable over the cable -- the exact bug X| once was.
# test_controller_mode.py asserts the two sets are disjoint; this asserts the
# new verb landed on the right side of that line.
def case_labels(src, signature):
    start = src.index(signature)
    i = src.index("{", start)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return set(re.findall(r"case\s+'(.)'\s*:", src[i:j]))


proto = case_labels(ino, "static void handleLine(char *line)")
config = case_labels(ino, "static bool handleConfigLine(char *line)")
check("I| is a config-console verb", "I" in config)
check("...and is not claimed by the protocol as well", "I" not in proto)

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ble link: OK")
