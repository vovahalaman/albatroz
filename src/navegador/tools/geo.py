"""Geo tools for the Detective: typed, docstrings, disk-cached, logged (SPEC §9.3).

Every result dict includes a `call_id` from `scribe.register_tool_call`, which enters the run's
tool registry (R2). The signatures are exposed to the model as tools, so the pano comes from
`use_run(...)` instead of a parameter. contextvars propagate through `asyncio.to_thread` (llm.py),
not through a bare `threading.Thread`.

Nominatim policy: ≤1 req/s (throttled per process, cache hits skip it), custom User-Agent with a
contact address (settings().geo.user_agent, TEAM_EMAIL), cache everything, no bulk/grid queries,
no autocomplete, at most `max_geocode_calls_per_pano` uncached requests per run.
"""

from __future__ import annotations

import contextlib
import contextvars
import math
import re
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, Literal

import requests
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from navegador.config import regions, settings
from navegador.core import scribe
from navegador.core import state as st
from navegador.tools import cache

Service = Literal["nominatim", "overpass", "wikipedia"]
NOMINATIM_TOOLS = frozenset({"geocode", "reverse"})
_OSM_TAG = re.compile(r'^([A-Za-z0-9_:]+)(?:=([^"\\\n\[\]]+))?$')
_LANG = re.compile(r"^[a-z]{2,3}$")
CORNER_MERGE_M = 15.0

_run: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar("geo_run", default=None)
_session = requests.Session()
_throttle_lock = threading.Lock()
_last_nominatim = [float("-inf")]  # monotonic time of the last real Nominatim request
_clock: Callable[[], float] = time.monotonic  # module attributes so tests can fake time
_sleep: Callable[[float], None] = time.sleep


class GeoError(RuntimeError):
    """A geo call failed after retries, was refused by policy, or exceeded the per-pano budget."""


class _Retryable(Exception):
    pass


@contextlib.contextmanager
def use_run(pano_id: str, actor: str = "detective") -> Iterator[None]:
    """Attribute geo calls in this block to `pano_id` (tool registry, log, budget)."""
    token = _run.set((pano_id, actor))
    try:
        yield
    finally:
        _run.reset(token)


# ---------------------------------------------------------------- plumbing


def _current() -> tuple[str, str]:
    run = _run.get()
    if run is None:
        raise RuntimeError("geo tools must be called inside geo.use_run(pano_id) (R2 tool registry)")
    return run


def _run_id(pano_id: str) -> str:
    try:
        return st.load(pano_id).run_id
    except (OSError, ValueError):
        return ""


def _check_policy(pano_id: str, tool: str) -> None:
    ua = settings().geo.user_agent
    if "<" in ua or "@" not in ua:
        raise GeoError(f"User-Agent {ua!r} has no contact address; set TEAM_EMAIL in .env (Nominatim policy)")
    if tool in NOMINATIM_TOOLS:
        limit = settings().geo.max_geocode_calls_per_pano
        used = sum(1 for e in scribe.read_events(pano_id, _run_id(pano_id))
                   if e["event"] == "tool_call" and e["payload"].get("tool") in NOMINATIM_TOOLS
                   and not e["payload"].get("cached"))
        if used >= limit:
            raise GeoError(f"Nominatim budget exhausted: {used}/{limit} uncached calls for this pano")


def _throttle() -> None:
    """Hold at least `nominatim_min_interval_s` between real Nominatim requests in this process."""
    with _throttle_lock:
        wait = _last_nominatim[0] + settings().geo.nominatim_min_interval_s - _clock()
        if wait > 0:
            _sleep(wait)
        _last_nominatim[0] = _clock()


def _request(service: Service, method: str, url: str, params: dict[str, Any] | None,
             data: dict[str, Any] | None, timeout: float) -> Any:
    cfg = settings().geo
    headers = {"User-Agent": cfg.user_agent, "Accept-Language": "pt,es,fr,en"}

    def once() -> Any:
        if service == "nominatim":
            _throttle()
        resp = _session.request(method, url, params=params, data=data, headers=headers, timeout=timeout)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _Retryable(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    retrying = Retrying(
        retry=retry_if_exception_type((_Retryable, requests.ConnectionError, requests.Timeout)),
        stop=stop_after_attempt(cfg.retries), wait=wait_exponential(multiplier=1, max=8),
        sleep=lambda s: _sleep(s), reraise=True)
    try:
        return retrying(once)
    except (requests.RequestException, _Retryable, ValueError) as exc:
        raise GeoError(f"{service} request failed: {type(exc).__name__}: {exc}") from exc


def _fetch(tool: str, args: dict[str, Any], service: Service, parse: Callable[[Any], Any], *,
           method: str = "GET", url: str, params: dict[str, Any] | None = None,
           data: dict[str, Any] | None = None, timeout: float | None = None) -> tuple[Any, str]:
    """Cache → policy → HTTP with retry → parse → cache → register. Returns (value, call_id)."""
    pano_id, actor = _current()
    namespace = f"geo.{tool}"
    hit, value = cache.lookup(namespace, args)
    if not hit:
        try:
            _check_policy(pano_id, tool)
            raw = _request(service, method, url, params, data, timeout or settings().geo.http_timeout_s)
            value = parse(raw)
        except GeoError as exc:
            scribe.log_event(pano_id, "error", actor, {"tool": tool, "args": args, "error": str(exc)})
            raise
        except (KeyError, TypeError, ValueError) as exc:
            scribe.log_event(pano_id, "error", actor, {"tool": tool, "args": args, "error": f"bad response: {exc}"})
            raise GeoError(f"{tool}: bad response: {exc}") from exc
        cache.put(namespace, args, value)
    return value, scribe.register_tool_call(pano_id, actor, tool, args, value, cached=hit)


def _place(r: dict[str, Any]) -> dict[str, Any]:
    return {"name": r.get("name") or str(r.get("display_name", "")).split(",")[0],
            "display_name": r.get("display_name"), "lat": float(r["lat"]), "lng": float(r["lon"]),
            "class": r.get("category", r.get("class")), "type": r.get("type"),
            "importance": r.get("importance"), "osm_type": r.get("osm_type"), "osm_id": r.get("osm_id"),
            "address": r.get("address", {})}


def _osm_elements(raw: dict[str, Any]) -> list[dict[str, Any]]:
    if "runtime error" in str(raw.get("remark", "")):  # Overpass timeouts arrive as HTTP 200
        raise ValueError(f"Overpass: {raw['remark']}")
    out = []
    for el in raw.get("elements", []):
        lat = el.get("lat", el.get("center", {}).get("lat"))
        lng = el.get("lon", el.get("center", {}).get("lon"))
        if lat is None or lng is None:
            continue
        tags = el.get("tags", {})
        out.append({"name": tags.get("name"), "lat": float(lat), "lng": float(lng),
                    "osm_type": el.get("type"), "osm_id": el.get("id"), "tags": tags})
    return out[: settings().geo.overpass_max_results]


def _overpass(tool: str, args: dict[str, Any], statements: str) -> list[dict]:
    cfg = settings().geo
    query = f"[out:json][timeout:{cfg.overpass_timeout_s}];({statements});out center {cfg.overpass_max_results};"
    value, call_id = _fetch(tool, args, "overpass", _osm_elements, method="POST", url=cfg.overpass_url,
                            data={"data": query}, timeout=cfg.overpass_timeout_s + 5)
    return [{**r, "call_id": call_id} for r in value]


def _quote(s: str) -> str:
    if "\n" in s or "\r" in s:
        raise ValueError("newline not allowed in an Overpass string")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------- tools


def geocode(query: str, country_codes: str = "pt,es,fr", limit: int = 5) -> list[dict]:
    """Find places matching a free-text query (Nominatim /search). Each result has name, lat, lng,
    class, type, address and a call_id to cite as provenance."""
    args = {"query": query.strip(), "country_codes": country_codes.replace(" ", "").lower(),
            "limit": max(1, min(int(limit), 10))}
    params = {"q": args["query"], "countrycodes": args["country_codes"], "limit": args["limit"],
              "format": "jsonv2", "addressdetails": 1}
    value, call_id = _fetch("geocode", args, "nominatim", lambda raw: [_place(r) for r in raw],
                            url=f"{settings().geo.nominatim_url}/search", params=params)
    return [{**r, "call_id": call_id} for r in value]


def reverse(lat: float, lng: float, zoom: int = 14) -> dict:
    """Describe the place at a coordinate (Nominatim /reverse): name, address and a call_id.
    Only call_id is present when nothing is there (e.g. open sea)."""
    args = {"lat": round(float(lat), 6), "lng": round(float(lng), 6), "zoom": max(0, min(int(zoom), 18))}
    params = {"lat": args["lat"], "lon": args["lng"], "zoom": args["zoom"], "format": "jsonv2",
              "addressdetails": 1}
    value, call_id = _fetch("reverse", args, "nominatim", lambda raw: {} if "error" in raw else _place(raw),
                            url=f"{settings().geo.nominatim_url}/reverse", params=params)
    return {**value, "call_id": call_id}


def overpass_near(lat: float, lng: float, osm_tag: str, radius_m: int = 500) -> list[dict]:
    """OSM features with `osm_tag` (e.g. amenity=place_of_worship, or just railway) within radius_m
    of a point. At most 50 results, each with name, lat, lng, tags and a call_id."""
    m = _OSM_TAG.match(osm_tag.strip())
    if not m:
        raise ValueError(f"osm_tag must look like 'key' or 'key=value', got {osm_tag!r}")
    key, val = m.groups()
    selector = f"[{_quote(key)}={_quote(val)}]" if val else f"[{_quote(key)}]"
    args = {"lat": round(float(lat), 6), "lng": round(float(lng), 6), "osm_tag": osm_tag.strip(),
            "radius_m": max(1, min(int(radius_m), 50_000))}
    return _overpass("overpass_near", args,
                     f"nwr{selector}(around:{args['radius_m']},{args['lat']},{args['lng']});")


def overpass_named(name: str, bbox: str | None = None) -> list[dict]:
    """OSM features whose name is exactly `name`, inside bbox "south,west,north,east" if given,
    else inside the competition regions. At most 50 results with a call_id."""
    if bbox is not None:
        parts = [float(p) for p in bbox.split(",")]
        if len(parts) != 4:
            raise ValueError(f"bbox must be 'south,west,north,east', got {bbox!r}")
        boxes = [tuple(parts)]
    else:  # regions.yaml boxes are [lat_min, lat_max, lng_min, lng_max]
        boxes = [(a, c, b, d) for a, b, c, d in regions().regions.values()]
    args = {"name": name.strip(), "bbox": bbox}
    selector = f"[name={_quote(args['name'])}]"
    statements = "".join(f"nwr{selector}({s},{w},{n},{e});" for s, w, n, e in boxes)
    return _overpass("overpass_named", args, statements)


def _corners(raw: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """Intersections of the ways named `name` with other named highways, plus street ends that are no
    intersection. Corners closer than CORNER_MERGE_M are merged (dual carriageways, offset nodes)."""
    if "runtime error" in str(raw.get("remark", "")):
        raise ValueError(f"Overpass: {raw['remark']}")
    nodes = {e["id"]: (float(e["lat"]), float(e["lon"])) for e in raw.get("elements", []) if e["type"] == "node"}
    ways = [e for e in raw.get("elements", []) if e["type"] == "way"]
    street = [w for w in ways if w.get("tags", {}).get("name") == name]
    street_nodes = {n for w in street for n in w.get("nodes", [])}
    crossing: dict[int, set[str]] = {}
    for w in ways:
        cross = w.get("tags", {}).get("name")
        if w in street or not cross:  # unnamed footways/service roads mostly duplicate a real corner
            continue
        for n in set(w.get("nodes", [])) & street_nodes:
            crossing.setdefault(n, set()).add(cross)
    out: list[dict[str, Any]] = []
    for n, names in crossing.items():
        if n not in nodes:
            continue
        lat, lng = nodes[n]
        near = next((c for c in out if c["kind"] == "corner" and _dist_m(lat, lng, c["lat"], c["lng"]) < CORNER_MERGE_M), None)
        if near:
            near["cross_streets"] = sorted(set(near["cross_streets"]) | names)
            continue
        out.append({"kind": "corner", "lat": lat, "lng": lng, "cross_streets": sorted(names)})
    for w in street:
        for n in (w["nodes"][0], w["nodes"][-1]) if w.get("nodes") else ():
            if n in nodes and n not in crossing and sum(n in v.get("nodes", []) for v in street) == 1:
                lat, lng = nodes[n]
                if all(_dist_m(lat, lng, c["lat"], c["lng"]) >= CORNER_MERGE_M for c in out):
                    out.append({"kind": "end", "lat": lat, "lng": lng, "cross_streets": []})
    for c in out:
        c["name"] = f"{name} × {' / '.join(c['cross_streets'])}" if c["cross_streets"] else f"{name} (end)"
    return out


def _dist_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(math.sqrt(h))


def street_corners(name: str, lat: float, lng: float, radius_m: int = 400) -> list[dict]:
    """Corners (intersections with other named streets) and dead ends of the street called exactly `name`
    near a point. Street name plaques hang on corner buildings, so the camera is usually close to one of
    these corners. Use the OSM name from a geocode result (e.g. "Rua de Rafael de Andrade") and its lat/lng.
    Each result has kind ("corner" | "end"), lat, lng, cross_streets, name and a call_id."""
    args = {"name": name.strip(), "lat": round(float(lat), 6), "lng": round(float(lng), 6),
            "radius_m": max(50, min(int(radius_m), 2000))}
    cfg = settings().geo
    query = (f"[out:json][timeout:{cfg.overpass_timeout_s}];"
             f"way[highway][name={_quote(args['name'])}](around:{args['radius_m']},{args['lat']},{args['lng']})->.s;"
             ".s out body;node(w.s)->.n;.n out skel;way[highway](bn.n)->.x;(.x; - .s;)->.o;.o out body;")
    value, call_id = _fetch("street_corners", args, "overpass", lambda raw: _corners(raw, args["name"]),
                            method="POST", url=cfg.overpass_url, data={"data": query},
                            timeout=cfg.overpass_timeout_s + 5)
    return [{**c, "call_id": call_id} for c in value]


def wiki_coords(query: str, lang: str = "pt") -> dict | None:
    """Search Wikipedia and return the best article with coordinates: title, lat, lng, url, call_id.
    None if no matching article has coordinates."""
    if not _LANG.match(lang):
        raise ValueError(f"lang must be a Wikipedia language code like 'pt', got {lang!r}")
    args = {"query": query.strip(), "lang": lang}
    params = {"action": "query", "format": "json", "formatversion": 2, "generator": "search",
              "gsrsearch": args["query"], "gsrlimit": 3, "prop": "coordinates|info", "inprop": "url"}

    def parse(raw: dict[str, Any]) -> dict | None:
        pages = sorted(raw.get("query", {}).get("pages", []), key=lambda p: p.get("index", 0))
        for p in pages:
            if p.get("coordinates"):
                c = p["coordinates"][0]
                return {"title": p["title"], "lat": float(c["lat"]), "lng": float(c["lon"]),
                        "url": p.get("fullurl")}
        return None

    value, call_id = _fetch("wiki_coords", args, "wikipedia", parse,
                            url=settings().geo.wiki_url.format(lang=lang), params=params)
    return {**value, "call_id": call_id} if value else None
