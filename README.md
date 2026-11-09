# Avian Visitors on a reTerminal E1002

A remake of Teddy Warner's [Avian Visitors](https://theodore.net/projects/AvianVisitors/) e-ink frame for the Seeed reTerminal E1002 (7.3" Spectra 6, 800×480). Birds come from public BirdWeather stations near you, so no microphone is needed.

```
BirdWeather ──► render server (Docker, any PC)  ──► reTerminal E1002
                - top birds near your postal code     - wakes every ~30 min
                - AvianVisitors collage @ 800×480     - GET /frame.bin (ETag)
                - dithered to the 6 inks, 4bpp packed - 304 → back to sleep
                                                      - 200 → draw, sleep
```

The ESP32-S3 never decodes or dithers anything. It downloads a 192,000-byte framebuffer that is ready to push to the panel. The panel only refreshes when the bird set changes. The device sleeps through quiet hours (22:00–06:00 by default), which keeps battery use low.

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
| `SATURATION`, `CONTRAST` | Pushed up a bit by default, because Spectra inks are muted |
| `BW_DAYS` | Lookback window in days |
| `REFRESH_MIN` | How often the server re-checks BirdWeather |
| `QUIET_START`, `QUIET_END` | Hours the device sleeps through |

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
- **On timer wake:** draws only when the server's ETag has changed.
- **Green button (right, GPIO3):** wakes the device and redraws now. To get a fresh render first, hit `/refresh`, wait about 5 seconds, then press the button.
- **LED:** on while awake.
- **Logs:** go out on the USB-C serial port at 115200.
- **Server unreachable:** keeps the last image and retries in 30 minutes.

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
