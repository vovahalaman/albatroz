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
