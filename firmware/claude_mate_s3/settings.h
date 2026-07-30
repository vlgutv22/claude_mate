#pragma once
/*
 * settings.h -- the device's own preferences, and the only state in this
 * firmware that is neither the daemon's nor the network's.
 *
 * WHY THESE LIVE HERE AT ALL. The rule everywhere else is that the daemon owns
 * every word on the glass and this firmware is a dumb renderer. These settings
 * break none of that, because none of them is about a session: they are about
 * the piece of hardware -- how bright it is, when it turns its own screen off,
 * which way up it is. The daemon has no business knowing, and asking it would
 * mean the device could not be dimmed while the link is down. So: no protocol
 * change, no daemon change. The precedent is already in the tree -- the battery
 * gauge, the link glyph, the setup portal and the LINK LOST screen are all
 * firmware-local for the same reason.
 *
 * STORAGE. NVS via Preferences, in its OWN namespace, separate from netcfg's.
 * That separation is what lets a factory reset choose: wiping the UI settings
 * must not cost you the WiFi credentials and the token, since re-provisioning
 * needs a phone and a portal while re-picking a brightness needs one button.
 *
 * WRITES ARE DEFERRED. Every cycle of a setting is a flash write, and holding
 * NEXT through the brightness steps would be five of them in a second. NVS
 * tolerates that, but there is no reason to spend it: commit() runs from the
 * main loop once the value has been still for SETTINGS_COMMIT_MS. The live
 * value is applied immediately either way, so nothing feels deferred.
 */

#include <Preferences.h>

// factoryResetAll() clears netcfg's namespace as well as ours, so this header
// must come AFTER netcfg.h. Fail loudly at compile time rather than let a
// reordered include turn "factory reset" into "reset half of it".
#if !defined(NET_NS)
#error "settings.h must be included after netcfg.h (it needs NET_NS)"
#endif

#define UI_NS "mate-ui"            // NVS namespace; netcfg.h owns "claudemate"
#define SETTINGS_COMMIT_MS 1500UL  // value must be still this long before flash

// ---- Screen sleep -----------------------------------------------------------
// Index 0 is OFF, and that is the whole on/off switch: one row that reads
// "off / 1m / 2m / 5m / 10m / 30m" is strictly better than a separate toggle
// plus a duration, because the two can never disagree and it costs one row
// instead of two on a screen that only has five.
static const uint8_t HIB_MINS[] = {0, 1, 2, 5, 10, 30};
#define HIB_STEPS       (sizeof(HIB_MINS) / sizeof(HIB_MINS[0]))
#define HIB_DEFAULT_IDX 0          // off, so an update never blanks a screen
                                   // someone was relying on

// ---- Backlight --------------------------------------------------------------
// Not linear in duty. Perceived brightness is roughly logarithmic, so evenly
// spaced duty values feel like one enormous step at the bottom and four
// indistinguishable ones at the top. Index 3 is 200, which is what
// BL_BRIGHTNESS was as a compile-time constant, so a device that never visits
// this menu behaves exactly as before.
static const uint8_t BL_LEVELS[] = {20, 60, 120, 200, 255};
#define BL_STEPS        (sizeof(BL_LEVELS) / sizeof(BL_LEVELS[0]))
#define BL_DEFAULT_IDX  3

// ---- Alert LED --------------------------------------------------------------
// Index 0 is genuinely dark. A WS2812 strobing red at 7 Hz is the correct
// response to a failed turn at your desk and the wrong one in a bedroom, and
// the alert still arrives -- the screen keeps flashing the name row and the
// fleet letter, which is the same information without the light. Index 2 is 60,
// the old LED_BRIGHT.
static const uint8_t LED_LEVELS[] = {0, 20, 60, 140};
#define LED_STEPS       (sizeof(LED_LEVELS) / sizeof(LED_LEVELS[0]))
#define LED_DEFAULT_IDX 2

class MateSettings {
 public:
  void begin() {
    Preferences p;
    if (p.begin(UI_NS, true)) {
      _hib  = clampIdx(p.getUChar("hib",  HIB_DEFAULT_IDX), HIB_STEPS);
      _bl   = clampIdx(p.getUChar("bl",   BL_DEFAULT_IDX),  BL_STEPS);
      _led  = clampIdx(p.getUChar("led",  LED_DEFAULT_IDX), LED_STEPS);
      _flip = p.getBool("flip", false);
      p.end();
    }
    _dirty = 0;
  }

  // ---- live values the rest of the firmware reads ------------------------
  uint8_t  blDuty()     const { return BL_LEVELS[_bl]; }
  uint8_t  ledBright()  const { return LED_LEVELS[_led]; }
  bool     flipped()    const { return _flip; }
  bool     hibernates() const { return HIB_MINS[_hib] > 0; }
  uint32_t hibernateMs() const {
    return (uint32_t)HIB_MINS[_hib] * 60000UL;
  }

  // ---- indices + labels, for the menu -----------------------------------
  uint8_t hibIdx() const { return _hib; }
  uint8_t blIdx()  const { return _bl;  }
  uint8_t ledIdx() const { return _led; }

  const char *hibLabel() const {
    static char buf[6];
    if (!HIB_MINS[_hib]) return "off";
    snprintf(buf, sizeof(buf), "%um", HIB_MINS[_hib]);
    return buf;
  }
  const char *ledLabel() const {
    static const char *n[] = {"off", "low", "med", "high"};
    return _led < LED_STEPS ? n[_led] : "?";
  }

  // ---- mutators. Each bumps _dirty; commit() does the flash later --------
  void cycleHib() { _hib = (uint8_t)((_hib + 1) % HIB_STEPS); touch(); }
  void cycleBl()  { _bl  = (uint8_t)((_bl  + 1) % BL_STEPS);  touch(); }
  void cycleLed() { _led = (uint8_t)((_led + 1) % LED_STEPS); touch(); }
  void toggleFlip() { _flip = !_flip; touch(); }

  // Called every loop. Writes only once the value has settled, so a thumb held
  // on NEXT costs one flash write rather than one per step.
  void commit() {
    if (!_dirty || (millis() - _dirty) < SETTINGS_COMMIT_MS) return;
    write();
  }

  // Write now, pending or not -- used on the path to a reboot or to deep sleep,
  // where the deferred commit would never get its chance to run.
  void flush() { if (_dirty) write(); }

  // Everything: both namespaces. "Factory reset" that left the WiFi
  // credentials behind would be a lie, and the menu row says so before it asks
  // for the confirming hold.
  void factoryResetAll() {
    Preferences p;
    if (p.begin(UI_NS, false))  { p.clear(); p.end(); }
    if (p.begin(NET_NS, false)) { p.clear(); p.end(); }
  }

 private:
  static uint8_t clampIdx(uint8_t v, size_t n) {
    return v < n ? v : 0;          // a value from a future build reads as 0
  }                                // rather than indexing off the end
  void touch() { _dirty = millis() | 1UL; }   // never 0: 0 means "clean"

  void write() {
    _dirty = 0;                    // cleared first: a failed begin() must not
    Preferences p;                 // leave us retrying the write every loop
    if (!p.begin(UI_NS, false)) return;
    p.putUChar("hib", _hib);
    p.putUChar("bl",  _bl);
    p.putUChar("led", _led);
    p.putBool("flip", _flip);
    p.end();
  }

  uint8_t       _hib  = HIB_DEFAULT_IDX;
  uint8_t       _bl   = BL_DEFAULT_IDX;
  uint8_t       _led  = LED_DEFAULT_IDX;
  bool          _flip = false;
  unsigned long _dirty = 0;
};
