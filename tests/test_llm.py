"""Offline tests for llm.py. The live check is `uv run python cli.py check-llm`."""

import pytest
from pydantic import BaseModel

from navegador import llm


class Out(BaseModel):
    x: int


def test_load_prompt_appends_knowledge():
    p = llm.load_prompt("baseline.md")
    assert p.knowledge == ("regional_cues.md", "confusion_pairs.md")
    assert "expert geolocation analyst" in p.system
    assert "# Knowledge file: regional_cues.md" in p.system
    assert not p.system.startswith("---")


def test_build_config_schema_and_tools_exclusive():
    cfg = llm.build_config(None, "baseline", Out, None, None)
    assert cfg.response_mime_type == "application/json" and cfg.response_json_schema["type"] == "object"

    def tool(a: int) -> int:
        """doc"""
        return a

    cfg = llm.build_config(None, "detetive", None, [tool], 3)
    assert cfg.automatic_function_calling.maximum_remote_calls == 3
    with pytest.raises(ValueError):
        llm.build_config(None, "detetive", Out, [tool], 3)


def test_image_part_logs_hash_not_bytes(tmp_path):
    from PIL import Image

    img = tmp_path / "a.jpg"
    Image.new("RGB", (8, 8)).save(img)
    part, log = llm.image_part(img, "views")
    assert log["media_resolution"] == "MEDIUM" and len(log["sha256"]) == 64
    assert part.media_resolution.level.value == "MEDIA_RESOLUTION_MEDIUM"


def test_unset_model_raises():
    with pytest.raises(llm.LLMConfigError):
        llm.model_for("perception")


def test_pick_flash():
    names = ["publishers/google/models/gemini-2.5-flash", "publishers/google/models/gemini-2.5-flash-lite",
             "publishers/google/models/gemini-3-flash", "publishers/google/models/gemini-2.5-flash-image"]
    assert llm.pick_flash(names) == "gemini-3-flash"
