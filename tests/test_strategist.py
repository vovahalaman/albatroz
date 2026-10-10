import pytest

from navegador.core.state import new_state
from navegador.schemas import Hypothesis, Penalty
from navegador.tools import strategist as E

LISBOA, PORTO = (38.7223, -9.1393), (41.1579, -8.6291)
DISTANCES = [5, 100, 500, 1_000, 5_000, 10_000, 50_000]
THETA_URBAN = [2.000, 1.000, 0.333, 0.182, 0.039, 0.020, 0.004]
THETA_NON_URBAN = [2.000, 1.980, 1.905, 1.818, 1.333, 1.000, 0.333]


def hyp(lat, lng, weight, radius_m=1.0, label="h"):
    return Hypothesis(lat=lat, lng=lng, radius_m=radius_m, weight=weight, label=label, provenance="estimated",
                      source="test")


def state_with(hyps, label="urban", **kw):
    s = new_state("panos/20261010140000.jpg", label=label, mode="autonomous", **kw)
    s.hypotheses = hyps
    return s


@pytest.mark.parametrize("d,urban,non_urban", list(zip(DISTANCES, THETA_URBAN, THETA_NON_URBAN)))
def test_theta_table(d, urban, non_urban):
    assert E.theta(d, "urban") == pytest.approx(urban, abs=5e-4)
    assert E.theta(d, "non_urban") == pytest.approx(non_urban, abs=5e-4)


def test_max_score():
    assert sum(E.rank_weights()) == pytest.approx(8.02526, abs=1e-5)
    assert E.score([LISBOA] * 10, LISBOA, "urban") == pytest.approx(16_050.5, abs=0.1)
    assert E.score([LISBOA] * 10, LISBOA, "urban", special_one=True) == pytest.approx(3 * 16_050.5, abs=0.3)


def test_haversine_lisboa_porto():
    assert E.haversine_m(LISBOA, PORTO) / 1000 == pytest.approx(274.3, abs=0.1)


def test_ev_returns_ten_identical_pairs():
    sub = E.submit(state_with([hyp(*LISBOA, 0.6), hyp(*PORTO, 0.4)]))
    assert len(sub.candidates) == 10
    assert len({(c.lat, c.lng) for c in sub.candidates}) == 1
    assert [c.rank for c in sub.candidates] == list(range(1, 11))


def test_empty_hypotheses_still_ten_valid_pairs():
    sub = E.submit(state_with([]))
    assert len(sub.candidates) == 10
    assert all(E.regions().contains(c.lat, c.lng) for c in sub.candidates)
    assert (sub.candidates[0].lat, sub.candidates[0].lng) == tuple(E.regions().fallback_point)


def test_out_of_region_hypothesis_is_clamped():
    sub = E.submit(state_with([hyp(48.0, -20.0, 1.0, radius_m=50)]))  # Atlantic
    assert all(E.regions().contains(c.lat, c.lng) for c in sub.candidates)


def test_best_point_in_heavier_cluster():
    hyps = [hyp(*LISBOA, 0.3, 150), hyp(LISBOA[0] + 0.001, LISBOA[1], 0.3, 150), hyp(*PORTO, 0.4, 150)]
    lat, lng, _ = E.best_point(hyps, "urban")
    assert E.haversine_m((lat, lng), LISBOA) < 1_000


def test_expected_score_matches_spec_examples():
    hyps = [hyp(*LISBOA, 0.6), hyp(*PORTO, 0.4)]
    assert E.submit(state_with(hyps)).expected_score == pytest.approx(9_630, abs=5)
    E.settings().strategist.mode = "hedge"
    try:
        assert E.submit(state_with(hyps)).expected_score == pytest.approx(8_230, abs=5)
    finally:
        E.settings().strategist.mode = "ev"


def test_penalties_and_special_one_scale_expected_score():
    s = state_with([hyp(*LISBOA, 1.0)], special_one=True)
    s.penalties = [Penalty(kind="mcq", factor=0.95, ts=s.created_at, ref="m1")]
    sub = E.submit(s)
    assert sub.penalty_factor == pytest.approx(0.95)
    assert sub.expected_score == pytest.approx(16_050.5 * 0.95 * 3, rel=1e-4)


def test_clusters():
    hyps = [hyp(*LISBOA, 0.3), hyp(LISBOA[0] + 0.001, LISBOA[1], 0.3), hyp(*PORTO, 0.4)]
    cl = E.clusters(hyps, eps_m=300)
    assert [round(c.weight, 2) for c in cl] == [0.6, 0.4]
    assert len(cl[0].members) == 2


def test_best_point_is_deterministic():
    hyps = [hyp(*LISBOA, 0.5, 400), hyp(*PORTO, 0.5, 400)]
    assert E.best_point(hyps, "urban") == E.best_point(hyps, "urban")
