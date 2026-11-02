// Shelter camera firmware "shelter-cam-1" (DFRobot DFR1154 ESP32-S3 AI Camera).
// Takes a JPEG on request and sends it to whoever asked: USB serial or the
// console's WiFi connection (ShelterNet). Camera wiring as in the official wiki:
// https://wiki.dfrobot.com/dfr1154/docs/21371
//
// Commands, one per line: ? (status) | p (photo) | WIFISSID / WIFIPASS / WIFI APPLY / WIFI? / WIFI RESET
// Photo reply: "JPEG_BEGIN <bytes>\n" + JPEG + "\nJPEG_END\n"
#include <Arduino.h>
#include "esp_camera.h"
#include <ShelterNet.h>

constexpr int LED = 3, IR = 47;
constexpr const char *FW = "shelter-cam-1";
ShelterNet net;
bool cameraReady = false;

void status() {
  net.printf("{\"fw\":\"%s\",\"camera_ready\":%s,\"psram_bytes\":%u,\"ip\":\"%s\",\"rssi\":%d,\"ble\":%d}\n",
             FW, cameraReady ? "true" : "false", ESP.getPsramSize(), net.ip().c_str(), net.rssi(),
             (int)net.bluetoothSetup());
}

bool initCamera() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = 16; config.pin_d1 = 18; config.pin_d2 = 21; config.pin_d3 = 17;
  config.pin_d4 = 14; config.pin_d5 = 7;  config.pin_d6 = 6;  config.pin_d7 = 4;
  config.pin_xclk = 5; config.pin_pclk = 15; config.pin_vsync = 1; config.pin_href = 2;
  config.pin_sccb_sda = 8; config.pin_sccb_scl = 9;
  config.pin_pwdn = -1; config.pin_reset = -1;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = FRAMESIZE_VGA;
  config.jpeg_quality = 12;
  config.fb_count = 2;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;
  if (!psramFound() || esp_camera_init(&config) != ESP_OK) return false;
  sensor_t *sensor = esp_camera_sensor_get();
  if (sensor && sensor->id.PID == OV3660_PID) {
    sensor->set_vflip(sensor, 1);
    sensor->set_brightness(sensor, 1);
    sensor->set_saturation(sensor, -2);
  }
  return true;
}

void capture(Print &to) {
  if (!cameraReady) { to.println("{\"event\":\"error\",\"reason\":\"camera not ready\"}"); return; }
  for (int i = 0; i < 3; ++i) {              // drop stale frames so the photo is current
    camera_fb_t *stale = esp_camera_fb_get();
    if (stale) esp_camera_fb_return(stale);
    delay(80);
  }
  digitalWrite(LED, HIGH);
  camera_fb_t *frame = esp_camera_fb_get();
  if (!frame) { digitalWrite(LED, LOW); to.println("{\"event\":\"error\",\"reason\":\"capture failed\"}"); return; }
  to.printf("JPEG_BEGIN %u\n", (unsigned)frame->len);
  size_t sent = 0;
  while (sent < frame->len) {
    size_t n = to.write(frame->buf + sent, min<size_t>(4096, frame->len - sent));
    if (!n) break;
    sent += n;
  }
  to.print("\nJPEG_END\n");
  esp_camera_fb_return(frame);
  digitalWrite(LED, LOW);
}

void setup() {
  Serial.begin(115200);
  uint32_t start = millis();
  while (!Serial && millis() - start < 2000) delay(10);
  pinMode(LED, OUTPUT); digitalWrite(LED, LOW);
  pinMode(IR, OUTPUT);  digitalWrite(IR, LOW);
  cameraReady = initCamera();
  net.begin("camera", FW, 0);   // hold BOOT 3 s to redo WiFi set-up
  status();
}

void loop() {
  char line[128];
  ShelterNet::Source from;
  net.loop();
  while (net.readLine(line, sizeof(line), &from)) {
    if (net.handle(line)) continue;
    if (!strcmp(line, "?")) status();
    else if (!strcmp(line, "p")) {
      if (from == ShelterNet::TCP) capture(net.client); else capture(Serial);
    }
  }
  delay(5);
}
