"""ui/streetview.py: links, coverage check and embed choice. HTTP is always faked."""

from urllib.parse import parse_qs, urlparse

import pytest

from ui import streetview


@pytest.fixture(autouse=True)
def _fresh_cache():
    streetview.coverage.cache_clear()
    yield
    streetview.coverage.cache_clear()


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def _fake_get(payload, calls):
    def get(url, params, timeout):
        calls.append((url, params, timeout))
        return FakeResponse(payload)
    return get


def test_links():
    assert streetview.streetview_link(38.7104, -9.136312) == (
        "https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=38.710400,-9.136312")
    assert streetview.maps_link(38.7104, -9.136312) == "https://www.google.com/maps/search/?api=1&query=38.710400,-9.136312"


def test_embed_key_from_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_EMBED_KEY", raising=False)
    assert streetview.embed_key() is None
    monkeypatch.setenv("GOOGLE_MAPS_EMBED_KEY", "k123")
    assert streetview.embed_key() == "k123"


def test_streetview_when_imagery_exists(monkeypatch):
    calls = []
    monkeypatch.setattr(streetview.requests, "get", _fake_get({"status": "OK", "pano_id": "PANO1"}, calls))
    emb = streetview.embed_for(38.7104, -9.1363, "urban", "k123")
    q = parse_qs(urlparse(emb.url).query)
    assert emb.kind == "streetview" and emb.url.startswith("https://www.google.com/maps/embed/v1/streetview?")
    assert q["pano"] == ["PANO1"] and q["key"] == ["k123"] and emb.note == ""
    assert calls[0][1]["radius"] == 100 and calls[0][2] == streetview.TIMEOUT_S


def test_satellite_when_no_imagery(monkeypatch):
    monkeypatch.setattr(streetview.requests, "get", _fake_get({"status": "ZERO_RESULTS"}, []))
    emb = streetview.embed_for(39.0, -7.5, "non_urban", "k123")
    q = parse_qs(urlparse(emb.url).query)
    assert emb.kind == "satellite" and q["maptype"] == ["satellite"] and q["center"] == ["39.000000,-7.500000"]
    assert "No Street View within 1000 m" in emb.note


def test_unchecked_coverage_still_embeds_with_a_note(monkeypatch):
    monkeypatch.setattr(streetview.requests, "get", _fake_get({"status": "REQUEST_DENIED"}, []))
    emb = streetview.embed_for(38.7, -9.1, "urban", "k123")
    assert emb.kind == "streetview" and "location=38.700000%2C-9.100000" in emb.url
    assert "could not be checked (REQUEST_DENIED)" in emb.note


def test_network_error_is_not_raised(monkeypatch):
    def boom(*a, **k):
        raise streetview.requests.ConnectionError("offline")
    monkeypatch.setattr(streetview.requests, "get", boom)
    assert streetview.embed_for(38.7, -9.1, "urban", "k123").note.startswith("Street View coverage could not")


def test_coverage_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(streetview.requests, "get", _fake_get({"status": "OK", "pano_id": "P"}, calls))
    streetview.embed_for(38.7, -9.1, "urban", "k")
    streetview.embed_for(38.7, -9.1, "urban", "k")
    assert len(calls) == 1
