"""Astrolabe Gemini fallback/verifier. The LLM is always mocked (conftest also blocks real requests)."""

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from navegador import llm
from navegador.config import settings
from navegador.tools import astrolabe_vlm
from navegador.tools.astrolabe import analyse, angles_to_pixel, detect

SKY, GROUND, WHITE = (90, 140, 220), (110, 90, 60), (255, 255, 255)
NAME = "IMG_20261010_140000_00_001.jpg"  # 10 Oct: date ceiling at 27.5°N ≈ 55.8° (+3° tilt)


def _pano(w: int = 3040, h: int = 1520) -> np.ndarray:
    img = np.empty((h, w, 3), dtype=np.uint8)
    img[: h // 2], img[h // 2:] = SKY, GROUND
    return img


def _disc(img: np.ndarray, yaw: float, elev: float, radius_deg: float) -> np.ndarray:
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    lon, lat = np.radians(((xx + 0.5) / w - 0.5) * 360.0), np.radians((0.5 - (yy + 0.5) / h) * 180.0)
    lon0, lat0 = np.radians(yaw), np.radians(elev)
    cosd = np.sin(lat) * np.sin(lat0) + np.cos(lat) * np.cos(lat0) * np.cos(lon - lon0)
    img[cosd >= np.cos(np.radians(radius_deg))] = WHITE
    return img


def _save(img: np.ndarray, tmp_path: Path, name: str = NAME) -> Path:
    path = tmp_path / name
    Image.fromarray(img).save(path, quality=95)
    return path


def _point(yaw: float, elev: float) -> dict[str, int]:
    """VLM coordinates (0–1000 over the 1520×380 sky image) of an angle."""
    x, y = angles_to_pixel(yaw, elev, 1520, 760)
    return {"x": round(1000 * x / 1520), "y": round(1000 * y / 380)}


class FakeLLM:
    def __init__(self) -> None:
        self.answers: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, role: str, prompt_file: str, **kw: Any) -> llm.LLMResult:
        self.calls.append({"role": role, "prompt_file": prompt_file, **kw})
        answer = self.answers[prompt_file]
        if answer == "INVALID":  # truncated JSON: llm.call(retry_invalid=False) returns parsed=None
            return llm.LLMResult('{"sun_visible": true, "x": 4', None, [], {"total_token_count": 200}, 0.0, "fake")
        return llm.LLMResult(json.dumps(answer), kw["schema"].model_validate(answer), [],
                             {"total_token_count": 100}, 0.0, "fake-flash")


@pytest.fixture
def fake(monkeypatch) -> FakeLLM:
    f = FakeLLM()
    monkeypatch.setattr(llm, "call_sync", f)
    monkeypatch.setattr(settings().astrolabe, "vlm_fallback", True)
    monkeypatch.setitem(settings().models, "perception", "gemini-test-flash")
    astrolabe_vlm.reset_vlm_stats()
    return f


def test_fallback_accepts_bright_point(fake: FakeLLM, tmp_path: Path):
    fake.answers["sun_locate.md"] = {"sun_visible": True, "kind": "behind_cloud", **_point(40.0, 35.0)}
    path = _save(_disc(_pano(), 40.0, 35.0, 2.0), tmp_path)  # 2° disc: too small for detect (weak_core)
    sun = detect(path)
    assert sun is not None and sun.source == "vlm"
    assert sun.yaw == pytest.approx(40.0, abs=1.0) and sun.elevation == pytest.approx(35.0, abs=1.0)
    assert sun.confidence == 0.3 and sun.uncertainty_deg == 8
    assert astrolabe_vlm.vlm_stats()["calls"] == 1 and astrolabe_vlm.vlm_stats()["tokens"] == 100


@pytest.mark.parametrize(("yaw", "elev", "disc", "reason"), [
    (-100.0, 30.0, False, "not_bright"),    # VLM points at plain blue sky
    (40.0, 70.0, True, "above_ceiling"),    # impossible sun height for 10 October
    (40.0, 1.5, True, "below_min_elev"),    # horizon object
])
def test_fallback_rejects_invalid_points(fake: FakeLLM, tmp_path: Path, yaw, elev, disc, reason):
    img = _disc(_pano(), yaw, elev, 2.0) if disc else _pano()
    fake.answers["sun_locate.md"] = {"sun_visible": True, "kind": "disc", **_point(yaw, elev)}
    a = analyse(_save(img, tmp_path))
    assert a.obs is None and a.why == f"vlm_rej:{reason}"


def test_fallback_accepts_point_inside_saturated_glare(fake: FakeLLM, tmp_path: Path):
    img = _pano()
    img[560:700, 600:1600] = WHITE  # wide saturated glare band: rejected by detect (too_extended), ring bright
    yaw, elev = (1100.5 / 3040 - 0.5) * 360, (0.5 - 630.5 / 1520) * 180
    fake.answers["sun_locate.md"] = {"sun_visible": True, "kind": "behind_object", **_point(yaw, elev)}
    a = analyse(_save(img, tmp_path))
    assert a.best is None, "glare must not pass detect for this test"
    assert a.obs is not None and a.obs.source == "vlm", a.why


def test_fallback_no_sun(fake: FakeLLM, tmp_path: Path):
    fake.answers["sun_locate.md"] = {"sun_visible": False}
    a = analyse(_save(_pano(), tmp_path))
    assert a.obs is None and a.why == "vlm_no_sun"


def test_verifier_veto(fake: FakeLLM, tmp_path: Path):
    fake.answers["sun_verify.md"] = {"sun_at_point": False}
    a = analyse(_save(_disc(_pano(), 40.0, 35.0, 8.0), tmp_path))
    assert a.best is not None and a.obs is None and a.why == "vlm_veto"
    assert [c["prompt_file"] for c in fake.calls] == ["sun_verify.md"]


def test_verifier_accept_keeps_detection(fake: FakeLLM, tmp_path: Path):
    fake.answers["sun_verify.md"] = {"sun_at_point": True}
    a = analyse(_save(_disc(_pano(), 40.0, 35.0, 8.0), tmp_path))
    assert a.obs is not None and a.obs.source == "detect" and a.why == "ok+vlm"


def test_cache_prevents_second_call(fake: FakeLLM, tmp_path: Path):
    fake.answers["sun_locate.md"] = {"sun_visible": False}
    path = _save(_pano(), tmp_path)
    detect(path)
    detect(path)
    assert len(fake.calls) == 1
    assert astrolabe_vlm.vlm_stats()["cache_hits"] == 1


def test_invalid_answer_is_cached_too(fake: FakeLLM, tmp_path: Path):
    fake.answers["sun_locate.md"] = "INVALID"
    path = _save(_pano(), tmp_path)
    assert analyse(path).why == "vlm_skipped"
    assert analyse(path).why == "vlm_skipped"
    assert len(fake.calls) == 1
    stats = astrolabe_vlm.vlm_stats()
    assert stats["cache_hits"] == 1 and stats["errors"] == 1 and stats["tokens"] == 200


def test_invalid_answer_is_retried_after_settings_change(fake: FakeLLM, tmp_path: Path, monkeypatch):
    fake.answers["sun_locate.md"] = "INVALID"
    path = _save(_pano(), tmp_path)
    analyse(path)
    monkeypatch.setattr(settings().astrolabe, "vlm_max_output_tokens", 400)
    fake.answers["sun_locate.md"] = {"sun_visible": False}
    assert analyse(path).why == "vlm_no_sun"
    assert len(fake.calls) == 2


def test_call_cap(fake: FakeLLM, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings().astrolabe, "vlm_max_calls", 1)
    fake.answers["sun_locate.md"] = {"sun_visible": False}
    detect(_save(_disc(_pano(), 10.0, 20.0, 2.0), tmp_path, "IMG_20261010_140000_00_002.jpg"))
    detect(_save(_disc(_pano(), 90.0, 20.0, 2.0), tmp_path, "IMG_20261010_140000_00_003.jpg"))
    assert len(fake.calls) == 1
    assert astrolabe_vlm.vlm_stats()["capped"] == 1


def test_pro_model_is_refused(fake: FakeLLM, tmp_path: Path, monkeypatch):
    monkeypatch.setitem(settings().models, "perception", "gemini-2.5-pro")
    a = analyse(_save(_pano(), tmp_path))
    assert a.obs is None and a.why == "vlm_skipped"
    assert fake.calls == [] and astrolabe_vlm.vlm_stats()["refused"] == 1


def test_request_limits_and_image_size(fake: FakeLLM, tmp_path: Path):
    fake.answers["sun_locate.md"] = {"sun_visible": False}
    detect(_save(_pano(), tmp_path))
    call = fake.calls[0]
    assert call["role"] == "perception"
    assert (call["max_output_tokens"], call["timeout_s"], call["max_attempts"]) == (200, 30, 2)
    assert call["retry_invalid"] is False and call["thinking_level"] == "LOW"
    (img_path, _), = call["images"]
    with Image.open(img_path) as sent:
        assert sent.size[0] <= 1520 and sent.size[1] <= 380


def test_vlm_disabled_makes_no_call(fake: FakeLLM, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings().astrolabe, "vlm_fallback", False)
    assert detect(_save(_pano(), tmp_path)) is None
    assert fake.calls == []


def test_llm_config_carries_limits():
    cfg = llm.build_config(None, "perception", None, None, None, max_output_tokens=200, timeout_s=30)
    assert cfg.max_output_tokens == 200 and cfg.http_options.timeout == 30_000


def test_unmocked_llm_never_reaches_the_api(monkeypatch):
    monkeypatch.setitem(settings().models, "perception", "gemini-test-flash")
    with pytest.raises((AssertionError, llm.LLMConfigError)):
        llm.call_sync("perception", "sun_verify.md", pano_id="_t", text_parts=["x"])
