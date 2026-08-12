/*
 * blelink.h -- the daemon<->device line protocol over BLE, for the battery build
 * =============================================================================
 *
 * The same bytes netcfg.h carries over TCP (F| / V| / M| / P down, H / K / B|
 * up), on a GATT service instead of a socket. Nothing above this file knows
 * which pipe it got.
 *
 * WHY, WHEN TCP ALREADY WORKS. Wi-Fi keeps an association up all day for a
 * device that has almost nothing to say. Status changes are rare and bursty --
 * a session goes WAIT, forty minutes later it goes DONE -- and in between the
 * radio is holding a link open to transmit nothing. That is the wrong shape for
 * a cell. This transport inverts it: the device advertises in short bursts and
 * is otherwise quiet, and the daemon connects when it has something to say.
 *
 * DUTY-CYCLED ADVERTISING. BLE_ADV_ON_MS of advertising, then BLE_ADV_OFF_MS of
 * silence, repeating, until a central connects. 200 ms on / 4 s off is the
 * rhythm from the issue (#21) -- credited there to Jack Jansen, who measured
 * roughly 5% of normal consumption with it on his own battery devices. The
 * numbers suit this device exactly: it does not need low latency, it needs to
 * never MISS a state change. A few seconds between "this session went idle" and
 * the glass updating is fine; missing the transition is not.
 *
 * BE HONEST ABOUT WHAT THAT SAVES HERE. Stopping the advertiser cuts the
 * RADIO's share of the draw. It does not stop the CPU -- this firmware never
 * light-sleeps, because the display, the LED pattern engine and the button poll
 * all want the loop running -- so the saving is real but bounded, and the
 * backlight remains the biggest single consumer on the board either way (see
 * the hibernate note in the sketch). docs/POWER.md holds the measurements.
 *
 * ADVERTISING IS NOT THE PAYLOAD. The issue floated encoding the compact status
 * into the advertising packet. It cannot work in this direction: the status
 * originates on the HOST and this device is the peripheral, so the only thing
 * the device could advertise about is itself. The advertisement therefore
 * carries identity (name + service UUID) and the status travels on GATT, where
 * it also gets acknowledgement and flow control for free.
 *
 * TWO CHARACTERISTICS, one per direction, because a GATT characteristic is not
 * bidirectional:
 *
 *     RX  write / write-no-response    daemon -> device   (frames, LED, mirror)
 *     TX  notify                       device -> daemon   (hello, acks, buttons)
 *
 * AUTHENTICATION is the SAME nonce/HMAC handshake the TCP transport uses, and
 * deliberately so: one shared token, one thing to configure, one thing to get
 * wrong. The daemon writes C|<nonce>, we notify A|<hex HMAC-SHA256(token,
 * nonce)>, it writes A|OK and the link is live. Bytes before A|OK never reach
 * the sketch.
 *
 * WHY NOT LEAN ON BLE PAIRING INSTEAD. Just Works pairing (this board has no
 * keypad, so there is no passkey to type) is unauthenticated -- it stops a
 * passive sniffer and nothing else -- and it would add an OS-level pairing step
 * to every fresh daemon on every Mac. The token handshake is what actually
 * decides who may drive this device, and it is already provisioned. So the link
 * is left unencrypted and the threat model is exactly NetLink's, stated in the
 * same terms: the payload is plaintext, anyone in radio range can read session
 * names and states, and an on-path attacker could inject button events into an
 * established connection. Use it where you would use the Wi-Fi transport.
 *
 * THE RADIO IS SHARED. One 2.4 GHz radio serves Wi-Fi, this link and the HID
 * gamepad, and running two of them at once degrades all of them. The sketch
 * owns that decision -- exactly one transport is up at a time -- and this file
 * deliberately knows nothing about it.
 */

#pragma once

#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <mbedtls/md.h>

// ---- tuning -----------------------------------------------------------------
#define BLE_ADV_ON_MS     200UL     // advertising burst...
#define BLE_ADV_OFF_MS    4000UL    // ...and the silence after it
// Inside a 200 ms burst the advertisement has to actually be SEEN, and a
// central's scan window is not continuous either. 30 ms between packets puts
// ~6 of them in the burst on each of the three advertising channels, which is
// enough for a scanner running a normal duty cycle to catch one. Advertising
// slower than the burst is long would mean bursts that emit nothing at all.
#define BLE_ADV_ITVL      0x30      // 0x30 * 0.625 ms = 30 ms
#define BLE_AUTH_TIMEOUT  5000UL    // the daemon allows 5 s; so do we
// ...but a pairing question is answered by a HUMAN, and five seconds is not an
// offer, it is a flicker. This has to outlast the 45 s the sketch gives someone
// to notice the screen and press GO, while still bounding a connection that is
// holding the radio open waiting for a room that turns out to be empty.
#define BLE_PAIR_TIMEOUT  60000UL
// The protocol's longest real line is ~94 bytes and the mirror sends 17 of them
// in one burst. A ring this size holds a whole mirror frame plus the status
// frame behind it, so a busy moment never truncates a row on the glass.
#define BLE_RX_RING       2048
// How hard to try before admitting the stack is not coming up this boot. Three
// is not timidity: on this chip a re-init after a deinit fails deterministically
// (see begin()), so the retries exist to distinguish that from a transient, not
// to grind away at something that will never work.
#define BLE_START_TRIES   3
#define BLE_START_RETRY_MS 2000UL

// 128-bit UUIDs, this project's own. A 16-bit UUID would have to come from the
// SIG's assigned list and none of them means "a triage companion's line
// protocol"; squatting on one would make this device lie about what it is to
// every scanner in range.
//
// These strings are the CONTRACT with the daemon (daemon/blelink.py). Changing
// one means changing both, and tools/test_ble_link.py fails if they drift.
#define BLE_SVC_UUID  "c1a0de00-3a7e-4b1d-9f2c-6d1e5a7b0001"
#define BLE_RX_UUID   "c1a0de00-3a7e-4b1d-9f2c-6d1e5a7b0002"
#define BLE_TX_UUID   "c1a0de00-3a7e-4b1d-9f2c-6d1e5a7b0003"

class MateBle {
 public:
  enum State : uint8_t {
    OFF,          // stack down
    ADVERTISING,  // duty-cycling, waiting for the daemon
    AUTHING,      // connected, nonce challenge in flight
    LINKED,       // protocol is flowing
  };

  // Bring the stack up and start advertising.
  //
  // Returns false if BLE would not start, and the caller must treat that as
  // "there is no link", not as "it will be along shortly" -- a transport that
  // silently never connects is the hardest possible thing to diagnose from
  // across a room.
  // THE STACK DOES NOT COME BACK IN THE SAME BOOT. Measured on hardware, not
  // assumed: BLEDevice::deinit(true) does not fully release the controller, and
  // every BLEDevice::init() after it fails. A fresh boot advertises; a boot that
  // has already torn BLE down once never gets it back, however many times you
  // ask. So begin() remembers what it was asked for and poll() retries -- and
  // when the retries are spent, stuck() tells the sketch, whose only real answer
  // is to reboot. That is not a workaround dressed up as a policy: rebooting is
  // genuinely the only way back, and the transport is in NVS so the device comes
  // up as exactly what it was.
  //
  // Before this, a failed start was SILENT and permanent: `ble : OFF`, no
  // advertising, no link, no retry, and no way to force one -- found by flashing
  // a board and wondering why the daemon had stopped seeing it.
  bool begin(const char *name, const String &token) {
    // Already up: take the token and stay up.
    //
    // The early return is not a no-op, and getting that wrong broke the ONLY
    // path a cordless device has to a token. `Z` over serial and SETTINGS ->
    // WiFi setup both open the portal with this stack still running; the token
    // is written to NVS by a page handler that has never heard of this object,
    // and the sketch's hand-back afterwards arrives here. Returning without
    // reading it left the live stack answering A|NOTOKEN with the right secret
    // in flash beside it -- indistinguishable from a daemon rejecting a token
    // that is correct. Restarting to pick it up is not available: the stack does
    // not come back in the same boot (see below).
    if (_state != OFF) { setToken(token); return true; }
    snprintf(_name, sizeof(_name), "%s", name ? name : "Claude Mate");
    _token = token;
    _wantUp = true;
    _fails = 0;
    return start();
  }

 private:
  bool start() {
    _lastTry = millis();
    if (!BLEDevice::getInitialized()) BLEDevice::init(_name);
    // 185 leaves the whole longest line in one ATT write. At the default 23 the
    // daemon would have to split every frame across four writes, and a split
    // that lands mid-line is fine (we reassemble on newlines) but pointless.
    BLEDevice::setMTU(185);

    _server = BLEDevice::createServer();
    if (!_server) return startFailed();
    _server->setCallbacks(&_srvCb);

    BLEService *svc = _server->createService(BLEUUID(BLE_SVC_UUID));
    if (!svc) return startFailed();

    _rx = svc->createCharacteristic(BLE_RX_UUID,
                                    BLECharacteristic::PROPERTY_WRITE |
                                    BLECharacteristic::PROPERTY_WRITE_NR);
    _tx = svc->createCharacteristic(BLE_TX_UUID,
                                    BLECharacteristic::PROPERTY_NOTIFY);
    if (!_rx || !_tx) return startFailed();
    _rxCb.owner = this;
    _rx->setCallbacks(&_rxCb);
    // The client-characteristic-configuration descriptor: without it Bluedroid
    // has nowhere to record that the daemon subscribed, and notify() quietly
    // does nothing -- a link that connects, authenticates and then appears to
    // be one-way. (A NimBLE build creates it on its own and ignores this.)
    _tx->addDescriptor(new BLE2902());

    svc->start();

    BLEAdvertising *adv = BLEDevice::getAdvertising();
    adv->addServiceUUID(svc->getUUID());
    adv->setScanResponse(true);       // the name lives here; the 31-byte
                                      // advertisement is full with one 128-bit
                                      // UUID and the flags
    adv->setMinInterval(BLE_ADV_ITVL);
    adv->setMaxInterval(BLE_ADV_ITVL);

    _fails = 0;
    go(ADVERTISING);
    startBurst();
    return true;
  }

  // A start that did not take. Tears the pieces down WITHOUT clearing _wantUp,
  // so poll() keeps trying -- the difference between "we gave up" and "the
  // caller asked us to stop", which shutdown() means and this does not.
  bool startFailed() {
    teardown();
    if (_fails < 250) _fails++;
    note("ble would not start");
    return false;
  }

 public:
  // Advance the duty cycle and time the handshake out. Never blocks -- which is
  // the quiet advantage over the Wi-Fi path, where MDNS.queryService() and the
  // TCP dial both stop the loop (and therefore the button poll) for most of a
  // second every retry.
  void poll() {
    if (_state == OFF) {
      // Down but wanted: keep trying. On this chip the retry is very unlikely
      // to succeed (see begin()), but it costs nothing, it covers a start that
      // failed for some other reason at boot, and it is what makes stuck()
      // meaningful -- "we asked BLE_START_TRIES times" rather than "we asked
      // once and gave up", which is not a basis for rebooting a device.
      if (!_wantUp || _fails >= BLE_START_TRIES) return;
      if ((millis() - _lastTry) < BLE_START_RETRY_MS) return;
      start();
      return;
    }

    // A handshake the callback rejected hangs up HERE, not there. Tearing the
    // connection down from inside a GATT write callback means calling into the
    // stack while it is still walking the write it just handed us; one poll
    // later cannot re-enter.
    if (_pendingDisconnect) {
      _pendingDisconnect = false;
      disconnectPeer();
      return;
    }

    unsigned long now = millis();

    // Connect and disconnect are both seen on the BLE task, so every state has
    // to reconcile with the flag rather than be driven by an event here.
    if (_state == ADVERTISING) {
      if (_srvCb.connected) { go(AUTHING); return; }
      if (_advOn && (now - _advSince) >= BLE_ADV_ON_MS) {
        BLEDevice::stopAdvertising();
        _advOn = false;
        _advSince = now;
      } else if (!_advOn && (now - _advSince) >= BLE_ADV_OFF_MS) {
        startBurst();
      }
      return;
    }

    if (!_srvCb.connected) { drop("link closed"); return; }

    // A HANDSHAKE IS A MACHINE WAITING; A PAIRING QUESTION IS A PERSON WALKING
    // OVER. Five seconds is right for the first and absurd for the second, and
    // applying it to both is why the first pairing attempt on real hardware got
    // no answer at all: the device hung up ~40 s before anyone could have
    // pressed GO, and reAdvertise() then cleared the request that was still on
    // the glass. The fake device in the host tests has no such timeout, so only
    // the board could find this.
    unsigned long budget = _pairAsk ? BLE_PAIR_TIMEOUT : BLE_AUTH_TIMEOUT;
    if (_state == AUTHING && (now - _stateSince) > budget) {
      // Say why before dropping. A daemon too old to know this service exists
      // will still connect -- macOS caches GATT and opens the link happily --
      // then sit there, and from the glass that is indistinguishable from a
      // wrong token.
      // ...and do not call a declined pairing a missing daemon. The connection
      // ends the same way either way, but "no handshake - is the daemon on
      // --ble?" after someone has just pressed a button to say NO would send
      // them to check a flag that was never the problem.
      note(_pairAsk    ? "pairing not answered"
           : _pairSpoke ? "pairing declined"
                        : "no handshake - is the daemon on --ble?");
      _pairAsk = false;
      disconnectPeer();
    }
  }

  // ---- the link surface (identical in shape to MateNet's) -------------------

  bool connected() const { return _state == LINKED; }
  State state() const { return _state; }

  // Take a token set AFTER the stack came up, without restarting anything.
  //
  // This is the whole bootstrap path on a fresh board and it has to work while
  // advertising: the device comes out of a factory reset with no token, you
  // send T|<token> over the cable you just flashed with, and the very next
  // handshake has to succeed. Re-begin()ing to pick it up is not an option --
  // BLE does not come back in the same boot -- and the token is only ever read
  // inside handleAuthLine(), so replacing it live is safe and is all that is
  // needed.
  void setToken(const String &token) { _token = token; }

  // ---- enrolment (E|), the way a cordless board gets its first token --------
  //
  // Every other way to provision this device needs something it is not: a cable
  // it may be nowhere near, or a Wi-Fi access point and a phone -- on a BLE
  // board, over a link that is already connected and talking. This is the verb
  // that was missing. The daemon asks, the DEVICE asks its human, and only then
  // does a secret move.
  //
  //   daemon -> device   E|?          may I enrol you?
  //   device -> daemon   E|OK         (a human pressed GO on the glass)
  //                      E|NO         (declined, already provisioned, or
  //                                    nobody was there)
  //   daemon -> device   E|<token>    ...only after E|OK
  //   device -> daemon   E|SET        adopted; challenge me again
  //
  // WHY A BUTTON RATHER THAN A CODE TO TYPE. Both prove the same thing -- that
  // whoever is enrolling can see the device -- and the button proves it without
  // anyone transcribing a secret between two machines. Nothing is granted by
  // the radio alone: a stranger in range can send E|? all day and gets a prompt
  // on a screen they cannot reach, which times out saying no.
  //
  // WHAT IT COSTS, said plainly because it is a real cost: the token crosses
  // the air once, in the clear, inside that window. The link is unencrypted by
  // design already (see the threat model at the top of this file), so this adds
  // no plaintext that was not there -- but a sniffer listening at the second
  // you press GO does learn the secret. Pair away from hostile radio, or use
  // the cable, which the daemon provisions over automatically.
  bool pairRequested() const { return _pairAsk; }

  // The human's answer, from the sketch, on the main loop.
  //
  // _pairOk is the ONLY thing that lets a token in, and nothing else sets it.
  // Without it, "has no token yet" would itself be permission, and a freshly
  // reset board would belong to whoever is in range first. The approval is also
  // single-use and dies with the connection (see reAdvertise): the peer that
  // asked is the only one that may send, and only once.
  void pairAnswer(bool yes) {
    _pairAsk = false;
    _pairOk = yes;
    _pairSpoke = true;
    // THE HANDSHAKE BUDGET STARTS NOW, and forgetting this broke the accept
    // path outright: _stateSince is however long ago the peer connected, the
    // person took thirty seconds to walk over and press GO, and the moment
    // _pairAsk cleared the budget snapped back to five -- so poll() would drop
    // the connection before the token could cross it. What follows this line is
    // an ordinary handshake and gets an ordinary handshake's time.
    _stateSince = millis();
    notifyLine(yes ? "E|OK" : "E|NO");
    if (!yes) note("pairing declined");
  }

  // A token granted over the air, for the sketch to put in NVS. False when
  // there is nothing waiting. The LIVE stack already has it -- adopting it is
  // what lets the challenge straight afterwards succeed -- so this is only the
  // durable half, and losing power between the two costs one more pairing
  // rather than anything worse.
  bool takeGrantedToken(String &out) {
    if (!_grantReady) return false;
    _grantReady = false;
    out = _token;
    return true;
  }

  // "I was asked to be up, I have tried as many times as is worth trying, and I
  // am not up." The sketch's only real answer is a reboot -- see begin(). Kept
  // as a QUESTION rather than acted on here: a transport header that reboots the
  // device on its own is not something the rest of the firmware could reason
  // about, and the sketch has a flush to do first.
  bool stuck() const {
    return _wantUp && _state == OFF && _fails >= BLE_START_TRIES;
  }

  bool write(const char *line) {
    if (_state != LINKED) return false;
    return notifyLine(line);
  }

  // One byte, or -1 when nothing is waiting. Drains the ring the BLE task fills.
  int read() {
    if (_rxTail == _rxHead) return -1;
    uint8_t c = _ring[_rxTail];
    _rxTail = (uint16_t)((_rxTail + 1) % BLE_RX_RING);
    return c;
  }

  // Take the stack down and hand the radio back.
  //
  // deinit(true) frees the controller rather than merely stopping it. A stack
  // that is parked but resident keeps contending for the one 2.4 GHz radio,
  // which is the entire reason the sketch ever asks for this.
  void shutdown() {
    _wantUp = false;      // deliberate: poll() must not resurrect it
    _fails = 0;
    teardown();
  }

  // The pieces, without the intent. Used both by shutdown() and by a start that
  // did not take.
  void teardown() {
    if (BLEDevice::getInitialized()) BLEDevice::deinit(true);
    _server = nullptr;
    _rx = _tx = nullptr;
    _srvCb.connected = false;
    _rxHead = _rxTail = 0;
    _lineLen = 0;
    _advOn = false;
    _pendingDisconnect = false;
    _state = OFF;
  }

  // ---- what the screen and the console show about us ------------------------

  static const unsigned long DROP_SHOW_MS = 20000UL;

  const char *statusText() {
    // A recent failure outranks the state, for the same reason it does on the
    // Wi-Fi path: "advertising..." forever tells you nothing, and "token
    // rejected" tells you everything.
    if (_dropAt && (millis() - _dropAt) < DROP_SHOW_MS && _state != LINKED) {
      snprintf(_status, sizeof(_status), "x %s", _dropWhy);
      return _status;
    }
    switch (_state) {
      case OFF:         return "ble off - usb only";
      // Name the FLAG, not the situation. "waiting for the daemon" is what the
      // device is doing and tells you nothing you could act on; the daemon
      // needs one specific option turned on, and this line is often the only
      // place anyone will ever be told which.
      //
      // ...unless there is no token, in which case that is the FIRST thing that
      // will fail and saying anything else sends you to look at the daemon. A
      // factory-reset device is exactly this device, so this is the line a
      // fresh board shows: it has to be the one that gets you moving.
      //
      // It names the command on the MAC, because that is where the shortest
      // route starts: one command, then one button on this device. Naming a
      // cable would be a dead end on a board that is cordless by design, and
      // naming the local menu row sends you the long way round -- an access
      // point, a phone, and a secret typed by hand.
      case ADVERTISING: return _token.isEmpty()
                                   ? "no token: claude-mate-connect"
                                   : "ble: start the daemon with --ble";
      case AUTHING:     return "authenticating...";
      case LINKED:      return "ble linked";
    }
    return "";
  }

  const char *stateName() const {
    switch (_state) {
      case OFF: return "OFF";              case ADVERTISING: return "ADVERTISING";
      case AUTHING: return "AUTHING";      case LINKED: return "LINKED";
    }
    return "?";
  }

  void printStatus(Print &out) {
    out.printf("ble   : %s  adv %lums/%lums  token %s\n", stateName(),
               BLE_ADV_ON_MS, BLE_ADV_OFF_MS,
               _token.isEmpty() ? "(unset)" : "(set)");
    if (_dropAt)
      out.printf("last  : %s (%lus ago)\n", _dropWhy,
                 (unsigned long)((millis() - _dropAt) / 1000));
  }

 private:
  // ---- callbacks (BLE task context) ----------------------------------------

  struct SrvCb : public BLEServerCallbacks {
    volatile bool connected = false;
    void onConnect(BLEServer *) override { connected = true; }
    void onDisconnect(BLEServer *) override { connected = false; }
  };

  struct RxCb : public BLECharacteristicCallbacks {
    MateBle *owner = nullptr;
    void onWrite(BLECharacteristic *c) override {
      if (owner) owner->onRxData(c->getData(), c->getLength());
    }
  };

  // Everything the central writes lands here, on the BLE task.
  //
  // Before the handshake finishes the bytes are assembled into lines and
  // answered HERE rather than handed up: the sketch has no business seeing a
  // challenge, and a C| that reached handleLine() would be dropped as an
  // unknown verb while the daemon waited five seconds for an answer.
  //
  // After it finishes the bytes go straight into the ring. Single producer
  // (this task), single consumer (read(), from loop()), so the two indices need
  // no lock -- each is written by exactly one side.
  void onRxData(const uint8_t *data, size_t len) {
    if (!data || !len) return;
    if (_state == LINKED) {
      for (size_t i = 0; i < len; i++) push(data[i]);
      return;
    }
    // NOT `if (_state == AUTHING)`. The central writes its challenge the moment
    // it has subscribed, and the move from ADVERTISING to AUTHING happens in
    // poll() on the main loop -- so a fast Mac can land the C| in the window
    // between the two. Dropping it there would stall the handshake until the
    // five-second timeout on both sides, intermittently, on exactly the
    // hardware that connects quickest.
    for (size_t i = 0; i < len; i++) {
      char c = (char)data[i];
      if (c == '\n' || c == '\r') {
        if (!_lineLen) continue;                    // tolerate CRLF
        _line[_lineLen] = 0;
        _lineLen = 0;
        handleAuthLine(_line);
        if (_state == LINKED) {
          // A daemon may pipeline the first frame straight behind A|OK.
          // Everything after this newline is protocol, so push the remainder
          // rather than dropping it on the floor.
          for (size_t j = i + 1; j < len; j++) push(data[j]);
          return;
        }
      } else if (_lineLen < sizeof(_line) - 1) {
        _line[_lineLen++] = c;
      } else {
        _lineLen = 0;                               // oversized: resync
      }
    }
  }

  void push(uint8_t c) {
    uint16_t next = (uint16_t)((_rxHead + 1) % BLE_RX_RING);
    if (next == _rxTail) return;    // full: drop. The alternative is overwriting
    _ring[_rxHead] = c;             // the oldest byte, which corrupts a line the
    _rxHead = next;                 // sketch is halfway through parsing.
  }

  void handleAuthLine(const char *line) {
    if (!strcmp(line, "E|?")) {
      // Already provisioned: nothing to enrol, and answering NO tells the
      // daemon to stop asking rather than leaving it waiting on a prompt this
      // device is never going to show.
      if (!_token.isEmpty()) { notifyLine("E|NO"); return; }
      _pairAsk = true;                  // the sketch draws the prompt and asks
      return;
    }
    if (!strncmp(line, "E|", 2)) {
      // A token -- accepted ONLY against a live approval a human just gave on
      // the glass. "The device has no token" must never be permission by
      // itself, or a freshly reset board belongs to whoever is in range first.
      // An EMPTY payload is not a token. Without the length check a bare `E|`
      // spent the single-use approval, stored "" as the live token, and reported
      // E|SET and "paired" -- both ends calling it a success while the next C|
      // answers A|NOTOKEN. Worse on one real path: the portal can set a token in
      // NVS while this stack still holds none, and a bare `E|` then wiped the
      // token that had just been typed.
      if (!_pairOk || !_token.isEmpty() || line[2] == 0) {
        notifyLine("E|NO");
        return;
      }
      _pairOk = false;                  // single use
      _token = line + 2;                // live at once; the next C| will pass
      _grantReady = true;               // ...and the sketch writes it to NVS
      notifyLine("E|SET");
      note("paired");
      return;
    }
    if (!strncmp(line, "C|", 2)) {
      if (_token.isEmpty()) {
        // Answer before hanging up. A silent disconnect makes the daemon log a
        // bad handshake, which is indistinguishable from a crashed device or a
        // dropped packet -- and this device knew the exact reason all along.
        notifyLine("A|NOTOKEN");
        // Names the shortest route, for the same reason statusText() does.
        // The daemon's log says the same thing from the other end, and the
        // cable does it with no instruction at all when there is one.
        note("no token - claude-mate-connect");
        // AND DO NOT HANG UP. This line used to set _pendingDisconnect, which
        // made enrolment impossible in a way neither end could see: the daemon
        // logged that it had written E|? and the firmware never received a byte
        // of it, because the device had already dropped the connection the
        // offer was addressed to. "I have no token" is the one moment when
        // staying on the line matters most -- it is exactly when someone may be
        // about to give you one.
        //
        // Still bounded, and by rules that already existed: a daemon that is
        // not pairing answers A|NO and we drop on that, and one that says
        // nothing at all is dropped by the handshake timeout in poll().
        return;
      }
      char mac[65];
      hmacSha256Hex(_token.c_str(), line + 2, mac);
      char out[68];
      snprintf(out, sizeof(out), "A|%s", mac);
      notifyLine(out);
      return;
    }
    if (!strcmp(line, "A|OK")) { go(LINKED); return; }
    if (!strcmp(line, "A|NO")) {
      // "Rejected" is only true if we offered something. A device with no token
      // gets A|NO as the ordinary end of the exchange it just started, and
      // overwriting its own accurate reason with a wrong one would send someone
      // hunting for a token mismatch that does not exist.
      if (!_token.isEmpty()) note("token rejected");
      _pendingDisconnect = true;
      return;
    }
    // Anything else during the handshake: ignore, the same as the TCP path.
  }

  // ---- helpers --------------------------------------------------------------

  bool notifyLine(const char *line) {
    if (!_tx) return false;
    size_t n = strlen(line);
    if (n > sizeof(_out) - 2) return false;
    memcpy(_out, line, n);
    _out[n] = '\n';                 // the wire is line-oriented on both
    _tx->setValue(_out, n + 1);     // transports; a notify boundary is not a
    _tx->notify();                  // line boundary and must not be treated as
    return true;                    // one
  }

  void startBurst() {
    BLEDevice::startAdvertising();
    _advOn = true;
    _advSince = millis();
  }

  void disconnectPeer() {
    if (_server && _srvCb.connected) _server->disconnect(_server->getConnId());
    _srvCb.connected = false;
    reAdvertise();
  }

  void drop(const char *why) {
    note(why);
    reAdvertise();
  }

  // Back to the duty cycle. The ring is emptied on the way: whatever was left
  // in it belongs to a connection that no longer exists, and half a frame from
  // the previous daemon parsed against the next one is worse than nothing.
  void reAdvertise() {
    _lineLen = 0;
    _rxHead = _rxTail = 0;
    // An approval belongs to the connection it was given in. Carrying it across
    // would mean pressing GO for one peer and handing the next one that turns
    // up a token it never asked a human for.
    _pairAsk = false;
    _pairOk = false;
    _pairSpoke = false;
    go(ADVERTISING);
    startBurst();
  }

  void note(const char *why) {
    if (!why) return;
    snprintf(_dropWhy, sizeof(_dropWhy), "%s", why);
    // 0 is the "nothing has failed" sentinel, so a stamp of 0 must become
    // something else -- but never something LATER than now. `millis() | 1`
    // looks tidy and rounds UP on any even millis(), which makes the unsigned
    // age comparison underflow to ~4.29e9. That exact trick has been fixed
    // twice elsewhere in this firmware; it is not getting a third outing.
    unsigned long t = millis();
    _dropAt = t ? t : 1UL;
  }

  void go(State s) { _state = s; _stateSince = millis(); }

  static void hmacSha256Hex(const char *key, const char *msg, char out[65]) {
    uint8_t mac[32];
    const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    mbedtls_md_hmac(info, (const uint8_t *)key, strlen(key),
                    (const uint8_t *)msg, strlen(msg), mac);
    static const char hex[] = "0123456789abcdef";
    for (int i = 0; i < 32; i++) {
      out[i * 2]     = hex[mac[i] >> 4];
      out[i * 2 + 1] = hex[mac[i] & 0x0F];
    }
    out[64] = 0;
  }

  // ---- state ----------------------------------------------------------------
  State              _state = OFF;
  BLEServer         *_server = nullptr;
  BLECharacteristic *_rx = nullptr;
  BLECharacteristic *_tx = nullptr;
  SrvCb              _srvCb;
  RxCb               _rxCb;
  String             _token;

  volatile uint16_t _rxHead = 0;      // written by the BLE task
  volatile uint16_t _rxTail = 0;      // written by loop()
  uint8_t           _ring[BLE_RX_RING];

  char          _line[192];           // handshake line assembly
  uint8_t       _lineLen = 0;
  uint8_t       _out[200];            // one outbound line + its newline
  // Set on the BLE task, cleared in poll(). One flag, one writer each way.
  volatile bool _pendingDisconnect = false;
  // Enrolment. _pairAsk is set on the BLE task and cleared by the sketch;
  // _pairOk is set only by pairAnswer(), on the main loop. See handleAuthLine().
  volatile bool _pairAsk = false;
  volatile bool _pairOk = false;
  volatile bool _pairSpoke = false;    // a pairing question was answered here
  volatile bool _grantReady = false;   // a token arrived; NVS has not seen it
  // What begin() was asked for, kept so poll() can retry without the caller.
  char          _name[24] = {0};
  bool          _wantUp = false;
  uint8_t       _fails = 0;
  unsigned long _lastTry = 0;
  bool          _advOn = false;
  unsigned long _advSince = 0;
  unsigned long _stateSince = 0;
  char          _status[64] = {0};
  char          _dropWhy[48] = {0};
  unsigned long _dropAt = 0;
};
