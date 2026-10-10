"""Astrolabe: sun detection, sun-position checks, north offset (SPEC §9.2).

Uses pvlib.solarposition.get_solarposition and timezonefinder.TimezoneFinder.
The camera clock is unreliable (local, UTC or Lisbon time, also in ES/FR), so sun_check
evaluates every entry of `astrolabe.time_hypotheses` and ignores flags.timestamp_is_local.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pvlib
from timezonefinder import TimezoneFinder

from navegador.config import AstrolabeSettings, settings
from navegador.core.state import timestamp_dt
from navegador.schemas import SunObs


def _cfg() -> AstrolabeSettings:
    return settings().astrolabe


def wrap180(deg: float) -> float:
    """Map an angle to (−180, 180]."""
    d = (deg + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d


def angles_to_pixel(yaw: float, pitch: float, w: int, h: int) -> tuple[float, float]:
    """Inverse of magnifier.pixel_to_angles: x = (yaw/360 + 0.5)·W, y = (0.5 − pitch/180)·H."""
    return (wrap180(yaw) / 360.0 + 0.5) * w, (0.5 - pitch / 180.0) * h


# ---------------------------------------------------------------- detection


def detect(pano_path: Path) -> SunObs | None:
    """Downscale to 1520×760, upper half, threshold near-saturated luminance, largest compact component."""
    raise NotImplementedError("P0b · B")


# ---------------------------------------------------------------- sun position


@lru_cache
def _tzf() -> TimezoneFinder:
    return TimezoneFinder()


def _local_tz(lat: float, lng: float) -> str | None:
    return _tzf().timezone_at(lat=lat, lng=lng)


def _resolve_times(lat: float, lng: float, naive: datetime) -> list[tuple[list[str], datetime]]:
    """One (names, utc instant) per distinct instant; e.g. local == Europe/Lisbon in PT is merged."""
    out: dict[datetime, list[str]] = {}
    for name in _cfg().time_hypotheses:
        tz = _local_tz(lat, lng) if name == "local" else name
        if tz is None:
            continue
        utc = naive.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)
        out.setdefault(utc, []).append(f"local ({tz})" if name == "local" else name)
    return [(names, utc) for utc, names in out.items()]


def sun_check(lat: float, lng: float, timestamp: str, sun_yaw: float | None = None,
              sun_elev: float | None = None, uncertainty_deg: float | None = None) -> dict[str, Any]:
    """Check a candidate location against the observed sun under every clock hypothesis.

    Args:
        lat, lng: candidate location (degrees).
        timestamp: camera clock as YYYYMMDDhhmmss (time zone unknown).
        sun_yaw: observed sun image yaw (0 = image centre, + right), if any.
        sun_elev: observed sun elevation in degrees, if any.
        uncertainty_deg: elevation uncertainty; defaults to astrolabe.tilt_margin_deg.

    Returns a dict with `hypotheses` (per clock: expected elevation/azimuth, above_horizon and,
    if a sun was observed, elevation_residual_deg and north_yaw_deg = image yaw pointing north),
    `best` (smallest residual), `consistency` (0–1, high only if best residual ≤ uncertainty)
    and `ambiguous` (more than one clock fits; azimuth cannot decide because heading is unknown).
    """
    cfg = _cfg()
    naive = timestamp_dt(timestamp)
    times = _resolve_times(lat, lng, naive)
    pos = pvlib.solarposition.get_solarposition(pd.DatetimeIndex([t for _, t in times]), lat, lng)
    u = cfg.tilt_margin_deg if uncertainty_deg is None else uncertainty_deg
    hyps: list[dict[str, Any]] = []
    for (names, utc), (_, row) in zip(times, pos.iterrows(), strict=True):
        h: dict[str, Any] = {
            "clock": names,
            "utc": utc.isoformat(),
            "expected_elev_deg": round(float(row["apparent_elevation"]), 2),
            "expected_azimuth_deg": round(float(row["azimuth"]), 2),
            "above_horizon": bool(row["apparent_elevation"] > 0),
        }
        if sun_elev is not None:
            h["elevation_residual_deg"] = round(abs(sun_elev - float(row["apparent_elevation"])), 2)
        if sun_yaw is not None:
            h["north_yaw_deg"] = round(wrap180(sun_yaw - float(row["azimuth"])), 2)
        hyps.append(h)

    result: dict[str, Any] = {
        "lat": lat, "lng": lng, "timestamp": naive.strftime("%Y%m%d%H%M%S"),
        "sun_observed": sun_elev is not None,
        "any_above_horizon": any(h["above_horizon"] for h in hyps),
        "hypotheses": hyps, "best": None, "consistency": None, "ambiguous": False,
    }
    if sun_elev is None or not hyps:
        return result
    best = min(hyps, key=lambda h: h["elevation_residual_deg"])
    r = best["elevation_residual_deg"]
    consistency = 0.0
    if best["above_horizon"]:
        consistency = min(1.0, max(0.0, 1.0 - max(0.0, r - u) / cfg.consistency_falloff_deg))
    fits = [h for h in hyps if h["above_horizon"] and h["elevation_residual_deg"] <= u]
    result.update(best=best, consistency=round(consistency, 3), ambiguous=len(fits) > 1,
                  uncertainty_deg=u)
    return result


def band(timestamp: str, sun_elev: float, tol: float = 2.0) -> list[tuple[float, float]]:
    """(P2) 0.25° grid points over the regions consistent with the observed sun elevation."""
    raise NotImplementedError("P2")
