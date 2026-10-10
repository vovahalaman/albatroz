"""Astrolabe summary for the agent: observed sun + clock hypotheses + compass, as one JSON-able dict.

Kept apart from astrolabe.py (detection, sun_check) to respect the file-size rule.
Compass: bearing = sun azimuth + (image yaw − sun yaw). Image yaw 0 = image centre column.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from navegador.config import regions
from navegador.schemas import SunObs
from navegador.tools.astrolabe import detect, sun_check, wrap180

DIRECTIONS: dict[str, float] = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}


def compass_label(bearing: float) -> str:
    return list(DIRECTIONS)[round((bearing % 360) / 45) % 8]


def _region_points() -> list[tuple[float, float]]:
    """Centres of the regions.yaml boxes: used when the location is unknown."""
    return [((a + b) / 2, (c + d) / 2) for a, b, c, d in regions().regions.values()]


def _plausible_azimuths(points: list[tuple[float, float]], timestamp: str, obs: SunObs) -> list[float]:
    """Expected sun azimuths for every (point, clock) whose elevation fits the observation."""
    out: list[float] = []
    for lat, lng in points:
        check = sun_check(lat, lng, timestamp, obs.yaw, obs.elevation, obs.uncertainty_deg)
        out += [h["expected_azimuth_deg"] for h in check["hypotheses"]
                if h["above_horizon"] and h["elevation_residual_deg"] <= obs.uncertainty_deg]
    return out


def _clock_names(hyps: list[dict[str, Any]]) -> str:
    return " and ".join(h["clock"][0] for h in hyps)


def summarise(obs: SunObs | None, timestamp: str, lat: float | None = None,
              lng: float | None = None) -> dict[str, Any]:
    """Summary for an already detected sun (or None). Without lat/lng the region centre is used."""
    given = lat is not None and lng is not None
    if not given:
        lat, lng = regions().fallback_point
    assert lat is not None and lng is not None
    check = sun_check(lat, lng, timestamp, obs.yaw if obs else None, obs.elevation if obs else None,
                      obs.uncertainty_deg if obs else None)
    out: dict[str, Any] = {
        "sun_observed": obs is not None, "source": obs.source if obs else None,
        "yaw": obs.yaw if obs else None, "elevation": obs.elevation if obs else None,
        "uncertainty_deg": obs.uncertainty_deg if obs else None, "confidence": obs.confidence if obs else None,
        "lat": lat, "lng": lng, "location": "given" if given else "region_centre", "timestamp": timestamp,
        "time_hypotheses": check["hypotheses"], "best": check["best"], "ambiguous": check["ambiguous"],
        "consistency": check["consistency"], "compass": None, "compass_uncertainty_deg": None,
    }
    if obs is None or check["best"] is None:
        above = "" if check["any_above_horizon"] else " The sun should be below the horizon for every clock reading."
        out["note"] = "No sun detected, so the image heading is unknown." + above
        return out

    best = check["best"]
    centre = (best["expected_azimuth_deg"] - obs.yaw) % 360.0
    points = [(lat, lng)] + ([] if given else _region_points())
    azimuths = _plausible_azimuths(points, timestamp, obs) or [best["expected_azimuth_deg"]]
    spread = max(abs(wrap180(a - best["expected_azimuth_deg"])) for a in azimuths)
    unc = round(obs.uncertainty_deg + spread, 1)
    out["compass"] = {
        "centre_bearing_deg": round(centre, 1), "centre_faces": compass_label(centre),
        "directions": {k: round(wrap180(b - centre), 1) for k, b in DIRECTIONS.items()},
    }
    out["compass_uncertainty_deg"] = unc
    out["note"] = _note(out, check, obs)
    return out


def _note(out: dict[str, Any], check: dict[str, Any], obs: SunObs) -> str:
    c = out["compass"]
    parts = [f"Image centre faces roughly {c['centre_faces']} ({c['centre_bearing_deg']:.0f}°, "
             f"±{out['compass_uncertainty_deg']:.0f}°)."]
    u = obs.uncertainty_deg
    fits = [h for h in check["hypotheses"] if h["above_horizon"] and h["elevation_residual_deg"] <= u]
    if check["ambiguous"]:
        parts.append(f"Clock ambiguous between {_clock_names(fits)}.")
    elif fits:
        parts.append(f"Clock fits {fits[0]['clock'][0]} time.")
    if (check["consistency"] or 0) < 0.5:
        parts.append(f"Sun elevation fits poorly here (residual {check['best']['elevation_residual_deg']:.0f}°), "
                     "so the location or the clock may be wrong.")
    if obs.source == "vlm":
        parts.append("Sun position comes from the visual model, low confidence.")
    if out["location"] == "region_centre":
        parts.append("Location unknown, region centre assumed.")
    return " ".join(parts)


def sun_summary(pano_path: Path, timestamp: str, lat: float | None = None,
                lng: float | None = None) -> dict[str, Any]:
    """Detect the sun in a panorama and summarise it for the agent (JSON-serialisable).

    Args:
        pano_path: equirectangular panorama.
        timestamp: camera clock as YYYYMMDDhhmmss (time zone unknown).
        lat, lng: candidate location; region centre (39.5, -8.0) if not given.

    Returns sun_observed, source, yaw, elevation, uncertainty_deg, confidence, time_hypotheses, best,
    ambiguous, compass (centre bearing + image yaw per N…NW, or null), compass_uncertainty_deg, note.
    """
    return summarise(detect(Path(pano_path)), timestamp, lat, lng)
