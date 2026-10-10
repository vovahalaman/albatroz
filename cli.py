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


def _ask_mcq(mcq) -> str:
    """Numbered input() prompt. No free text (R3)."""
    print(f"\nMCQ (costs ×0.95): {mcq.question}")
    for i, opt in enumerate(mcq.options, 1):
        print(f"  {i}. {opt.text}")
    while True:
        choice = input(f"Choose 1–{len(mcq.options)}: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(mcq.options):
            return mcq.options[int(choice) - 1].id


def _run_one(pano: Path, label: str, mode: str, special: bool = False, interactive: bool = True):
    from navegador.core import pipeline

    pano_id = pipeline.start_run(pano, label, special, mode)
    state = pipeline.advance(pano_id)
    while state.status == "awaiting_human" and interactive:
        state = pipeline.answer_mcq(pano_id, _ask_mcq(state.pending_mcq))
    return state


def _errors(state) -> list[str]:
    from navegador.core import scribe

    return [f"{e['payload'].get('stage') or e['payload'].get('tool') or e['payload'].get('where')}: "
            f"{e['payload'].get('error')}" for e in scribe.read_events(state.pano_id, state.run_id)
            if e["event"] == "error"]


def _print_run(state) -> None:
    from navegador.agents import baseline
    from navegador.config import runs_dir
    from navegador.core import export

    result = baseline.load_result(state)
    print(f"\n{state.pano_id} · {state.label} · mode {state.mode} · timestamp {state.timestamp} · status {state.status}")
    if result:
        cp = result.country_probs
        print(f"countries: pt {cp.pt:.2f} · es {cp.es:.2f} · fr {cp.fr:.2f}")
        for p in sorted(result.places, key=lambda p: -p.confidence):
            hits = [h for h in state.hypotheses if h.label == p.query]
            where = ", ".join(f"{h.lat:.5f}, {h.lng:.5f} [{h.provenance}]" for h in hits) or "not geocoded / not used"
            print(f"  {p.confidence:.2f}  {p.query!r} ({p.country}) → {where}\n        why: {p.why}")
        for c in result.clues[:8]:
            print(f"  · {c}")
    for err in _errors(state):
        print(f"  ERROR {err}")
    sub = state.submission
    if sub:
        print(f"expected score {sub.expected_score:.0f} · penalty ×{sub.penalty_factor:.4f} · "
              f"hypotheses {len(state.hypotheses)} · tokens {state.token_usage.get('total_token_count', '?')}")
        print("\n" + export.to_text(sub))
    print(f"files: {runs_dir() / state.pano_id}  (submission.*, log.json, report.md)")


def solve(args: argparse.Namespace) -> int:
    """start_run → advance; MCQs are answered via a numbered input() prompt (no free text)."""
    state = _run_one(args.pano, args.label, args.mode, args.special)
    _print_run(state)
    return 0 if state.status == "done" else 1


def _read_csv(path: Path) -> list[dict[str, str]]:
    import csv

    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def batch(args: argparse.Namespace) -> int:
    """Solve every labelled panorama under a folder; write a summary CSV to runs/_batch/."""
    import csv
    import statistics
    import time
    from datetime import datetime

    from navegador.config import runs_dir
    from navegador.core import scribe
    from navegador.tools import strategist

    labels = {Path(r.get("filename") or r["file"]).name: r["label"] for r in _read_csv(args.labels)}
    truth = {r["filename"]: (float(r["lat"]), float(r["lng"])) for r in _read_csv(args.truth)
             if r.get("lat") and r.get("lng")} if args.truth.exists() else {}
    panos = [p for p in sorted(args.folder.rglob("*.jpg")) if p.name in labels][: args.limit]
    if not panos:
        print(f"no labelled .jpg under {args.folder} (labels from {args.labels})", file=sys.stderr)
        return 1
    meta = scribe.metadata()
    rows: list[dict] = []
    for i, pano in enumerate(panos, 1):
        t0 = time.perf_counter()
        try:
            state = _run_one(pano, labels[pano.name], args.mode, interactive=False)
        except Exception as exc:  # noqa: BLE001 - one bad pano must not stop the batch
            print(f"[{i}/{len(panos)}] {pano.name} FAILED: {type(exc).__name__}: {exc}")
            rows.append({"filename": pano.name, "label": labels[pano.name], "status": "crash", "error": str(exc)})
            continue
        sub, top = state.submission, max(state.hypotheses, key=lambda h: h.weight, default=None)
        row = {"filename": pano.name, "label": state.label, "status": state.status,
               "lat": sub.candidates[0].lat if sub else "", "lng": sub.candidates[0].lng if sub else "",
               "expected_score": round(sub.expected_score) if sub else "", "hypotheses": len(state.hypotheses),
               "top_place": top.label if top else "", "top_weight": top.weight if top else "",
               "errors": len(_errors(state)), "seconds": round(time.perf_counter() - t0, 1),
               "tokens": state.token_usage.get("total_token_count", ""), "git_commit": meta["git_commit"],
               "prompt_hash": meta["prompt_hashes"].get("baseline.md", ""), "pano_id": state.pano_id}
        if sub and pano.name in truth:
            cands = [(c.lat, c.lng) for c in sub.candidates]
            row["distance_m"] = round(strategist.haversine_m(cands[0], truth[pano.name]))
            row["score"] = round(strategist.score(cands, truth[pano.name], state.label))
        rows.append(row)
        print(f"[{i}/{len(panos)}] {pano.name} {row['label']:9} {row['lat']}, {row['lng']}  E={row['expected_score']}"
              f"  {row['seconds']}s  top={row['top_place']!r}" + (f"  d={row['distance_m']} m" if "distance_m" in row else ""))
    out = runs_dir() / "_batch" / f"batch_{datetime.now():%Y%m%d_%H%M%S}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    done = [r for r in rows if r["status"] == "done"]
    scored = [r for r in done if "score" in r]
    print(f"\n{len(done)}/{len(rows)} done · prompt baseline.md {meta['prompt_hashes'].get('baseline.md')} · {out}")
    if scored:
        print(f"pseudo-score mean {statistics.mean(r['score'] for r in scored):.0f} · median distance "
              f"{statistics.median(r['distance_m'] for r in scored):.0f} m ({len(scored)} with labels)")
    return 0 if len(done) == len(rows) else 1


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
    s = sub.add_parser("solve", help="solve one panorama and print the 10 coordinates")
    s.add_argument("pano", type=Path)
    s.add_argument("--label", choices=["urban", "non_urban"], required=True)
    s.add_argument("--mode", choices=["baseline", "autonomous", "assisted"], default="baseline")
    s.add_argument("--special", action="store_true")
    s.set_defaults(func=solve)
    b = sub.add_parser("batch", help="solve every labelled panorama under a folder; summary CSV in runs/_batch/")
    b.add_argument("folder", type=Path)
    b.add_argument("--labels", type=Path, required=True, help="CSV with columns file|filename,label")
    b.add_argument("--truth", type=Path, default=Path("eval/labels.csv"),
                   help="CSV filename,lat,lng for pseudo-scores (rows without lat/lng are ignored)")
    b.add_argument("--mode", choices=["baseline", "autonomous"], default="baseline")
    b.add_argument("--limit", type=int, default=None, help="only the first N panoramas")
    b.set_defaults(func=batch)
    lp = sub.add_parser("learn")
    lp.add_argument("pano_id")
    lp.set_defaults(func=learn)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
