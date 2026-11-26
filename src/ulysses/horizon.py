"""Horizon helper: identities per arm and a look schedule.

Flagsmith has no sample-size calculator. Given the smallest relative lift the
owner cares about and the metric's baseline, this works out how many
identities per arm the contract needs so that the signed boundary detects that
lift with the chosen power, and when each look will land at the experiment's
current exposure rate (read from Flagsmith's exposures timeseries).

The variance model is the one Flagsmith's engine uses for its chance-to-win:
the delta-method variance of the relative lift (``api/experimentation/stats.py``),

    Var(lift) = s_t^2 / (n m_c^2) + m_t^2 s_c^2 / (n m_c^4)

so the expected z at the horizon is lift / sqrt(Var(lift)). The sequential
design needs a slightly larger expected z than a single look (the drift that
gives the requested power under the boundary), which is where the small
inflation over a fixed-sample calculation comes from.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from scipy.special import ndtri

from . import boundary as bd


@dataclass(frozen=True)
class LookSlot:
    look: int
    identities_per_arm: int
    hours_from_start: float | None


@dataclass(frozen=True)
class HorizonPlan:
    identities_per_arm: int
    fixed_sample_identities_per_arm: int
    inflation: float
    looks: int
    alpha: float
    power: float
    min_relative_lift: float
    exposure_rate_per_hour: float | None
    hours_to_horizon: float | None
    minutes_between_looks: float | None
    schedule: list[LookSlot]
    notes: list[str]
    comparisons: int = 1
    comparison_alpha: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _unit_variance_conversion(p: float, lift: float) -> float:
    """n * Var(lift) for a 0/1 metric with control rate p and relative lift ``lift``."""
    pt = p * (1 + lift)
    if not (0 < p < 1) or not (0 < pt < 1):
        raise ValueError("baseline rate and lifted rate must be strictly between 0 and 1")
    return pt * (1 - pt) / p**2 + pt**2 * p * (1 - p) / p**4


def _unit_variance_mean(mean: float, sd: float, lift: float) -> float:
    if mean <= 0 or sd <= 0:
        raise ValueError("baseline mean and standard deviation must be positive")
    mt = mean * (1 + lift)
    return sd**2 / mean**2 + mt**2 * sd**2 / mean**4


def plan(
    *,
    min_relative_lift: float,
    baseline_rate: float | None = None,
    baseline_mean: float | None = None,
    baseline_sd: float | None = None,
    alpha: float = 0.05,
    power: float = 0.8,
    looks: int | None = None,
    exposure_rate_per_hour: float | None = None,
    min_minutes_between_looks: int = 5,
    max_looks: int = 30,
    comparisons: int = 1,
) -> HorizonPlan:
    """``comparisons`` > 1 sizes each treatment-vs-control comparison at alpha / comparisons (the
    contract's Bonferroni split), so the horizon is per arm with that stricter boundary."""
    if min_relative_lift == 0:
        raise ValueError("the smallest lift you care about cannot be zero")
    if not (0.5 <= power < 1):
        raise ValueError("power must be in [0.5, 1)")
    lift = abs(min_relative_lift)
    if baseline_rate is not None:
        uv = _unit_variance_conversion(baseline_rate, lift)
    elif baseline_mean is not None and baseline_sd is not None:
        uv = _unit_variance_mean(baseline_mean, baseline_sd, lift)
    else:
        raise ValueError("give a baseline conversion rate, or a baseline mean and standard deviation")

    notes: list[str] = []
    if comparisons < 1:
        raise ValueError("comparisons must be at least 1")
    a_c = alpha / comparisons if comparisons > 1 else alpha
    fixed_z = float(ndtri(1 - a_c / 2) + ndtri(power))
    n_fixed = math.ceil(uv * (fixed_z / lift) ** 2)

    # Choose the number of looks: one per refresh window is the most Flagsmith
    # allows; more than max_looks adds almost nothing.
    if looks is None:
        looks = max_looks
        if exposure_rate_per_hour:
            hours = n_fixed / exposure_rate_per_hour
            possible = int(hours * 60 // min_minutes_between_looks)
            looks = max(1, min(max_looks, possible))
            if possible < max_looks:
                notes.append(
                    f"Only {possible} refresh windows fit before the horizon at the current rate, "
                    f"so the plan uses {looks} looks."
                )
    fractions = [k / looks for k in range(1, looks + 1)]
    bounds = bd.spending_boundaries(fractions, a_c)
    theta = bd.drift_for_power(fractions, bounds, power)
    n = math.ceil(uv * (theta / lift) ** 2)
    if n < 100:
        n = 100
        notes.append("Raised to 100 identities per arm, the smallest horizon a contract accepts.")

    hours_to = minutes_between = None
    if exposure_rate_per_hour:
        hours_to = n / exposure_rate_per_hour
        minutes_between = hours_to * 60 / looks
        if minutes_between < min_minutes_between_looks:
            notes.append(
                "Looks would come faster than Flagsmith's five-minute refresh floor; "
                "the scheduler will simply take them at the next allowed refresh."
            )
    schedule = [
        LookSlot(
            look=k,
            identities_per_arm=math.ceil(n * k / looks),
            hours_from_start=None if not exposure_rate_per_hour else round(n * k / looks / exposure_rate_per_hour, 2),
        )
        for k in range(1, looks + 1)
    ]
    return HorizonPlan(
        identities_per_arm=n,
        fixed_sample_identities_per_arm=n_fixed,
        inflation=round(n / n_fixed, 4) if n_fixed else 1.0,
        looks=looks,
        alpha=alpha,
        power=power,
        min_relative_lift=lift,
        exposure_rate_per_hour=exposure_rate_per_hour,
        hours_to_horizon=None if hours_to is None else round(hours_to, 2),
        minutes_between_looks=None if minutes_between is None else round(minutes_between, 1),
        schedule=schedule,
        notes=notes,
        comparisons=comparisons,
        comparison_alpha=a_c,
    )
