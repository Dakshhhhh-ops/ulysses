"""One look: compare Flagsmith's chance-to-win with the signed boundary.

This is a pure function of the contract, the information fractions of earlier
counted looks, and one fresh read of Flagsmith's results. It does no
statistics of its own beyond the boundary: the only number it derives is
z = Phi^-1(chance to win), which inverts Flagsmith's own
``chance_to_win = Phi(lift / SE)``.

Scheduling: the contract plans ``looks`` analyses at equal steps of
identities per arm. A refresh only counts as an analysis when the smaller arm
has reached the next planned step; refreshes in between are shown as "watch"
and spend nothing. Because the boundary comes from an alpha-spending function,
an analysis that lands a little after its planned step simply gets the
boundary for the fraction it actually has.

Guardrails are judged only at counted looks, each against its own signed
one-sided boundary (Lan-DeMets Pocock-type spending at the guardrail's alpha),
on z = Phi^-1(Flagsmith's chance that treatment moved it the wrong way).
Between looks their numbers are shown but nothing stops. A guardrail stop
never ships. By default a win does not ship while Flagsmith has no result for
a guardrail.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Literal

from . import boundary as bd
from .contract import Contract
from .flagsmith.gateway import ResultsSnapshot

Outcome = Literal["not_ready", "watch", "hold", "win", "loss", "horizon", "guardrail_breach"]
FIRES = {"win", "loss", "horizon", "guardrail_breach"}


@dataclass(frozen=True)
class Observation:
    """One read of Flagsmith's results, already mapped from the API payload."""

    identities_control: int
    identities_treatment: int
    chance_to_win: float | None
    """Flagsmith's chance that treatment beats control on the primary metric,
    oriented by the metric's direction (Flagsmith flips it for lower-is-better
    metrics). ``None`` when Flagsmith shows no result yet."""
    guardrail_chance_worse: dict[str, float | None] = field(default_factory=dict)
    """For each guardrail, Flagsmith's chance that treatment moved it the wrong way."""


@dataclass(frozen=True)
class LookDecision:
    outcome: Outcome
    action: str | None
    counted: bool
    look_number: int | None
    looks_planned: int
    fraction: float
    identities_per_arm: int
    chance_to_win: float | None
    z: float | None
    boundary_z: float | None
    boundary_chance: float | None
    next_look_at_identities: int | None
    guardrail: str | None
    reason: str
    guardrails: list[dict] = field(default_factory=list)
    """Per guardrail: Flagsmith's chance worse, its z, the signed boundary at this look and the state
    (pass, breach, no_result, or watch between looks)."""
    guardrail_chance_worse: dict = field(default_factory=dict)

    @property
    def fires(self) -> bool:
        return self.outcome in FIRES

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fires"] = self.fires
        return d


def _round_t(t: float) -> float:
    return round(min(max(t, 0.0), 1.0), 6)


def next_planned_fraction(looks: int, last_fraction: float) -> float:
    for j in range(1, looks + 1):
        f = j / looks
        # Fractions are stored rounded to six places, so compare with a tolerance
        # well under the smallest gap between planned looks (1/200).
        if f > last_fraction + 1e-6:
            return f
    return 1.0


def boundary_at(contract: Contract, prior_fractions: list[float], t: float, final: bool) -> float | None:
    fr = [*prior_fractions, t]
    return bd.spending_boundaries(fr, contract.boundary.alpha, final_spends_all=final)[-1]


def _pct(x: float | None) -> str:
    """A percentage that never rounds a boundary up to 100% or down to 0%."""
    if x is None:
        return "n/a"
    for places in (2, 4, 6, 8):
        text = f"{x * 100:.{places}f}"
        if 0.0 < float(text) < 100.0 or x in (0.0, 1.0):
            return text + "%"
    return f"{x * 100:.8f}%"


def guardrail_boundary(alpha: float, prior_fractions: list[float], t: float, final: bool) -> float | None:
    return bd.one_sided_boundaries([*prior_fractions, t], alpha, final_spends_all=final)[-1]


def _guardrail_rows(contract: Contract, obs: Observation, prior: list[float], t: float, final: bool,
                    counted: bool) -> list[dict]:
    rows = []
    for g in contract.guardrails:
        w = obs.guardrail_chance_worse.get(g.name)
        z = None if w is None else round(bd.chance_to_z(w), 4)
        b = guardrail_boundary(g.alpha, prior, t, final) if counted else None
        if w is None:
            state = "no_result"
        elif not counted:
            state = "watch"
        elif b is not None and z is not None and z >= b:
            state = "breach"
        else:
            state = "pass"
        rows.append({
            "name": g.name, "direction": g.direction, "alpha": g.alpha, "chance_worse": w, "z": z,
            "boundary_z": None if b is None else round(b, 4),
            "boundary_chance": bd.z_to_chance(b), "state": state,
        })
    return rows


def evaluate(contract: Contract, prior_fractions: list[float], obs: Observation) -> LookDecision:
    h = contract.horizon
    n = min(obs.identities_control, obs.identities_treatment)
    t = _round_t(n / h.identities_per_arm)
    last = prior_fractions[-1] if prior_fractions else 0.0
    nxt = next_planned_fraction(h.looks, last)
    next_n = int(math.ceil(nxt * h.identities_per_arm - 1e-9))
    k = len(prior_fractions) + 1
    final = n >= h.identities_per_arm
    if final:
        t = 1.0
    scheduled = (final or n >= next_n) and t > last
    counted = scheduled and (obs.chance_to_win is not None or final)
    grows = _guardrail_rows(contract, obs, prior_fractions, t, final, counted)
    base = dict(
        looks_planned=h.looks,
        fraction=t,
        identities_per_arm=n,
        chance_to_win=obs.chance_to_win,
        guardrails=grows,
        guardrail_chance_worse={r["name"]: r["chance_worse"] for r in grows},
    )
    z_now = None if obs.chance_to_win is None else round(bd.chance_to_z(obs.chance_to_win), 4)

    if not scheduled:
        return LookDecision(
            outcome="watch", action=None, counted=False, look_number=None, z=z_now,
            boundary_z=None, boundary_chance=None, next_look_at_identities=next_n, guardrail=None,
            reason=(
                f"Between looks: {n} identities in the smaller arm; the next analysis is at {next_n} "
                f"(look {k} of {h.looks}). Flagsmith's chance to win is {_pct(obs.chance_to_win)}; "
                "the contract does not act on it, or on guardrails, until then."
            ),
            **base,
        )

    if not counted:
        return LookDecision(
            outcome="not_ready", action=None, counted=False, look_number=None, z=None,
            boundary_z=None, boundary_chance=None, next_look_at_identities=next_n, guardrail=None,
            reason=(
                "Flagsmith has not produced a result for the primary metric yet "
                "(it waits for its minimum sample). This refresh is not counted as a look."
            ),
            **base,
        )

    # A counted look. Guardrails first: a breach stops and keeps control.
    breach = next((r for r in grows if r["state"] == "breach"), None)
    if breach is not None:
        return LookDecision(
            outcome="guardrail_breach", action=contract.actions.guardrail_breach, counted=True, look_number=k,
            z=z_now, boundary_z=None, boundary_chance=None, next_look_at_identities=None, guardrail=breach["name"],
            reason=(
                f"Look {k}: guardrail '{breach['name']}' ({breach['direction'].replace('_', ' ')}). Flagsmith gives "
                f"{_pct(breach['chance_worse'])} that treatment made it worse; the signed guardrail boundary at this "
                f"look is {_pct(breach['boundary_chance'])}. "
                f"Pre-signed action: {contract.actions.guardrail_breach.replace('_', ' ')}."
            ),
            **base,
        )

    if obs.chance_to_win is None:  # final look with no primary result
        return LookDecision(
            outcome="horizon", action=contract.actions.horizon, counted=True, look_number=k, z=None,
            boundary_z=None, boundary_chance=None, next_look_at_identities=None, guardrail=None,
            reason=(
                "Horizon reached and Flagsmith still shows no result for the primary metric. "
                f"No decision; pre-signed action: {contract.actions.horizon.replace('_', ' ')}."
            ),
            **base,
        )

    z = bd.chance_to_z(obs.chance_to_win)
    b = boundary_at(contract, prior_fractions, t, final)
    b_ch = bd.z_to_chance(b)
    common = dict(
        counted=True, look_number=k, z=round(z, 4),
        boundary_z=None if b is None else round(b, 4), boundary_chance=b_ch, guardrail=None, **base,
    )
    needs = (
        "no stop is possible at this look"
        if b is None
        else f"the contract needs {_pct(b_ch)} (z {b:.2f}) to call a win, or {_pct(1 - b_ch)} to call a loss"
    )
    head = (f"Look {k} at {n} identities per arm ({t:.0%} of horizon). "
            f"Flagsmith chance to win {_pct(obs.chance_to_win)}; {needs}.")
    silent = [r["name"] for r in grows if r["state"] == "no_result"]
    gnote = "" if not grows else (
        " Guardrails: " + "; ".join(
            f"{r['name']} " + ("no Flagsmith result yet" if r["chance_worse"] is None
                               else f"{_pct(r['chance_worse'])} chance worse")
            for r in grows) + ".")

    if b is not None and z >= b:
        if silent and contract.require_guardrail_results_to_ship:
            if final:
                return LookDecision(
                    outcome="horizon", action=contract.actions.horizon, next_look_at_identities=None,
                    reason=(f"{head} The win boundary is crossed, but Flagsmith never produced a result for "
                            f"guardrail {', '.join(silent)} (it needs 5 conversions per arm), and the contract "
                            "does not ship unchecked. No decision; pre-signed action: "
                            f"{contract.actions.horizon.replace('_', ' ')}."),
                    **common,
                )
            following = next_planned_fraction(h.looks, t)
            return LookDecision(
                outcome="hold", action=None,
                next_look_at_identities=int(math.ceil(following * h.identities_per_arm - 1e-9)),
                reason=(f"{head} The win boundary is crossed, but guardrail {', '.join(silent)} has no Flagsmith "
                        "result yet (it needs 5 conversions per arm), and the contract does not ship unchecked. Hold."),
                **common,
            )
        return LookDecision(
            outcome="win", action=contract.actions.win, next_look_at_identities=None,
            reason=f"{head} Boundary crossed: win.{gnote} Pre-signed action: {contract.actions.win.replace('_', ' ')}.",
            **common,
        )
    if b is not None and z <= -b:
        return LookDecision(
            outcome="loss", action=contract.actions.loss, next_look_at_identities=None,
            reason=f"{head} Boundary crossed: loss. Pre-signed action: {contract.actions.loss.replace('_', ' ')}.",
            **common,
        )
    if final:
        return LookDecision(
            outcome="horizon", action=contract.actions.horizon, next_look_at_identities=None,
            reason=(f"{head} Horizon reached without crossing: no decision. "
                    f"Pre-signed action: {contract.actions.horizon.replace('_', ' ')}."),
            **common,
        )
    following = next_planned_fraction(h.looks, t)
    return LookDecision(
        outcome="hold", action=None,
        next_look_at_identities=int(math.ceil(following * h.identities_per_arm - 1e-9)),
        reason=f"{head} Hold.{gnote}",
        **common,
    )


def observation_from_snapshot(contract: Contract, snap: ResultsSnapshot | None) -> Observation:
    """Map Flagsmith's results payload onto the contract's arms and metrics.

    The only arithmetic is orientation: Flagsmith reports chance_to_win for a
    metric's own polarity (it flips Phi(lift/SE) for lower-is-better metrics);
    the contract states its own direction, so the flip is undone and re-applied.
    """
    ref = contract.experiment
    if snap is None:
        return Observation(0, 0, None, {})
    nc = int(snap.identities.get(ref.control, 0))
    nt = int(snap.identities.get(ref.treatment, 0))
    prim = snap.metrics.get(f"{contract.primary_metric.name}@{ref.treatment}") or snap.metrics.get(
        contract.primary_metric.name
    )
    chance = None
    if prim is not None:
        inc = prim.chance_of_increase()
        if inc is not None:
            chance = inc if contract.primary_metric.direction == "increase" else 1.0 - inc
    worse: dict[str, float | None] = {}
    for g in contract.guardrails:
        m = snap.metrics.get(f"{g.name}@{ref.treatment}") or snap.metrics.get(g.name)
        inc = None if m is None else m.chance_of_increase()
        if inc is None:
            worse[g.name] = None
        else:
            worse[g.name] = inc if g.direction == "not_increase" else 1.0 - inc
    return Observation(nc, nt, chance, worse)
