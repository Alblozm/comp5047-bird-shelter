// DFR1154 camera bring-up: official DFRobot camera wiring, JPEG over USB.
// Source: https://wiki.dfrobot.com/dfr1154/docs/21371
// Audio pins follow DFRobot/DFR1154_Examples, example 5.2 Recording & Playback.
#include <Arduino.h>
#include "esp_camera.h"
#include "ESP_I2S.h"
#include <math.h>

bool cameraReady = false;
I2SClass microphone(I2S_NUM_0), speaker(I2S_NUM_1);
bool micReady = false, speakerReady = false, micEnabled = false;
float micRms = NAN;
uint32_t ledUntil = 0, irUntil = 0, lastMic = 0;

void status() {
  Serial.printf("{\"fw\":\"camera-hardware-test-1\",\"camera_ready\":%s,\"psram_bytes\":%u,\"flash_bytes\":%u,"
                "\"audio_supported\":true,\"mic_ready\":%s,\"speaker_ready\":%s,\"mic_enabled\":%s,\"mic_rms\":",
                cameraReady ? "true" : "false",
                ESP.getPsramSize(), ESP.getFlashChipSize(),
                micReady ? "true" : "false", speakerReady ? "true" : "false", micEnabled ? "true" : "false");
  if (isfinite(micRms)) Serial.print(micRms, 1); else Serial.print("null");
  Serial.println('}');
}

void enableMic() {
  if (!micReady) {
    microphone.setPinsPdmRx(38, 39);
    micReady = microphone.begin(I2S_MODE_PDM_RX, 16000, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO);
  }
  micEnabled = micReady;
  if (!micReady) Serial.println("ERROR: PDM microphone initialization failed");
  status();
}

void sampleMic() {
  int16_t samples[512];
  size_t bytes = 0;
  esp_err_t result = i2s_channel_read(microphone.rxChan(), samples, sizeof(samples), &bytes, 100);
  size_t count = bytes / sizeof(int16_t);
  micRms = NAN;
  if (result == ESP_OK && count) {
    double sum = 0, square = 0;
    for (size_t i = 0; i < count; ++i) { sum += samples[i]; square += double(samples[i]) * samples[i]; }
    micRms = sqrt(max(0.0, square / count - (sum / count) * (sum / count)));
  } else Serial.println("ERROR: microphone read timed out");
  status();
}

void beep() {
  if (!speakerReady) {
    speaker.setPins(45, 46, 42);
    speakerReady = speaker.begin(I2S_MODE_STD, 16000, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO);
  }
  if (!speakerReady) { Serial.println("ERROR: speaker I2S initialization failed"); return; }
  int16_t samples[256];
  // Quiet, bounded 400ms tone; no recording is stored or sent elsewhere.
  for (int base = 0; base < 6400; base += 256) {
    for (int i = 0; i < 256; ++i) samples[i] = int16_t(1800 * sin(2 * PI * 440 * (base + i) / 16000.0));
    size_t sent = 0;
    if (i2s_channel_write(speaker.txChan(), samples, sizeof(samples), &sent, 100) != ESP_OK || sent != sizeof(samples)) {
      Serial.println("ERROR: speaker write failed"); break;
    }
  }
  // Release the TX channel so the last DMA block cannot repeat indefinitely.
  delay(50);
  speaker.end(); speakerReady = false;
  Serial.println("{\"event\":\"speaker_test_sent\",\"duration_ms\":400}");
  status();
}

void setup() {
  Serial.begin(115200);
  uint32_t start = millis();
  while (!Serial && millis() - start < 5000) delay(10);
  delay(500);
  pinMode(3, OUTPUT);
  digitalWrite(3, LOW);
  pinMode(47, OUTPUT);
  digitalWrite(47, LOW);

  if (!psramFound()) {
    Serial.println("ERROR: PSRAM unavailable; check qio_opi board configuration.");
    status();
    return;
  }

  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = 16;
  config.pin_d1 = 18;
  config.pin_d2 = 21;
  config.pin_d3 = 17;
  config.pin_d4 = 14;
  config.pin_d5 = 7;
  config.pin_d6 = 6;
  config.pin_d7 = 4;
  config.pin_xclk = 5;
  config.pin_pclk = 15;
  config.pin_vsync = 1;
  config.pin_href = 2;
  config.pin_sccb_sda = 8;
  config.pin_sccb_scl = 9;
  config.pin_pwdn = -1;
  config.pin_reset = -1;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = FRAMESIZE_VGA;
  config.jpeg_quality = 12;
  config.fb_count = 2;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("ERROR: camera initialization failed: 0x%x\n", err);
    status();
    return;
  }
  sensor_t *sensor = esp_camera_sensor_get();
  if (sensor && sensor->id.PID == OV3660_PID) {
    sensor->set_vflip(sensor, 1);
    sensor->set_brightness(sensor, 1);
    sensor->set_saturation(sensor, -2);
  }
  cameraReady = true;
  Serial.printf("Sensor PID: 0x%04x\n", sensor ? sensor->id.PID : 0);
  Serial.println("Commands: ? status, p photo, l LED, m/n mic on/off, b speaker, i IR 1s");
  status();
}

void capture() {
  if (!cameraReady) {
    Serial.println("ERROR: camera is not ready");
    return;
  }
  // Discard buffered frames so the JPEG reflects the current scene.
  for (int i = 0; i < 3; ++i) {
    camera_fb_t *stale = esp_camera_fb_get();
    if (stale) esp_camera_fb_return(stale);
    delay(80);
  }
  digitalWrite(3, HIGH);
  camera_fb_t *frame = esp_camera_fb_get();
  if (!frame) {
    digitalWrite(3, LOW);
    Serial.println("ERROR: failed to capture frame");
    return;
  }
  Serial.printf("JPEG_BEGIN %u\n", (unsigned)frame->len);
  size_t sent = 0;
  while (sent < frame->len) {
    size_t n = Serial.write(frame->buf + sent, frame->len - sent);
    if (!n) break;
    sent += n;
  }
  Serial.flush();
  Serial.println("\nJPEG_END");
  esp_camera_fb_return(frame);
  digitalWrite(3, LOW);
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '?') status();
    if (c == 'p') capture();
    if (c == 'l') ledUntil = millis() + 500;
    if (c == 'i') irUntil = millis() + 1000;
    if (c == 'm') enableMic();
    if (c == 'n') {
      micEnabled = false; micRms = NAN;
      if (micReady) microphone.end();
      micReady = false; status();
    }
    if (c == 'b') beep();
  }
  uint32_t now = millis();
  digitalWrite(3, ledUntil && (int32_t)(ledUntil - now) > 0);
  digitalWrite(47, irUntil && (int32_t)(irUntil - now) > 0);
  if (micEnabled && now - lastMic >= 300) { lastMic = now; sampleMic(); }
  delay(10);
}
