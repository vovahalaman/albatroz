"""Submission exports (SPEC §11). TODO(P0b · D)."""

from __future__ import annotations

from navegador.schemas import Submission


def validate(submission: Submission) -> None:
    """R1: exactly 10 numeric (lat, lng) pairs, all inside config/regions.yaml. Raise ValueError otherwise."""
    raise NotImplementedError("P0b · D")


def to_text(submission: Submission) -> str:
    """One `lat, lng` per line, 6 decimals, rank order."""
    raise NotImplementedError("P0b · D")


def to_json(submission: Submission) -> str:
    raise NotImplementedError("P0b · D")


def to_csv(submission: Submission) -> str:
    """Header `rank,lat,lng`."""
    raise NotImplementedError("P0b · D")


def to_albatroz(submission: Submission) -> str:
    """[CONFIRM format on Discord during the trial]. Placeholder: defaults to `to_json`."""
    return to_json(submission)
