"""ui/progress.py: stage weights from past runs, smooth in-stage movement, caps and holds."""

import json
from datetime import datetime, timedelta, timezone

from ui import progress

T0 = datetime(2026, 10, 11, 10, 0, 0, tzinfo=timezone.utc)


def _start(stage: str, at: datetime) -> dict:
    return {"ts": at.isoformat(), "event": "stage", "payload": {"stage": stage, "status": "start"}}


def _write_runs(root, timings: list[dict], mode: str = "assisted") -> None:
    for i, t in enumerate(timings):
        d = root / f"run{i}"
        d.mkdir()
        (d / "state.json").write_text(json.dumps({"mode": mode, "timings": t}))


def test_defaults_when_too_few_runs(tmp_path):
    _write_runs(tmp_path, [{"S3": 99.0}])
    typ = progress.typical_durations(str(tmp_path))
    assert typ == {k: max(progress.MIN_STAGE_S, v) for k, v in progress.DEFAULTS.items()}
    assert typ["S3"] + typ["S4"] > 0.7 * sum(typ.values())  # perception and investigation dominate


def test_medians_from_past_crew_runs(tmp_path):
    _write_runs(tmp_path, [{"S3": 20.0, "S4": 60.0, "S5": 0.0}, {"S3": 30.0, "S4": 80.0}, {"S3": 40.0, "S4": 70.0}])
    _write_runs(tmp_path / "..", [], mode="baseline")
    typ = progress.typical_durations(str(tmp_path))
    assert typ["S3"] == 30.0 and typ["S4"] == 70.0
    assert typ["S5"] == progress.DEFAULTS["S5"]  # only one sample for S5 → default
    assert typ["S8"] == progress.MIN_STAGE_S or typ["S8"] == progress.DEFAULTS["S8"]


def test_moves_inside_a_stage_but_never_past_its_end():
    typ = {s: 10.0 for s in progress.ORDER}  # 8 equal stages
    ev = [_start("S3", T0)]
    at = lambda secs: progress.fraction("S3", "running", ev, T0 + timedelta(seconds=secs), typ)  # noqa: E731
    assert at(0) == 2 / 8
    assert at(5) == (2 + 0.5) / 8
    assert at(60) == (2 + progress.CAP) / 8  # stage overran: wait below its end
    assert at(5) < at(9) < at(60)


def test_waiting_for_an_answer_holds_and_done_is_full():
    typ = {s: 10.0 for s in progress.ORDER}
    ev = [_start("S6", T0)]
    assert progress.fraction("S6", "awaiting_human", ev, T0 + timedelta(seconds=99), typ) == 5 / 8
    assert progress.fraction("done", "done", ev, T0, typ) == 1.0
