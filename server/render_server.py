#!/usr/bin/env python3
"""Avian Visitors -> reTerminal E1002 render server.

Every REFRESH_MIN minutes this pulls the top birds near you from BirdWeather,
renders the Avian Visitors collage at 800x480 (re-using the upstream
AvianVisitors frame code + illustrations), dithers it to the E1002's six inks,
and serves it as a ready-to-push 4bpp framebuffer.

Endpoints
  GET /frame.bin    192000 bytes, 4bpp packed, ED2208 sprite codes (hi nibble = left pixel)
                    honours If-None-Match -> 304 so the panel only refreshes on change
                    response header X-Sleep-Seconds tells the device when to wake next
  GET /frame.png    the full-colour render
  GET /preview.png  simulation of what the 6-colour panel will show
  GET /status       JSON: species, last render, last device check-in, battery
  GET /refresh      force a re-fetch + re-render now
"""
from __future__ import annotations

import datetime as dt
import hashlib
import http.server
import io
import json
import logging
import os
import socketserver
import sys
import threading
import time
import urllib.parse
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image, ImageEnhance

AV_FRAME = os.environ.get("AV_FRAME_DIR", "/opt/AvianVisitors/frame")
sys.path.insert(0, AV_FRAME)
import birdweather  # noqa: E402  (upstream AvianVisitors)
import shoot  # noqa: E402

log = logging.getLogger("avian")


# --- config (env vars) -------------------------------------------------------
def env(name, default=""):
    v = os.environ.get(name, "")
    return v.strip() if v.strip() else default


CFG = {
    "zip": env("BW_ZIP"),                        # Canada: first 3 chars only, e.g. N2L
    "country": env("BW_COUNTRY", "ca"),
    "station": env("BW_STATION_ID"),             # use instead of BW_ZIP for one station
    "lat": env("BW_LAT"), "lon": env("BW_LON"),  # or skip the geocoder entirely
    "days": int(env("BW_DAYS", "7")),
    "target": int(env("MAX_BIRDS", "12")),
    "title": env("TITLE", "Heard This Week"),     # small line on top
    "subtitle": env("SUBTITLE", "Avian Visitors"),  # big headline
    "bird_names": env("BIRD_NAMES", "1") not in ("0", "false", "no", "off"),
    "refresh_min": int(env("REFRESH_MIN", "30")),
    # Daily mode: the device wakes only at these local times (comma-separated).
    # Leave empty to wake every REFRESH_MIN instead (with quiet hours).
    "wake_times": [t.strip() for t in env("WAKE_TIMES", "07:00").split(",") if t.strip()],
    "quiet_start": int(env("QUIET_START", "22")),  # device sleeps through these hours
    "quiet_end": int(env("QUIET_END", "6")),       # set both to 0 to disable
    "tz": ZoneInfo(env("TZ", "America/Toronto")),
    "saturation": float(env("SATURATION", "1.35")),
    "contrast": float(env("CONTRAST", "1.1")),
    "collage_vh": float(env("COLLAGE_VH", "80")),
    "headline_px": int(env("HEADLINE_PX", "30")),
    "eyebrow_px": int(env("EYEBROW_PX", "13")),
    "count_exp": float(env("COUNT_EXP", "0.6")),  # bird size vs detections; 1.0 = loudest dominates
    # Illustration style: woodblock (built in), impressionist, audubon,
    # bundle:<catalog id>, or the name of a generated set in STYLES_DIR
    # (e.g. ghibli, linocut - see stylegen.py).
    "style": env("STYLE", "woodblock"),
    "styles_dir": env("STYLES_DIR", "/app/styles"),
    "port": int(env("PORT", "8088")),
    "data": env("DATA_DIR", "/data"),
}

W, H = 800, 480

# ED2208 / Spectra-6 inks. RGB is roughly what the inks *look* like on the
# panel (used for error diffusion so colours match better); code is what the
# Seeed_GFX 4bpp sprite expects (0x0=W 0x2=G 0x6=R 0xB=Y 0xD=B 0xF=K).
INKS = [
    ((242, 242, 236), 0x0),  # white
    ((25, 25, 28), 0xF),     # black
    ((178, 36, 32), 0x6),    # red
    ((222, 200, 30), 0xB),   # yellow
    ((38, 70, 160), 0xD),    # blue
    ((52, 118, 74), 0x2),    # green
]
# What the panel actually shows, for /preview.png
SHOW = [(236, 234, 223), (26, 26, 28), (165, 60, 56), (198, 176, 74), (49, 71, 130), (58, 110, 72)]


# --- state -------------------------------------------------------------------
class State:
    lock = threading.Lock()
    frame_bin: bytes | None = None
    etag = ""
    png: bytes | None = None
    preview: bytes | None = None
    species: list = []
    signature = ""
    rendered_at: float = 0
    error = ""
    device: dict = {}
    force = threading.Event()


S = State()


# --- illustration styles -----------------------------------------------------
BUNDLE_ALIASES = {
    "impressionist": "official-western-us-impressionist",
    "audubon": "official-birds-of-america",
    "birds-of-america": "official-birds-of-america",
}


class Style:
    """Where the collage gets its bird cutouts. The default (woodblock) uses the
    illustrations baked into the upstream checkout."""
    name = "woodblock"
    drawable = None  # None = upstream's own drawable list

    def shoot_kwargs(self):
        return {}


class LocalStyle(Style):
    """A generated set: <dir>/<slug>.png (perched), <slug>-2.png (flight),
    plus dims.json + masks.json from upstream build_masks.py."""

    def __init__(self, name, directory):
        self.name, self.dir = name, directory
        for f in ("dims.json", "masks.json"):
            if not os.path.isfile(os.path.join(directory, f)):
                raise RuntimeError(f"style {name}: {f} missing in {directory}")
        with open(os.path.join(directory, "dims.json")) as f:
            dims = json.load(f)
        self.drawable = frozenset(k[:-2] if k.endswith("-2") else k for k in dims)

    def resolve(self, sci, pose):
        slug = birdweather.slugify(sci) + ("-2" if pose == "flight" else "")
        path = os.path.join(self.dir, slug + ".png")
        if not os.path.isfile(path) and pose == "flight":  # fall back to the perched pose
            path = os.path.join(self.dir, slug[:-2] + ".png")
        return path if os.path.isfile(path) else None

    def shoot_kwargs(self):
        return {"cutout_resolver": self.resolve,
                "dims_path": os.path.join(self.dir, "dims.json"),
                "masks_path": os.path.join(self.dir, "masks.json")}


class BundleStyle(Style):
    """An official/community bundle from avianvisitors.com/bundles, downloaded
    (only the birds this location hears) via upstream's verified bundle runtime."""

    def __init__(self, name, bundle_id):
        import bundle_runtime as br
        self.name, self.bundle_id, self.br = name, bundle_id, br
        self.root = os.path.join(CFG["data"], "bundles", bundle_id)
        self.installed_for = None
        self.active = None
        self.pool = set()

    def ensure(self, species_pool):
        """(Re)install the bundle for these scientific names, at most once a day
        unless new birds show up."""
        pool = sorted({n for n in species_pool if self.br.SCI_RE.fullmatch(n)})
        key = (dt.date.today().isoformat(), tuple(pool))
        if self.active is not None and (self.installed_for == key or set(pool) <= self.pool):
            return
        basis = {"schema_version": 1, "species": pool,
                 "basis_sha256": hashlib.sha256(json.dumps(pool).encode()).hexdigest()}
        self.br._resolve_selection_basis = lambda _cfg: basis  # station list, not the BirdNET model
        log.info("installing bundle %s for %d species", self.bundle_id, len(pool))
        self.br.use(self.bundle_id, root=self.root, config={})
        self.active = self.br.active_bundle(self.root)
        self.installed_for, self.pool = key, set(pool)
        log.info("bundle %s ready: %d drawable species", self.bundle_id, len(self.active.drawable_slugs))

    @property
    def drawable(self):
        return None if self.active is None else self.active.drawable_slugs

    def shoot_kwargs(self):
        a = self.active
        return {"cutout_resolver": a.resolve, "dims_path": a.dims_path,
                "masks_path": a.masks_path, "bundle_assets": a.assets_response}


def make_style(spec):
    spec = (spec or "woodblock").strip()
    if spec in ("", "woodblock", "default"):
        return Style()
    if spec.startswith("bundle:"):
        return BundleStyle(spec, spec.split(":", 1)[1])
    if spec in BUNDLE_ALIASES:
        return BundleStyle(spec, BUNDLE_ALIASES[spec])
    directory = spec if os.path.isabs(spec) else os.path.join(CFG["styles_dir"], spec)
    return LocalStyle(os.path.basename(directory.rstrip("/")), directory)


STYLE = None  # set in main()


# --- birds -------------------------------------------------------------------
def species_pool():
    """Everything the location heard over 30 days - used to pick which bundle
    birds to download."""
    if CFG["station"]:
        rows = birdweather.top_species_for_station(CFG["station"], days=30, limit=200)
    else:
        lat, lon = location()
        rows = birdweather.top_species(lat, lon, 50, days=30, limit=200)
    return [r["sci"] for r in rows]


def location():
    if CFG["lat"] and CFG["lon"]:
        return float(CFG["lat"]), float(CFG["lon"])
    return birdweather.geocode(CFG["zip"], CFG["country"])


def fetch_species(style=None):
    style = style or STYLE
    if isinstance(style, BundleStyle):
        style.ensure(species_pool())
    drawable = {} if style is None or style.drawable is None else {"drawable": style.drawable}
    if CFG["station"]:
        return birdweather.species_for_station(CFG["station"], target=CFG["target"],
                                               days=CFG["days"], **drawable)
    if CFG["lat"] and CFG["lon"]:
        lat, lon = float(CFG["lat"]), float(CFG["lon"])
        birdweather.geocode = lambda *a, **k: (lat, lon)
        return birdweather.species_for_zip("latlon", target=CFG["target"], days=CFG["days"], **drawable)
    if CFG["zip"]:
        return birdweather.species_for_zip(CFG["zip"], country=CFG["country"],
                                           target=CFG["target"], days=CFG["days"], **drawable)
    raise RuntimeError("set BW_ZIP, BW_STATION_ID, or BW_LAT + BW_LON")


def _bucket(n):
    for i, edge in enumerate((1, 2, 5, 15, 40, 100, 300, 1000)):
        if n <= edge:
            return i
    return 8


def signature(species):
    items = sorted((birdweather.slugify(s["sci"]), _bucket(int(s.get("n") or 1))) for s in species)
    style = STYLE.name if STYLE else ""
    return hashlib.sha256(json.dumps([CFG["title"], CFG["subtitle"], style, items]).encode()).hexdigest()[:16]


# --- image -------------------------------------------------------------------
def render(species, style=None, out=None) -> Image.Image:
    style = style or STYLE or Style()
    tmp = out or os.path.join(CFG["data"], "render.png")
    # Some art sets (odd silhouettes, scenery around Audubon's birds) leave a
    # tile with no clean edge for its name at 800x480. Retry with smaller
    # labels, then once more without names, rather than failing the frame.
    attempts = [{"bird_names": CFG["bird_names"]}]
    if CFG["bird_names"]:
        attempts += [{"bird_names": True, "label_min_px": 8}, {"bird_names": False}]
    for i, extra in enumerate(attempts):
        try:
            shoot.shoot_birdweather(
                tmp, species, title=CFG["title"], subtitle=CFG["subtitle"],
                vw=W, vh=H, dsf=2, collage_vh=CFG["collage_vh"],
                headline_px=CFG["headline_px"], eyebrow_px=CFG["eyebrow_px"],
                count_exp=CFG["count_exp"], timeout_ms=120_000,
                **extra, **style.shoot_kwargs(),
            )
            break
        except RuntimeError as e:
            if "labels missing" not in str(e) or i == len(attempts) - 1:
                raise
            log.warning("%s - retrying with %s", e, attempts[i + 1])
    img = Image.open(tmp).convert("RGB")
    return img.resize((W, H), Image.LANCZOS)


def dither(img: Image.Image):
    """Floyd-Steinberg onto the six inks. Returns (packed 4bpp bytes, preview PNG)."""
    img = ImageEnhance.Color(img).enhance(CFG["saturation"])
    img = ImageEnhance.Contrast(img).enhance(CFG["contrast"])
    # Snap neutral near-black (text, outlines) and near-white (paper) to pure
    # ink so lettering stays crisp instead of picking up rainbow dither noise.
    a = np.asarray(img).astype(np.int16)
    chroma = a.max(axis=2) - a.min(axis=2)
    lum = a.mean(axis=2)
    a[(chroma < 40) & (lum < 110)] = 0
    a[(chroma < 30) & (lum > 228)] = 255
    img = Image.fromarray(a.clip(0, 255).astype(np.uint8))
    pal = Image.new("P", (1, 1))
    flat = [c for rgb, _ in INKS for c in rgb]
    pal.putpalette(flat + flat[:3] * (256 - len(INKS)))
    q = img.quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG)
    codes = np.array([code for _, code in INKS], dtype=np.uint8)
    c = codes[np.asarray(q)].reshape(-1)  # one ink code per pixel, row-major
    out = (c[0::2] << 4) | c[1::2]  # high nibble = left pixel
    prev = q.copy()
    prev.putpalette([c for rgb in SHOW for c in rgb] + [255] * 3 * (256 - len(SHOW)))
    buf = io.BytesIO()
    prev.convert("RGB").save(buf, "PNG")
    return bytes(out), buf.getvalue()


def to_png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def update(force=False):
    species = fetch_species()
    sig = signature(species)
    if not force and sig == S.signature and S.frame_bin:
        log.info("no change (%d species, sig %s)", len(species), sig)
        return
    t = time.time()
    img = render(species)
    fb, preview = dither(img)
    with S.lock:
        S.frame_bin, S.png, S.preview = fb, to_png(img), preview
        S.etag = hashlib.sha1(fb).hexdigest()[:16]
        S.species, S.signature, S.rendered_at, S.error = species, sig, time.time(), ""
    for name, data in (("frame.bin", fb), ("frame.png", S.png), ("preview.png", preview)):
        with open(os.path.join(CFG["data"], name), "wb") as f:
            f.write(data)
    log.info("rendered %d species in %.1fs, etag %s", len(species), time.time() - t, S.etag)


def worker():
    while True:
        force = S.force.is_set()
        S.force.clear()
        try:
            update(force=force)
        except Exception as exc:  # keep serving the last good frame
            log.exception("update failed")
            S.error = f"{type(exc).__name__}: {exc}"
        S.force.wait(CFG["refresh_min"] * 60)


def next_wake(now):
    """Next WAKE_TIMES slot after `now` (at least 30 min away, so a device that
    woke a little early for a slot doesn't immediately wake again for it)."""
    best = None
    for t in CFG["wake_times"]:
        h, m = (int(x) for x in t.split(":"))
        for d in (0, 1, 2):
            w = (now + dt.timedelta(days=d)).replace(hour=h, minute=m, second=0, microsecond=0)
            if w - now > dt.timedelta(minutes=30):
                best = w if best is None or w < best else best
                break
    return best


def sleep_seconds():
    """How long the device should deep-sleep. Daily mode: until the next
    WAKE_TIMES slot. Otherwise until just after the next render, or until
    QUIET_END if we're inside quiet hours. The server recomputes this on every
    wake, so the ESP32's drifty sleep timer never accumulates error."""
    now = dt.datetime.now(CFG["tz"])
    if CFG["wake_times"]:
        return int((next_wake(now) - now).total_seconds())
    qs, qe = CFG["quiet_start"], CFG["quiet_end"]
    if qs != qe:
        quiet = (qs <= now.hour < qe) if qs < qe else (now.hour >= qs or now.hour < qe)
        if quiet:
            wake = now.replace(hour=qe, minute=2, second=0, microsecond=0)
            if wake <= now:
                wake += dt.timedelta(days=1)
            return int((wake - now).total_seconds())
    period = CFG["refresh_min"] * 60
    since = time.time() - (S.rendered_at or time.time())
    return int(max(300, period - (since % period) + 90))


# --- http --------------------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _send(self, code, body=b"", ctype="application/octet-stream", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if code != 304:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if code != 304 and self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        if url.path == "/frame.bin":
            S.device = {**S.device, "seen": dt.datetime.now(CFG["tz"]).isoformat(timespec="seconds"),
                        "ip": self.client_address[0], **q}
            if not S.frame_bin:
                return self._send(503, b"not rendered yet", "text/plain", {"X-Sleep-Seconds": "300"})
            hdr = {"ETag": f'"{S.etag}"', "X-Sleep-Seconds": str(sleep_seconds())}
            if self.headers.get("If-None-Match", "").strip('"') == S.etag:
                return self._send(304, headers=hdr)
            log.info("serving new frame to %s (%s)", self.client_address[0], q)
            return self._send(200, S.frame_bin, headers=hdr)
        if url.path in ("/", "/preview.png") and S.preview:
            return self._send(200, S.preview, "image/png")
        if url.path == "/frame.png" and S.png:
            return self._send(200, S.png, "image/png")
        if url.path == "/status":
            body = json.dumps({
                "etag": S.etag, "signature": S.signature, "error": S.error,
                "rendered_at": dt.datetime.fromtimestamp(S.rendered_at, CFG["tz"]).isoformat(timespec="seconds") if S.rendered_at else None,
                "next_device_sleep_s": sleep_seconds(), "device": S.device,
                "species": S.species, "config": {k: str(v) for k, v in CFG.items()},
            }, indent=2).encode()
            return self._send(200, body, "application/json")
        if url.path == "/refresh":
            S.force.set()
            return self._send(200, b"re-render requested\n", "text/plain")
        return self._send(404, b"not found\n", "text/plain")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global STYLE
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.makedirs(CFG["data"], exist_ok=True)
    try:
        STYLE = make_style(CFG["style"])
    except Exception:
        log.exception("style %r unavailable; using the built-in woodblock set", CFG["style"])
        STYLE = Style()
    log.info("illustration style: %s", STYLE.name)
    # Serve the last frame immediately after a restart
    try:
        with open(os.path.join(CFG["data"], "frame.bin"), "rb") as f:
            S.frame_bin = f.read()
        S.etag = hashlib.sha1(S.frame_bin).hexdigest()[:16]
        for name in ("frame.png", "preview.png"):
            with open(os.path.join(CFG["data"], name), "rb") as f:
                setattr(S, "png" if name == "frame.png" else "preview", f.read())
    except OSError:
        pass
    threading.Thread(target=worker, daemon=True).start()
    log.info("listening on :%d", CFG["port"])
    Server(("0.0.0.0", CFG["port"]), Handler).serve_forever()


if __name__ == "__main__":
    main()
