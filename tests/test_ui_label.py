"""ui/label_classifier.py with a mocked LLM: request shape, cache, failures and timeout fall back to urban."""

import asyncio
import json
from pathlib import Path

import pytest

from navegador import llm
from navegador.config import settings
from tests.ui_fakes import make_pano
from ui import label_classifier as lc


class FakeLLM:
    def __init__(self, answer=None, error=None, delay=0.0):
        self.answer, self.error, self.delay, self.calls = answer, error, delay, []

    async def __call__(self, role, prompt_file, **kw):
        self.calls.append({"role": role, "prompt_file": prompt_file, **kw})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return llm.LLMResult(json.dumps(self.answer), kw["schema"].model_validate(self.answer), [],
                             {"total_token_count": 321}, 0.4, kw["model"])


@pytest.fixture
def pano(tmp_path) -> Path:
    return make_pano(tmp_path)


def test_one_cheap_call_with_only_a_low_res_overview(monkeypatch, pano):
    fake = FakeLLM({"label": "non_urban", "reason": "Open plain with a reservoir."})
    monkeypatch.setattr(llm, "call", fake)
    det = lc.classify(pano)
    assert (det.label, det.reason, det.source, det.tokens) == ("non_urban", "Open plain with a reservoir.", "classifier", 321)
    (call,) = fake.calls
    assert "lite" in call["model"] and call["model"] in settings().models.values()
    (img, level), = call["images"]
    assert level == "LOW" and Path(img).parent.name == "label_overview" and Path(img).suffix == ".jpg"
    assert call["schema"] is lc.LabelResult and call["timeout_s"] == lc.TIMEOUT_S
    assert call["max_attempts"] == 1 and call["retry_invalid"] is False  # exactly one request
    assert call["prompt_file"].endswith("ui/prompts/label_classifier.md")
    assert llm.load_prompt(call["prompt_file"]).system.startswith("You classify one 360° panorama")


def test_result_is_cached_per_image(monkeypatch, pano):
    fake = FakeLLM({"label": "urban", "reason": "Dense streets."})
    monkeypatch.setattr(llm, "call", fake)
    lc.classify(pano)
    again = lc.classify(pano)
    assert len(fake.calls) == 1 and again.source == "cache" and again.label == "urban"


def test_errors_fall_back_to_urban(monkeypatch, pano):
    monkeypatch.setattr(llm, "call", FakeLLM(error=RuntimeError("Vertex 504")))
    det = lc.classify(pano)
    assert (det.label, det.source) == ("urban", "default") and "Vertex 504" in det.error


def test_timeout_falls_back_to_urban(monkeypatch, pano):
    monkeypatch.setattr(lc, "TIMEOUT_S", 0.05)
    monkeypatch.setattr(llm, "call", FakeLLM({"label": "non_urban", "reason": "x"}, delay=1.0))
    det = lc.classify(pano)
    assert (det.label, det.source) == ("urban", "default") and "TimeoutError" in det.error


def test_writes_nothing_outside_cache(monkeypatch, tmp_path):
    """The overview goes to cache/label_overview/ only: never next to the panorama or into data/."""
    from navegador.config import cache_dir

    data = tmp_path / "data" / "train1"
    data.mkdir(parents=True)
    pano = make_pano(data)
    monkeypatch.setattr(llm, "call", FakeLLM({"label": "urban", "reason": "Streets."}))

    def snapshot(root: Path) -> set[Path]:
        return {p for p in root.rglob("*") if p.is_file()}

    cache_root = cache_dir().resolve()
    before = snapshot(tmp_path)
    lc.classify(pano)
    created = {p.resolve() for p in snapshot(tmp_path) - before}
    assert created, "expected the overview and the label result in the cache"
    assert all(cache_root in p.parents for p in created), sorted(map(str, created))
    assert sorted(p.name for p in data.iterdir()) == [pano.name]
    (ov,) = list((cache_root / "label_overview").glob("*.jpg"))
    assert ov.stem == lc.magnifier.pano_hash(pano)
