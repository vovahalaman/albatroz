"""tools/geo.py and tools/cache.py with mocked HTTP and a fake clock (no network)."""

from typing import Any

import pytest
import requests

from navegador.config import settings
from navegador.core import scribe
from navegador.tools import cache, geo

PANO = "geo-test"
UA = "navegador-test/1.0 (team@example.com)"
BELEM = {"place_id": 1, "lat": "38.6916", "lon": "-9.2160", "name": "Torre de Belém",
         "display_name": "Torre de Belém, Avenida Brasília, Belém, Lisboa, Portugal",
         "category": "historic", "type": "castle", "importance": 0.6, "osm_type": "way",
         "osm_id": 23464, "address": {"city": "Lisboa", "country_code": "pt"}}


class FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code, self._payload = status, payload

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Replays queued responses (or exceptions) and records every request."""

    def __init__(self) -> None:
        self.queue: list[Any] = []
        self.calls: list[dict[str, Any]] = []

    def push(self, payload: Any, status: int = 200) -> None:
        self.queue.append(FakeResponse(status, payload))

    def request(self, method: str, url: str, **kw: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kw})
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def http(monkeypatch):
    session, clock = FakeSession(), FakeClock()
    monkeypatch.setattr(geo, "_session", session)
    monkeypatch.setattr(geo, "_clock", clock)
    monkeypatch.setattr(geo, "_sleep", clock.sleep)
    monkeypatch.setattr(geo, "_last_nominatim", [float("-inf")])
    monkeypatch.setattr(settings().geo, "user_agent", UA)
    session.clock = clock
    with geo.use_run(PANO):
        yield session


def tool_events(event: str = "tool_call") -> list[dict]:
    return [e for e in scribe.read_events(PANO) if e["event"] == event]


# ---------------------------------------------------------------- cache


def test_cache_roundtrip_and_none():
    assert cache.lookup("ns", {"a": 1}) == (False, None)
    cache.put("ns", {"a": 1}, None)
    assert cache.lookup("ns", {"a": 1}) == (True, None)  # a cached None is a hit
    cache.put("ns", {"a": 2}, [{"x": "é"}])
    assert cache.get("ns", {"a": 2}) == [{"x": "é"}]


def test_cached_decorator():
    calls = []

    @cache.cached("deco")
    def f(x: int, y: int = 2) -> int:
        calls.append(x)
        return x * y

    assert f(3) == 6 and f(3, y=2) == 6 and f(x=3) == 6
    assert calls == [3]


# ---------------------------------------------------------------- nominatim


def test_geocode_registers_call_id_and_sends_policy_headers(http):
    http.push([BELEM])
    res = geo.geocode("Torre de Belém, Lisboa", limit=1)
    assert len(res) == 1
    r = res[0]
    assert (r["lat"], r["lng"]) == pytest.approx((38.6916, -9.2160))
    assert isinstance(r["lat"], float) and r["class"] == "historic"
    assert r["call_id"].startswith("geocode-")
    assert scribe.tool_registry(PANO) == {r["call_id"]}
    call = http.calls[0]
    assert call["url"].endswith("/search") and call["headers"]["User-Agent"] == UA
    assert call["timeout"] == settings().geo.http_timeout_s
    assert call["params"]["countrycodes"] == "pt,es,fr" and call["params"]["format"] == "jsonv2"


def test_second_call_hits_cache_with_same_call_id(http):
    http.push([BELEM])
    first = geo.geocode("Torre de Belém, Lisboa")
    second = geo.geocode("  Torre de Belém, Lisboa ")  # normalised to the same key
    assert len(http.calls) == 1
    assert first == second
    assert [e["payload"]["cached"] for e in tool_events()] == [False, True]


def test_throttle_between_uncached_nominatim_calls(http):
    http.push([BELEM])
    http.push({"error": "Unable to geocode"})
    geo.geocode("a")
    geo.geocode("a")  # cache hit: no wait
    assert http.clock.sleeps == []
    assert geo.reverse(0.0, 0.0) == {"call_id": tool_events()[-1]["payload"]["call_id"]}
    assert http.clock.sleeps == [pytest.approx(settings().geo.nominatim_min_interval_s)]


def test_retry_then_success(http):
    http.push(None, status=503)
    http.queue.append(requests.ConnectionError("reset"))
    http.push([BELEM])
    assert geo.geocode("Belém")[0]["name"] == "Torre de Belém"
    assert len(http.calls) == 3


def test_retries_exhausted_logs_error_and_caches_nothing(http):
    for _ in range(settings().geo.retries):
        http.push(None, status=503)
    with pytest.raises(geo.GeoError, match="503"):
        geo.geocode("Belém")
    assert tool_events() == [] and len(tool_events("error")) == 1
    http.push([BELEM])
    assert geo.geocode("Belém")  # nothing was cached, so this goes to the network
    assert len(http.calls) == settings().geo.retries + 1


def test_client_error_is_not_retried(http):
    http.push(None, status=403)
    with pytest.raises(geo.GeoError):
        geo.geocode("Belém")
    assert len(http.calls) == 1


def test_budget_per_pano(http, monkeypatch):
    monkeypatch.setattr(settings().geo, "max_geocode_calls_per_pano", 2)
    http.push([BELEM])
    http.push([BELEM])
    geo.geocode("a")
    geo.geocode("b")
    geo.geocode("a")  # cache hits are free
    with pytest.raises(geo.GeoError, match="budget"):
        geo.geocode("c")
    assert len(http.calls) == 2 and len(tool_events("error")) == 1


def test_placeholder_user_agent_is_refused(http, monkeypatch):
    monkeypatch.setattr(settings().geo, "user_agent", "navegador-junctionx/1.0 (<team email>)")
    with pytest.raises(geo.GeoError, match="TEAM_EMAIL"):
        geo.geocode("Belém")
    assert http.calls == []


def test_outside_use_run_raises():
    with pytest.raises(RuntimeError, match="use_run"):
        geo.geocode("Belém")


def test_reverse(http):
    http.push(BELEM)
    r = geo.reverse(38.69160012, -9.216)
    assert r["name"] == "Torre de Belém" and r["call_id"].startswith("reverse-")
    assert http.calls[0]["params"]["lat"] == 38.6916 and http.calls[0]["params"]["zoom"] == 14


# ---------------------------------------------------------------- overpass


def overpass_payload(n: int) -> dict:
    nodes = [{"type": "node", "id": i, "lat": 38.7, "lon": -9.1, "tags": {"name": f"Igreja {i}"}}
             for i in range(n)]
    way = {"type": "way", "id": 999, "center": {"lat": 38.71, "lon": -9.11}, "tags": {}}
    return {"elements": [way, *nodes, {"type": "relation", "id": 5}]}  # last has no coords


def test_overpass_near_query_and_cap(http):
    http.push(overpass_payload(80))
    res = geo.overpass_near(38.7, -9.1, "amenity=place_of_worship")
    assert len(res) == settings().geo.overpass_max_results
    assert res[0]["osm_type"] == "way" and res[0]["lat"] == 38.71 and res[0]["name"] is None
    assert len({r["call_id"] for r in res}) == 1
    call = http.calls[0]
    q = call["data"]["data"]
    assert call["method"] == "POST" and call["url"] == settings().geo.overpass_url
    assert '["amenity"="place_of_worship"](around:500,38.7,-9.1)' in q
    assert "[timeout:25]" in q and "out center 50;" in q
    assert http.clock.sleeps == []  # Overpass is not under the Nominatim throttle


def test_overpass_runtime_error_is_not_cached(http):
    http.push({"elements": [], "remark": "runtime error: Query timed out"})
    with pytest.raises(geo.GeoError, match="timed out"):
        geo.overpass_near(38.7, -9.1, "railway")
    assert len(tool_events("error")) == 1


@pytest.mark.parametrize("tag", ['amenity="x"];out;', "amenity=a]b", "", "a b"])
def test_overpass_rejects_bad_tags(http, tag):
    with pytest.raises(ValueError):
        geo.overpass_near(38.7, -9.1, tag)
    assert http.calls == []


def test_overpass_named_bbox_and_regions(http):
    http.push(overpass_payload(1))
    http.push(overpass_payload(1))
    geo.overpass_named('Café "A Brasileira"', bbox="38.7,-9.2,38.8,-9.1")
    q = http.calls[0]["data"]["data"]
    assert '[name="Café \\"A Brasileira\\""](38.7,-9.2,38.8,-9.1)' in q
    geo.overpass_named("Sé")
    from navegador.config import regions
    q = http.calls[1]["data"]["data"]
    assert q.count('[name="Sé"]') == len(regions().regions)
    with pytest.raises(ValueError):
        geo.overpass_named("x", bbox="1,2,3")


# ---------------------------------------------------------------- wikipedia


def test_wiki_coords(http):
    http.push({"query": {"pages": [
        {"index": 2, "title": "Belém", "coordinates": [{"lat": 38.697, "lon": -9.206}], "fullurl": "u2"},
        {"index": 1, "title": "Torre de Belém", "coordinates": [{"lat": 38.6916, "lon": -9.216}],
         "fullurl": "https://pt.wikipedia.org/wiki/Torre_de_Bel%C3%A9m"}]}})
    r = geo.wiki_coords("Torre de Belém")
    assert r is not None and r["title"] == "Torre de Belém" and r["call_id"].startswith("wiki_coords-")
    assert http.calls[0]["url"] == "https://pt.wikipedia.org/w/api.php"
    assert http.calls[0]["headers"]["User-Agent"] == UA


def test_wiki_coords_none(http):
    http.push({"query": {"pages": [{"index": 1, "title": "Saudade"}]}})
    http.push({"batchcomplete": True})
    assert geo.wiki_coords("saudade") is None
    assert geo.wiki_coords("zzzz", lang="es") is None
    assert len(tool_events()) == 2  # the call is still registered
    with pytest.raises(ValueError):
        geo.wiki_coords("x", lang="pt/../evil")
