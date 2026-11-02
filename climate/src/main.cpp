// Bird shelter - climate node (FireBeetle 2 ESP32-E)
// Reads SHT31 (temp/humidity), BH1750 (light), water level, perch FSR.
// Decides locally whether to mist. Prints one JSON line every 2 s over USB serial.
// Serial commands:  d = toggle demo mode   f = force a 5 s mist test   ? = print status
#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_SHT31.h>
#include <BH1750.h>
#include "ShelterPins.h"

// ---------- pins: GPIO numbers, NOT the D-numbers printed on the board ----------
const int PIN_SDA     = ShelterPins::SDA_GPIO;
const int PIN_SCL     = ShelterPins::SCL_GPIO;
const int PIN_MIST    = 25;   // mister driver SIG (HIGH = mist)
const int PIN_WATER   = 26;   // SEN0508: LOW = liquid detected
const int PIN_FSR     = 34;   // perch FSR + 10k divider, ADC1 (input only)
const int PIN_DEMO    = 13;   // optional button to GND: toggles demo mode
const int PIN_LED     = 2;    // on-board LED mirrors the mister

// ---------- control rules ----------
struct Rules {
  float onTempC;        // start cooling at or above this
  float offTempC;       // stop cooling at or below this (hysteresis)
  float rhOnMax;        // only start if humidity is below this
  float rhOffMin;       // stop if humidity rises to this
  float dayLux;         // "daytime" if lux is above this
  uint32_t mistMs;      // mist this long...
  uint32_t pauseMs;     // ...then rest this long
};
const Rules REAL = {30.0, 28.0, 70.0, 75.0, 150.0, 20000, 60000};
const Rules DEMO = {26.0, 25.0, 90.0, 95.0, 150.0,  5000, 10000};

// ---------- perch (bird present) ----------
const int FSR_ON  = 400;      // raw 0-4095; calibrate with the real perch + toy bird
const int FSR_OFF = 250;

Adafruit_SHT31 sht;
BH1750 lightMeter;
bool shtOk = false, luxOk = false;
bool demoMode = false;
bool cooling = false, mistOn = false, birdOn = false, waterOk = false;
uint32_t phaseStart = 0, forceUntil = 0, lastReport = 0;
int waterLowCount = 0;

void setMist(bool on) {
  mistOn = on;
  digitalWrite(PIN_MIST, on ? HIGH : LOW);
  digitalWrite(PIN_LED, on ? HIGH : LOW);
  phaseStart = millis();
}

void i2cScan() {
  Serial.println("I2C scan (expect 0x23 BH1750, 0x44 or 0x45 SHT31):");
  int n = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { Serial.printf("  found 0x%02X\n", a); n++; }
  }
  if (!n) Serial.printf("  nothing found - check SDA=GPIO%d, SCL=GPIO%d, 3V3, GND\n", PIN_SDA, PIN_SCL);
}

void printNum(const char *k, float v, int dec) {
  if (isnan(v)) Serial.printf("\"%s\":null,", k);
  else Serial.printf("\"%s\":%.*f,", k, dec, v);
}

void setup() {
  Serial.begin(115200);
  delay(500);
  pinMode(PIN_MIST, OUTPUT);  digitalWrite(PIN_MIST, LOW);   // mister OFF at boot
  pinMode(PIN_LED, OUTPUT);   digitalWrite(PIN_LED, LOW);
  pinMode(PIN_WATER, INPUT_PULLUP);
  pinMode(PIN_DEMO, INPUT_PULLUP);
  analogReadResolution(12);

  Wire.begin(PIN_SDA, PIN_SCL);
  i2cScan();
  shtOk = sht.begin(0x44) || sht.begin(0x45);
  luxOk = lightMeter.begin(BH1750::CONTINUOUS_HIGH_RES_MODE);
  Serial.printf("SHT31: %s   BH1750: %s\n", shtOk ? "ok" : "NOT FOUND", luxOk ? "ok" : "NOT FOUND");
  Serial.println("Commands: d = demo mode, f = 5 s mist test, ? = status");
}

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'd') { demoMode = !demoMode; Serial.printf("demo mode %s\n", demoMode ? "ON" : "OFF"); }
    if (c == 'f') { forceUntil = millis() + 5000; Serial.println("forced mist test for 5 s"); }
  }
  static bool lastBtn = HIGH;
  bool btn = digitalRead(PIN_DEMO);
  if (lastBtn == HIGH && btn == LOW) { demoMode = !demoMode; Serial.printf("demo mode %s\n", demoMode ? "ON" : "OFF"); delay(50); }
  lastBtn = btn;
}

void loop() {
  handleSerial();
  const Rules &R = demoMode ? DEMO : REAL;

  // --- water level, debounced: LOW (liquid present) must be seen 3 times in a row
  if (digitalRead(PIN_WATER) == LOW) { if (waterLowCount < 3) waterLowCount++; }
  else waterLowCount = 0;
  waterOk = (waterLowCount >= 3);

  // --- perch FSR with hysteresis
  int fsr = analogRead(PIN_FSR);
  if (!birdOn && fsr > FSR_ON) { birdOn = true;  Serial.println("{\"event\":\"perch_on\"}"); }
  if ( birdOn && fsr < FSR_OFF) { birdOn = false; Serial.println("{\"event\":\"perch_off\"}"); }

  // --- slow sensors every 2 s
  static float t = NAN, rh = NAN, lux = NAN;
  uint32_t now = millis();
  bool report = (now - lastReport >= 2000);
  if (report) {
    lastReport = now;
    if (shtOk) { t = sht.readTemperature(); rh = sht.readHumidity(); }
    if (luxOk) lux = lightMeter.readLightLevel();
  }

  // --- decide whether cooling is wanted (hysteresis on temp/humidity)
  bool day = !isnan(lux) && lux > R.dayLux;
  bool want = !isnan(t) && !isnan(rh) && day && waterOk;
  if (!cooling && want && t >= R.onTempC && rh < R.rhOnMax) cooling = true;
  if (cooling && (!want || t <= R.offTempC || rh >= R.rhOffMin)) cooling = false;

  // --- pulse: mist for mistMs, rest for pauseMs
  bool forced = (int32_t)(forceUntil - now) > 0 && waterOk;
  if (!waterOk) {
    if (mistOn) setMist(false);                       // water safety always wins
  } else if (forced) {
    if (!mistOn) setMist(true);
  } else if (cooling) {
    if (!mistOn && now - phaseStart >= R.pauseMs) setMist(true);
    if ( mistOn && now - phaseStart >= R.mistMs)  setMist(false);
  } else if (mistOn) {
    setMist(false);
  }

  if (report) {
    Serial.print("{");
    printNum("t", t, 1); printNum("rh", rh, 1); printNum("lux", lux, 0);
    Serial.printf("\"water\":%d,\"fsr\":%d,\"bird\":%d,\"day\":%d,\"cooling\":%d,\"mist\":%d,\"demo\":%d}\n",
                  waterOk, fsr, birdOn, day, cooling, mistOn, demoMode);
  }
  delay(20);
}
