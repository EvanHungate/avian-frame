/*
 * Avian Visitors frame -- reTerminal E1002 (7.3" Spectra 6, 800x480)
 *
 * Wake -> Wi-Fi -> GET http://<server>/frame.bin (If-None-Match: last ETag)
 *   200: push the 4bpp framebuffer to the panel, refresh, remember the ETag
 *   304: birds haven't changed, leave the panel alone
 * -> deep sleep for X-Sleep-Seconds (server decides; longer overnight).
 *
 * Green button (right, KEY0/GPIO3): wake and redraw now.
 *
 * Arduino IDE setup:
 *   Board:  "XIAO_ESP32S3" (esp32 by Espressif)
 *   PSRAM:  "OPI PSRAM"   <-- required, the frame buffer lives in PSRAM
 *   Library: Seeed_GFX (https://github.com/Seeed-Studio/Seeed_GFX)
 *   driver.h (in this folder) selects BOARD_SCREEN_COMBO 521 = E1002.
 *   Copy config.example.h to config.h and fill in Wi-Fi + server.
 *
 * Logs: Serial1 @115200 on the carrier's USB-UART (GPIO43 TX / GPIO44 RX).
 */
#include <WiFi.h>
#include <HTTPClient.h>
#include <esp_sleep.h>
#include "TFT_eSPI.h"
#include "config.h"

#ifndef EPAPER_ENABLE
#error "driver.h must select BOARD_SCREEN_COMBO 521 (reTerminal E1002)"
#endif

// ---- board pins (reTerminal E1001/E1002) ------------------------------------
static constexpr int PIN_KEY_GREEN = 3;   // KEY0, right/green button, active LOW
static constexpr int PIN_LED       = 6;   // active LOW
static constexpr int PIN_BATT_ADC  = 1;
static constexpr int PIN_BATT_EN   = 21;
static constexpr int PIN_DBG_RX    = 44;
static constexpr int PIN_DBG_TX    = 43;
#define LOG Serial1

static constexpr int    FB_W = 800, FB_H = 480;
static constexpr size_t FB_BYTES = FB_W * FB_H / 2;   // 4bpp = 192000

// Survives deep sleep
RTC_DATA_ATTR char     g_etag[48] = "";
RTC_DATA_ATTR uint32_t g_boots = 0;
RTC_DATA_ATTR uint8_t  g_fails = 0;

EPaper epaper;

static float readBattery() {
  pinMode(PIN_BATT_EN, OUTPUT);
  digitalWrite(PIN_BATT_EN, HIGH);
  delay(10);
  uint32_t mv = 0;
  for (int i = 0; i < 8; ++i) mv += analogReadMilliVolts(PIN_BATT_ADC);
  digitalWrite(PIN_BATT_EN, LOW);
  return (mv / 8) * 2 / 1000.0f;   // 2:1 divider
}

static bool connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(DEVICE_HOSTNAME);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  const uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - t0 > 20000) return false;
    delay(100);
  }
  LOG.printf("[wifi] connected %s rssi=%d in %lums\n",
             WiFi.localIP().toString().c_str(), WiFi.RSSI(), millis() - t0);
  return true;
}

static void deepSleep(uint32_t seconds) {
  if (seconds < 60) seconds = 60;
  if (seconds > 12 * 3600) seconds = 12 * 3600;
  for (int i = 0; i < 300 && digitalRead(PIN_KEY_GREEN) == LOW; ++i) delay(10);  // wait for release
  LOG.printf("[sleep] %lu s\n", (unsigned long)seconds);
  LOG.flush();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  digitalWrite(PIN_LED, HIGH);
  esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);
  // Board has external pull-ups on the keys, so ANY_LOW works in deep sleep.
  esp_sleep_enable_ext1_wakeup(1ULL << PIN_KEY_GREEN, ESP_EXT1_WAKEUP_ANY_LOW);
  esp_deep_sleep_start();
}

// Returns seconds to sleep.
static uint32_t fetchAndDraw(bool force, float vbat) {
  HTTPClient http;
  String url = String(SERVER_URL) + "/frame.bin?batt=" + String(vbat, 2) +
               "&rssi=" + String(WiFi.RSSI()) + "&boot=" + String(g_boots);
  http.begin(url);
  http.setTimeout(15000);
  const char* keep[] = {"ETag", "X-Sleep-Seconds"};
  http.collectHeaders(keep, 2);
  if (!force && g_etag[0]) http.addHeader("If-None-Match", g_etag);

  const int code = http.GET();
  uint32_t sleepS = http.header("X-Sleep-Seconds").toInt();
  if (sleepS == 0) sleepS = FALLBACK_SLEEP_S;
  LOG.printf("[http] %d  sleep=%lu  etag=%s\n", code, (unsigned long)sleepS,
             http.header("ETag").c_str());

  if (code == HTTP_CODE_NOT_MODIFIED) { http.end(); g_fails = 0; return sleepS; }
  if (code != HTTP_CODE_OK || http.getSize() != (int)FB_BYTES) {
    LOG.printf("[http] unexpected response (size %d)\n", http.getSize());
    http.end();
    g_fails++;
    return FALLBACK_SLEEP_S;
  }

  uint8_t* fb = (uint8_t*)ps_malloc(FB_BYTES);
  if (!fb) { LOG.println("[mem] ps_malloc failed -- is PSRAM enabled?"); http.end(); return FALLBACK_SLEEP_S; }

  WiFiClient* s = http.getStreamPtr();
  size_t got = 0;
  uint32_t last = millis();
  while (got < FB_BYTES && millis() - last < 10000) {
    int n = s->available();
    if (n > 0) {
      got += s->readBytes(fb + got, min((size_t)n, FB_BYTES - got));
      last = millis();
    } else {
      delay(2);
    }
  }
  String etag = http.header("ETag");
  http.end();
  WiFi.disconnect(true);   // radio off before the ~15 s refresh
  WiFi.mode(WIFI_OFF);

  if (got != FB_BYTES) {
    LOG.printf("[http] short read %u/%u\n", (unsigned)got, (unsigned)FB_BYTES);
    free(fb);
    g_fails++;
    return FALLBACK_SLEEP_S;
  }

  LOG.println("[epd] drawing");
  const uint32_t t0 = millis();
  epaper.begin();
  epaper.pushImage(0, 0, FB_W, FB_H, (uint16_t*)fb);
  epaper.update();
  epaper.sleep();
  free(fb);
  LOG.printf("[epd] done in %lums\n", millis() - t0);

  strlcpy(g_etag, etag.c_str(), sizeof(g_etag));
  g_fails = 0;
  return sleepS;
}

void setup() {
  LOG.begin(115200, SERIAL_8N1, PIN_DBG_RX, PIN_DBG_TX);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);   // on while awake
  pinMode(PIN_KEY_GREEN, INPUT);
  g_boots++;

  const bool button = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT1;
  const bool cold   = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_UNDEFINED;
  const float vbat  = readBattery();
  LOG.printf("\n[boot] #%lu button=%d cold=%d batt=%.2fV psram=%luKB\n",
             (unsigned long)g_boots, button, cold, vbat,
             (unsigned long)(ESP.getPsramSize() / 1024));

  if (!connectWifi()) {
    LOG.println("[wifi] failed");
    g_fails++;
    deepSleep(g_fails > 3 ? FALLBACK_SLEEP_S * 2 : FALLBACK_SLEEP_S);
  }
  // Cold boot or button press: always redraw, even if the ETag matches.
  deepSleep(fetchAndDraw(button || cold, vbat));
}

void loop() {}
