"""MockBackend: an in-memory pipeline for development and the demo. No API calls, nothing written to runs/.

It follows the real contracts: State/MCQ/Cue/Penalty schemas, navigator.assess for the question rule,
Strategist maths for the submission, ×0.95 per answer and ×0.8 per cue, scribe-style events and a
log.json-shaped export. One `step()` advances one stage (the UI calls it once per second).
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from navegador.config import settings
from navegador.core import navigator
from navegador.core.state import State, utcnow
from navegador.schemas import MCQ, Cue, CueResult, ExcludedZone, Hypothesis, MCQOption, Penalty, SunObs
from navegador.tools import strategist
from ui.backend import RunView, assess_reason, penalty_factor, stages_for

NONE_TEXT = "None of these areas"

# Two rounds per label: round 2 is used after "none of these".
SCENARIOS: dict[str, list[list[tuple[str, float, float, float]]]] = {
    "urban": [[("Baixa, Lisboa", 38.7104, -9.1366, 0.42), ("Ribeira, Porto", 41.1407, -8.6131, 0.33),
               ("Baixa, Coimbra", 40.2085, -8.4290, 0.17), ("Belém, Lisboa", 38.6970, -9.2060, 0.08)],
              [("Centro, Braga", 41.5503, -8.4201, 0.62), ("Sé, Évora", 38.5714, -7.9077, 0.38)]],
    "non_urban": [[("Alto Alentejo", 38.95, -7.55, 0.45), ("Extremadura", 39.00, -6.50, 0.30),
                   ("Ribatejo", 39.30, -8.60, 0.25)],
                  [("Beira Interior", 40.30, -7.30, 0.70), ("Trás-os-Montes", 41.60, -7.00, 0.30)]],
}
FINDINGS = {
    "urban": {
        "reader": {"texts": [{"text": "Farmácia", "view": "h090", "kind": "shop", "language": "pt", "confidence": 0.9},
                             {"text": "Rua dos Fanq…", "view": "h180", "kind": "sign", "language": "pt",
                              "confidence": 0.55}],
                   "language_verdict": {"pt": 0.92, "es": 0.05, "fr": 0.0, "other": 0.03}, "search_terms": []},
        "geographer": {"country_probs": {"pt": 0.9, "es": 0.08, "fr": 0.02},
                       "region_guesses": [{"region": "Lisboa", "country": "pt", "prob": 0.55,
                                           "why": "limestone calçada g000, azulejo façade h090"},
                                          {"region": "Porto", "country": "pt", "prob": 0.3, "why": "granite h270"}],
                       "terrain": "hilly old town", "season": "winter"},
        "detective": {"summary": "Portuguese old-town street with calçada and azulejos; the street sign is only "
                                 "partly legible, so Lisboa and Porto both fit."},
        "advocate": {"objections": [{"hypothesis_label": "Ribeira, Porto", "issue": "No granite paving visible.",
                                     "severity": "medium"}], "weight_multipliers": {"Ribeira, Porto": 0.7}},
    },
    "non_urban": {
        "reader": {"texts": [], "language_verdict": {"pt": 0.4, "es": 0.4, "fr": 0.0, "other": 0.2},
                   "search_terms": []},
        "geographer": {"country_probs": {"pt": 0.6, "es": 0.38, "fr": 0.02},
                       "region_guesses": [{"region": "Alentejo", "country": "pt", "prob": 0.5,
                                           "why": "cork oaks h090, dry grass, rolling plain"},
                                          {"region": "Extremadura", "country": "es", "prob": 0.3,
                                           "why": "same dehesa landscape"}],
                       "terrain": "rolling plain", "season": "late summer"},
        "detective": {"summary": "Montado landscape on both sides of the border; no road signs to separate "
                                 "Alentejo from Extremadura."},
        "advocate": {"objections": [{"hypothesis_label": "Extremadura", "issue": "Chevron style looks Portuguese.",
                                     "severity": "low"}], "weight_multipliers": {"Extremadura": 0.8}},
    },
}
# Mock cue guard. The real guard is an LLM (agents/cue_guard.py); this only catches obvious cases.
_PLACE_WORDS = re.compile(
    r"\b(portugal|spain|españa|espanha|france|lisbo[an]|lisboa|porto|madrid|paris|sevilla|barcelona|coimbra|braga|"
    r"évora|evora|faro|alentejo|algarve|douro|minho|galicia|extremadura|andaluc[ií]a|madeira|azores|açores|"
    r"canar(y|ias)|corsica|balear\w*)\b|\b(near|north of|south of|east of|west of|city of|town of|village of)\b|"
    r"\d+(\.\d+)?\s*°|\blat(itude)?\b|\blng\b|\blongitude\b", re.I)


def mock_guard(text: str) -> tuple[bool, str]:
    m = _PLACE_WORDS.search(text)
    return (True, f'mentions "{m.group(0)}", which points to a location') if m else (False, "")


class MockBackend:
    """In-memory backend. Every public method returns the run's current RunView."""

    name = "Mock"
    can_answer_mcq = True
    can_add_cue = True

    def __init__(self) -> None:
        self.runs: dict[str, RunView] = {}
        self.rounds: dict[str, int] = {}

    # ------------------------------------------------------------ events

    def _log(self, view: RunView, event: str, actor: str, payload: dict[str, Any]) -> None:
        s = view.state
        view.events.append({"ts": utcnow().isoformat(), "run_id": s.run_id, "pano_id": s.pano_id, "event": event,
                            "actor": actor, "payload": json.loads(json.dumps(payload, default=str))})

    def _penalty(self, view: RunView, kind: str, ref: str) -> None:
        s = view.state
        s.penalties.append(Penalty(kind=kind, factor=penalty_factor(kind), ts=utcnow(), ref=ref))
        total = 1.0
        for p in s.penalties:
            total *= p.factor
        self._log(view, "penalty", "scribe", {"kind": kind, "factor": penalty_factor(kind), "ref": ref,
                                              "total_factor": total})

    # ------------------------------------------------------------ API (mirrors core/pipeline.py)

    def start(self, pano_path: Path, label: str, special_one: bool, mode: str) -> str:
        pano_id = f"mock-{Path(pano_path).stem}"
        state = State(pano_id=pano_id, run_id=uuid.uuid4().hex[:12], pano_path=Path(pano_path), label=label,
                      mode=mode, special_one=special_one, stage="S1")
        view = RunView(state=state, stages=stages_for(mode))
        self.runs[pano_id], self.rounds[pano_id] = view, 0
        self._log(view, "run_start", "pipeline", {"pano_path": str(pano_path), "label": label, "mode": mode,
                                                  "special_one": special_one, "backend": "mock"})
        return pano_id

    def has(self, pano_id: str | None) -> bool:
        return pano_id in self.runs

    def busy(self, pano_id: str | None) -> bool:
        return False  # the UI ticker drives the mock one stage at a time

    def error(self, pano_id: str | None) -> str | None:
        return None

    def view(self, pano_id: str) -> RunView:
        return self.runs[pano_id]

    def step(self, pano_id: str) -> RunView:
        """Run the current stage and move to the next one (only while status is running)."""
        view = self.runs[pano_id]
        s = view.state
        if s.status != "running":
            return view
        stage, order = s.stage, [c for c, _ in view.stages if c != "S0"]
        getattr(self, f"_{stage.lower()}")(view)
        self._log(view, "stage", "pipeline", {"stage": stage, "status": "done"})
        if s.status == "running" and s.stage == stage:  # the stage did not redirect
            s.stage = order[order.index(stage) + 1] if stage != order[-1] else "done"
        if s.stage == "done":
            s.status = "done"
        return view

    def answer_mcq(self, pano_id: str, option_id: str) -> RunView:
        view = self.runs[pano_id]
        s, mcq = view.state, view.state.pending_mcq
        if mcq is None:
            raise ValueError("no pending MCQ")
        option = next(o for o in mcq.options if o.id == option_id)
        mcq.answer, mcq.answered_at = option.id, utcnow()
        self._penalty(view, "mcq", mcq.mcq_id)
        cls = {c.cluster_id: c for c in strategist.clusters(s.hypotheses, settings().mcq.cluster_eps_m.get(s.label))}
        if option.cluster_id is not None and option.cluster_id in cls:
            chosen = {id(h) for h in cls[option.cluster_id].members}
            q = settings().mcq.q_trust
            for group, total in (([h for h in s.hypotheses if id(h) in chosen], q),
                                 ([h for h in s.hypotheses if id(h) not in chosen], 1 - q)):
                w = sum(h.weight for h in group)
                for h in group:
                    h.weight = total * (h.weight / w if w else 1 / len(group))
            s.stage, nxt = "S6", "S6"
        else:
            shown = [cls[o.cluster_id] for o in mcq.options if o.cluster_id in cls]
            for c in shown:
                s.excluded_zones.append(ExcludedZone(lat=c.lat, lng=c.lng, radius_m=max(h.radius_m for h in c.members) * 4,
                                                     label=c.members[0].label, ref=mcq.mcq_id))
            gone = {id(h) for c in shown for h in c.members}
            s.hypotheses = [h for h in s.hypotheses if id(h) not in gone]
            self.rounds[pano_id] += 1
            s.stage, nxt = "S4", "S4"
        s.status = "running"
        self._log(view, "mcq_answer", "navigator", {"mcq_id": mcq.mcq_id, "option": option.id, "text": option.text,
                                                    "cluster_id": option.cluster_id, "next": nxt})
        return view

    def add_cue(self, pano_id: str, text: str, override: bool = False) -> CueResult:
        """Same rules as pipeline.add_cue: assisted mode only, not while a question is pending."""
        view = self.runs[pano_id]
        s = view.state
        if s.mode != "assisted":
            raise ValueError(f"cues are only allowed in assisted mode (run is {s.mode})")
        if s.pending_mcq is not None:
            raise ValueError("answer the pending MCQ before sending a cue")
        text = text.strip()
        if not text or len(text) > 1000:
            return CueResult(accepted=False, guard_flag=False, why="A cue must be 1–1000 characters.")
        flagged, why = mock_guard(text)
        if flagged and not override:
            self._log(view, "cue_blocked", "cue_guard", {"text": text, "why": why})
            return CueResult(accepted=False, guard_flag=True, why=why)
        cue = Cue(text=text, ts=utcnow(), guard_flag=flagged, override=override and flagged)
        s.cues.append(cue)
        self._log(view, "cue_override" if cue.override else "cue", "ui", {"text": text, "guard_flag": flagged,
                                                                          "why": why})
        self._penalty(view, "cue", f"cue-{len(s.cues)}")
        s.stage, s.status = "S4", "running"
        return CueResult(accepted=True, guard_flag=flagged, why=why, cue=cue)

    def force_submit(self, pano_id: str) -> RunView:
        view = self.runs[pano_id]
        s = view.state
        if s.status == "done":
            return view
        self._log(view, "stage", "pipeline", {"stage": "force_submit", "from": s.stage})
        if (pending := s.pending_mcq) is not None:
            s.mcqs.remove(pending)
        s.status, s.stage = "running", "S7"
        self.step(pano_id)
        return self.step(pano_id)

    def log_json(self, pano_id: str) -> str:
        view = self.runs[pano_id]
        meta = {"backend": "mock", "pano_id": pano_id, "run_id": view.state.run_id}
        return json.dumps({"metadata": meta, "events": view.events}, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------ stages

    def _s1(self, view: RunView) -> None:
        pass  # views

    def _s2(self, view: RunView) -> None:
        view.state.sun = SunObs(yaw=-61.0, elevation=54.4, area_px=180, confidence=0.5, uncertainty_deg=10.8)

    def _s3(self, view: RunView) -> None:
        f = FINDINGS[view.state.label]
        view.findings.update(reader=f["reader"], geographer=f["geographer"])

    def _s4(self, view: RunView) -> None:
        s = view.state
        rows = SCENARIOS[s.label][min(self.rounds[s.pano_id], 1)]
        radius = 250 if s.label == "urban" else 8000
        excluded = [(z.lat, z.lng, z.radius_m) for z in s.excluded_zones]
        s.hypotheses = [Hypothesis(lat=lat, lng=lng, radius_m=radius, weight=w, label=lab, provenance="estimated",
                                   evidence=["mock"], why="mock", source="detective")
                        for lab, lat, lng, w in rows
                        if all(strategist.haversine_m((lat, lng), (zl, zg)) > zr for zl, zg, zr in excluded)]
        view.findings["detective"] = FINDINGS[s.label]["detective"]

    def _s5(self, view: RunView) -> None:
        s, adv = view.state, FINDINGS[view.state.label]["advocate"]
        view.findings["advocate"] = adv
        for h in s.hypotheses:
            h.weight *= adv["weight_multipliers"].get(h.label, 1.0)

    def _s6(self, view: RunView) -> None:
        s = view.state
        a = navigator.assess(s)
        view.mcq_reason = assess_reason(a)
        if a["decision"] != "ASK_MCQ":
            return
        cls = strategist.clusters(s.hypotheses, settings().mcq.cluster_eps_m.get(s.label))
        far = [c for c in cls if c.cluster_id in a["far_clusters"]][:3]
        options = [MCQOption(id="ABC"[i], text=c.members[0].label, cluster_id=c.cluster_id) for i, c in enumerate(far)]
        options.append(MCQOption(id="ABCD"[len(far)], text=NONE_TEXT, cluster_id=None))
        mcq = MCQ(mcq_id=f"mcq{len(s.mcqs) + 1}-{uuid.uuid4().hex[:6]}", question="Which area does this panorama show?",
                  options=options, asked_at=utcnow())
        s.mcqs.append(mcq)
        s.status = "awaiting_human"
        self._log(view, "mcq_shown", "navigator", {"mcq_id": mcq.mcq_id, "question": mcq.question,
                                                   "options": [o.model_dump() for o in options]})

    def _baseline(self, view: RunView) -> None:
        self._s3(view)
        self._s4(view)
        view.findings["baseline"] = {"country_probs": view.findings["geographer"]["country_probs"]}

    def _s7(self, view: RunView) -> None:
        sub = strategist.submit(view.state)
        view.state.submission = sub
        self._log(view, "submission", "strategist", {"candidates": [c.model_dump() for c in sub.candidates],
                                                     "expected_score": sub.expected_score,
                                                     "penalty_factor": sub.penalty_factor})

    def _s8(self, view: RunView) -> None:
        pass  # report

