// Copy to config.h and fill in.
#pragma once
#define WIFI_SSID        "your-ssid"
#define WIFI_PASS        "your-password"
#define SERVER_URL       "http://192.168.1.50:8088"   // the machine running the Docker container, no trailing slash
#define DEVICE_HOSTNAME  "avian-frame"
#define FALLBACK_SLEEP_S (60 * 60)                     // first retry when the server is unreachable (doubles each failure)
// Optional: low-battery icon thresholds (volts). Defaults: show < 3.60, clear > 3.85.
// #define LOW_BATT_V 3.60f
// #define OK_BATT_V  3.85f
