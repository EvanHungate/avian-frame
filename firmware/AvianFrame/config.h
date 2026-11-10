// Avian Visitors frame settings. No passwords live here: Wi-Fi is set up on
// the device itself (it shows a setup screen), so this file is committed and
// GitHub Actions can build the firmware.
#pragma once

// Where the daily picture is published (GitHub Pages), no trailing slash.
#define SITE_URL         "https://evanhungate.github.io/avian-frame"

// Wi-Fi setup: the frame opens this open hotspot when it has no Wi-Fi yet,
// can't join its saved network after power-on or a button press, or when the
// green button is held for 5 seconds.
#define SETUP_AP_NAME    "AvianFrame-Setup"
#define PORTAL_TIMEOUT_S (15 * 60)   // give up and sleep after 15 min unused

#define DEVICE_HOSTNAME  "avian-frame"

// Used until the first successful download; after that meta.txt decides
// (set WAKE_TIMES / TZ as repo variables, see README).
#define TZ_DEFAULT       "EST5EDT,M3.2.0,M11.1.0"   // America/Toronto
#define WAKE_DEFAULT     "07:00"

#define FALLBACK_SLEEP_S (60 * 60)   // first retry after a failure (doubles each time, max 24 h)

// Low-battery icon thresholds (volts): show below LOW, clear above OK.
#define LOW_BATT_V 3.60f
#define OK_BATT_V  3.85f

// Written by the firmware workflow: FW_VERSION and (optionally) HC_URL, a
// healthchecks.io ping URL from the HC_PING_URL repo secret.
#if __has_include("build_secrets.h")
#include "build_secrets.h"
#endif
#ifndef FW_VERSION
#define FW_VERSION 0                 // local builds: any published release updates over it
#endif
#ifndef HC_URL
#define HC_URL ""                    // e.g. "https://hc-ping.com/<uuid>" for daily check-ins
#endif
