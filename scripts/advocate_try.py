"""Try the Advocate on an existing run (e.g. a baseline run in runs/) without changing its state.

    uv run python scripts/advocate_try.py <pano_id> [--sun]

Loads runs/<pano_id>/state.json. With --sun and no sun in the state, runs astrolabe.detect (in memory).
Runs the Advocate with force=True (real LLM call + Nominatim reverse lookups) and applies its multipliers
to an in-memory copy. Prints each hypothesis group's weight before → after, reverse place and sun
consistency, then the objections. state.json is never saved; the Advocate still appends its events to
the run's log.jsonl and writes runs/<pano_id>/advocate.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from navegador.agents import advocate
from navegador.config import ROOT
from navegador.core import scribe
from navegador.core import state as st
from navegador.tools import astrolabe


def _last(state: st.State, event: str, where: Any) -> dict[str, Any]:
    """Payload of this run's last Advocate event of a kind matching `where`."""
    return [e["payload"] for e in scribe.read_events(state.pano_id, state.run_id)
            if e["event"] == event and e["actor"] == "advocate" and where(e["payload"])][-1]


def _weight(state: st.State, label: str) -> float:
    return sum(h.weight for h in state.hypotheses if h.label == label)


def _place(rev: dict[str, Any] | None) -> str:
    if rev is None:
        return "(lookup failed)"
    town = rev.get("municipality") or rev.get("city") or rev.get("town") or rev.get("display_name")
    return f"{town or '(nothing there)'}" + (f" ({rev['country_code']})" if rev.get("country_code") else "")


def _sun(check: dict[str, Any] | None) -> str:
    if check is None:
        return "—"
    return f"{check['consistency']} (residual {check['best_residual_deg']}°{', ambiguous' if check['ambiguous'] else ''})"


def _cut(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pano_id")
    ap.add_argument("--sun", action="store_true", help="detect the sun first if the state has none")
    args = ap.parse_args()
    state = st.load(args.pano_id)
    if not state.pano_path.exists() and (ROOT / state.pano_path).exists():
        state.pano_path = ROOT / state.pano_path  # runs store the path as typed on the command line
    if args.sun and state.sun is None:
        state.sun = astrolabe.detect(state.pano_path)
    sun = state.sun.model_dump(include={"yaw", "elevation", "confidence", "uncertainty_deg", "source"}) if state.sun else None
    print(f"{state.pano_id} · {state.label} · timestamp {state.timestamp} · {len(state.hypotheses)} hypotheses")
    print(f"sun: {sun or 'none'}\n")

    result = asyncio.run(advocate.run(state, force=True))
    after = state.model_copy(deep=True)
    advocate.apply(after, result)

    call = _last(state, "llm_call", lambda p: True)
    part = next(p for p in call["text_parts"] if p.startswith("Hypotheses:\n"))
    groups = json.loads(part.removeprefix("Hypotheses:\n"))  # exactly what the model saw
    print(f"{'id':<3} {'label':<44} {'weight before → after':<22} {'reverse':<22} sun consistency")
    for g in groups:
        before, now = _weight(state, g["label"]), _weight(after, g["label"])
        print(f"{g['id']:<3} {_cut(g['label'], 44):<44} {f'{before:.3f} → {now:.3f}':<22} "
              f"{_cut(_place(g['reverse']), 22):<22} {_sun(g['sun_check'])}")

    ids = {g["label"]: g["id"] for g in groups}
    print(f"\nObjections ({len(result.objections)}):")
    for o in result.objections:
        print(f"  [{o.severity}] {ids.get(o.hypothesis_label, '?')} {o.hypothesis_label}: {o.issue}")
    print(f"Weight multipliers: {result.weight_multipliers or 'none'}")
    dropped = _last(state, "stage", lambda p: p.get("stage") == "advocate")["dropped"]
    print(f"Dropped by code: {dropped or 'none'}")
    usage = call.get("usage", {})
    print(f"LLM: {call['model']} · {call['latency_s']} s · {usage.get('total_token_count', usage)} tokens")


if __name__ == "__main__":
    main()
