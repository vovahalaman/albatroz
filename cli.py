"""Navegador CLI: check-llm | geo-check | solve | batch | learn (SPEC §13). A thin shell over core/pipeline.py (R7)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def check_llm(args: argparse.Namespace) -> int:
    from navegador import llm

    try:
        report = asyncio.run(llm.smoke_test(args.model))
    except Exception as exc:  # noqa: BLE001 - report any failure to the operator
        print(f"check-llm FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("Gemini models visible to this project:")
    for name in report["models"]:
        print(f"  {name}")
    print(f"\nModel used: {report['model']}")
    for kind in ("text", "image"):
        r = report[kind]
        print(f"\n[{kind}] reply={r['reply']!r} latency={r['latency_s']}s")
        print(f"[{kind}] usage={json.dumps(r['usage'])}")
    return 0


def geo_check(args: argparse.Namespace) -> int:
    from navegador.tools import geo

    try:
        with geo.use_run("geo-check", actor="cli"):
            results = geo.geocode(args.query, limit=1)
    except geo.GeoError as exc:
        print(f"geo-check FAILED: {exc}", file=sys.stderr)
        return 1
    if not results:
        print(f"geo-check FAILED: no result for {args.query!r}", file=sys.stderr)
        return 1
    r = results[0]
    print(f"{r['display_name']}\nlat, lng = {r['lat']:.6f}, {r['lng']:.6f}\ncall_id  = {r['call_id']}")
    return 0


def _print_result(state) -> None:
    from navegador.core import pipeline

    r = pipeline.summary(state)
    print(f"\n{r['pano_id']}: status={r['status']} hypotheses={r['hypotheses']} "
          f"expected_score={r['expected_score']} timings={r['timings']}")
    for rank, (lat, lng) in enumerate(r["candidates"], 1):
        print(f"  {rank:2d}. {lat:.6f}, {lng:.6f}")
    print(f"run folder: {r['run_dir']}")


def solve(args: argparse.Namespace) -> int:
    """start_run → advance; MCQs are answered by number (buttons only, no free text: R3)."""
    from navegador.core import pipeline

    pano_id = pipeline.start_run(args.pano, args.label, args.special, args.mode)
    state = pipeline.advance(pano_id)
    while state.status == "awaiting_human" and state.pending_mcq:
        mcq = state.pending_mcq
        print(f"\n{mcq.question}")
        for i, o in enumerate(mcq.options, 1):
            print(f"  {i}. {o.text}")
        choice = ""
        while not (choice.isdigit() and 1 <= int(choice) <= len(mcq.options)):
            choice = input(f"answer 1-{len(mcq.options)}: ").strip()
        state = pipeline.answer_mcq(pano_id, mcq.options[int(choice) - 1].id)
    _print_result(state)
    return 0 if state.status == "done" else 1


def batch(args: argparse.Namespace) -> int:
    """Solve every panorama listed in the labels CSV (columns `file`, `label`) that exists in `folder`."""
    import csv

    from navegador.core import pipeline

    with args.labels.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if (args.folder / r["file"]).is_file()]
    if not rows:
        print(f"no panoramas from {args.labels} found in {args.folder}", file=sys.stderr)
        return 1
    failed = 0
    for row in rows:
        label = row["label"].strip().lower().replace("-", "_")
        try:
            state = pipeline.advance(pipeline.start_run(args.folder / row["file"], label, False, args.mode))
            _print_result(state)
            failed += state.status != "done"
        except Exception as exc:  # noqa: BLE001 - one bad panorama must not stop the batch
            failed += 1
            print(f"\n{row['file']}: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"\n{len(rows) - failed}/{len(rows)} done")
    return 0 if failed == 0 else 1


def learn(args: argparse.Namespace) -> int:
    """TODO(P2): Professor proposal for a reviewed run."""
    raise NotImplementedError("P2")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="navegador")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check-llm", help="smoke test: list models, one text + one image call")
    c.add_argument("--model", help="model ID to test (default: settings models.perception, else newest flash)")
    c.set_defaults(func=check_llm)
    g = sub.add_parser("geo-check", help="smoke test: geocode one place via Nominatim (cached, logged)")
    g.add_argument("--query", default="Torre de Belém, Lisboa")
    g.set_defaults(func=geo_check)
    s = sub.add_parser("solve")
    s.add_argument("pano", type=Path)
    s.add_argument("--label", choices=["urban", "non_urban"], required=True)
    s.add_argument("--mode", choices=["baseline", "autonomous", "assisted"], default="baseline")
    s.add_argument("--special", action="store_true")
    s.set_defaults(func=solve)
    b = sub.add_parser("batch")
    b.add_argument("folder", type=Path)
    b.add_argument("--labels", type=Path, required=True)
    b.add_argument("--mode", choices=["baseline", "autonomous"], default="baseline")
    b.set_defaults(func=batch)
    lp = sub.add_parser("learn")
    lp.add_argument("pano_id")
    lp.set_defaults(func=learn)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
