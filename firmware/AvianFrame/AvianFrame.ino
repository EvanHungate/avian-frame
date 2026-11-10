/*
 * Avian Visitors frame -- reTerminal E1002 (7.3" Spectra 6, 800x480)
 *
 * No server: GitHub Actions renders the picture every morning and publishes
 * it with GitHub Pages (SITE_URL). Each wake:
 *   Wi-Fi -> internet time -> GET meta.txt (frame id, wake times, time zone,
 *   newest firmware) -> install new firmware if there is one -> if the frame
 *   id changed, GET frame.bin (192000 bytes, 4bpp) and draw it -> optional
 *   check-in ping -> deep sleep until the next wake time (07:00 by default).
 *
 * Wi-Fi setup: with no saved Wi-Fi (or after power-on / a button press when
 * the saved network can't be joined) the panel shows instructions and the
 * frame opens the "AvianFrame-Setup" hotspot. Join it from a phone, pick the
 * home network, done. Hold the green button 5 s to do it again.
 *
 * Green button (right, KEY0/GPIO3): press = redraw now, hold 5 s = Wi-Fi setup.
 * Low battery: below LOW_BATT_V a small battery icon appears bottom-right.
 * If anything fails it retries after 1 h, 2 h, 4 h ... up to 24 h.
 *
 * Arduino IDE: board "XIAO_ESP32S3", PSRAM "OPI PSRAM" (required).
 * Libraries: Seeed_GFX (GitHub), WiFiManager by tzapu (Library Manager).
 * Logs: Serial1 @115200 on the USB-C port (GPIO43 TX / GPIO44 RX).
 */
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>
#include <WiFiManager.h>
#include <esp_sleep.h>
#include <esp_ota_ops.h>
#include <time.h>
#include "TFT_eSPI.h"
#include "config.h"
#include "ca_roots.h"

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
static constexpr uint32_t HOLD_FOR_SETUP_MS = 5000;

// Survives deep sleep
RTC_DATA_ATTR char     g_frame[24]  = "";            // id of the picture on the panel
RTC_DATA_ATTR char     g_tz[64]     = TZ_DEFAULT;
RTC_DATA_ATTR char     g_wake[64]   = WAKE_DEFAULT;
RTC_DATA_ATTR uint32_t g_boots = 0;
RTC_DATA_ATTR uint8_t  g_fails = 0;
RTC_DATA_ATTR bool     g_lowShown = false;           // battery icon on the panel?
RTC_DATA_ATTR bool     g_setupShown = false;         // setup screen on the panel?
RTC_DATA_ATTR int32_t  g_otaSkip = -1;               // firmware version that failed to install

EPaper epaper;
static bool g_epBegun = false;
WiFiClientSecure tls;

// Keep a freshly installed firmware "on probation": if it crashes before it
// first reaches deep sleep, the bootloader goes back to the previous one.
extern "C" bool verifyRollbackLater() { return true; }

static void epBegin() {
  if (!g_epBegun) { epaper.begin(); g_epBegun = true; }
}

static float readBattery() {
  pinMode(PIN_BATT_EN, OUTPUT);
  digitalWrite(PIN_BATT_EN, HIGH);
  delay(10);
  uint32_t mv = 0;
  for (int i = 0; i < 8; ++i) mv += analogReadMilliVolts(PIN_BATT_ADC);
  digitalWrite(PIN_BATT_EN, LOW);
  return (mv / 8) * 2 / 1000.0f;   // 2:1 divider
}

static void deepSleep(uint32_t seconds) {
  if (seconds < 60) seconds = 60;
  if (seconds > 30 * 3600) seconds = 30 * 3600;
  esp_ota_mark_app_valid_cancel_rollback();   // made it through a full wake: keep this firmware
  for (int i = 0; i < 300 && digitalRead(PIN_KEY_GREEN) == LOW; ++i) delay(10);  // wait for release
  if (g_epBegun) epaper.sleep();
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

// Retry delay after a failure: FALLBACK_SLEEP_S doubled per consecutive
// failure, capped at a day.
static uint32_t backoff() {
  uint32_t s = FALLBACK_SLEEP_S;
  for (uint8_t i = 1; i < g_fails && s < 24 * 3600; ++i) s *= 2;
  return s > 24 * 3600 ? 24 * 3600 : s;
}

static void fail(const char* why) {
  LOG.printf("[fail] %s\n", why);
  if (g_fails < 250) g_fails++;
  deepSleep(backoff());
}

// ---- screens ----------------------------------------------------------------
static void centered(const char* s, int y, int size, uint16_t color = TFT_BLACK) {
  epaper.setTextSize(size);
  epaper.setTextColor(color, TFT_WHITE);
  epaper.drawString(s, FB_W / 2, y);
}

static void drawSetupScreen() {
  LOG.println("[epd] setup screen");
  epBegin();
  epaper.fillScreen(TFT_WHITE);
  epaper.setTextFont(1);
  epaper.setTextDatum(TC_DATUM);
  centered("Avian Visitors", 40, 5);
  centered("Let's get this frame on your Wi-Fi", 110, 3);
  centered("1. On your phone, join the Wi-Fi network", 175, 2);
  epaper.fillRect(170, 205, 460, 52, TFT_GREEN);
  epaper.setTextSize(4);
  epaper.setTextColor(TFT_WHITE, TFT_GREEN);
  epaper.drawString(SETUP_AP_NAME, FB_W / 2, 215);
  centered("2. A setup page opens (if not, browse to 192.168.4.1)", 285, 2);
  centered("3. Tap \"Configure WiFi\", pick your network, enter its password", 320, 2);
  centered("The birds appear a minute later.", 375, 2, TFT_BLUE);
  centered("Hold the green button for 5 seconds to do this again.", 440, 1);
  epaper.update();
  g_setupShown = true;
}

// Small low-battery icon in the bottom-right corner: white backing so it reads
// over any bird, black outline, one red bar for "nearly empty". ~34x18 px.
static void drawLowBattery() {
  const int w = 26, h = 14, nub = 3;
  const int x = FB_W - w - nub - 10, y = FB_H - h - 10;
  epaper.fillRect(x - 3, y - 3, w + nub + 6, h + 6, TFT_WHITE);
  epaper.drawRect(x, y, w, h, TFT_BLACK);
  epaper.drawRect(x + 1, y + 1, w - 2, h - 2, TFT_BLACK);   // 2 px outline
  epaper.fillRect(x + w, y + 4, nub, h - 8, TFT_BLACK);     // terminal
  epaper.fillRect(x + 3, y + 3, 5, h - 6, TFT_RED);         // last bar
}

// ---- Wi-Fi --------------------------------------------------------------------
static bool joinSaved(uint32_t timeoutMs = 20000) {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(DEVICE_HOSTNAME);
  WiFi.begin();   // credentials saved by the setup portal
  const uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - t0 > timeoutMs) return false;
    delay(100);
  }
  LOG.printf("[wifi] %s rssi=%d in %lums\n", WiFi.localIP().toString().c_str(),
             WiFi.RSSI(), millis() - t0);
  return true;
}

// Show the instructions and run the setup hotspot. Returns once connected;
// sleeps (keeping the instructions on screen) if nobody finishes setup.
static void runSetup(WiFiManager& wm) {
  drawSetupScreen();
  wm.setConfigPortalTimeout(PORTAL_TIMEOUT_S);
  wm.setConnectTimeout(30);
  wm.setTitle("Avian Visitors");
  wm.setMenu(std::vector<const char*>{"wifi", "exit"});
  digitalWrite(PIN_LED, LOW);
  if (!wm.startConfigPortal(SETUP_AP_NAME)) {
    LOG.println("[wifi] setup timed out");
    deepSleep(24 * 3600);   // retries any saved network tomorrow; button = setup again
  }
  LOG.printf("[wifi] set up, joined %s\n", WiFi.SSID().c_str());
  g_fails = 0;
}

// ---- time --------------------------------------------------------------------
static bool syncClock() {
  configTzTime(g_tz, "pool.ntp.org", "time.google.com", "time.cloudflare.com");
  time_t now = 0;
  for (int i = 0; i < 100 && now < 1700000000; ++i) { delay(100); time(&now); }
  if (now < 1700000000) { LOG.println("[time] NTP failed"); return false; }
  struct tm t;
  localtime_r(&now, &t);
  LOG.printf("[time] %04d-%02d-%02d %02d:%02d %s\n", t.tm_year + 1900, t.tm_mon + 1,
             t.tm_mday, t.tm_hour, t.tm_min, g_tz);
  return true;
}

// Seconds until the next wake time that is at least 30 min away, so a wake a
// little early (the sleep timer drifts) doesn't draw twice.
static uint32_t secondsToNextWake() {
  time_t now;
  time(&now);
  if (now < 1700000000) return 12 * 3600;   // no clock: check again in 12 h
  time_t best = 0;
  char buf[sizeof(g_wake)];
  strlcpy(buf, g_wake, sizeof(buf));
  for (char* tok = strtok(buf, ", "); tok; tok = strtok(nullptr, ", ")) {
    int hh = 0, mm = 0;
    if (sscanf(tok, "%d:%d", &hh, &mm) < 1 || hh < 0 || hh > 23 || mm < 0 || mm > 59) continue;
    for (int day = 0; day <= 1; ++day) {
      struct tm t;
      localtime_r(&now, &t);
      t.tm_mday += day;
      t.tm_hour = hh; t.tm_min = mm; t.tm_sec = 0;
      t.tm_isdst = -1;
      time_t c = mktime(&t);
      if (c > now + 30 * 60 && (best == 0 || c < best)) best = c;
    }
  }
  if (best == 0) return 24 * 3600;
  return (uint32_t)(best - now) + 20;   // land just after the minute
}

// ---- HTTPS --------------------------------------------------------------------
// Certificates are checked against ca_roots.h. If that ever fails (GitHub moves
// to a root we don't carry) the picture is still fetched without the check,
// but firmware updates are only ever installed over a verified connection.
static bool g_verified = true;

static bool beginGet(HTTPClient& http, const String& url) {
  http.setTimeout(15000);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  return http.begin(tls, url);
}

static int get(HTTPClient& http, const String& url) {
  tls.setCACert(CA_ROOTS);
  g_verified = true;
  int code = beginGet(http, url) ? http.GET() : -1;
  if (code < 0) {
    LOG.printf("[http] %s: %s, retrying without certificate check\n", url.c_str(),
               http.errorToString(code).c_str());
    http.end();
    tls.setInsecure();
    g_verified = false;
    code = beginGet(http, url) ? http.GET() : -1;
  }
  return code;
}

struct Meta { String frame, wake, tz; long fw = 0; };

static bool fetchMeta(Meta& m) {
  HTTPClient http;
  const int code = get(http, String(SITE_URL) + "/meta.txt?b=" + String(g_boots));
  if (code != HTTP_CODE_OK) { LOG.printf("[meta] HTTP %d\n", code); http.end(); return false; }
  String body = http.getString();
  http.end();
  int start = 0;
  while (start < (int)body.length()) {
    int nl = body.indexOf('\n', start);
    if (nl < 0) nl = body.length();
    String line = body.substring(start, nl);
    line.trim();
    start = nl + 1;
    const int eq = line.indexOf('=');
    if (eq <= 0) continue;
    String k = line.substring(0, eq), v = line.substring(eq + 1);
    if (k == "frame") m.frame = v;
    else if (k == "wake") m.wake = v;
    else if (k == "tz") m.tz = v;
    else if (k == "fw") m.fw = v.toInt();
  }
  LOG.printf("[meta] frame=%s wake=%s fw=%ld (running %d)\n", m.frame.c_str(),
             m.wake.c_str(), m.fw, FW_VERSION);
  return m.frame.length() > 0;
}

static void maybeUpdateFirmware(const Meta& m, float vbat) {
  if (m.fw <= FW_VERSION || m.fw == g_otaSkip) return;
  if (!g_verified) { LOG.println("[ota] skipped: connection not verified"); return; }
  if (vbat > 1.0f && vbat < 3.55f) { LOG.println("[ota] skipped: battery low"); return; }
  LOG.printf("[ota] installing v%ld\n", m.fw);
  tls.setCACert(CA_ROOTS);
  httpUpdate.rebootOnUpdate(true);
  httpUpdate.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  const String url = String(SITE_URL) + "/firmware.bin?v=" + String(m.fw);
  const t_httpUpdate_return r = httpUpdate.update(tls, url);   // reboots on success
  LOG.printf("[ota] failed (%d): %s\n", (int)r, httpUpdate.getLastErrorString().c_str());
  g_otaSkip = m.fw;   // don't retry this version every day
}

// Download the framebuffer into PSRAM. Caller frees.
static uint8_t* fetchFrame(const String& id) {
  HTTPClient http;
  const int code = get(http, String(SITE_URL) + "/frame.bin?v=" + id);
  const int size = http.getSize();
  if (code != HTTP_CODE_OK || (size != -1 && size != (int)FB_BYTES)) {
    LOG.printf("[frame] HTTP %d size %d\n", code, size);
    http.end();
    return nullptr;
  }
  uint8_t* fb = (uint8_t*)ps_malloc(FB_BYTES);
  if (!fb) { LOG.println("[mem] ps_malloc failed -- is PSRAM enabled?"); http.end(); return nullptr; }
  WiFiClient* s = http.getStreamPtr();
  size_t got = 0;
  uint32_t last = millis();
  while (got < FB_BYTES && millis() - last < 15000) {
    const int n = s->available();
    if (n > 0) {
      got += s->readBytes(fb + got, min((size_t)n, FB_BYTES - got));
      last = millis();
    } else if (!s->connected()) {
      break;
    } else {
      delay(2);
    }
  }
  http.end();
  if (got != FB_BYTES) {
    LOG.printf("[frame] short read %u/%u\n", (unsigned)got, (unsigned)FB_BYTES);
    free(fb);
    return nullptr;
  }
  return fb;
}

// Optional daily check-in (healthchecks.io): you get an email if the frame
// stops checking in, or when the battery gets low (sent as a "fail" ping).
static void checkIn(float vbat, bool lowBatt, bool drew) {
  if (!HC_URL[0]) return;
  HTTPClient http;
  tls.setCACert(CA_ROOTS);
  String url = String(HC_URL) + (lowBatt ? "/fail" : "");
  if (!beginGet(http, url)) return;
  String body = String("batt=") + String(vbat, 2) + "V rssi=" + String(WiFi.RSSI()) +
                " fw=" + String(FW_VERSION) + " boot=" + String(g_boots) +
                " drew=" + (drew ? "yes" : "no") + " frame=" + String(g_frame) +
                (lowBatt ? " LOW BATTERY - please charge (USB-C)" : "");
  const int code = http.POST(body);
  LOG.printf("[checkin] %d\n", code);
  http.end();
}

void setup() {
  LOG.begin(115200, SERIAL_8N1, PIN_DBG_RX, PIN_DBG_TX);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);   // on while awake
  pinMode(PIN_KEY_GREEN, INPUT);
  g_boots++;

  const auto cause = esp_sleep_get_wakeup_cause();
  const bool button = cause == ESP_SLEEP_WAKEUP_EXT1;
  const bool cold   = cause == ESP_SLEEP_WAKEUP_UNDEFINED;

  // Holding the button for 5 s means "set up Wi-Fi again". The LED blinks
  // once the hold has been recognised.
  bool held = false;
  if (button) {
    const uint32_t t0 = millis();
    while (digitalRead(PIN_KEY_GREEN) == LOW && millis() - t0 < HOLD_FOR_SETUP_MS) delay(20);
    held = digitalRead(PIN_KEY_GREEN) == LOW;
    if (held) for (int i = 0; i < 6; ++i) { digitalWrite(PIN_LED, i & 1); delay(120); }
  }

  const float vbat = readBattery();
  LOG.printf("\n[boot] #%lu fw=%d button=%d held=%d cold=%d batt=%.2fV psram=%luKB\n",
             (unsigned long)g_boots, FW_VERSION, button, held, cold, vbat,
             (unsigned long)(ESP.getPsramSize() / 1024));

  // ---- Wi-Fi ----
  WiFi.mode(WIFI_STA);
  WiFiManager wm;
  wm.setDebugOutput(false);
  wm.setHostname(DEVICE_HOSTNAME);
  const bool saved = wm.getWiFiIsSaved();
  if (!saved && !held && !cold && !button) {
    deepSleep(24 * 3600);   // never set up and nobody around: the setup screen stays up
  } else if (held || !saved) {
    runSetup(wm);
  } else if (!joinSaved()) {
    // Only an attended wake (power-on or button) opens setup; a failed 7 am
    // wake just retries later so a router outage can't drain the battery.
    if (cold || button) runSetup(wm);
    else fail("wifi");
  }

  // ---- time + what's published ----
  syncClock();
  Meta m;
  if (!fetchMeta(m)) fail("meta");
  if (m.tz.length() && m.tz.length() < sizeof(g_tz) && m.tz != g_tz) {
    strlcpy(g_tz, m.tz.c_str(), sizeof(g_tz));
    setenv("TZ", g_tz, 1);
    tzset();
  }
  if (m.wake.length() && m.wake.length() < sizeof(g_wake)) strlcpy(g_wake, m.wake.c_str(), sizeof(g_wake));

  maybeUpdateFirmware(m, vbat);

  // ---- battery icon (with hysteresis; ~0 V means no battery fitted) ----
  bool lowBatt = g_lowShown;
  if (vbat > 1.0f && vbat < LOW_BATT_V) lowBatt = true;
  if (vbat > OK_BATT_V) lowBatt = false;
  const bool iconChange = lowBatt != g_lowShown;

  // ---- draw if the birds changed (or a person is watching) ----
  const bool draw = m.frame != g_frame || button || cold || iconChange || g_setupShown;
  uint8_t* fb = nullptr;
  if (draw) {
    fb = fetchFrame(m.frame);
    if (!fb) fail("frame");
  }
  checkIn(vbat, lowBatt, draw);
  WiFi.disconnect(true);   // radio off before the ~20 s refresh
  WiFi.mode(WIFI_OFF);

  if (fb) {
    LOG.println("[epd] drawing");
    const uint32_t t0 = millis();
    epBegin();
    epaper.pushImage(0, 0, FB_W, FB_H, (uint16_t*)fb);
    if (lowBatt) drawLowBattery();
    epaper.update();
    free(fb);
    LOG.printf("[epd] done in %lums\n", millis() - t0);
    strlcpy(g_frame, m.frame.c_str(), sizeof(g_frame));
    g_lowShown = lowBatt;
    g_setupShown = false;
  } else {
    LOG.println("[epd] birds unchanged");
  }
  g_fails = 0;
  deepSleep(secondsToNextWake());
}

void loop() {}
