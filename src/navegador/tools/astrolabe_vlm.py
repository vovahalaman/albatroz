"""Astrolabe Gemini fallback and verifier (models.perception through llm.py, R5).

- detect() found a sun → verify(): the VLM is shown the sky with the point circled; "no" vetoes it.
- detect() found nothing → locate(): the VLM may point at the sun; the point is accepted only if it is
  locally bright and between min_elev_deg and the date ceiling. Accepted results are low confidence.

Cost safeguards (all from settings.yaml → astrolabe): disk cache keyed by image hash, at most
vlm_max_calls API calls per process, sky image ≤ 1520×380, max_output_tokens / timeout / attempts
limits, and no call at all if the configured model is a "pro" model.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from typing import Any

import numpy as np
from PIL import Image, ImageDraw
from pydantic import BaseModel

from navegador import llm
from navegador.config import AstrolabeSettings, cache_dir, settings
from navegador.core import scribe
from navegador.schemas import SunLocateResult, SunObs, SunVerifyResult
from navegador.tools.astrolabe import Blob, annulus_mean, luminance, max_sun_elev, pixel_to_angles

log = logging.getLogger(__name__)
MAX_IMAGE = (1520, 380)  # never send more than the downscaled upper half
ROLE = "perception"
_stats: dict[str, int] = {"calls": 0, "cache_hits": 0, "tokens": 0, "capped": 0, "refused": 0, "errors": 0}


def _cfg() -> AstrolabeSettings:
    return settings().astrolabe


def vlm_stats() -> dict[str, int]:
    return dict(_stats)


def reset_vlm_stats() -> None:
    for k in _stats:
        _stats[k] = 0


def sky_image(rgb: np.ndarray) -> Image.Image:
    """Upper half of the detection image, shrunk to fit MAX_IMAGE."""
    img = Image.fromarray(rgb[: rgb.shape[0] // 2])
    img.thumbnail(MAX_IMAGE)
    return img


def _ask(img: Image.Image, data: str, prompt_file: str, schema: type[BaseModel], pano_id: str) -> Any | None:
    """One cached, capped VLM query; `data` is the only text sent (no instructions, R6). None if refused,
    capped or failed."""
    cfg = _cfg()
    model = llm.model_for(ROLE)
    if "pro" in model.lower():
        _stats["refused"] += 1
        log.warning("astrolabe VLM: model %r looks like a pro model; refusing to call it", model)
        return None
    key = hashlib.sha256(np.asarray(img).tobytes() + f"|{img.size}|{data}|{model}|{prompt_file}".encode()).hexdigest()
    path = cache_dir() / "astrolabe_vlm" / f"{key[:32]}.json"
    gen = {"max_output_tokens": cfg.vlm_max_output_tokens, "thinking_level": cfg.vlm_thinking_level}
    if path.exists():  # invalid answers are cached too, but re-asked once the generation settings change
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry["parsed"] is not None or entry.get("gen") == gen:
            _stats["cache_hits"] += 1
            return schema.model_validate(entry["parsed"]) if entry["parsed"] is not None else None
    if _stats["calls"] >= cfg.vlm_max_calls:
        if not _stats["capped"]:
            log.warning("astrolabe VLM: call cap of %d reached for this process; no further calls", cfg.vlm_max_calls)
            scribe.log_event(pano_id, "error", "astrolabe", {"where": "vlm", "error": "vlm_max_calls reached"})
        _stats["capped"] += 1
        return None
    _stats["calls"] += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    jpg = path.with_suffix(".jpg")
    img.save(jpg, quality=90)
    try:
        res = llm.call_sync(ROLE, prompt_file, pano_id=pano_id, text_parts=[data],
                            images=[(jpg, cfg.vlm_media_resolution)], schema=schema,
                            max_output_tokens=cfg.vlm_max_output_tokens, timeout_s=cfg.vlm_timeout_s,
                            max_attempts=cfg.vlm_max_attempts, retry_invalid=False,
                            thinking_level=cfg.vlm_thinking_level)
    except Exception as exc:  # noqa: BLE001 - no response (transport/auth); the VLM is optional
        _stats["errors"] += 1
        log.warning("astrolabe VLM call failed: %s: %s", type(exc).__name__, exc)
        return None
    _stats["tokens"] += int(res.usage.get("total_token_count", 0) or 0)
    if res.parsed is None:
        _stats["errors"] += 1
        log.warning("astrolabe VLM: invalid JSON (cached as no answer): %r", res.text[:80])
    path.write_text(json.dumps({"parsed": res.parsed.model_dump(mode="json") if res.parsed else None,
                                "text": res.text[:500], "model": res.model, "usage": res.usage, "gen": gen}),
                    encoding="utf-8")
    return res.parsed


def verify(rgb: np.ndarray, best: Blob, pano_id: str) -> bool | None:
    """Ask whether the sun is inside a circle drawn at the detected point. None = no answer."""
    img = sky_image(rgb)
    s = img.width / rgb.shape[1]
    r = max(best.core_radius_deg, 3.0) * rgb.shape[0] / 180.0 * s
    x, y = best.x * s, best.y * s
    ImageDraw.Draw(img).ellipse((x - r, y - r, x + r, y + r), outline=(255, 0, 0), width=3)
    data = (f"Image {img.width}x{img.height} px. Red circle centre: "
            f"x={round(1000 * x / img.width)}, y={round(1000 * y / img.height)} (0-1000).")
    res = _ask(img, data, "sun_verify.md", SunVerifyResult, pano_id)
    return None if res is None else bool(res.sun_at_point)


def check_point(rgb: np.ndarray, x: float, y: float, day: date | None) -> str | None:
    """Reject reason for a VLM point (detection-image pixels), or None if it is acceptable."""
    cfg, (h, w) = _cfg(), rgb.shape[:2]
    _, elev = pixel_to_angles(x, y, w, h)
    if elev < cfg.min_elev_deg:
        return "below_min_elev"
    if day is not None and elev > max_sun_elev(day) + cfg.tilt_margin_deg:
        return "above_ceiling"
    lum = luminance(rgb)
    disc = annulus_mean(lum, x, y, elev, 0.0, cfg.vlm_bright_radius_deg)
    ring = annulus_mean(lum, x, y, elev, cfg.vlm_bright_radius_deg, cfg.vlm_ring_outer_deg)
    if disc is None or ring is None or disc < cfg.vlm_min_lum:
        return "not_bright"
    if disc < cfg.core_min and disc - ring < cfg.vlm_min_ring_delta:  # saturated glare counts as bright
        return "not_bright"
    return None


def locate(rgb: np.ndarray, orig: tuple[int, int], day: date | None, pano_id: str) -> tuple[SunObs | None, str]:
    """Ask the VLM where the sun is; validate the point. Returns (obs or None, reason)."""
    img = sky_image(rgb)
    res = _ask(img, f"Image {img.width}x{img.height} px.", "sun_locate.md", SunLocateResult, pano_id)
    if res is None:
        return None, "vlm_skipped"
    if not res.sun_visible or res.x is None or res.y is None:
        return None, "vlm_no_sun"
    h, w = rgb.shape[:2]
    x, y = res.x / 1000 * w, res.y / 1000 * (h // 2)
    reason = check_point(rgb, x, y, day)
    if reason:
        return None, f"vlm_rej:{reason}"
    yaw, elev = pixel_to_angles(x, y, w, h)
    cfg = _cfg()
    return SunObs(yaw=round(yaw, 2), elevation=round(elev, 2), area_px=0, confidence=cfg.vlm_confidence_cap,
                  uncertainty_deg=cfg.vlm_uncertainty_deg, source="vlm",
                  pixel_xy=(round(x * orig[0] / w, 1), round(y * orig[1] / h, 1))), "vlm"


def review(rgb: np.ndarray, orig: tuple[int, int], day: date | None, best: Blob | None, obs: SunObs | None,
           why: str, pano_id: str) -> tuple[SunObs | None, str]:
    """Verify a detected sun, or try the fallback when there is none."""
    if best is not None and obs is not None:
        answer = verify(rgb, best, pano_id)
        if answer is False:
            return None, "vlm_veto"
        return obs, "ok+vlm" if answer else why
    found, reason = locate(rgb, orig, day, pano_id)
    return found, reason
