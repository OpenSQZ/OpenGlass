/*
 * CameraWebServer_PDM_Audio — Phase A: Concurrent Mode
 *
 * Camera + PDM microphone run simultaneously (no time-division multiplexing).
 *
 * Core design: concurrent operation
 *   - Camera uses DVP interface, PDM uses I2S_NUM_0; both are hardware-independent
 *   - Both camera and PDM are initialized in setup()
 *   - /ws_audio: streaming starts on connect, stops on disconnect; no audio_begin/audio_end needed
 *   - /audio_begin /audio_end kept as no-ops (backward compatibility)
 *
 * Hardware: Seeed Studio XIAO ESP32S3 + OV5640
 * PDM microphone pins (per schematic XIAO_ESP32S3_V1.3_SCH_260115):
 *   - IO42 = PDM_CLK (clock)
 *   - IO41 = PDM_DATA (data)
 *
 * WS audio format: 16kHz, mono, PCM16 LE, 20ms/frame (640 bytes)
 * Audio processing: DC offset removal + software gain (done on ESP32)
 *
 * Endpoints:
 *   GET  /           - Web UI (Camera)
 *   GET  /capture    - JPEG capture (always available)
 *   GET  :81/stream  - MJPEG video stream (always available)
 *   WS   /ws_audio   - PCM16 audio stream (stream on connect)
 *   GET  /status     - System status JSON
 *   GET  /audio_begin, /audio_end - Backward compatibility (no-op)
 *
 * Arduino IDE settings:
 *   Board: XIAO_ESP32S3 | PSRAM: OPI PSRAM
 *   Partition: Huge APP (3MB No OTA)
 */

#include "esp_camera.h"
#include <WiFi.h>

// ===================
// Select camera model
// ===================
#define CAMERA_MODEL_XIAO_ESP32S3 // Has PSRAM
#include "camera_pins.h"

// ===========================
// Enter your WiFi credentials
// ===========================
const char *ssid = "YOUR_WIFI_NAME";
const char *password = "YOUR_WIFI_PASSWORD";


// Forward declarations
void startCameraServer();
void setupLedFlash(int pin);
extern bool pdm_mic_init();  // defined in app_httpd.cpp

// ========================================
// Camera config (global, for reinit)
// ========================================
static camera_config_t cam_config;

static void setup_cam_config() {
  cam_config.ledc_channel = LEDC_CHANNEL_0;
  cam_config.ledc_timer = LEDC_TIMER_0;
  cam_config.pin_d0 = Y2_GPIO_NUM;
  cam_config.pin_d1 = Y3_GPIO_NUM;
  cam_config.pin_d2 = Y4_GPIO_NUM;
  cam_config.pin_d3 = Y5_GPIO_NUM;
  cam_config.pin_d4 = Y6_GPIO_NUM;
  cam_config.pin_d5 = Y7_GPIO_NUM;
  cam_config.pin_d6 = Y8_GPIO_NUM;
  cam_config.pin_d7 = Y9_GPIO_NUM;
  cam_config.pin_xclk = XCLK_GPIO_NUM;
  cam_config.pin_pclk = PCLK_GPIO_NUM;
  cam_config.pin_vsync = VSYNC_GPIO_NUM;
  cam_config.pin_href = HREF_GPIO_NUM;
  cam_config.pin_sccb_sda = SIOD_GPIO_NUM;
  cam_config.pin_sccb_scl = SIOC_GPIO_NUM;
  cam_config.pin_pwdn = PWDN_GPIO_NUM;
  cam_config.pin_reset = RESET_GPIO_NUM;
  cam_config.xclk_freq_hz = 10000000;   // 10MHz, headroom for WiFi stack
  cam_config.frame_size = FRAMESIZE_HD;  // 1280x720
  cam_config.pixel_format = PIXFORMAT_JPEG;
  cam_config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  cam_config.fb_location = CAMERA_FB_IN_PSRAM;
  cam_config.jpeg_quality = 12;
  cam_config.fb_count = 2;

  if (cam_config.pixel_format == PIXFORMAT_JPEG) {
    if (psramFound()) {
      cam_config.jpeg_quality = 10;
      cam_config.fb_count = 2;
      cam_config.grab_mode = CAMERA_GRAB_LATEST;
    } else {
      cam_config.frame_size = FRAMESIZE_SVGA;
      cam_config.fb_location = CAMERA_FB_IN_DRAM;
    }
  } else {
    cam_config.frame_size = FRAMESIZE_240X240;
#if CONFIG_IDF_TARGET_ESP32S3
    cam_config.fb_count = 2;
#endif
  }
}

static void apply_sensor_settings() {
  sensor_t *s = esp_camera_sensor_get();
  if (!s) {
    Serial.println("[CAM] WARNING: sensor_get returned NULL");
    return;
  }

  // Basic orientation
  s->set_vflip(s, 1);
  s->set_hmirror(s, 0);

  // Exposure
  s->set_aec2(s, 1);
  s->set_ae_level(s, -2);
  s->set_aec_value(s, 168);

  // Gain
  s->set_agc_gain(s, 0);
  s->set_gainceiling(s, (gainceiling_t)0);

  // White balance
  s->set_awb_gain(s, 1);
  s->set_wb_mode(s, 0);

  // Contrast/saturation
  s->set_contrast(s, 2);
  s->set_brightness(s, 0);
  s->set_saturation(s, 2);

  // Lens correction + sharpness
  s->set_lenc(s, 1);
  s->set_sharpness(s, 1);

  // Resolution
  s->set_framesize(s, FRAMESIZE_HD);
}

// ========================================
// Camera functions callable from app_httpd.cpp
// ========================================
bool init_camera() {
  setup_cam_config();
  esp_err_t err = esp_camera_init(&cam_config);
  if (err != ESP_OK) {
    Serial.printf("[CAM] init FAILED: 0x%x\n", err);
    return false;
  }
  apply_sensor_settings();
  Serial.printf("[CAM] init OK, free heap: %u, PSRAM free: %u\n",
                (unsigned)ESP.getFreeHeap(),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
  return true;
}

void deinit_camera() {
  esp_err_t err = esp_camera_deinit();
  if (err != ESP_OK) {
    Serial.printf("[CAM] deinit failed: 0x%x, retrying...\n", err);
    delay(200);
    err = esp_camera_deinit();
  }
  Serial.printf("[CAM] deinit: %s, free heap: %u\n",
                err == ESP_OK ? "OK" : "FAIL",
                (unsigned)ESP.getFreeHeap());
}

// ========================================
// setup & loop
// ========================================
void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(true);
  Serial.println();
  Serial.println("========================================");
  Serial.println(" CameraWebServer + PDM Audio (XIAO S3)");
  Serial.println("========================================");

  // Camera init (DVP interface)
  if (!init_camera()) {
    Serial.println("[FATAL] Camera init failed!");
    return;
  }

  // PDM mic init (I2S_NUM_0, runs concurrently with camera)
  if (!pdm_mic_init()) {
    Serial.println("[WARNING] PDM mic init failed! Audio will not work.");
    // Continue — camera still works
  }

#if defined(CAMERA_MODEL_ESP_EYE)
  pinMode(13, INPUT_PULLUP);
  pinMode(14, INPUT_PULLUP);
#endif

#if defined(LED_GPIO_NUM)
  setupLedFlash(LED_GPIO_NUM);
#endif

  // WiFi
  WiFi.begin(ssid, password);
  WiFi.setSleep(false);

  Serial.print("[WiFi] Connecting");
  int wifi_retry = 0;
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    wifi_retry++;
    if (wifi_retry > 40) { // 20 seconds timeout
      Serial.println("\n[WiFi] FAILED to connect! Restarting...");
      ESP.restart();
    }
  }
  Serial.println();
  Serial.printf("[WiFi] Connected! IP: %s\n", WiFi.localIP().toString().c_str());

  // Start web server
  startCameraServer();

  Serial.println("========================================");
  Serial.printf("[READY] Web UI:    http://%s/\n", WiFi.localIP().toString().c_str());
  Serial.printf("[READY] Capture:   http://%s/capture\n", WiFi.localIP().toString().c_str());
  Serial.printf("[READY] Stream:    http://%s:81/stream\n", WiFi.localIP().toString().c_str());
  Serial.printf("[READY] Audio WS:  ws://%s/ws_audio\n", WiFi.localIP().toString().c_str());
  Serial.printf("[READY] Free heap: %u bytes\n", (unsigned)ESP.getFreeHeap());
  Serial.println("========================================");
}

void loop() {
  // Health monitoring
  Serial.printf("[HEALTH] heap=%u  PSRAM=%u  RSSI=%d  uptime=%lus\n",
                (unsigned)ESP.getFreeHeap(),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                WiFi.RSSI(),
                millis() / 1000);
  delay(10000);
}
