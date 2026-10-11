"""Google Maps links and the Street View embed for a coordinate (Maps Embed API).

The embed key comes from GOOGLE_MAPS_EMBED_KEY in .env (loaded by navegador.config). Before embedding,
the Street View metadata endpoint (free, same key) checks coverage near the point: with imagery the embed
shows that panorama; without it the embed falls back to a satellite map with a note. If coverage cannot be
checked (for example the key does not allow the Street View Static API), the Street View embed is shown as is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlencode

import requests

EMBED = "https://www.google.com/maps/embed/v1"
METADATA = "https://maps.googleapis.com/maps/api/streetview/metadata"
RADIUS_M = {"urban": 100, "non_urban": 1000}  # how far from the point to look for Street View imagery
TIMEOUT_S = 5
ENABLE_HINT = ("Street View preview is off. To enable it, add GOOGLE_MAPS_EMBED_KEY=<key> (Maps Embed API) "
               "to .env and restart the UI.")


@dataclass(frozen=True)
class Embed:
    url: str
    kind: str  # "streetview" | "satellite"
    note: str = ""


def embed_key() -> str | None:
    return os.getenv("GOOGLE_MAPS_EMBED_KEY") or None


def streetview_link(lat: float, lng: float) -> str:
    return f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat:.6f},{lng:.6f}"


def maps_link(lat: float, lng: float) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lng:.6f}"


@lru_cache(maxsize=512)
def coverage(lat: float, lng: float, radius_m: int, key: str) -> dict:
    """Street View metadata near the point: {"status": "OK" | "ZERO_RESULTS" | ... , "pano_id"?, "location"?}."""
    try:
        r = requests.get(METADATA, params={"location": f"{lat:.6f},{lng:.6f}", "radius": radius_m, "key": key},
                         timeout=TIMEOUT_S)
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        return {"status": "UNAVAILABLE", "error_message": type(exc).__name__}


def embed_for(lat: float, lng: float, label: str, key: str) -> Embed:
    radius = RADIUS_M.get(label, 100)
    meta = coverage(round(lat, 6), round(lng, 6), radius, key)
    status = meta.get("status")
    if status == "OK" and meta.get("pano_id"):
        return Embed(f"{EMBED}/streetview?" + urlencode({"key": key, "pano": meta["pano_id"], "fov": 90}), "streetview")
    if status == "ZERO_RESULTS":
        url = f"{EMBED}/view?" + urlencode({"key": key, "center": f"{lat:.6f},{lng:.6f}", "zoom": 18,
                                           "maptype": "satellite"})
        return Embed(url, "satellite", f"No Street View within {radius} m of this point: satellite view instead.")
    url = f"{EMBED}/streetview?" + urlencode({"key": key, "location": f"{lat:.6f},{lng:.6f}", "radius": radius,
                                             "fov": 90})
    return Embed(url, "streetview")  # coverage unknown: no note (only confirmed absence gets one)
