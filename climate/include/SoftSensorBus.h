#pragma once
#include <Arduino.h>

// Slow, open-drain diagnostic I2C. All high levels release the line.
// Used by the hardware tester so the ESP32 native I2C driver is not required.
class SoftSensorBus {
 public:
  int sda = 21, scl = 22;
  void begin(int data, int clock) {
    sda = data; scl = clock;
    release(sda); release(scl);
  }
  bool probe(uint8_t address) { bool ok = start(address); stop(); return ok; }
  bool command(uint8_t address, const uint8_t *bytes, size_t size) {
    bool ok = start(address);
    for (size_t i = 0; ok && i < size; ++i) ok = write(bytes[i]);
    stop(); return ok;
  }
  bool receive(uint8_t address, uint8_t *bytes, size_t size) {
    bool ok = start(address, true);
    for (size_t i = 0; ok && i < size; ++i) ok = read(bytes[i], i + 1 == size);
    stop(); return ok;
  }
  bool climate(uint8_t address, float &t, float &rh) {
    const uint8_t measure[] = {0x24, 0x00};
    uint8_t bytes[6];
    if (!command(address, measure, 2)) return false;
    delay(25);
    if (!receive(address, bytes, 6)) return false;
    if (crc(bytes) != bytes[2] || crc(bytes + 3) != bytes[5]) return false;
    t = -45.0f + 175.0f * ((bytes[0] << 8) | bytes[1]) / 65535.0f;
    rh = 100.0f * ((bytes[3] << 8) | bytes[4]) / 65535.0f;
    return true;
  }
  bool light(uint8_t address, float &lux) {
    const uint8_t power = 0x01, mode = 0x10;
    uint8_t bytes[2];
    if (!command(address, &power, 1) || !command(address, &mode, 1)) return false;
    delay(200);
    if (!receive(address, bytes, 2)) return false;
    lux = ((bytes[0] << 8) | bytes[1]) / 1.2f;
    return true;
  }
 private:
  static void release(int pin) { pinMode(pin, INPUT_PULLUP); }
  static void low(int pin) { pinMode(pin, OUTPUT_OPEN_DRAIN); digitalWrite(pin, LOW); }
  bool highClock() {
    release(scl);
    uint32_t start = micros();
    while (!digitalRead(scl)) if (micros() - start > 3000) return false;
    delayMicroseconds(20); return true;
  }
  void lowClock() { low(scl); delayMicroseconds(20); }
  bool start(uint8_t address, bool reading = false) {
    release(sda);
    if (!highClock() || !digitalRead(sda)) return false;
    low(sda); delayMicroseconds(20); lowClock();
    return write((address << 1) | (reading ? 1 : 0));
  }
  void stop() { low(sda); highClock(); release(sda); delayMicroseconds(20); }
  bool write(uint8_t byte) {
    for (int bit = 7; bit >= 0; --bit) {
      if (byte & (1 << bit)) release(sda); else low(sda);
      delayMicroseconds(20);
      if (!highClock()) return false;
      lowClock();
    }
    release(sda);
    bool ok = highClock() && !digitalRead(sda);
    lowClock(); return ok;
  }
  bool read(uint8_t &byte, bool last) {
    byte = 0; release(sda);
    for (int bit = 0; bit < 8; ++bit) {
      if (!highClock()) return false;
      byte = (byte << 1) | digitalRead(sda); lowClock();
    }
    if (last) release(sda); else low(sda);
    bool ok = highClock(); lowClock(); release(sda); return ok;
  }
  static uint8_t crc(const uint8_t *bytes) {
    uint8_t value = 0xFF;
    for (int i = 0; i < 2; ++i) {
      value ^= bytes[i];
      for (int b = 0; b < 8; ++b) value = value & 0x80 ? (value << 1) ^ 0x31 : value << 1;
    }
    return value;
  }
};
