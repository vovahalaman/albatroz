"""Detects the panorama label (urban / non_urban) before a run, so the operator does not choose it.

One llm.call with the cheapest configured model (flash-lite) on a low-resolution overview only. Results are
cached on disk (tools/cache) by image content, model and prompt, so the same panorama is never asked twice.
Any failure or a timeout (TIMEOUT_S) falls back to "urban" (source "default"). The prompt lives in
ui/prompts/label_classifier.md (R6: no prompt text in Python).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import BaseModel, Field

from navegador import llm
from navegador.config import PROMPTS_DIR, settings
from navegador.core.state import pano_id_for
from navegador.tools import cache, magnifier

PROMPT = os.path.relpath(Path(__file__).resolve().parent / "prompts" / "label_classifier.md", PROMPTS_DIR)
ROLE = "label_classifier"
TIMEOUT_S = 30.0
OVERVIEW_W = 1024  # px wide; sent at LOW media resolution
DEFAULT = "urban"


class LabelResult(BaseModel):
    label: Literal["urban", "non_urban"]
    reason: str = Field(default="", max_length=300)


@dataclass(frozen=True)
class Detected:
    label: str
    reason: str
    source: str  # "classifier" | "cache" | "default"
    model: str = ""
    tokens: int = 0
    error: str = ""

    def as_log(self) -> dict:
        return asdict(self)


def cheapest_model() -> str:
    """The configured flash-lite model (cheapest), else the navigator's model."""
    models = [m for m in settings().models.values() if m and not m.startswith("<")]
    lite = sorted(m for m in models if "flash-lite" in m) or sorted(m for m in models if "lite" in m)
    return lite[0] if lite else llm.model_for("navigator")


def overview(path: Path) -> Path:
    """A small JPEG of the whole panorama next to the upload (made once per file)."""
    out = path.with_name(f"{path.stem}.overview.jpg")
    if not out.exists() or out.stat().st_mtime < path.stat().st_mtime:
        with Image.open(path) as im:
            im.draft("RGB", (OVERVIEW_W, OVERVIEW_W // 2))
            im.convert("RGB").resize((OVERVIEW_W, OVERVIEW_W // 2), Image.Resampling.LANCZOS).save(out, quality=85)
    return out


def _key(path: Path, model: str) -> dict:
    system = llm.load_prompt(PROMPT).system
    return {"pano": magnifier.pano_hash(path), "model": model,
            "prompt": hashlib.sha256(system.encode("utf-8")).hexdigest()}


async def _ask(path: Path, model: str) -> llm.LLMResult:
    return await llm.call(ROLE, PROMPT, pano_id=pano_id_for(path), text_parts=["Panorama overview (low resolution)."],
                          images=[(overview(path), "LOW")], schema=LabelResult, model=model,
                          timeout_s=TIMEOUT_S, max_attempts=1, max_output_tokens=1024,
                          retry_invalid=False)  # one request at most; invalid JSON → default


def classify(path: Path) -> Detected:
    """Label for one panorama; never raises."""
    path = Path(path)
    try:
        model = cheapest_model()
        key = _key(path, model)
        hit, value = cache.lookup(ROLE, key)
        if hit:
            return Detected(value["label"], value["reason"], "cache", model)
        res = asyncio.run(asyncio.wait_for(_ask(path, model), TIMEOUT_S))
        parsed: LabelResult = res.parsed
        cache.put(ROLE, key, parsed.model_dump())
        return Detected(parsed.label, parsed.reason, "classifier", res.model,
                        int(res.usage.get("total_token_count", 0) or 0))
    except Exception as exc:  # noqa: BLE001 - the label must never block the run
        return Detected(DEFAULT, "Label detection failed; using the default.", "default",
                        error=f"{type(exc).__name__}: {exc}"[:300])
