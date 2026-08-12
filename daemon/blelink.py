#!/usr/bin/env python3
"""BLE central for the Claude Mate device: the same line protocol, off the cable.

WHY THIS EXISTS
---------------
The Wi-Fi transport keeps an association open all day for a device that has
almost nothing to say. Status changes are rare and bursty -- a session goes
WAIT, forty minutes later it goes DONE -- and between them the radio is holding
a link up to transmit nothing. On a 14500 cell that is the wrong shape.

So the device advertises in short bursts (200 ms on, ~4 s off) instead, and this
module is the other half: a BLE central that finds it, connects, authenticates,
and then carries EXACTLY the bytes the serial and TCP links carry. No protocol
change. `F|`/`V|`/`M|`/`P` down, `H`/`K`/`B|` up, newline-delimited ASCII.

The duty-cycle figures come from issue #21, credited there to Jack Jansen in the
ESP8266/ESP32 group, who measured roughly 5% of normal consumption with that
rhythm on his own battery devices.

WHO IS THE CENTRAL
------------------
The Mac. Inverted from the TCP transport, where the device dials out, and for
the same reason in reverse: on BLE the low-power side should be the one that
advertises and sleeps between bursts, and the side with mains power and a real
scheduler should be the one doing the scanning. It also means the device needs
no address for the daemon at all -- no mDNS browse, no host field in the portal.

BLEAK IS OPTIONAL
-----------------
This module imports `bleak` at CALL time, not at import time, so a daemon
without it starts and runs exactly as before; `--ble` reports a clear message
instead of a traceback. The daemon's only hard third-party dependency stays
pyserial, which is a promise the README makes.

WHY A SEPARATE MODULE AND AN OWN EVENT LOOP
-------------------------------------------
bleak is async-only and macOS's CoreBluetooth wants its callbacks on a loop that
keeps running. The daemon is threads-and-blocking-sockets throughout and is not
becoming an asyncio program for one transport. So the loop lives in one daemon
thread here, and the surface this exposes to the rest of the daemon is the
plain, synchronous one `NetLink` already exposes -- start / stop / has_clients /
write_line -- with incoming lines pushed onto the same `queue.Queue` the other
transports use. `LinkHub` cannot tell the three apart.

THREAT MODEL
------------
Identical to the TCP transport's, stated in the same terms so nobody has to
guess whether "it is Bluetooth" bought them anything. The payload is plaintext:
anyone in radio range can read session names, models and states, and an on-path
attacker could inject button events into an established connection. What the
nonce/HMAC handshake buys is that only a device holding the shared token can
attach at all, and that a captured handshake cannot be replayed -- the same
guarantee, over a different pipe, with the same token. Use it on hardware you
trust, in a room you trust.

Deliberately NOT relying on BLE pairing for that: "Just Works" is what a device
with no keypad gets, it is unauthenticated by definition, and it would add an
OS-level pairing dance to every fresh Mac. The token is already provisioned.
"""

from __future__ import annotations

import asyncio
import hmac
import queue
import secrets
import threading
import time
from typing import Optional

# ---- the contract with the firmware ---------------------------------------- #
# These four constants are shared with firmware/claude_mate_s3/blelink.h and
# mean nothing on their own. tools/test_ble_link.py reads both files and fails
# if they drift, because a UUID typo presents as a device that advertises
# forever and a daemon that scans forever, with neither saying anything is
# wrong.
BLE_SVC_UUID = "c1a0de00-3a7e-4b1d-9f2c-6d1e5a7b0001"
BLE_RX_UUID = "c1a0de00-3a7e-4b1d-9f2c-6d1e5a7b0002"   # we write, device reads
BLE_TX_UUID = "c1a0de00-3a7e-4b1d-9f2c-6d1e5a7b0003"   # device notifies, we read
BLE_DEVICE_NAME = "Claude Mate"

# ---- timings (seconds) ------------------------------------------------------ #
# The scan has to outlast one whole silent stretch of the device's duty cycle or
# it can miss a device that is right there: 4 s off means a 3 s scan can land
# entirely inside the gap and come back empty. Eight is two full cycles, which
# makes a miss a coincidence rather than a coin flip.
BLE_SCAN_S = 8.0
BLE_AUTH_S = 5.0            # the firmware allows 5 s; so do we
# Pairing waits on a person, not a radio. The device gives its human 45 s to
# answer the prompt on its screen, so allow a little more than that before
# giving up on them -- and keep the ARMED window short, because it is a standing
# permission to give away a secret.
BLE_PAIR_CONFIRM_S = 50.0
BLE_PAIR_ARM_S = 180.0
BLE_RETRY_S = 3.0           # between scan attempts...
BLE_RETRY_MAX_S = 30.0      # ...doubling on consecutive failures, capped
BLE_MAX_LINE = 512          # drop over-long lines (the longest real one is ~94B)
BLE_NOT_FOUND_GAP_S = 120.0  # between "still looking" notes; see _log_not_found
BLE_WRITE_FAIL_LIMIT = 3     # consecutive failed writes that end a session


class BleLink:
    """Finds the device, authenticates it, and carries the line protocol.

    The surface is NetLink's, so `LinkHub` treats a BLE device exactly as it
    treats a Wi-Fi one:

        start()        -> bool   spin up the loop thread (False = unusable)
        stop()                   disconnect and shut the loop down
        has_clients()  -> bool   is a device attached RIGHT NOW?
        write_line(s)  -> bool   send one line; False if nothing is attached

    Reconnection is this class's own problem and is never the caller's: a device
    on a battery goes out of range, gets carried to another room, or is switched
    into gamepad mode for an hour, and comes back with nobody there to press
    anything.
    """

    def __init__(self, token: str, rx: "queue.Queue[str]",
                 address: Optional[str] = None,
                 log=print) -> None:
        self._token = token.encode("utf-8")
        self._rx = rx
        # An explicit address skips the scan entirely. Worth having for a desk
        # with two of these on it, and for the case the scan is slow: macOS
        # hands out its own per-host UUIDs rather than MAC addresses, so this is
        # the string bleak printed, not something off a sticker.
        self._address = address
        self._log = log
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._client = None            # bleak.BleakClient once authenticated
        self._linked = False
        self._lock = threading.Lock()  # guards _client / _linked
        self._buf = bytearray()        # notification reassembly
        self._auth_q: "queue.Queue[str]" = queue.Queue()
        self._not_found_logged = 0.0   # throttle for _log_not_found
        self._write_fails = 0          # consecutive failed GATT writes
        # Pairing is ARMED, never on. An unprovisioned device in range is not
        # consent to hand it this daemon's secret -- somebody has to ask, at the
        # terminal, and then press a button on the device. See _enrol().
        self._pair_until = 0.0

    # ---- lifecycle --------------------------------------------------------- #

    def start(self) -> bool:
        """Import bleak, then run the connect loop on its own thread.

        False means BLE is not available at all, and the caller must carry on
        without it rather than fail: a Mac with no Bluetooth, or a daemon
        without the optional dependency, is still a perfectly good USB daemon.
        """
        try:
            import bleak  # noqa: F401
        except ImportError:
            self._log("ERROR: --ble needs the 'bleak' package "
                      "(pip install bleak). Continuing without BLE.")
            return False
        self._thread = threading.Thread(target=self._run, name="ble-central",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_evt.set()
        loop = self._loop
        if loop is not None:
            # The loop is running on another thread; poking it any other way is
            # a race. call_soon_threadsafe is the one supported door.
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)

    # ---- the link surface -------------------------------------------------- #

    def has_clients(self) -> bool:
        with self._lock:
            return self._linked

    def write_line(self, line: str) -> bool:
        """Queue one line for the device. False if nothing is attached.

        Returns as soon as the write is HANDED to the loop, not when the radio
        has taken it: this is called from the Screen lock, and a GATT write that
        waits on a connection interval would hold every other thread in the
        daemon for the duration.
        """
        with self._lock:
            client, linked = self._client, self._linked
        if not linked or client is None or self._loop is None:
            return False
        data = (line.rstrip("\n") + "\n").encode("ascii", errors="replace")
        try:
            asyncio.run_coroutine_threadsafe(self._write(client, data),
                                             self._loop)
        except RuntimeError:
            return False        # the loop went away between the check and here
        return True

    # ---- the loop ---------------------------------------------------------- #

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_forever())
        except Exception as exc:                        # noqa: BLE001
            # Never let this thread's death take the daemon with it. A BLE stack
            # that throws something unexpected must cost the wireless link and
            # nothing else -- the USB device on the same desk keeps working.
            #
            # ...but say nothing when we are the ones stopping it. stop() calls
            # loop.stop() from another thread, which is the supported door and
            # which surfaces here as "Event loop stopped before Future
            # completed" -- the ordinary shutdown path, reported as a fault.
            # Observed on hardware, on every clean Ctrl-C.
            if not self._stop_evt.is_set():
                self._log(f"BLE central stopped: {exc}")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _connect_forever(self) -> None:
        fails = 0
        while not self._stop_evt.is_set():
            try:
                found = await self._find()
            except Exception as exc:                    # noqa: BLE001
                self._log(f"BLE scan failed: {exc}")
                found = None
            if found is None:
                # SAY SO, at least sometimes. Silence here is the single most
                # confusing state this transport has: the daemon logged
                # "ble : scanning" once at startup and then nothing, forever,
                # whether the device was on the wrong transport, out of range,
                # off, or being a gamepad. From the outside that is
                # indistinguishable from a daemon that is working, and the one
                # thing the user needs -- what to check -- was never said.
                # Throttled, because the scan repeats and a message per attempt
                # is its own kind of useless.
                self._log_not_found()
                # Exponential backoff, capped. A device that is off stays off
                # for hours -- asleep in a drawer, or being a gamepad -- and
                # scanning every three seconds for hours is a pointless drain on
                # the Mac's radio and a noisy log. Success resets it.
                fails = min(fails + 1, 4)
                await self._sleep(min(BLE_RETRY_S * (2 ** fails),
                                      BLE_RETRY_MAX_S))
                continue
            fails = 0
            try:
                await self._session(found)
            except Exception as exc:                    # noqa: BLE001
                # Same rule as _run(): a session torn down BY stop() is not a
                # failure. Killing the loop out from under an awaiting
                # coroutine surfaces as "coroutine ignored GeneratorExit",
                # which is alarming, accurate and completely uninteresting.
                if not self._stop_evt.is_set():
                    self._log(f"BLE session ended: {exc}")
            finally:
                with self._lock:
                    self._client, self._linked = None, False
                self._buf.clear()
            await self._sleep(1.0)

    def _log_not_found(self) -> None:
        """"Nothing out there yet", with the two things to check, throttled.

        The first one is immediate, because the commonest time to be staring at
        this log is the first minute after turning --ble on. After that it is
        every BLE_NOT_FOUND_GAP_S, which is rare enough to sit under a daemon
        that runs all day and often enough that a device switched on an hour
        later is not met with silence.
        """
        now = time.monotonic()
        if self._not_found_logged and \
                now - self._not_found_logged < BLE_NOT_FOUND_GAP_S:
            return
        first = self._not_found_logged == 0.0
        self._not_found_logged = now
        if self._address:
            self._log(f"BLE: no device at {self._address} yet -- still looking")
            return
        self._log("BLE: no device found yet -- still looking")
        if first:
            # Only once, and only the two things that are actually ever wrong.
            self._log("    on the device: SETTINGS -> Link must read 'ble' "
                      "(or send I|BLE over USB)")
            self._log("    on this Mac: System Settings -> Privacy & Security "
                      "-> Bluetooth must allow the program running this daemon")

    async def _find(self):
        """One scan. Returns a BLEDevice, or None."""
        from bleak import BleakScanner
        if self._address:
            return await BleakScanner.find_device_by_address(
                self._address, timeout=BLE_SCAN_S)
        # By SERVICE UUID rather than by name: the name lives in the scan
        # response and a busy 2.4 GHz room drops those, while the service UUID
        # is in the advertisement itself. Filtering on the name alone made the
        # device appear intermittently, which reads as flaky hardware.
        return await BleakScanner.find_device_by_filter(
            lambda d, ad: (BLE_SVC_UUID.lower() in
                           [u.lower() for u in (ad.service_uuids or [])]),
            timeout=BLE_SCAN_S)

    async def _session(self, device) -> None:
        """Connect, authenticate, then pump until the link drops."""
        from bleak import BleakClient

        disconnected = asyncio.Event()

        def on_disconnect(_client) -> None:
            # Fires on bleak's loop. Setting an Event is all that is safe here.
            disconnected.set()

        async with BleakClient(device,
                               disconnected_callback=on_disconnect) as client:
            self._write_fails = 0
            await client.start_notify(BLE_TX_UUID, self._on_notify)
            if not await self._authenticate(client):
                return
            with self._lock:
                self._client, self._linked = client, True
            self._log(f"BLE device connected: {device.address}")
            try:
                while not self._stop_evt.is_set() and not disconnected.is_set():
                    # DO NOT TRUST THE DISCONNECT CALLBACK ALONE. Observed on
                    # hardware: the device rebooted three times and this daemon
                    # went on believing it was connected for an hour and a half
                    # -- has_clients() true, frames written into nothing, and no
                    # scan ever started again, because the callback that was
                    # supposed to end the session never fired. A transport whose
                    # recovery depends on one notification it does not control
                    # has no recovery.
                    #
                    # So ask, every tick, as well as being told. The TCP
                    # transport learned the same lesson from the other
                    # direction: NetLink reaps a client the first time a write
                    # to it fails, rather than waiting to be informed.
                    if not client.is_connected or \
                            self._write_fails >= BLE_WRITE_FAIL_LIMIT:
                        break
                    try:
                        await asyncio.wait_for(disconnected.wait(), timeout=0.5)
                    except asyncio.TimeoutError:
                        pass
            finally:
                with self._lock:
                    self._client, self._linked = None, False
                self._log(f"BLE device disconnected: {device.address}")

    async def _authenticate(self, client) -> bool:
        """Nonce challenge / HMAC response. False = reject and drop.

        The same three lines the TCP handshake uses, in the same order, verified
        the same way -- one handshake to understand, one token to provision, one
        thing to get wrong.
        """
        # Drain anything the previous session left behind, or a stale A|OK could
        # authenticate a connection that never answered this nonce.
        while not self._auth_q.empty():
            try:
                self._auth_q.get_nowait()
            except queue.Empty:
                break
        nonce = secrets.token_hex(16)
        expect = hmac.new(self._token, nonce.encode("ascii"),
                          "sha256").hexdigest()
        await self._write(client, f"C|{nonce}\n".encode("ascii"))
        reply = await self._await_auth_line()
        if reply is None:
            self._log("BLE: handshake timed out")
            return False
        if reply.strip().upper() == "A|NOTOKEN":
            # An unprovisioned device. If somebody armed pairing at the terminal
            # in the last few minutes, this is the moment it was armed for.
            if self._pair_armed():
                if await self._enrol(client):
                    # _authenticate, not _handshake -- there is no _handshake on
                    # this class. The first cut called one, so every SUCCESSFUL
                    # pairing logged "paired" and then raised AttributeError one
                    # statement later; _connect_forever swallowed it as "session
                    # ended" and disconnected the device it had just paired.
                    # Nothing noticed: the host test's 8 s wait absorbed the
                    # reconnect, and the CLI greps for the log line written just
                    # BEFORE the crash. Recursion is bounded -- the device now
                    # has a token, so it cannot answer A|NOTOKEN again.
                    return await self._authenticate(client)
                return False
            # Otherwise say what to do about it. Pairing FIRST, because it is
            # the only route that needs neither a cable the device may be
            # nowhere near nor a Wi-Fi access point and a phone.
            self._log("BLE: THE DEVICE HAS NO TOKEN. Pair it: run "
                      "`claude-mate-connect --pair` (or press `d` at the "
                      "account picker) and press GO on the device. With a "
                      "cable, plugging it in is enough.")
            await self._write(client, b"A|NO\n")
            return False
        if not reply.startswith("A|"):
            self._log(f"BLE: bad handshake {reply!r}")
            return False
        # compare_digest keeps the comparison time independent of how much of
        # the MAC an attacker guessed right.
        if not hmac.compare_digest(reply[2:].strip().lower(), expect):
            self._log("BLE: token rejected")
            await self._write(client, b"A|NO\n")
            await asyncio.sleep(0.5)          # take the shine off brute force
            return False
        await self._write(client, b"A|OK\n")
        return True

    # ---- pairing ----------------------------------------------------------- #

    def arm_pairing(self, seconds: float = BLE_PAIR_ARM_S) -> None:
        """Allow the next unprovisioned device to be enrolled, for a while.

        A window rather than a switch: whoever asked for this is standing at the
        device right now, and an arming that outlived them would hand the token
        to the next unprovisioned board that wandered into range.
        """
        self._pair_until = time.monotonic() + seconds

    def _pair_armed(self) -> bool:
        return time.monotonic() < self._pair_until

    async def _enrol(self, client) -> bool:
        """Give an unprovisioned device this daemon's token, if a human agrees.

        The device is the one that asks the human -- it puts PAIR? on its own
        screen and waits for a button. That is what makes this safe to expose to
        the radio at all: being in range gets you a prompt on a screen you
        cannot reach, and nothing else. See the E| protocol note in blelink.h.
        """
        self._log("BLE: pairing — press GO on the device to accept "
                  f"(waiting up to {int(BLE_PAIR_CONFIRM_S)}s)")
        await self._write(client, b"E|?\n")
        reply = await self._await_auth_line(BLE_PAIR_CONFIRM_S)
        if reply is None:
            self._log("BLE: pairing timed out — nobody pressed GO")
            self._pair_until = 0.0
            return False
        if reply.strip().upper() != "E|OK":
            # A refusal is a decision, so it disarms. Re-asking a device whose
            # owner just said no would be the wrong kind of persistent.
            self._log(f"BLE: pairing declined by the device ({reply.strip()})")
            self._pair_until = 0.0
            return False
        await self._write(client, b"E|" + self._token + b"\n")
        confirm = await self._await_auth_line()
        if confirm is None or confirm.strip().upper() != "E|SET":
            self._log(f"BLE: the device did not take the token ({confirm!r})")
            return False
        # Disarm on SUCCESS as well: this window existed for one device, and it
        # has been paired.
        self._pair_until = 0.0
        self._log("BLE: paired — the device now has this daemon's token")
        return True

    async def _await_auth_line(self, timeout: float = BLE_AUTH_S) -> Optional[str]:
        """The device's one handshake line, or None if it never came.

        The timeout is a parameter because pairing waits on a HUMAN pressing a
        button, and five seconds is the right budget for a handshake and the
        wrong one for a person noticing their device has lit up.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return self._auth_q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
        return None

    # ---- plumbing ---------------------------------------------------------- #

    def _on_notify(self, _sender, data: bytearray) -> None:
        """Reassemble lines out of notifications.

        A notification boundary is NOT a line boundary and must never be treated
        as one: the device can pack two short lines into one notification, and
        can split a mirror row across two. Buffer, split on newlines, and drop
        anything absurd rather than let one lost byte wedge the buffer forever.
        """
        self._buf += data
        while b"\n" in self._buf:
            raw, _, rest = self._buf.partition(b"\n")
            self._buf = bytearray(rest)
            line = raw.decode("ascii", errors="replace").strip()
            if not line:
                continue
            # The handshake's answer is consumed HERE and never reaches the
            # daemon: an A| line arriving in the ButtonReader would be an
            # unknown verb, logged as noise, while _authenticate() sat waiting
            # for the line it had already been handed.
            with self._lock:
                linked = self._linked
            if not linked:
                self._auth_q.put(line)
            else:
                self._rx.put(line)
        if len(self._buf) > BLE_MAX_LINE:
            self._buf.clear()

    async def _write(self, client, data: bytes) -> None:
        """One GATT write, without response.

        Without response deliberately: the protocol is idempotent -- every frame
        is the whole screen, so a lost one is corrected by the next one a second
        later -- and waiting for an ATT acknowledgement on every frame would
        double the radio time for a guarantee this link does not need.

        A write that RAISES is a second, independent way of learning the device
        has gone -- the one NetLink relies on entirely. It used to be swallowed
        here on the theory that the disconnect callback would end the session;
        hardware disagreed. One failure is a glitch, three in a row is a device
        that is not there.
        """
        try:
            await client.write_gatt_char(BLE_RX_UUID, data, response=False)
            self._write_fails = 0
        except Exception:                               # noqa: BLE001
            self._write_fails += 1

    async def _sleep(self, secs: float) -> None:
        """Sleep, but wake early when stop() is called."""
        end = time.monotonic() + secs
        while time.monotonic() < end and not self._stop_evt.is_set():
            await asyncio.sleep(min(0.25, end - time.monotonic()))
