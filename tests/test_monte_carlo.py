"""Every boundary family Ulysses signs, checked against Monte Carlo with fixed seeds.

The integration in ``boundary.py`` is exact for the canonical joint distribution of sequential z
statistics; these tests simulate that distribution directly and require agreement within four
standard errors (plus 5e-4 for grid error). Fixed seeds keep the suite deterministic and fast.
"""

import math

import numpy as np
import pytest

from ulysses import boundary as bd
from ulysses import contract as ct
from ulysses import power, srm

REPS = 200_000


def paths(rng, ts, reps=REPS, theta=0.0):
    ts = np.asarray(ts, dtype=float)
    dt = np.diff(np.concatenate([[0.0], ts]))
    s = np.cumsum(rng.standard_normal((reps, len(ts))) * np.sqrt(dt) + theta * dt, axis=1)
    return s / np.sqrt(ts)


def first_crossing(z, b):
    """(upper rate, lower rate, mean stop fraction index) for two-sided absorbing boundaries."""
    hit = np.abs(z) >= b
    stopped = hit.any(axis=1)
    first = np.argmax(hit, axis=1)
    sign = z[np.arange(len(z)), first] > 0
    return float(np.mean(stopped & sign)), float(np.mean(stopped & ~sign)), np.where(stopped, first, z.shape[1] - 1)


def tol(p, reps=REPS):
    return 4 * math.sqrt(max(p, 1e-4) * (1 - p) / reps) + 5e-4


def as_array(bs):
    return np.array([np.inf if b is None else b for b in bs], dtype=float)


TWO_SIDED = [
    ([k / 2 for k in range(1, 3)], 0.05),
    ([k / 5 for k in range(1, 6)], 0.05),
    ([k / 12 for k in range(1, 13)], 0.10),
    ([k / 30 for k in range(1, 31)], 0.05),
    ([k / 6 for k in range(1, 7)], 0.05 / 3),       # one comparison of a three-treatment contract
    ([0.13, 0.29, 0.31, 0.58, 0.77, 0.94], 0.05),     # looks landing off schedule, short of the horizon
]


@pytest.mark.parametrize("ts, alpha", TWO_SIDED)
def test_two_sided_obrien_fleming_null_rates_match_simulation(ts, alpha):
    rng = np.random.default_rng(101)
    bs = bd.spending_boundaries(ts, alpha)
    up, low = bd.crossing_probabilities(ts, bs, 0.0)
    mc_up, mc_low, _ = first_crossing(paths(rng, ts), as_array(bs))
    assert sum(up) == pytest.approx(alpha / 2, abs=1e-6)
    assert mc_up == pytest.approx(alpha / 2, abs=tol(alpha / 2))
    assert mc_low == pytest.approx(alpha / 2, abs=tol(alpha / 2))


def test_signed_table_rows_hold_their_level_in_simulation():
    """The rounded z values written into a contract (4 places) still spend alpha/2 per side."""
    h = ct.Horizon(identities_per_arm=3000, looks=30)
    rows = ct.planned_table(h, 0.05)
    ts = [r.fraction for r in rows]
    bs = as_array([r.z for r in rows])
    mc_up, mc_low, _ = first_crossing(paths(np.random.default_rng(7), ts), bs)
    assert mc_up == pytest.approx(0.025, abs=tol(0.025))
    assert mc_low == pytest.approx(0.025, abs=tol(0.025))


@pytest.mark.parametrize("theta", [1.5, 2.8, 4.0])
def test_power_and_expected_stop_match_simulation(theta):
    ts = [k / 10 for k in range(1, 11)]
    bs = bd.spending_boundaries(ts, 0.05)
    w, l, f = power.analytic(ts, bs, theta)
    z = paths(np.random.default_rng(int(theta * 10)), ts, theta=theta)
    mc_up, mc_low, first = first_crossing(z, as_array(bs))
    mc_f = float(np.mean(np.asarray(ts)[first]))
    assert mc_up == pytest.approx(w, abs=tol(w))
    assert mc_low == pytest.approx(l, abs=tol(l))
    assert mc_f == pytest.approx(f, abs=4 * float(np.std(np.asarray(ts)[first])) / math.sqrt(REPS) + 1e-3)


@pytest.mark.parametrize("looks, alpha", [(6, 0.01), (12, 0.01 / 3), (30, 0.01), (30, 0.05)])
def test_one_sided_pocock_guardrail_boundaries_match_simulation(looks, alpha):
    ts = [k / looks for k in range(1, looks + 1)]
    bs = as_array(bd.one_sided_boundaries(ts, alpha))
    z = paths(np.random.default_rng(looks), ts)
    mc = float(np.mean((z >= bs).any(axis=1)))
    assert mc == pytest.approx(alpha, abs=tol(alpha))


def test_lower_boundary_is_non_binding_for_one_arm_of_many():
    """With several treatments an arm below its lower boundary keeps running; its chance of a later
    upper crossing is the one-sided crossing probability of the same boundaries, and it stays at
    alpha/2 to within the grid."""
    ts = [k / 12 for k in range(1, 13)]
    bs = bd.spending_boundaries(ts, 0.05 / 3)
    exact = bd.one_sided_crossing(ts, bs)
    z = paths(np.random.default_rng(3), ts)
    mc = float(np.mean((z >= as_array(bs)).any(axis=1)))
    assert mc == pytest.approx(exact, abs=tol(exact))
    assert exact == pytest.approx(0.05 / 6, abs=2e-5)


@pytest.mark.parametrize("m", [2, 3, 5])
def test_bonferroni_keeps_false_ships_under_half_alpha_with_a_shared_control(m):
    ts = [k / 10 for k in range(1, 11)]
    split = bd.spending_boundaries(ts, 0.05 / m)
    whole = bd.spending_boundaries(ts, 0.05)
    s = power.simulate(ts, split, [0.0], m, runs=REPS, seed=11)[0]
    any_ship = s["win_effective"] + s["win_other"]
    assert any_ship <= 0.025 + tol(0.025)
    # And without the split the false-ship rate is well over alpha/2: the correction is needed.
    u = power.simulate(ts, whole, [0.0], m, runs=REPS, seed=11)[0]
    assert u["win_effective"] + u["win_other"] > 0.025 + 4 * tol(0.025)


def test_srm_false_alarm_rate_and_planted_split():
    rng = np.random.default_rng(5)
    # A correct 50/50 split read once: p < 0.01 about 1% of the time.
    n = 2000
    a = rng.binomial(n, 0.5, size=40_000)
    ps = np.array([srm.chi_square([int(x), n - int(x)], [0.5, 0.5])[1] for x in a[:20_000]])
    assert float(np.mean(ps < 0.01)) == pytest.approx(0.01, abs=4 * math.sqrt(0.01 * 0.99 / 20_000) + 2e-3)
    # A planted 54/46 split at 4,000 identities is caught at the default threshold nearly always.
    n = 4000
    b = rng.binomial(n, 0.54, size=2_000)
    caught = np.mean([srm.chi_square([int(x), n - int(x)], [0.5, 0.5])[1] < ct.SRM_P_THRESHOLD for x in b])
    assert caught > 0.9


def test_decision_evaluate_matches_simulation_path_by_path_for_three_arms_under_the_null():
    """Drive the engine itself (``decision.evaluate``) over null paths of three treatments sharing one
    control. Its outcome must agree with ``power.simulate``'s rule on every path, and its false-ship rate
    must stay under alpha/2. Pins the per-comparison upper and lower boundaries at alpha/m."""
    from statistics import NormalDist

    from ulysses.decision import Observation, evaluate
    from ulysses.signing import Keypair

    arms = ["big_cta", "wide_cta", "green_cta"]
    looks, n, runs = 6, 1200, 3000
    keys = {k: Keypair.generate() for k in ("maya", "sam")}
    c = ct.draft(
        experiment=ct.ExperimentRef(project=1, environment="env-key", experiment=1, flag="checkout_button",
                                    treatment=arms[0], more_treatments=arms[1:]),
        hypothesis="One of three buttons lifts purchases",
        primary_metric=ct.Metric(name="purchase", direction="increase"),
        guardrails=[ct.Guardrail(name="refunds", direction="not_increase")],
        horizon=ct.Horizon(identities_per_arm=n, looks=looks),
        owner=ct.Signer(name="maya", public_key=keys["maya"].public),
        second_signer=ct.Signer(name="sam", public_key=keys["sam"].public),
    )
    ts = [k / looks for k in range(1, looks + 1)]
    z = power.null_z_paths(ts, 3, runs=runs, seed=29)
    ref = power.path_outcomes(z, bd.spending_boundaries(ts, c.comparison_alpha))
    phi = NormalDist().cdf
    neutral = {a: {"refunds": 0.5} for a in arms}
    ships = 0
    for i in range(runs):
        prior: list[float] = []
        for j in range(looks):
            m = n * (j + 1) // looks
            ids = {"control": m, **{a: m for a in arms}}
            chances = {a: phi(float(z[i, ai, j])) for ai, a in enumerate(arms)}
            d = evaluate(c, prior, Observation(m, m, chances[arms[0]], identities=ids, arm_chance=chances,
                                               arm_guardrail_worse=neutral))
            assert d.counted
            if d.fires:
                break
            prior.append(d.fraction)
        stopped_at = j
        if ref["won"][i]:
            assert d.outcome == "win" and d.winner == arms[int(ref["winner"][i])], i
            ships += 1
        elif ref["lost"][i]:
            assert d.outcome == "loss", i
        else:
            assert d.outcome == "horizon", i
        assert stopped_at == int(ref["first"][i]), i
    assert ships / runs <= 0.025 + tol(0.025, runs)
