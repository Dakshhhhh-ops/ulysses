"""Power and expected sample size of a signed contract, for the signer to read before signing.

For each true effect the curves give the chance the contract ships the treatment (win), the chance
it keeps control on a loss, and the expected identities per arm used before it stops (a win, a loss,
or the horizon). They describe the boundary in the contract, at the planned looks.

* Two arms: computed analytically from the same numerical integration that builds the boundary
  (``boundary.crossing_probabilities`` and ``boundary.expected_stop_fraction``) on the canonical
  joint distribution of sequential z statistics. Exact for that model.
* Several treatments: the comparisons share one control, so their z statistics are correlated (1/2
  for equal arms) and a win is the first look at which any arm crosses; there is no closed form for
  that, so it is simulated with a fixed seed. The configuration drawn is one treatment with the
  given effect and every other treatment with none.

The x axis is the relative lift when the contract records a baseline conversion rate (the effect is
turned into an expected z with the delta-method variance Flagsmith uses, as in ``horizon.py``), and
the drift (the expected z at the horizon) otherwise.

What the curves do not cover: they assume the z values behave like the sequential Wald statistics
they approximate (normal, independent increments). At low counts Flagsmith's relative-lift z is
skewed (docs/STATISTICS.md), and the simulation of Flagsmith's own code there is the better guide.
They also assume each look lands on its planned count.
"""

from __future__ import annotations

import functools
import math

import numpy as np
from scipy.special import ndtri

from . import boundary as bd
from .horizon import _unit_variance_conversion

SIM_SEED = 61803
SIM_RUNS = 4000
POINTS = 25


def _theta(lift: float, baseline_rate: float, n: int) -> float | None:
    """Expected z at the horizon for a true relative lift, with Flagsmith's delta-method variance."""
    if lift == 0:
        return 0.0
    try:
        uv = _unit_variance_conversion(baseline_rate, lift)
    except ValueError:
        return None
    return lift / math.sqrt(uv / n)


def _grid(design_lift: float | None, baseline_rate: float | None) -> tuple[str, list[float]]:
    """POINTS evenly spaced values that always include no effect (0) and the design lift itself."""
    if design_lift and baseline_rate:
        d = abs(design_lift)
        step = d / 10  # -0.5d .. 1.9d in tenths of the design lift: 0 and d are grid points
        return "relative_lift", [round((i - 5) * step, 6) for i in range(POINTS)]
    return "drift", [round(-1.5 + 0.25 * i, 6) for i in range(POINTS)]


def analytic(fractions: list[float], bounds: list[float | None], theta: float) -> tuple[float, float, float]:
    """(P(win), P(loss), expected information fraction at stop) for one comparison."""
    up, low = bd.crossing_probabilities(fractions, bounds, theta)
    return float(sum(up)), float(sum(low)), float(bd.expected_stop_fraction(fractions, bounds, theta))


def null_z_paths(fractions: list[float], comparisons: int, runs: int = SIM_RUNS, seed: int = SIM_SEED) -> np.ndarray:
    """No-effect z paths of each treatment against one shared control, shape (runs, comparisons, looks).

    Each z is N(0, 1) at every look and two treatments' z values correlate 1/2 (equal arms).
    """
    ts = np.asarray(fractions, dtype=float)
    dt = np.diff(np.concatenate([[0.0], ts]))
    rng = np.random.default_rng(seed)
    # Brownian scores of control and of each treatment, per unit of information.
    w = np.cumsum(rng.standard_normal((runs, comparisons + 1, len(ts))) * np.sqrt(dt), axis=2)
    return (w[:, 1:, :] - w[:, :1, :]) / np.sqrt(2.0 * ts)


def path_outcomes(z: np.ndarray, bounds: list[float | None]) -> dict[str, np.ndarray]:
    """Per path: won, winner (treatment index), lost and the index of the look it stopped at (the last
    look when it never stopped), for z of shape (runs, comparisons, looks) against the signed bounds."""
    runs, _, k = z.shape
    b = np.array([np.inf if x is None else float(x) for x in bounds])
    up = z >= b
    down = (z <= -b).all(axis=1)  # (runs, k)
    any_up = up.any(axis=1)
    stop = any_up | down
    stopped = stop.any(axis=1)
    first = np.where(stopped, np.argmax(stop, axis=1), k - 1)
    idx = np.arange(runs)
    won = stopped & any_up[idx, first]
    zf = np.where(up[idx, :, first], z[idx, :, first], -np.inf)
    return {"stopped": stopped, "won": won, "winner": np.argmax(zf, axis=1), "lost": stopped & ~won,
            "first": first}


def simulate(fractions: list[float], bounds: list[float | None], thetas: list[float], comparisons: int,
             runs: int = SIM_RUNS, seed: int = SIM_SEED) -> list[dict]:
    """Several treatments against one shared control, one treatment with drift theta, the rest with none.

    Returns per theta: win_effective (ships the treatment with the effect), win_other (ships one with no
    effect), loss (every treatment below its lower boundary), mean stop fraction. Common random numbers
    across thetas, so the curves are smooth and differences between points are not noise.
    """
    ts = np.asarray(fractions, dtype=float)
    noise = null_z_paths(fractions, comparisons, runs, seed)
    out = []
    for theta in thetas:
        drift = np.zeros((comparisons, 1))
        drift[0, 0] = theta
        o = path_outcomes(noise + drift * np.sqrt(ts), bounds)
        won, winner, lost = o["won"], o["winner"], o["lost"]
        t_stop = np.where(o["stopped"], ts[o["first"]], ts[-1])
        out.append({
            "win_effective": float(np.mean(won & (winner == 0))),
            "win_other": float(np.mean(won & (winner != 0))),
            "loss": float(np.mean(lost)),
            "stop_fraction": float(np.mean(t_stop)),
        })
    return out


@functools.lru_cache(maxsize=128)
def _curves(looks: int, alpha_c: float, comparisons: int, n: int, baseline_rate: float | None,
            design_lift: float | None, design_power: float | None) -> dict:
    fractions = [i / looks for i in range(1, looks + 1)]
    bounds = bd.spending_boundaries(fractions, alpha_c)
    kind, xs = _grid(design_lift, baseline_rate)
    if kind == "relative_lift":
        pairs = [(x, _theta(x, baseline_rate, n)) for x in xs]
        pairs = [(x, th) for x, th in pairs if th is not None]
    else:
        pairs = [(x, x) for x in xs]
    points = []
    if comparisons == 1:
        method = "analytic"
        for x, th in pairs:
            w, l, f = analytic(fractions, bounds, th)
            points.append({"x": x, "theta": round(th, 6), "win": w, "win_other": 0.0, "loss": l,
                           "expected_identities_per_arm": f * n})
    else:
        method = "simulation"
        sims = simulate(fractions, bounds, [th for _, th in pairs], comparisons)
        for (x, th), s in zip(pairs, sims):
            points.append({"x": x, "theta": round(th, 6), "win": s["win_effective"], "win_other": s["win_other"],
                           "loss": s["loss"], "expected_identities_per_arm": s["stop_fraction"] * n})
    design = None
    if kind == "relative_lift" and design_lift:
        th = _theta(abs(design_lift), baseline_rate, n)
        if th is not None:
            if comparisons == 1:
                w, l, f = analytic(fractions, bounds, th)
                design = {"x": abs(design_lift), "win": w, "expected_identities_per_arm": f * n}
            else:
                s = simulate(fractions, bounds, [th], comparisons)[0]
                design = {"x": abs(design_lift), "win": s["win_effective"],
                          "expected_identities_per_arm": s["stop_fraction"] * n}
    fixed = None
    if design and 0.5 < design["win"] < 1:
        # A single read at the horizon with the same alpha per comparison and the same power at the design
        # lift (the fair fixed-sample comparator, as in docs/STATISTICS.md).
        uv = _unit_variance_conversion(baseline_rate, abs(design_lift))
        fixed = math.ceil(uv * ((float(ndtri(1 - alpha_c / 2)) + float(ndtri(design["win"]))) / abs(design_lift)) ** 2)
    return {
        "fixed_sample_identities_per_arm": fixed,
        "method": method,
        "x_kind": kind,
        "looks": looks,
        "comparisons": comparisons,
        "comparison_alpha": alpha_c,
        "horizon_identities_per_arm": n,
        "baseline_rate": baseline_rate,
        "design": design,
        "design_power_signed": design_power,
        "simulation": None if method == "analytic" else {"runs": SIM_RUNS, "seed": SIM_SEED},
        "points": points,
    }


def curves_for_contract(c) -> dict:
    """Power and expected-sample-size curves for a ``contract.Contract``."""
    d = c.design
    baseline = d.baseline_rate if d and d.baseline_rate and 0 < d.baseline_rate < 1 else None
    lift = d.min_relative_lift if d and d.min_relative_lift else None
    return _curves(c.horizon.looks, c.comparison_alpha, c.comparisons, c.horizon.identities_per_arm,
                   baseline, lift, d.power if d else None)
