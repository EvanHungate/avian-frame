#!/usr/bin/env python3
"""Generate a new illustration style for the birds your station hears.

Uses the upstream AvianVisitors pipeline pieces:
  Wikipedia photo (anatomy reference) -> Gemini image model with a style prompt
  -> rembg/BiRefNet cutout -> build_masks.py (dims.json + masks.json)

    GEMINI_API_KEY=... python3 stylegen.py --style ghibli --station 10393
    KIE_API_KEY=...    python3 stylegen.py --style ghibli --station 10393   # via kie.ai

Two providers for the same model (Google's "Nano Banana", gemini-2.5-flash-image):
  KIE_API_KEY     kie.ai reseller - cheaper, prepaid credits (used if set)
  GEMINI_API_KEY  Google AI Studio directly - needs billing enabled

Writes styles/<style>/<slug>.png (perched), <slug>-2.png (flight), dims.json,
masks.json - the layout render_server.py's STYLE=<style> expects. Only birds
not already generated are made, so re-running after new birds turn up is cheap.
About 2 images per bird.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AV = Path(os.environ.get("AV_ROOT", "/opt/AvianVisitors"))
sys.path.insert(0, str(AV / "frame"))
sys.path.insert(0, str(AV / "avian" / "scripts"))
import birdweather  # noqa: E402
import pregen  # noqa: E402

MAX_SIDE = 768  # keep cutouts small; the frame draws birds at <=300 px


KIE = "https://api.kie.ai/api/v1/jobs"
UA = {"User-Agent": "avian-frame-stylegen/1.0"}


def wiki_image_url(sci, com):
    """Public URL of the species' Wikipedia lead photo (kie.ai takes image URLs,
    not uploads)."""
    for title in (sci.replace(" ", "_"), com.replace(" ", "_")):
        try:
            req = urllib.request.Request(
                "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title), headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                meta = json.loads(r.read())
        except Exception:
            continue
        src = (meta.get("thumbnail") or meta.get("originalimage") or {}).get("source")
        if src and urllib.parse.urlsplit(src).path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            return src
    return None


def kie_generate(key, prompt, ref_url, timeout=300):
    """One image via kie.ai's google/nano-banana-edit (or nano-banana with no
    reference). Returns PNG bytes."""
    auth = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", **UA}
    body = {"model": "google/nano-banana-edit" if ref_url else "google/nano-banana",
            "input": {"prompt": prompt[:5000], "output_format": "png", "image_size": "1:1",
                      **({"image_urls": [ref_url]} if ref_url else {})}}
    req = urllib.request.Request(KIE + "/createTask", data=json.dumps(body).encode(), headers=auth)
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    task = (resp.get("data") or {}).get("taskId")
    if not task:
        raise RuntimeError(f"kie.ai createTask: {resp.get('msg') or resp}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(4)
        req = urllib.request.Request(KIE + "/recordInfo?taskId=" + urllib.parse.quote(task), headers=auth)
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read()).get("data") or {}
        state = data.get("state")
        if state == "success":
            url = json.loads(data["resultJson"])["resultUrls"][0]
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                return r.read()
        if state == "fail":
            raise RuntimeError(f"kie.ai task failed: {data.get('failMsg') or data.get('failCode')}")
    raise RuntimeError("kie.ai task timed out")


def fill(prompt, sci, com, pose):
    return (prompt.replace("{sci_name}", sci).replace("{com_name}", com)
            .replace("{pose}", pregen.POSES[pose]).replace("{anti_ref_line}", ""))


def species_list(a):
    if a.species:
        return [{"sci": s.split("|")[0].strip(), "com": s.split("|")[1].strip()} for s in a.species]
    rows = birdweather.top_species_for_station(a.station, days=a.days, limit=200)
    return rows[: a.limit]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--style", required=True, help="name of styles/<name>.prompt.md")
    ap.add_argument("--station", help="BirdWeather station ID to pull the bird list from")
    ap.add_argument("--species", action="append", help='"Scientific name|Common name" (repeatable)')
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=24, help="max species (by detections)")
    ap.add_argument("--out", type=Path, help="default: styles/<style>")
    ap.add_argument("--sleep", type=float, default=6.0, help="seconds between Gemini calls")
    a = ap.parse_args()
    kie_key = os.environ.get("KIE_API_KEY", "")
    key = os.environ.get("GEMINI_API_KEY", "")
    if not (kie_key or key):
        sys.exit("set KIE_API_KEY (kie.ai) or GEMINI_API_KEY (Google AI Studio, billing on)")
    print("provider:", "kie.ai" if kie_key else "Google Gemini")
    if not (a.station or a.species):
        sys.exit("give --station or --species")

    prompt = (HERE / "styles" / f"{a.style}.prompt.md").read_text()
    out = a.out or HERE / "styles" / a.style
    out.mkdir(parents=True, exist_ok=True)
    refs = HERE / ".refs"

    from PIL import Image
    made, failed = [], []
    for s in species_list(a):
        sci, com = s["sci"], s["com"]
        slug = birdweather.slugify(sci)
        ref = None if kie_key else pregen.ensure_reference(refs, slug, sci, com)
        ref_url = wiki_image_url(sci, com) if kie_key else None
        for pose, name in ((1, slug), (2, slug + "-2")):
            path = out / f"{name}.png"
            if path.exists():
                continue
            try:
                if kie_key:
                    raw = kie_generate(kie_key, fill(prompt, sci, com, pose), ref_url)
                else:
                    raw = pregen.gen_one(key, prompt, sci, com, pose, positive_ref=ref)
                path.write_bytes(raw)
                made.append(name)
                print(f"  ok   {name}")
            except Exception as e:  # keep going; a missing pose falls back to perched
                failed.append(name)
                print(f"  FAIL {name}: {e}", file=sys.stderr)
            time.sleep(a.sleep)

    if made:
        print(f"cutting out {len(made)} images ...")
        subprocess.run([sys.executable, str(AV / "avian/scripts/cutout.py"), "--dir", str(out), *made], check=True)
        for name in made:
            p = out / f"{name}.png"
            im = Image.open(p)
            if max(im.size) > MAX_SIDE:
                im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
                im.save(p, optimize=True)
    subprocess.run([sys.executable, str(AV / "avian/scripts/build_masks.py"),
                    "--illustrations", str(out), "--frontend", str(out)], check=True)
    print(f"done: {len(made)} new, {len(failed)} failed -> {out}")
    return 1 if failed and not made else 0


if __name__ == "__main__":
    sys.exit(main())
