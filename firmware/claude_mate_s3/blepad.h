// -----------------------------------------------------------------------------
// blepad.h -- the device as a standard Bluetooth HID gamepad.
//
// WHY THIS EXISTS. The daemon-driven controller mode works, and on a cable it
// works well: measured end to end, the Mac-side path (serial read -> dispatch ->
// SSE -> browser) is 0.32 ms median. But on battery it is a long chain of things
// that can be unhealthy -- Wi-Fi association, an mDNS browse, a TCP dial, a
// daemon, an SSE stream -- and when the link flaps, MDNS.queryService() (no
// timeout parameter exists on this core) and a 600 ms TCP dial BLOCK the main
// loop, so the buttons are not even sampled. A gamepad that freezes for most of
// a second every few seconds is not a gamepad.
//
// Over HID none of that chain exists: device -> BLE -> macOS -> Gamepad API ->
// browser. It also removes the one limitation the SSE path can never fix -- a
// page served over https cannot reach http://127.0.0.1, so the public site
// could never use the device. The OS-level gamepad works on any origin.
//
// COST, measured rather than guessed: a minimal HID gamepad spike compiled to
// 567,242 bytes against a 276,775 byte empty sketch, so the stack costs about
// 290 KB of flash and 5.7 KB of RAM. Against a 3 MB app partition that is
// comfortable.
//
// THE RADIO. The S3 has ONE 2.4 GHz radio, shared. Running BLE and Wi-Fi at once
// degrades both, so the sketch parks Wi-Fi while you are actually playing and
// brings it back when you stop -- see the pad-idle policy in the .ino. This file
// owns only the HID side and deliberately knows nothing about that decision.
// -----------------------------------------------------------------------------
#pragma once

#include <BLEDevice.h>
#include <BLEHIDDevice.h>
#include <BLESecurity.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <HIDTypes.h>

// Four buttons in one byte, no axes. A button-only gamepad is legal HID and is
// what the Gamepad API surfaces; adding axes would mean inventing analogue
// values for switches that do not have them. Report ID 1.
//
// Button order is the PHYSICAL order, so the mapping stays obvious at both ends:
//   1 = PREV   2 = GO   3 = NEXT   4 = 4th
static const uint8_t BLEPAD_REPORT_MAP[] = {
  0x05, 0x01,        // Usage Page (Generic Desktop)
  0x09, 0x05,        // Usage (Game Pad)
  0xA1, 0x01,        // Collection (Application)
  0x85, 0x01,        //   Report ID (1)
  0x05, 0x09,        //   Usage Page (Button)
  0x19, 0x01,        //   Usage Minimum (Button 1)
  0x29, 0x04,        //   Usage Maximum (Button 4)
  0x15, 0x00,        //   Logical Minimum (0)
  0x25, 0x01,        //   Logical Maximum (1)
  0x75, 0x01,        //   Report Size (1)
  0x95, 0x04,        //   Report Count (4)
  0x81, 0x02,        //   Input (Data, Variable, Absolute)
  0x75, 0x04,        //   Report Size (4)      -- pad the byte out
  0x95, 0x01,        //   Report Count (1)
  0x81, 0x03,        //   Input (Constant)     -- padding, never read
  0xC0               // End Collection
};

class BlePad {
 public:
  // Bring the stack up and start advertising. Safe to call when already up.
  //
  // Returns false if BLE could not start, and the caller must treat that as
  // "stay where you were": a controller mode that silently does nothing is
  // worse than one that refuses to open.
  bool begin(const char *name) {
    if (_up) return true;
    if (!BLEDevice::getInitialized()) BLEDevice::init(name);
    _server = BLEDevice::createServer();
    if (!_server) { end(); return false; }
    _server->setCallbacks(&_cb);

    _hid = new BLEHIDDevice(_server);
    if (!_hid) { end(); return false; }
    _input = _hid->inputReport(1);
    if (!_input) { end(); return false; }

    _hid->manufacturer()->setValue("Claude Mate");
    // Vendor 0xe502 is Espressif's; the product id is this project's own and
    // means nothing to anyone else. macOS shows the advertised NAME, not these.
    _hid->pnp(0x02, 0xe502, 0xa111, 0x0210);
    _hid->hidInfo(0x00, 0x01);

    // HID over GATT REQUIRES an encrypted link -- macOS will discover an
    // unbonded HID device and then refuse to use it, which looks exactly like
    // a broken gamepad. Bonding with no IO capability is "just pair it": there
    // is no keypad here to type a passkey on.
    BLESecurity sec;
    sec.setAuthenticationMode(ESP_LE_AUTH_REQ_SC_BOND);
    sec.setCapability(ESP_IO_CAP_NONE);
    sec.setInitEncryptionKey(ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK);

    _hid->reportMap((uint8_t *)BLEPAD_REPORT_MAP, sizeof(BLEPAD_REPORT_MAP));
    _hid->startServices();
    _hid->setBatteryLevel(100);

    BLEAdvertising *adv = BLEDevice::getAdvertising();
    adv->setAppearance(HID_GAMEPAD);
    adv->addServiceUUID(_hid->hidService()->getUUID());
    adv->setScanResponse(true);
    adv->start();

    _up = true;
    _lastMask = 0xFF;                  // force the first report to be sent
    return true;
  }

  // Take the stack back down and hand the radio to Wi-Fi. deinit(true) frees
  // the controller memory rather than just stopping it, which is the whole
  // point of leaving: BLE parked but resident would keep contending.
  void end() {
    if (!BLEDevice::getInitialized()) { _reset(); return; }
    BLEDevice::deinit(true);
    _reset();
  }

  bool up() const { return _up; }
  bool connected() const { return _cb.connected; }

  // Send the four switches as one report. Cheap enough to call every loop: it
  // returns immediately unless the state actually changed, because a HID host
  // wants edges, not a stream of identical frames.
  void setButtons(uint8_t mask) {
    mask &= 0x0F;
    if (!_up || !_input || !_cb.connected) { _lastMask = 0xFF; return; }
    if (mask == _lastMask) return;
    _lastMask = mask;
    _input->setValue(&mask, 1);
    _input->notify();
  }

  // Re-advertise after the host goes away, so closing the lid and coming back
  // does not need a trip through the menu.
  void poll() {
    if (!_up) return;
    if (!_cb.connected && _cb.wasConnected) {
      _cb.wasConnected = false;
      _lastMask = 0xFF;
      BLEDevice::startAdvertising();
    }
  }

 private:
  struct Cb : public BLEServerCallbacks {
    volatile bool connected = false;
    volatile bool wasConnected = false;
    void onConnect(BLEServer *) override { connected = true; wasConnected = true; }
    void onDisconnect(BLEServer *) override { connected = false; }
  };

  void _reset() {
    _up = false;
    _server = nullptr;
    _hid = nullptr;
    _input = nullptr;
    _cb.connected = false;
    _cb.wasConnected = false;
    _lastMask = 0xFF;
  }

  BLEServer        *_server = nullptr;
  BLEHIDDevice     *_hid = nullptr;
  BLECharacteristic *_input = nullptr;
  Cb                _cb;
  bool              _up = false;
  uint8_t           _lastMask = 0xFF;
};
