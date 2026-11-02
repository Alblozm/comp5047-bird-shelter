// Controlled I2C investigation: native driver vs GPIO open-drain signaling.
// Only GPIO21/22 are used as a bus. Mister GPIO25 stays LOW.
#include <Arduino.h>
#include <Wire.h>

int dataPin = 21, clockPin = 22;

void releasePin(int pin) { pinMode(pin, INPUT_PULLUP); }
void pullLow(int pin) {
  pinMode(pin, OUTPUT_OPEN_DRAIN);
  digitalWrite(pin, LOW);
}
bool clockHigh() {
  releasePin(clockPin);
  uint32_t start = micros();
  while (!digitalRead(clockPin)) {
    if (micros() - start > 10000) return false;
  }
  delayMicroseconds(20);
  return true;
}
void clockLow() { pullLow(clockPin); delayMicroseconds(20); }
void stopBit() {
  pullLow(dataPin);
  clockHigh();
  releasePin(dataPin);
  delayMicroseconds(20);
}
bool writeByte(uint8_t byte) {
  for (int bit = 7; bit >= 0; --bit) {
    if (byte & (1 << bit)) releasePin(dataPin);
    else pullLow(dataPin);
    delayMicroseconds(20);
    if (!clockHigh()) return false;
    clockLow();
  }
  releasePin(dataPin);
  bool high = clockHigh();
  bool ack = high && !digitalRead(dataPin);
  clockLow();
  return ack;
}

bool startBit(uint8_t address, bool read = false) {
  releasePin(dataPin);
  if (!clockHigh() || !digitalRead(dataPin)) return false;
  pullLow(dataPin);
  delayMicroseconds(20);
  clockLow();
  return writeByte((address << 1) | (read ? 1 : 0));
}

bool softProbe(uint8_t address) {
  bool ack = startBit(address);
  stopBit();
  return ack;
}

bool readByte(uint8_t &value, bool last) {
  value = 0;
  releasePin(dataPin);
  for (int bit = 7; bit >= 0; --bit) {
    if (!clockHigh()) return false;
    value = (value << 1) | digitalRead(dataPin);
    clockLow();
  }
  if (last) releasePin(dataPin);
  else pullLow(dataPin);
  bool ok = clockHigh();
  clockLow();
  releasePin(dataPin);
  return ok;
}

void selectBus(int sda, int scl) {
  dataPin = sda;
  clockPin = scl;
  releasePin(dataPin);
  releasePin(clockPin);
  delayMicroseconds(100);
}

bool command(uint8_t address, const uint8_t *bytes, size_t size) {
  bool ok = startBit(address);
  for (size_t i = 0; ok && i < size; ++i) ok = writeByte(bytes[i]);
  stopBit();
  return ok;
}

bool receive(uint8_t address, uint8_t *bytes, size_t size) {
  bool ok = startBit(address, true);
  for (size_t i = 0; ok && i < size; ++i)
    ok = readByte(bytes[i], i + 1 == size);
  stopBit();
  return ok;
}

uint8_t crc8(const uint8_t *bytes) {
  uint8_t crc = 0xFF;
  for (int i = 0; i < 2; ++i) {
    crc ^= bytes[i];
    for (int bit = 0; bit < 8; ++bit)
      crc = (crc & 0x80) ? (crc << 1) ^ 0x31 : crc << 1;
  }
  return crc;
}

bool readLight(int sda, int scl, float &lux) {
  selectBus(sda, scl);
  const uint8_t power = 0x01, mode = 0x10;
  uint8_t bytes[2];
  if (!command(0x23, &power, 1) || !command(0x23, &mode, 1)) return false;
  delay(200);
  if (!receive(0x23, bytes, sizeof(bytes))) return false;
  lux = ((bytes[0] << 8) | bytes[1]) / 1.2f;
  return true;
}

bool readClimate(int sda, int scl, uint8_t address, float &t, float &rh) {
  selectBus(sda, scl);
  const uint8_t measure[] = {0x24, 0x00};
  uint8_t bytes[6];
  if (!command(address, measure, sizeof(measure))) return false;
  delay(25);
  if (!receive(address, bytes, sizeof(bytes))) return false;
  if (crc8(bytes) != bytes[2] || crc8(bytes + 3) != bytes[5]) return false;
  t = -45.0f + 175.0f * ((bytes[0] << 8) | bytes[1]) / 65535.0f;
  rh = 100.0f * ((bytes[3] << 8) | bytes[4]) / 65535.0f;
  return true;
}

void sampleSensors() {
  float lux = NAN, t = NAN, rh = NAN;
  int lightSda = 0, shtSda = 0;
  uint8_t shtAddress = 0;
  for (int sda : {21, 22}) {
    if (readLight(sda, sda == 21 ? 22 : 21, lux)) {
      lightSda = sda;
      break;
    }
  }
  for (int sda : {21, 22}) {
    for (uint8_t address : {0x44, 0x45}) {
      if (readClimate(sda, sda == 21 ? 22 : 21, address, t, rh)) {
        shtSda = sda;
        shtAddress = address;
        break;
      }
    }
    if (shtSda) break;
  }
  Serial.print("{\"fw\":\"bus-test-4\",\"t\":");
  if (shtSda) Serial.print(t, 2); else Serial.print("null");
  Serial.print(",\"rh\":");
  if (shtSda) Serial.print(rh, 2); else Serial.print("null");
  Serial.print(",\"lux\":");
  if (lightSda) Serial.print(lux, 2); else Serial.print("null");
  Serial.printf(",\"sht_ok\":%d,\"lux_ok\":%d,\"sht_sda\":%d,"
                "\"light_sda\":%d,\"sht_addr\":%u,\"mist\":0}\n",
                bool(shtSda), bool(lightSda), shtSda, lightSda, shtAddress);
}

void nativeTest(uint32_t hz, int sda = 21, int scl = 22) {
  const uint8_t addresses[] = {0x23, 0x44, 0x45, 0x5C};
  for (uint8_t address : addresses) {
    // A fresh bus for each probe prevents one NACK from affecting the next.
    bool ok = Wire.begin(sda, scl, hz);
    Wire.setTimeOut(100);
    uint8_t result = 99;
    if (ok) {
      Wire.beginTransmission(address);
      result = Wire.endTransmission();
      Wire.end();
    }
    Serial.printf("NATIVE %luHz SDA=%d SCL=%d addr=0x%02X begin=%d result=%u\n",
                  (unsigned long)hz, sda, scl, address, ok, result);
    delay(20);
  }
}

void softTest(int sda, int scl) {
  dataPin = sda;
  clockPin = scl;
  releasePin(dataPin);
  releasePin(clockPin);
  delay(20);
  Serial.printf("SOFT SDA=%d SCL=%d idle=%d/%d ACK addresses:",
                sda, scl, digitalRead(dataPin), digitalRead(clockPin));
  int count = 0;
  for (uint8_t address = 8; address < 120; ++address) {
    if (softProbe(address)) {
      Serial.printf(" 0x%02X", address);
      ++count;
    }
  }
  if (!count) Serial.print(" none");
  Serial.println();
  releasePin(dataPin);
  releasePin(clockPin);
}

void runTests() {
  Serial.println("=== Shelter bus-test-4 BEGIN ===");
  nativeTest(100000);
  nativeTest(10000);
  nativeTest(100000, 22, 21);
  softTest(21, 22);
  softTest(22, 21);
  Serial.println("=== Shelter bus-test-4 END ===");
}

void setup() {
  pinMode(25, OUTPUT);
  digitalWrite(25, LOW);
  pinMode(2, OUTPUT);
  digitalWrite(2, LOW);
  Serial.begin(115200);
  delay(1200);
  runTests();
}

void loop() {
  if (Serial.available() && Serial.read() == 'r') runTests();
  static uint32_t lastSample = 0;
  if (millis() - lastSample >= 2000) {
    lastSample = millis();
    sampleSensors();
  }
  delay(20);
}
