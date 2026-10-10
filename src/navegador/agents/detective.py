"""Detective: turns clues into coordinates via a function-calling loop (SPEC §10).

Input: runs/<id>/reader.json and geographer.json (whichever exist), the sun summary, label, timestamp,
cues, MCQ answers and excluded zones. Output: hypotheses + final summary in runs/<id>/detective.json.
R2: hypotheses enter only through add_hypothesis; provenance must be in scribe.tool_registry(pano_id) and
the point must lie near a coordinate in that tool result. provenance="estimated" is allowed with
weight × settings().strategist.estimated_weight_factor.
Annotations are real types (no `from __future__ import annotations`): the SDK reads them to build the
function declarations.
"""

import inspect
import json
import time
import typing
from collections.abc import Callable, Iterator
from functools import wraps
from typing import Any

from navegador import llm
from navegador.config import regions, settings
from navegador.core import scribe
from navegador.core import state as st
from navegador.core.state import State
from navegador.schemas import ESTIMATED, Hypothesis
from navegador.tools import astrolabe, astrolabe_summary, geo, strategist

ACTOR = "detective"
# Tools whose results contain looked-up places. sun_check is not one: its result echoes the input lat/lng,
# so citing it would let a remembered coordinate pass as a tool result (R2).
PROVENANCE_TOOLS = frozenset({"geocode", "reverse", "overpass_near", "overpass_named", "wiki_coords"})


class _Session:
    """Mutable state of one tool loop: lookup budget, recorded and rejected hypotheses."""

    def __init__(self, state: State):
        self.state = state
        self.budget = settings().budgets.detective_max_tool_calls
        # Soft deadline (pipeline integration): llm.call's timeout_s cannot stop the AFC worker thread, and the
        # model records hypotheses only at the end. So lookups say "time is up" after soft_deadline_s, and the
        # model records its candidates well before the hard timeout.
        self.deadline = time.monotonic() + settings().detective.soft_deadline_s
        self.used = 0
        self.hypotheses: list[Hypothesis] = []
        self.rejected: list[dict[str, Any]] = []


def _coords(obj: Any) -> Iterator[tuple[float, float]]:
    """Every (lat, lng) pair inside a tool result."""
    if isinstance(obj, dict):
        if isinstance(obj.get("lat"), (int, float)) and isinstance(obj.get("lng"), (int, float)):
            yield float(obj["lat"]), float(obj["lng"])
        for v in obj.values():
            yield from _coords(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _coords(v)


def _cited_call(state: State, call_id: str) -> dict[str, Any] | None:
    """The `tool_call` payload with this call_id in this run, or None."""
    for e in scribe.read_events(state.pano_id, state.run_id):
        if e["event"] == "tool_call" and e["payload"].get("call_id") == call_id:
            return e["payload"]
    return None


def _check(state: State, lat: float, lng: float, provenance: str) -> str | None:
    """Why this point must be rejected, or None if it is acceptable."""
    if not regions().contains(lat, lng):
        return "outside the competition regions (PT, ES, FR incl. islands)"
    for z in state.excluded_zones:
        if strategist.haversine_m((lat, lng), (z.lat, z.lng)) <= z.radius_m:
            return f"inside excluded zone {z.label or z.ref!r}"
    if provenance == ESTIMATED:
        return None
    call = _cited_call(state, provenance)
    if call is None:
        return f"provenance {provenance!r} is not a call_id of this run; cite the call_id of a tool result"
    if call.get("tool") not in PROVENANCE_TOOLS:
        return (f"{call.get('tool')} results cannot be provenance; cite a call_id from "
                f"{', '.join(sorted(PROVENANCE_TOOLS))} that returned this place")
    cited = list(_coords(call.get("result")))
    tol = settings().detective.provenance_tolerance_m.get(state.label)
    if not cited:
        return f"tool result {provenance!r} contains no coordinates"
    nearest = min(cited, key=lambda c: strategist.haversine_m((lat, lng), c))
    d = strategist.haversine_m((lat, lng), nearest)
    if d > tol:
        return (f"point is {d:.0f} m from the nearest coordinate in {provenance!r} "
                f"({nearest[0]:.6f}, {nearest[1]:.6f}); max {tol:.0f} m. Use coordinates from the tool result.")
    return None


def make_add_hypothesis(state: State, session: _Session | None = None) -> Callable[..., dict]:
    """Return the typed `add_hypothesis(...)` tool bound to this run's state and tool registry."""
    session = session or _Session(state)
    cfg = settings().detective

    def add_hypothesis(lat: float, lng: float, radius_m: float, weight: float, label: str, provenance: str,
                       evidence: list[str], why: str) -> dict:
        """Record one candidate location. The only way to propose a location.

        Args:
            lat: latitude in degrees, taken from a tool result.
            lng: longitude in degrees, taken from a tool result.
            radius_m: how far the true spot may be from this point, in metres.
            weight: belief in this candidate, 0 to 1.
            label: short name of the place.
            provenance: call_id of the tool result that gave these coordinates, or "estimated".
            evidence: short list of the clues that support this place.
            why: one or two sentences.

        Returns {"ok": true, ...} or {"ok": false, "error": ...}; fix the error and call again.
        """
        def reject(error: str) -> dict:
            session.rejected.append({"lat": lat, "lng": lng, "label": label, "provenance": provenance, "error": error})
            scribe.log_event(state.pano_id, "tool_call", ACTOR, {"tool": "add_hypothesis", "accepted": False,
                             "args": {"lat": lat, "lng": lng, "label": label, "provenance": provenance},
                             "error": error}, run_id=state.run_id)
            return {"ok": False, "error": error}

        if len(session.hypotheses) >= cfg.max_hypotheses:
            return reject(f"already {cfg.max_hypotheses} hypotheses; stop and write the summary")
        if error := _check(state, lat, lng, provenance):
            return reject(error)
        w = max(0.0, min(1.0, float(weight)))
        if provenance == ESTIMATED:
            w *= settings().strategist.estimated_weight_factor
        r = max(cfg.radius_min_m.get(state.label), min(cfg.radius_max_m.get(state.label), float(radius_m)))
        registry = scribe.tool_registry(state.pano_id, state.run_id)
        h = Hypothesis.model_validate(
            {"lat": lat, "lng": lng, "radius_m": r, "weight": w, "label": label, "provenance": provenance,
             "evidence": list(evidence), "why": why, "source": ACTOR}, context={"registry": registry})
        session.hypotheses.append(h)
        scribe.log_event(state.pano_id, "tool_call", ACTOR, {"tool": "add_hypothesis", "accepted": True,
                         "hypothesis": h.model_dump()}, run_id=state.run_id)
        return {"ok": True, "hypothesis_no": len(session.hypotheses), "radius_m": r, "weight": round(w, 3)}

    return add_hypothesis


def _lookup(session: _Session, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Budget, run attribution and error capture for one lookup tool; errors go back to the model."""
    @wraps(fn)
    def tool(*args: Any, **kwargs: Any) -> Any:
        if session.used >= session.budget:
            return {"error": "tool budget exhausted; record your best candidates with add_hypothesis and stop"}
        if time.monotonic() >= session.deadline:
            scribe.log_event(session.state.pano_id, "tool_call", ACTOR,
                             # not "tool": geo counts tool_call events by "tool" against the Nominatim budget
                             {"refused_tool": fn.__name__, "reason": "soft deadline"}, run_id=session.state.run_id)
            return {"error": "time is up; record your best candidates with add_hypothesis now and stop"}
        session.used += 1
        try:
            with geo.use_run(session.state.pano_id, ACTOR):
                result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the model decides what to do next
            scribe.log_event(session.state.pano_id, "error", ACTOR,
                             {"tool": fn.__name__, "args": kwargs or list(args), "error": f"{type(exc).__name__}: {exc}"},
                             run_id=session.state.run_id)
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {"results": result} if isinstance(result, list) else (result if result is not None else {"results": []})

    # geo.py uses `from __future__ import annotations`, so its hints are strings; the SDK isinstance-checks
    # arguments against them and fails. Give the wrapper the resolved types.
    hints = typing.get_type_hints(fn)
    sig = inspect.signature(fn)
    tool.__signature__ = sig.replace(  # type: ignore[attr-defined]
        parameters=[p.replace(annotation=hints.get(p.name, p.annotation)) for p in sig.parameters.values()],
        return_annotation=hints.get("return", sig.return_annotation))
    tool.__annotations__ = hints
    return tool


def _tools(session: _Session) -> list[Callable[..., Any]]:
    state = session.state
    sun = state.sun

    def sun_check(lat: float, lng: float) -> dict:
        """Expected sun position at a candidate for this panorama's timestamp, compared with the observed sun
        (filled in automatically). Returns per clock hypothesis the expected elevation/azimuth, the elevation
        residual and north_yaw_deg (image yaw that points north), plus `consistency` (0-1)."""
        if not state.timestamp:
            return {"error": "panorama has no timestamp"}
        result = astrolabe.sun_check(lat, lng, state.timestamp, sun.yaw if sun else None,
                                     sun.elevation if sun else None, sun.uncertainty_deg if sun else None)
        call_id = scribe.register_tool_call(state.pano_id, ACTOR, "sun_check", {"lat": lat, "lng": lng}, result)
        return {**result, "call_id": call_id}

    lookups = [geo.geocode, geo.reverse, geo.overpass_near, geo.overpass_named, geo.wiki_coords, sun_check]
    return [*(_lookup(session, f) for f in lookups), make_add_hypothesis(state, session)]


def _read_json(state: State, name: str) -> Any:
    path = st.run_dir(state.pano_id) / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _inputs(state: State) -> dict[str, Any]:
    """Everything the Detective may use, as data (SPEC §10)."""
    sun = astrolabe_summary.summarise(state.sun, state.timestamp) if state.timestamp else None
    return {
        "label": state.label, "timestamp": state.timestamp,
        "reader": _read_json(state, "reader.json"), "geographer": _read_json(state, "geographer.json"),
        "sun": sun,
        "cues": [c.text for c in state.cues if not c.guard_flag or c.override],
        "mcq_answers": [{"question": m.question, "answer": next((o.text for o in m.options if o.id == m.answer), None)}
                        for m in state.mcqs if m.answer],
        "excluded_zones": [z.model_dump() for z in state.excluded_zones],
        "tool_budget": settings().budgets.detective_max_tool_calls,
    }


def _add_usage(state: State, usage: dict) -> None:
    for u in usage.get("attempts", [usage]):
        for k, v in u.items():
            if isinstance(v, int):
                state.token_usage[k] = state.token_usage.get(k, 0) + v


async def run(state: State) -> list[Hypothesis]:
    """Run the tool loop; save hypotheses + final summary to runs/<id>/detective.json.

    Hypotheses recorded before a failure or timeout are kept (the failure is logged)."""
    session = _Session(state)
    inputs = _inputs(state)
    summary, error = "", None
    try:
        res = await llm.call("detective", "detective.md", pano_id=state.pano_id,
                             text_parts=[json.dumps(inputs, ensure_ascii=False, default=str)],
                             tools=_tools(session), max_tool_calls=session.budget + settings().detective.extra_turns,
                             timeout_s=settings().detective.timeout_s, max_attempts=2)
        _add_usage(state, res.usage)
        summary = res.text
    except Exception as exc:  # noqa: BLE001 - keep what was recorded
        error = f"{type(exc).__name__}: {exc}"
        scribe.log_event(state.pano_id, "error", ACTOR, {"where": "tool loop", "error": error}, run_id=state.run_id)
    out = {"hypotheses": [h.model_dump() for h in session.hypotheses], "summary": summary,
           "lookups_used": session.used, "rejected": session.rejected, "error": error}
    (st.run_dir(state.pano_id) / "detective.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                                              encoding="utf-8")
    return session.hypotheses
