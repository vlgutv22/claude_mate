#pragma once
// Host stand-in for the ESP32 NVS wrapper: an in-memory map, same API surface
// as ship_it.h actually uses.
#include <cstdint>
#include <map>
#include <string>
class Preferences {
 public:
  static std::map<std::string, uint32_t> store;
  bool begin(const char *, bool = false) { return true; }
  void end() {}
  uint16_t getUShort(const char *k, uint16_t d = 0) {
    auto it = store.find(k); return it == store.end() ? d : (uint16_t)it->second; }
  uint8_t getUChar(const char *k, uint8_t d = 0) {
    auto it = store.find(k); return it == store.end() ? d : (uint8_t)it->second; }
  void putUShort(const char *k, uint16_t v) { store[k] = v; }
  void putUChar(const char *k, uint8_t v) { store[k] = v; }
};
std::map<std::string, uint32_t> Preferences::store;
