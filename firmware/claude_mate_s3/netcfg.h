/*
 * Claude Mate - wireless transport for the ESP32-S3 companion
 * ==========================================================
 *
 * Carries the daemon<->device line protocol over TCP instead of a USB cable, so
 * the companion can sit on a desk across the room on a battery. The bytes are
 * IDENTICAL to the serial ones (F| / V| / P down, H / K / B| up); only the pipe
 * changes. Everything transport-shaped lives here so the sketch stays about the
 * protocol and the UI.
 *
 * WHY TCP AND NOT BLUETOOTH: the Mac side needs no new dependency to accept a
 * socket (the daemon is pure-Python sockets already), a BLE central on macOS
 * would mean CoreBluetooth and a real dependency, and WiFi reaches the whole
 * home/office rather than one room. The protocol is line-oriented, which maps
 * onto a stream socket with nothing lost.
 *
 * THE DEVICE DIALS OUT. The daemon's address is stable and advertised over
 * mDNS; a DHCP device's address is not. Dialling out also means the device
 * needs no inbound reachability and no port forwarding.
 *
 * CONNECT SEQUENCE (each step is non-blocking; poll() advances one state):
 *
 *   SETUP        no WiFi credentials (or BOOT held at power-on): run a SoftAP
 *                + captive portal so the user can enter them from a phone
 *   JOINING      WiFi.begin() with the stored credentials
 *   DISCOVERING  no host configured -> browse mDNS for _claudemate._tcp
 *   DIALING      open the TCP connection
 *   AUTHING      answer the daemon's nonce challenge:
 *
 *                    daemon -> us   C|<nonce>
 *                    us -> daemon   A|<hex HMAC-SHA256(token, nonce)>
 *                    daemon -> us   A|OK        (or A|NO and we are dropped)
 *
 *                The shared token never crosses the wire, and the nonce is
 *                fresh per connection, so a sniffed handshake is worthless.
 *   LINKED       the protocol flows; read()/write() are live
 *
 * Any failure drops back a state and retries with a bounded backoff -- a device
 * on a battery must recover from a rebooted router or a stopped daemon on its
 * own, with nobody there to press anything.
 *
 * CONFIG lives in NVS (Preferences, namespace "claudemate") and survives
 * reflashing the sketch. It is written either by the setup portal or by the
 * config lines the sketch accepts on USB serial (see the sketch header).
 */

#pragma once

#include <WiFi.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <mbedtls/md.h>

// ---- tuning -----------------------------------------------------------------
#define NET_NS            "claudemate"   // NVS namespace
#define NET_DEFAULT_PORT  8787
#define NET_JOIN_TIMEOUT  20000UL   // give up on an SSID after this and retry
// BOTH of these are time the main loop is STOPPED, not time it waits in the
// background: TCPClient::connect() and MDNS.queryService() are blocking calls
// made from poll(), which is called from loop(), which also polls the buttons.
// While one is in flight the firmware cannot see a button at all -- a press and
// release inside the window is dropped, not delayed. Keep them short.
#define NET_DIAL_TIMEOUT  600       // per TCP connect attempt (ms). A LAN
                                    // connect answers in single-digit ms; 1500
                                    // only ever bought us dead air.
#define NET_RETRY_MS      2000UL    // between dial / discovery attempts...
// ...doubling each consecutive failure up to this. A daemon that is off stays
// off for hours (the Mac is asleep, or you carried the device to another room),
// and retrying every 2 s for hours means blocking the loop every 2 s for hours
// -- sluggish to use and a pointless drain on the cell. Success resets it.
#define NET_RETRY_MAX_MS  30000UL
#define NET_AUTH_TIMEOUT  5000UL    // the daemon allows 5 s; so do we
#define NET_DISCOVER_MS   4000UL    // between mDNS browses
// An UNFINISHED portal gives up after this and goes back to joining -- but ONLY
// on a device that already has credentials to go back to. Opening the portal is
// now one menu item away, so opening it by accident, or changing your mind, is
// easy; without a timeout that strands a cordless device in SETUP with no
// daemon link and no button that exits, recoverable only by a power cycle. An
// UNPROVISIONED device keeps the portal up indefinitely, because for it there
// is nothing to fall back to and the portal is the entire point.
#define NET_PORTAL_TIMEOUT 300000UL  // 5 minutes
#define NET_AP_PREFIX     "Claude-Mate-"

class MateNet {
 public:
  enum State : uint8_t {
    OFF,          // no credentials and no portal (WiFi disabled)
    SETUP,        // SoftAP + captive portal is up
    JOINING,      // associating with the AP
    DISCOVERING,  // looking for the daemon over mDNS
    DIALING,      // opening the TCP connection
    AUTHING,      // nonce challenge in flight
    LINKED,       // protocol is flowing
  };

  // ---- lifecycle ------------------------------------------------------------

  // `forcePortal` comes from BOOT being held at power-on: it re-runs setup even
  // when credentials are already stored (how you move the device to a new WiFi).
  void begin(bool forcePortal) {
    loadConfig();
    WiFi.persistent(false);        // we own the credentials; don't let the SDK
                                   // keep a second stale copy in its own NVS
    WiFi.setAutoReconnect(true);
    if (forcePortal || _ssid.isEmpty()) {
      startPortal();
    } else {
      startJoin();
    }
  }

  // Advance the state machine. Must be called every loop(); never blocks for
  // more than one connect attempt.
  void poll() {
    switch (_state) {
      case SETUP:       pollPortal();     break;
      case JOINING:     pollJoin();       break;
      // The two states that BLOCK. While the user is driving a firmware-local
      // screen, reconnection waits: a menu that drops every other button press
      // is worse than a link that comes back a few seconds later, and the user
      // is right there watching, so the moment they leave the menu it resumes.
      // Nothing else is paused -- an established link keeps flowing, a join
      // keeps progressing, the portal keeps serving.
      case DISCOVERING: if (!_holdReconnect) pollDiscover(); break;
      case DIALING:     if (!_holdReconnect) pollDial();     break;
      case AUTHING:     pollAuth();       break;
      case LINKED:      pollLinked();     break;
      case OFF:         break;
    }
  }

  // Set by the sketch whenever the UI is not on the CONDUCTOR view. See poll().
  //
  // Leaving the menu RESETS the backoff and retries at once. Someone who has
  // just been in Settings is standing over the device wanting it to work, and
  // making them wait out a 30 s gap they cannot see would read as the fix not
  // having taken.
  void holdReconnect(bool hold) {
    if (_holdReconnect && !hold) { _fails = 0; _lastTry = 0; }
    _holdReconnect = hold;
  }

  // ---- the link surface (what the sketch uses) ------------------------------

  bool connected() const { return _state == LINKED; }

  bool write(const char *line) {
    if (_state != LINKED) return false;
    size_t n = strlen(line);
    if (_client.write((const uint8_t *)line, n) != n) { drop("write failed"); return false; }
    if (_client.write((uint8_t)'\n') != 1)            { drop("write failed"); return false; }
    return true;
  }

  int read() {                     // one byte, or -1 when nothing is waiting
    if (_state != LINKED) return -1;
    return _client.available() ? _client.read() : -1;
  }

  State state() const { return _state; }

  // ---- what the screen shows about us --------------------------------------

  // How long a failure reason stays on the glass before the state text takes
  // over again. Long enough to walk back to the device and read it, short
  // enough that a stale message never masks a link that has since recovered.
  static const unsigned long DROP_SHOW_MS = 20000UL;

  // One short line for the bottom of the display: honest about where we are.
  const char *statusText() {
    // A recent failure outranks the state, because "authenticating..." on a
    // loop tells you nothing and "no token configured" tells you everything.
    if (_dropAt && (millis() - _dropAt) < DROP_SHOW_MS && _state != LINKED) {
      snprintf(_status, sizeof(_status), "x %s", _dropWhy);
      return _status;
    }
    switch (_state) {
      case OFF:         return "wifi off - usb only";
      case SETUP:       return "setup: join the AP shown";
      case JOINING:     snprintf(_status, sizeof(_status), "joining %s", _ssid.c_str());
                        return _status;
      case DISCOVERING: return "looking for daemon...";
      case DIALING:     snprintf(_status, sizeof(_status), "dialing %s:%u",
                                 _host.isEmpty() ? _foundIp.toString().c_str()
                                                 : _host.c_str(), _port);
                        return _status;
      case AUTHING:     return "authenticating...";
      case LINKED:      snprintf(_status, sizeof(_status), "wifi %s",
                                 WiFi.localIP().toString().c_str());
                        return _status;
    }
    return "";
  }

  // Portal details, for the SETUP screen.
  const char *apName()  const { return _apName; }
  const char *apPass()  const { return _apPass; }
  String      apIp()    const { return WiFi.softAPIP().toString(); }
  bool        hasToken() const { return !_token.isEmpty(); }
  bool        configured() const { return !_ssid.isEmpty(); }
  int8_t      rssi()    const { return WiFi.RSSI(); }

  // ---- configuration (also driven by USB config lines) ---------------------

  void setWifi(const String &ssid, const String &pass) {
    _ssid = ssid; _pass = pass;
    save("ssid", _ssid); save("pass", _pass);
  }
  void setDaemon(const String &host, uint16_t port) {
    _host = host; _port = port ? port : NET_DEFAULT_PORT;
    save("host", _host);
    Preferences p; if (p.begin(NET_NS, false)) { p.putUShort("port", _port); p.end(); }
  }
  void setToken(const String &token) { _token = token; save("token", _token); }

  void wipe() {
    Preferences p;
    if (p.begin(NET_NS, false)) { p.clear(); p.end(); }
  }

  // A dump for the USB console. Deliberately never prints the token itself --
  // only whether one is set -- so a console log cannot leak it.
  void printConfig(Print &out) {
    out.printf("ssid  : %s\n", _ssid.isEmpty() ? "(unset)" : _ssid.c_str());
    out.printf("pass  : %s\n", _pass.isEmpty() ? "(unset)" : "(set)");
    out.printf("host  : %s\n", _host.isEmpty() ? "(mDNS discovery)" : _host.c_str());
    out.printf("port  : %u\n", _port);
    out.printf("token : %s\n", _token.isEmpty() ? "(unset)" : "(set)");
    // The driver's own verdict, verbatim. Without it a device that will never
    // join and a device that is merely slow are indistinguishable over serial.
    out.printf("wifi  : status=%d %s\n", (int)WiFi.status(),
               WiFi.status() == WL_CONNECTED ? "connected" : joinFailWhy());
    if (_dropAt) out.printf("last  : %s (%lus ago)\n", _dropWhy,
                            (unsigned long)((millis() - _dropAt) / 1000));
    out.printf("state : %s\n", stateName());
    if (WiFi.status() == WL_CONNECTED)
      out.printf("ip    : %s  rssi %d dBm\n",
                 WiFi.localIP().toString().c_str(), WiFi.RSSI());
  }

  const char *stateName() const {
    switch (_state) {
      case OFF: return "OFF";                 case SETUP: return "SETUP";
      case JOINING: return "JOINING";         case DISCOVERING: return "DISCOVERING";
      case DIALING: return "DIALING";         case AUTHING: return "AUTHING";
      case LINKED: return "LINKED";
    }
    return "?";
  }

  // Restart the link from the top (after a config change).
  void restart() {
    _client.stop();
    if (_state == SETUP) stopPortal();
    if (_ssid.isEmpty()) { _state = OFF; return; }
    WiFi.disconnect();
    startJoin();
  }

  void startPortalNow() { startPortal(); }

  // What the radio can actually see, with the configured SSID called out.
  // Blocking for a couple of seconds, which is acceptable: it is a diagnostic
  // typed by a human at a serial console, not something the loop calls.
  void scanTo(Print &out) {
    out.println("scanning...");
    int n = cleanScan();
    if (n <= 0) {
      out.printf("  (no networks found, n=%d) -- if this repeats with a phone "
                 "hotspot right beside the device, suspect the radio, not the "
                 "air.\n", n);
      resumeAfterScan();
      return;
    }
    bool sawOurs = false;
    for (int i = 0; i < n && i < 30; i++) {
      bool ours = (!_ssid.isEmpty() && WiFi.SSID(i) == _ssid);
      sawOurs |= ours;
      out.printf("  %-32s %4d dBm  ch%-3d %s%s\n",
                 WiFi.SSID(i).c_str(), (int)WiFi.RSSI(i), (int)WiFi.channel(i),
                 WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "open " : "psk  ",
                 ours ? " <- configured" : "");
    }
    if (!_ssid.isEmpty() && !sawOurs)
      out.printf("  NOT VISIBLE: '%s'. Wrong band (this radio is 2.4 GHz only),"
                 " out of range, or hidden.\n", _ssid.c_str());
    WiFi.scanDelete();
    resumeAfterScan();
  }

  // Hand the radio back to whoever owned it before the scan.
  //
  // NOT unconditionally startJoin(): that calls WiFi.mode(WIFI_STA), which
  // tears down a running softAP. Typing `Y` while the setup portal was up
  // therefore killed the portal out from under the phone looking at it -- the
  // diagnostic destroying the thing it was meant to help diagnose. In SETUP the
  // portal owns the radio and cleanScan() only ever dropped the STA
  // association, so there is nothing to restore.
  void resumeAfterScan() {
    if (_state == SETUP) return;
    startJoin();
  }

  // Close the link and power the radio down, for deep sleep. Dropping the TCP
  // connection POLITELY matters: a half-open socket leaves the daemon holding a
  // dead client, still listing this device as present, until its own timeout
  // eventually notices. Turning the radio off also removes the largest current
  // draw before sleeping, which is the point of the exercise.
  void shutdown() {
    _client.stop();
    if (_state == SETUP) stopPortal();
    WiFi.disconnect(true);          // true: also switch the radio off
    WiFi.mode(WIFI_OFF);
    _state = OFF;
  }

 private:
  // ---- stored config -------------------------------------------------------
  String   _ssid, _pass, _host, _token;
  uint16_t _port = NET_DEFAULT_PORT;

  // ---- runtime -------------------------------------------------------------
  State         _state = OFF;
  WiFiClient    _client;
  IPAddress     _foundIp;
  uint16_t      _foundPort = 0;
  unsigned long _stateSince = 0;
  unsigned long _lastTry = 0;
  char          _status[64] = {0};
  char          _dropWhy[48] = {0};   // why the last connection attempt failed
  unsigned long _dropAt = 0;          // ...and when (0 = nothing has failed)
  unsigned long _portalTouched = 0;   // last portal page load, for the timeout
  uint8_t       _fails = 0;           // consecutive dial/discover failures
  bool          _holdReconnect = false;  // UI is busy; do not block the loop
  char          _line[192];        // handshake line assembly
  uint8_t       _lineLen = 0;

  // ---- portal --------------------------------------------------------------
  WebServer  *_web = nullptr;
  DNSServer  *_dns = nullptr;
  char        _apName[32] = {0};
  char        _apPass[16] = {0};

  void go(State s) { _state = s; _stateSince = millis(); }

  // ---- NVS -----------------------------------------------------------------

  void loadConfig() {
    Preferences p;
    if (!p.begin(NET_NS, true)) return;      // never written yet
    _ssid  = p.getString("ssid", "");
    _pass  = p.getString("pass", "");
    _host  = p.getString("host", "");
    _token = p.getString("token", "");
    _port  = p.getUShort("port", NET_DEFAULT_PORT);
    p.end();
  }

  static void save(const char *key, const String &val) {
    Preferences p;
    if (p.begin(NET_NS, false)) { p.putString(key, val); p.end(); }
  }

  // ---- WiFi join -----------------------------------------------------------

  void startJoin() {
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);             // startPortal() turns this off
    WiFi.setSleep(true);                     // modem sleep: the point of a battery
    WiFi.begin(_ssid.c_str(), _pass.isEmpty() ? nullptr : _pass.c_str());
    go(JOINING);
  }

  void pollJoin() {
    if (WiFi.status() == WL_CONNECTED) {
      MDNS.begin("claude-mate");             // also lets you reach us by name
      go(_host.isEmpty() ? DISCOVERING : DIALING);
      _lastTry = 0;                          // dial immediately
      return;
    }
    if (millis() - _stateSince > NET_JOIN_TIMEOUT) {
      // "Wrong password, AP out of range, router rebooting -- all look the same
      // from here" was true of the retry, but NOT of the reason: the driver
      // distinguishes them and we were throwing that away, so a device that
      // could never join just said "joining ..." forever. Retry identically,
      // but keep what the driver told us -- it is the difference between
      // retyping a password and moving the device closer to the router.
      note(joinFailWhy());
      WiFi.disconnect();
      startJoin();
    }
  }

  // The one thing that actually distinguishes the join failures.
  static const char *joinFailWhy() {
    switch (WiFi.status()) {
      case WL_NO_SSID_AVAIL:  return "network not found";
      case WL_CONNECT_FAILED: return "wrong wifi password?";
      case WL_CONNECTION_LOST: return "wifi connection lost";
      case WL_IDLE_STATUS:    return "wifi idle - retrying";
      default:                return "cannot join wifi";
    }
  }

  // ---- daemon discovery ----------------------------------------------------

  void pollDiscover() {
    if (WiFi.status() != WL_CONNECTED) { startJoin(); return; }
    if (millis() - _lastTry < retryGap(NET_DISCOVER_MS) && _lastTry) return;
    _lastTry = millis();
    // No timeout parameter exists on this core's queryService, so this one
    // cannot be shortened through the API -- which is exactly why the backoff
    // and the menu hold below matter: they reduce how OFTEN we pay it.
    int n = MDNS.queryService("claudemate", "tcp");
    if (n > 0) {
      _fails = 0;
      _foundIp   = MDNS.address(0);
      _foundPort = MDNS.port(0);
      go(DIALING);
      _lastTry = 0;
    } else {
      bumpFail();
    }
  }

  // ---- dial + handshake ----------------------------------------------------

  void pollDial() {
    if (WiFi.status() != WL_CONNECTED) { startJoin(); return; }
    if (_lastTry && millis() - _lastTry < retryGap(NET_RETRY_MS)) return;
    _lastTry = millis();

    bool ok;
    if (_host.isEmpty()) {
      if (!_foundPort) { go(DISCOVERING); return; }   // lost the mDNS answer
      ok = _client.connect(_foundIp, _foundPort, NET_DIAL_TIMEOUT);
    } else {
      ok = _client.connect(_host.c_str(), _port, NET_DIAL_TIMEOUT);
    }
    if (!ok) {
      bumpFail();
      // A host that stops answering may have moved: re-browse rather than
      // hammering a dead address forever.
      if (_host.isEmpty()) { _foundPort = 0; go(DISCOVERING); }
      return;
    }
    _fails = 0;                    // reaching the daemon resets the backoff
    _client.setNoDelay(true);      // button presses are tiny; never coalesce them
    _lineLen = 0;
    go(AUTHING);
  }

  // Exponential backoff on consecutive failures, capped. Each attempt costs the
  // main loop a blocking connect or mDNS query, so the gap between them is a
  // responsiveness budget as much as a network one.
  //
  // The cap is applied to the RESULT, not to the shift count. Capping the shift
  // alone would have let the discovery gap reach 4000<<4 = 64 s -- meaning up to
  // a minute before a device noticed the daemon had come back, which is the
  // wrong side of the trade. Both paths now top out at NET_RETRY_MAX_MS exactly,
  // so the comment and the behaviour agree.
  unsigned long retryGap(unsigned long base) const {
    unsigned long g = base << (_fails > 4 ? 4 : _fails);
    return g > NET_RETRY_MAX_MS ? NET_RETRY_MAX_MS : g;
  }
  void bumpFail() { if (_fails < 4) _fails++; }

  void pollAuth() {
    if (!_client.connected()) { drop("closed during handshake"); return; }
    if (millis() - _stateSince > NET_AUTH_TIMEOUT) { drop("handshake timeout"); return; }

    // Read strictly a line at a time: the daemon may pipeline the first frame
    // right behind A|OK, and reading past the newline would swallow it.
    while (_client.available()) {
      int c = _client.read();
      if (c < 0) return;
      if (c == '\n' || c == '\r') {
        if (_lineLen == 0) continue;             // tolerate CRLF
        _line[_lineLen] = 0;
        _lineLen = 0;
        if (!handleAuthLine(_line)) return;      // dropped or done
        if (_state == LINKED) return;            // hand the rest to the sketch
      } else if (_lineLen < sizeof(_line) - 1) {
        _line[_lineLen++] = (char)c;
      } else {
        _lineLen = 0;                            // oversized: resync
      }
    }
  }

  // Returns false once the connection is gone; sets LINKED on success.
  bool handleAuthLine(const char *line) {
    if (!strncmp(line, "C|", 2)) {
      if (_token.isEmpty()) {
        // SAY SO before hanging up. Closing the socket silently made the daemon
        // log "bad handshake None", which is indistinguishable from a crashed
        // device, a truncated read or a network glitch -- and the device knew
        // the exact answer the whole time. A|NOTOKEN cannot be mistaken for a
        // real MAC (a hex digest never contains these letters at this length),
        // and a daemon too old to recognise it just reports a rejected token,
        // which is still better than nothing.
        _client.print("A|NOTOKEN\n");
        _client.flush();
        drop("no token - set one in the setup portal");
        return false;
      }
      char mac[65];
      hmacSha256Hex(_token.c_str(), line + 2, mac);
      _client.printf("A|%s\n", mac);
      return true;
    }
    if (!strcmp(line, "A|OK")) {
      go(LINKED);
      return true;
    }
    if (!strcmp(line, "A|NO")) { drop("token rejected"); return false; }
    return true;                    // anything else during auth: ignore
  }

  void pollLinked() {
    if (!_client.connected() && !_client.available()) { drop("link closed"); return; }
    if (WiFi.status() != WL_CONNECTED) { drop("wifi lost"); return; }
  }

  // A scan that actually returns results.
  //
  // A bare WiFi.scanNetworks() returns 0 whenever an association attempt is in
  // flight -- and one usually is, from pollJoin()'s retry or from the ESP32's
  // own background auto-reconnect, which stays armed even in AP_STA mode. The
  // symptom is the worst kind: not an error, but an empty list, which reads as
  // "there are no networks here" and sends you diagnosing the radio, or the
  // room, or your router. It bit the serial diagnostic first and the SETUP
  // PORTAL second -- and the portal is where it matters, because an empty
  // network dropdown is the one thing that makes the device unprovisionable.
  //
  // disconnect(false, false) drops the association only: the radio stays
  // powered, the stored credentials survive, and a running softAP (the portal
  // the user's phone is currently looking at) is untouched.
  int cleanScan() {
    WiFi.scanDelete();                 // free any previous results first
    WiFi.disconnect(false /*wifioff*/, false /*eraseap*/);
    delay(150);
    return WiFi.scanNetworks(false /*async*/, true /*show hidden*/);
  }

  // Record a failure reason without tearing the socket down. drop() is for
  // "the link died"; this is for "an attempt failed and we are retrying".
  void note(const char *why) {
    if (!why) return;
    snprintf(_dropWhy, sizeof(_dropWhy), "%s", why);
    unsigned long t = millis();
    _dropAt = t ? t : 1UL;
  }

  void drop(const char *why) {
    // Keep the reason. It used to be discarded, which meant every failure --
    // wrong token, no token, daemon not listening, wifi gone -- presented
    // identically as the device quietly cycling back to DIALING. From the far
    // side of the room that reads as "it just doesn't work", and the one thing
    // the user needed to know (which of four things is wrong) was the thing
    // being thrown away.
    // 0 is the "no error" sentinel, so a stamp of 0 must become something else
    // -- but NOT `millis() | 1`, which rounds UP on any even millis() and makes
    // the unsigned age comparison underflow to ~4.29e9. That exact trick has
    // already been fixed twice in this firmware. See note().
    note(why);
    _client.stop();
    _lineLen = 0;
    _lastTry = millis();            // honour the backoff before redialling
    go(WiFi.status() == WL_CONNECTED
           ? (_host.isEmpty() ? DISCOVERING : DIALING)
           : JOINING);
    if (_state == JOINING) startJoin();
  }

  // ---- HMAC-SHA256 (mbedtls, already in the SDK) ---------------------------

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

  // ---- setup portal --------------------------------------------------------
  // A SoftAP with a captive portal, so a device with no keyboard can be given
  // WiFi credentials from a phone. The AP password is random per session and
  // shown on the LCD: an open AP would let any passer-by rewrite the config.

  void startPortal() {
    uint8_t mac[6];
    WiFi.macAddress(mac);
    snprintf(_apName, sizeof(_apName), "%s%02X%02X", NET_AP_PREFIX, mac[4], mac[5]);
    uint32_t r = esp_random();
    snprintf(_apPass, sizeof(_apPass), "%08u", (unsigned)(r % 100000000u));

    WiFi.mode(WIFI_AP_STA);          // AP_STA so the portal can still scan
    // ...but stop the STA fighting it. The ESP32 re-associates in the
    // background on its own, and any attempt in flight makes scanNetworks()
    // return zero -- which showed up as an empty network dropdown, the one
    // failure that makes the device impossible to provision.
    WiFi.setAutoReconnect(false);
    WiFi.disconnect(false, false);
    WiFi.softAP(_apName, _apPass);

    _dns = new DNSServer();
    _dns->start(53, "*", WiFi.softAPIP());     // catch-all -> captive portal

    _web = new WebServer(80);
    _web->on("/", HTTP_GET, [this]() { serveForm(); });
    _web->on("/save", HTTP_POST, [this]() { serveSave(); });
    _web->onNotFound([this]() { serveForm(); });   // any URL opens the portal
    _web->begin();
    _portalTouched = millis();
    go(SETUP);
  }

  void stopPortal() {
    if (_web) { _web->stop(); delete _web; _web = nullptr; }
    if (_dns) { _dns->stop(); delete _dns; _dns = nullptr; }
    WiFi.softAPdisconnect(true);
  }

  void pollPortal() {
    if (_dns) _dns->processNextRequest();
    if (_web) _web->handleClient();
    // Self-heal an abandoned portal. Only when there is something to go back
    // to: an unprovisioned device must keep it up, since the portal is the only
    // way it will ever be configured. The clock is reset by every page load, so
    // a user who is mid-setup with the form open is never timed out from under
    // them -- only a portal nobody is looking at expires.
    if (configured() && (millis() - _portalTouched) > NET_PORTAL_TIMEOUT) {
      note("setup timed out - rejoining");
      stopPortal();
      startJoin();
    }
  }

  void serveForm() {
    _portalTouched = millis();          // someone is here; do not time out
    // Scanning blocks for a couple of seconds, which is fine: in SETUP there is
    // no link to keep alive and the user is waiting on this page anyway.
    // cleanScan(), not a bare scanNetworks(): see the note there. An empty
    // dropdown here is fatal -- it is the one failure that makes the device
    // impossible to provision, and it looks like "there is no wifi" rather than
    // like a bug.
    int n = cleanScan();
    String opts;
    uint8_t shown = 0;
    for (int i = 0; i < n && shown < 20; i++) {
      String s = WiFi.SSID(i);
      if (s.isEmpty()) continue;          // hidden SSID: nothing to select
      opts += "<option value='" + s + "'>" + s + " (" + WiFi.RSSI(i) + " dBm)</option>";
      shown++;
    }
    // Never serve a form you cannot submit. If the scan came back empty the
    // dropdown alone is a dead end, so offer a text box instead and say what
    // happened -- the network may simply be hidden, or on 5 GHz.
    bool manual = (shown == 0);
    String html =
        F("<!doctype html><meta charset=utf-8>"
          "<meta name=viewport content='width=device-width,initial-scale=1'>"
          "<title>Claude Mate setup</title><style>"
          "body{font:16px system-ui;margin:0;padding:24px;background:#0b0d10;color:#e8eaed}"
          "h1{font-size:20px;margin:0 0 4px}p{color:#9aa0a6;margin:0 0 20px;font-size:14px}"
          "label{display:block;margin:14px 0 4px;font-size:13px;color:#9aa0a6}"
          "input,select{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;"
          "border:1px solid #2a2f36;background:#15181d;color:#e8eaed;font-size:16px}"
          "button{margin-top:22px;width:100%;padding:13px;border:0;border-radius:8px;"
          "background:#35c4f0;color:#06212b;font-size:16px;font-weight:600}"
          "small{color:#6b7280;display:block;margin-top:6px;font-size:12px}"
          "</style><h1>Claude Mate</h1><p>Point this companion at your daemon.</p>"
          "<form method=POST action=/save>"
          "<label>Network</label>");
    if (manual) {
      html += F("<input name=ssid autocomplete=off placeholder='type the network name'>"
                "<small>No networks were seen in this scan. Type the name - it may "
                "be hidden, or on 5 GHz, which this radio cannot see. Reload to "
                "scan again.</small>");
    } else {
      html += "<select name=ssid>" + opts + "</select>";
    }
    html += F(
              "<label>Password</label><input name=pass type=password autocomplete=off>"
              "<label>Shared token</label><input name=token autocomplete=off ");
    // Tell the user what state the token is in and what to type. "Must match
    // the daemon's CLAUDE_MATE_TOKEN" is only useful advice if you already know
    // you were supposed to have one -- and the commonest way to arrive here is
    // not knowing that at all.
    if (hasToken()) {
      html += F("placeholder='already set - leave blank to keep it'>"
                "<small>A token is stored. Leave this blank unless you are "
                "changing it.</small>"
                "<label style='display:flex;gap:8px;align-items:center;margin-top:10px'>"
                "<input type=checkbox name=cleartoken value=1 style='width:auto'>"
                "Erase the stored token</label>");
    } else {
      html += F("placeholder='paste it from the Mac'>"
                "<small><b>No token stored yet.</b> On the Mac, run the daemon "
                "with <code>--tcp</code> once: it creates one and prints it. "
                "Or read it with<br><code>cat ~/.config/claude-mate/token</code>"
                "</small>");
    }
    html += F("<label>Daemon host <em>(optional)</em></label><input name=host placeholder='found automatically'>"
              "<small>Leave empty to discover it over mDNS.</small>"
              "<label>Port</label><input name=port value='8787'>"
              "<button type=submit>Save &amp; connect</button></form>");
    _web->send(200, "text/html", html);
  }

  void serveSave() {
    _portalTouched = millis();
    String ssid  = _web->arg("ssid");
    String pass  = _web->arg("pass");
    String token = _web->arg("token");
    String host  = _web->arg("host");
    uint16_t port = (uint16_t)_web->arg("port").toInt();
    if (ssid.isEmpty()) { _web->send(400, "text/plain", "network required"); return; }
    setWifi(ssid, pass);
    // AN EMPTY TOKEN BOX MEANS "KEEP THE ONE I HAVE", NOT "ERASE IT".
    //
    // This used to write unconditionally, and it was the single worst bug in
    // the setup flow: the obvious thing to do when you come back to the portal
    // to change networks is to fill in the wifi password and nothing else --
    // which silently wiped the token, after which the device could never
    // complete the handshake and simply said it was not connected. The
    // documentation warned about it ("re-enter it every time"), which is an
    // admission that the behaviour was wrong, not a fix for it.
    //
    // Clearing a token is still possible, deliberately and explicitly, via the
    // checkbox or `X|WIPE` over serial.
    if (_web->arg("cleartoken") == "1") setToken("");
    else if (!token.isEmpty())          setToken(token);
    setDaemon(host, port);
    _web->send(200, "text/html",
               F("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                 "<style>body{font:16px system-ui;background:#0b0d10;color:#e8eaed;padding:24px}"
                 "</style><h1>Saved</h1><p>Connecting... watch the screen.</p>"));
    _pendingApply = millis();       // let the response actually flush first
  }

  unsigned long _pendingApply = 0;

 public:
  // Called from the sketch's loop: applies a portal submission once the HTTP
  // response has gone out (tearing the AP down mid-response would leave the
  // phone showing a connection error and the user unsure whether it worked).
  void applyPendingConfig() {
    if (!_pendingApply || millis() - _pendingApply < 600) return;
    _pendingApply = 0;
    stopPortal();
    restart();
  }
};
