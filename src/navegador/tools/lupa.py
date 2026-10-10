"""Lupa: cuts the panorama into perspective views and zoom crops (SPEC §9.1). TODO(P0b · B).

py360convert.e2p(img, fov_deg, u_deg, v_deg, out_hw): u_deg > 0 looks right, v_deg > 0 looks up.
Image yaw: 0 = centre column, positive to the right, range (−180, 180]. NOT compass north.
"""

from __future__ import annotations

from pathlib import Path

from navegador.schemas import View


def pixel_to_angles(x: float, y: float, w: int, h: int) -> tuple[float, float]:
    """yaw = (x/W − 0.5)·360, pitch = (0.5 − y/H)·180."""
    return (x / w - 0.5) * 360.0, (0.5 - y / h) * 180.0


def views(pano_path: Path, out_dir: Path) -> list[View]:
    """h000…h315 (8 horizontal, FOV 90, 1024²), g000/g090/g180/g270 (pitch −50), overview.jpg (2048×1024)."""
    raise NotImplementedError("P0b · B")


def zoom(pano_path: Path, yaw: float, pitch: float, fov: float, size: int = 1024) -> Path:
    """FOV clamped to 8–90. Cached by parameters."""
    raise NotImplementedError("P0b · B")
