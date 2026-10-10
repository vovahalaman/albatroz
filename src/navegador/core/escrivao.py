"""Escrivão: JSON event log, tool-call registry, penalty ledger, exports (SPEC §11).

`runs/<pano_id>/log.jsonl` is append-only, one event per line. `export()` writes
`log.json` (events + metadata) and `report.md` (readable deduction trace).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import BaseModel

from navegador.config import KNOWLEDGE_DIR, PROMPTS_DIR, ROOT, runs_dir, settings
from navegador.core import state as st
from navegador.schemas import Penalty

Event = Literal[
    "run_start", "stage", "llm_call", "tool_call", "mcq_shown", "mcq_answer",
    "cue", "cue_blocked", "cue_override", "penalty", "submission", "error",
]
EVENTS: frozenset[str] = frozenset(get_args(Event))
_lock = threading.Lock()


# ---------------------------------------------------------------- helpers


def _default(o: Any) -> Any:
    if isinstance(o, BaseModel):
        return o.model_dump(mode="json")
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if isinstance(o, (bytes, bytearray)):  # never base64 in logs
        return {"bytes": len(o), "sha256": hashlib.sha256(o).hexdigest()}
    return repr(o)


def to_jsonable(o: Any) -> Any:
    return json.loads(json.dumps(o, default=_default, ensure_ascii=False))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def short_hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, default=_default, sort_keys=True).encode()).hexdigest()[:10]


@lru_cache
def git_commit() -> str:
    return _git("rev-parse", "--short", "HEAD") or "unknown"


def git_file_hash(path: Path) -> str:
    """Short hash of the last commit touching `path`, with '+dirty' if the file has local changes."""
    commit = _git("log", "-n1", "--format=%h", "--", str(path)) or "uncommitted"
    dirty = _git("status", "--porcelain", "--", str(path))
    return commit + ("+dirty" if dirty else "")


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _log_path(pano_id: str) -> Path:
    return st.run_dir(pano_id) / "log.jsonl"


def _current_run_id(pano_id: str) -> str:
    path = runs_dir() / pano_id / "state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("run_id", "")
    except (OSError, ValueError):
        return ""


# ---------------------------------------------------------------- event log


def log_event(
    pano_id: str,
    event: Event,
    actor: str,
    payload: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Append one event to `runs/<pano_id>/log.jsonl` and return it."""
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}; allowed: {sorted(EVENTS)}")
    record = {
        "ts": st.utcnow().isoformat(),
        "run_id": run_id if run_id is not None else _current_run_id(pano_id),
        "pano_id": pano_id,
        "event": event,
        "actor": actor,
        "payload": to_jsonable(payload or {}),
    }
    line = json.dumps(record, ensure_ascii=False)
    with _lock, _log_path(pano_id).open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return record


def read_events(pano_id: str, run_id: str | None = None) -> list[dict[str, Any]]:
    path = runs_dir() / pano_id / "log.jsonl"
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [e for e in events if run_id is None or e.get("run_id") == run_id]


# ---------------------------------------------------------------- tool registry (R2)


def register_tool_call(
    pano_id: str, actor: str, tool: str, args: dict[str, Any], result: Any, cached: bool = False
) -> str:
    """Log a `tool_call` event and return its `call_id`; hypotheses cite it as provenance."""
    call_id = f"{tool}-{short_hash(tool, args, result)}"
    log_event(pano_id, "tool_call", actor,
              {"call_id": call_id, "tool": tool, "args": args, "result": result, "cached": cached})
    return call_id


def tool_registry(pano_id: str, run_id: str | None = None) -> set[str]:
    """All call_ids registered in this run (default: the run in state.json)."""
    rid = run_id if run_id is not None else _current_run_id(pano_id)
    return {e["payload"]["call_id"] for e in read_events(pano_id, rid)
            if e["event"] == "tool_call" and "call_id" in e["payload"]}


# ---------------------------------------------------------------- penalty ledger


def add_penalty(state: st.State, kind: Literal["cue", "mcq", "text_location"], ref: str) -> Penalty:
    """Append a penalty to the state (caller saves) and log a `penalty` event."""
    factor = settings().scoring.penalty_factors[kind]
    penalty = Penalty(kind=kind, factor=factor, ts=st.utcnow(), ref=ref)
    state.penalties.append(penalty)
    log_event(state.pano_id, "penalty", "escrivao",
              {"kind": kind, "factor": factor, "ref": ref, "total_factor": penalty_factor(state)},
              run_id=state.run_id)
    return penalty


def penalty_factor(state: st.State) -> float:
    total = 1.0
    for p in state.penalties:
        total *= p.factor
    return total


# ---------------------------------------------------------------- export


def metadata() -> dict[str, Any]:
    prompts = {p.name: sha256_file(p)[:12] for p in sorted(PROMPTS_DIR.glob("*.md"))}
    knowledge = hashlib.sha256(
        b"".join(p.read_bytes() for p in sorted(KNOWLEDGE_DIR.glob("*.md")))).hexdigest()[:12]
    return {"git_commit": git_commit(), "prompt_hashes": prompts,
            "knowledge_hash": knowledge, "models": dict(settings().models)}


def export(pano_id: str) -> tuple[Path, Path]:
    """Write `log.json` and `report.md` for the current run; return both paths."""
    state = st.load(pano_id)
    events = read_events(pano_id, state.run_id)
    out = st.run_dir(pano_id)
    log_json = out / "log.json"
    log_json.write_text(json.dumps(
        {"metadata": {**metadata(), "pano_id": pano_id, "run_id": state.run_id}, "events": events},
        ensure_ascii=False, indent=2), encoding="utf-8")
    report = out / "report.md"
    report.write_text(render_report(state, events), encoding="utf-8")
    return log_json, report


def render_report(state: st.State, events: list[dict[str, Any]]) -> str:
    out = st.run_dir(state.pano_id)
    lines = [f"# Report · {state.pano_id}", "",
             f"- run `{state.run_id}` · mode **{state.mode}** · label **{state.label}** · "
             f"timestamp `{state.timestamp}` · special one: {state.special_one}",
             f"- status **{state.status}** at stage {state.stage} · git `{git_commit()}`", ""]

    lines += ["## Clues", ""]
    for agent in ("baseline", "leitor", "geografo", "detetive", "advogado"):
        path = out / f"{agent}.json"
        if path.exists():
            lines += [f"<details><summary>{agent}.json</summary>", "", "```json",
                      path.read_text(encoding="utf-8").strip(), "```", "</details>", ""]
    if state.sun:
        lines += [f"- Sun: yaw {state.sun.yaw:.1f}°, elevation {state.sun.elevation:.1f}°, "
                  f"confidence {state.sun.confidence:.2f}", ""]

    lines += ["## Hypotheses", "", "| # | label | lat, lng | radius m | weight | source | provenance | why |",
              "|---|---|---|---|---|---|---|---|"]
    for i, h in enumerate(sorted(state.hypotheses, key=lambda h: -h.weight), 1):
        lines.append(f"| {i} | {h.label} | {h.lat:.6f}, {h.lng:.6f} | {h.radius_m:.0f} | {h.weight:.3f} "
                     f"| {h.source} | `{h.provenance}` | {h.why} |")
    lines.append("")

    lines += ["## Decisions, MCQs and cues", ""]
    for e in events:
        if e["event"] in {"mcq_shown", "mcq_answer", "cue", "cue_blocked", "cue_override", "penalty", "error"}:
            lines.append(f"- `{e['ts']}` **{e['event']}** ({e['actor']}): "
                         f"{json.dumps(e['payload'], ensure_ascii=False)[:300]}")
    if state.excluded_zones:
        lines.append(f"- Excluded zones: {', '.join(z.label or f'{z.lat:.3f},{z.lng:.3f}' for z in state.excluded_zones)}")
    lines.append("")

    lines += ["## Submission", ""]
    sub = state.submission
    if sub:
        lines += [f"Penalty factor {sub.penalty_factor:.4f} · expected score {sub.expected_score:.0f}", "",
                  "| rank | lat | lng |", "|---|---|---|"]
        lines += [f"| {c.rank} | {c.lat:.6f} | {c.lng:.6f} |" for c in sub.candidates]
    else:
        lines.append("_No submission yet._")

    usage = sum(1 for e in events if e["event"] == "llm_call")
    lines += ["", "## Cost", "", f"- LLM calls: {usage} · tokens: {state.token_usage} · timings: {state.timings}", ""]
    return "\n".join(lines)
