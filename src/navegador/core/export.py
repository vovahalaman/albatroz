"""Submission exports (SPEC §11)."""

from __future__ import annotations

import math

from navegador.config import regions
from navegador.schemas import Submission


def validate(submission: Submission) -> None:
    """R1: exactly 10 numeric (lat, lng) pairs, all inside config/regions.yaml. Raise ValueError otherwise."""
    cands = submission.candidates
    if len(cands) != 10:
        raise ValueError(f"R1: expected 10 candidates, got {len(cands)}")
    if [c.rank for c in cands] != list(range(1, 11)):
        raise ValueError(f"R1: ranks must be 1..10 in order, got {[c.rank for c in cands]}")
    for c in cands:
        if not (math.isfinite(c.lat) and math.isfinite(c.lng)):
            raise ValueError(f"R1: non-numeric candidate {c}")
        if not regions().contains(c.lat, c.lng):
            raise ValueError(f"R1: candidate {c} is outside config/regions.yaml")


def to_text(submission: Submission) -> str:
    """One `lat, lng` per line, 6 decimals, rank order."""
    return "\n".join(f"{c.lat:.6f}, {c.lng:.6f}" for c in sorted(submission.candidates, key=lambda c: c.rank)) + "\n"


def to_json(submission: Submission) -> str:
    return submission.model_dump_json(indent=2)


def to_csv(submission: Submission) -> str:
    """Header `rank,lat,lng`."""
    rows = [f"{c.rank},{c.lat:.6f},{c.lng:.6f}" for c in sorted(submission.candidates, key=lambda c: c.rank)]
    return "\n".join(["rank,lat,lng", *rows]) + "\n"


def to_albatroz(submission: Submission) -> str:
    """[CONFIRM format on Discord during the trial]. Placeholder: defaults to `to_json`."""
    return to_json(submission)
