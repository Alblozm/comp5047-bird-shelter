// Initial wiring diagnostics only. Mister output is held LOW at all times.
// The original climate controller remains in main.cpp in its own environment.
#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_SHT31.h>
#include <BH1750.h>
#include "ShelterPins.h"

constexpr char VERSION[] = "bringup-4";
constexpr uint32_t I2C_HZ = 100000;
constexpr uint32_t REPORT_MS = 2000;
constexpr uint32_t RETRY_MS = 5000;

Adafruit_SHT31 sht;
BH1750 light;
bool busReady = false, shtReady = false, lightReady = false;
bool recoveryNeeded = false;
uint8_t shtAddress = 0, lightAddress = 0;
uint32_t lastReport = 0, lastRecovery = 0;
uint32_t recoveries = 0;

bool startBus() {
  if (busReady) {
    Wire.end();
    delay(10);
  }
  busReady = Wire.begin(ShelterPins::SDA_GPIO, ShelterPins::SCL_GPIO, I2C_HZ);
  Wire.setTimeOut(100);
  if (!busReady) Serial.println("ERROR: I2C begin failed");
  return busReady;
}

// Discovery and normal sensor reading are separate phases. No recurring
// full-address scans while the sensors are taking measurements.
void discover() {
  shtAddress = lightAddress = 0;
  const uint8_t addresses[] = {0x44, 0x45, 0x23, 0x5C};
  for (uint8_t address : addresses) {
    // Isolate probe failures so one failed transaction cannot poison the next.
    if (!startBus()) continue;
    Wire.beginTransmission(address);
    uint8_t result = Wire.endTransmission();
    Serial.printf("PROBE %s 0x%02X result=%u%s\n",
                  address == 0x44 || address == 0x45 ? "SHT31" : "BH1750",
                  address, result, result == 0 ? " ACK" : " (no ACK / bus error)");
    if (result == 0) {
      if ((address == 0x44 || address == 0x45) && !shtAddress)
        shtAddress = address;
      if ((address == 0x23 || address == 0x5C) && !lightAddress)
        lightAddress = address;
    }
  }
}

void initializeSensors() {
  shtReady = lightReady = false;
  if (!busReady) return;

  if (shtAddress) {
    shtReady = sht.begin(shtAddress);
    Serial.printf("INIT SHT31 0x%02X: %s\n", shtAddress,
                  shtReady ? "OK" : "FAILED");
    if (!shtReady) recoveryNeeded = true;
  } else {
    Serial.println("SHT31: no address ACK at 0x44/0x45");
    recoveryNeeded = true;
  }

  if (lightAddress) {
    lightReady = light.begin(BH1750::CONTINUOUS_HIGH_RES_MODE,
                             lightAddress, &Wire);
    Serial.printf("INIT BH1750 0x%02X: %s\n", lightAddress,
                  lightReady ? "OK" : "FAILED");
    if (!lightReady) recoveryNeeded = true;
    // The library's begin() only waits briefly after configuring the sensor.
    // High-resolution measurement takes up to 180 ms at default MTreg.
    // Wait after initialization AND check measurementReady() before reading.
    if (lightReady) delay(200);
  } else {
    Serial.println("BH1750: no address ACK at 0x23/0x5C");
    recoveryNeeded = true;
  }
}

void printValue(const char *name, float value) {
  Serial.printf("\"%s\":", name);
  if (isfinite(value)) Serial.print(value, 2);
  else Serial.print("null");
  Serial.print(',');
}

void setup() {
  pinMode(25, OUTPUT);
  digitalWrite(25, LOW);
  pinMode(2, OUTPUT);
  digitalWrite(2, LOW);
  Serial.begin(115200);
  delay(500);
  Serial.printf("Shelter %s: SDA=%d SCL=%d I2C=%luHz; mister OFF\n",
                VERSION, ShelterPins::SDA_GPIO, ShelterPins::SCL_GPIO,
                (unsigned long)I2C_HZ);
  if (startBus()) {
    discover();
    // Recreate the ESP32 driver after address probing. This makes the read
    // phase independent of driver state left by failed probe transactions.
    if (startBus()) initializeSensors();
    else recoveryNeeded = true;
  } else {
    recoveryNeeded = true;
  }
  lastRecovery = millis();
}

void loop() {
  uint32_t now = millis();
  if (recoveryNeeded && now - lastRecovery >= RETRY_MS) {
    ++recoveries;
    recoveryNeeded = false;
    Serial.println("RECOVER I2C: rediscover addresses and initialize sensors");
    if (startBus()) {
      discover();
      if (startBus()) initializeSensors();
      else recoveryNeeded = true;
    } else recoveryNeeded = true;
    lastRecovery = millis();
    now = millis();
  }

  if (now - lastReport >= REPORT_MS) {
    lastReport = now;
    float t = NAN, rh = NAN, lux = NAN;

    // Read light first so an SHT31 transaction failure does not prevent
    // us from observing a working BH1750 during this report cycle.
    if (lightReady && light.measurementReady(true)) {
      lux = light.readLightLevel();
      if (!isfinite(lux) || lux < 0) {
        Serial.println("READ BH1750 FAILED: expected 2 data bytes");
        lightReady = false;
        recoveryNeeded = true;
        lux = NAN;
      }
    }
    // One measurement yields matching temperature and humidity readings;
    // the library checks both CRCs before returning success.
    if (shtReady && !sht.readBoth(&t, &rh)) {
      Serial.println("READ SHT31 FAILED: transaction or CRC check failed");
      shtReady = false;
      recoveryNeeded = true;
    }

    Serial.printf("{\"stage\":\"bringup\",\"fw\":\"%s\",", VERSION);
    printValue("t", t);
    printValue("rh", rh);
    printValue("lux", lux);
    Serial.printf("\"sht_ok\":%d,\"lux_ok\":%d,\"sht_addr\":%u,"
                  "\"lux_addr\":%u,\"recoveries\":%lu,\"mist\":0}\n",
                  isfinite(t) && isfinite(rh), isfinite(lux),
                  shtAddress, lightAddress, (unsigned long)recoveries);
  }
  delay(20);
}
