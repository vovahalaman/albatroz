"""Disk cache for external calls (R5). TODO(P0b · C).

Key = sha256 of (namespace, args); value = JSON under cache/<namespace>/<key>.json.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def get(namespace: str, key: dict[str, Any]) -> Any | None:
    raise NotImplementedError("P0b · C")


def put(namespace: str, key: dict[str, Any], value: Any) -> None:
    raise NotImplementedError("P0b · C")


def cached(namespace: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: memoise a JSON-returning function on disk."""
    raise NotImplementedError("P0b · C")
