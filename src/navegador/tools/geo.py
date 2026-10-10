"""Geo tools for the Detetive: typed, docstrings, disk-cached, logged (SPEC §9.3). TODO(P0b · C).

Every result dict includes a `call_id` from `escrivao.register_tool_call`, which enters the run's
tool registry (R2). Nominatim policy: ≤1 req/s for the whole team, custom User-Agent
(settings().geo.user_agent), cache everything, no bulk/grid queries, no autocomplete.
"""

from __future__ import annotations


def geocode(query: str, country_codes: str = "pt,es,fr", limit: int = 5) -> list[dict]:
    """Nominatim /search."""
    raise NotImplementedError("P0b · C")


def reverse(lat: float, lng: float, zoom: int = 14) -> dict:
    """Nominatim /reverse."""
    raise NotImplementedError("P0b · C")


def overpass_near(lat: float, lng: float, osm_tag: str, radius_m: int = 500) -> list[dict]:
    """OSM features with `osm_tag` (e.g. amenity=place_of_worship) near a point. Cap 50, timeout 25 s."""
    raise NotImplementedError("P1 · C")


def overpass_named(name: str, bbox: str | None = None) -> list[dict]:
    """OSM features by name, optionally within a bbox."""
    raise NotImplementedError("P1 · C")


def wiki_coords(query: str, lang: str = "pt") -> dict | None:
    """Wikipedia search + coordinates."""
    raise NotImplementedError("P1 · C")
