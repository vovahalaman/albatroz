"""Prepare eval/labels.csv for human review (SPEC §14): one row per training panorama.

    uv run python eval/prepare_labels.py [--panos data/panoramas.csv] [--labels eval/labels.csv]

Columns 1–6 are the SPEC labels (`filename,label,lat,lng,confidence,reviewer`). The reviewer fills
`lat,lng` with the TRUE location; they stay empty until someone has checked it. The other columns are
review help: the latest baseline prediction, Google Maps / Street View links, and the run report.
Safe to re-run: filled-in lat/lng/confidence/reviewer/notes are kept, prediction columns are refreshed.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from navegador.config import ROOT, runs_dir
from navegador.core.state import pano_id_for

SPEC_COLS = ["filename", "label", "lat", "lng", "confidence", "reviewer"]
HELP_COLS = ["pred_lat", "pred_lng", "pred_place", "pred_maps", "pred_streetview", "report", "notes"]
KEEP = ("lat", "lng", "confidence", "reviewer", "notes")


def _prediction(filename: str) -> dict[str, str]:
    """Latest run of this panorama: first candidate + the top hypothesis label."""
    run = runs_dir() / pano_id_for(Path(filename))
    try:
        state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sub = state.get("submission")
    if not sub:
        return {}
    lat, lng = sub["candidates"][0]["lat"], sub["candidates"][0]["lng"]
    top = max(state.get("hypotheses", []), key=lambda h: h["weight"], default=None)
    return {"pred_lat": f"{lat:.6f}", "pred_lng": f"{lng:.6f}",
            "pred_place": top["label"] if top else "(fallback, nothing geocoded)",
            "pred_maps": f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lng:.6f}",
            "pred_streetview": f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat:.6f},{lng:.6f}",
            "report": str((run / "report.md").relative_to(ROOT)) if run.is_relative_to(ROOT) else str(run / "report.md")}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--panos", type=Path, default=ROOT / "data" / "panoramas.csv")
    p.add_argument("--labels", type=Path, default=ROOT / "eval" / "labels.csv")
    args = p.parse_args(argv)

    with args.panos.open(encoding="utf-8-sig", newline="") as fh:
        panos = [(Path(r.get("filename") or r["file"]).name, r["label"].strip().lower().replace("-", "_"))
                 for r in csv.DictReader(fh)]
    existing: dict[str, dict[str, str]] = {}
    if args.labels.exists():
        with args.labels.open(encoding="utf-8-sig", newline="") as fh:
            existing = {r["filename"]: r for r in csv.DictReader(fh) if r.get("filename")}

    rows = []
    for filename, label in panos:
        old = existing.pop(filename, {})
        row = {c: "" for c in SPEC_COLS + HELP_COLS} | {"filename": filename, "label": label}
        row |= {k: old[k].strip() for k in KEEP if old.get(k)}
        if "," in row["lat"] and not row["lng"]:  # a Google Maps "lat, lng" pasted into the lat cell
            row["lat"], row["lng"] = (part.strip() for part in row["lat"].split(",", 1))
        row |= _prediction(filename)
        rows.append(row)
    rows += [{c: r.get(c, "") for c in SPEC_COLS + HELP_COLS} for r in existing.values()]  # never drop labels

    with args.labels.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SPEC_COLS + HELP_COLS)
        writer.writeheader()
        writer.writerows(rows)
    done = sum(1 for r in rows if r["lat"] and r["lng"])
    with_pred = sum(1 for r in rows if r["pred_lat"])
    print(f"{args.labels}: {len(rows)} rows · {done} labelled · {with_pred} with a prediction")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
