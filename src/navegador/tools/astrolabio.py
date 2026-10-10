"""Astrolábio: sun detection, sun-position checks, north offset (SPEC §9.2). TODO(P0b · B).

Uses pvlib.solarposition.get_solarposition and timezonefinder.TimezoneFinder.
"""

from __future__ import annotations

from pathlib import Path

from navegador.schemas import SunObs


def detect(pano_path: Path) -> SunObs | None:
    """Downscale to 1520×760, upper half, threshold near-saturated luminance, largest compact component."""
    raise NotImplementedError("P0b · B")


def sun_check(lat: float, lng: float, timestamp: str, sun_yaw: float | None = None,
              sun_elev: float | None = None) -> dict:
    """Return {expected_elev, expected_azimuth, elev_residual, north_offset_yaw}.
    Timestamp is local (flags.timestamp_is_local, [CONFIRM]) else UTC."""
    raise NotImplementedError("P0b · B")


def band(timestamp: str, sun_elev: float, tol: float = 2.0) -> list[tuple[float, float]]:
    """(P2) 0.25° grid points over the regions consistent with the observed sun elevation."""
    raise NotImplementedError("P2")
