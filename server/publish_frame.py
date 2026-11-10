#!/usr/bin/env python3
"""Render today's frame once and write it out as static files.

    python3 publish_frame.py --out site

Used by the daily GitHub Actions job (.github/workflows/publish.yml), which
publishes the folder with GitHub Pages so the frame needs no server:

  frame.bin    192000-byte 4bpp framebuffer, ready to push to the panel
  meta.txt     small key=value file the frame reads first:
                 frame=<id>   changes only when the birds change -> redraw
                 wake=07:00   local wake times (comma-separated)
                 tz=<POSIX>   time zone rule for the ESP32 clock
                 fw=<n>       newest firmware build (see firmware.yml)
  preview.png  6-ink panel simulation, frame.png full colour, index.html
  frame.html   bare 800x480 page showing frame.png (for SenseCraft HMI)

Same env vars as render_server.py (BW_STATION_ID, TITLE, STYLE, ...), plus
WAKE_TIMES and TZ for the device, and FRAME_REV to force a redraw.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("DATA_DIR", "/tmp/avian-publish")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_server as rs  # noqa: E402

FB_BYTES = 800 * 480 // 2


def posix_tz(name: str) -> str:
    """POSIX TZ rule for an IANA zone (the last line of the compiled tzfile),
    e.g. America/Toronto -> EST5EDT,M3.2.0,M11.1.0. The ESP32 needs this form."""
    for root in ("/usr/share/zoneinfo",):
        p = Path(root) / name
        if p.is_file():
            tail = p.read_bytes().rstrip(b"\n").rsplit(b"\n", 1)[-1].decode()
            if tail and "\x00" not in tail:
                return tail
    raise SystemExit(f"unknown time zone {name!r}")


def stamp(img, text):
    """Burn a small 'updated ...' label into the bottom-right corner, so you can
    tell on the panel whether a fresh copy was fetched. STAMP=1 turns it on."""
    from PIL import ImageDraw, ImageFont
    font = None
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",):
        try:
            font = ImageFont.truetype(f, 16)
            break
        except OSError:
            pass
    if font is None:
        try:
            font = ImageFont.load_default(size=16)
        except TypeError:  # Pillow < 10.1
            font = ImageFont.load_default()
    img = img.copy()
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = d.textbbox((0, 0), text, font=font)
    w, h = x1 - x0, y1 - y0
    pad = 5
    bx, by = img.width - w - 2 * pad - 6, img.height - h - 2 * pad - 6
    d.rectangle([bx, by, bx + w + 2 * pad, by + h + 2 * pad], fill="white", outline="black")
    d.text((bx + pad - x0, by + pad - y0), text, font=font, fill="black")
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--fw", type=Path, help="version.txt of the released firmware, if any")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    os.makedirs(rs.CFG["data"], exist_ok=True)

    try:
        style = rs.make_style(rs.CFG["style"])
    except Exception as e:  # missing generated set etc.
        print(f"style {rs.CFG['style']!r} unavailable ({e}); using woodblock")
        style = rs.Style()
    rs.STYLE = style
    species = rs.fetch_species(style)
    if not species:
        raise SystemExit("no species from BirdWeather")
    img = rs.render(species, style, out=str(Path(rs.CFG["data"]) / "render.png"))
    now_local = dt.datetime.now(rs.CFG["tz"])
    stamp_text = "updated " + now_local.strftime("%a %b %-d, %-I:%M %p")
    if os.environ.get("STAMP", "0") not in ("0", "", "false", "no"):
        img = stamp(img, stamp_text)
    fb, preview = rs.dither(img)
    if len(fb) != FB_BYTES:
        raise SystemExit(f"frame.bin is {len(fb)} bytes, expected {FB_BYTES}")

    # The frame only redraws when this id changes: same birds at roughly the
    # same counts -> same id, so day-to-day count jitter doesn't burn a refresh.
    look = [rs.CFG["saturation"], rs.CFG["contrast"], rs.CFG["count_exp"], os.environ.get("FRAME_REV", "")]
    frame_id = hashlib.sha256((rs.signature(species) + json.dumps(look)).encode()).hexdigest()[:16]

    tz_name = os.environ.get("TZ", "America/Toronto")
    fw = "0"
    if a.fw and a.fw.is_file():
        fw = a.fw.read_text().strip() or "0"
    meta = {
        "frame": frame_id,
        "wake": ",".join(rs.CFG["wake_times"]) or "07:00",
        "tz": posix_tz(tz_name),
        "fw": fw,
    }

    (a.out / "frame.bin").write_bytes(fb)
    (a.out / "preview.png").write_bytes(preview)
    img.save(a.out / "frame.png")
    (a.out / "meta.txt").write_text("".join(f"{k}={v}\n" for k, v in meta.items()))
    (a.out / "species.json").write_text(json.dumps(species, indent=1))

    now = dt.datetime.now(rs.CFG["tz"]).strftime("%a %b %-d, %-I:%M %p %Z")
    birds = "".join(f"<li>{html.escape(s['com'])} <small>({int(s.get('n') or 0)})</small></li>" for s in species)
    (a.out / "index.html").write_text(f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Avian Visitors frame</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:820px;margin:24px auto;padding:0 16px;color:#222;background:#faf8f2}}
img{{width:100%;border:1px solid #ccc;background:#fff}}small{{color:#777}}code{{background:#eee;padding:1px 4px}}</style>
<h1>Avian Visitors frame</h1>
<p>Rendered {html.escape(now)} &middot; style <code>{html.escape(style.name)}</code> &middot;
frame id <code>{frame_id}</code> &middot; wakes {html.escape(meta['wake'])} &middot; firmware v{html.escape(fw)}</p>
<h2>What the panel shows</h2><img src="preview.png" alt="6-ink preview">
<h2>Birds</h2><ul>{birds}</ul>
<p><a href="frame.png">Full-colour render</a> &middot; <a href="meta.txt">meta.txt</a></p>
""")
    # Bare 800x480 page for SenseCraft HMI's Web page / HTML widget. The query
    # string changes every publish so nothing serves a stale cached copy.
    v = now_local.strftime("%Y%m%d%H%M")
    (a.out / "frame.html").write_text(f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=800,height=480,initial-scale=1">
<title>Avian Visitors</title>
<style>html,body{{margin:0;padding:0;width:800px;height:480px;overflow:hidden;background:#fff}}
img{{display:block;width:800px;height:480px}}</style>
<img src="frame.png?v={v}" alt="">
""")
    print(f"published {len(species)} species, frame id {frame_id}, meta {meta}")


if __name__ == "__main__":
    main()
