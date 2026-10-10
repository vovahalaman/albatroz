import json
from pathlib import Path

import pytest

from navegador.schemas import SunObs
from navegador.tools import astrolabe_summary
from navegador.tools.astrolabe import sun_check, wrap180
from navegador.tools.astrolabe_summary import sun_summary, summarise

LISBON = (38.7223, -9.1393)
TS = "20261010140000"  # Lisbon local 14:00: elev 43.7, az 192.6; UTC reading: elev 39.3, az 211.6


def _obs(yaw: float, elev: float, unc: float = 3.0, source: str = "detect") -> SunObs:
    return SunObs(yaw=yaw, elevation=elev, area_px=100, confidence=0.8, uncertainty_deg=unc, source=source)


def test_compass_maps_sun_azimuth_back_to_sun_yaw():
    r = summarise(_obs(30.0, 43.7), TS, *LISBON)
    json.dumps(r)  # JSON-serialisable for the agent
    az = r["best"]["expected_azimuth_deg"]
    c = r["compass"]
    assert c["centre_bearing_deg"] == pytest.approx((az - 30.0) % 360, abs=0.1)
    assert c["centre_faces"] == "S"
    assert c["directions"]["N"] == pytest.approx(wrap180(-c["centre_bearing_deg"]), abs=0.1)
    assert wrap180(c["directions"]["S"] + az - 180) == pytest.approx(30.0, abs=0.2)  # sun azimuth → sun yaw
    assert r["compass_uncertainty_deg"] == pytest.approx(3.0, abs=0.1)  # one clock fits, location given
    assert r["location"] == "given" and not r["ambiguous"]
    assert "faces roughly S" in r["note"] and "Lisbon" in r["note"]


def test_ambiguous_clock_widens_compass_uncertainty():
    r = summarise(_obs(30.0, 41.5), TS, *LISBON)  # fits both local (43.7) and UTC (39.3) within 3°
    assert r["ambiguous"] is True
    spread = abs(wrap180(211.6 - 192.6))
    assert r["compass_uncertainty_deg"] >= 3.0 + spread - 0.5
    assert "ambiguous" in r["note"] and "UTC" in r["note"]


def test_region_centre_used_without_location():
    r = summarise(_obs(30.0, 43.7), TS)
    assert (r["lat"], r["lng"]) == (39.5, -8.0)
    assert r["location"] == "region_centre"
    given = summarise(_obs(30.0, 43.7), TS, *LISBON)
    assert r["compass_uncertainty_deg"] >= given["compass_uncertainty_deg"]
    assert "region centre" in r["note"]


def test_no_sun_gives_null_compass():
    r = summarise(None, TS, *LISBON)
    json.dumps(r)
    assert r["sun_observed"] is False and r["compass"] is None and r["compass_uncertainty_deg"] is None
    assert r["time_hypotheses"] and r["best"] is None
    assert "unknown" in r["note"]


def test_poor_fit_and_vlm_source_are_mentioned():
    r = summarise(_obs(30.0, 10.0, source="vlm"), TS, *LISBON)
    assert r["consistency"] == 0.0
    assert "fits poorly" in r["note"] and "visual model" in r["note"]


def test_sun_summary_runs_detect(monkeypatch, tmp_path: Path):
    expected = sun_check(*LISBON, TS)["hypotheses"][0]
    monkeypatch.setattr(astrolabe_summary, "detect", lambda p: _obs(-20.0, expected["expected_elev_deg"]))
    r = sun_summary(tmp_path / "IMG_20261010_140000_00_001.jpg", TS, *LISBON)
    assert r["sun_observed"] and r["source"] == "detect"
    assert r["compass"]["centre_bearing_deg"] == pytest.approx((expected["expected_azimuth_deg"] + 20.0) % 360, abs=0.1)
