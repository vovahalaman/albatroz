"""Run Astrolabe sun detection on every panorama in one or more folders, for threshold tuning.

    uv run python scripts/sun_debug.py data/train1 data/train2 [--out runs/sun_debug] [--labels eval/sun_labels.csv]

Saves one PNG per panorama (downscaled): the chosen sun circled in green (inner circle = saturated
core), rejected candidates in red with their reject reason (too_small blobs are only counted).
Prints a table and a summary against eval/sun_labels.csv (yes = clear sun, cloud = sun behind cloud,
both are targets; no = no sun; unclear = ignored). Every reported sun is checked with sun_check at
the region centre; a best elevation residual above 10° is flagged as a likely wrong spot.
`ceil` is the date's highest possible sun (+ tilt margin is allowed).
Thresholds live in config/settings.yaml → astrolabe.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw

from navegador.config import ROOT, regions, runs_dir, settings
from navegador.core.state import parse_timestamp
from navegador.tools.astrolabe import Blob, choose, find_blobs, load_small, max_sun_elev, pano_date, sun_check

GREEN, RED, YELLOW = (0, 255, 0), (255, 40, 40), (255, 220, 0)
RESIDUAL_FLAG_DEG = 10.0


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


def load_labels(path: Path) -> dict[str, str]:
    """pano key (last `_`-separated part of the file stem, e.g. "059") → yes | cloud | no | unclear."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return {row["pano"].strip(): row["sun"].strip() for row in csv.DictReader(fh)}


def pano_key(path: Path) -> str:
    return path.stem.rsplit("_", 1)[-1]


def summary(found: dict[str, str], labels: dict[str, str], keys: list[str], flagged: list[str]) -> str:
    """found: pano key → label of panoramas with a reported sun."""
    targets = {k: labels[k] for k in keys if labels.get(k) in ("yes", "cloud")}
    hit = [k for k in found if k in targets]
    false = sorted(k for k in found if labels.get(k) == "no")
    n_clear, n_cloud = (sum(v == kind for v in targets.values()) for kind in ("yes", "cloud"))
    h_clear, h_cloud = (sum(targets[k] == kind for k in hit) for kind in ("yes", "cloud"))
    unlabelled = sorted(k for k in keys if k not in labels)
    return (f"suns found {len(hit)}/{len(targets)} (clear {h_clear}/{n_clear}, cloud {h_cloud}/{n_cloud}), "
            f"false suns {len(false)}{' ' + str(false) if false else ''}, "
            f"residual > {RESIDUAL_FLAG_DEG:.0f}°: {len(flagged)}{' ' + str(sorted(flagged)) if flagged else ''}"
            + (f", unlabelled: {unlabelled}" if unlabelled else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", type=Path, nargs="+")
    ap.add_argument("--out", type=Path, default=None, help="default: runs/sun_debug/")
    ap.add_argument("--labels", type=Path, default=ROOT / "eval" / "sun_labels.csv")
    args = ap.parse_args()
    out = args.out or runs_dir() / "sun_debug"
    out.mkdir(parents=True, exist_ok=True)
    panos = sorted(p for d in args.folders for p in d.iterdir() if p.suffix.lower() in {".jpg", ".jpeg"})
    if not panos:
        raise SystemExit(f"no .jpg files in {[str(d) for d in args.folders]}")
    labels = load_labels(args.labels)
    centre = regions().fallback_point

    tilt = settings().astrolabe.tilt_margin_deg
    header = (f"{'file':<32} {'label':<7} {'result':<15} {'yaw':>7} {'elev':>6} {'unc':>5} {'conf':>5} "
              f"{'resid':>6} {'ceil':>5} {'blobs':>6}  rejects")
    print(header)
    print("-" * len(header))
    results: Counter[str] = Counter()
    found: dict[str, str] = {}
    flagged: list[str] = []
    for path in panos:
        rgb, _ = load_small(path)
        day = pano_date(path)
        blobs = find_blobs(rgb, day)
        best, why = choose(blobs)
        results[why] += 1
        key = pano_key(path)
        annotate(Image.fromarray(rgb), blobs, best).save(out / f"{path.stem}_sun.png")
        rejects = Counter(b.reject or "pass" for b in blobs)
        rej = ", ".join(f"{k}:{n}" for k, n in rejects.most_common(3))
        resid = f"{'-':>6}"
        if best is not None:
            found[key] = labels.get(key, "?")
            unc = best.core_radius_deg + tilt
            nums = f"{best.yaw:7.1f} {best.elev:6.1f} {unc:5.1f} {best.score:5.2f}"
            ts = parse_timestamp(path.name)
            if ts:
                r = sun_check(*centre, ts, sun_yaw=best.yaw, sun_elev=best.elev, uncertainty_deg=unc)["best"]
                res = r["elevation_residual_deg"]
                resid = f"{res:5.1f}" + ("!" if res > RESIDUAL_FLAG_DEG else " ")
                if res > RESIDUAL_FLAG_DEG:
                    flagged.append(key)
        else:
            nums = f"{'-':>7} {'-':>6} {'-':>5} {'-':>5}"
        ceil = f"{max_sun_elev(day):5.1f}" if day else f"{'-':>5}"
        name = path.name[:32]
        print(f"{name:<32} {labels.get(key, '?'):<7} {why:<15} {nums} {resid} {ceil} {len(blobs):6d}  {rej}")
    print("-" * len(header))
    print(", ".join(f"{k}: {n}" for k, n in results.most_common()), f"| PNGs in {out}")
    print(summary(found, labels, [pano_key(p) for p in panos], flagged))


if __name__ == "__main__":
    main()
