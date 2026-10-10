"""LiveBackend: the real pipeline (core/pipeline.py). Makes real Gemini/Nominatim calls.

pipeline.advance / answer_mcq / force_submit block until the agent stops (a question or the result), which
can take minutes, so they run on ONE background worker thread; the UI reads runs/<id>/state.json, the agent
JSON files and log.jsonl on every tick. One worker for all panoramas: stages share llm.call_sync's event
loop, so two runs must never overlap (a second start waits in the queue).
pipeline.add_cue runs directly (cue guard, ~seconds); the worker then continues the run.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from navegador.core import navigator, pipeline, scribe
from navegador.core import state as st
from navegador.schemas import CueResult
from ui.backend import RunView, assess_reason, stages_for

FINDING_FILES = ("reader", "geographer", "detective", "advocate", "baseline")


class LiveBackend:
    name = "Live"
    can_answer_mcq = True
    can_add_cue = True

    def __init__(self, inline: bool = False) -> None:
        """inline=True runs tasks synchronously (tests); the UI uses the worker thread."""
        self.inline = inline
        self.pano_ids: set[str] = set()
        self._tasks: queue.Queue[tuple[str, Callable[..., Any], tuple[Any, ...]]] = queue.Queue()
        self._busy: dict[str, int] = {}  # pano_id -> queued or running tasks
        self._errors: dict[str, str] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    # ------------------------------------------------------------ worker

    def _submit(self, pano_id: str, fn: Callable[..., Any], *args: Any) -> None:
        with self._lock:
            self._busy[pano_id] = self._busy.get(pano_id, 0) + 1
            self._errors.pop(pano_id, None)
        if self.inline:
            self._run(pano_id, fn, args)
            return
        self._tasks.put((pano_id, fn, args))
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, name="navegador-live", daemon=True)
            self._worker.start()

    def _loop(self) -> None:
        while True:
            pano_id, fn, args = self._tasks.get()
            self._run(pano_id, fn, args)

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

    # ------------------------------------------------------------ Backend API

    def busy(self, pano_id: str | None) -> bool:
        return pano_id in self._busy

    def error(self, pano_id: str | None) -> str | None:
        return self._errors.get(pano_id) if pano_id else None

    def has(self, pano_id: str | None) -> bool:
        return pano_id in self.pano_ids

    def start(self, pano_path: Path, label: str, special_one: bool, mode: str) -> str:
        pano_id = pipeline.start_run(Path(pano_path), label, special_one, mode)  # S0 only: fast
        self.pano_ids.add(pano_id)
        self._submit(pano_id, pipeline.advance, pano_id)
        return pano_id

    def view(self, pano_id: str) -> RunView:
        state = st.load(pano_id)
        out = st.run_dir(pano_id)
        findings: dict[str, Any] = {}
        for name in FINDING_FILES:
            try:
                findings[name] = json.loads((out / f"{name}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        reason = assess_reason(navigator.assess(state)) if state.mode != "baseline" else ""
        return RunView(state=state, findings=findings, events=scribe.read_events(pano_id, state.run_id),
                       stages=stages_for(state.mode), mcq_reason=reason)

    def step(self, pano_id: str) -> RunView:
        return self.view(pano_id)  # the worker advances the run; nothing to do per UI tick

    def answer_mcq(self, pano_id: str, option_id: str) -> RunView:
        self._submit(pano_id, pipeline.answer_mcq, pano_id, option_id)
        return self.view(pano_id)

    def add_cue(self, pano_id: str, text: str, override: bool = False) -> CueResult:
        res = pipeline.add_cue(pano_id, text, override=override)
        if res.accepted:
            self._submit(pano_id, pipeline.advance, pano_id)
        return res

    def force_submit(self, pano_id: str) -> RunView:
        self._submit(pano_id, pipeline.force_submit, pano_id)  # after any task already queued for this run
        return self.view(pano_id)

    def log_json(self, pano_id: str) -> str:
        """runs/<id>/log.json once the run is reported, else the same shape built from log.jsonl."""
        state = st.load(pano_id)
        path = st.run_dir(pano_id) / "log.json"
        if state.status == "done" and path.exists():
            return path.read_text(encoding="utf-8")
        meta = {**scribe.metadata(), "pano_id": pano_id, "run_id": state.run_id}
        return json.dumps({"metadata": meta, "events": scribe.read_events(pano_id, state.run_id)},
                          ensure_ascii=False, indent=2)
