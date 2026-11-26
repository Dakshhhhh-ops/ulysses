"""Sample-ratio mismatch: were identities split between the arms as the contract signed?

Flagsmith computes the same test itself (``srm_p_value`` in ``api/experimentation/stats.py``: a
chi-square on exposure counts against the expected shares), but it reads the expected shares from the
flag's environment-default weights, and once an experiment's rollout is applied those are all zero, so
Flagsmith returns ``null`` for every running experiment (see the field guide). Ulysses therefore runs
the test against the split the owner signed, on the exposure counts in Flagsmith's own results payload
(``variants[key].n``: exposed identities, quarantined ones already removed).

The statistic and p-value are the textbook Pearson chi-square with k - 1 degrees of freedom, the
same formula as Flagsmith's ``srm_p_value``. A p-value under the signed threshold holds a win; it never
ships and never stops the experiment on its own.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from scipy.stats import chi2


@dataclass(frozen=True)
class SampleRatioCheck:
    state: str  # "ok", "mismatch", or "too_few" (fewer identities than min_total, not tested)
    p_value: float | None
    statistic: float | None
    threshold: float
    total: int
    observed: dict[str, int]
    expected_share: dict[str, float]

    @property
    def blocks_win(self) -> bool:
        return self.state == "mismatch"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["blocks_win"] = self.blocks_win
        return d


def chi_square(observed: list[int], shares: list[float]) -> tuple[float, float]:
    """(statistic, p-value) of observed counts against expected shares."""
    total = sum(observed)
    stat = sum((o - total * s) ** 2 / (total * s) for o, s in zip(observed, shares, strict=True))
    return float(stat), float(chi2.sf(stat, df=len(observed) - 1))


def check(identities: dict[str, int], split: dict[str, float], threshold: float, min_total: int) -> SampleRatioCheck:
    arms = list(split)
    total_pct = sum(split.values())
    shares = {a: split[a] / total_pct for a in arms}
    observed = {a: int(identities.get(a, 0)) for a in arms}
    total = sum(observed.values())
    base = dict(threshold=threshold, total=total, observed=observed, expected_share=shares)
    if total < min_total:
        return SampleRatioCheck(state="too_few", p_value=None, statistic=None, **base)
    stat, p = chi_square([observed[a] for a in arms], [shares[a] for a in arms])
    return SampleRatioCheck(state="mismatch" if p < threshold else "ok", p_value=p, statistic=round(stat, 4), **base)
