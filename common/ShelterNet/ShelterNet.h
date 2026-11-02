#pragma once
// ShelterNet: WiFi for the Shelter boards, using the same line protocol as USB.
//
// Setting up WiFi (once):
//  - A board with no saved WiFi advertises over Bluetooth as "PROV_shelter-<role>".
//    In Espressif's free "ESP BLE Provisioning" app (iOS/Android), pick the board,
//    enter the PIN "shelter5047", choose the hotspot and type its password.
//    The board saves it and reconnects by itself after every power-up.
//  - To change networks: hold the board's button for 3 s (FireBeetle: on-board
//    button IO27; DFR1154: BOOT). The saved WiFi is erased and Bluetooth set-up starts.
//  - Over USB the console can still write WiFi with WIFISSID / WIFIPASS / WIFI APPLY.
//
// Once connected:
//  - Every 2 s broadcasts {"shelter":"<role>","fw":...,"port":5049} on UDP 5048 so the
//    console finds the board without an IP address.
//  - Accepts one console connection on TCP 5049 (a new connection replaces the old).
//  - Accepts firmware updates over WiFi (ArduinoOTA, host shelter-<role>.local).
//  - As a Print, writes go to USB serial and to the console connection.
#include <Arduino.h>
#include <ArduinoOTA.h>
#include <WiFi.h>
#include <WiFiProv.h>
#include <WiFiUdp.h>
#include <esp_wifi.h>

class ShelterNet : public Print {
 public:
  enum Source { NONE, USB, TCP };
  static constexpr uint16_t ANNOUNCE_PORT = 5048, LINK_PORT = 5049;
  static constexpr const char *PIN = "shelter5047";   // BLE proof-of-possession and OTA password
  WiFiClient client;
  void (*onOtaStart)() = nullptr;   // e.g. switch the mister off before updating

  // button: GPIO of a push button used to re-run Bluetooth set-up (-1 for none).
  void begin(const char *role, const char *fw, int button = -1) {
    role_ = role; fw_ = fw; button_ = button;
    if (button_ >= 0) { pinMode(button_, INPUT_PULLUP); delay(5); idle_ = digitalRead(button_); }
    String name = String("PROV_shelter-") + role;
    WiFi.onEvent(onEvent);
    WiFi.setHostname((String("shelter-") + role).c_str());
    WiFi.begin();   // saved network, if any
    // Starts Bluetooth set-up only when no network is saved; otherwise just connects.
    static uint8_t uuid[16] = {0x5e, 0x17, 0x7e, 0x20, 0x26, 0x50, 0x47, 0x00,
                               0xb1, 0x7d, 0x5c, 0x00, 0x01, 0x00, 0x00, 0x01};
    WiFiProv.beginProvision(NETWORK_PROV_SCHEME_BLE, NETWORK_PROV_SCHEME_HANDLER_FREE_BTDM,
                            NETWORK_PROV_SECURITY_1, PIN, name.c_str(), NULL, uuid, false);
    WiFi.setSleep(false);
  }

  void loop() {
    checkButton();
    if (WiFi.status() != WL_CONNECTED) return;
    if (!started_) {
      started_ = true;
      server_.begin(LINK_PORT);
      server_.setNoDelay(true);
      udp_.begin(ANNOUNCE_PORT + 1);
      ArduinoOTA.setHostname((String("shelter-") + role_).c_str());
      ArduinoOTA.setPassword(PIN);
      ArduinoOTA.onStart([this]() { if (onOtaStart) onOtaStart(); });
      ArduinoOTA.begin();
    }
    ArduinoOTA.handle();
    if (server_.hasClient()) {
      if (client) client.stop();
      client = server_.accept();
      client.setNoDelay(true);
      tcpUsed_ = 0;
    }
    if (millis() - lastAnnounce_ >= 2000) {
      lastAnnounce_ = millis();
      udp_.beginPacket(WiFi.broadcastIP(), ANNOUNCE_PORT);
      udp_.printf("{\"shelter\":\"%s\",\"fw\":\"%s\",\"port\":%u,\"ip\":\"%s\",\"rssi\":%d}",
                  role_, fw_, LINK_PORT, WiFi.localIP().toString().c_str(), WiFi.RSSI());
      udp_.endPacket();
    }
  }

  // Returns true with a complete line from USB or the console connection.
  bool readLine(char *line, size_t size, Source *from = nullptr) {
    if (take(Serial, serialBuf_, serialUsed_, line, size)) { if (from) *from = USB; return true; }
    if (client && client.connected() && take(client, tcpBuf_, tcpUsed_, line, size)) {
      if (from) *from = TCP; return true;
    }
    return false;
  }

  // WiFi commands from the console; returns true if the line was one of them.
  bool handle(const char *line) {
    if (!strncmp(line, "WIFISSID ", 9)) { pendingSsid_ = line + 9; return true; }
    if (!strncmp(line, "WIFIPASS", 8)) { pendingPass_ = line[8] ? line + 9 : ""; return true; }
    if (!strcmp(line, "WIFI APPLY")) {
      if (provisioning_) WiFiProv.endProvision();
      WiFi.disconnect();
      WiFi.begin(pendingSsid_.c_str(), pendingPass_.c_str());   // saved for the next power-up
      report();
      return true;
    }
    if (!strcmp(line, "WIFI RESET")) { forget(); return true; }
    if (!strcmp(line, "WIFI?")) { report(); return true; }
    return false;
  }

  void report() {
    printf("{\"event\":\"wifi\",\"ssid\":\"%s\",\"connected\":%d,\"bluetooth_setup\":%d,\"ip\":\"%s\",\"rssi\":%d}\n",
           savedSsid().c_str(), WiFi.status() == WL_CONNECTED, (int)provisioning_, ip().c_str(), rssi());
  }
  String ip() { return WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : String(""); }
  int rssi() { return WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0; }
  bool bluetoothSetup() { return provisioning_; }

  size_t write(uint8_t c) override { return write(&c, 1); }
  size_t write(const uint8_t *data, size_t size) override {
    Serial.write(data, size);
    if (client && client.connected()) client.write(data, size);
    return size;
  }

 private:
  const char *role_ = "", *fw_ = "";
  int button_ = -1, idle_ = HIGH;
  uint32_t pressedSince_ = 0;
  String pendingSsid_, pendingPass_;
  WiFiServer server_;
  WiFiUDP udp_;
  bool started_ = false;
  uint32_t lastAnnounce_ = 0;
  char serialBuf_[128], tcpBuf_[128];
  size_t serialUsed_ = 0, tcpUsed_ = 0;
  static inline volatile bool provisioning_ = false;

  static void onEvent(arduino_event_t *event) {
    if (event->event_id == ARDUINO_EVENT_PROV_START) provisioning_ = true;
    if (event->event_id == ARDUINO_EVENT_PROV_END) provisioning_ = false;
  }

  static String savedSsid() {
    wifi_config_t config = {};
    if (esp_wifi_get_config(WIFI_IF_STA, &config) != ESP_OK) return String("");
    return String((const char *)config.sta.ssid);
  }

  // Erase the saved network and restart into Bluetooth set-up.
  void forget() {
    println("{\"event\":\"wifi\",\"reset\":1}");
    delay(100);
    WiFi.disconnect(true, true);
    delay(300);
    ESP.restart();
  }

  void checkButton() {
    if (button_ < 0) return;
    if (digitalRead(button_) != idle_) {
      if (!pressedSince_) pressedSince_ = millis();
      if (millis() - pressedSince_ >= 3000) forget();
    } else {
      pressedSince_ = 0;
    }
  }

  static bool take(Stream &in, char *buf, size_t &used, char *line, size_t size) {
    while (in.available()) {
      char c = in.read();
      if (c == '\n') {
        buf[used] = 0;
        strncpy(line, buf, size - 1); line[size - 1] = 0;
        bool full = used > 0;
        used = 0;
        if (full) return true;
      } else if (c != '\r' && used < 127) {
        buf[used++] = c;
      }
    }
    return false;
  }
};
