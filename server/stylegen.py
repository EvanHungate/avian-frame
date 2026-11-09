#!/usr/bin/env python3
"""Generate a new illustration style for the birds your station hears.

Uses the upstream AvianVisitors pipeline pieces:
  Wikipedia photo (anatomy reference) -> Gemini image model with a style prompt
  -> rembg/BiRefNet cutout -> build_masks.py (dims.json + masks.json)

    GEMINI_API_KEY=... python3 stylegen.py --style ghibli --station 10393

Writes styles/<style>/<slug>.png (perched), <slug>-2.png (flight), dims.json,
masks.json - the layout render_server.py's STYLE=<style> expects. Only birds
not already generated are made, so re-running after new birds turn up is cheap.
Needs a PAID Gemini key (image output isn't on the free tier); ~2 images/bird.
"""
from __future__ import annotations

import argparse
import os
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
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        sys.exit("set GEMINI_API_KEY (a paid Google AI Studio key)")
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
        ref = pregen.ensure_reference(refs, slug, sci, com)
        for pose, name in ((1, slug), (2, slug + "-2")):
            path = out / f"{name}.png"
            if path.exists():
                continue
            try:
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
