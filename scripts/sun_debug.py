"""Run Astrolabe sun detection on every panorama in one or more folders, for threshold tuning.

    uv run python scripts/sun_debug.py data/train1 data/train2 [--out runs/sun_debug] [--labels eval/sun_labels.csv]
                                       [--no-vlm]

Saves one PNG per panorama (downscaled): the chosen sun circled in green (inner circle = saturated
core), rejected candidates in red with their reject reason (too_small blobs are only counted).
Prints a table and a summary against eval/sun_labels.csv (yes = clear sun, cloud = sun behind cloud,
both are targets; no = no sun; unclear = ignored). Every reported sun is checked with sun_check at
the region centre; a best elevation residual above 10° is flagged as a likely wrong spot.
`faces` is the compass direction of the image centre from sun_summary (region centre, ± uncertainty).
`ceil` is the date's highest possible sun (+ tilt margin is allowed).
With the VLM on (astrolabe.vlm_fallback, default), detected suns are verified by Gemini and panoramas
without a detection get the Gemini fallback (magenta circle). VLM responses are cached on disk; the run
prints VLM calls, cache hits and tokens at the end.
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
from navegador.schemas import SunObs
from navegador.tools.astrolabe import Blob, analyse, max_sun_elev, pano_date, sun_check
from navegador.tools.astrolabe_summary import summarise
from navegador.tools.astrolabe_vlm import vlm_stats

GREEN, RED, YELLOW, MAGENTA, ORANGE = (0, 255, 0), (255, 40, 40), (255, 220, 0), (255, 0, 255), (255, 140, 0)
RESIDUAL_FLAG_DEG = 10.0


def _circle(draw: ImageDraw.ImageDraw, b: Blob, r: float, color: tuple[int, int, int], width: int) -> None:
    draw.ellipse((b.x - r, b.y - r, b.x + r, b.y + r), outline=color, width=width)


def annotate(rgb: Image.Image, blobs: list[Blob], best: Blob | None, obs: SunObs | None, why: str,
             orig: tuple[int, int]) -> Image.Image:
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
        color = ORANGE if why == "vlm_veto" else GREEN
        _circle(draw, best, max(best.radius_deg * px_per_deg, 4) + 6, color, 3)
        _circle(draw, best, max(best.core_radius_deg * px_per_deg, 2), color, 1)
        draw.text((best.x + 10, best.y - 18), f"{'VETO' if why == 'vlm_veto' else 'SUN'} {best.score:.2f}", fill=color)
    if obs is not None and obs.source == "vlm" and obs.pixel_xy:
        x, y = obs.pixel_xy[0] * img.width / orig[0], obs.pixel_xy[1] * img.height / orig[1]
        r = obs.uncertainty_deg * px_per_deg
        draw.ellipse((x - r, y - r, x + r, y + r), outline=MAGENTA, width=3)
        draw.text((x + r + 4, y - 8), "VLM", fill=MAGENTA)
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
    ap.add_argument("--no-vlm", action="store_true", help="blob detection only, no Gemini calls")
    args = ap.parse_args()
    out = args.out or runs_dir() / "sun_debug"
    out.mkdir(parents=True, exist_ok=True)
    panos = sorted(p for d in args.folders for p in d.iterdir() if p.suffix.lower() in {".jpg", ".jpeg"})
    if not panos:
        raise SystemExit(f"no .jpg files in {[str(d) for d in args.folders]}")
    labels = load_labels(args.labels)
    centre = regions().fallback_point

    use_vlm = settings().astrolabe.vlm_fallback and not args.no_vlm
    header = (f"{'file':<32} {'label':<7} {'result':<20} {'src':<6} {'yaw':>7} {'elev':>6} {'unc':>5} {'conf':>5} "
              f"{'resid':>6} {'faces':>11} {'ceil':>5} {'blobs':>6}  rejects")
    print(header)
    print("-" * len(header))
    results: Counter[str] = Counter()
    found: dict[str, str] = {}
    flagged: list[str] = []
    for path in panos:
        a = analyse(path, vlm=use_vlm)
        rgb, orig, blobs, obs, why, day = a.rgb, a.orig, a.blobs, a.obs, a.why, pano_date(path)
        results[why] += 1
        key = pano_key(path)
        annotate(Image.fromarray(rgb), blobs, a.best, obs, why, orig).save(out / f"{path.stem}_sun.png")
        rejects = Counter(b.reject or "pass" for b in blobs)
        rej = ", ".join(f"{k}:{n}" for k, n in rejects.most_common(3))
        resid, faces, src = f"{'-':>6}", f"{'-':>11}", "-"
        if obs is not None:
            found[key] = labels.get(key, "?")
            unc, src = obs.uncertainty_deg, obs.source
            nums = f"{obs.yaw:7.1f} {obs.elevation:6.1f} {unc:5.1f} {obs.confidence:5.2f}"
            ts = parse_timestamp(path.name)
            if ts:
                c = summarise(obs, ts)
                if c["compass"]:
                    faces = f"{c['compass']['centre_faces']:>2} {c['compass']['centre_bearing_deg']:3.0f}±{c['compass_uncertainty_deg']:<3.0f}"
                r = sun_check(*centre, ts, sun_yaw=obs.yaw, sun_elev=obs.elevation, uncertainty_deg=unc)["best"]
                res = r["elevation_residual_deg"]
                resid = f"{res:5.1f}" + ("!" if res > RESIDUAL_FLAG_DEG else " ")
                if res > RESIDUAL_FLAG_DEG:
                    flagged.append(key)
        else:
            nums = f"{'-':>7} {'-':>6} {'-':>5} {'-':>5}"
        ceil = f"{max_sun_elev(day):5.1f}" if day else f"{'-':>5}"
        name = path.name[:32]
        print(f"{name:<32} {labels.get(key, '?'):<7} {why:<20} {src:<6} {nums} {resid} {faces:>11} {ceil} {len(blobs):6d}  {rej}")
    print("-" * len(header))
    print(", ".join(f"{k}: {n}" for k, n in results.most_common()), f"| PNGs in {out}")
    print(summary(found, labels, [pano_key(p) for p in panos], flagged))
    v = vlm_stats()
    print(f"VLM {'on' if use_vlm else 'off'}: calls {v['calls']}, cache hits {v['cache_hits']}, "
          f"total tokens {v['tokens']}, capped {v['capped']}, refused {v['refused']}, errors {v['errors']}")


if __name__ == "__main__":
    main()
