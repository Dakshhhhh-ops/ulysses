from statistics import NormalDist

import pytest

from ulysses.decision import Observation, evaluate, observation_from_snapshot
from ulysses.flagsmith.gateway import MetricResult, ResultsSnapshot

from .conftest import make_contract

PHI = NormalDist().cdf


def test_between_looks_is_watch_and_spends_nothing(keys):
    c = make_contract(keys, n=1000, looks=10)
    d = evaluate(c, [], Observation(50, 60, 0.99))
    assert d.outcome == "watch" and not d.counted
    assert d.next_look_at_identities == 100


def test_banner_winning_but_contract_holds(keys):
    c = make_contract(keys, n=1000, looks=10)
    # Flagsmith would show "Winning" (chance over 95%); look 3 of 10 needs far more.
    d = evaluate(c, [0.1, 0.2], Observation(300, 305, 0.97))
    assert d.outcome == "hold" and d.counted and d.look_number == 3
    assert d.boundary_chance > 0.999
    assert "Hold" in d.reason


def test_crossing_fires_win_and_names_the_signed_action(keys):
    c = make_contract(keys, n=1000, looks=10)
    d = evaluate(c, [0.1, 0.2], Observation(300, 300, PHI(4.0), {"refunds": 0.4}))
    assert d.outcome == "win" and d.action == "ship_treatment"
    assert d.guardrails[0]["state"] == "pass" and d.guardrails[0]["chance_worse"] == 0.4


def test_win_does_not_ship_while_a_guardrail_has_no_result(keys):
    c = make_contract(keys, n=1000, looks=10)
    d = evaluate(c, [0.1, 0.2], Observation(300, 300, PHI(4.0), {"refunds": None}))
    assert d.outcome == "hold" and "no Flagsmith result" in d.reason
    final = evaluate(c, [k / 10 for k in range(1, 10)], Observation(1000, 1000, PHI(4.0), {"refunds": None}))
    assert final.outcome == "horizon" and final.action == "keep_control"


def test_win_ships_without_guardrail_result_only_if_contract_says_so(keys):
    c = make_contract(keys, n=1000, looks=10, require_guardrail_results_to_ship=False)
    d = evaluate(c, [0.1, 0.2], Observation(300, 300, PHI(4.0), {"refunds": None}))
    assert d.outcome == "win" and "no Flagsmith result yet" in d.reason


def test_loss_keeps_control(keys):
    c = make_contract(keys, n=1000, looks=10)
    d = evaluate(c, [0.1, 0.2, 0.3, 0.4], Observation(500, 500, PHI(-3.5)))
    assert d.outcome == "loss" and d.action == "keep_control"


def test_horizon_without_crossing_is_no_decision(keys):
    c = make_contract(keys, n=1000, looks=10)
    prior = [k / 10 for k in range(1, 10)]
    d = evaluate(c, prior, Observation(1000, 1003, 0.96))  # banner says Winning; final boundary near 97.9%
    assert d.outcome == "horizon" and d.action == "keep_control"


def test_guardrail_never_stops_between_looks(keys):
    c = make_contract(keys, n=1000, looks=10)
    d = evaluate(c, [], Observation(40, 40, 0.5, {"refunds": 0.9999}))
    assert d.outcome == "watch" and d.guardrails[0]["state"] == "watch"


def test_guardrail_breach_at_a_counted_look_uses_the_signed_spending_boundary(keys):
    from ulysses import boundary as bd

    c = make_contract(keys, n=1000, looks=10)
    b = bd.one_sided_boundaries([0.1, 0.2, 0.3], 0.05, final_spends_all=False)[-1]
    just_under = evaluate(c, [0.1, 0.2], Observation(300, 300, 0.6, {"refunds": PHI(b - 0.01)}))
    assert just_under.outcome == "hold" and just_under.guardrails[0]["state"] == "pass"
    over = evaluate(c, [0.1, 0.2], Observation(300, 300, 0.6, {"refunds": PHI(b + 0.01)}))
    assert over.outcome == "guardrail_breach" and over.action == "keep_control" and over.guardrail == "refunds"
    assert over.guardrails[0]["boundary_z"] == pytest.approx(b, abs=1e-3)


def test_not_ready_is_not_counted(keys):
    c = make_contract(keys, n=1000, looks=10)
    d = evaluate(c, [0.1], Observation(200, 200, None))
    assert d.outcome == "not_ready" and not d.counted


def test_unreachable_first_look(keys):
    c = make_contract(keys, n=3000, looks=30)
    d = evaluate(c, [], Observation(100, 100, 0.999999))
    assert d.outcome == "hold" and d.boundary_z is None
    assert "no stop is possible" in d.reason


def test_snapshot_orientation_for_lower_is_better_guardrail(keys):
    c = make_contract(keys)
    # Flagsmith reports refunds (lower is better) with chance_to_win = 1 - Phi(lift/SE).
    # A 99% chance that refunds rose shows up there as chance_to_win 0.01.
    snap = ResultsSnapshot(
        identities={"control": 400, "big_cta": 410},
        metrics={"purchase": MetricResult("purchase", "up", 0.7), "refunds": MetricResult("refunds", "down", 0.01)},
    )
    obs = observation_from_snapshot(c, snap)
    assert obs.chance_to_win == pytest.approx(0.7)
    assert obs.guardrail_chance_worse["refunds"] == pytest.approx(0.99)
    assert obs.identities_control == 400 and obs.identities_treatment == 410


def test_snapshot_for_decrease_goal(keys):
    from ulysses import contract as ct

    c = make_contract(keys)
    data = c.canonical()
    data["primary_metric"]["direction"] = "decrease"
    c2 = ct.from_canonical(data)
    snap = ResultsSnapshot(identities={"control": 1, "big_cta": 1},
                           metrics={"purchase": MetricResult("purchase", "down", 0.9)})
    # Flagsmith already oriented a 'down' metric so 0.9 = 90% chance it fell, which is what the contract wants.
    assert observation_from_snapshot(c2, snap).chance_to_win == pytest.approx(0.9)


def test_sequence_of_looks_on_exact_planned_counts(keys):
    """Looks landing exactly on planned identity counts are each counted once, in order."""
    c = make_contract(keys, n=3000, looks=30)
    prior: list[float] = []
    for k in range(1, 31):
        n = 100 * k
        d = evaluate(c, prior, Observation(n, n, 0.5))
        assert d.counted and d.look_number == k, (k, d.outcome)
        # A second refresh with no new identities is not another look.
        again = evaluate(c, prior + [d.fraction], Observation(n, n, 0.5))
        assert not again.counted or again.outcome == "horizon"
        if d.outcome == "horizon":
            assert k == 30
            break
        prior.append(d.fraction)
