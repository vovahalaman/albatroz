"""Run Astrolabe sun detection on every panorama in a folder, for threshold tuning.

    uv run python scripts/sun_debug.py data/train1 [--out runs/sun_debug]

Saves one PNG per panorama (downscaled): the chosen sun circled in green (inner circle = saturated
core), rejected candidates in red with their reject reason (too_small blobs are only counted).
Prints a summary table; `ceil` is the date's highest possible sun (+ tilt margin is allowed).
Thresholds live in config/settings.yaml → astrolabe.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw

from navegador.config import runs_dir, settings
from navegador.tools.astrolabe import Blob, choose, find_blobs, load_small, max_sun_elev, pano_date

GREEN, RED, YELLOW = (0, 255, 0), (255, 40, 40), (255, 220, 0)


def _circle(draw: ImageDraw.ImageDraw, b: Blob, r: float, color: tuple[int, int, int], width: int) -> None:
    draw.ellipse((b.x - r, b.y - r, b.x + r, b.y + r), outline=color, width=width)


def annotate(rgb: Image.Image, blobs: list[Blob], best: Blob | None) -> Image.Image:
    img = rgb.copy()
    draw = ImageDraw.Draw(img)
    px_per_deg = img.height / 180.0
    draw.line((0, img.height // 2, img.width, img.height // 2), fill=YELLOW, width=1)  # search limit
    for b in blobs:
        if b is best or b.reject == "too_small":
            continue
        _circle(draw, b, max(b.radius_deg * px_per_deg, 3) + 3, RED, 1)
        draw.text((b.x + 6, b.y + 4), b.reject or f"pass {b.score:.2f}", fill=RED)
    if best is not None:
        _circle(draw, best, max(best.radius_deg * px_per_deg, 4) + 6, GREEN, 3)
        _circle(draw, best, max(best.core_radius_deg * px_per_deg, 2), GREEN, 1)
        draw.text((best.x + 10, best.y - 18), f"SUN {best.score:.2f}", fill=GREEN)
    return img


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="default: runs/sun_debug/")
    args = ap.parse_args()
    out = args.out or runs_dir() / "sun_debug"
    out.mkdir(parents=True, exist_ok=True)
    panos = sorted(p for p in args.folder.iterdir() if p.suffix.lower() in {".jpg", ".jpeg"})
    if not panos:
        raise SystemExit(f"no .jpg files in {args.folder}")

    tilt = settings().astrolabe.tilt_margin_deg
    header = f"{'file':<40} {'result':<15} {'yaw':>7} {'elev':>6} {'unc':>5} {'conf':>5} {'ceil':>5} {'blobs':>6}  rejects"
    print(header)
    print("-" * len(header))
    results: Counter[str] = Counter()
    for path in panos:
        rgb, _ = load_small(path)
        day = pano_date(path)
        blobs = find_blobs(rgb, day)
        best, why = choose(blobs)
        results[why] += 1
        annotate(Image.fromarray(rgb), blobs, best).save(out / f"{path.stem}_sun.png")
        rejects = Counter(b.reject or "pass" for b in blobs)
        rej = ", ".join(f"{k}:{n}" for k, n in rejects.most_common())
        if best is not None:
            nums = f"{best.yaw:7.1f} {best.elev:6.1f} {best.core_radius_deg + tilt:5.1f} {best.score:5.2f}"
        else:
            nums = f"{'-':>7} {'-':>6} {'-':>5} {'-':>5}"
        ceil = f"{max_sun_elev(day):5.1f}" if day else f"{'-':>5}"
        print(f"{path.name[:40]:<40} {why:<15} {nums} {ceil} {len(blobs):6d}  {rej}")
    print("-" * len(header))
    print(", ".join(f"{k}: {n}" for k, n in results.most_common()), f"| PNGs in {out}")


if __name__ == "__main__":
    main()
