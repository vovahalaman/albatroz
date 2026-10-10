"""Advocate (P2): tries to refute the top hypotheses after the Detective (SPEC §4 S5, §10).

Hypotheses are grouped by label; the top `advocate.top_n` groups go to the model as H1…Hn, with facts
computed by code (reverse geocode, sun_check). The model never adds locations: it returns objections and
weight multipliers. `resolve` maps the ids back to labels and enforces the limits (a multiplier needs an
objection and is clamped to [advocate.min_multiplier, 1]); `apply` multiplies the weights.
Skipped unless flags.advocate_enabled (or force=True). Writes runs/<id>/advocate.json.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from typing import Any

from navegador import llm
from navegador.config import settings
from navegador.core import scribe
from navegador.core import state as st
from navegador.core.state import State
from navegador.schemas import AdvocateResult, Hypothesis
from navegador.tools import astrolabe, geo, magnifier

H_VIEWS = [f"h{a:03d}" for a in range(0, 360, 45)]
REVERSE_KEYS = ("road", "neighbourhood", "suburb", "city_district", "village", "town", "city", "municipality",
                "county", "state", "region", "country_code")  # kept from the address
MAX_EVIDENCE = 6


def _context(state: State) -> str:
    """Run facts for the model (data, not prompt text): label, timestamp and the image order."""
    ts = state.timestamp
    when = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}" if ts else "unknown"
    tz = "local time" if settings().flags.timestamp_is_local else "UTC"
    order = ", ".join(["overview"] + [f"{n} (yaw {int(n[1:])}°)" for n in H_VIEWS])
    return f"Label: {state.label}\nTimestamp: {when} ({tz})\nImages in order: {order}"


def _add_usage(state: State, usage: dict) -> None:
    for u in usage.get("attempts", [usage]):
        for k, v in u.items():
            if isinstance(v, int):
                state.token_usage[k] = state.token_usage.get(k, 0) + v


def top_groups(hypotheses: list[Hypothesis], top_n: int) -> list[dict[str, Any]]:
    """Label groups by summed weight, best first, as H1…H<top_n>. Point, radius and why come from the
    heaviest member; weight_share is the group's share of the weight of all hypotheses."""
    by_label: dict[str, list[Hypothesis]] = {}
    for h in sorted(hypotheses, key=lambda h: -h.weight):  # members heaviest first
        by_label.setdefault(h.label, []).append(h)
    total = sum(h.weight for h in hypotheses)
    ranked = sorted(by_label.items(), key=lambda kv: -sum(h.weight for h in kv[1]))[:top_n]
    groups = []
    for i, (label, members) in enumerate(ranked, 1):
        top, weight = members[0], sum(h.weight for h in members)
        groups.append({"id": f"H{i}", "label": label, "lat": top.lat, "lng": top.lng, "radius_m": top.radius_m,
                       "weight_share": round(weight / total, 3) if total else 0.0,
                       "sources": sorted({h.source for h in members}), "why": top.why,
                       "evidence": list(dict.fromkeys(e for h in members for e in h.evidence if e))[:MAX_EVIDENCE]})
    return groups


def _fact(state: State, where: str, label: str, fn: Callable[..., Any], *args: Any) -> Any:
    """fn(*args), or None if it fails (error logged): a missing fact must not stop the Advocate."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        scribe.log_event(state.pano_id, "error", "advocate",
                         {"where": where, "hypothesis": label, "error": f"{type(exc).__name__}: {exc}"},
                         run_id=state.run_id)
        return None


def _reverse(lat: float, lng: float) -> dict[str, Any]:
    r = geo.reverse(lat, lng, zoom=17)  # Nominatim zoom 17 = street level (road, neighbourhood, suburb)
    place = {"display_name": r.get("display_name"), **{k: r.get("address", {}).get(k) for k in REVERSE_KEYS}}
    return {k: v for k, v in place.items() if v}  # {} = nothing there (e.g. open sea)


def _sun(state: State, lat: float, lng: float) -> dict[str, Any] | None:
    sun = state.sun
    if sun is None or not state.timestamp:
        return None
    r = astrolabe.sun_check(lat, lng, state.timestamp, sun_yaw=sun.yaw, sun_elev=sun.elevation,
                            uncertainty_deg=sun.uncertainty_deg or None)
    return {"consistency": r["consistency"], "best_residual_deg": (r["best"] or {}).get("elevation_residual_deg"),
            "ambiguous": r["ambiguous"]}


def _add_facts(state: State, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with geo.use_run(state.pano_id, actor="advocate"):
        for g in groups:
            g["reverse"] = _fact(state, "reverse", g["label"], _reverse, g["lat"], g["lng"])
            g["sun_check"] = _fact(state, "sun_check", g["label"], _sun, state, g["lat"], g["lng"])
    return groups


def _read_json(state: State, name: str) -> str | None:
    """runs/<id>/<name> as compact JSON, or None if missing or unreadable."""
    try:
        return json.dumps(json.loads((st.run_dir(state.pano_id) / name).read_text(encoding="utf-8")),
                          ensure_ascii=False)
    except (OSError, ValueError):
        return None


def _text_parts(state: State, groups: list[dict[str, Any]]) -> list[str]:
    """The input contract with prompts/advocate.md: keep the headings verbatim."""
    reader, geographer = _read_json(state, "reader.json"), _read_json(state, "geographer.json")
    parts = [_context(state), "Hypotheses:\n" + json.dumps(groups, ensure_ascii=False, indent=2),
             "Reader result: none" if reader is None else f"Reader result:\n{reader}",
             "Geographer result: none" if geographer is None else f"Geographer result:\n{geographer}"]
    if reader is None and geographer is None and (baseline := _read_json(state, "baseline.json")) is not None:
        parts.append(f"Baseline result:\n{baseline}")
    human = [f"Cue: {c.text}" for c in state.cues]
    human += [f"MCQ: {m.question} → {next((o.text for o in m.options if o.id == m.answer), m.answer)}"
              for m in state.mcqs if m.answer is not None]
    human += [f"Excluded zone: {z.label or f'{z.lat:.3f}, {z.lng:.3f}'}" for z in state.excluded_zones]
    return [*parts, "Human input:\n" + "\n".join(human) if human else "Human input: none"]


def _limit(m: float, floor: float) -> float | None:
    """Clamp to [floor, 1]; None if not finite or 1.0 after clamping (a multiplier never raises a weight)."""
    if not math.isfinite(m):
        return None
    m = min(max(m, floor), 1.0)
    return m if m < 1.0 else None


def resolve(result: AdvocateResult, id_to_label: dict[str, str], min_mult: float,
            dropped: list[dict[str, str]] | None = None) -> AdvocateResult:
    """Map ids (H1…) or exact labels to labels; anything else is dropped. A multiplier is kept only for a
    label with an objection, clamped to [min_mult, 1]; the lowest wins if a label is named twice.
    No I/O: each drop is appended to `dropped` ({"objection"|"multiplier": ref, "reason"}) for the caller."""
    dropped = [] if dropped is None else dropped
    labels = set(id_to_label.values())

    def label_of(ref: str) -> str | None:
        return id_to_label.get(ref, ref if ref in labels else None)

    objections = []
    for o in result.objections:
        if (label := label_of(o.hypothesis_label)) is None:
            dropped.append({"objection": o.hypothesis_label, "reason": "unknown hypothesis"})
        else:
            objections.append(o.model_copy(update={"hypothesis_label": label}))
    objected = {o.hypothesis_label for o in objections}
    multipliers: dict[str, float] = {}
    for ref, m in result.weight_multipliers.items():
        label, kept = label_of(ref), _limit(m, min_mult)
        if label is None or label not in objected or kept is None:
            reason = ("unknown hypothesis" if label is None else "no objection" if label not in objected
                      else "not finite or >= 1")
            dropped.append({"multiplier": ref, "reason": reason})
        else:
            multipliers[label] = min(kept, multipliers.get(label, 1.0))
    return AdvocateResult(objections=objections, weight_multipliers=multipliers)


async def run(state: State, *, force: bool = False) -> AdvocateResult:
    """One LLM call on the top label groups; writes `advocate.json` (real labels) and adds token usage
    (caller saves). Weights are unchanged until `apply`. LLM errors propagate to the caller."""
    skipped = ("flags.advocate_enabled is false" if not (settings().flags.advocate_enabled or force)
               else "no hypotheses" if not state.hypotheses else None)
    if skipped:
        scribe.log_event(state.pano_id, "stage", "advocate", {"stage": "advocate", "skipped": skipped},
                         run_id=state.run_id)
        return AdvocateResult()
    cfg, out = settings().advocate, st.run_dir(state.pano_id)
    views = {v.name: v for v in magnifier.views(state.pano_path, out / "views")}  # no-op if S1 ran
    images = [(views["overview"].path, "overview"), *((views[n].path, "views") for n in H_VIEWS)]
    groups = await asyncio.to_thread(_add_facts, state, top_groups(state.hypotheses, cfg.top_n))
    res = await llm.call("advocate", "advocate.md", pano_id=state.pano_id, text_parts=_text_parts(state, groups),
                         images=images, schema=AdvocateResult)
    _add_usage(state, res.usage)
    parsed: AdvocateResult = res.parsed
    dropped: list[dict[str, str]] = []
    result = resolve(parsed, {g["id"]: g["label"] for g in groups}, cfg.min_multiplier, dropped)
    if unknown := [d for d in dropped if d["reason"] == "unknown hypothesis"]:
        scribe.log_event(state.pano_id, "error", "advocate", {"where": "resolve", "dropped": unknown},
                         run_id=state.run_id)
    scribe.log_event(state.pano_id, "stage", "advocate",
                     {"stage": "advocate", "groups": {g["id"]: g["label"] for g in groups},
                      "objections": len(result.objections), "weight_multipliers": result.weight_multipliers,
                      "dropped": dropped}, run_id=state.run_id)
    (out / "advocate.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result


def apply(state: State, result: AdvocateResult) -> None:
    """Multiply every hypothesis of a label by its multiplier (limits re-checked). Logs one `stage` event
    with each group's summed weight before and after. Caller saves."""
    floor, changes = settings().advocate.min_multiplier, []
    for label, m in result.weight_multipliers.items():
        members, mult = [h for h in state.hypotheses if h.label == label], _limit(m, floor)
        if mult is None or not members:
            continue
        before = sum(h.weight for h in members)
        for h in members:
            h.weight *= mult
        changes.append({"label": label, "multiplier": mult, "weight_before": round(before, 4),
                        "weight_after": round(sum(h.weight for h in members), 4)})
    scribe.log_event(state.pano_id, "stage", "advocate", {"stage": "advocate_apply", "changes": changes},
                     run_id=state.run_id)
