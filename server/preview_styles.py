#!/usr/bin/env python3
"""Render the current frame in several illustration styles side by side.

    python3 preview_styles.py --out ../previews woodblock impressionist audubon ghibli linocut

Uses the same station / title settings as the server (env vars). For each style
writes <style>.png (full colour) and <style>-eink.png (6-ink panel simulation),
plus sheet.jpg (all styles in one image) and README.md. Styles that can't be
loaded (e.g. a generated set that doesn't exist yet) are skipped with a note.
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

os.environ.setdefault("DATA_DIR", "/tmp/avian-preview")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_server as rs  # noqa: E402

LABELS = {
    "woodblock": "Japanese Woodblock (current)",
    "impressionist": "Evolutionary Impressionist",
    "audubon": "Audubon - Birds of America",
    "ghibli": "Ghibli-style (generated)",
    "linocut": "Linocut print (generated)",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("styles", nargs="+")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    os.makedirs(rs.CFG["data"], exist_ok=True)

    done, notes = [], []
    for name in a.styles:
        try:
            style = rs.make_style(name)
            rs.STYLE = style
            species = rs.fetch_species(style)
            img = rs.render(species, style, out=str(a.out / f".{name}.tmp.png"))
            fb, eink = rs.dither(img)
            img.save(a.out / f"{name}.png")
            (a.out / f"{name}-eink.png").write_bytes(eink)
            done.append((name, [s["com"] for s in species]))
            print(f"{name}: {len(species)} birds")
        except Exception as e:
            traceback.print_exc()
            notes.append(f"- **{name}**: not rendered ({type(e).__name__}: {e})")
        finally:
            tmp = a.out / f".{name}.tmp.png"
            if tmp.exists():
                tmp.unlink()

    if not done:
        sys.exit("no style rendered")

    # contact sheet: 2 columns of the full-colour renders at half size, labelled
    cw, ch, pad, lab = 400, 240, 12, 22
    cols = 2
    rows = (len(done) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (cw + pad) + pad, rows * (ch + lab + pad) + pad), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 15)
    except OSError:
        font = ImageFont.load_default()
    for i, (name, _) in enumerate(done):
        x = pad + (i % cols) * (cw + pad)
        y = pad + (i // cols) * (ch + lab + pad)
        d.text((x, y), f"{i + 1}. {LABELS.get(name, name)}", fill=(20, 20, 20), font=font)
        sheet.paste(Image.open(a.out / f"{name}.png").resize((cw, ch), Image.LANCZOS), (x, y + lab))
    sheet.save(a.out / "sheet.jpg", quality=82)

    lines = ["# Style previews", "",
             "Live birds from the configured station, rendered in each style. "
             "Left: full colour. Right: what the 6-colour e-paper panel will show.", ""]
    for name, birds in done:
        lines += [f"## {LABELS.get(name, name)}  (`STYLE={name}`)", "",
                  f"{len(birds)} birds: {', '.join(birds)}", "",
                  f'<img src="{name}.png" width="49%"> <img src="{name}-eink.png" width="49%">', ""]
    if notes:
        lines += ["## Not rendered", "", *notes, ""]
    (a.out / "README.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
