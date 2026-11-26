"""The stop contract: a small TOML file, signed before an experiment starts.

The contract says, before any data exists, exactly when the experiment may stop
and what happens on each outcome. Its SHA-256 (over the canonical JSON form,
see ``canonical.py``) is written into the Flagsmith experiment's hypothesis
field, so Flagsmith's own UI shows which contract governs the test.

The start window is relative (``start_within_days`` counted from the owner's
signature), so no calendar date ever appears in a contract file.
"""

from __future__ import annotations

import functools
import math
import tomllib
import unicodedata
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import boundary as bd
from .canonical import content_hash

SCHEMA_VERSION = 2
SPENDING = "lan_demets_obrien_fleming"
REFRESH_FLOOR_MINUTES = 5
# Measured offline (docs/STATISTICS.md): at 0.05 a not_decrease guardrail false-stopped 10.7% of A/A
# experiments at 100 identities per arm per look, because Flagsmith's relative-lift z is skewed at low
# counts; at 0.01 it was 4.2% (not_increase 0.1%).
GUARDRAIL_ALPHA = 0.01  # Flagsmith allows one results refresh per experiment every five minutes
# Sample-ratio mismatch: Flagsmith's own stats.py docstring calls p < 0.001 a broken split, and it skips the
# test below 100 identities in all arms (SRM_MIN_TOTAL_IDENTITIES in api/experimentation/constants.py).
SRM_P_THRESHOLD = 0.001
SRM_MIN_TOTAL = 100
MAX_TREATMENTS = 5

Action = Literal["ship_treatment", "keep_control"]


class _Model(BaseModel):
    # No NaN or infinity anywhere: they have no canonical JSON form, so they could not be hashed.
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _nfc(v: str | None) -> str | None:
    """Prose is hashed in Unicode NFC, so text that reads the same hashes the same.

    Identifiers (flag, variation, metric, environment and signer names) are left byte-exact: they must
    equal what Flagsmith and the signer registry hold, and two spellings there are two different things.
    """
    return None if v is None else unicodedata.normalize("NFC", v)


class ExperimentRef(_Model):
    project: int = Field(description="Flagsmith project id")
    environment: str = Field(description="Flagsmith environment key the experiment runs in")
    experiment: int = Field(description="Flagsmith experiment id")
    flag: str = Field(description="Feature (flag) name the experiment runs on")
    control: str = Field("control", description="Variation key of the control arm (Flagsmith reserves 'control')")
    treatment: str = Field(description="Variation key of the treatment arm (the first, if there are several)")
    more_treatments: list[str] | None = Field(
        None, description="Further treatment arms, each compared with control. Absent for a two-arm experiment. "
        "With more than one treatment the boundary splits alpha across the comparisons (boundary.multiplicity).")

    @model_validator(mode="after")
    def _arms_differ(self) -> "ExperimentRef":
        if self.control == self.treatment:
            raise ValueError("control and treatment must be different variations")
        if self.more_treatments is not None:
            if not self.more_treatments:
                raise ValueError("more_treatments must list at least one arm, or be left out")
            arms = [self.control, *self.treatments]
            if len(set(arms)) != len(arms):
                raise ValueError("every arm must be a different variation")
            if len(self.treatments) > MAX_TREATMENTS:
                raise ValueError(f"at most {MAX_TREATMENTS} treatments against one control")
        return self

    @property
    def treatments(self) -> list[str]:
        return [self.treatment, *(self.more_treatments or [])]

    @property
    def arms(self) -> list[str]:
        return [self.control, *self.treatments]


class Metric(_Model):
    name: str
    direction: Literal["increase", "decrease"] = Field(
        description="Which way is better for the primary metric (as configured in Flagsmith)"
    )


class Guardrail(_Model):
    name: str
    direction: Literal["not_increase", "not_decrease"]
    alpha: float = Field(
        GUARDRAIL_ALPHA,
        description=(
            "One-sided false-stop budget for this guardrail across all looks. It is spent with a "
            "Lan-DeMets Pocock-type function at the counted looks, on z = Phi^-1(Flagsmith's chance "
            "that the metric moved the wrong way). A breach stops and keeps control."
        ),
    )

    @field_validator("alpha")
    @classmethod
    def _range(cls, v: float) -> float:
        if not (0.001 <= v <= 0.2):
            raise ValueError("guardrail alpha must be between 0.001 and 0.2")
        return v


class Horizon(_Model):
    identities_per_arm: int = Field(description="Information at full horizon: identities in the smaller arm")
    looks: int = Field(description="Number of analyses, equally spaced in identities")
    min_minutes_between_looks: int = REFRESH_FLOOR_MINUTES

    @field_validator("identities_per_arm")
    @classmethod
    def _n(cls, v: int) -> int:
        if v < 100:
            raise ValueError("identities_per_arm must be at least 100")
        return v

    @field_validator("looks")
    @classmethod
    def _k(cls, v: int) -> int:
        if not (1 <= v <= 200):
            raise ValueError("looks must be between 1 and 200")
        return v

    @field_validator("min_minutes_between_looks")
    @classmethod
    def _floor(cls, v: int) -> int:
        if v < REFRESH_FLOOR_MINUTES:
            raise ValueError(f"Flagsmith refreshes results at most every {REFRESH_FLOOR_MINUTES} minutes")
        return v


class PlannedLook(_Model):
    look: int
    fraction: float
    identities_per_arm: int
    z: float | None = Field(None, description="Boundary on Flagsmith's z = Phi^-1(chance to win); None = no stop possible")
    chance_to_win: float | None = Field(None, description="The same boundary as a chance to win, for reading")


class Boundary(_Model):
    spending: Literal["lan_demets_obrien_fleming"] = SPENDING
    alpha: float = Field(0.05, description="Two-sided nominal level: alpha/2 for a false win, alpha/2 for a false loss")
    sides: Literal["two_sided"] = "two_sided"
    multiplicity: Literal["bonferroni"] | None = Field(
        None, description="How alpha is split across several treatment-vs-control comparisons. 'bonferroni': each "
        "comparison runs its own boundary at alpha / (number of treatments), so the chance of shipping any "
        "do-nothing treatment stays at most alpha/2 whatever the other arms do. Absent for two arms.")
    planned: list[PlannedLook] = Field(default_factory=list)

    @field_validator("alpha")
    @classmethod
    def _alpha(cls, v: float) -> float:
        if not (0.0 < v <= 0.2):
            raise ValueError("alpha must be in (0, 0.2]")
        return v


class SampleRatio(_Model):
    """The signed traffic split and the sample-ratio-mismatch test that guards a win."""

    split: dict[str, float] = Field(
        description="Percent of enrolled identities per variation key, control included (sums to 100). It must "
        "match the experiment's rollout weights in Flagsmith when the contract is submitted.")
    p_threshold: float = Field(
        SRM_P_THRESHOLD, description="A win is held while the chi-square p-value of Flagsmith's exposure counts "
        "against the split is below this")
    min_total: int = Field(SRM_MIN_TOTAL, description="Below this many identities in all arms the test is not run")

    @field_validator("split")
    @classmethod
    def _split(cls, v: dict[str, float]) -> dict[str, float]:
        if len(v) < 2:
            raise ValueError("a split needs at least two arms")
        if any(not (0 < x < 100) for x in v.values()):
            raise ValueError("each arm's share must be between 0 and 100 percent")
        if abs(sum(v.values()) - 100) > 0.05:
            raise ValueError(f"the split must sum to 100 percent, got {sum(v.values())}")
        return v

    @field_validator("p_threshold")
    @classmethod
    def _p(cls, v: float) -> float:
        if not (1e-6 <= v <= 0.05):
            raise ValueError("p_threshold must be between 0.000001 and 0.05")
        return v

    @field_validator("min_total")
    @classmethod
    def _min(cls, v: int) -> int:
        if v < 20:
            raise ValueError("min_total must be at least 20 (the chi-square approximation needs counts)")
        return v

    def shares(self, arms: list[str]) -> list[float]:
        total = sum(self.split[a] for a in arms)
        return [self.split[a] / total for a in arms]


def equal_split(arms: list[str]) -> dict[str, float]:
    """An even split in percent, rounded so the canonical form stays short; the last arm takes the rest."""
    each = round(100 / len(arms), 4)
    out = {a: each for a in arms[:-1]}
    out[arms[-1]] = round(100 - each * (len(arms) - 1), 4)
    return out


class Actions(_Model):
    win: Action = "ship_treatment"
    loss: Action = "keep_control"
    horizon: Action = "keep_control"
    guardrail_breach: Action = "keep_control"

    @model_validator(mode="after")
    def _guardrail_never_ships(self) -> "Actions":
        if self.guardrail_breach != "keep_control":
            raise ValueError("a guardrail breach must keep control")
        if self.loss != "keep_control":
            raise ValueError("a loss must keep control")
        return self


class Signer(_Model):
    name: str
    public_key: str

    @field_validator("public_key")
    @classmethod
    def _key(cls, v: str) -> str:
        from .signing import decode_public

        decode_public(v)
        return v


class Validity(_Model):
    start_within_days: int = Field(
        30, description="The signed contract can start its experiment only within this many days of the owner's "
        "signature. Once started it governs until the experiment ends.")

    @field_validator("start_within_days")
    @classmethod
    def _days(cls, v: int) -> int:
        if not (1 <= v <= 365):
            raise ValueError("start_within_days must be between 1 and 365")
        return v


class Design(_Model):
    """What the horizon was sized for, so the service can check the design is sound."""

    baseline_rate: float | None = Field(None, description="Control conversion rate the horizon was sized for")
    min_relative_lift: float | None = Field(None, description="Smallest relative lift the owner cares about")
    power: float | None = None
    override_reason: str | None = Field(
        None, description="Why the signers accept a design outside the service's policy (shown on the signing page)")

    @field_validator("override_reason")
    @classmethod
    def _prose(cls, v: str | None) -> str | None:
        return _nfc(v)


class Health(_Model):
    """Flagsmith Feature Health as a signed ship condition."""

    block_ship_when_unhealthy: bool = Field(
        True, description="Shipping the treatment is refused while Flagsmith's Feature Health reports the flag "
        "unhealthy in this experiment's environment, from any provider (Ulysses' own Webhook provider, Grafana, "
        "or another Webhook caller). Keeping control is never blocked. Absent in contracts signed before the rule "
        "existed; those ship without the check.")


class Amendment(_Model):
    supersedes: str = Field(description="Hash of the contract this one replaces")
    reason: str
    action_now: Action | None = Field(
        None, description="If set, this amendment authorises one action immediately (for example: ship early)"
    )

    @field_validator("reason")
    @classmethod
    def _prose(cls, v: str) -> str:
        return _nfc(v)


class Contract(_Model):
    ulysses_contract: int = SCHEMA_VERSION
    experiment: ExperimentRef
    hypothesis: str
    primary_metric: Metric
    guardrails: list[Guardrail] = Field(default_factory=list)
    horizon: Horizon
    boundary: Boundary
    actions: Actions = Field(default_factory=Actions)
    owner: Signer
    second_signer: Signer
    validity: Validity = Field(default_factory=Validity)
    require_guardrail_results_to_ship: bool = Field(
        True, description="A win does not ship while Flagsmith has no result for a guardrail (collecting data)")
    design: Design | None = None
    amendment: Amendment | None = None
    sample_ratio: SampleRatio | None = Field(
        None, description="Signed traffic split and mismatch test. Absent in contracts signed before the check "
        "existed; those can ship without it.")
    health: Health | None = Field(
        None, description="Feature Health rule. Absent in contracts signed before it existed.")

    @field_validator("ulysses_contract")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(f"unsupported contract version {v}")
        return v

    @field_validator("hypothesis")
    @classmethod
    def _hyp(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("hypothesis must not be empty")
        return _nfc(v)

    @model_validator(mode="after")
    def _checks(self) -> "Contract":
        if self.owner.name == self.second_signer.name or self.owner.public_key == self.second_signer.public_key:
            raise ValueError("the second signer must be a different person with a different key")
        names = [g.name for g in self.guardrails]
        if len(set(names)) != len(names):
            raise ValueError("guardrail names must be unique")
        if self.primary_metric.name in names:
            raise ValueError("the primary metric cannot also be a guardrail")
        m = len(self.experiment.treatments)
        if m > 1 and self.boundary.multiplicity is None:
            raise ValueError("several treatments need boundary.multiplicity (bonferroni) to split alpha across them")
        if m == 1 and self.boundary.multiplicity is not None:
            raise ValueError("boundary.multiplicity applies only with more than one treatment")
        if self.sample_ratio is not None and set(self.sample_ratio.split) != set(self.experiment.arms):
            raise ValueError(f"sample_ratio.split must name exactly the arms {self.experiment.arms}, "
                             f"not {sorted(self.sample_ratio.split)}")
        expected = planned_table(self.horizon, self.comparison_alpha)
        if len(self.boundary.planned) != len(expected):
            raise ValueError("boundary.planned must have one row per look; regenerate it with `ulysses contract fix`")
        for got, want in zip(self.boundary.planned, expected):
            # Exactly the table the spending rule gives (it is rounded to fixed places), so one contract
            # has one hash, and no row can show a reading (chance_to_win) that its z does not give.
            if got != want:
                raise ValueError(
                    f"boundary.planned look {got.look} does not match the signed spending rule "
                    f"(expected z={want.z}, chance_to_win={want.chance_to_win}); the table was edited or "
                    "computed with other settings"
                )
        return self

    @property
    def comparisons(self) -> int:
        """Treatment-vs-control comparisons the boundary covers."""
        return len(self.experiment.treatments)

    @property
    def comparison_alpha(self) -> float:
        """Two-sided level each comparison's boundary is built at (alpha split by Bonferroni)."""
        return comparison_alpha(self.boundary.alpha, self.comparisons)

    # ---- identity -------------------------------------------------------

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    @property
    def hash(self) -> str:
        return content_hash(self.canonical())


def comparison_alpha(alpha: float, comparisons: int) -> float:
    return alpha / comparisons if comparisons > 1 else alpha


@functools.lru_cache(maxsize=256)
def _planned_z(looks: int, alpha: float) -> tuple[float | None, ...]:
    fractions = [i / looks for i in range(1, looks + 1)]
    return tuple(bd.spending_boundaries(fractions, alpha))


def planned_table(horizon: Horizon, alpha: float) -> list[PlannedLook]:
    k = horizon.looks
    fractions = [i / k for i in range(1, k + 1)]
    zs = _planned_z(k, alpha)
    rows = []
    for i, (t, z) in enumerate(zip(fractions, zs), start=1):
        rows.append(
            PlannedLook(
                look=i,
                fraction=round(t, 6),
                identities_per_arm=int(math.ceil(horizon.identities_per_arm * t)),
                z=None if z is None else round(z, 4),
                chance_to_win=None if z is None else round(bd.z_to_chance(z), 6),
            )
        )
    return rows


def draft(
    *,
    experiment: ExperimentRef,
    hypothesis: str,
    primary_metric: Metric,
    horizon: Horizon,
    owner: Signer,
    second_signer: Signer,
    guardrails: list[Guardrail] | None = None,
    alpha: float = 0.05,
    actions: Actions | None = None,
    validity: Validity | None = None,
    amendment: Amendment | None = None,
    design: Design | None = None,
    require_guardrail_results_to_ship: bool = True,
    sample_ratio: "SampleRatio | None | Literal['equal']" = "equal",
    health: "Health | None | Literal['default']" = "default",
) -> Contract:
    """Build a contract. ``sample_ratio="equal"`` signs an even split at the default threshold; pass a
    ``SampleRatio`` for Flagsmith's actual weights, or None to sign no sample-ratio check. ``health="default"``
    signs the Feature Health rule (block a ship while Flagsmith reports the flag unhealthy); None leaves it out."""
    m = len(experiment.treatments)
    if sample_ratio == "equal":
        sample_ratio = SampleRatio(split=equal_split(experiment.arms))
    if health == "default":
        health = Health()
    return Contract(
        sample_ratio=sample_ratio,
        health=health,
        design=design,
        require_guardrail_results_to_ship=require_guardrail_results_to_ship,
        experiment=experiment,
        hypothesis=hypothesis,
        primary_metric=primary_metric,
        guardrails=guardrails or [],
        horizon=horizon,
        boundary=Boundary(alpha=alpha, multiplicity="bonferroni" if m > 1 else None,
                          planned=planned_table(horizon, comparison_alpha(alpha, m))),
        actions=actions or Actions(),
        owner=owner,
        second_signer=second_signer,
        validity=validity or Validity(),
        amendment=amendment,
    )


# ---- files -----------------------------------------------------------------

_HEADER = """\
# Ulysses stop contract.
# Sign it before the experiment starts. Its hash is written into the
# experiment's hypothesis field in Flagsmith. Editing any value changes the
# hash, and a changed contract needs fresh signatures.
"""


def to_toml(contract: Contract) -> str:
    data = contract.canonical()
    return _HEADER + "\n" + tomli_w.dumps(data)


def from_toml(text: str) -> Contract:
    return Contract.model_validate(tomllib.loads(text))


def load(path: str | Path) -> Contract:
    return from_toml(Path(path).read_text(encoding="utf-8"))


def save(contract: Contract, path: str | Path) -> Path:
    p = Path(path)
    p.write_text(to_toml(contract), encoding="utf-8")
    return p


def from_canonical(data: dict[str, Any]) -> Contract:
    return Contract.model_validate(data)


def with_regenerated_table(data: dict[str, Any]) -> Contract:
    """Rebuild ``boundary.planned`` from the horizon and alpha (for hand-edited drafts)."""
    horizon = Horizon.model_validate(data["horizon"])
    b = dict(data.get("boundary", {}))
    m = 1 + len((data.get("experiment") or {}).get("more_treatments") or [])
    if m > 1:
        b["multiplicity"] = "bonferroni"
    else:
        b.pop("multiplicity", None)
    b["planned"] = [p.model_dump(mode="json", exclude_none=True)
                    for p in planned_table(horizon, comparison_alpha(b.get("alpha", 0.05), m))]
    data = {**data, "boundary": b}
    return Contract.model_validate(data)
