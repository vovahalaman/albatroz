"""S0 EXIF/XMP probe (R8): read and log. GPS is returned but only used if `flags.use_exif_gps`."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image

MAX_TAGS = 60
_XMP_ATTR = re.compile(r'(GPano|exif|tiff|xmp):(\w+)="([^"]{0,200})"')
_XMP_ELEM = re.compile(r"<(GPano):(\w+)>([^<]{0,200})</")


def _dms(value: Any) -> float:
    d, m, s = (float(x) for x in value)
    return d + m / 60 + s / 3600


def _gps(ifd: dict) -> dict[str, float] | None:
    try:
        lat, lng = _dms(ifd[2]), _dms(ifd[4])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    lat = -lat if str(ifd.get(1, "N")).upper().startswith("S") else lat
    lng = -lng if str(ifd.get(3, "E")).upper().startswith("W") else lng
    return {"lat": round(lat, 7), "lng": round(lng, 7)}


def _xmp(raw: bytes | str | None) -> dict[str, str]:
    if not raw:
        return {}
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    found = {f"{ns}:{k}": v for ns, k, v in _XMP_ATTR.findall(text)}
    found.update({f"{ns}:{k}": v for ns, k, v in _XMP_ELEM.findall(text)})
    return dict(list(found.items())[:MAX_TAGS])


def probe(path: Path) -> dict[str, Any]:
    """Return {tags, gps, xmp} (all JSON-safe, values truncated). Never raises."""
    out: dict[str, Any] = {"tags": {}, "gps": None, "xmp": {}}
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            items = list(exif.items()) + list(exif.get_ifd(ExifTags.IFD.Exif).items())
            for key, value in items:
                name = ExifTags.TAGS.get(key, str(key))
                if name in ("MakerNote", "ExifOffset", "GPSInfo") or isinstance(value, bytes):
                    continue
                if len(out["tags"]) < MAX_TAGS:
                    out["tags"][name] = str(value)[:120]
            out["gps"] = _gps(dict(exif.get_ifd(ExifTags.IFD.GPSInfo)))
            out["xmp"] = _xmp(im.info.get("xmp"))
    except Exception as exc:  # noqa: BLE001 - a probe must never break S0
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
