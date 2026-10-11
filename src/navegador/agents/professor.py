"""Professor (P2, between runs only): proposes knowledge updates from reviewed runs (SPEC §10).

Input: runs/<id>/IMG_20261005_171832_00_163_log.json + review.md. Output: markdown diff proposal for knowledge/lessons.md and
confusion_pairs.md. General rules only, never "panorama X is in Y".
"""

from __future__ import annotations


async def propose(pano_id: str) -> str:
    raise NotImplementedError("P2")
