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

Finally two sets of static assertions about the firmware. One guards the
invariants the menu rework depends on: ONE gamepad row, persisted, and a config
verb that does not collide with the protocol's. The other guards the bootstrap --
every state a device with EMPTY NVS was found stuck in, unable to be given a
token at all without a reflash. Those are static of necessity: a stranded device
advertises nothing and answers nothing, so there is no behaviour on either side
of the protocol for a test to drive.
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
netcfg = read(os.path.join(FW, "netcfg.h"))


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
    pair_answer = "ok"         # "ok" | "no" | "silent" (nobody pressed anything)
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
            # Enrolment. The real device puts PAIR? on its screen here and waits
            # for a thumb; `pair_answer` stands in for what the thumb did, with
            # "silent" for the case that matters most -- nobody was there.
            if raw == "E|?":
                if FakeClient.pair_answer == "ok":
                    self.notify("E|OK")
                elif FakeClient.pair_answer == "no":
                    self.notify("E|NO")
                continue
            if raw.startswith("E|"):
                FakeClient.token = raw[2:]      # adopted, exactly as the board
                FakeClient.mode = "normal"      # does: live, before NVS
                self.notify("E|SET")
                continue
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
print("\n== pairing: a token crosses the air only when a human says so ==")
# The point of the whole exchange is that being in radio range buys you nothing.
# An unprovisioned device is not consent, and the daemon must not hand out the
# secret because it found something willing to take one.
blelink.BLE_PAIR_CONFIRM_S = 0.6      # the tests are not waiting on a human


def pairing_phase(answer):
    """A fresh unprovisioned device, advertising OUR service.

    install_fake_bleak also clears `written`, which each phase below reads as
    'everything this daemon said to this device' -- and it puts the advertised
    UUID back, which the foreign-service test above deliberately left wrong.
    """
    install_fake_bleak(py_const("BLE_SVC_UUID"))
    FakeClient.mode = "notoken"
    FakeClient.token = "not-the-daemons-token"
    FakeClient.pair_answer = answer
    return new_link()


link, rx, logs = pairing_phase("ok")
try:
    link.start()                      # NOT armed
    time.sleep(1.2)
    check("an unprovisioned device is NOT enrolled just for being there",
          not any(l.startswith("E|") for l in FakeClient.written))
    check("...and the log says how to pair it rather than nothing",
          any("--pair" in l for l in logs))
finally:
    link.stop()

link, rx, logs = pairing_phase("silent")
try:
    link.arm_pairing(30)
    link.start()
    time.sleep(2.0)
    check("arming asks the device first, and asks with E|?",
          "E|?" in FakeClient.written)
    check("...and nobody pressing anything sends NO token at all",
          not any(l.startswith("E|") and l != "E|?" for l in FakeClient.written))
    check("...which is reported as the timeout it is",
          any("nobody pressed" in l or "timed out" in l for l in logs))
finally:
    link.stop()

link, rx, logs = pairing_phase("no")
try:
    link.arm_pairing(30)
    link.start()
    time.sleep(1.5)
    check("a device that DECLINES is not sent the token either",
          not any(l.startswith("E|") and l != "E|?" for l in FakeClient.written))
    check("...and one refusal disarms, so it is not asked again on a loop",
          not link._pair_armed())
finally:
    link.stop()

# And the happy path, which has to end LINKED without anyone touching a cable.
link, rx, logs = pairing_phase("ok")
try:
    link.arm_pairing(30)
    link.start()
    ok = wait_for(lambda: link.has_clients(), 8.0)
    check("a device whose human presses GO is enrolled and ends up LINKED",
          bool(ok))
    check("...having been sent the daemon's real token, once",
          [l for l in FakeClient.written if l.startswith("E|") and l != "E|?"]
          == [f"E|{TOKEN}"])
    check("...and it authenticated with it afterwards, so the token took",
          FakeClient.token == TOKEN)
    check("...and pairing disarmed itself, rather than staying open",
          not link._pair_armed())
    check("...and the handshake lines never leaked to the daemon as verbs",
          rx.empty() or all(not l.startswith(("E|", "A|", "C|"))
                            for l in list(rx.queue)))
finally:
    link.stop()
FakeClient.pair_answer = "ok"
FakeClient.token = TOKEN
FakeClient.mode = "normal"

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
# NO transport row on the glass. `Link` was a plain toggle, so one press moved a
# cordless board onto Wi-Fi -- which reboots into a portal that outranks the menu
# and does not time out for a Wi-Fi device, hiding the row you would undo it
# with. The transport still compiles; only the glass cannot reach it, which keeps
# that switch behind a cable you have to actually have.
check("no Link row -- one press must not be able to move a cordless board "
      "onto Wi-Fi", "SR_LINK" not in rows)
check("...and no dangling case for it in the row painter or the input handler",
      not re.search(r"case SR_LINK\b", ino))
check("...but the transport is still switchable over the cable",
      re.search(r'strcasecmp\(a, "ble"\)', ino)
      and re.search(r'strcasecmp\(a, "wifi"\)', ino))

# NO SETUP-PORTAL ROW EITHER: there is no Wi-Fi anywhere on this menu.
#
# It existed for exactly one reason -- a factory-reset board with no cable had no
# way to be given a token -- and BLE ENROLMENT replaced that reason outright. The
# check is worth keeping in this order: removing the row BEFORE pairing existed
# stranded a real board within minutes, so what makes it safe now is not taste,
# it is that `E|?` is implemented and tested above.
check("no setup-portal row -- pairing replaced the only reason it existed",
      "SR_SETUP" not in rows and "SR_WIFI" not in rows)
check("...and no row paints or opens one",
      not re.search(r"case SR_(SETUP|WIFI)\b", ino)
      and not re.search(r'label = "Wi-Fi setup";', ino))
# What makes the removal safe, asserted rather than assumed.
check("...because the device can be given a token over BLE instead",
      re.search(r'if \(!strcmp\(line, "E\|\?"\)\)', fw_ble))
check("...and the portal is still reachable where a mistake cannot reach it",
      "net.startPortalNow();" in ino          # `Z` over USB
      and re.search(r"digitalRead\(PIN_BTN_BOOT\) == LOW", ino))
# GO MUST NOT ARM A WI-FI FLOW. Factory reset is confirmed with a long press of
# GO and reboots at once, so GO was still down when setup() read the buttons:
# every menu factory reset came back up in the Wi-Fi setup portal, on a BLE
# device, reliably. Two independent fixes, because either alone leaves a trap.
check("a held GO cannot raise the setup portal at boot",
      not re.search(r"digitalRead\(PIN_BTN_GO\) == LOW\)",
                    re.search(r"bool forcePortal = .*?;", ino, re.S).group(0)))
check("...and a reboot waits for the button that asked for it to come up",
      re.search(r"static void rebootAfterRelease\(\)", ino)
      and not re.search(r"cfg\.factoryResetAll\(\);\s*\n\s*ESP\.restart", ino))
check("...bounded, so a stuck button cannot block a decided reboot",
      re.search(r"millis\(\) < until &&", ino))
# And the radio is OFF on a build that will never use it -- said plainly in the
# firmware as "certainty, not the reason the battery lasts".
check("a BLE build powers the Wi-Fi radio down explicitly",
      "net.radioOff();" in ino
      and re.search(r"void radioOff\(\) \{\s*\n\s*WiFi\.mode\(WIFI_OFF\);",
                    netcfg))
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
def fn_body(src, signature):
    """The braced body of one function, so a check cannot match the whole file."""
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
    return src[i:j]


def case_labels(src, signature):
    return set(re.findall(r"case\s+'(.)'\s*:", fn_body(src, signature)))


proto = case_labels(ino, "static void handleLine(char *line)")
config = case_labels(ino, "static bool handleConfigLine(char *line)")
check("I| is a config-console verb", "I" in config)
check("...and is not claimed by the protocol as well", "I" not in proto)

# --------------------------------------------------------------------------- #
# 4. A factory-reset device can get itself a token.
# --------------------------------------------------------------------------- #
# Every check here stands for a state a device with EMPTY NVS was found in and
# could not leave without a reflash. None of them is visible from either side of
# the protocol -- a stranded device advertises nothing and says nothing -- so
# there is no behavioural test that would catch a regression, only these.
print("\n== a device with nothing in NVS can bootstrap itself ==")

stored = fn_body(netcfg, "static MateTransport storedTransport()")
# The portal outranks every screen in render() and blocks the menu, and it only
# ever timed out on a device that had credentials to fall back to. So a
# factory-reset device that defaulted to Wi-Fi was locked in the Wi-Fi portal,
# with the Link row that would have got it out visible to nobody.
check("nothing in NVS at all comes up on BLE, not in the Wi-Fi portal",
      re.search(r"if \(!p\.begin\(NET_NS, true\)\) return LINK_BLE", stored))
check("...and so does a wiped namespace with no SSID in it",
      re.search(r"return haveSsid \? LINK_WIFI : LINK_BLE", stored))
# The other half of that: an upgrade must not move a working device onto a
# different radio behind its owner's back.
check("...but a stored SSID still means Wi-Fi", 'p.getString("ssid"' in stored)
check("...and an explicit setting outranks both",
      re.search(r"if \(v == LINK_BLE\)\s+return LINK_BLE;", stored)
      and re.search(r"if \(v == LINK_WIFI\) return LINK_WIFI;", stored))

# The two ways a token reaches a device that is ALREADY advertising. Both were
# broken, both in the same way -- the secret landed in NVS and the running stack
# never heard about it -- and both present as the daemon rejecting a token that
# is right.
check("T| over USB hands the token to the live BLE stack, not just to NVS",
      re.search(r"net\.setToken\(a \+ 1\);.*?ble\.setToken\(a \+ 1\);",
                fn_body(ino, "static bool handleConfigLine(char *line)"), re.S))
check("...and so does the portal, whose page handler cannot reach the stack",
      re.search(r"if \(_state != OFF\) \{ setToken\(token\); return true; \}",
                fw_ble))
# ...and the portal has to ACCEPT a token on its own to be that route at all. It
# used to 400 on a blank network box, which on a BLE device -- where the page is
# only ever visited for the token -- left "type a fake network name" as the way
# through, and a <select> that always submits something silently stored whichever
# network topped the scan.
save = fn_body(netcfg, "void serveSave()")
check("...and the portal saves a token with no network at all",
      re.search(r"if \(ssid\.isEmpty\(\) && token\.isEmpty\(\) && !clearing\)",
                save)
      and re.search(r"if \(!ssid\.isEmpty\(\)\) setWifi\(ssid, pass\);", save))
check("...and offers 'none' to a device that has another link",
      re.search(r"if \(_fallbackLink\)\s*\n\s*html \+= F\(\"<option value=''>",
                fn_body(netcfg, "void serveForm()")))

# A BLE device stores Wi-Fi credentials for later; it must not ASSOCIATE on them.
# Both radios up at once is the contention one-transport-at-a-time exists to
# prevent, and net.restart() on a BLE build with an SSID stored does exactly it.
check("no config verb restarts Wi-Fi without checking it is the live transport",
      not re.search(r"^\s*net\.restart\(\);",
                    fn_body(ino, "static bool handleConfigLine(char *line)"),
                    re.M))

# The escape hatch has to be an escape hatch in both directions: BOOT held at
# power-on on a cordless BLE board opens a Wi-Fi portal it can never fill in.
check("a device with another link says so, so the portal is allowed to expire",
      "net.setFallbackLink(transport == LINK_BLE);" in ino)
check("...and the portal timeout honours it",
      re.search(r"if \(\(configured\(\) \|\| _fallbackLink\) &&",
                netcfg))
check("...and expiring with no SSID powers the radio down rather than "
      "dialling an empty one",
      re.search(r"\} else \{\s*\n\s*note\(\"setup timed out\"\);\s*\n\s*"
                r"shutdown\(\);", fn_body(netcfg, "void pollPortal()")))
# ...which strands the device unless the sketch notices SETUP ending and starts
# the transport the Link row claims this device is on.
check("...and the sketch hands the glass back to BLE afterwards",
      re.search(r"transport == LINK_BLE && lastNetState == MateNet::SETUP.*?"
                r"net\.shutdown\(\);\s*\n\s*ble\.begin\(", ino, re.S))

# Last: the screens a fresh board actually shows. A device with no token that
# says "check the daemon is running with --ble" sends you to read the wrong log.
check("the BLE status line names the missing token first",
      re.search(r"case ADVERTISING: return _token\.isEmpty\(\)", fw_ble))
check("...and so does the NO LINK screen",
      re.search(r"gfx->print\(!net\.hasToken\(\)", ino))
# Matched against the note() STRING, not the file: the comments around these
# lines quote the wording they replaced, and a check that reads comments would
# pass or fail on prose.
check("...and the NOTOKEN answer names neither a cable nor a portal, which a "
      "cordless board may have neither of",
      not re.search(r'note\("[^"]*setup portal', fw_ble)
      and not re.search(r'note\("[^"]*over USB', fw_ble))
# The three no-token strings a person can actually see, all pointing at the same
# place. They drifted once already -- the glass said "over USB" while the only
# on-glass route had been deleted -- so they are pinned together.
# --------------------------------------------------------------------------- #
# The other half of the pairing contract, which lives in C++ and cannot be
# driven from here. The daemon's side is exercised above against a fake device;
# these check the real device would answer it the same way.
print("\n== the firmware's half of the pairing handshake ==")

check("the device answers E|? rather than ignoring it",
      re.search(r'if \(!strcmp\(line, "E\|\?"\)\)', fw_ble))
# THE ONE THAT MADE PAIRING IMPOSSIBLE, and that neither end could see: the
# device answered A|NOTOKEN and hung up in the same breath, so E|? always
# arrived at a connection that had already gone. The daemon logged that it had
# asked; the firmware never saw a byte. "I have no token" is precisely the
# moment to stay on the line -- it is when someone may be about to give you one.
# Scoped to the answer itself -- from the A|NOTOKEN notify to the return that
# ends that branch -- rather than to a brace-matched block, which silently ran
# past its own closing brace and swallowed the code after it.
# Matching the ASSIGNMENT, not the word: the comment right there explains what
# used to be on that line, and a check that reads comments fails on prose. Third
# time this file has been caught by that, hence the note.
notok = re.search(r'notifyLine\("A\|NOTOKEN"\);(.*?)return;', fw_ble, re.S)
check("...and stays connected after saying it has no token",
      notok and not re.search(r"_pendingDisconnect\s*=\s*true", notok.group(1)))
check("...refuses to be re-enrolled once it HAS a token",
      re.search(r'if \(!_token\.isEmpty\(\)\) \{ notifyLine\("E\|NO"\); return; \}',
                fw_ble))
# The one that matters: without it, "no token yet" is itself permission and a
# freshly reset board belongs to whoever is in radio range first.
check("...and takes a token ONLY against an approval a human just gave",
      re.search(r"if \(!_pairOk \|\| !_token\.isEmpty\(\)", fw_ble))
# An EMPTY payload is not a token. Without this, a bare `E|` spent the one-shot
# approval, stored "" as the live token and reported E|SET and "paired" -- and on
# the path where the portal had just written a real token to NVS while this stack
# still held none, it wiped it.
check("...and an empty E| is not one",
      re.search(r"\|\| line\[2\] == 0", fw_ble))
# Every other assignment in the file clears it; exactly one grants it, and that
# one is the human's answer. Written as "count the grants" rather than "count the
# assignments" so that adding another place that CLEARS approval -- which is
# always safe -- does not fail a test about who may give it.
# Counting the GRANTS, not the assignments: adding another place that clears
# approval is always safe, and a negative lookahead here would be defeated by
# backtracking over the whitespace anyway (`\s*` can match nothing, and " false"
# does not start with "false").
check("...where that approval is granted in exactly one place, pairAnswer()",
      [v for v in re.findall(r"_pairOk\s*=\s*(\w+)", fw_ble) if v != "false"]
      == ["yes"]
      and re.search(r"void pairAnswer\(bool yes\) \{\s*\n\s*_pairAsk = false;"
                    r"\s*\n\s*_pairOk = yes;", fw_ble))
check("...is single-use", re.search(r"_pairOk = false;\s+// single use", fw_ble))
check("...and does not survive the connection it was given in",
      re.search(r"void reAdvertise\(\) \{.*?_pairOk = false;", fw_ble, re.S))
check("the sketch puts the question on the glass",
      "drawPairAsk()" in ino and "PAIR THIS DEVICE?" in ino)
# ...and says which BUTTON, in the biggest type on the screen. Someone looking up
# at this having just run a command does not need the situation described, they
# need to know what to press.
check("...naming the button rather than describing the situation",
      re.search(r'gfx->print\("GO = ACCEPT"\);', ino))
# A RECEIPT ON THE DEVICE. Without it the screen snapped straight back to the
# conductor view and the only evidence was on the Mac -- reported as "paired but
# it is not obvious", which for a security decision made with a button press on
# this device is a fair complaint.
check("...and a confirmation afterwards, where the button was pressed",
      "drawPairedOk()" in ino and re.search(r'"PAIRED"', ino))
check("...which dismisses itself, so it is a receipt and not a mode",
      re.search(r"pairedNoticeMs && \(now - pairedNoticeMs\) >= "
                r"PAIRED_NOTICE_MS", ino))
check("...and any button clears it early",
      re.search(r"if \(pairedNoticeMs\) \{\s*\n\s*pairedNoticeMs = 0;", ino))
check("...stamped from `now`, like every other deadline in this loop",
      re.search(r"pairedNoticeMs = now \? now : 1UL;", ino))
check("...answers it with a button, GO for yes",
      re.search(r"if \(ev == 'G' \|\| ev == 'K'\) answerPairing\(true\);", ino))
check("...and lets it expire rather than standing open",
      re.search(r"else if \(\(now - pairAskedMs\) >= PAIR_ASK_MS\) "
                r"answerPairing\(false\);", ino))
check("...and takes it down when the peer that asked goes away",
      re.search(r"if \(!ble\.pairRequested\(\)\) \{ pairAskedMs = 0;", ino))
# Stamped from `now`, which was read at the top of this loop pass. A fresh
# millis() here is a few microseconds LATER, the unsigned `now - pairAskedMs`
# underflows to ~4.29e9, and the prompt answers itself with "no" in the same
# pass that raised it. Same family as the `| 1` sentinel bugs elsewhere.
check("...and the countdown cannot start in the future",
      re.search(r"pairAskedMs = now \? now : 1UL;", ino))
# THE BUG THE HOST TESTS COULD NOT SEE. The fake device has no handshake
# timeout; the real one hung up 5 s into a 45 s question, ~40 s before anyone
# could have answered it, and the first pairing attempt on hardware therefore
# got no reply at all. A handshake is a machine waiting, a pairing question is a
# person walking over, and one budget cannot serve both.
pair_to = re.search(r"#define BLE_PAIR_TIMEOUT\s+(\d+)", fw_ble)
ask_ms = re.search(r"#define PAIR_ASK_MS (\d+)", ino)
check("the link does not hang up while a human is being asked",
      re.search(r"unsigned long budget = _pairAsk \? BLE_PAIR_TIMEOUT "
                r": BLE_AUTH_TIMEOUT;", fw_ble))
check("...with a window that outlasts the one the glass offers",
      pair_to and ask_ms and int(pair_to.group(1)) > int(ask_ms.group(1)))
check("a granted token reaches NVS, not just the live stack",
      re.search(r"ble\.takeGrantedToken\(granted\)\) \{\s*\n\s*net\.setToken\(granted\);",
                ino))

print("\n== the no-token guidance still agrees with itself ==")
# These have drifted twice: once when the glass said "over USB" after the only
# on-glass route had been deleted, and once when the daemon learned to pair while
# both screens went on naming the long way round. Pinned together since.
check("every no-token message names the same route",
      re.search(r'\? "no token: claude-mate-connect"', fw_ble)
      and re.search(r'\? "no token: claude-mate-connect"', ino)
      and re.search(r'note\("no token - claude-mate-connect"\)', fw_ble)
      and "claude-mate-connect --pair" in read(
          os.path.join(DAEMON_DIR, "blelink.py")))

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ble link: OK")
