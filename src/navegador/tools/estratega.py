"""Estratega: scoring math, best point under uncertainty, and the 10-slot submission (SPEC §1, §9.4).

Pure functions, no I/O. Repeating the single best point 10× maximises expected score (`ev` mode).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from navegador.config import regions, settings
from navegador.schemas import Candidate, Hypothesis, Label, Submission

EARTH_R_M = 6_371_008.8
SEED = 20261010
N_SLOTS = 10
_GRID_HALF_STEPS_MAX = 10

LatLng = tuple[float, float]


# ---------------------------------------------------------------- scoring


def haversine_m(a: LatLng, b: LatLng) -> float:
    return float(_haversine_np(np.array(a[0]), np.array(a[1]), np.array(b[0]), np.array(b[1])))


def _haversine_np(lat1: np.ndarray, lng1: np.ndarray, lat2: np.ndarray, lng2: np.ndarray) -> np.ndarray:
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(lng2 - lng1)
    h = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * np.arcsin(np.sqrt(np.clip(h, 0.0, 1.0)))


def _theta_np(d: np.ndarray, label: Label) -> np.ndarray:
    sc = settings().scoring
    d = np.where(d < sc.zero_distance_m, 0.0, d)
    return 2.0 / (1.0 + d / sc.theta_scale_m.get(label))


def theta(d: float, label: Label) -> float:
    """Distance score θ(d); d < 10 m counts as 0."""
    return float(_theta_np(np.asarray(d, dtype=float), label))


def rank_weights() -> list[float]:
    decay = settings().scoring.rank_decay
    return [decay**r for r in range(N_SLOTS)]


def score(
    cands: Sequence[LatLng],
    truth: LatLng,
    label: Label,
    penalty_factors_per_rank: Sequence[float] | None = None,
    special_one: bool = False,
) -> float:
    """Actual score of a 10-candidate list against a known truth."""
    pf = penalty_factors_per_rank or [1.0] * len(cands)
    total = sum(1000 * w * theta(haversine_m(c, truth), label) * p
                for w, c, p in zip(rank_weights(), cands, pf, strict=False))
    return total * (settings().scoring.special_one_multiplier if special_one else 1.0)


# ---------------------------------------------------------------- uncertainty


def _normalised_weights(hyps: Sequence[Hypothesis]) -> np.ndarray:
    w = np.array([h.weight for h in hyps], dtype=float)
    return w / w.sum() if w.sum() > 0 else np.full(len(hyps), 1.0 / len(hyps))


def _offset(lat: np.ndarray, lng: np.ndarray, north_m: np.ndarray, east_m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    dlat = np.degrees(north_m / EARTH_R_M)
    dlng = np.degrees(east_m / (EARTH_R_M * np.cos(np.radians(lat))))
    return lat + dlat, lng + dlng


def sample_truth(hyps: Sequence[Hypothesis], samples: int, seed: int = SEED) -> tuple[np.ndarray, np.ndarray]:
    """Monte Carlo draws from a mixture of isotropic Gaussians (σ = radius_m).

    Component counts are allocated by weight (stratified, largest remainder) rather than drawn,
    which removes the mixture-selection noise from E[θ].
    """
    rng = np.random.default_rng(seed)
    exact = _normalised_weights(hyps) * samples
    counts = np.floor(exact).astype(int)
    counts[np.argsort(-(exact - counts))[: samples - counts.sum()]] += 1
    idx = np.repeat(np.arange(len(hyps)), counts)
    lat = np.array([hyps[i].lat for i in idx])
    lng = np.array([hyps[i].lng for i in idx])
    sigma = np.array([hyps[i].radius_m for i in idx])
    return _offset(lat, lng, rng.normal(size=samples) * sigma, rng.normal(size=samples) * sigma)


def _expected_theta_many(points: np.ndarray, truth: tuple[np.ndarray, np.ndarray], label: Label) -> np.ndarray:
    out = np.empty(len(points))
    for start in range(0, len(points), 256):  # chunked to bound memory
        p = points[start:start + 256]
        d = _haversine_np(p[:, :1], p[:, 1:], truth[0][None, :], truth[1][None, :])
        out[start:start + 256] = _theta_np(d, label).mean(axis=1)
    return out


def expected_theta(point: LatLng, hypotheses: Sequence[Hypothesis], label: Label, samples: int | None = None) -> float:
    if not hypotheses:
        return 0.0
    truth = sample_truth(hypotheses, samples or settings().estratega.mc_samples)
    return float(_expected_theta_many(np.array([point], dtype=float), truth, label)[0])


def _grid(h: Hypothesis, step_m: float) -> np.ndarray:
    k = int(min(max(np.ceil(h.radius_m / step_m), 1), _GRID_HALF_STEPS_MAX))
    steps = np.arange(-k, k + 1) * step_m
    north, east = np.meshgrid(steps, steps)
    lat, lng = _offset(np.full(north.size, h.lat), np.full(north.size, h.lng), north.ravel(), east.ravel())
    return np.column_stack([lat, lng])


def best_point(hypotheses: Sequence[Hypothesis], label: Label) -> tuple[float, float, float] | None:
    """Argmax of E[θ] over hypothesis centres + a local grid around the top 3. Deterministic (fixed seed)."""
    if not hypotheses:
        return None
    est = settings().estratega
    truth = sample_truth(hypotheses, est.mc_samples)
    top3 = sorted(hypotheses, key=lambda h: -h.weight)[:3]
    cands = [np.array([[h.lat, h.lng] for h in hypotheses])]
    cands += [_grid(h, est.grid_step_m.get(label)) for h in top3]
    points = np.vstack(cands)
    inside = np.array([regions().contains(lat, lng) for lat, lng in points])
    if inside.any():
        points = points[inside]
    else:
        points = np.array([regions().clamp(lat, lng) for lat, lng in points])
    e = _expected_theta_many(points, truth, label)
    i = int(np.argmax(e))
    return float(points[i, 0]), float(points[i, 1]), float(e[i])


# ---------------------------------------------------------------- clustering


@dataclass
class Cluster:
    cluster_id: str
    members: list[Hypothesis]
    weight: float  # normalised over all clusters
    lat: float
    lng: float


def clusters(hypotheses: Sequence[Hypothesis], eps_m: float) -> list[Cluster]:
    """Single-linkage clustering by Haversine distance; sorted by weight, descending."""
    if not hypotheses:
        return []
    n = len(hypotheses)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = hypotheses[i], hypotheses[j]
            if haversine_m((a.lat, a.lng), (b.lat, b.lng)) <= eps_m:
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    w = _normalised_weights(hypotheses)
    out = []
    for members in groups.values():
        cw = float(w[members].sum())
        mw = w[members] / cw if cw > 0 else np.full(len(members), 1.0 / len(members))
        out.append(Cluster("", [hypotheses[i] for i in members], cw,
                           float(sum(mw[k] * hypotheses[i].lat for k, i in enumerate(members))),
                           float(sum(mw[k] * hypotheses[i].lng for k, i in enumerate(members)))))
    out.sort(key=lambda c: -c.weight)
    for k, c in enumerate(out):
        c.cluster_id = f"c{k}"
    return out


# ---------------------------------------------------------------- submission


def submit(state, fallback: LatLng | None = None) -> Submission:  # state: navegador.core.state.State
    """Fill exactly 10 valid slots (R1).

    `fallback` is the geocoded centroid of the Geógrafo's top region, supplied by the pipeline
    (Estratega stays I/O-free); used only when there are no hypotheses. Else `fallback_point`.
    """
    sc, est = settings().scoring, settings().estratega
    hyps, label = state.hypotheses, state.label
    best = best_point(hyps, label)
    if best is None:
        point = regions().clamp(*(fallback or regions().fallback_point))
        points = [point] * N_SLOTS
        e_theta = [0.0] * N_SLOTS
    elif est.mode == "hedge":
        cl = clusters(hyps, settings().mcq.cluster_eps_m.get(label))
        second = best_point(cl[1].members, label) if len(cl) > 1 else None
        second = second or best
        half = N_SLOTS // 2
        points = [best[:2]] * half + [second[:2]] * (N_SLOTS - half)
        e_theta = [best[2] if i < half else expected_theta(second[:2], hyps, label) for i in range(N_SLOTS)]
    else:
        points, e_theta = [best[:2]] * N_SLOTS, [best[2]] * N_SLOTS

    pf = 1.0
    for p in state.penalties:
        pf *= p.factor
    # [CONFIRM] Q5: all penalties issued before submission apply to every rank (commit_then_ask=false).
    expected = 1000 * sum(w * e * pf for w, e in zip(rank_weights(), e_theta, strict=True))
    if state.special_one:
        expected *= sc.special_one_multiplier
    cands = [Candidate(rank=r + 1, lat=round(lat, 6), lng=round(lng, 6)) for r, (lat, lng) in enumerate(points)]
    for c in cands:
        if not regions().contains(c.lat, c.lng):
            raise ValueError(f"R1 violated: candidate {c} outside config/regions.yaml")
    return Submission(pano_id=state.pano_id, filename=state.pano_path.name, label=label, mode=state.mode,
                      candidates=cands, penalty_factor=pf, expected_score=expected, special_one=state.special_one)
