"""Reader: reads text and signage, determines the language (SPEC §10).

Pass 1: 8 views → ReaderResult incl. ≤6 zoom_requests. Code renders zooms (HIGH) via magnifier.zoom.
Pass 2: zoom crops + pass-1 JSON → merged final result, saved to runs/<id>/reader.json.
If pass 2 fails, the pass-1 result is kept (error logged).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from navegador import llm
from navegador.config import settings
from navegador.core import scribe
from navegador.core import state as st
from navegador.core.state import State
from navegador.schemas import ReaderResult, ZoomRequest
from navegador.tools import magnifier

H_VIEWS = [f"h{a:03d}" for a in range(0, 360, 45)]


def _context(state: State) -> str:
    """Run facts for the model (data, not prompt text)."""
    ts = state.timestamp
    when = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}" if ts else "unknown"
    tz = "local time" if settings().flags.timestamp_is_local else "UTC"
    return f"Label: {state.label}\nTimestamp: {when} ({tz})"


def _add_usage(state: State, usage: dict) -> None:
    for u in usage.get("attempts", [usage]):
        for k, v in u.items():
            if isinstance(v, int):
                state.token_usage[k] = state.token_usage.get(k, 0) + v


def _render_zooms(state: State, requests: list[ZoomRequest]) -> list[tuple[str, ZoomRequest, Path]]:
    """Render each zoom request; a request that cannot be rendered is logged and skipped."""
    out = []
    min_fov = settings().reader.zoom_min_fov
    for i, z in enumerate(requests, 1):
        z = z.model_copy(update={"fov": max(z.fov, min_fov)}) if z.fov < min_fov else z
        try:
            out.append((f"z{i}", z, magnifier.zoom(state.pano_path, z.yaw, z.pitch, z.fov)))
        except (ValueError, OSError) as exc:
            scribe.log_event(state.pano_id, "error", "reader",
                             {"where": "zoom", "request": z.model_dump(), "error": str(exc)}, run_id=state.run_id)
    return out


async def run(state: State) -> ReaderResult:
    """Two LLM calls (one if no zooms requested); writes `reader.json`, adds token usage (caller saves)."""
    out = st.run_dir(state.pano_id)
    views = {v.name: v for v in magnifier.views(state.pano_path, out / "views")}  # no-op if S1 ran
    order = ", ".join(f"{n} (yaw {views[n].yaw:g}°)" for n in H_VIEWS)
    res = await llm.call("perception", "reader.md", pano_id=state.pano_id,
                         text_parts=[f"Pass: 1\n{_context(state)}\nImages in order: {order}"],
                         images=[(views[n].path, "views") for n in H_VIEWS], schema=ReaderResult)
    _add_usage(state, res.usage)
    first: ReaderResult = res.parsed
    result = first

    zooms = await asyncio.to_thread(_render_zooms, state, first.zoom_requests)
    if zooms:
        listing = "\n".join(f"{name}: yaw {z.yaw:g}°, pitch {z.pitch:g}°, fov {z.fov:g}° — {z.why}"
                            for name, z, _ in zooms)
        try:
            res = await llm.call("perception", "reader.md", pano_id=state.pano_id,
                                 text_parts=[f"Pass: 2\n{_context(state)}\nZoom crops in order:\n{listing}",
                                             f"Pass-1 result:\n{first.model_dump_json()}"],
                                 images=[(path, "zoom") for _, _, path in zooms], schema=ReaderResult)
            _add_usage(state, res.usage)
            result = res.parsed
        except Exception as exc:  # noqa: BLE001 - keep pass 1 rather than lose the Reader
            scribe.log_event(state.pano_id, "error", "reader",
                             {"where": "pass 2", "error": f"{type(exc).__name__}: {exc}"}, run_id=state.run_id)

    # The final result records the zooms that were actually looked at.
    result = result.model_copy(update={"zoom_requests": [z for _, z, _ in zooms]})
    (out / "reader.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result
