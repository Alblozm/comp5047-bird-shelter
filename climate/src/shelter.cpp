// Bird shelter firmware "shelter-4" (FireBeetle 2 ESP32-E).
//
// The board checks the environment and drives the mister itself. The laptop
// console only adds one fact: whether the AI saw a bird on the perch.
//   perch pressed -> console takes a photo -> AI -> "BIRD 1" / "BIRD 0"
//   bird present + daytime + hot + not too humid + water -> mist in pulses
// If the console goes silent, the board falls back to the perch sensor alone
// (setting "fallback"), so cooling never depends on the network.
// The perch counts a bird when the pressure jumps above its usual reading
// (setting "perch_mode" 1; 0 = the old fixed thresholds).
// Every spray is time-limited: at most 30 s in one go, whatever started it, and
// at most "mist_hour_s" seconds in any hour. The 20 ms safety task enforces both.
//
// Protocol (USB serial 115200 baud, or WiFi TCP 5049 via ShelterNet), one line per message:
//   board -> console: status JSON every ~750 ms, {"event":...} lines
//   console -> board: HB | STATUS | CFG? | BIRD 0/1 | SET <key> <value>
//                     MIST <ms> (manual test, <= 3000) | STOP | REBASE (perch empty now)
//                     WIFISSID <name> | WIFIPASS <password> | WIFI APPLY | WIFI? | WIFI RESET
#include <Arduino.h>
#include <Preferences.h>
#include "SoftSensorBus.h"
#include "ShelterLogic.h"
#include <ShelterNet.h>

constexpr int SDA_PIN = 21, SCL_PIN = 22;   // verified wiring
constexpr int WATER = 26;                   // SEN0508: LOW = water at sensor height
constexpr int FSR = 34;                     // FSR + 10k divider, ADC1
constexpr int MIST = 25;                    // Grove atomizer SIG/EN
constexpr int LED = 2;                      // mirrors the mister
constexpr const char *FW = "shelter-4";

struct Setting { const char *key; float value, lo, hi; };
Setting settings[] = {
  {"t_on", 30, 0, 60},          // start cooling at or above (C)
  {"t_off", 28, 0, 60},         // stop cooling at or below (C)
  {"rh_max", 75, 10, 100},      // do not start misting when humidity is at or above (%)
  {"rh_stop", 80, 10, 100},     // stop misting when humidity reaches (%)
  {"lux_day", 150, 0, 60000},   // becomes day when light rises above (lux)
  {"lux_night", 100, 0, 60000}, // becomes night when light falls below (lux)
  {"perch_mode", 1, 0, 1},      // 1: sudden rise above the usual reading; 0: fixed thresholds below
  {"fsr_jump", 300, 20, 4095},  // perch_mode 1: arrival when ADC jumps this far above the usual reading
  {"fsr_on", 600, 1, 4095},     // perch_mode 0: perch pressed when ADC rises above
  {"fsr_off", 300, 0, 4095},    // perch_mode 0: perch released when ADC falls below
  {"mist_s", 20, 1, 30},        // each mist pulse (s); never more than MistBudget::MAX_MS
  {"pause_s", 60, 1, 600},      // rest between pulses (s)
  {"linger_s", 30, 0, 600},     // keep cooling this long after the bird leaves (s)
  {"ai_required", 1, 0, 1},     // 1: wait for AI to confirm a bird; 0: perch alone
  {"fallback", 1, 0, 1},        // console silent -> trust the perch sensor alone
  {"water_req", 1, 0, 1},       // 1: never mist without water at the sensor
  {"mist_hour_s", 300, 10, 1800}, // all misting in any hour, tests included (s)
};
constexpr int SETTINGS = sizeof(settings) / sizeof(settings[0]);
float &cfg(const char *key) {
  for (auto &s : settings) if (!strcmp(s.key, key)) return s.value;
  static float none = NAN; return none;
}

Preferences prefs;
SoftSensorBus bus;
ShelterNet net;   // USB + WiFi output; use net.printf instead of Serial.printf
float t = NAN, rh = NAN, lux = NAN;
int fsrRaw = 0;
volatile bool wet = false, mistOn = false;
volatile int waterRaw = HIGH;
volatile uint32_t mistStart = 0, mistLimit = 0, hourUsedMs = 0;
PerchDetector perchDetector;
MistGuard guard;   // written only by the safety task
bool perch = false, confirmed = false, cooling = false, day = false;
uint32_t presentUntil = 0, lastHost = 0, lastSample = 0, nextPulse = 0;
const char *mistReason = "off";

bool hostAlive() { return lastHost && millis() - lastHost < 15000; }
bool waterOk() { return wet || cfg("water_req") < 0.5f; }
uint32_t hourCapMs() { return (uint32_t)(cfg("mist_hour_s") * 1000); }
bool budgetLeft() { return hourUsedMs + 1000 <= hourCapMs(); }   // at least 1 s of spraying left this hour

void stopMist() { mistOn = false; digitalWrite(MIST, LOW); }

// Only requests a spray; the safety task switches the pin on within 20 ms.
bool startMist(uint32_t ms, const char *reason) {
  if (!waterOk() || !budgetLeft() || guard.resting) return false;
  ms = min(ms, min(MistBudget::MAX_MS, hourCapMs() - hourUsedMs));
  mistReason = reason;
  mistStart = millis();
  mistLimit = ms;
  mistOn = true;
  net.printf("{\"event\":\"mist\",\"on\":1,\"ms\":%lu,\"reason\":\"%s\"}\n", (unsigned long)ms, reason);
  return true;
}

// Runs every 20 ms regardless of slow sensor reads: debounces water and ends
// every spray at its own length, at 30 s continuous, at the hourly total, or
// when the tank runs dry. Only this task switches the mister on, so the pin
// always matches the guard's bookkeeping.
void safetyTask(void *) {
  uint32_t lowSince = 0;
  for (;;) {
    bool on = mistOn;   // read the request before the clock, so its start time is never ahead of now
    uint32_t start = mistStart, limit = mistLimit, now = millis();
    waterRaw = digitalRead(WATER);
    if (waterRaw == LOW) { if (!lowSince) lowSince = now; wet = now - lowSince >= 300; }
    else { lowSince = 0; wet = false; }
    bool allow = guard.allow(now, on, start, limit, hourCapMs(), waterOk());
    if (on && !allow) mistOn = false;
    digitalWrite(MIST, allow ? HIGH : LOW);
    digitalWrite(LED, allow);
    hourUsedMs = guard.budget.usedMs();
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

void num(const char *key, float v, int digits = 2) {
  net.printf("\"%s\":", key);
  if (isfinite(v)) net.print(v, digits); else net.print("null");
  net.print(',');
}

void sendConfig() {
  net.print("{\"event\":\"cfg\"");
  for (auto &s : settings) net.printf(",\"%s\":%g", s.key, s.value);
  net.println("}");
}

bool birdPresent() { return presentUntil && (int32_t)(presentUntil - millis()) > 0; }
bool isDay() { return isfinite(lux) && day; }

// Light has a buffer so dusk or a passing shadow does not toggle day/night.
void updateDay() {
  if (!isfinite(lux)) return;
  if (!day && lux > cfg("lux_day")) day = true;
  else if (day && lux < cfg("lux_night")) day = false;
}

void report() {
  net.printf("{\"fw\":\"%s\",", FW);
  num("t", t); num("rh", rh); num("lux", lux, 1);
  net.printf("\"water\":%d,\"water_raw\":%d,\"fsr\":%d,\"fsr_base\":%d,\"perch\":%d,\"confirmed\":%d,"
                "\"bird\":%d,\"day\":%d,\"cooling\":%d,\"mist\":%d,\"mist_hour_s\":%.1f,\"mist_limit\":%d,"
                "\"host\":%d,\"ip\":\"%s\",\"rssi\":%d,\"ble\":%d,\"uptime_ms\":%lu}\n",
                (int)wet, (int)waterRaw, fsrRaw, (int)perchDetector.base, perch, confirmed, birdPresent(), isDay(),
                cooling, (int)mistOn, hourUsedMs / 1000.0, (int)!budgetLeft(),
                hostAlive(), net.ip().c_str(), net.rssi(), (int)net.bluetoothSetup(), (unsigned long)millis());
}

void command(char *line) {
  lastHost = millis();
  if (net.handle(line)) return;
  char key[24]; float value; int n;
  if (!strcmp(line, "HB")) return;
  if (!strcmp(line, "STATUS")) { report(); return; }
  if (!strcmp(line, "CFG?")) { sendConfig(); return; }
  if (!strcmp(line, "STOP")) { cooling = false; stopMist(); net.println("{\"event\":\"ack\",\"cmd\":\"STOP\"}"); return; }
  if (!strcmp(line, "REBASE")) {
    if (perch) net.printf("{\"event\":\"perch_off\",\"fsr\":%d}\n", fsrRaw);
    perchDetector.rebase(fsrRaw);
    perch = false;
    net.printf("{\"event\":\"ack\",\"cmd\":\"REBASE\",\"base\":%d}\n", fsrRaw);
    return;
  }
  if (sscanf(line, "BIRD %d", &n) == 1) {
    confirmed = n == 1;
    if (confirmed && perch) presentUntil = millis() + 1000;  // refreshed while perched
    if (!confirmed) presentUntil = 0;
    net.printf("{\"event\":\"ack\",\"cmd\":\"BIRD\",\"value\":%d}\n", confirmed);
    return;
  }
  if (sscanf(line, "MIST %d", &n) == 1) {
    if (n <= 0) { stopMist(); return; }
    if (!waterOk()) { net.println("{\"event\":\"rejected\",\"reason\":\"no water at sensor\"}"); return; }
    if (!startMist(min(n, 3000), "manual")) net.println("{\"event\":\"rejected\",\"reason\":\"hourly mist limit reached\"}");
    return;
  }
  if (sscanf(line, "SET %23s %f", key, &value) == 2) {
    for (auto &s : settings) {
      if (strcmp(s.key, key)) continue;
      if (!(value >= s.lo && value <= s.hi)) break;
      s.value = value;
      prefs.putFloat(s.key, value);
      sendConfig();
      return;
    }
    net.printf("{\"event\":\"rejected\",\"reason\":\"invalid setting %s\"}\n", key);
    return;
  }
  net.println("{\"event\":\"rejected\",\"reason\":\"unknown command\"}");
}

void updatePerch() {
  uint32_t sum = 0;
  for (int i = 0; i < 8; ++i) sum += analogRead(FSR);
  fsrRaw = sum / 8;
  switch (perchDetector.update(fsrRaw, millis(), cfg("perch_mode") > 0.5f, cfg("fsr_jump"), cfg("fsr_on"), cfg("fsr_off"))) {
    case PerchDetector::ARRIVED:
      confirmed = false;                    // a new arrival needs a new AI check
      net.printf("{\"event\":\"perch_on\",\"fsr\":%d,\"base\":%d}\n", fsrRaw, (int)perchDetector.base);
      break;
    case PerchDetector::LEFT:
      net.printf("{\"event\":\"perch_off\",\"fsr\":%d}\n", fsrRaw);
      break;
    case PerchDetector::SETTLED:            // pressed for 10 min: not a bird, now the usual reading
      net.printf("{\"event\":\"perch_off\",\"fsr\":%d,\"settled\":1}\n", fsrRaw);
      break;
    default:
      break;
  }
  perch = perchDetector.perch;
  // Who vouches for the bird: the AI, the perch alone, or the perch while offline.
  bool trusted = confirmed || cfg("ai_required") < 0.5f || (!hostAlive() && cfg("fallback") > 0.5f);
  if (perch && trusted) presentUntil = millis() + (uint32_t)(cfg("linger_s") * 1000) + 1000;
}

void updateCooling() {
  bool canCool = birdPresent() && isDay() && waterOk() && isfinite(t) && isfinite(rh);
  bool start = canCool && t >= cfg("t_on") && rh < cfg("rh_max");
  bool keep = canCool && t > cfg("t_off") && rh < cfg("rh_stop");
  if (!cooling && start) { cooling = true; nextPulse = millis(); net.println("{\"event\":\"cooling\",\"on\":1}"); }
  if (cooling && !keep) { cooling = false; stopMist(); net.println("{\"event\":\"cooling\",\"on\":0}"); }
  if (cooling && !mistOn && (int32_t)(millis() - nextPulse) >= 0) {
    if (!startMist((uint32_t)(cfg("mist_s") * 1000), "cooling"))
      net.printf("{\"event\":\"mist_limit\",\"used_s\":%.1f,\"max_s\":%g}\n", hourUsedMs / 1000.0, cfg("mist_hour_s"));
    nextPulse = millis() + (uint32_t)((cfg("mist_s") + cfg("pause_s")) * 1000);
  }
}

void setup() {
  pinMode(MIST, OUTPUT); digitalWrite(MIST, LOW);
  pinMode(LED, OUTPUT); digitalWrite(LED, LOW);
  pinMode(WATER, INPUT_PULLUP);
  analogReadResolution(12);
  Serial.begin(115200);
  delay(300);
  prefs.begin("shelter", false);
  for (auto &s : settings) {   // a stored value outside today's limits (e.g. an old 60 s pulse) is ignored
    float v = prefs.getFloat(s.key, s.value);
    if (v >= s.lo && v <= s.hi) s.value = v;
  }
  bus.begin(SDA_PIN, SCL_PIN);
  if (xTaskCreate(safetyTask, "safety", 2048, nullptr, 3, nullptr) != pdPASS) {
    for (;;) { stopMist(); delay(100); }
  }
  net.onOtaStart = []() { cooling = false; stopMist(); };
  net.begin("climate", FW, 27);   // hold the on-board button (IO27) 3 s to redo WiFi set-up
  sendConfig();
}

void loop() {
  char input[128];
  net.loop();
  while (net.readLine(input, sizeof(input))) command(input);
  updatePerch();
  if (millis() - lastSample >= 750) {
    lastSample = millis();
    float tt = NAN, hh = NAN, ll = NAN;
    if (!bus.climate(0x45, tt, hh)) bus.climate(0x44, tt, hh);
    bus.light(0x23, ll);
    t = tt; rh = hh; lux = ll;
    updateDay();
    updatePerch();
    report();
  }
  updateCooling();
  delay(10);
}
