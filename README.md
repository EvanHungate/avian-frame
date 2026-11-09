# Avian Visitors on a reTerminal E1002

A remake of Teddy Warner's [Avian Visitors](https://theodore.net/projects/AvianVisitors/) e-ink frame for the Seeed reTerminal E1002 (7.3" Spectra 6, 800×480). Birds come from public BirdWeather stations near you, so no microphone is needed.

```
BirdWeather ──► render server (Docker, any PC)  ──► reTerminal E1002
                - top birds near your postal code     - wakes once a day   
                - AvianVisitors collage @ 800×480     - GET /frame.bin (ETag)
                - dithered to the 6 inks, 4bpp packed - 304 → back to sleep
                                                      - 200 → draw, sleep
```

The ESP32-S3 never decodes or dithers anything. It downloads a 192,000-byte framebuffer that is ready to push to the panel. By default the frame wakes **once a day at 07:00** (`WAKE_TIMES`), redraws only if the birds changed, and deep-sleeps the rest of the time, so the 2,000 mAh battery should last many months. The server sets each wake time, which also corrects the ESP32's drifting sleep timer every day.

---

## 1. Render server (Docker image)

The server is one Docker image for both amd64 and arm64. It runs on any always-on machine: Docker Desktop on a PC or Mac, a Raspberry Pi 4/5, or a NAS. It needs about 1 GB of disk and about 512 MB of RAM.

### Get the image

**Option A (pre-built, recommended).** GitHub builds and publishes the image for you:
1. Create a new GitHub repo, e.g. `avian-frame`, and push this whole folder to it (the `.github/workflows/docker.yml` file does the work).
2. Watch the **Actions** tab. After about 10 minutes the image is at `ghcr.io/evanhungate/avian-frame:latest`.
3. Optional: under **Packages → avian-frame → Package settings**, set visibility to *Public* so you can pull it without `docker login`.

**Option B (build locally):** run `cd server && docker build -t avian-frame .`

### Run it

```bash
docker run -d --name avian-frame --restart unless-stopped -p 8088:8088 \
  -v avian-data:/data -e TZ=America/Toronto \
  -e BW_STATION_ID=10393 -e TITLE="55 Parkway" -e SUBTITLE="Avian Visitors" \
  ghcr.io/evanhungate/avian-frame:latest
```

Or edit `server/docker-compose.yml` (put your GitHub user in `image:`) and run `docker compose up -d`.

Check `docker logs -f avian-frame`. It should say "rendered N species in ~3s". Give the machine a fixed IP or DHCP reservation, because the reTerminal is pointed at it.

Then open these in a browser:

| URL | What |
|---|---|
| `http://<host>:8088/` | Preview of what the panel will show (6-ink simulation) |
| `/frame.png` | The full-colour render |
| `/status` | Species list, last render, and last device check-in, including battery voltage and RSSI |
| `/refresh` | Force a re-fetch and re-render now |

**Location notes**
- **Station 10393 is the default.** The compose file and the command above follow `BW_STATION_ID=10393`. Only birds that station hears are shown, with no fallback.
- **Canadian postal codes:** use only the first three characters (the FSA), e.g. `N2L`. The keyless geocoder that upstream uses only knows FSAs.
- `BW_LAT` / `BW_LON` skip the geocoder entirely.
- `BW_STATION_ID` follows exactly one station. It is the number at the end of a BirdWeather station URL.
- The search starts at a 15-mile box and grows to 50 miles, then to the nearest 3 stations, until it finds `MAX_BIRDS` drawable species.

**Look and cadence** are set with environment variables in `docker-compose.yml`:

| Variable | Controls |
|---|---|
| `TITLE`, `SUBTITLE` | Frame text |
| `BIRD_NAMES` | Whether bird names are shown |
| `COUNT_EXP` | How much bird size follows detection count (default 0.6; 1.0 lets the loudest bird dominate) |
| `SATURATION`, `CONTRAST` | Pushed up a bit by default, because Spectra inks are muted |
| `BW_DAYS` | Lookback window in days |
| `WAKE_TIMES` | Local times the frame wakes and redraws (default `07:00`; e.g. `07:00,17:00` for twice a day; empty = every `REFRESH_MIN`) |
| `REFRESH_MIN` | How often the server re-checks BirdWeather |
| `QUIET_START`, `QUIET_END` | Hours the device sleeps through (only when `WAKE_TIMES` is empty) |

### Illustration styles

Set `STYLE` to choose the art. See `previews/` for the same birds in each style.

| `STYLE` | What it is | Source |
|---|---|---|
| `woodblock` (default) | Japanese kachō-e woodblock | Built into the image |
| `impressionist` | Loose, energetic gesture sketches | Official bundle, downloaded on first use |
| `audubon` | Audubon's *Birds of America* plates, with their branches and flowers | Official bundle (public domain), downloaded on first use |
| `ghibli` | Soft hand-painted animated-film look | Generated for your birds with Gemini |
| `linocut` | Bold carved print in the panel's own six inks | Generated for your birds with Gemini |

Bundle styles only download the birds your station has heard in the last 30 days.

The generated styles need a paid Google Gemini API key (about 2 images per bird). To make them:
1. Add the key as the `GEMINI_API_KEY` repo secret (**Settings → Secrets and variables → Actions**).
2. Run the **styles** workflow from the **Actions** tab.

The workflow generates the cutouts into `server/styles/<name>/` and renders the previews. It also rebuilds the image so the new style is baked in. You can also run `python3 server/stylegen.py --style ghibli --station 10393` yourself. To add a new style, add a prompt in `server/styles/<name>.prompt.md`.

## 2. Firmware (reTerminal E1002)

**Arduino IDE**
1. Install the **esp32 by Espressif** board package. Install **Seeed_GFX** from Library Manager or from [GitHub](https://github.com/Seeed-Studio/Seeed_GFX).
2. Open `firmware/AvianFrame/AvianFrame.ino`.
3. Copy `config.example.h` to `config.h`. Fill in Wi-Fi details and `SERVER_URL`, e.g. `http://192.168.1.50:8088`.
4. Set **Tools → Board → XIAO_ESP32S3** and **Tools → PSRAM → OPI PSRAM**. PSRAM is required.
5. Upload. `driver.h` already selects `BOARD_SCREEN_COMBO 521` (E1002).

**PlatformIO** (alternative): run `cd firmware && pio run -t upload`.

**Behaviour**
- **On power-up:** always draws.
- **Daily wake:** draws only when the server's ETag has changed, then sleeps until the next `WAKE_TIMES` slot.
- **Green button (right, GPIO3):** wakes the device and redraws now. To get a fresh render first, hit `/refresh`, wait about 5 seconds, then press the button.
- **Low battery:** below about 10 % (3.60 V) a small battery icon with a red bar appears in the bottom-right corner. It clears after charging (above 3.85 V). Tune with `LOW_BATT_V` / `OK_BATT_V` in `config.h`.
- **LED:** on while awake.
- **Logs:** go out on the USB-C serial port at 115200.
- **Server unreachable:** keeps the last image and retries after 1 h, then 2 h, 4 h … up to 24 h, so a dead server can't drain the battery.

## How it maps to the original

| Original | Here |
|---|---|
| BirdNET-Pi mic station | BirdWeather public stations (upstream `frame/birdweather.py`) |
| Pi Zero renders the collage in headless Chromium | Docker container runs the same `shoot.py` at 800×480 |
| Pimoroni Inky 13.3" (Spectra 6) | reTerminal E1002 7.3" (Spectra 6) via Seeed_GFX |
| 15-minute systemd timer | ESP32 deep sleep; the server sets the wake time |

The illustrations, collage packing, and BirdWeather logic all come unchanged from [Twarner491/AvianVisitors](https://github.com/Twarner491/AvianVisitors), cloned at image build time. That project is licensed CC BY-NC-SA 4.0, so keep this setup non-commercial.

## Tuning ideas
- **Want your own birds later:** put a BirdNET-Go/Pi station on BirdWeather and switch to `BW_STATION_ID`.
- **Grey birds look speckled:** Spectra 6 has no grey ink, so greys are dithered from the colour inks. Lowering `SATURATION` to about 1.1 calms this.
- **Mixed-up colours on the panel:** if a colour is consistently wrong (e.g. blue shows as green), the ink code mapping in `INKS` in `render_server.py` is the place to fix it.
