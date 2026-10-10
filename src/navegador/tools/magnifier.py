"""Magnifier: cuts the panorama into perspective views and zoom crops (SPEC §9.1).

py360convert.e2p(img, fov_deg, u_deg, v_deg, out_hw): u_deg > 0 looks right, v_deg > 0 looks up.
Image yaw: 0 = centre column, positive to the right, range (−180, 180]. NOT compass north.
"""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
import py360convert
from PIL import Image

from navegador.config import cache_dir
from navegador.schemas import View

VIEW_FOV = 90.0
VIEW_SIZE = 1024
GROUND_PITCH = -50.0
UP_PITCH = 50.0
OVERVIEW_WH = (2048, 1024)
OVERVIEW_FOV = 360.0
ZOOM_FOV_MIN, ZOOM_FOV_MAX = 8.0, 90.0
JPEG_QUALITY = 90


def pixel_to_angles(x: float, y: float, w: int, h: int) -> tuple[float, float]:
    """yaw = (x/W − 0.5)·360, pitch = (0.5 − y/H)·180."""
    return (x / w - 0.5) * 360.0, (0.5 - y / h) * 180.0


def wrap_yaw(deg: float) -> float:
    """Map any angle to (−180, 180]."""
    y = (deg + 180.0) % 360.0 - 180.0
    return 180.0 if y == -180.0 else y


def clamp_fov(fov: float) -> float:
    return min(max(fov, ZOOM_FOV_MIN), ZOOM_FOV_MAX)


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


def _render(img: np.ndarray, fov: float, yaw: float, pitch: float, size: int) -> np.ndarray:
    out = py360convert.e2p(img, fov_deg=fov, u_deg=yaw, v_deg=pitch, out_hw=(size, size))
    return np.clip(out, 0, 255).astype(np.uint8)


def _save(arr: np.ndarray, path: Path) -> Path:
    """Write via a temp file so a crashed run never leaves a half-written JPG in the cache."""
    tmp = path.with_name(path.name + ".tmp")
    Image.fromarray(arr).save(tmp, format="JPEG", quality=JPEG_QUALITY)
    os.replace(tmp, path)
    return path


def views(pano_path: Path, out_dir: Path) -> list[View]:
    """h000…h315 (8 horizontal), g000…g270 (pitch −50), u000…u270 (pitch +50), overview.jpg (2048×1024).

    Perspective views are FOV 90, 1024². u-views are an addition to SPEC §9.1 so that the views cover
    the full sphere. Returns 17 Views: 8 h, 4 g, 4 u, then the overview (yaw 0, pitch 0, fov 360).
    """
    img = _load(pano_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    specs = [(f"h{a:03d}", a, 0.0) for a in range(0, 360, 45)]
    specs += [(f"g{a:03d}", a, GROUND_PITCH) for a in range(0, 360, 90)]
    specs += [(f"u{a:03d}", a, UP_PITCH) for a in range(0, 360, 90)]
    result: list[View] = []
    for name, angle, pitch in specs:
        yaw = wrap_yaw(float(angle))
        path = _save(_render(img, VIEW_FOV, yaw, pitch, VIEW_SIZE), out_dir / f"{name}.jpg")
        result.append(View(name=name, yaw=yaw, pitch=pitch, fov=VIEW_FOV, path=path))
    overview = np.asarray(Image.fromarray(img).resize(OVERVIEW_WH, Image.Resampling.LANCZOS))
    path = _save(overview, out_dir / "overview.jpg")
    result.append(View(name="overview", yaw=0.0, pitch=0.0, fov=OVERVIEW_FOV, path=path))
    return result


def zoom_cache_path(pano_path: Path, yaw: float, pitch: float, fov: float, size: int = 1024) -> Path:
    """Cache key = resolved path + mtime + file size + normalised parameters."""
    p = Path(pano_path).resolve()
    st = p.stat()
    key = f"{p}|{st.st_mtime_ns}|{st.st_size}|{wrap_yaw(yaw):.3f}|{pitch:.3f}|{clamp_fov(fov):.3f}|{size}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:16]
    return cache_dir() / "zoom" / f"{p.stem}_{digest}.jpg"


def zoom(pano_path: Path, yaw: float, pitch: float, fov: float, size: int = 1024) -> Path:
    """FOV clamped to 8–90. Cached by parameters."""
    out = zoom_cache_path(pano_path, yaw, pitch, fov, size)
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    arr = _render(_load(pano_path), clamp_fov(fov), wrap_yaw(yaw), pitch, size)
    return _save(arr, out)
