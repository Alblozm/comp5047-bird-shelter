// Component tester only: no automatic cooling, AI, WiFi or bird logic.
#include <Arduino.h>
#include "SoftSensorBus.h"

constexpr int WATER = 26, FSR = 34, MIST = 25, LED = 2;
SoftSensorBus bus;
int sdaPin = 21, sclPin = 22;
volatile bool waterEnabled = false, fsrEnabled = false, armed = false;
volatile bool wet = false, mistOn = false;
volatile int waterRaw = HIGH;
volatile uint32_t mistUntil = 0, ledUntil = 0;
float t = NAN, rh = NAN, lux = NAN, lux2 = NAN;
int shtAddress = 0, fsrRaw = 0, fsrMv = 0;
uint32_t lastSample = 0;

void stopMist() {
  mistOn = false;
  mistUntil = 0;
  digitalWrite(MIST, LOW);
}

// Independent component tests: water sensing does not gate manual mist tests.
// A separate task bounds the pulse even while I2C or serial operations block.
void pulseWatchdog(void *) {
  uint32_t lowSince = 0;
  for (;;) {
    uint32_t now = millis();
    waterRaw = digitalRead(WATER);
    if (waterRaw == LOW) {
      if (!lowSince) lowSince = now;
      wet = now - lowSince >= 300;
    } else {
      lowSince = 0; wet = false;
    }
    if (!armed ||
        (mistUntil && (int32_t)(now - mistUntil) >= 0)) stopMist();
    digitalWrite(LED, mistOn || (ledUntil && (int32_t)(ledUntil - now) > 0));
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

void value(const char *key, float number) {
  Serial.printf("\"%s\":", key);
  if (isfinite(number)) Serial.print(number, 2); else Serial.print("null");
  Serial.print(',');
}

void report() {
  Serial.print("{\"fw\":\"hardware-test-2\",\"board\":\"FireBeetle 2 ESP32-E\",\"bus\":\"software I2C\",");
  value("t", t); value("rh", rh); value("lux", lux); value("lux2", lux2);
  Serial.printf("\"sda\":%d,\"scl\":%d,\"sht_addr\":%d,\"sht_ok\":%d,\"lux_ok\":%d,"
                "\"water_raw\":%d,\"water_enabled\":%d,\"water_wet\":%d,"
                "\"fsr_raw\":%d,\"fsr_mv\":%d,\"fsr_enabled\":%d,"
                "\"mist\":%d,\"armed\":%d,\"uptime_ms\":%lu}\n",
                sdaPin, sclPin, shtAddress, isfinite(t) && isfinite(rh), isfinite(lux),
                waterRaw, waterEnabled, waterEnabled ? int(wet) : -1,
                fsrRaw, fsrMv, fsrEnabled, mistOn, armed, (unsigned long)millis());
}

void scan() {
  Serial.print("{\"event\":\"scan\",\"addresses\":[");
  bool first = true;
  for (uint8_t address : {0x23, 0x44, 0x45, 0x5C}) {
    if (bus.probe(address)) {
      if (!first) Serial.print(',');
      Serial.print(address); first = false;
    }
  }
  Serial.println("]}");
}

void command(const char *line) {
  int a = 0, b = 0;
  if (!strcmp(line, "STOP")) { armed = false; stopMist(); }
  else if (!strcmp(line, "LED")) ledUntil = millis() + 500;
  else if (!strcmp(line, "SCAN")) scan();
  else if (!strcmp(line, "STATUS")) report();
  else if (sscanf(line, "PINS %d %d", &a, &b) == 2 &&
           ((a == 21 && b == 22) || (a == 22 && b == 21))) {
    armed = false; stopMist();
    sdaPin = a; sclPin = b; bus.begin(a, b);
    t = rh = lux = lux2 = NAN; shtAddress = 0;
  }
  else if (sscanf(line, "WATER %d", &a) == 1) {
    waterEnabled = a == 1;
  }
  else if (sscanf(line, "FSR %d", &a) == 1) fsrEnabled = a == 1;
  else if (sscanf(line, "ARM %d", &a) == 1) {
    armed = a == 1;
    if (!armed) stopMist();
  }
  else if (sscanf(line, "MIST %d", &a) == 1) {
    if (a <= 0) stopMist();
    else if (armed) {
      mistUntil = millis() + min(a, 1000);
      mistOn = true;
      digitalWrite(MIST, HIGH);
    } else {
      Serial.println("{\"event\":\"rejected\",\"reason\":\"mist requires manual wet-wick confirmation (ARM)\"}");
      return;
    }
  }
  else {
    Serial.println("{\"event\":\"rejected\",\"reason\":\"unknown command\"}");
    return;
  }
  Serial.println("{\"event\":\"command_ack\"}");
}

void setup() {
  pinMode(MIST, OUTPUT); digitalWrite(MIST, LOW);
  pinMode(LED, OUTPUT); digitalWrite(LED, LOW);
  pinMode(WATER, INPUT_PULLUP);
  analogReadResolution(12);
  Serial.begin(115200);
  delay(500);
  bus.begin(sdaPin, sclPin);
  if (xTaskCreate(pulseWatchdog, "mist-watchdog", 2048, nullptr, 3, nullptr) != pdPASS) {
    Serial.println("{\"event\":\"fatal\",\"reason\":\"pulse watchdog task failed\"}");
    for (;;) { stopMist(); delay(100); }
  }
  report();
}

void loop() {
  static char input[80];
  static size_t used = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') { input[used] = 0; command(input); used = 0; }
    else if (c != '\r' && used < sizeof(input) - 1) input[used++] = c;
  }
  if (millis() - lastSample >= 750) {
    lastSample = millis();
    t = rh = lux = lux2 = NAN;
    shtAddress = 0;
    for (uint8_t address : {0x44, 0x45}) {
      if (bus.climate(address, t, rh)) { shtAddress = address; break; }
    }
    bus.light(0x23, lux);
    bus.light(0x5C, lux2);
    uint32_t sum = 0;
    for (int i = 0; i < 8; ++i) sum += analogRead(FSR);
    fsrRaw = sum / 8;
    fsrMv = analogReadMilliVolts(FSR);
    report();
  }
  delay(5);
}
