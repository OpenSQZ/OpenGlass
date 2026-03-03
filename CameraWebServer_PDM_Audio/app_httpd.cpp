// Copyright 2015-2016 Espressif Systems (Shanghai) PTE LTD
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
#include <Arduino.h>
#include "esp_http_server.h"
#include "esp_timer.h"
#include "esp_camera.h"
#include "img_converters.h"
#include "fb_gfx.h"
#include "esp32-hal-ledc.h"
#include "sdkconfig.h"
#include "camera_index.h"

// ---- PDM Audio ----
#include "driver/i2s_pdm.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#if defined(ARDUINO_ARCH_ESP32) && defined(CONFIG_ARDUHAL_ESP_LOG)
#include "esp32-hal-log.h"
#endif

// ============================================================
// WebSocket support check
// ESP32 Arduino 3.x enables CONFIG_HTTPD_WS_SUPPORT by default
// If build fails, enable WS support in Arduino IDE sdkconfig
// ============================================================
#ifndef CONFIG_HTTPD_WS_SUPPORT
#error "WebSocket support required! Use ESP32 Arduino Core 3.x or enable CONFIG_HTTPD_WS_SUPPORT manually"
#endif

// Enable LED FLASH setting
#define CONFIG_LED_ILLUMINATOR_ENABLED 1

// LED FLASH setup
#if CONFIG_LED_ILLUMINATOR_ENABLED

#define LED_LEDC_GPIO            22  //configure LED pin
#define CONFIG_LED_MAX_INTENSITY 255

int led_duty = 0;
bool isStreaming = false;

#endif

// ============================================================
// PDM microphone config (XIAO ESP32S3)
// Per schematic XIAO_ESP32S3_V1.3_SCH_260115:
//   IO42 = PDM_CLK (MTMS)
//   IO41 = PDM_DATA (MTDI)
// ============================================================
#define PDM_CLK_IO        42
#define PDM_DATA_IO       41
#define PDM_SAMPLE_RATE   16000   // 16kHz, suitable for ASR
#define PDM_DMA_DESC_NUM  6       // Number of DMA descriptors
#define PDM_DMA_FRAME_NUM 1024    // Samples per DMA buffer
#define AUDIO_READ_BYTES  640     // 20ms @ 16kHz mono 16bit = 320 samples * 2 bytes
#define AUDIO_GAIN        3       // Software gain (1=no gain, 2-4 suitable for PDM mic)
#define AUDIO_DISCARD_FRAMES 5    // Discard first N frames after connect (flush stale DMA data)

// ============================================================
// Audio state (cross-task, volatile)
// ============================================================
// g_audio_mode removed — Phase A concurrent mode, camera + PDM run simultaneously
static volatile bool g_audio_streaming = false;   // Whether audio streaming task is running
static i2s_chan_handle_t g_pdm_rx_handle = NULL;  // I2S PDM RX channel handle
static volatile int g_ws_audio_fd = -1;           // WS client socket fd
static volatile TaskHandle_t g_audio_task = NULL; // Audio streaming task handle

// External: camera init/deinit (defined in .ino)
extern bool init_camera();
extern void deinit_camera();

// ============================================================
// Original camera types & variables
// ============================================================
typedef struct {
  httpd_req_t *req;
  size_t len;
} jpg_chunking_t;

#define PART_BOUNDARY "123456789000000000000987654321"
static const char *_STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char *_STREAM_BOUNDARY = "\r\n--" PART_BOUNDARY "\r\n";
static const char *_STREAM_PART = "Content-Type: image/jpeg\r\nContent-Length: %u\r\nX-Timestamp: %d.%06d\r\n\r\n";

httpd_handle_t stream_httpd = NULL;
httpd_handle_t camera_httpd = NULL;

typedef struct {
  size_t size;   //number of values used for filtering
  size_t index;  //current value index
  size_t count;  //value count
  int sum;
  int *values;  //array to be filled with values
} ra_filter_t;

static ra_filter_t ra_filter;

static ra_filter_t *ra_filter_init(ra_filter_t *filter, size_t sample_size) {
  memset(filter, 0, sizeof(ra_filter_t));

  filter->values = (int *)malloc(sample_size * sizeof(int));
  if (!filter->values) {
    return NULL;
  }
  memset(filter->values, 0, sample_size * sizeof(int));

  filter->size = sample_size;
  return filter;
}

#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
static int ra_filter_run(ra_filter_t *filter, int value) {
  if (!filter->values) {
    return value;
  }
  filter->sum -= filter->values[filter->index];
  filter->values[filter->index] = value;
  filter->sum += filter->values[filter->index];
  filter->index++;
  filter->index = filter->index % filter->size;
  if (filter->count < filter->size) {
    filter->count++;
  }
  return filter->sum / filter->count;
}
#endif

#if CONFIG_LED_ILLUMINATOR_ENABLED
void enable_led(bool en) {  // Turn LED On or Off
  int duty = en ? led_duty : 0;
  if (en && isStreaming && (led_duty > CONFIG_LED_MAX_INTENSITY)) {
    duty = CONFIG_LED_MAX_INTENSITY;
  }
  ledcWrite(LED_LEDC_GPIO, duty);
  log_i("Set LED intensity to %d", duty);
}
#endif

// ============================================================
// PDM Microphone Init / Deinit
// ============================================================

/**
 * Initialize PDM microphone (I2S0, RX only)
 * Must be called after camera deinit to avoid resource conflicts
 */
bool pdm_mic_init() {
  Serial.printf("[PDM] Init: CLK=IO%d, DATA=IO%d, rate=%dHz\n",
                PDM_CLK_IO, PDM_DATA_IO, PDM_SAMPLE_RATE);
  Serial.printf("[PDM] Heap before: free=%u, PSRAM=%u\n",
                (unsigned)ESP.getFreeHeap(),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

  // 1. Create I2S channel (RX only, I2S_NUM_0)
  i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  chan_cfg.dma_desc_num = PDM_DMA_DESC_NUM;
  chan_cfg.dma_frame_num = PDM_DMA_FRAME_NUM;

  esp_err_t err = i2s_new_channel(&chan_cfg, NULL, &g_pdm_rx_handle);
  if (err != ESP_OK) {
    Serial.printf("[PDM] i2s_new_channel FAILED: 0x%x (%s)\n", err, esp_err_to_name(err));
    g_pdm_rx_handle = NULL;
    return false;
  }

  // 2. Configure PDM RX mode
  i2s_pdm_rx_config_t pdm_cfg = {
    .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(PDM_SAMPLE_RATE),
    .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
    .gpio_cfg = {
      .clk = (gpio_num_t)PDM_CLK_IO,
      .din = (gpio_num_t)PDM_DATA_IO,
      .invert_flags = {
        .clk_inv = false,
      },
    },
  };

  err = i2s_channel_init_pdm_rx_mode(g_pdm_rx_handle, &pdm_cfg);
  if (err != ESP_OK) {
    Serial.printf("[PDM] init_pdm_rx_mode FAILED: 0x%x (%s)\n", err, esp_err_to_name(err));
    i2s_del_channel(g_pdm_rx_handle);
    g_pdm_rx_handle = NULL;
    return false;
  }

  // 3. Enable channel
  err = i2s_channel_enable(g_pdm_rx_handle);
  if (err != ESP_OK) {
    Serial.printf("[PDM] channel_enable FAILED: 0x%x (%s)\n", err, esp_err_to_name(err));
    i2s_del_channel(g_pdm_rx_handle);
    g_pdm_rx_handle = NULL;
    return false;
  }

  Serial.printf("[PDM] Init OK! Heap after: free=%u, PSRAM=%u\n",
                (unsigned)ESP.getFreeHeap(),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
  return true;
}

/**
 * Stop and release PDM microphone resources
 */
static void pdm_mic_deinit() {
  if (g_pdm_rx_handle) {
    i2s_channel_disable(g_pdm_rx_handle);
    i2s_del_channel(g_pdm_rx_handle);
    g_pdm_rx_handle = NULL;
    Serial.printf("[PDM] Deinit OK, free heap: %u\n", (unsigned)ESP.getFreeHeap());
  }
}

// ============================================================
// Audio WebSocket streaming task
// Runs in dedicated FreeRTOS task, reads audio from PDM and pushes via WS
// ============================================================
static void audio_stream_task(void *param) {
  // Allocate read buffer
  uint8_t *buf = (uint8_t *)malloc(AUDIO_READ_BYTES);
  if (!buf) {
    Serial.printf("[WS_AUDIO] OOM! Cannot allocate %d bytes, heap=%u\n",
                  AUDIO_READ_BYTES, (unsigned)ESP.getFreeHeap());
    g_audio_streaming = false;
    g_audio_task = NULL;
    vTaskDelete(NULL);
    return;
  }

  Serial.println("[WS_AUDIO] Streaming task started (with DC-offset + gain)");
  uint32_t total_bytes = 0;
  uint32_t start_ms = millis();
  uint32_t last_log_ms = start_ms;
  uint32_t send_errors = 0;
  uint32_t frame_count = 0;
  int32_t dc_acc = 0;  // DC offset EMA accumulator

  while (g_audio_streaming && g_pdm_rx_handle != NULL && g_ws_audio_fd >= 0) {
    size_t bytes_read = 0;
    esp_err_t err = i2s_channel_read(g_pdm_rx_handle, buf, AUDIO_READ_BYTES,
                                      &bytes_read, 200); // 200ms timeout

    if (err != ESP_OK) {
      if (err == ESP_ERR_TIMEOUT) {
        continue; // Timeout is normal, continue
      }
      Serial.printf("[WS_AUDIO] i2s_channel_read error: 0x%x\n", err);
      break;
    }
    if (bytes_read == 0) {
      continue;
    }

    frame_count++;
    // Discard initial frames (flush stale DMA buffer data)
    if (frame_count <= AUDIO_DISCARD_FRAMES) continue;

    // Audio processing: DC offset removal + software gain
    {
      int16_t *samples = (int16_t *)buf;
      int n = bytes_read / 2;
      for (int i = 0; i < n; i++) {
        // DC offset removal via exponential moving average
        dc_acc = dc_acc - (dc_acc >> 8) + (int32_t)samples[i];
        int32_t val = (int32_t)samples[i] - (dc_acc >> 8);
        // Software gain
        val *= AUDIO_GAIN;
        // Clamp to int16 range
        if (val > 32767) val = 32767;
        if (val < -32768) val = -32768;
        samples[i] = (int16_t)val;
      }
    }

    // Send binary frame via WebSocket
    httpd_ws_frame_t ws_frame;
    memset(&ws_frame, 0, sizeof(ws_frame));
    ws_frame.type = HTTPD_WS_TYPE_BINARY;
    ws_frame.payload = buf;
    ws_frame.len = bytes_read;
    ws_frame.final = true;

    err = httpd_ws_send_frame_async(camera_httpd, g_ws_audio_fd, &ws_frame);
    if (err != ESP_OK) {
      send_errors++;
      if (send_errors >= 3) {
        Serial.printf("[WS_AUDIO] WS send failed %u times (last: 0x%x), stopping\n",
                      send_errors, err);
        break;
      }
      vTaskDelay(pdMS_TO_TICKS(10)); // Brief wait before retry
      continue;
    }

    send_errors = 0; // Reset error count
    total_bytes += bytes_read;

    // Print stats every 5 seconds
    uint32_t now_ms = millis();
    if (now_ms - last_log_ms >= 5000) {
      float elapsed_s = (now_ms - start_ms) / 1000.0f;
      Serial.printf("[WS_AUDIO] Streaming: %u bytes (%.1f KB/s), heap=%u\n",
                    total_bytes,
                    elapsed_s > 0 ? (total_bytes / 1024.0f / elapsed_s) : 0,
                    (unsigned)ESP.getFreeHeap());
      last_log_ms = now_ms;
    }
  }

  uint32_t elapsed_ms = millis() - start_ms;
  Serial.printf("[WS_AUDIO] Stream ended: %u bytes in %u ms (%.1f KB/s)\n",
                total_bytes, elapsed_ms,
                elapsed_ms > 0 ? (total_bytes / 1024.0f / (elapsed_ms / 1000.0f)) : 0);

  free(buf);
  g_audio_streaming = false;
  g_audio_task = NULL;
  vTaskDelete(NULL);
}

// ============================================================
// HTTP Handlers: Original camera handlers (with audio mode guard)
// ============================================================

static esp_err_t bmp_handler(httpd_req_t *req) {
  camera_fb_t *fb = NULL;
  esp_err_t res = ESP_OK;
#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
  uint64_t fr_start = esp_timer_get_time();
#endif
  fb = esp_camera_fb_get();
  if (!fb) {
    log_e("Camera capture failed");
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }

  httpd_resp_set_type(req, "image/x-windows-bmp");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.bmp");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  char ts[32];
  snprintf(ts, 32, "%lld.%06ld", fb->timestamp.tv_sec, fb->timestamp.tv_usec);
  httpd_resp_set_hdr(req, "X-Timestamp", (const char *)ts);

  uint8_t *buf = NULL;
  size_t buf_len = 0;
  bool converted = frame2bmp(fb, &buf, &buf_len);
  esp_camera_fb_return(fb);
  if (!converted) {
    log_e("BMP Conversion failed");
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }
  res = httpd_resp_send(req, (const char *)buf, buf_len);
  free(buf);
#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
  uint64_t fr_end = esp_timer_get_time();
#endif
  log_i("BMP: %llums, %uB", (uint64_t)((fr_end - fr_start) / 1000), buf_len);
  return res;
}

static size_t jpg_encode_stream(void *arg, size_t index, const void *data, size_t len) {
  jpg_chunking_t *j = (jpg_chunking_t *)arg;
  if (!index) {
    j->len = 0;
  }
  if (httpd_resp_send_chunk(j->req, (const char *)data, len) != ESP_OK) {
    return 0;
  }
  j->len += len;
  return len;
}

static esp_err_t capture_handler(httpd_req_t *req) {
  camera_fb_t *fb = NULL;
  esp_err_t res = ESP_OK;
#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
  int64_t fr_start = esp_timer_get_time();
#endif

#if CONFIG_LED_ILLUMINATOR_ENABLED
  enable_led(true);
  vTaskDelay(150 / portTICK_PERIOD_MS);
  fb = esp_camera_fb_get();
  enable_led(false);
#else
  fb = esp_camera_fb_get();
#endif

  if (!fb) {
    log_e("Camera capture failed");
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }

  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.jpg");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  char ts[32];
  snprintf(ts, 32, "%lld.%06ld", fb->timestamp.tv_sec, fb->timestamp.tv_usec);
  httpd_resp_set_hdr(req, "X-Timestamp", (const char *)ts);

#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
  size_t fb_len = 0;
#endif
  if (fb->format == PIXFORMAT_JPEG) {
#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
    fb_len = fb->len;
#endif
    res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
  } else {
    jpg_chunking_t jchunk = {req, 0};
    res = frame2jpg_cb(fb, 80, jpg_encode_stream, &jchunk) ? ESP_OK : ESP_FAIL;
    httpd_resp_send_chunk(req, NULL, 0);
#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
    fb_len = jchunk.len;
#endif
  }
  esp_camera_fb_return(fb);
#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
  int64_t fr_end = esp_timer_get_time();
#endif
  log_i("JPG: %uB %ums", (uint32_t)(fb_len), (uint32_t)((fr_end - fr_start) / 1000));
  return res;
}

static esp_err_t stream_handler(httpd_req_t *req) {
  camera_fb_t *fb = NULL;
  struct timeval _timestamp;
  esp_err_t res = ESP_OK;
  size_t _jpg_buf_len = 0;
  uint8_t *_jpg_buf = NULL;
  char *part_buf[128];

  static int64_t last_frame = 0;
  if (!last_frame) {
    last_frame = esp_timer_get_time();
  }

  res = httpd_resp_set_type(req, _STREAM_CONTENT_TYPE);
  if (res != ESP_OK) {
    return res;
  }

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "X-Framerate", "60");

#if CONFIG_LED_ILLUMINATOR_ENABLED
  isStreaming = true;
  enable_led(true);
#endif

  while (true) {
    fb = esp_camera_fb_get();
    if (!fb) {
      log_e("Camera capture failed");
      res = ESP_FAIL;
    } else {
      _timestamp.tv_sec = fb->timestamp.tv_sec;
      _timestamp.tv_usec = fb->timestamp.tv_usec;
      if (fb->format != PIXFORMAT_JPEG) {
        bool jpeg_converted = frame2jpg(fb, 80, &_jpg_buf, &_jpg_buf_len);
        esp_camera_fb_return(fb);
        fb = NULL;
        if (!jpeg_converted) {
          log_e("JPEG compression failed");
          res = ESP_FAIL;
        }
      } else {
        _jpg_buf_len = fb->len;
        _jpg_buf = fb->buf;
      }
    }
    if (res == ESP_OK) {
      res = httpd_resp_send_chunk(req, _STREAM_BOUNDARY, strlen(_STREAM_BOUNDARY));
    }
    if (res == ESP_OK) {
      size_t hlen = snprintf((char *)part_buf, 128, _STREAM_PART, _jpg_buf_len, _timestamp.tv_sec, _timestamp.tv_usec);
      res = httpd_resp_send_chunk(req, (const char *)part_buf, hlen);
    }
    if (res == ESP_OK) {
      res = httpd_resp_send_chunk(req, (const char *)_jpg_buf, _jpg_buf_len);
    }
    if (fb) {
      esp_camera_fb_return(fb);
      fb = NULL;
      _jpg_buf = NULL;
    } else if (_jpg_buf) {
      free(_jpg_buf);
      _jpg_buf = NULL;
    }
    if (res != ESP_OK) {
      log_e("Send frame failed");
      break;
    }
    int64_t fr_end = esp_timer_get_time();

    int64_t frame_time = fr_end - last_frame;
    last_frame = fr_end;

    frame_time /= 1000;
#if ARDUHAL_LOG_LEVEL >= ARDUHAL_LOG_LEVEL_INFO
    uint32_t avg_frame_time = ra_filter_run(&ra_filter, frame_time);
#endif
    log_i(
      "MJPG: %uB %ums (%.1ffps), AVG: %ums (%.1ffps)", (uint32_t)(_jpg_buf_len), (uint32_t)frame_time, 1000.0 / (uint32_t)frame_time, avg_frame_time,
      1000.0 / avg_frame_time
    );
  }

#if CONFIG_LED_ILLUMINATOR_ENABLED
  isStreaming = false;
  enable_led(false);
#endif

  return res;
}

// ============================================================
// Original control/status/register handlers (unchanged)
// ============================================================

static esp_err_t parse_get(httpd_req_t *req, char **obuf) {
  char *buf = NULL;
  size_t buf_len = 0;

  buf_len = httpd_req_get_url_query_len(req) + 1;
  if (buf_len > 1) {
    buf = (char *)malloc(buf_len);
    if (!buf) {
      httpd_resp_send_500(req);
      return ESP_FAIL;
    }
    if (httpd_req_get_url_query_str(req, buf, buf_len) == ESP_OK) {
      *obuf = buf;
      return ESP_OK;
    }
    free(buf);
  }
  httpd_resp_send_404(req);
  return ESP_FAIL;
}

static esp_err_t cmd_handler(httpd_req_t *req) {
  char *buf = NULL;
  char variable[32];
  char value[32];

  if (parse_get(req, &buf) != ESP_OK) {
    return ESP_FAIL;
  }
  if (httpd_query_key_value(buf, "var", variable, sizeof(variable)) != ESP_OK || httpd_query_key_value(buf, "val", value, sizeof(value)) != ESP_OK) {
    free(buf);
    httpd_resp_send_404(req);
    return ESP_FAIL;
  }
  free(buf);

  int val = atoi(value);
  log_i("%s = %d", variable, val);
  sensor_t *s = esp_camera_sensor_get();
  int res = 0;

  if (!strcmp(variable, "framesize")) {
    if (s->pixformat == PIXFORMAT_JPEG) {
      res = s->set_framesize(s, (framesize_t)val);
    }
  } else if (!strcmp(variable, "quality")) {
    res = s->set_quality(s, val);
  } else if (!strcmp(variable, "contrast")) {
    res = s->set_contrast(s, val);
  } else if (!strcmp(variable, "brightness")) {
    res = s->set_brightness(s, val);
  } else if (!strcmp(variable, "saturation")) {
    res = s->set_saturation(s, val);
  } else if (!strcmp(variable, "gainceiling")) {
    res = s->set_gainceiling(s, (gainceiling_t)val);
  } else if (!strcmp(variable, "colorbar")) {
    res = s->set_colorbar(s, val);
  } else if (!strcmp(variable, "awb")) {
    res = s->set_whitebal(s, val);
  } else if (!strcmp(variable, "agc")) {
    res = s->set_gain_ctrl(s, val);
  } else if (!strcmp(variable, "aec")) {
    res = s->set_exposure_ctrl(s, val);
  } else if (!strcmp(variable, "hmirror")) {
    res = s->set_hmirror(s, val);
  } else if (!strcmp(variable, "vflip")) {
    res = s->set_vflip(s, val);
  } else if (!strcmp(variable, "awb_gain")) {
    res = s->set_awb_gain(s, val);
  } else if (!strcmp(variable, "agc_gain")) {
    res = s->set_agc_gain(s, val);
  } else if (!strcmp(variable, "aec_value")) {
    res = s->set_aec_value(s, val);
  } else if (!strcmp(variable, "aec2")) {
    res = s->set_aec2(s, val);
  } else if (!strcmp(variable, "dcw")) {
    res = s->set_dcw(s, val);
  } else if (!strcmp(variable, "bpc")) {
    res = s->set_bpc(s, val);
  } else if (!strcmp(variable, "wpc")) {
    res = s->set_wpc(s, val);
  } else if (!strcmp(variable, "raw_gma")) {
    res = s->set_raw_gma(s, val);
  } else if (!strcmp(variable, "lenc")) {
    res = s->set_lenc(s, val);
  } else if (!strcmp(variable, "special_effect")) {
    res = s->set_special_effect(s, val);
  } else if (!strcmp(variable, "wb_mode")) {
    res = s->set_wb_mode(s, val);
  } else if (!strcmp(variable, "ae_level")) {
    res = s->set_ae_level(s, val);
  }
#if CONFIG_LED_ILLUMINATOR_ENABLED
  else if (!strcmp(variable, "led_intensity")) {
    led_duty = val;
    if (isStreaming) {
      enable_led(true);
    }
  }
#endif
  else {
    log_i("Unknown command: %s", variable);
    res = -1;
  }

  if (res < 0) {
    return httpd_resp_send_500(req);
  }

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, NULL, 0);
}

static int print_reg(char *p, sensor_t *s, uint16_t reg, uint32_t mask) {
  return sprintf(p, "\"0x%x\":%u,", reg, s->get_reg(s, reg, mask));
}

static esp_err_t status_handler(httpd_req_t *req) {
  static char json_response[1024];

  sensor_t *s = esp_camera_sensor_get();
  char *p = json_response;
  *p++ = '{';

  // Audio status (Phase A: audio_mode removed, always concurrent)
  p += sprintf(p, "\"audio_streaming\":%s,", g_audio_streaming ? "true" : "false");

  if (s != NULL) {
    if (s->id.PID == OV5640_PID || s->id.PID == OV3660_PID) {
      for (int reg = 0x3400; reg < 0x3406; reg += 2) {
        p += print_reg(p, s, reg, 0xFFF);
      }
      p += print_reg(p, s, 0x3406, 0xFF);

      p += print_reg(p, s, 0x3500, 0xFFFF0);
      p += print_reg(p, s, 0x3503, 0xFF);
      p += print_reg(p, s, 0x350a, 0x3FF);
      p += print_reg(p, s, 0x350c, 0xFFFF);

      for (int reg = 0x5480; reg <= 0x5490; reg++) {
        p += print_reg(p, s, reg, 0xFF);
      }

      for (int reg = 0x5380; reg <= 0x538b; reg++) {
        p += print_reg(p, s, reg, 0xFF);
      }

      for (int reg = 0x5580; reg < 0x558a; reg++) {
        p += print_reg(p, s, reg, 0xFF);
      }
      p += print_reg(p, s, 0x558a, 0x1FF);
    } else if (s->id.PID == OV2640_PID) {
      p += print_reg(p, s, 0xd3, 0xFF);
      p += print_reg(p, s, 0x111, 0xFF);
      p += print_reg(p, s, 0x132, 0xFF);
    }

    p += sprintf(p, "\"xclk\":%u,", s->xclk_freq_hz / 1000000);
    p += sprintf(p, "\"pixformat\":%u,", s->pixformat);
    p += sprintf(p, "\"framesize\":%u,", s->status.framesize);
    p += sprintf(p, "\"quality\":%u,", s->status.quality);
    p += sprintf(p, "\"brightness\":%d,", s->status.brightness);
    p += sprintf(p, "\"contrast\":%d,", s->status.contrast);
    p += sprintf(p, "\"saturation\":%d,", s->status.saturation);
    p += sprintf(p, "\"sharpness\":%d,", s->status.sharpness);
    p += sprintf(p, "\"special_effect\":%u,", s->status.special_effect);
    p += sprintf(p, "\"wb_mode\":%u,", s->status.wb_mode);
    p += sprintf(p, "\"awb\":%u,", s->status.awb);
    p += sprintf(p, "\"awb_gain\":%u,", s->status.awb_gain);
    p += sprintf(p, "\"aec\":%u,", s->status.aec);
    p += sprintf(p, "\"aec2\":%u,", s->status.aec2);
    p += sprintf(p, "\"ae_level\":%d,", s->status.ae_level);
    p += sprintf(p, "\"aec_value\":%u,", s->status.aec_value);
    p += sprintf(p, "\"agc\":%u,", s->status.agc);
    p += sprintf(p, "\"agc_gain\":%u,", s->status.agc_gain);
    p += sprintf(p, "\"gainceiling\":%u,", s->status.gainceiling);
    p += sprintf(p, "\"bpc\":%u,", s->status.bpc);
    p += sprintf(p, "\"wpc\":%u,", s->status.wpc);
    p += sprintf(p, "\"raw_gma\":%u,", s->status.raw_gma);
    p += sprintf(p, "\"lenc\":%u,", s->status.lenc);
    p += sprintf(p, "\"hmirror\":%u,", s->status.hmirror);
    p += sprintf(p, "\"dcw\":%u,", s->status.dcw);
    p += sprintf(p, "\"colorbar\":%u", s->status.colorbar);
  } else {
    // Camera not available (audio mode)
    p += sprintf(p, "\"camera\":\"unavailable\"");
  }

#if CONFIG_LED_ILLUMINATOR_ENABLED
  p += sprintf(p, ",\"led_intensity\":%u", led_duty);
#else
  p += sprintf(p, ",\"led_intensity\":%d", -1);
#endif
  *p++ = '}';
  *p++ = 0;
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, json_response, strlen(json_response));
}

static esp_err_t xclk_handler(httpd_req_t *req) {
  char *buf = NULL;
  char _xclk[32];

  if (parse_get(req, &buf) != ESP_OK) {
    return ESP_FAIL;
  }
  if (httpd_query_key_value(buf, "xclk", _xclk, sizeof(_xclk)) != ESP_OK) {
    free(buf);
    httpd_resp_send_404(req);
    return ESP_FAIL;
  }
  free(buf);

  int xclk = atoi(_xclk);
  log_i("Set XCLK: %d MHz", xclk);

  sensor_t *s = esp_camera_sensor_get();
  int res = s->set_xclk(s, LEDC_TIMER_0, xclk);
  if (res) {
    return httpd_resp_send_500(req);
  }

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, NULL, 0);
}

static esp_err_t reg_handler(httpd_req_t *req) {
  char *buf = NULL;
  char _reg[32];
  char _mask[32];
  char _val[32];

  if (parse_get(req, &buf) != ESP_OK) {
    return ESP_FAIL;
  }
  if (httpd_query_key_value(buf, "reg", _reg, sizeof(_reg)) != ESP_OK || httpd_query_key_value(buf, "mask", _mask, sizeof(_mask)) != ESP_OK
      || httpd_query_key_value(buf, "val", _val, sizeof(_val)) != ESP_OK) {
    free(buf);
    httpd_resp_send_404(req);
    return ESP_FAIL;
  }
  free(buf);

  int reg = atoi(_reg);
  int mask = atoi(_mask);
  int val = atoi(_val);
  log_i("Set Register: reg: 0x%02x, mask: 0x%02x, value: 0x%02x", reg, mask, val);

  sensor_t *s = esp_camera_sensor_get();
  int res = s->set_reg(s, reg, mask, val);
  if (res) {
    return httpd_resp_send_500(req);
  }

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, NULL, 0);
}

static esp_err_t greg_handler(httpd_req_t *req) {
  char *buf = NULL;
  char _reg[32];
  char _mask[32];

  if (parse_get(req, &buf) != ESP_OK) {
    return ESP_FAIL;
  }
  if (httpd_query_key_value(buf, "reg", _reg, sizeof(_reg)) != ESP_OK || httpd_query_key_value(buf, "mask", _mask, sizeof(_mask)) != ESP_OK) {
    free(buf);
    httpd_resp_send_404(req);
    return ESP_FAIL;
  }
  free(buf);

  int reg = atoi(_reg);
  int mask = atoi(_mask);
  sensor_t *s = esp_camera_sensor_get();
  int res = s->get_reg(s, reg, mask);
  if (res < 0) {
    return httpd_resp_send_500(req);
  }
  log_i("Get Register: reg: 0x%02x, mask: 0x%02x, value: 0x%02x", reg, mask, res);

  char buffer[20];
  const char *val = itoa(res, buffer, 10);
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, val, strlen(val));
}

static int parse_get_var(char *buf, const char *key, int def) {
  char _int[16];
  if (httpd_query_key_value(buf, key, _int, sizeof(_int)) != ESP_OK) {
    return def;
  }
  return atoi(_int);
}

static esp_err_t pll_handler(httpd_req_t *req) {
  char *buf = NULL;

  if (parse_get(req, &buf) != ESP_OK) {
    return ESP_FAIL;
  }

  int bypass = parse_get_var(buf, "bypass", 0);
  int mul = parse_get_var(buf, "mul", 0);
  int sys = parse_get_var(buf, "sys", 0);
  int root = parse_get_var(buf, "root", 0);
  int pre = parse_get_var(buf, "pre", 0);
  int seld5 = parse_get_var(buf, "seld5", 0);
  int pclken = parse_get_var(buf, "pclken", 0);
  int pclk = parse_get_var(buf, "pclk", 0);
  free(buf);

  log_i("Set Pll: bypass: %d, mul: %d, sys: %d, root: %d, pre: %d, seld5: %d, pclken: %d, pclk: %d", bypass, mul, sys, root, pre, seld5, pclken, pclk);
  sensor_t *s = esp_camera_sensor_get();
  int res = s->set_pll(s, bypass, mul, sys, root, pre, seld5, pclken, pclk);
  if (res) {
    return httpd_resp_send_500(req);
  }

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, NULL, 0);
}

static esp_err_t win_handler(httpd_req_t *req) {
  char *buf = NULL;

  if (parse_get(req, &buf) != ESP_OK) {
    return ESP_FAIL;
  }

  int startX = parse_get_var(buf, "sx", 0);
  int startY = parse_get_var(buf, "sy", 0);
  int endX = parse_get_var(buf, "ex", 0);
  int endY = parse_get_var(buf, "ey", 0);
  int offsetX = parse_get_var(buf, "offx", 0);
  int offsetY = parse_get_var(buf, "offy", 0);
  int totalX = parse_get_var(buf, "tx", 0);
  int totalY = parse_get_var(buf, "ty", 0);
  int outputX = parse_get_var(buf, "ox", 0);
  int outputY = parse_get_var(buf, "oy", 0);
  bool scale = parse_get_var(buf, "scale", 0) == 1;
  bool binning = parse_get_var(buf, "binning", 0) == 1;
  free(buf);

  log_i(
    "Set Window: Start: %d %d, End: %d %d, Offset: %d %d, Total: %d %d, Output: %d %d, Scale: %u, Binning: %u", startX, startY, endX, endY, offsetX, offsetY,
    totalX, totalY, outputX, outputY, scale, binning
  );
  sensor_t *s = esp_camera_sensor_get();
  int res = s->set_res_raw(s, startX, startY, endX, endY, offsetX, offsetY, totalX, totalY, outputX, outputY, scale, binning);
  if (res) {
    return httpd_resp_send_500(req);
  }

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, NULL, 0);
}

static esp_err_t index_handler(httpd_req_t *req) {
  httpd_resp_set_type(req, "text/html");
  httpd_resp_set_hdr(req, "Content-Encoding", "gzip");
  sensor_t *s = esp_camera_sensor_get();
  if (s != NULL) {
    if (s->id.PID == OV3660_PID) {
      return httpd_resp_send(req, (const char *)index_ov3660_html_gz, index_ov3660_html_gz_len);
    } else if (s->id.PID == OV5640_PID) {
      return httpd_resp_send(req, (const char *)index_ov5640_html_gz, index_ov5640_html_gz_len);
    } else {
      return httpd_resp_send(req, (const char *)index_ov2640_html_gz, index_ov2640_html_gz_len);
    }
  } else {
    httpd_resp_set_type(req, "text/html");
    const char *html = "<html><body><h1>Camera initializing</h1>"
                       "<p>Sensor not ready yet. Please wait.</p></body></html>";
    return httpd_resp_send(req, html, HTTPD_RESP_USE_STRLEN);
  }
}

// ============================================================
// NEW: Audio control handlers
// ============================================================

/**
 * GET /audio_begin
 * Phase A: Deprecated (backward compatibility). Camera + PDM now run concurrently, no switching needed.
 */
static esp_err_t audio_begin_handler(httpd_req_t *req) {
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_type(req, "text/plain");
  Serial.println("[AUDIO_BEGIN] (no-op in Phase A concurrent mode)");
  return httpd_resp_send(req, "ok (concurrent mode, no action needed)", HTTPD_RESP_USE_STRLEN);
}

/**
 * GET /audio_end
 * Phase A: Deprecated (backward compatibility). Camera + PDM now run concurrently, no switching needed.
 */
static esp_err_t audio_end_handler(httpd_req_t *req) {
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_type(req, "text/plain");
  Serial.println("[AUDIO_END] (no-op in Phase A concurrent mode)");
  return httpd_resp_send(req, "ok (concurrent mode, no action needed)", HTTPD_RESP_USE_STRLEN);
}

/**
 * WebSocket handler for /ws_audio
 * When PC connects to this endpoint, ESP32 starts pushing PCM16 audio data
 *
 * Data format: 16kHz, mono, PCM16 little-endian, binary frames
 */
static esp_err_t ws_audio_handler(httpd_req_t *req) {
  // WebSocket handshake (first call, method == HTTP_GET)
  if (req->method == HTTP_GET) {
    // Reject new connection if client already connected
    if (g_ws_audio_fd >= 0 || g_audio_streaming) {
      Serial.println("[WS_AUDIO] Rejected: another client already connected");
      httpd_resp_set_status(req, "409 Conflict");
      httpd_resp_set_type(req, "text/plain");
      return httpd_resp_send(req, "Another audio client already connected", HTTPD_RESP_USE_STRLEN);
    }

    g_ws_audio_fd = httpd_req_to_sockfd(req);
    Serial.printf("[WS_AUDIO] Client connected, fd=%d\n", g_ws_audio_fd);

    // Start audio streaming task (Core 1, priority 5)
    g_audio_streaming = true;
    BaseType_t ok = xTaskCreatePinnedToCore(
      audio_stream_task,  // Task function
      "audio_ws",         // Name
      8192,               // Stack size
      NULL,               // Parameters
      5,                  // Priority
      (TaskHandle_t *)&g_audio_task, // Handle (cast to remove volatile)
      1                   // Pin to Core 1 (Core 0 for WiFi/network)
    );

    if (ok != pdPASS) {
      Serial.printf("[WS_AUDIO] Failed to create task! err=%d, heap=%u\n",
                    ok, (unsigned)ESP.getFreeHeap());
      g_audio_streaming = false;
      g_ws_audio_fd = -1;
      return ESP_FAIL;
    }

    Serial.println("[WS_AUDIO] Streaming task launched");
    return ESP_OK;
  }

  // Handle WS frames from client (we don't expect to receive data, but need to handle close/ping)
  httpd_ws_frame_t pkt;
  memset(&pkt, 0, sizeof(pkt));
  pkt.type = HTTPD_WS_TYPE_BINARY;

  // Read header first
  esp_err_t ret = httpd_ws_recv_frame(req, &pkt, 0);
  if (ret != ESP_OK) {
    Serial.printf("[WS_AUDIO] recv_frame error: 0x%x, client may have disconnected\n", ret);
    g_audio_streaming = false;
    g_ws_audio_fd = -1;
    return ret;
  }

  // CLOSE frame
  if (pkt.type == HTTPD_WS_TYPE_CLOSE) {
    Serial.println("[WS_AUDIO] Client sent CLOSE frame");
    g_audio_streaming = false;
    g_ws_audio_fd = -1;
  }

  // If client sent text/data, we don't process it, just discard
  if (pkt.len > 0) {
    uint8_t *temp = (uint8_t *)malloc(pkt.len);
    if (temp) {
      pkt.payload = temp;
      httpd_ws_recv_frame(req, &pkt, pkt.len);
      free(temp);
    }
  }

  return ESP_OK;
}

// ============================================================
// Server startup
// ============================================================
void startCameraServer() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.max_uri_handlers = 20; // Increased to accommodate new endpoints

  // ---- URI definitions ----

  httpd_uri_t index_uri = {
    .uri = "/",
    .method = HTTP_GET,
    .handler = index_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t status_uri = {
    .uri = "/status",
    .method = HTTP_GET,
    .handler = status_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t cmd_uri = {
    .uri = "/control",
    .method = HTTP_GET,
    .handler = cmd_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t capture_uri = {
    .uri = "/capture",
    .method = HTTP_GET,
    .handler = capture_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t stream_uri = {
    .uri = "/stream",
    .method = HTTP_GET,
    .handler = stream_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t bmp_uri = {
    .uri = "/bmp",
    .method = HTTP_GET,
    .handler = bmp_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t xclk_uri = {
    .uri = "/xclk",
    .method = HTTP_GET,
    .handler = xclk_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t reg_uri = {
    .uri = "/reg",
    .method = HTTP_GET,
    .handler = reg_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t greg_uri = {
    .uri = "/greg",
    .method = HTTP_GET,
    .handler = greg_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t pll_uri = {
    .uri = "/pll",
    .method = HTTP_GET,
    .handler = pll_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t win_uri = {
    .uri = "/resolution",
    .method = HTTP_GET,
    .handler = win_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  // ---- Audio endpoints ----

  httpd_uri_t audio_begin_uri = {
    .uri = "/audio_begin",
    .method = HTTP_GET,
    .handler = audio_begin_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t audio_end_uri = {
    .uri = "/audio_end",
    .method = HTTP_GET,
    .handler = audio_end_handler,
    .user_ctx = NULL,
    .is_websocket = false,
    .handle_ws_control_frames = false,
    .supported_subprotocol = NULL
  };

  httpd_uri_t ws_audio_uri = {
    .uri = "/ws_audio",
    .method = HTTP_GET,
    .handler = ws_audio_handler,
    .user_ctx = NULL,
    .is_websocket = true,             // Critical: enable WebSocket
    .handle_ws_control_frames = false, // Let httpd handle ping/pong automatically
    .supported_subprotocol = NULL
  };

  // ---- Start server ----

  ra_filter_init(&ra_filter, 20);

  log_i("Starting web server on port: '%d'", config.server_port);
  if (httpd_start(&camera_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(camera_httpd, &index_uri);
    httpd_register_uri_handler(camera_httpd, &cmd_uri);
    httpd_register_uri_handler(camera_httpd, &status_uri);
    httpd_register_uri_handler(camera_httpd, &capture_uri);
    httpd_register_uri_handler(camera_httpd, &bmp_uri);

    httpd_register_uri_handler(camera_httpd, &xclk_uri);
    httpd_register_uri_handler(camera_httpd, &reg_uri);
    httpd_register_uri_handler(camera_httpd, &greg_uri);
    httpd_register_uri_handler(camera_httpd, &pll_uri);
    httpd_register_uri_handler(camera_httpd, &win_uri);

    // Register audio endpoints
    httpd_register_uri_handler(camera_httpd, &audio_begin_uri);
    httpd_register_uri_handler(camera_httpd, &audio_end_uri);
    httpd_register_uri_handler(camera_httpd, &ws_audio_uri);

    Serial.println("[HTTP] Registered: / /capture /bmp /control /status + /audio_begin /audio_end /ws_audio");
  }

  config.server_port += 1;
  config.ctrl_port += 1;
  log_i("Starting stream server on port: '%d'", config.server_port);
  if (httpd_start(&stream_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(stream_httpd, &stream_uri);
    Serial.println("[HTTP] Registered stream on port 81: /stream");
  }
}

void setupLedFlash(int pin) {
#if CONFIG_LED_ILLUMINATOR_ENABLED
  ledcAttach(pin, 5000, 8);
#else
  log_i("LED flash is disabled -> CONFIG_LED_ILLUMINATOR_ENABLED = 0");
#endif
}
