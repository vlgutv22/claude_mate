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
// The protocol's longest real line is ~94 bytes and the mirror sends 17 of them
// in one burst. A ring this size holds a whole mirror frame plus the status
// frame behind it, so a busy moment never truncates a row on the glass.
#define BLE_RX_RING       2048

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
  bool begin(const char *name, const String &token) {
    if (_state != OFF) return true;
    _token = token;
    if (!BLEDevice::getInitialized()) BLEDevice::init(name);
    // 185 leaves the whole longest line in one ATT write. At the default 23 the
    // daemon would have to split every frame across four writes, and a split
    // that lands mid-line is fine (we reassemble on newlines) but pointless.
    BLEDevice::setMTU(185);

    _server = BLEDevice::createServer();
    if (!_server) { shutdown(); return false; }
    _server->setCallbacks(&_srvCb);

    BLEService *svc = _server->createService(BLEUUID(BLE_SVC_UUID));
    if (!svc) { shutdown(); return false; }

    _rx = svc->createCharacteristic(BLE_RX_UUID,
                                    BLECharacteristic::PROPERTY_WRITE |
                                    BLECharacteristic::PROPERTY_WRITE_NR);
    _tx = svc->createCharacteristic(BLE_TX_UUID,
                                    BLECharacteristic::PROPERTY_NOTIFY);
    if (!_rx || !_tx) { shutdown(); return false; }
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

    go(ADVERTISING);
    startBurst();
    return true;
  }

  // Advance the duty cycle and time the handshake out. Never blocks -- which is
  // the quiet advantage over the Wi-Fi path, where MDNS.queryService() and the
  // TCP dial both stop the loop (and therefore the button poll) for most of a
  // second every retry.
  void poll() {
    if (_state == OFF) return;

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

    if (_state == AUTHING && (now - _stateSince) > BLE_AUTH_TIMEOUT) {
      // Say why before dropping. A daemon too old to know this service exists
      // will still connect -- macOS caches GATT and opens the link happily --
      // then sit there, and from the glass that is indistinguishable from a
      // wrong token.
      note("no handshake - is the daemon on --ble?");
      disconnectPeer();
    }
  }

  // ---- the link surface (identical in shape to MateNet's) -------------------

  bool connected() const { return _state == LINKED; }
  State state() const { return _state; }

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
      case ADVERTISING: return "ble: start the daemon with --ble";
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
    if (!strncmp(line, "C|", 2)) {
      if (_token.isEmpty()) {
        // Answer before hanging up. A silent disconnect makes the daemon log a
        // bad handshake, which is indistinguishable from a crashed device or a
        // dropped packet -- and this device knew the exact reason all along.
        notifyLine("A|NOTOKEN");
        note("no token - set one in the setup portal");
        _pendingDisconnect = true;
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
      note("token rejected");
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
  bool          _advOn = false;
  unsigned long _advSince = 0;
  unsigned long _stateSince = 0;
  char          _status[64] = {0};
  char          _dropWhy[48] = {0};
  unsigned long _dropAt = 0;
};
