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
// P2P only: the DHCP lease table of our own AP, which is how we learn the one
// address we need. WiFi.h does not expose it -- softAPgetStationNum() counts
// stations but will not tell you where they are.
#include <esp_wifi.h>
#include <esp_wifi_ap_get_sta_list.h>

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

// ---- which radio carries the link -------------------------------------------
// Stored here rather than with the UI settings, and deliberately: this is a
// LINK setting, so it belongs in the namespace a factory reset treats as
// provisioning -- alongside the SSID and the token, which it is useless
// without. Wiping the brightness must not silently move a device back onto
// Wi-Fi, and re-provisioning already means visiting the portal.
//
// A stored byte rather than a compile-time #define because the issue this
// implements (#21) asks for exactly that: one binary that can be either, so a
// device on a desk can be moved onto BLE without a cable and a toolchain.
// LINK_P2P is Wi-Fi with the infrastructure taken out. Instead of joining a
// router, the DEVICE is the access point and the Mac joins IT: one AP, one
// station, nothing else on the segment and no third party in the path. The
// device reads the Mac's DHCP lease off its own AP and dials it on the usual
// port, so every byte above the socket -- the nonce handshake, the line
// protocol, all of it -- is the same code as LINK_WIFI. See startAP().
//
// WHY IT EXISTS: LINK_WIFI needs the credentials of a network you both trust,
// which is a non-starter on a guest network, a locked-down corporate SSID, or
// anywhere you would simply rather not put a keypad's password. P2P needs no
// network at all.
//
// WHAT IT COSTS: the Mac has one Wi-Fi radio, so while it is on the device's AP
// it is not on yours. That is the trade, it is not fixable from this side, and
// docs/USING.md says so plainly rather than letting people discover it.
// LINK_CABLE is "no radio for the LINK": the USB serial link carries everything,
// which
// it already does in every other mode -- config lines, token provisioning, the
// boot H. So this mode does not start anything, it declines to. On a desk where
// the device is plugged into the Mac it is running against, that is the whole
// link, and neither radio is started for it. (The BLE gamepad is a separate
// role on a separate switch -- turn that on and the BLE controller comes up for
// the pad, which is a choice the user made about a different thing.)
//
// LINK_AUTO is a MODE, NEVER AN EFFECTIVE TRANSPORT. It is stored, and it is
// resolved to one of the real values at boot by resolveTransport(); nothing
// downstream ever compares against it. Keeping it out of the effective set is
// what stops every `transport == LINK_x` site in the sketch from having to know
// that a fifth value exists.
enum MateTransport : uint8_t {
  LINK_WIFI = 0, LINK_BLE = 1, LINK_P2P = 2, LINK_CABLE = 3, LINK_AUTO = 4
};

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
    HOSTING,      // P2P: our AP is up, waiting for the Mac to join it
  };

  // APPENDED, NOT INSERTED. Nothing persists a State, but the sketch compares
  // them and blelink mirrors the shape, so renumbering the existing values to
  // put HOSTING "in order" would be a silent, wide behaviour change for a
  // cosmetic gain.

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

  // Come up as the access point instead of joining one -- the P2P transport.
  //
  // Deliberately a separate entry point rather than a flag on begin(): begin()
  // is about credentials (have them -> join, lack them -> portal) and P2P has
  // no concept of either. Sharing one function would have meant threading a
  // mode through every credential branch it owns.
  void beginP2P() {
    loadConfig();                  // the token, the port, and what `?` prints
    WiFi.persistent(false);
    _p2pMode = true;
    startAP();
  }

  // Advance the state machine. Must be called every loop(); never blocks for
  // more than one connect attempt.
  void poll() {
    switch (_state) {
      case SETUP:       pollPortal();     break;
      case JOINING:     pollJoin();       break;
      // HOSTING is cheap -- it reads a lease table, it never blocks -- so
      // unlike DIALING it is not gated on _holdReconnect. The gate exists to
      // keep blocking calls out of the button poll; there is nothing to gate.
      case HOSTING:     pollHost();       break;
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
      // In P2P the interesting address is the PEER's, not ours: ours is always
      // 192.168.4.1 and tells the user nothing they can act on.
      case LINKED:      if (_p2pMode) {
                          snprintf(_status, sizeof(_status), "p2p %s",
                                   _foundIp.toString().c_str());
                        } else {
                          snprintf(_status, sizeof(_status), "wifi %s",
                                   WiFi.localIP().toString().c_str());
                        }
                        return _status;
      // The one screen that has to carry instructions: nothing happens in P2P
      // until a human joins this network from the Mac, and the SSID is the
      // whole of what they need. The password sits on the SETUP-style panel.
      case HOSTING:     snprintf(_status, sizeof(_status), "p2p: join %s",
                                 _apName);
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

  // "There is another transport that works, so this portal is not the only way
  // out of here." Set by the sketch, because only the sketch knows.
  void setFallbackLink(bool has) { _fallbackLink = has; }

  // The shared secret, for the transport that is not this one. Both radios
  // authenticate the same way against the same token, so there is exactly one
  // to provision and exactly one to get wrong.
  const String &token() const { return _token; }

  // Read the config WITHOUT associating. On a BLE device the join never
  // happens, but `?` must still print what is provisioned, the setup portal
  // must still be reachable from the menu, and blelink needs the token -- all
  // of which live in this object.
  //
  // persistent(false) is set here as well as in begin(), and has to be: it is
  // what stops the SDK keeping its own second copy of the credentials in its
  // own NVS, and a device that switches to Wi-Fi later would otherwise
  // associate from a stale copy nothing in this file wrote.
  void loadConfigOnly() {
    loadConfig();
    WiFi.persistent(false);
  }

  // Put the Wi-Fi radio down and leave it down, for a build that is never going
  // to use it.
  //
  // BE HONEST ABOUT WHAT THIS SAVES. Nothing here starts the Wi-Fi driver on a
  // BLE build -- loadConfigOnly() only reads NVS and sets a flag -- so on a
  // clean boot this is close to a no-op, and it is NOT the reason the battery
  // lasts (the backlight is, by an order of magnitude; see the hibernate note).
  // What it buys is certainty and one less thing to reason about: the radio is
  // off because something turned it off, not because we believe nobody turned
  // it on. It also matters on the path that HAS started it -- a BOOT-held
  // portal that timed out, or a transport switched at runtime -- where the
  // driver is genuinely up and holding the shared 2.4 GHz radio that BLE wants.
  void radioOff() {
    WiFi.mode(WIFI_OFF);
    _state = OFF;
  }

  // ---- transport selection -------------------------------------------------
  // Static, because they are read in setup() BEFORE anything decides whether
  // this instance is going to be started at all.
  // AN UNPROVISIONED DEVICE DEFAULTS TO BLE, and that is a bootstrap fix, not a
  // preference.
  //
  // A factory reset wipes this namespace: no SSID, no token, and no stored
  // transport. Defaulting that to Wi-Fi meant begin() found no SSID and opened
  // the setup portal -- which outranks every screen in render(), blocks the menu
  // in fourthDoubleTap(), and only ever times out on a device that HAS
  // credentials to fall back to. A factory-reset device was therefore locked in
  // the Wi-Fi portal permanently, with SETTINGS -> Link visible to nobody. You
  // could not choose BLE because choosing anything required getting past a
  // screen that existed only to configure Wi-Fi.
  //
  // So: if there is nothing to join, come up on the radio that needs nothing to
  // join. If an SSID IS stored, this device was provisioned for Wi-Fi by someone
  // and keeps behaving as it always did -- an upgrade must never move a working
  // device onto a different radio behind its owner's back. An explicit setting
  // always wins over both.
  static MateTransport storedTransport() {
    Preferences p;
    if (!p.begin(NET_NS, true)) return LINK_BLE;   // nothing written ever
    uint8_t v = p.getUChar("link", 0xFF);          // 0xFF = never set
    bool haveSsid = !p.getString("ssid", "").isEmpty();
    p.end();
    if (v == LINK_BLE)   return LINK_BLE;
    if (v == LINK_WIFI)  return LINK_WIFI;
    // P2P needs no SSID, so unlike LINK_WIFI it is honoured on a device that
    // has never been given credentials -- which is the normal state of a device
    // that only ever uses P2P.
    if (v == LINK_P2P)   return LINK_P2P;
    if (v == LINK_CABLE) return LINK_CABLE;
    if (v == LINK_AUTO)  return LINK_AUTO;
    return haveSsid ? LINK_WIFI : LINK_BLE;        // a value from a future
  }                                                // build falls here too

  // The MODE resolved to something the rest of the firmware can switch on.
  // AUTO is the only value that needs resolving, and it resolves ONCE, at boot,
  // in setup() -- see the note on LINK_AUTO.
  //
  // isPlugged() is SOF-based: it reports that a computer is talking to the USB
  // peripheral, not merely that 5 V is present on the cable. A charger in a wall
  // socket is therefore not mistaken for a Mac, which is the one confusion that
  // would make AUTO pick the cable and then link to nothing.
  static MateTransport resolveTransport(bool usbHost) {
    MateTransport mode = storedTransport();
    if (mode != LINK_AUTO) return mode;
    if (usbHost) return LINK_CABLE;
    MateTransport radio = storedRadio();
    // AUTO MUST OBEY THE SAME GATE THE MENU DOES. The Connection row refuses to
    // offer Wi-Fi to a device with no network, because such a device reboots
    // into a portal that outranks the menu -- the whole trap the row was
    // designed around. AUTO could walk straight past that gate: pick wifi while
    // credentials exist, wipe them later, and every boot from then on resolves
    // to a Wi-Fi transport with nothing to join. The gate belongs to the
    // decision, not to the row that happens to present it.
    if (radio == LINK_WIFI && !hasSsid()) return LINK_BLE;
    return radio;
  }

  static bool hasSsid() {
    Preferences p;
    if (!p.begin(NET_NS, true)) return false;
    bool has = !p.getString("ssid", "").isEmpty();
    p.end();
    return has;
  }

  // Which radio AUTO falls back to when there is no host on the cable. Written
  // whenever the user picks a radio explicitly, so "auto" means "the cable when
  // it is there, otherwise what I last chose" rather than a fixed guess.
  static MateTransport storedRadio() {
    Preferences p;
    if (!p.begin(NET_NS, true)) return LINK_BLE;
    uint8_t v = p.getUChar("radio", 0xFF);
    if (v != LINK_WIFI && v != LINK_P2P && v != LINK_BLE) {
      // NEVER WRITTEN, which is the state of every device that predates this
      // key -- including one that has been happily on Wi-Fi for months. Falling
      // straight to BLE would move it off its own network the moment its owner
      // chose AUTO, which is the opposite of what "auto" promises.
      uint8_t l = p.getUChar("link", 0xFF);
      if (l == LINK_WIFI || l == LINK_P2P) {
        v = l;
      } else if (!p.getString("ssid", "").isEmpty()) {
        // AND "link" IS NOT ENOUGH EITHER. A board provisioned through the
        // setup portal never wrote "link" -- it reaches Wi-Fi through
        // storedTransport()'s `haveSsid ? LINK_WIFI : LINK_BLE`, which is the
        // rule this has to match or the fallback misses exactly the devices it
        // was added for. One rule, stated in both places.
        v = LINK_WIFI;
      }
    }
    p.end();
    if (v == LINK_WIFI || v == LINK_P2P) return (MateTransport)v;
    return LINK_BLE;              // the cordless default, and the cheap one
  }

  static void storeTransport(MateTransport t) {
    Preferences p;
    if (p.begin(NET_NS, false)) {
      p.putUChar("link", (uint8_t)t);
      // Remember the radio behind an explicit choice, so a later switch to AUTO
      // returns to it rather than to a default the user never picked.
      if (t == LINK_BLE || t == LINK_WIFI || t == LINK_P2P)
        p.putUChar("radio", (uint8_t)t);
      p.end();
    }
  }

  static const char *transportName(MateTransport t) {
    switch (t) {
      case LINK_BLE:   return "ble";
      case LINK_P2P:   return "p2p";
      case LINK_CABLE: return "cable";
      case LINK_AUTO:  return "auto";
      default:         return "wi-fi";
    }
  }

  // ---- "open the portal after the next reboot" -----------------------------
  // A ONE-SHOT, and the one-shot is the safety property. The settings row that
  // sets this reboots immediately; setup() reads it and CLEARS IT BEFORE the
  // portal opens, so a power cut with the portal on screen -- or a portal
  // nobody finishes -- costs one ordinary boot, not a device that comes back
  // into the portal forever with the row that set it hidden behind it.
  //
  // WHY A REBOOT AT ALL, rather than just calling startPortalNow(). The portal
  // is a SoftAP, so it wants the 2.4 GHz radio that BLE is holding on a BLE
  // device, and BLE cannot be brought back up after a teardown in the same boot
  // (see blelink.h). Opening the portal in place would therefore cost the
  // device its link until the next power cycle -- which is precisely the
  // "one press, no way back" trap the Link row was removed for. Rebooting
  // reaches the portal the same way BOOT-held does: instead of the other
  // transport, never alongside it.
  static void requestPortalOnBoot() {
    Preferences p;
    if (p.begin(NET_NS, false)) { p.putUChar("portal1", 1); p.end(); }
  }
  static bool takePortalRequest() {
    Preferences p;
    if (!p.begin(NET_NS, false)) return false;
    bool want = p.getUChar("portal1", 0) == 1;
    if (want) p.remove("portal1");           // consumed, whatever happens next
    p.end();
    return want;
  }

  void wipe() {
    Preferences p;
    if (p.begin(NET_NS, false)) { p.clear(); p.end(); }
  }

  // A dump for the USB console. Deliberately never prints the token itself --
  // only whether one is set -- so a console log cannot leak it.
  void printConfig(Print &out, MateTransport effective = LINK_AUTO) {
    out.printf("ssid  : %s\n", _ssid.isEmpty() ? "(unset)" : _ssid.c_str());
    out.printf("pass  : %s\n", _pass.isEmpty() ? "(unset)" : "(set)");
    out.printf("host  : %s\n", _host.isEmpty() ? "(mDNS discovery)" : _host.c_str());
    out.printf("port  : %u\n", _port);
    out.printf("token : %s\n", _token.isEmpty() ? "(unset)" : "(set)");
    // The stored MODE, and -- when that mode is AUTO -- what it actually chose
    // this boot. Printing only "auto" makes the one mode that decides for you
    // the one mode you cannot diagnose over the console you reach for when it
    // is behaving oddly.
    MateTransport mode = storedTransport();
    if (mode == LINK_AUTO && effective != LINK_AUTO)
      out.printf("link  : auto -> %s\n", transportName(effective));
    else
      out.printf("link  : %s\n", transportName(mode));
    if (_p2pMode) {
      // Everything a human needs to finish the job, on the console they are
      // already looking at: the network to join, the password to type, and
      // whether the Mac is on it yet. The AP password is printed in full and
      // that is deliberate -- unlike the token it is not a capability (it
      // guards one keypad's private segment), and a P2P device that will not
      // tell you its password cannot be connected to at all.
      out.printf("ap    : %s  pass %s\n", _apName, _apPass);
      out.printf("peers : %u\n", (unsigned)WiFi.softAPgetStationNum());
      IPAddress peer;
      out.printf("mac   : %s\n",
                 peerAddress(peer) ? peer.toString().c_str() : "(not joined)");
      out.printf("state : %s\n", stateName());
      return;                    // the STA lines below are all meaningless here
    }
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

  // The join instructions for P2P, printable BEFORE the AP is up.
  //
  // Called from `I|P2P`, which reboots immediately afterwards, so it must not
  // depend on any radio state -- p2pCredentials() only reads (and on the first
  // ever call, writes) NVS. That also means the SSID and password printed here
  // are exactly the ones the device will come back up with.
  void printP2PInvite(Print &out) {
    p2pCredentials();
    out.println("p2p: after the reboot, join this network from the Mac:");
    out.printf("  network : %s\n", _apName);
    out.printf("  password: %s\n", _apPass);
    out.println("  then the device dials the daemon on its own.");
    if (_token.isEmpty())
      out.println("  WARNING: no token set. Send T|<token> or the daemon will "
                  "reject the link.");
  }

  const char *stateName() const {
    switch (_state) {
      case OFF: return "OFF";                 case SETUP: return "SETUP";
      case JOINING: return "JOINING";         case DISCOVERING: return "DISCOVERING";
      case DIALING: return "DIALING";         case AUTHING: return "AUTHING";
      case LINKED: return "LINKED";           case HOSTING: return "HOSTING";
    }
    return "?";
  }

  // Restart the link from the top (after a config change).
  void restart() {
    _client.stop();
    // P2P first, and before every credential test below it: none of them apply
    // to a device that is its own network. This is also what makes linkStart()
    // in the sketch work unchanged -- it calls restart() for anything that is
    // not BLE, and the mode is remembered here rather than passed in.
    if (_p2pMode) { startAP(); return; }
    // NOTHING TO JOIN MEANS THE PORTAL, not silence -- the same rule begin()
    // follows, and it has to be the same or the two disagree about what an
    // unprovisioned device does.
    //
    // This used to stop the portal, see no SSID and drop to OFF. An
    // unprovisioned Wi-Fi device is a device sitting IN the portal, because
    // that is where begin() put it, so any config write -- `T|<token>` typed at
    // exactly the device that needs one -- tore down the only screen that could
    // finish the job and left no link, no portal and no way back but a reboot.
    // Found by doing it to a real board.
    //
    // Tested on _web rather than on SETUP because applyPendingConfig() calls
    // stopPortal() before this and stopPortal() does not touch the state: the
    // state is the intent, the pointer is whether a portal is actually being
    // served. Getting that backwards leaves a SETUP screen advertising an AP
    // that no longer exists.
    if (_ssid.isEmpty()) {
      // ...unless this device has a link that is not Wi-Fi. A BLE board that
      // saved a token through the portal and no network -- the route this
      // firmware added and advertises -- came straight back to a REGENERATED
      // portal: _state never left SETUP, so the sketch's "SETUP ended, start
      // BLE" handoff never fired, the phone was kicked off an AP whose password
      // had changed, and resubmitting just repeated it. pollPortal() consults
      // _fallbackLink for exactly this device; this path did not.
      if (_fallbackLink) { stopPortal(); radioOff(); return; }
      if (!_web) startPortal();
      return;
    }
    if (_state == SETUP) stopPortal();
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
    // An AP is not torn down by disconnect(), which is about the STA. Without
    // this the "radio off" path left our own network advertising itself into an
    // empty room for the whole of a deep sleep -- the single largest current
    // draw on the board, on a device that had just been told to save power.
    if (_p2pMode) WiFi.softAPdisconnect(true);
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
  bool          _fallbackLink = false;  // see setFallbackLink()
  bool          _p2pMode = false;     // we are the AP, not a station (LINK_P2P)
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

  // ---- P2P: we are the access point ----------------------------------------

  // Bring up our own AP and wait. No STA, no scan, no mDNS: in P2P the only
  // other machine on the segment is the one we want, and its address comes from
  // our own DHCP server rather than from a discovery protocol.
  //
  // WIFI_AP, not the portal's WIFI_AP_STA. The portal needs the STA half to
  // scan for networks to show you; P2P never joins one, and leaving the STA up
  // just gives the driver a second thing to do with a radio it is already
  // using as an AP.
  void startAP() {
    p2pCredentials();
    WiFi.mode(WIFI_AP);
    WiFi.softAP(_apName, _apPass);
    _lastTry = 0;
    go(HOSTING);
  }

  // The AP's SSID and password, generated ONCE and kept.
  //
  // NOT the portal's random-per-session password. That is right for a captive
  // portal you visit once from a phone, and wrong here: the Mac saves this
  // network and is expected to rejoin it automatically at every login, which a
  // password that changes on every boot would break on the first reboot. The
  // SSID keeps the portal's MAC suffix so a desk with two devices on it still
  // shows two distinct networks.
  void p2pCredentials() {
    Preferences p;
    String ssid, pass;
    if (p.begin(NET_NS, false)) {
      ssid = p.getString("apssid", "");
      pass = p.getString("appass", "");
      // 8 characters is not a style rule, it is the WPA2 minimum -- softAP()
      // silently falls back to an OPEN network on a shorter one, which would
      // put an unauthenticated AP on the desk without saying so.
      if (ssid.isEmpty() || pass.length() < 8) {
        uint8_t mac[6];
        WiFi.macAddress(mac);
        char n[sizeof(_apName)], q[sizeof(_apPass)];
        snprintf(n, sizeof(n), "%s%02X%02X", NET_AP_PREFIX, mac[4], mac[5]);
        snprintf(q, sizeof(q), "%08u", (unsigned)(esp_random() % 100000000u));
        ssid = n;
        pass = q;
        p.putString("apssid", ssid);
        p.putString("appass", pass);
      }
      p.end();
    }
    snprintf(_apName, sizeof(_apName), "%s", ssid.c_str());
    snprintf(_apPass, sizeof(_apPass), "%s", pass.c_str());
  }

  // Wait for the Mac to associate AND pick up a lease, then dial it.
  //
  // Association and address are two separate events and the gap between them is
  // real (a DHCP exchange, plus whatever macOS spends deciding the network is
  // usable). Dialling on association alone means connecting to 0.0.0.0.
  void pollHost() {
    IPAddress peer;
    if (!peerAddress(peer)) return;       // nobody joined yet, or no lease yet
    _foundIp   = peer;
    _foundPort = _port;
    _fails     = 0;
    _lastTry   = 0;                       // dial immediately
    go(DIALING);
  }

  // The address our DHCP server handed the one station on our AP.
  //
  // Read rather than assumed. 192.168.4.2 is what the pool hands out first and
  // would be right almost every time -- but "almost" here means a device that
  // silently dials the wrong host after a lease churn, with a status line
  // claiming it is dialling the Mac. The API costs two stack structs.
  bool peerAddress(IPAddress &out) const {
    wifi_sta_list_t sta;
    if (esp_wifi_ap_get_sta_list(&sta) != ESP_OK || sta.num <= 0) return false;
    wifi_sta_mac_ip_list_t ips;
    if (esp_wifi_ap_get_sta_list_with_ip(&sta, &ips) != ESP_OK) return false;
    for (int i = 0; i < ips.num; i++) {
      uint32_t ip = ips.sta[i].ip.addr;
      if (ip) { out = IPAddress(ip); return true; }
    }
    return false;                          // associated, lease not issued yet
  }

  bool apUp() const { return _p2pMode && WiFi.softAPgetStationNum() > 0; }

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
    // EVERY "not connected" TEST IN THIS FILE IS ABOUT THE STA. In P2P there is
    // no STA to be connected: WiFi.status() is WL_DISCONNECTED forever and the
    // unguarded version of this line called startJoin() -- WIFI_STA, which
    // tears our own AP down -- on the first poll after the AP came up. The
    // equivalent question for an AP is whether anyone is still associated.
    if (_p2pMode) {
      if (WiFi.softAPgetStationNum() == 0) { go(HOSTING); return; }
    } else if (WiFi.status() != WL_CONNECTED) {
      startJoin();
      return;
    }
    if (_lastTry && millis() - _lastTry < retryGap(NET_RETRY_MS)) return;
    _lastTry = millis();

    bool ok;
    // In P2P the peer is whatever our DHCP server just leased, so a stored host
    // (left over from a Wi-Fi provisioning, or set by S|) must NOT win here --
    // it would name an address that does not exist on this segment.
    if (_p2pMode || _host.isEmpty()) {
      if (!_foundPort) { go(_p2pMode ? HOSTING : DISCOVERING); return; }
      ok = _client.connect(_foundIp, _foundPort, NET_DIAL_TIMEOUT);
    } else {
      ok = _client.connect(_host.c_str(), _port, NET_DIAL_TIMEOUT);
    }
    if (!ok) {
      bumpFail();
      // A host that stops answering may have moved: re-browse rather than
      // hammering a dead address forever. In P2P "moved" means a new lease, so
      // the equivalent is to go back and re-read the lease table.
      if (_p2pMode) { _foundPort = 0; go(HOSTING); return; }
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
    // THE STA TEST IS NOT THE P2P TEST. WiFi.status() only ever describes the
    // station interface, and a P2P device has none -- it reports
    // WL_DISCONNECTED for its entire life. Left unguarded this dropped the link
    // on the first poll after A|OK, every time, so P2P authenticated
    // successfully and then tore itself down in the same breath. The equivalent
    // liveness question for an AP is whether the Mac is still associated.
    if (_p2pMode) {
      if (WiFi.softAPgetStationNum() == 0) drop("peer left the network");
      return;
    }
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
    if (_p2pMode) {
      // Our AP does not go down because the daemon hung up. Stay hosting and
      // redial the same peer; if the Mac itself left, pollDial() sees the empty
      // station list and comes back here anyway.
      go(HOSTING);
      return;
    }
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
    // ...or a device that has a DIFFERENT link to fall back to. The original
    // rule was "only time out if credentials are stored", because for an
    // unprovisioned Wi-Fi device the portal is the only thing there is. A BLE
    // device breaks that assumption: it has a working link and no need of this
    // screen, so leaving the portal up forever would strand it exactly the way
    // the rule was written to prevent.
    if ((configured() || _fallbackLink) &&
        (millis() - _portalTouched) > NET_PORTAL_TIMEOUT) {
      // Two endings, because "rejoining" is only true when there is something to
      // rejoin. The fallback-link device that lands here has no SSID at all: it
      // is on BLE and got here by holding BOOT at power-on. startJoin() would
      // hand WiFi.begin() an empty string, put the STA back up on the one radio
      // the other transport wants, and leave a status line naming a network that
      // does not exist. The radio goes OFF instead, and the sketch sees SETUP
      // end and hands the glass back to BLE.
      if (configured()) {
        note("setup timed out - rejoining");
        stopPortal();
        startJoin();
      } else {
        note("setup timed out");
        shutdown();
      }
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
          "<form method=POST action=/save>");
    // Say so when the network is optional. On a device whose link is BLE this
    // page is only ever visited for the token, and a required-looking field it
    // will never use is how you end up typing a fake network name.
    html += _fallbackLink
                ? F("<label>Network <em>(optional - this device is on BLE)</em></label>")
                : F("<label>Network</label>");
    if (manual) {
      html += F("<input name=ssid autocomplete=off placeholder='type the network name'>"
                "<small>No networks were seen in this scan. Type the name - it may "
                "be hidden, or on 5 GHz, which this radio cannot see. Reload to "
                "scan again.</small>");
    } else {
      // ...and give the dropdown a way to say "none", first and selected. A
      // <select> always submits something, so without this a BLE user setting a
      // token would silently store whichever network happened to top the scan.
      html += "<select name=ssid>";
      if (_fallbackLink)
        html += F("<option value=''>(none - leave the Wi-Fi config alone)</option>");
      html += opts + "</select>";
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
    // PREFILLED FROM WHAT IS STORED, both of them. A form that shows blanks for
    // fields it is about to overwrite is a form that erases whatever you do not
    // retype -- and the commonest visit here re-enters one field and leaves the
    // rest alone. The token box is the exception and says so, because a secret
    // should not be echoed back into a page.
    html += F("<label>Daemon host <em>(optional)</em></label><input name=host "
              "placeholder='found automatically' value='");
    html += _host;
    html += F("'><small>Leave empty to discover it over mDNS.</small>"
              "<label>Port</label><input name=port value='");
    html += String(_port);
    html += F("'><button type=submit>Save &amp; connect</button></form>");
    _web->send(200, "text/html", html);
  }

  void serveSave() {
    _portalTouched = millis();
    String ssid  = _web->arg("ssid");
    String pass  = _web->arg("pass");
    String token = _web->arg("token");
    String host  = _web->arg("host");
    uint16_t port = (uint16_t)_web->arg("port").toInt();
    // "Network required" is only true when the network is what you came for. On
    // a device whose link is BLE this page is a TOKEN form and nothing else --
    // there is no Wi-Fi anywhere in that device's path -- and rejecting an
    // otherwise complete submission over the one field it will never use made
    // the no-cable route a 400, leaving "type a fake network name" as the way
    // through. Refuse only a submission that would save nothing at all.
    bool clearing = _web->arg("cleartoken") == "1";
    if (ssid.isEmpty() && token.isEmpty() && !clearing) {
      _web->send(400, "text/plain", "fill in a network, a token, or both");
      return;
    }
    if (!ssid.isEmpty()) setWifi(ssid, pass);
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
    if (clearing)              setToken("");
    else if (!token.isEmpty()) setToken(token);
    // Safe to write unconditionally ONLY because the form now round-trips both
    // fields -- see serveForm(). It did not: the host input had no prefill and
    // the port input carried a literal 8787, so a visit that only re-entered a
    // token (the expected visit on a BLE device, now that an empty network box
    // is no longer a 400) submitted an empty host and the default port. That
    // silently erased an explicitly configured daemon address and dropped the
    // device back to the mDNS discovery it had been configured to avoid.
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
