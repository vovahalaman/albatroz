"""Geographer: landscape, vegetation, architecture, infrastructure → country/region probabilities (SPEC §10).

One LLM call over all magnifier views (8 h, 4 g, 4 u at `views` resolution + overview at `overview`
resolution). Run facts and the Astrolabe sun/compass note are sent as data (R6: instructions live in
prompts/geographer.md). Answers are cached on disk by pano content, prompt + knowledge, model and input,
so reruns cost nothing. Writes runs/<id>/geographer.json; adds token usage to the state (caller saves).
"""

from __future__ import annotations

import hashlib

from navegador import llm
from navegador.core import scribe
from navegador.core import state as st
from navegador.core.state import State
from navegador.schemas import GeographerResult, View
from navegador.tools import cache, magnifier
from navegador.tools.astrolabe_summary import summarise

PROMPT = "geographer.md"
ROLE = "perception"
MAX_REGION_GUESSES = 5  # prompts/geographer.md: "at most 5"


def _context(state: State, views: list[View]) -> str:
    """Run facts for the model (data, not prompt text)."""
    ts = state.timestamp
    when = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}" if ts else "unknown"
    order = ", ".join(f"{v.name} (yaw {v.yaw:g}°, pitch {v.pitch:g}°)" if v.name != "overview"
                      else "overview (whole panorama)" for v in views)
    lines = [f"Label: {state.label}", f"Camera clock: {when} (time zone not known)", f"Images in order: {order}"]
    if state.sun is not None and ts:
        lines.append(f"Sun/compass note (Astrolabe, low confidence): {summarise(state.sun, ts)['note']}")
    return "\n".join(lines)


def _add_usage(state: State, usage: dict) -> None:
    for u in usage.get("attempts", [usage]):
        for k, v in u.items():
            if isinstance(v, int):
                state.token_usage[k] = state.token_usage.get(k, 0) + v


def _cache_key(state: State, context: str, model: str) -> dict[str, str]:
    system = llm.load_prompt(PROMPT).system  # prompt body + knowledge files
    return {"pano": magnifier.pano_hash(state.pano_path), "model": model, "context": context,
            "prompt": hashlib.sha256(system.encode("utf-8")).hexdigest()}


async def run(state: State) -> GeographerResult:
    """One (cached) LLM call; writes `geographer.json`. LLM errors propagate to the caller."""
    out = st.run_dir(state.pano_id)
    views = magnifier.views(state.pano_path, out / "views")  # no-op if S1 already rendered them
    context = _context(state, views)
    key = _cache_key(state, context, llm.model_for(ROLE))
    hit, cached = cache.lookup("geographer", key)
    if hit:
        result = GeographerResult.model_validate(cached)
        scribe.log_event(state.pano_id, "tool_call", "geographer", {"cache": "hit", "model": key["model"]},
                         run_id=state.run_id)
    else:
        images = [(v.path, "overview" if v.name == "overview" else "views") for v in views]
        res = await llm.call(ROLE, PROMPT, pano_id=state.pano_id, text_parts=[context], images=images,
                             schema=GeographerResult)
        _add_usage(state, res.usage)
        result = res.parsed
        cache.put("geographer", key, result.model_dump(mode="json"))

    result = result.model_copy(update={"region_guesses": result.region_guesses[:MAX_REGION_GUESSES]})
    (out / "geographer.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result
