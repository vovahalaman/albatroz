"""Runs the real pipeline (core/pipeline.py) for the UI, one panorama at a time, in assisted mode.

pipeline.advance / answer_mcq block until the agent stops (a question or the result), which can take
minutes, so they run on a background worker thread; the UI reads runs/<id>/ on every tick.
pipeline.add_cue runs directly (cue guard, seconds); the worker then continues the run.
NAVEGADOR_UI_INLINE=1 runs the worker tasks synchronously (tests).
"""

from __future__ import annotations

import json
import os
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from navegador.config import runs_dir, settings
from navegador.core import pipeline, scribe
from navegador.core import state as st
from navegador.core.state import State
from navegador.schemas import CueResult
from navegador.tools import strategist
from ui.progress import LINES

MODE = "assisted"
FINDING_FILES = ("detective", "geographer", "reader", "advocate")


@dataclass
class RunView:
    state: State
    findings: dict[str, Any] = field(default_factory=dict)
    busy: bool = False
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


def upload_dir() -> Path:
    d = runs_dir() / "_uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_upload(name: str, data: bytes) -> Path:
    """Keep the original file name: the pipeline reads the capture timestamp from it."""
    path = upload_dir() / Path(name).name
    path.write_bytes(data)
    return path


def stage_line(view: RunView) -> tuple[str, str]:
    """(text, kind) for the short line under the progress bar. kind: '' | 'warn' | 'error' | 'done'."""
    s = view.state
    if view.error:
        return f"Stopped with an error: {view.error}", "error"
    if s.pending_mcq is not None and not view.busy:
        return "Waiting for your answer", "warn"
    if s.status == "done" and not view.busy:
        return "Result ready", "done"
    if s.status == "error" and not view.busy:
        return "The run ended with an error; a fallback answer was submitted if possible", "error"
    return f"{LINES.get(s.stage, 'Working')}…", ""


def finished_at(view: RunView) -> float | None:
    """Epoch seconds when the result was ready (last logged event), else None."""
    if view.state.status != "done" or view.busy or not view.events:
        return None
    return datetime.fromisoformat(view.events[-1]["ts"]).timestamp()


def top_place(view: RunView) -> tuple[str, float, str]:
    """(place, weight share 0–1, one-sentence summary) for the result card. The place is the heaviest
    hypothesis in the cluster that holds coordinate #1; the share is that cluster's weight."""
    s, sub = view.state, view.state.submission
    summary = str((view.findings.get("detective") or {}).get("summary") or "").strip()
    summary = (summary.split(". ")[0].rstrip(".") + ".") if summary else ""
    if not s.hypotheses or sub is None:
        return "No confident place (fallback point)", 0.0, summary
    top = min(sub.candidates, key=lambda c: c.rank)
    cls = strategist.clusters(s.hypotheses, settings().mcq.cluster_eps_m.get(s.label))
    best = min(cls, key=lambda c: min(strategist.haversine_m((top.lat, top.lng), (h.lat, h.lng)) for h in c.members))
    total = sum(h.weight for h in s.hypotheses) or 1.0
    label = max(best.members, key=lambda h: h.weight).label
    return label, sum(h.weight for h in best.members) / total, summary


def cue_block_reason(view: RunView) -> str | None:
    """pipeline.add_cue rules: not while a question is pending; the agent only stops at a question or
    the result, so a cue is possible once it has stopped (it then investigates again)."""
    if view.state.pending_mcq is not None:
        return "Answer the question first."
    if view.busy or view.state.status == "running":
        return "Available when the agent stops."
    return None


class Runner:
    def __init__(self, inline: bool | None = None) -> None:
        self.inline = os.getenv("NAVEGADOR_UI_INLINE") == "1" if inline is None else inline
        self._tasks: queue.Queue[tuple[str, Callable[..., Any], tuple[Any, ...]]] = queue.Queue()
        self._busy: dict[str, int] = {}
        self._errors: dict[str, str] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def _submit(self, pano_id: str, fn: Callable[..., Any], *args: Any) -> None:
        with self._lock:
            self._busy[pano_id] = self._busy.get(pano_id, 0) + 1
            self._errors.pop(pano_id, None)
        if self.inline:
            self._run(pano_id, fn, args)
            return
        self._tasks.put((pano_id, fn, args))
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, name="navegador-ui", daemon=True)
            self._worker.start()

    def _loop(self) -> None:
        while True:
            self._run(*self._tasks.get())

    def _run(self, pano_id: str, fn: Callable[..., Any], args: tuple[Any, ...]) -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - shown to the operator; the pipeline logs its own errors
            self._errors[pano_id] = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._busy[pano_id] -= 1
                if not self._busy[pano_id]:
                    del self._busy[pano_id]

    # ------------------------------------------------------------ pipeline calls

    def start(self, path: Path, label: str) -> str:
        pano_id = pipeline.start_run(Path(path), label, False, MODE)  # S0 only: fast
        self._submit(pano_id, pipeline.advance, pano_id)
        return pano_id

    def answer(self, pano_id: str, option_id: str) -> None:
        self._submit(pano_id, pipeline.answer_mcq, pano_id, option_id)

    def add_cue(self, pano_id: str, text: str, override: bool = False) -> CueResult:
        res = pipeline.add_cue(pano_id, text, override=override)
        if res.accepted:
            self._submit(pano_id, pipeline.advance, pano_id)
        return res

    def view(self, pano_id: str) -> RunView:
        out = st.run_dir(pano_id)
        findings: dict[str, Any] = {}
        for name in FINDING_FILES:
            try:
                findings[name] = json.loads((out / f"{name}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        state = st.load(pano_id)
        return RunView(state=state, findings=findings, busy=pano_id in self._busy, error=self._errors.get(pano_id),
                       events=scribe.read_events(pano_id, state.run_id))

    def log_json(self, pano_id: str) -> str:
        """runs/<id>/log.json once reported, else the same shape built from log.jsonl."""
        state = st.load(pano_id)
        path = st.run_dir(pano_id) / "log.json"
        if state.status == "done" and path.exists():
            return path.read_text(encoding="utf-8")
        meta = {**scribe.metadata(), "pano_id": pano_id, "run_id": state.run_id}
        return json.dumps({"metadata": meta, "events": scribe.read_events(pano_id, state.run_id)},
                          ensure_ascii=False, indent=2)
