"""Astrolabe: sun detection, sun-position checks, north offset (SPEC §9.2).

Uses pvlib.solarposition.get_solarposition and timezonefinder.TimezoneFinder.
The camera clock is unreliable (local, UTC or Lisbon time, also in ES/FR), so sun_check
evaluates every entry of `astrolabe.time_hypotheses` and ignores flags.timestamp_is_local.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pvlib
from PIL import Image
from scipy import ndimage
from timezonefinder import TimezoneFinder

from navegador.config import AstrolabeSettings, settings
from navegador.core.state import parse_timestamp, timestamp_dt
from navegador.schemas import SunObs
from navegador.tools.magnifier import pixel_to_angles


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


@dataclass
class Blob:
    """A bright glare component. Shape fields describe the whole glare; x, y, yaw, elev locate its
    saturated core (min channel ≥ core_min) in the downscaled detection image."""

    x: float
    y: float
    n_px: int
    yaw: float
    elev: float
    solid_angle_deg2: float
    extent_deg: float
    radius_deg: float
    circularity: float
    core_px: int = 0
    core_radius_deg: float = 0.0
    contrast: float = 0.0
    score: float = 0.0
    reject: str | None = None


def load_small(pano_path: Path) -> tuple[np.ndarray, tuple[int, int]]:
    """RGB uint8 array at astrolabe.detect_size, plus the original (W, H)."""
    size = tuple(_cfg().detect_size)
    with Image.open(pano_path) as im:
        orig = im.size
        im.draft("RGB", size)  # JPEG: decode directly at reduced scale
        rgb = im.convert("RGB").resize(size, Image.Resampling.BOX)
    return np.asarray(rgb), orig


def pano_date(pano_path: Path) -> date | None:
    """Capture date from the filename. The clock time is unreliable; the date is good enough here."""
    ts = parse_timestamp(Path(pano_path).name)
    return timestamp_dt(ts).date() if ts else None


def max_sun_elev(day: date) -> float:
    """Highest noon sun on this date at astrolabe.ceiling_lat_deg (higher further south)."""
    decl = math.degrees(float(pvlib.solarposition.declination_spencer71(day.timetuple().tm_yday)))
    return 90.0 - abs(_cfg().ceiling_lat_deg - decl)


def _solid_angle(ys: np.ndarray, h: int, w: int) -> float:
    """Σ pixel solid angles in deg² (pixel area shrinks with cos(elevation))."""
    return float(np.cos(np.radians((0.5 - (ys + 0.5) / h) * 180.0)).sum()) * (360.0 / w) * (180.0 / h)


def _merge_seam(labels: np.ndarray, n: int) -> np.ndarray:
    """Join components that touch across the x=0 / x=W−1 seam."""
    parent = list(range(n + 1))

    def find(a: int) -> int:
        while parent[a] != a:
            a = parent[a]
        return a

    left, right = labels[:, 0], labels[:, -1]
    for r in np.flatnonzero(left):
        for rr in range(max(r - 1, 0), min(r + 2, len(right))):
            if right[rr]:
                parent[find(int(right[rr]))] = find(int(left[r]))
    return np.array([find(i) for i in range(n + 1)])[labels]


def _ring_contrast(lum: np.ndarray, b: Blob, blob_mean: float) -> float:
    """(blob − ring) / blob mean luminance; the ring is an angular annulus (x stretch corrected)."""
    cfg, (h, w) = _cfg(), lum.shape
    dpx, dpy = 360.0 / w, 180.0 / h
    r_in, r_out = (k * max(b.radius_deg, dpy) for k in (cfg.ring_inner, cfg.ring_outer))
    cos_e = max(math.cos(math.radians(b.elev)), 0.05)
    ry, rx = math.ceil(r_out / dpy), min(math.ceil(r_out / (dpx * cos_e)), w // 2)
    rows = np.arange(int(b.y) - ry, int(b.y) + ry + 1)
    rows = rows[(rows >= 0) & (rows < h)]
    cols = np.arange(int(b.x) - rx, int(b.x) + rx + 1)
    d = np.hypot(((cols + 0.5 - b.x) * dpx * cos_e)[None, :], ((rows + 0.5 - b.y) * dpy)[:, None])
    ring = lum[np.ix_(rows, cols % w)][(d >= r_in) & (d < r_out)]
    return (blob_mean - float(ring.mean())) / blob_mean if ring.size else 0.0


def _measure(ys: np.ndarray, xs: np.ndarray, lum: np.ndarray, mn: np.ndarray, ceiling: float | None) -> Blob:
    cfg, (h, w) = _cfg(), lum.shape
    dpx, dpy = 360.0 / w, 180.0 / h
    top_span = len(np.unique(xs[ys == 0])) * dpx
    if xs.min() == 0 and xs.max() == w - 1:  # straddles the seam: unwrap
        xs = np.where(xs < w // 2, xs + w, xs)
    cos_e = max(math.cos(math.radians((0.5 - (float(ys.mean()) + 0.5) / h) * 180.0)), 0.05)
    omega = _solid_angle(ys, h, w)
    extent = float(max((xs.max() - xs.min() + 1) * dpx * cos_e, (ys.max() - ys.min() + 1) * dpy))
    core = mn[ys, xs % w] >= cfg.core_min  # locate the sun from the saturated core, not the glare
    cys, cxs = (ys[core], xs[core]) if core.any() else (ys, xs)
    core_cos = max(math.cos(math.radians((0.5 - (float(cys.mean()) + 0.5) / h) * 180.0)), 0.05)
    core_ext = float(max((cxs.max() - cxs.min() + 1) * dpx * core_cos, (cys.max() - cys.min() + 1) * dpy))
    circ = min(1.0, _solid_angle(cys, h, w) / (math.pi * (core_ext / 2) ** 2)) if core.any() else 0.0  # core shape
    cx, cy = (float(cxs.mean()) + 0.5) % w, float(cys.mean()) + 0.5
    yaw, elev = pixel_to_angles(cx, cy, w, h)
    b = Blob(cx, cy, len(xs), yaw, elev, omega, extent, math.sqrt(omega / math.pi), circ,
             int(core.sum()), math.sqrt(_solid_angle(ys[core], h, w) / math.pi))
    checks = (("too_small", b.n_px < cfg.min_pixels), ("top_edge", top_span > cfg.top_edge_max_span_deg),
              ("too_large", omega > cfg.max_solid_angle_deg2), ("too_extended", extent > cfg.max_extent_deg),
              ("not_round", circ < cfg.min_circularity),
              ("weak_core", b.core_radius_deg < cfg.min_core_radius_deg or core.mean() < cfg.min_core_fraction),
              ("below_min_elev", elev < cfg.min_elev_deg),
              ("above_ceiling", ceiling is not None and elev > ceiling + cfg.tilt_margin_deg))
    b.reject = next((name for name, bad in checks if bad), None)
    if b.reject is None:  # contrast only for blobs that pass the cheap geometric checks
        b.contrast = _ring_contrast(lum, b, float(lum[ys, xs % w].mean()))
        b.reject = "low_contrast" if b.contrast < cfg.min_contrast else None
    if b.reject is None:
        b.score = (circ * min(1.0, b.contrast / (2 * cfg.min_contrast))
                   * min(1.0, b.core_radius_deg / (2 * cfg.min_core_radius_deg)))
    return b


def find_blobs(rgb: np.ndarray, day: date | None = None) -> list[Blob]:
    """All bright, low-saturation components in the upper half, measured and scored (best first).
    With `day`, blobs above the highest possible sun for that date are rejected."""
    cfg, h = _cfg(), rgb.shape[0]
    f = rgb.astype(np.float32)
    v, mn = f.max(axis=2), f.min(axis=2)
    lum = f @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    mask = (v >= cfg.brightness_min) & ((v - mn) / np.maximum(v, 1.0) <= cfg.saturation_max)
    mask[h // 2:] = False
    labels, n = ndimage.label(mask, structure=np.ones((3, 3), dtype=int))
    if n == 0:
        return []
    labels = _merge_seam(labels, n)
    ys, xs = np.nonzero(labels)
    order = np.argsort(labels[ys, xs], kind="stable")
    ys, xs = ys[order], xs[order]
    cuts = np.flatnonzero(np.diff(labels[ys, xs])) + 1
    ceiling = max_sun_elev(day) if day else None
    blobs = [_measure(y, x, lum, mn, ceiling) for y, x in zip(np.split(ys, cuts), np.split(xs, cuts), strict=True)]
    return sorted(blobs, key=lambda b: -b.score)


def choose(blobs: list[Blob]) -> tuple[Blob | None, str]:
    """Best passing blob and a reason: ok | no_candidate | low_confidence | ambiguous."""
    cfg = _cfg()
    ok = [b for b in blobs if b.reject is None]
    if not ok:
        return None, "no_candidate"
    if ok[0].score < cfg.min_confidence:
        return None, "low_confidence"
    if len(ok) > 1 and ok[1].score >= cfg.ambiguity_ratio * ok[0].score:
        return None, "ambiguous"
    return ok[0], "ok"


def detect(pano_path: Path) -> SunObs | None:
    """Sun position in image angles, or None. Conservative: a wrong sun is worse than no sun."""
    rgb, (w0, h0) = load_small(Path(pano_path))
    best, _ = choose(find_blobs(rgb, pano_date(pano_path)))
    if best is None:
        return None
    h, w = rgb.shape[:2]
    return SunObs(yaw=round(best.yaw, 2), elevation=round(best.elev, 2), area_px=best.n_px,
                  confidence=round(best.score, 3),
                  uncertainty_deg=round(best.core_radius_deg + _cfg().tilt_margin_deg, 2),
                  pixel_xy=(round(best.x * w0 / w, 1), round(best.y * h0 / h, 1)))


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
