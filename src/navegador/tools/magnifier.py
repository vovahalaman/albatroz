"""Magnifier: cuts the panorama into perspective views and zoom crops (SPEC §9.1).

py360convert.e2p(img, fov_deg, u_deg, v_deg, out_hw): u_deg > 0 looks right, v_deg > 0 looks up.
Image yaw: 0 = centre column, positive to the right, range (−180, 180]. NOT compass north.
Render settings (FOV, sizes, pitches, JPEG quality) live in `settings.yaml → magnifier:`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

import numpy as np
import py360convert
from PIL import Image

from navegador.config import MagnifierSettings, cache_dir, settings
from navegador.schemas import View

OVERVIEW_FOV = 360.0  # convention for the overview View, not a render setting
MANIFEST = "views.json"


def _cfg() -> MagnifierSettings:
    return settings().magnifier


def pixel_to_angles(x: float, y: float, w: int, h: int) -> tuple[float, float]:
    """yaw = (x/W − 0.5)·360, pitch = (0.5 − y/H)·180."""
    return (x / w - 0.5) * 360.0, (0.5 - y / h) * 180.0


def wrap_yaw(deg: float) -> float:
    """Map any angle to (−180, 180]."""
    y = (deg + 180.0) % 360.0 - 180.0
    return 180.0 if y == -180.0 else y


def clamp_fov(fov: float) -> float:
    lo, hi = _cfg().zoom_fov
    return min(max(fov, lo), hi)


def clamp_pitch(pitch: float) -> float:
    return min(max(pitch, -90.0), 90.0)


def clamp_size(size: int) -> int:
    lo, hi = _cfg().zoom_size
    return min(max(int(size), lo), hi)


def zoom_params(yaw: float, pitch: float, fov: float, size: int) -> tuple[float, float, float, int]:
    """Validate and normalise zoom parameters (they come from model output): finite numbers only,
    yaw wrapped to (−180, 180], pitch clamped to ±90, FOV and size to `magnifier.zoom_fov/zoom_size`."""
    raw = {"yaw": yaw, "pitch": pitch, "fov": fov, "size": size}
    bad = [k for k, v in raw.items() if not math.isfinite(v)]
    if bad:
        raise ValueError(f"zoom parameters must be finite numbers, got {raw}")
    return wrap_yaw(float(yaw)), clamp_pitch(float(pitch)), clamp_fov(float(fov)), clamp_size(size)


# ---------------------------------------------------------------- pano I/O


@lru_cache(maxsize=2)
def _decode(path: str, mtime_ns: int) -> np.ndarray:
    """Decode once per (path, mtime); ~55 MB per 6080×3040 pano, so keep only two."""
    with Image.open(path) as im:
        arr = np.asarray(im.convert("RGB"))
    arr.setflags(write=False)
    return arr


def _load(pano_path: Path) -> np.ndarray:
    p = Path(pano_path).resolve()
    return _decode(str(p), p.stat().st_mtime_ns)


@lru_cache(maxsize=64)
def _content_hash(path: str, mtime_ns: int, size: int) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def pano_hash(pano_path: Path) -> str:
    """Hash of the file content, so a moved or copied pano keeps its cache. Hashed once per (path, mtime)."""
    p = Path(pano_path).resolve()
    st = p.stat()
    return _content_hash(str(p), st.st_mtime_ns, st.st_size)


def _render(img: np.ndarray, fov: float, yaw: float, pitch: float, size: int) -> np.ndarray:
    out = py360convert.e2p(img, fov_deg=fov, u_deg=yaw, v_deg=pitch, out_hw=(size, size))
    return np.clip(out, 0, 255).astype(np.uint8)


def _atomic_write(path: Path, write: Callable[[Path], None]) -> Path:
    """Write via a temp file so a crashed run never leaves a half-written file behind.

    The temp name is unique per process and thread, so concurrent writers of the same file (two
    Streamlit tabs, or a rerun while the old run still renders) never share one temp file.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        write(tmp)
        try:
            os.replace(tmp, path)
        except PermissionError:
            # Windows: another writer is replacing the same file right now; its content is identical.
            if not path.exists():
                raise
    finally:
        tmp.unlink(missing_ok=True)
    return path


def _save(arr: np.ndarray, path: Path) -> Path:
    quality = _cfg().jpeg_quality
    return _atomic_write(path, lambda tmp: Image.fromarray(arr).save(tmp, format="JPEG", quality=quality))


# ---------------------------------------------------------------- views


def _view_specs(cfg: MagnifierSettings) -> list[tuple[str, float, float]]:
    specs = [(f"h{a:03d}", wrap_yaw(float(a)), 0.0) for a in range(0, 360, 45)]
    specs += [(f"g{a:03d}", wrap_yaw(float(a)), cfg.ground_pitch) for a in range(0, 360, 90)]
    specs += [(f"u{a:03d}", wrap_yaw(float(a)), cfg.up_pitch) for a in range(0, 360, 90)]
    return specs


def _views_are_current(out_dir: Path, key: dict, names: list[str]) -> bool:
    try:
        manifest = json.loads((out_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return manifest.get("key") == key and all((out_dir / f"{n}.jpg").exists() for n in names)


def views(pano_path: Path, out_dir: Path) -> list[View]:
    """h000…h315 (8 horizontal), g000…g270 (ground), u000…u270 (up), overview.jpg.

    u-views are an addition to SPEC §9.1 so that the views cover the full sphere. Returns 17 Views:
    8 h, 4 g, 4 u, then the overview (yaw 0, pitch 0, fov 360). If `out_dir` already holds views of
    the same pano content with the same settings (`views.json`), they are reused without rendering.
    """
    cfg = _cfg()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    specs = _view_specs(cfg)
    result = [View(name=n, yaw=y, pitch=p, fov=cfg.view_fov, path=out_dir / f"{n}.jpg") for n, y, p in specs]
    result.append(View(name="overview", yaw=0.0, pitch=0.0, fov=OVERVIEW_FOV, path=out_dir / "overview.jpg"))
    key = {"pano": pano_hash(pano_path), "settings": cfg.model_dump(mode="json")}
    if _views_are_current(out_dir, key, [v.name for v in result]):
        return result

    img = _load(pano_path)
    for view in result[:-1]:
        _save(_render(img, cfg.view_fov, view.yaw, view.pitch, cfg.view_size), view.path)
    overview = np.asarray(Image.fromarray(img).resize(tuple(cfg.overview_wh), Image.Resampling.LANCZOS))
    _save(overview, result[-1].path)
    manifest = json.dumps({"key": key, "views": [v.name for v in result]}, indent=2)
    _atomic_write(out_dir / MANIFEST, lambda tmp: tmp.write_text(manifest, encoding="utf-8"))
    return result


# ---------------------------------------------------------------- zoom


def zoom_cache_path(pano_path: Path, yaw: float, pitch: float, fov: float, size: int = 1024) -> Path:
    """Cache key = pano content hash + normalised parameters (see `zoom_params`) + JPEG quality."""
    yaw, pitch, fov, size = zoom_params(yaw, pitch, fov, size)
    pano = pano_hash(pano_path)
    key = f"{pano}|{yaw:.3f}|{pitch:.3f}|{fov:.3f}|{size}|q{_cfg().jpeg_quality}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:16]
    return cache_dir() / "zoom" / f"{pano}_{digest}.jpg"


def zoom(pano_path: Path, yaw: float, pitch: float, fov: float, size: int = 1024) -> Path:
    """FOV clamped to 8–90, pitch to ±90, size to 64–2048 (settings); non-finite input raises
    ValueError. Cached by the pano content and the normalised parameters."""
    out = zoom_cache_path(pano_path, yaw, pitch, fov, size)
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    yaw, pitch, fov, size = zoom_params(yaw, pitch, fov, size)
    return _save(_render(_load(pano_path), fov, yaw, pitch, size), out)
