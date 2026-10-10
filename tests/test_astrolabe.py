from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pvlib
import pytest
from PIL import Image

from navegador.tools.astrolabe import angles_to_pixel, detect, sun_check, wrap180
from navegador.tools.magnifier import pixel_to_angles

LISBON = (38.7223, -9.1393)
TS = "20261010140000"


def _by_clock(result: dict, name: str) -> dict:
    return next(h for h in result["hypotheses"] if name in h["clock"])


# ---------------------------------------------------------------- sun_check


def test_sun_check_lisbon_matches_spec_and_pvlib():
    r = sun_check(*LISBON, TS)
    h = _by_clock(r, "Europe/Lisbon")
    assert h["expected_elev_deg"] == pytest.approx(43.7, abs=0.2)  # SPEC §9.2 table
    assert h["expected_azimuth_deg"] == pytest.approx(192.6, abs=0.2)
    t = pd.DatetimeIndex([datetime(2026, 10, 10, 14, tzinfo=ZoneInfo("Europe/Lisbon"))])
    ref = pvlib.solarposition.get_solarposition(t, *LISBON).iloc[0]
    assert h["expected_elev_deg"] == pytest.approx(ref["apparent_elevation"], abs=0.01)
    assert h["expected_azimuth_deg"] == pytest.approx(ref["azimuth"], abs=0.01)


def test_sun_check_merges_identical_clocks_and_keeps_utc():
    r = sun_check(*LISBON, TS)
    assert len(r["hypotheses"]) == 2  # local == Europe/Lisbon in Portugal
    assert "local (Europe/Lisbon)" in _by_clock(r, "Europe/Lisbon")["clock"]
    assert _by_clock(r, "UTC")["utc"].startswith("2026-10-10T14:00")


def test_sun_check_madrid_has_three_clocks():
    r = sun_check(40.4168, -3.7038, TS)
    assert len(r["hypotheses"]) == 3
    assert _by_clock(r, "local (Europe/Madrid)")["expected_azimuth_deg"] == pytest.approx(179.4, abs=0.2)


def test_sun_check_picks_utc_when_observation_fits_utc():
    utc = _by_clock(sun_check(*LISBON, TS), "UTC")
    r = sun_check(*LISBON, TS, sun_yaw=30.0, sun_elev=utc["expected_elev_deg"])
    assert r["best"]["clock"] == ["UTC"]
    assert r["consistency"] == 1.0
    assert r["ambiguous"] is False
    assert r["best"]["north_yaw_deg"] == pytest.approx(wrap180(30.0 - utc["expected_azimuth_deg"]), abs=0.01)


def test_sun_check_inconsistent_observation_scores_zero():
    r = sun_check(*LISBON, TS, sun_yaw=0.0, sun_elev=5.0)
    assert r["consistency"] == 0.0


def test_sun_check_without_observation_returns_expected_only():
    r = sun_check(*LISBON, TS)
    assert r["sun_observed"] is False and r["best"] is None and r["consistency"] is None
    assert r["any_above_horizon"] is True
    assert all("elevation_residual_deg" not in h for h in r["hypotheses"])
    night = sun_check(*LISBON, "20261010230000")
    assert night["any_above_horizon"] is False


# ---------------------------------------------------------------- pixel <-> angle


@pytest.mark.parametrize("x", [0.5, 100.0, 759.5, 760.0, 1519.0])
@pytest.mark.parametrize("y", [0.5, 190.0, 380.0, 700.0])
def test_pixel_angle_round_trip(x: float, y: float):
    w, h = 1520, 760
    yaw, pitch = pixel_to_angles(x, y, w, h)
    assert angles_to_pixel(yaw, pitch, w, h) == pytest.approx((x, y), abs=1e-9)


# ---------------------------------------------------------------- detect (synthetic panoramas)

SKY, GROUND, WHITE = (90, 140, 220), (110, 90, 60), (255, 255, 255)


def _pano(w: int = 3040, h: int = 1520, sky: tuple[int, int, int] = SKY) -> np.ndarray:
    img = np.empty((h, w, 3), dtype=np.uint8)
    img[: h // 2], img[h // 2:] = sky, GROUND
    return img


def _disc(img: np.ndarray, yaw: float, elev: float, radius_deg: float) -> np.ndarray:
    """Paint a disc that is round on the sphere (great-circle distance)."""
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    lon = np.radians(((xx + 0.5) / w - 0.5) * 360.0)
    lat = np.radians((0.5 - (yy + 0.5) / h) * 180.0)
    lon0, lat0 = np.radians(yaw), np.radians(elev)
    cosd = np.sin(lat) * np.sin(lat0) + np.cos(lat) * np.cos(lat0) * np.cos(lon - lon0)
    img[cosd >= np.cos(np.radians(radius_deg))] = WHITE
    return img


def _save(img: np.ndarray, tmp_path: Path, name: str = "IMG_20261010_140000_00_001.jpg") -> Path:
    path = tmp_path / name
    Image.fromarray(img).save(path, quality=95)
    return path


def test_detect_finds_disc_at_known_position(tmp_path: Path):
    sun = detect(_save(_disc(_pano(), yaw=40.0, elev=35.0, radius_deg=2.0), tmp_path))
    assert sun is not None
    assert sun.yaw == pytest.approx(40.0, abs=1.0)
    assert sun.elevation == pytest.approx(35.0, abs=1.0)
    assert sun.uncertainty_deg == pytest.approx(2.0 + 3.0, abs=0.5)
    assert sun.confidence >= 0.4
    x, y = angles_to_pixel(40.0, 35.0, 3040, 1520)
    assert sun.pixel_xy == pytest.approx((x, y), abs=8)


def test_detect_disc_across_the_seam(tmp_path: Path):
    sun = detect(_save(_disc(_pano(), yaw=180.0, elev=20.0, radius_deg=2.0), tmp_path))
    assert sun is not None
    assert abs(wrap180(sun.yaw - 180.0)) < 1.0
    assert sun.elevation == pytest.approx(20.0, abs=1.0)


def test_detect_none_on_fully_white_sky(tmp_path: Path):
    assert detect(_save(_pano(sky=WHITE), tmp_path)) is None


def test_detect_none_on_white_wall(tmp_path: Path):
    img = _pano()
    img[500:760, 1000:1400] = WHITE  # large bright facade reaching the horizon
    assert detect(_save(img, tmp_path)) is None


def test_detect_none_when_two_equal_suns(tmp_path: Path):
    img = _disc(_disc(_pano(), -60.0, 30.0, 2.0), 90.0, 30.0, 2.0)
    assert detect(_save(img, tmp_path)) is None


def test_detect_none_on_plain_sky(tmp_path: Path):
    assert detect(_save(_pano(), tmp_path)) is None
