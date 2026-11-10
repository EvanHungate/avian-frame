# Avian Visitors on a reTerminal E1002

A remake of Teddy Warner's [Avian Visitors](https://theodore.net/projects/AvianVisitors/) e-ink frame for the Seeed reTerminal E1002 (7.3" Spectra 6, 800×480). The birds come from a public BirdWeather station, so the frame needs no microphone. It also needs no server.

```
GitHub Actions, every morning              GitHub Pages                  reTerminal E1002
- top birds at BirdWeather station 10393   evanhungate.github.io/       - wakes at 07:00
- AvianVisitors collage at 800×480    ──►   avian-frame/            ──►  - reads meta.txt
- dithered to the 6 inks, 4bpp               frame.bin, meta.txt,          - birds changed? draw
                                             preview, firmware             - deep sleep till tomorrow
```

The frame downloads a ready-made 192,000-byte framebuffer over HTTPS. It redraws only when the set of birds changes, and deep-sleeps the rest of the time, so the 2,000 mAh battery should last for months.

**Status page:** <https://evanhungate.github.io/avian-frame/> shows what the panel shows today, the bird list, and the current firmware version.

---

## For whoever has the frame

- **First switch-on:** the screen says *"Let's get this frame on your Wi-Fi"*.
  1. On a phone, join the Wi-Fi network **AvianFrame-Setup**.
  2. A setup page opens. If it doesn't, go to `192.168.4.1` in a browser.
  3. Tap **Configure WiFi**, pick your home network, and type its password.

  The birds appear a minute or so later.
- **New router or password:** hold the **green button** (right side) for 5 seconds, then do the same steps again.
- **See today's birds now:** press the green button once.
- **Small battery icon in the bottom-right corner:** charge it over USB-C.
- **Wi-Fi network:** the frame only joins 2.4 GHz Wi-Fi. Most routers broadcast 2.4 GHz and 5 GHz under the same name, and that works.

---

## Setup (the maker's side)

### 1. Daily picture: GitHub Actions + Pages

`.github/workflows/publish.yml` runs every morning, around 5 am Toronto time. It renders the frame with the upstream AvianVisitors code and publishes it with GitHub Pages. Pages has to be set to **Settings → Pages → Source: GitHub Actions**. You can also start a run by hand: **Actions → publish → Run workflow**.

The workflow keeps its own schedule switched on. Normally GitHub pauses schedules after 60 days without commits. If a run fails, for example because BirdWeather is down, GitHub emails you and the frame keeps yesterday's picture.

**Settings** are repo variables, under **Settings → Secrets and variables → Actions → Variables**. The frame picks them up on its next wake, so there's no reflashing.

| Variable | Default | Controls |
|---|---|---|
| `BW_STATION_ID` | `10393` | BirdWeather station (the number at the end of its URL) |
| `STYLE` | `woodblock` | Illustration set (see below) |
| `WAKE_TIMES` | `07:00` | Local times the frame wakes, e.g. `07:00,17:00` |
| `FRAME_TZ` | `America/Toronto` | The frame's time zone (IANA name) |
| `TITLE` / `SUBTITLE` | `{date}` / `Avian Visitors This Week` | Small line / headline; `{date}` becomes e.g. Tuesday November 10 2026 |
| `FRAME_REV` | | Change it to any new value to force every frame to redraw |

### 2. Flash the frame once over USB

After that, updates arrive over Wi-Fi (see step 4).

**Option A: no software to install.**
1. Open **Actions → firmware**, open the latest green run, and download the `firmware-v…` artifact.
2. In Chrome, open <https://espressif.github.io/esptool-js/> and connect the reTerminal over USB-C.
3. Flash `firmware-full.bin` at address **0x0**.

**Option B: Arduino IDE.**
1. Install **esp32 by Espressif** (3.x).
2. Install the libraries **Seeed_GFX** (from [GitHub](https://github.com/Seeed-Studio/Seeed_GFX)) and **WiFiManager** by tzapu (from the Library Manager).
3. Open `firmware/AvianFrame/AvianFrame.ino`.
4. Set **Board: XIAO_ESP32S3** and **PSRAM: OPI PSRAM**. PSRAM is required.
5. Upload.

`config.h` has no passwords in it. The Wi-Fi network is chosen on the frame itself.

The frame logs to the USB-C serial port at 115200 baud, which helps when testing.

### 3. Optional: a daily check-in email

1. Create a free check at [healthchecks.io](https://healthchecks.io) with a period of 1 day and a grace time of a few hours.
2. Copy its ping URL into a repo **secret** called `HC_PING_URL`.
3. Make a firmware release (step 4).

From then on:

- The frame pings the check every morning with its battery voltage, Wi-Fi signal strength and firmware version.
- You get an email if a day is missed.
- You also get an email when the battery gets low. The frame sends a "fail" ping with the reason in the body.

The ping URL ends up inside the published firmware file. Anyone who found it could only send fake check-ins.

### 4. Updating the firmware remotely

**Actions → firmware → Run workflow**, with **release** ticked. That run:

- builds the firmware;
- stores it in `firmware/release/`;
- republishes the site.

Each frame installs the new version on its next wake, but only over a verified HTTPS connection and only if the battery isn't nearly empty. If a new version crashes before it completes one full wake, the frame goes back to the previous one.

Every push to `firmware/` is also compiled in CI. This catches build errors early, but nothing is sent to frames unless you tick release.

## Illustration styles

Set the `STYLE` variable. See `previews/` for the same birds drawn in each style.

| `STYLE` | What it is | Source |
|---|---|---|
| `woodblock` (default) | Japanese kachō-e woodblock | Upstream, built in |
| `impressionist` | Loose, energetic gesture sketches | Official bundle, downloaded on demand |
| `audubon` | Audubon's *Birds of America* plates | Official bundle (public domain) |
| `ghibli` | Soft, hand-painted animated-film look | Generated (only a 5-bird test set so far) |
| `linocut` | Bold carved print in the panel's six inks | Generated |

**Making a generated style:**

1. Add a `KIE_API_KEY` ([kie.ai](https://kie.ai)) or `GEMINI_API_KEY` secret.
2. Run **Actions → styles**. It costs about 2 images per bird, and only birds that are missing get generated.
3. To add a brand-new style, put its prompt in `server/styles/<name>.prompt.md`.

## Running your own server instead (optional)

`server/` also builds a Docker image (`docker.yml` → `ghcr.io/evanhungate/avian-frame`). It serves the same render at `http://<host>:8088/`, which is handy for trying settings or styles locally:

```bash
docker run -d --name avian-frame -p 8088:8088 -v avian-data:/data -e TZ=America/Toronto \
  -e BW_STATION_ID=10393 ghcr.io/evanhungate/avian-frame:latest
```

Endpoints are `/` (6-ink preview), `/frame.png`, `/status` and `/refresh`. The current firmware reads from GitHub Pages, not from this server.

## Tuning

- **Speckled grey birds:** Spectra 6 has no grey ink, so greys are dithered from the colour inks. Lowering `SATURATION` in `publish.yml` to about 1.1 calms this.
- **Wrong colours on the panel:** if a colour is consistently wrong (e.g. blue shows as green), fix the ink codes in `INKS` in `server/render_server.py`.

## Credits

The illustrations, the collage packing and the BirdWeather logic come unchanged from [Twarner491/AvianVisitors](https://github.com/Twarner491/AvianVisitors), cloned at build time. That project is licensed CC BY-NC-SA 4.0, so keep this setup non-commercial.
