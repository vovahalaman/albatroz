"""Baseline (P0): single-call solver, kept as the evaluation yardstick (SPEC §10).

Input: overview (LOW) + 8 horizontal views (MEDIUM) + label + timestamp → `BaselineResult`.
Code geocodes the top 3 `places[].query` via `geo.geocode`; each result becomes a Hypothesis with
provenance = that call_id, weight = confidence, radius = 300 m (urban) / 8 km (non-urban).
"""

from __future__ import annotations

import json

from navegador import llm
from navegador.config import regions, settings
from navegador.core import scribe
from navegador.core import state as st
from navegador.core.state import State
from navegador.schemas import BaselineResult, GeographerResult, Hypothesis
from navegador.tools import geo, magnifier

H_VIEWS = [f"h{a:03d}" for a in range(0, 360, 45)]


def _context(state: State) -> str:
    """Run facts for the model (data, not prompt text): label, timestamp and the image order."""
    ts = state.timestamp
    when = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}" if ts else "unknown"
    tz = "local time" if settings().flags.timestamp_is_local else "UTC"
    order = ", ".join(["overview"] + [f"{n} (yaw {int(n[1:])}°)" for n in H_VIEWS])
    return f"Label: {state.label}\nTimestamp: {when} ({tz})\nImages in order: {order}"


def _add_usage(state: State, usage: dict) -> None:
    for u in usage.get("attempts", [usage]):
        for k, v in u.items():
            if isinstance(v, int):
                state.token_usage[k] = state.token_usage.get(k, 0) + v


async def run(state: State) -> BaselineResult:
    """One LLM call; writes `baseline.json` and adds token usage to the state (caller saves)."""
    out = st.run_dir(state.pano_id)
    views = {v.name: v for v in magnifier.views(state.pano_path, out / "views")}  # no-op if S1 ran
    images = [(views["overview"].path, "overview"), *((views[n].path, "views") for n in H_VIEWS)]
    res = await llm.call("baseline", "baseline.md", pano_id=state.pano_id, text_parts=[_context(state)],
                         images=images, schema=BaselineResult)
    _add_usage(state, res.usage)
    result = BaselineResult.model_validate(res.parsed.model_dump())
    (out / "baseline.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result


def _geocode(query: str, country: str) -> list[dict]:
    """Try the model's country first, then all three (a wrong country guess should not lose the place)."""
    limit = settings().baseline.geocode_limit
    hits = geo.geocode(query, country_codes=country, limit=limit)
    return hits or geo.geocode(query, country_codes="pt,es,fr", limit=limit)


def to_hypotheses(state: State, result: BaselineResult) -> list[Hypothesis]:
    """Geocode the top places. Failed geocodes are logged by geo and skipped. R2-validated."""
    cfg = settings().baseline
    places = sorted(result.places, key=lambda p: -p.confidence)[: cfg.top_places]
    raw: list[dict] = []
    with geo.use_run(state.pano_id, actor="baseline"):
        for place in places:
            try:
                hits = _geocode(place.query, place.country)
            except geo.GeoError:
                continue
            for hit in hits:
                if not regions().contains(hit["lat"], hit["lng"]):
                    continue
                raw.append({"lat": hit["lat"], "lng": hit["lng"], "radius_m": cfg.radius_m.get(state.label),
                            "weight": place.confidence, "label": place.query, "provenance": hit["call_id"],
                            "evidence": [hit.get("display_name") or ""], "why": place.why, "source": "baseline"})
    registry = scribe.tool_registry(state.pano_id, state.run_id)
    hyps = [Hypothesis.model_validate(h, context={"registry": registry}) for h in raw]
    scribe.log_event(state.pano_id, "stage", "baseline",
                     {"stage": "geocode", "places": [p.query for p in places], "hypotheses": len(hyps)},
                     run_id=state.run_id)
    return hyps


def country_fallback(result: BaselineResult | GeographerResult | None) -> tuple[float, float] | None:
    """Centre of the top country's mainland box in regions.yaml, used when nothing geocoded."""
    if result is None:
        return None
    probs = result.country_probs.model_dump()
    top = max(probs, key=probs.get)
    box = regions().regions.get(f"{top}_mainland")
    if not box or probs[top] <= 0:
        return None
    return (box[0] + box[1]) / 2, (box[2] + box[3]) / 2


def load_result(state: State) -> BaselineResult | None:
    path = st.run_dir(state.pano_id) / "baseline.json"
    try:
        return BaselineResult.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
