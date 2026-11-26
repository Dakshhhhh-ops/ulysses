"""Group-sequential stopping boundaries with an O'Brien-Fleming shape.

Everything here works on the canonical joint distribution of a sequence of
Wald statistics Z_1..Z_K observed at information fractions t_1 < ... < t_K
(Jennison and Turnbull, *Group Sequential Methods with Applications to Clinical
Trials*, chapter 3). On the score scale S_k = Z_k * sqrt(t_k) the increments are
independent normals with mean theta * (t_k - t_{k-1}) and variance t_k - t_{k-1},
where theta is the drift (the expected z at full information).

Two boundary families are provided:

* ``lan_demets_obf``: the Lan-DeMets alpha-spending function of O'Brien-Fleming
  type, applied to each side at alpha/2: a(t) = 2 - 2 * Phi(z_{1-alpha/4} / sqrt(t)). The boundary at each
  look is solved so that the probability of a first crossing at that look,
  under no effect, equals the alpha spent since the previous look. Looks may
  land at any information fraction, which is why the contract uses it.
* ``classical_obf``: the original O'Brien-Fleming boundary C * sqrt(K / k) for
  K equally spaced looks, with C solved numerically. Used to check the
  integration against published tables.

Boundaries are two-sided and symmetric: the upper boundary declares a win, the
lower boundary declares a loss. ``alpha`` is the total two-sided level.

Ulysses computes nothing else statistical. The z it compares with these
boundaries is derived from Flagsmith's own chance-to-win (see ``decision.py``).
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from scipy.optimize import brentq
from scipy.special import ndtr, ndtri

# A z this large cannot be represented by a chance-to-win in double precision:
# Phi(8.3) rounds to 1.0. A look whose alpha increment is smaller than this
# floor is marked as unreachable (no stop possible at that look).
UNREACHABLE_SPEND = 1e-15
Z_CAP = 8.2

# Integration grid density. The step is kept well under the standard deviation
# of the next increment so Simpson's rule stays accurate for many looks.
_MIN_POINTS = 121
_STEPS_PER_SD = 8
_TAIL_SD = 9.0


def obf_spending(t: float, alpha: float) -> float:
    """Cumulative two-sided alpha spent by information fraction ``t``.

    Each side spends alpha/2 with the Lan-DeMets O'Brien-Fleming-type function
    for a one-sided level a = alpha/2:  a(t) = 2 - 2 Phi(z_{1-a/2} / sqrt(t)).
    This is the convention of gsDesign (``sfLDOF``) and ldbounds, so boundaries
    can be checked against either package.
    """
    if t <= 0:
        return 0.0
    if t >= 1:
        return alpha
    side = alpha / 2
    z = float(ndtri(1 - side / 2))
    return float(2 * (2 - 2 * ndtr(z / math.sqrt(t))))


@dataclass
class _Stage:
    """Sub-density of S_k restricted to the continuation region, on a grid.

    ``points`` are grid locations on the score scale and ``mass`` is the
    density at each point multiplied by its quadrature weight, so integrals
    over the continuation region are dot products with ``mass``.
    """

    t: float
    points: np.ndarray
    mass: np.ndarray


def _simpson(lo: float, hi: float, step: float) -> tuple[np.ndarray, np.ndarray]:
    n = max(_MIN_POINTS, int(math.ceil((hi - lo) / step)) + 1)
    if n % 2 == 0:
        n += 1
    x = np.linspace(lo, hi, n)
    h = (hi - lo) / (n - 1)
    w = np.ones(n)
    w[1:-1:2] = 4
    w[2:-1:2] = 2
    return x, w * h / 3


def _crossing(stage: _Stage | None, t: float, b: float, theta: float) -> tuple[float, float]:
    """P(first crossing at this look above +b, below -b), given the previous stage."""
    if stage is None:
        mean, sd = theta * t, math.sqrt(t)
        up_s = b * math.sqrt(t)
        upper = float(ndtr((mean - up_s) / sd))
        lower = float(ndtr((-up_s - mean) / sd))
        return upper, lower
    dt = t - stage.t
    sd = math.sqrt(dt)
    up_s = b * math.sqrt(t)
    centre = stage.points + theta * dt
    upper = float(np.dot(stage.mass, ndtr((centre - up_s) / sd)))
    lower = float(np.dot(stage.mass, ndtr((-up_s - centre) / sd)))
    return upper, lower


def _advance(stage: _Stage | None, t: float, b: float, theta: float, next_dt: float) -> _Stage:
    """Build the continuation sub-density at fraction ``t`` with boundary ``b``."""
    sd_total = math.sqrt(t)
    half = min(b, _TAIL_SD + abs(theta) * sd_total) * sd_total if math.isfinite(b) else (_TAIL_SD * sd_total + abs(theta) * t)
    lo, hi = -half, half
    # Keep the step small relative to both this stage's spread and the next increment.
    step_sd = math.sqrt(next_dt) if next_dt > 0 else sd_total
    step = min(step_sd, sd_total) / _STEPS_PER_SD
    x, w = _simpson(lo, hi, step)
    if stage is None:
        dens = np.exp(-0.5 * ((x - theta * t) / sd_total) ** 2) / (sd_total * math.sqrt(2 * math.pi))
    else:
        dt = t - stage.t
        sd = math.sqrt(dt)
        diff = (x[:, None] - stage.points[None, :] - theta * dt) / sd
        kern = np.exp(-0.5 * diff**2) / (sd * math.sqrt(2 * math.pi))
        dens = kern @ stage.mass
    return _Stage(t=t, points=x, mass=dens * w)


def _validate_fractions(fractions: Sequence[float]) -> list[float]:
    ts = [float(t) for t in fractions]
    if not ts:
        raise ValueError("at least one look is needed")
    prev = 0.0
    for t in ts:
        if not (prev < t <= 1.0 + 1e-12):
            raise ValueError(f"information fractions must increase within (0, 1]; got {ts}")
        prev = t
    ts[-1] = min(ts[-1], 1.0)
    return ts


def spending_boundaries(
    fractions: Sequence[float],
    alpha: float,
    final_spends_all: bool = True,
) -> list[float | None]:
    """Cached front for :func:`_spending_boundaries` (looks repeat across refreshes)."""
    key = tuple(round(float(t), 9) for t in fractions)
    return list(_spending_cached(key, float(alpha), bool(final_spends_all)))


@functools.lru_cache(maxsize=4096)
def _spending_cached(fractions: tuple[float, ...], alpha: float, final_spends_all: bool) -> tuple:
    return tuple(_spending_boundaries(list(fractions), alpha, final_spends_all))


def _spending_boundaries(
    fractions: Sequence[float],
    alpha: float,
    final_spends_all: bool = True,
) -> list[float | None]:
    """Two-sided Lan-DeMets O'Brien-Fleming boundaries at the given fractions.

    Returns a z boundary per look, or ``None`` when the alpha available at that
    look is too small for any representable z to cross (no stop possible).
    When ``final_spends_all`` is true the last look spends whatever alpha is
    left, so the total false-call rate is exactly ``alpha`` even if the last
    look lands short of full information.
    """
    ts = _validate_fractions(fractions)
    out: list[float | None] = []
    stage: _Stage | None = None
    spent = 0.0
    for i, t in enumerate(ts):
        last = i == len(ts) - 1
        target = alpha if (last and final_spends_all) else obf_spending(t, alpha)
        inc = target - spent
        next_dt = (ts[i + 1] - t) if not last else t
        if inc < UNREACHABLE_SPEND:
            b = math.inf
            out.append(None)
        else:
            b = _solve_two_sided(stage, t, inc)
            out.append(float(b))
            u, l = _crossing(stage, t, b, 0.0)
            spent += u + l
        if not last:
            stage = _advance(stage, t, b, 0.0, next_dt)
    return out


def _solve_two_sided(stage: _Stage | None, t: float, inc: float) -> float:
    """The symmetric boundary whose first-crossing probability at ``t`` (no effect) is ``inc``."""

    def f(bb: float) -> float:
        u, l = _crossing(stage, t, bb, 0.0)
        return (u + l) - inc

    if f(Z_CAP) > 0:
        return float(brentq(f, Z_CAP, 40.0, xtol=1e-10))
    return float(brentq(f, 0.0, Z_CAP, xtol=1e-10))


@dataclass(frozen=True)
class NextBoundary:
    """The boundary for the next look, given the boundaries earlier looks actually applied."""

    boundary: float | None
    spent_before: float
    """Alpha already spent by the earlier looks (both sides), integrated through their applied boundaries."""
    spent_after: float
    """Alpha spent once this look is analysed with ``boundary``."""


def _key(xs: Sequence[float | None], places: int) -> tuple:
    return tuple(None if x is None or not math.isfinite(float(x)) else round(float(x), places) for x in xs)


def next_two_sided(prior_fractions: Sequence[float], prior_boundaries: Sequence[float | None], t: float,
                   alpha: float, final: bool) -> NextBoundary:
    """Lan-DeMets O'Brien-Fleming boundary for a look at ``t`` after looks that applied ``prior_boundaries``.

    The earlier boundaries are taken as they were applied (``None``: no stop was possible there), not
    re-derived from the current horizon or level. The look spends ``a(t)`` minus what the earlier looks
    spent, or everything left when ``final``. When the earlier looks already spent ``a(t)`` (a horizon
    amended upwards) no stop is possible at this look. Fractions may be on any common scale as long as
    ``t`` is the information fraction of the current horizon; for a final look past the horizon pass
    them divided by that look's own count, so the final look sits at 1.
    """
    if len(prior_fractions) != len(prior_boundaries):
        raise ValueError("one applied boundary per earlier look is needed")
    return _next_two_sided_cached(_key(prior_fractions, 9), _key(prior_boundaries, 12), round(float(t), 9),
                                  float(alpha), bool(final))


@functools.lru_cache(maxsize=8192)
def _next_two_sided_cached(fractions: tuple, boundaries: tuple, t: float, alpha: float,
                           final: bool) -> NextBoundary:
    ts = _validate_fractions([*fractions, t])
    t = ts[-1]
    stage: _Stage | None = None
    spent = 0.0
    for i, b in enumerate(boundaries):
        bb = math.inf if b is None else float(b)
        if math.isfinite(bb):
            u, l = _crossing(stage, ts[i], bb, 0.0)
            spent += u + l
        stage = _advance(stage, ts[i], bb, 0.0, ts[i + 1] - ts[i])
    target = alpha if final else obf_spending(t, alpha)
    inc = target - spent
    if inc < UNREACHABLE_SPEND:
        return NextBoundary(None, spent, spent)
    b = _solve_two_sided(stage, t, inc)
    u, l = _crossing(stage, t, b, 0.0)
    return NextBoundary(b, spent, spent + u + l)


def crossing_probabilities(
    fractions: Sequence[float],
    boundaries: Sequence[float | None],
    theta: float = 0.0,
) -> tuple[list[float], list[float]]:
    """Probability of a first upper / lower crossing at each look, for drift ``theta``.

    ``theta`` is the expected z at full information (0 under no effect).
    """
    ts = _validate_fractions(fractions)
    if len(boundaries) != len(ts):
        raise ValueError("one boundary per look is needed")
    ups: list[float] = []
    lows: list[float] = []
    stage: _Stage | None = None
    for i, (t, b) in enumerate(zip(ts, boundaries)):
        bb = math.inf if b is None else float(b)
        if math.isfinite(bb):
            u, l = _crossing(stage, t, bb, theta)
        else:
            u, l = 0.0, 0.0
        ups.append(u)
        lows.append(l)
        if i < len(ts) - 1:
            stage = _advance(stage, t, bb, theta, ts[i + 1] - t)
    return ups, lows


def classical_obf_constant(looks: int, alpha: float) -> float:
    """O'Brien-Fleming constant C for ``looks`` equally spaced looks (two-sided)."""
    ts = [k / looks for k in range(1, looks + 1)]

    def total(c: float) -> float:
        bs = [c * math.sqrt(looks / k) for k in range(1, looks + 1)]
        u, l = crossing_probabilities(ts, bs, 0.0)
        return sum(u) + sum(l) - alpha

    return float(brentq(total, 1.0, 6.0, xtol=1e-10))


def classical_obf_boundaries(looks: int, alpha: float) -> list[float]:
    c = classical_obf_constant(looks, alpha)
    return [c * math.sqrt(looks / k) for k in range(1, looks + 1)]


def drift_for_power(
    fractions: Sequence[float],
    boundaries: Sequence[float | None],
    power: float,
) -> float:
    """Drift theta at which the upper boundary is crossed with probability ``power``."""

    def f(theta: float) -> float:
        u, _ = crossing_probabilities(fractions, boundaries, theta)
        return sum(u) - power

    return float(brentq(f, 0.0, 15.0, xtol=1e-8))


def inflation_factor(looks: int, alpha: float, power: float) -> float:
    """Sample size of the sequential design relative to a single-look design.

    The ratio (theta_sequential / (z_{1-alpha/2} + z_power))^2 for equally spaced
    looks under the Lan-DeMets O'Brien-Fleming boundary.
    """
    ts = [k / looks for k in range(1, looks + 1)]
    bs = spending_boundaries(ts, alpha)
    theta = drift_for_power(ts, bs, power)
    fixed = float(ndtri(1 - alpha / 2) + ndtri(power))
    return (theta / fixed) ** 2


def expected_stop_fraction(
    fractions: Sequence[float],
    boundaries: Sequence[float | None],
    theta: float,
) -> float:
    """Expected information fraction at stopping (1.0 if never crossed)."""
    ts = _validate_fractions(fractions)
    ups, lows = crossing_probabilities(ts, boundaries, theta)
    stop = 0.0
    cont = 1.0
    for t, u, l in zip(ts, ups, lows):
        stop += t * (u + l)
        cont -= u + l
    return stop + ts[-1] * cont


def z_to_chance(z: float | None) -> float | None:
    """The chance-to-win that corresponds to a z boundary (for display)."""
    if z is None or not math.isfinite(z):
        return None
    return float(ndtr(z))


def chance_to_z(chance: float) -> float:
    """Invert Flagsmith's chance-to-win, Phi(lift / SE), back to lift / SE.

    A chance of exactly 0 or 1 (rounding at the extremes) is clamped to the
    largest z a double can carry, which is above every reachable boundary.
    """
    if not (0.0 <= chance <= 1.0) or math.isnan(chance):
        raise ValueError(f"chance-to-win must be in [0, 1], got {chance!r}")
    lo = 1e-16
    c = min(max(chance, lo), 1 - lo)
    z = float(ndtri(c))
    return max(-Z_CAP - 1, min(Z_CAP + 1, z))


# ---- one-sided boundaries (guardrails) ------------------------------------------


def pocock_spending(t: float, alpha: float) -> float:
    """Lan-DeMets Pocock-type spending: alpha * ln(1 + (e - 1) t).

    Unlike O'Brien-Fleming it spends evenly, so a guardrail can stop early.
    """
    if t <= 0:
        return 0.0
    if t >= 1:
        return alpha
    return float(alpha * math.log(1 + (math.e - 1) * t))


def _advance_upper(stage: _Stage | None, t: float, b: float, next_dt: float) -> _Stage:
    """Continuation sub-density below an upper boundary only (theta = 0)."""
    sd_total = math.sqrt(t)
    lo = -_TAIL_SD * sd_total
    hi = (min(b, _TAIL_SD) if math.isfinite(b) else _TAIL_SD) * sd_total
    step_sd = math.sqrt(next_dt) if next_dt > 0 else sd_total
    x, w = _simpson(lo, hi, min(step_sd, sd_total) / _STEPS_PER_SD)
    if stage is None:
        dens = np.exp(-0.5 * (x / sd_total) ** 2) / (sd_total * math.sqrt(2 * math.pi))
    else:
        sd = math.sqrt(t - stage.t)
        diff = (x[:, None] - stage.points[None, :]) / sd
        dens = (np.exp(-0.5 * diff**2) / (sd * math.sqrt(2 * math.pi))) @ stage.mass
    return _Stage(t=t, points=x, mass=dens * w)


def _upper_crossing(stage: _Stage | None, t: float, b: float) -> float:
    up_s = b * math.sqrt(t)
    if stage is None:
        return float(ndtr(-up_s / math.sqrt(t)))
    sd = math.sqrt(t - stage.t)
    return float(np.dot(stage.mass, ndtr((stage.points - up_s) / sd)))


@functools.lru_cache(maxsize=4096)
def _one_sided_cached(fractions: tuple[float, ...], alpha: float, final_spends_all: bool) -> tuple:
    ts = _validate_fractions(fractions)
    out: list[float | None] = []
    stage: _Stage | None = None
    spent = 0.0
    for i, t in enumerate(ts):
        last = i == len(ts) - 1
        target = alpha if (last and final_spends_all) else pocock_spending(t, alpha)
        inc = target - spent
        if inc < UNREACHABLE_SPEND:
            b = math.inf
            out.append(None)
        else:
            def f(bb: float) -> float:
                return _upper_crossing(stage, t, bb) - inc

            b = brentq(f, -8.0, 40.0, xtol=1e-10) if f(Z_CAP) <= 0 else Z_CAP + 1
            out.append(float(b))
            spent += _upper_crossing(stage, t, b)
        if not last:
            stage = _advance_upper(stage, t, b, ts[i + 1] - t)
    return tuple(out)


def one_sided_boundaries(fractions: Sequence[float], alpha: float, final_spends_all: bool = True) -> list[float | None]:
    """One-sided Lan-DeMets Pocock-type boundaries: stop when z >= b_k.

    The probability, under no effect, of ever crossing at any of the looks is
    ``alpha``. Used for guardrails, where only one direction is a breach.
    """
    key = tuple(round(float(t), 9) for t in fractions)
    return list(_one_sided_cached(key, float(alpha), bool(final_spends_all)))


@functools.lru_cache(maxsize=8192)
def _spent_two_sided_cached(fractions: tuple, boundaries: tuple) -> float:
    if not fractions:
        return 0.0
    ups, lows = crossing_probabilities(list(fractions), list(boundaries), 0.0)
    return float(sum(ups) + sum(lows))


def spent_two_sided(fractions: Sequence[float], boundaries: Sequence[float | None]) -> float:
    """Alpha (both sides, no effect) the applied boundaries have spent so far."""
    if len(fractions) != len(boundaries):
        raise ValueError("one applied boundary per look is needed")
    return _spent_two_sided_cached(_key(fractions, 9), _key(boundaries, 12))


def next_one_sided(prior_fractions: Sequence[float], prior_boundaries: Sequence[float | None], t: float,
                   alpha: float, final: bool) -> NextBoundary:
    """Pocock-type one-sided boundary for a look at ``t``, integrated forward from the applied boundaries.

    The one-sided counterpart of :func:`next_two_sided` (guardrails)."""
    if len(prior_fractions) != len(prior_boundaries):
        raise ValueError("one applied boundary per earlier look is needed")
    return _next_one_sided_cached(_key(prior_fractions, 9), _key(prior_boundaries, 12), round(float(t), 9),
                                  float(alpha), bool(final))


@functools.lru_cache(maxsize=8192)
def _next_one_sided_cached(fractions: tuple, boundaries: tuple, t: float, alpha: float,
                           final: bool) -> NextBoundary:
    ts = _validate_fractions([*fractions, t])
    t = ts[-1]
    stage: _Stage | None = None
    spent = 0.0
    for i, b in enumerate(boundaries):
        bb = math.inf if b is None else float(b)
        if math.isfinite(bb):
            spent += _upper_crossing(stage, ts[i], bb)
        stage = _advance_upper(stage, ts[i], bb, ts[i + 1] - ts[i])
    target = alpha if final else pocock_spending(t, alpha)
    inc = target - spent
    if inc < UNREACHABLE_SPEND:
        return NextBoundary(None, spent, spent)

    def f(bb: float) -> float:
        return _upper_crossing(stage, t, bb) - inc

    b = float(brentq(f, -8.0, 40.0, xtol=1e-10)) if f(Z_CAP) <= 0 else Z_CAP + 1
    return NextBoundary(b, spent, spent + _upper_crossing(stage, t, b))


def one_sided_crossing(fractions: Sequence[float], boundaries: Sequence[float | None]) -> float:
    """Total probability of crossing an upper boundary under no effect (for checks)."""
    ts = _validate_fractions(fractions)
    stage: _Stage | None = None
    total = 0.0
    for i, (t, b) in enumerate(zip(ts, boundaries)):
        bb = math.inf if b is None else float(b)
        if math.isfinite(bb):
            total += _upper_crossing(stage, t, bb)
        if i < len(ts) - 1:
            stage = _advance_upper(stage, t, bb, ts[i + 1] - t)
    return total
