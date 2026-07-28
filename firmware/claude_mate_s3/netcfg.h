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
#define NET_DIAL_TIMEOUT  1500      // per TCP connect attempt (ms)
#define NET_RETRY_MS      2000UL    // between dial / discovery attempts
#define NET_AUTH_TIMEOUT  5000UL    // the daemon allows 5 s; so do we
#define NET_DISCOVER_MS   4000UL    // between mDNS browses
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
      case DISCOVERING: pollDiscover();   break;
      case DIALING:     pollDial();       break;
      case AUTHING:     pollAuth();       break;
      case LINKED:      pollLinked();     break;
      case OFF:         break;
    }
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

  // One short line for the bottom of the display: honest about where we are.
  const char *statusText() {
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
      // Wrong password, AP out of range, router rebooting -- all look the same
      // from here, so just start over rather than guessing.
      WiFi.disconnect();
      startJoin();
    }
  }

  // ---- daemon discovery ----------------------------------------------------

  void pollDiscover() {
    if (WiFi.status() != WL_CONNECTED) { startJoin(); return; }
    if (millis() - _lastTry < NET_DISCOVER_MS && _lastTry) return;
    _lastTry = millis();
    int n = MDNS.queryService("claudemate", "tcp");
    if (n > 0) {
      _foundIp   = MDNS.address(0);
      _foundPort = MDNS.port(0);
      go(DIALING);
      _lastTry = 0;
    }
  }

  // ---- dial + handshake ----------------------------------------------------

  void pollDial() {
    if (WiFi.status() != WL_CONNECTED) { startJoin(); return; }
    if (_lastTry && millis() - _lastTry < NET_RETRY_MS) return;
    _lastTry = millis();

    bool ok;
    if (_host.isEmpty()) {
      if (!_foundPort) { go(DISCOVERING); return; }   // lost the mDNS answer
      ok = _client.connect(_foundIp, _foundPort, NET_DIAL_TIMEOUT);
    } else {
      ok = _client.connect(_host.c_str(), _port, NET_DIAL_TIMEOUT);
    }
    if (!ok) {
      // A host that stops answering may have moved: re-browse rather than
      // hammering a dead address forever.
      if (_host.isEmpty()) { _foundPort = 0; go(DISCOVERING); }
      return;
    }
    _client.setNoDelay(true);      // button presses are tiny; never coalesce them
    _lineLen = 0;
    go(AUTHING);
  }

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
      if (_token.isEmpty()) { drop("daemon wants a token, none configured"); return false; }
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

  void drop(const char *why) {
    (void)why;
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
    WiFi.softAP(_apName, _apPass);

    _dns = new DNSServer();
    _dns->start(53, "*", WiFi.softAPIP());     // catch-all -> captive portal

    _web = new WebServer(80);
    _web->on("/", HTTP_GET, [this]() { serveForm(); });
    _web->on("/save", HTTP_POST, [this]() { serveSave(); });
    _web->onNotFound([this]() { serveForm(); });   // any URL opens the portal
    _web->begin();
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
  }

  void serveForm() {
    // Scanning blocks for a couple of seconds, which is fine: in SETUP there is
    // no link to keep alive and the user is waiting on this page anyway.
    int n = WiFi.scanNetworks();
    String opts;
    for (int i = 0; i < n && i < 20; i++) {
      String s = WiFi.SSID(i);
      if (s.isEmpty()) continue;
      opts += "<option value='" + s + "'>" + s + " (" + WiFi.RSSI(i) + " dBm)</option>";
    }
    String html =
        F("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
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
          "<label>Network</label><select name=ssid>");
    html += opts;
    html += F("</select>"
              "<label>Password</label><input name=pass type=password autocomplete=off>"
              "<label>Shared token</label><input name=token autocomplete=off>"
              "<small>Must match the daemon's CLAUDE_MATE_TOKEN.</small>"
              "<label>Daemon host <em>(optional)</em></label><input name=host placeholder='found automatically'>"
              "<small>Leave empty to discover it over mDNS.</small>"
              "<label>Port</label><input name=port value='8787'>"
              "<button type=submit>Save &amp; connect</button></form>");
    _web->send(200, "text/html", html);
  }

  void serveSave() {
    String ssid  = _web->arg("ssid");
    String pass  = _web->arg("pass");
    String token = _web->arg("token");
    String host  = _web->arg("host");
    uint16_t port = (uint16_t)_web->arg("port").toInt();
    if (ssid.isEmpty()) { _web->send(400, "text/plain", "network required"); return; }
    setWifi(ssid, pass);
    setToken(token);
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
