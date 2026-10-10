from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from navegador.schemas import (MCQ, BaselineResult, Candidate, Cue, GeographerResult, Hypothesis, ReaderResult,
                               MCQOption, Submission)

NOW = datetime.now(timezone.utc)
H = dict(lat=38.7, lng=-9.1, radius_m=100, weight=0.5, label="x", source="detective")


def test_hypothesis_provenance_registry_r2():
    reg = {"geocode-abc"}
    Hypothesis.model_validate({**H, "provenance": "geocode-abc"}, context={"registry": reg})
    Hypothesis.model_validate({**H, "provenance": "estimated"}, context={"registry": reg})
    with pytest.raises(ValidationError, match="R2"):
        Hypothesis.model_validate({**H, "provenance": "from-memory"}, context={"registry": reg})


def test_hypothesis_bounds():
    with pytest.raises(ValidationError):
        Hypothesis(**{**H, "radius_m": 0}, provenance="estimated")
    with pytest.raises(ValidationError):
        Hypothesis(**{**H, "weight": -1}, provenance="estimated")


def test_mcq_option_count():
    opt = MCQOption(id="A", text="a")
    MCQ(mcq_id="m", question="q", options=[opt, opt], asked_at=NOW)
    with pytest.raises(ValidationError):
        MCQ(mcq_id="m", question="q", options=[opt], asked_at=NOW)
    with pytest.raises(ValidationError):
        MCQ(mcq_id="m", question="q", options=[opt] * 5, asked_at=NOW)


def test_cue_length():
    Cue(text="x" * 1000, ts=NOW, guard_flag=False, override=False)
    with pytest.raises(ValidationError):
        Cue(text="x" * 1001, ts=NOW, guard_flag=False, override=False)


def test_submission_exactly_ten():
    base = dict(pano_id="p", filename="p.jpg", label="urban", mode="baseline", penalty_factor=1,
                expected_score=0, special_one=False)
    cands = [Candidate(rank=i + 1, lat=38.7, lng=-9.1) for i in range(10)]
    Submission(**base, candidates=cands)
    with pytest.raises(ValidationError):
        Submission(**base, candidates=cands[:9])


@pytest.mark.parametrize("schema", [BaselineResult, ReaderResult, GeographerResult])
def test_agent_schemas_produce_json_schema(schema):
    js = schema.model_json_schema()
    assert js["type"] == "object"


def test_agent_example_parses():
    BaselineResult.model_validate_json(
        '{"country_probs":{"pt":0.8,"es":0.2,"fr":0},'
        '"places":[{"query":"Monsaraz, Évora","country":"pt","confidence":0.4,"why":"whitewash"}],"clues":["x"]}')


def test_zoom_request_corrects_out_of_range_numbers():
    from navegador.schemas import ZoomRequest

    z = ZoomRequest(yaw=270, pitch=100, fov=3)
    assert (z.yaw, z.pitch, z.fov) == (-90.0, 90.0, 8.0)
    z = ZoomRequest(yaw=-180, pitch=-120, fov=120)
    assert (z.yaw, z.pitch, z.fov) == (180.0, -90.0, 90.0)
    reader = ReaderResult.model_validate(
        {"language_verdict": {}, "zoom_requests": [{"yaw": 400, "pitch": 10, "fov": 5, "why": "sign"}]})
    assert reader.zoom_requests[0].fov == 8.0  # one bad number no longer fails the whole answer
    for bad in ({"yaw": float("nan"), "pitch": 0, "fov": 20}, {"yaw": 0, "pitch": float("inf"), "fov": 20},
                {"yaw": "left", "pitch": 0, "fov": 20}):
        with pytest.raises(ValidationError):
            ZoomRequest(**bad)
    props = ZoomRequest.model_json_schema()["properties"]  # the model still sees the bounds
    assert (props["fov"]["minimum"], props["fov"]["maximum"]) == (8, 90)
    assert (props["pitch"]["minimum"], props["pitch"]["maximum"]) == (-90, 90)
