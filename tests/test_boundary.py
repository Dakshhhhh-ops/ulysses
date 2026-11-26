"""Boundary checks against published references and against simulation.

References:
* Jennison and Turnbull (2000), Table 2.3: O'Brien-Fleming constants C_B(K, 0.05),
  two-sided alpha = 0.05.
* gsDesign ``sfLDOF`` / ldbounds Lan-DeMets O'Brien-Fleming bounds for K = 5
  equally spaced looks, two-sided alpha = 0.05 (0.025 per side):
  4.8769, 3.3569, 2.6803, 2.2898, 2.0310.
"""

import math

import numpy as np
import pytest

from ulysses import boundary as bd


@pytest.mark.parametrize(
    "looks, expected",
    [(1, 1.960), (2, 1.977), (3, 2.004), (4, 2.024), (5, 2.040), (10, 2.087), (20, 2.126)],
)
def test_classical_obf_constants_match_jennison_turnbull(looks, expected):
    assert bd.classical_obf_constant(looks, 0.05) == pytest.approx(expected, abs=0.0015)


def test_lan_demets_obf_matches_gsdesign_k5():
    got = bd.spending_boundaries([0.2, 0.4, 0.6, 0.8, 1.0], 0.05)
    assert got == pytest.approx([4.8769, 3.3569, 2.6803, 2.2898, 2.0310], abs=5e-4)


def test_total_false_call_rate_is_alpha_for_many_looks():
    ts = [k / 30 for k in range(1, 31)]
    bs = bd.spending_boundaries(ts, 0.05)
    up, low = bd.crossing_probabilities(ts, bs, 0.0)
    assert sum(up) == pytest.approx(0.025, abs=1e-6)
    assert sum(low) == pytest.approx(0.025, abs=1e-6)


def test_boundary_shape_is_obrien_fleming():
    ts = [k / 30 for k in range(1, 31)]
    bs = bd.spending_boundaries(ts, 0.05)
    finite = [b for b in bs if b is not None]
    # Early looks demand far more evidence than late ones, and the boundary only falls.
    assert all(a > b for a, b in zip(finite, finite[1:]))
    assert bs[0] is None  # spending at look 1 of 30 is below any representable chance-to-win
    assert finite[-1] < 2.2


def test_unequal_actual_fractions_still_spend_exactly_alpha():
    ts = [0.13, 0.29, 0.31, 0.58, 0.77, 0.94]  # looks landing off-schedule; last one short of horizon
    bs = bd.spending_boundaries(ts, 0.05, final_spends_all=True)
    up, low = bd.crossing_probabilities(ts, bs, 0.0)
    assert sum(up) + sum(low) == pytest.approx(0.05, abs=1e-6)


def test_monte_carlo_agrees_with_integration():
    """Simulate the canonical joint distribution directly and compare crossing rates."""
    rng = np.random.default_rng(7)
    k = 10
    ts = np.array([i / k for i in range(1, k + 1)])
    bs = np.array(bd.spending_boundaries(list(ts), 0.05), dtype=float)
    reps = 200_000
    for theta, expect_power in ((0.0, None), (3.0, None)):
        inc = rng.normal(theta * np.diff(np.concatenate([[0], ts])), np.sqrt(np.diff(np.concatenate([[0], ts]))),
                         size=(reps, k))
        s = np.cumsum(inc, axis=1)
        z = s / np.sqrt(ts)
        crossed_up = (z >= bs).any(axis=1)
        crossed = (np.abs(z) >= bs).any(axis=1)
        first = np.argmax(np.abs(z) >= bs, axis=1)
        sign_up = z[np.arange(reps), first] > 0
        mc_up = float(np.mean(crossed & sign_up))
        up, low = bd.crossing_probabilities(list(ts), list(bs), theta)
        assert mc_up == pytest.approx(sum(up), abs=3 * math.sqrt(sum(up) * (1 - sum(up)) / reps) + 1e-3)
        del crossed_up


def test_drift_for_power_and_inflation():
    infl = bd.inflation_factor(5, 0.05, 0.9)
    # Sequential designs need slightly more information than one look, never less.
    assert 1.0 < infl < 1.05
    ts = [0.2, 0.4, 0.6, 0.8, 1.0]
    bs = bd.spending_boundaries(ts, 0.05)
    theta = bd.drift_for_power(ts, bs, 0.9)
    up, _ = bd.crossing_probabilities(ts, bs, theta)
    assert sum(up) == pytest.approx(0.9, abs=1e-6)


def test_chance_and_z_conversions_invert_flagsmith():
    from statistics import NormalDist

    for z in (-3.1, -1.0, 0.0, 0.5, 2.3, 4.0):
        chance = NormalDist().cdf(z)  # exactly how Flagsmith computes chance_to_win from lift/SE
        assert bd.chance_to_z(chance) == pytest.approx(z, abs=1e-9)
    assert bd.chance_to_z(1.0) > bd.Z_CAP
    assert bd.chance_to_z(0.0) < -bd.Z_CAP
    with pytest.raises(ValueError):
        bd.chance_to_z(1.2)


def test_one_sided_pocock_boundaries_spend_exactly_alpha():
    for looks in (1, 5, 12, 30):
        ts = [k / looks for k in range(1, looks + 1)]
        bs = bd.one_sided_boundaries(ts, 0.05)
        assert bd.one_sided_crossing(ts, bs) == pytest.approx(0.05, abs=1e-6)
    assert bd.one_sided_boundaries([1.0], 0.05)[0] == pytest.approx(1.6449, abs=1e-3)
    # Pocock-type spending is roughly flat: early looks are not much harder than late ones.
    b = bd.one_sided_boundaries([k / 12 for k in range(1, 13)], 0.05)
    assert b[0] - b[-1] < 0.5


def test_one_sided_boundary_agrees_with_monte_carlo():
    rng = np.random.default_rng(11)
    k = 12
    ts = np.array([i / k for i in range(1, k + 1)])
    bs = np.array(bd.one_sided_boundaries(list(ts), 0.01), dtype=float)
    reps = 200_000
    inc = rng.normal(0.0, np.sqrt(np.diff(np.concatenate([[0], ts]))), size=(reps, k))
    z = np.cumsum(inc, axis=1) / np.sqrt(ts)
    mc = float(np.mean((z >= bs).any(axis=1)))
    assert mc == pytest.approx(0.01, abs=3 * math.sqrt(0.01 * 0.99 / reps) + 5e-4)
