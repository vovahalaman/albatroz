from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pvlib
import pytest

from navegador.tools.astrolabe import angles_to_pixel, sun_check, wrap180
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
