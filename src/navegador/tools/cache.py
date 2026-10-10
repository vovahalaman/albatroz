"""Disk cache for external calls (R5).

Key = sha256 of (namespace, args); value = JSON under cache/<namespace>/<key>.json.
Entries never expire. Callers cache successful results only (empty results included), never errors.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from navegador.config import cache_dir

_SAFE_NS = re.compile(r"[^A-Za-z0-9_.-]")


def key_hash(namespace: str, key: dict[str, Any]) -> str:
    blob = json.dumps([namespace, key], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def path_for(namespace: str, key: dict[str, Any]) -> Path:
    return cache_dir() / _SAFE_NS.sub("_", namespace) / f"{key_hash(namespace, key)}.json"


def lookup(namespace: str, key: dict[str, Any]) -> tuple[bool, Any]:
    """Return (hit, value). Distinguishes a cached `None` from a miss."""
    path = path_for(namespace, key)
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None
    return True, entry.get("value")


def get(namespace: str, key: dict[str, Any]) -> Any | None:
    return lookup(namespace, key)[1]


def put(namespace: str, key: dict[str, Any], value: Any) -> None:
    """Atomically write one entry (tmp file + rename)."""
    path = path_for(namespace, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"namespace": namespace, "key": key, "value": value},
                              ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def cached(namespace: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: memoise a JSON-returning function on disk, keyed by its bound arguments."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            key = dict(bound.arguments)
            hit, value = lookup(namespace, key)
            if hit:
                return value
            value = fn(*args, **kwargs)
            put(namespace, key, value)
            return value

        return wrapper

    return deco
