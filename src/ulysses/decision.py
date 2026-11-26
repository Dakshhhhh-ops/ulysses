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

Several treatments (``experiment.more_treatments``): each treatment-vs-control
comparison has its own boundary at alpha / m (Bonferroni over the m
comparisons; Flagsmith reports one chance to win per treatment). A look wins
when any treatment crosses its upper boundary, and the winner is the crossing
treatment with the largest z. It loses only when every treatment is below its
lower boundary at the same look. Each guardrail is judged per treatment at
its alpha / m; a breach in any arm keeps control.

Sample ratio: when the contract signs a split, every look runs a chi-square
test of Flagsmith's exposure counts against it (``srm.py``). A p-value under
the signed threshold holds a win (and at the horizon turns it into no
decision). It never ships and never stops the experiment by itself.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Literal

from . import boundary as bd
from . import srm
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
    identities: dict[str, int] | None = None
    """Identities per variation key, every arm. When absent the two counts above are used."""
    arm_chance: dict[str, float | None] | None = None
    """Several treatments: Flagsmith's oriented chance to win per treatment key."""
    arm_guardrail_worse: dict[str, dict[str, float | None]] | None = None
    """Several treatments: per treatment key, each guardrail's chance it got worse."""


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
    winner: str | None = None
    """The treatment a win ships (the crossing treatment with the largest z)."""
    arms: list[dict] = field(default_factory=list)
    """Per treatment: Flagsmith's chance to win, its z and where it stands against the boundary."""
    sample_ratio: dict | None = None
    """The sample-ratio check at this read (``srm.SampleRatioCheck.to_dict``), when the contract signs one."""

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
    return bd.spending_boundaries(fr, contract.comparison_alpha, final_spends_all=final)[-1]


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


def _counts(contract: Contract, obs: Observation) -> dict[str, int]:
    ref = contract.experiment
    if obs.identities is not None:
        return {a: int(obs.identities.get(a, 0)) for a in ref.arms}
    return {ref.control: obs.identities_control, ref.treatment: obs.identities_treatment}


def _chances(contract: Contract, obs: Observation) -> dict[str, float | None]:
    ref = contract.experiment
    if obs.arm_chance is not None:
        return {a: obs.arm_chance.get(a) for a in ref.treatments}
    return {ref.treatment: obs.chance_to_win}


def _worse(contract: Contract, obs: Observation) -> dict[str, dict[str, float | None]]:
    ref = contract.experiment
    if obs.arm_guardrail_worse is not None:
        return {a: dict(obs.arm_guardrail_worse.get(a) or {}) for a in ref.treatments}
    return {ref.treatment: dict(obs.guardrail_chance_worse)}


def _guardrail_rows(contract: Contract, worse: dict[str, dict[str, float | None]], prior: list[float], t: float,
                    final: bool, counted: bool) -> list[dict]:
    rows = []
    multi = contract.comparisons > 1
    for arm in contract.experiment.treatments:
        for g in contract.guardrails:
            w = worse.get(arm, {}).get(g.name)
            z = None if w is None else round(bd.chance_to_z(w), 4)
            a = g.alpha / contract.comparisons
            b = guardrail_boundary(a, prior, t, final) if counted else None
            if w is None:
                state = "no_result"
            elif not counted:
                state = "watch"
            elif b is not None and z is not None and z >= b:
                state = "breach"
            else:
                state = "pass"
            row = {
                "name": g.name, "direction": g.direction, "alpha": g.alpha, "chance_worse": w, "z": z,
                "boundary_z": None if b is None else round(b, 4),
                "boundary_chance": bd.z_to_chance(b), "state": state,
            }
            if multi:
                row["arm"] = arm
                row["alpha_per_arm"] = a
            rows.append(row)
    return rows


def _srm_note(s: srm.SampleRatioCheck) -> str:
    obs = ", ".join(f"{k} {v}" for k, v in s.observed.items())
    want = ", ".join(f"{k} {v:.1%}" for k, v in s.expected_share.items())
    return (f"Flagsmith counted {obs} identities against a signed split of {want}; the chi-square p-value is "
            f"{s.p_value:.2g}, below the signed threshold {s.threshold:g}")


def evaluate(contract: Contract, prior_fractions: list[float], obs: Observation) -> LookDecision:
    h = contract.horizon
    ref = contract.experiment
    multi = contract.comparisons > 1
    counts = _counts(contract, obs)
    chances = _chances(contract, obs)
    n = min(counts.values())
    t = _round_t(n / h.identities_per_arm)
    last = prior_fractions[-1] if prior_fractions else 0.0
    nxt = next_planned_fraction(h.looks, last)
    next_n = int(math.ceil(nxt * h.identities_per_arm - 1e-9))
    k = len(prior_fractions) + 1
    final = n >= h.identities_per_arm
    if final:
        t = 1.0
    scheduled = (final or n >= next_n) and t > last
    have_all = all(c is not None for c in chances.values())
    counted = scheduled and (have_all or final)
    grows = _guardrail_rows(contract, _worse(contract, obs), prior_fractions, t, final, counted)
    sr = None
    if contract.sample_ratio is not None:
        s = contract.sample_ratio
        sr = srm.check(counts, s.split, s.p_threshold, s.min_total)
    raw = {a: (None if c is None else bd.chance_to_z(c)) for a, c in chances.items()}
    zs = {a: (None if z is None else round(z, 4)) for a, z in raw.items()}
    # The headline arm: the only treatment, or the one with the largest z.
    lead = ref.treatment if not multi else max(
        (a for a in ref.treatments if zs[a] is not None), key=lambda a: zs[a], default=ref.treatment)
    lead_chance = chances.get(lead)
    base = dict(
        looks_planned=h.looks,
        fraction=t,
        identities_per_arm=n,
        chance_to_win=lead_chance,
        guardrails=grows,
        guardrail_chance_worse={(f"{r['name']}@{r['arm']}" if multi else r["name"]): r["chance_worse"]
                                for r in grows},
        sample_ratio=None if sr is None else sr.to_dict(),
    )
    z_now = zs.get(lead)

    def arm_rows(b: float | None) -> list[dict]:
        out = []
        for a in ref.treatments:
            z = raw[a]
            if z is None:
                state = "no_result"
            elif b is None:
                state = "within"
            elif z >= b:
                state = "above"
            elif z <= -b:
                state = "below"
            else:
                state = "within"
            out.append({"arm": a, "chance_to_win": chances[a], "z": zs[a], "state": state,
                        "identities": counts.get(a)})
        return out

    if not scheduled:
        return LookDecision(
            outcome="watch", action=None, counted=False, look_number=None, z=z_now,
            boundary_z=None, boundary_chance=None, next_look_at_identities=next_n, guardrail=None,
            arms=arm_rows(None) if multi else [],
            reason=(
                f"Between looks: {n} identities in the smaller arm; the next analysis is at {next_n} "
                f"(look {k} of {h.looks}). Flagsmith's chance to win is {_pct(lead_chance)}"
                + (f" ({lead}, the leading treatment)" if multi else "")
                + "; the contract does not act on it, or on guardrails, until then."
            ),
            **base,
        )

    if not counted:
        missing = [a for a, c in chances.items() if c is None]
        return LookDecision(
            outcome="not_ready", action=None, counted=False, look_number=None, z=None,
            boundary_z=None, boundary_chance=None, next_look_at_identities=next_n, guardrail=None,
            arms=arm_rows(None) if multi else [],
            reason=(
                "Flagsmith has not produced a result for the primary metric yet "
                + (f"for {', '.join(missing)} " if multi else "")
                + "(it waits for its minimum sample). This refresh is not counted as a look."
            ),
            **base,
        )

    # A counted look. Guardrails first: a breach stops and keeps control.
    breach = next((r for r in grows if r["state"] == "breach"), None)
    if breach is not None:
        where = f" on {breach['arm']}" if multi else ""
        return LookDecision(
            outcome="guardrail_breach", action=contract.actions.guardrail_breach, counted=True, look_number=k,
            z=z_now, boundary_z=None, boundary_chance=None, next_look_at_identities=None, guardrail=breach["name"],
            arms=arm_rows(None) if multi else [],
            reason=(
                f"Look {k}: guardrail '{breach['name']}' ({breach['direction'].replace('_', ' ')}){where}. Flagsmith "
                f"gives {_pct(breach['chance_worse'])} that treatment made it worse; the signed guardrail boundary at "
                f"this look is {_pct(breach['boundary_chance'])}. "
                f"Pre-signed action: {contract.actions.guardrail_breach.replace('_', ' ')}."
            ),
            **base,
        )

    if all(c is None for c in chances.values()):  # final look with no primary result
        return LookDecision(
            outcome="horizon", action=contract.actions.horizon, counted=True, look_number=k, z=None,
            boundary_z=None, boundary_chance=None, next_look_at_identities=None, guardrail=None,
            arms=arm_rows(None) if multi else [],
            reason=(
                "Horizon reached and Flagsmith still shows no result for the primary metric. "
                f"No decision; pre-signed action: {contract.actions.horizon.replace('_', ' ')}."
            ),
            **base,
        )

    b = boundary_at(contract, prior_fractions, t, final)
    b_ch = bd.z_to_chance(b)
    rows = arm_rows(b)
    common = dict(
        counted=True, look_number=k, z=None if z_now is None else round(z_now, 4),
        boundary_z=None if b is None else round(b, 4), boundary_chance=b_ch, guardrail=None,
        arms=rows if multi else [], **base,
    )
    needs = (
        "no stop is possible at this look"
        if b is None
        else f"the contract needs {_pct(b_ch)} (z {b:.2f}) to call a win, or {_pct(1 - b_ch)} to call a loss"
    )
    if not multi:
        head = (f"Look {k} at {n} identities per arm ({t:.0%} of horizon). "
                f"Flagsmith chance to win {_pct(lead_chance)}; {needs}.")
    else:
        each = "; ".join(f"{r['arm']} {_pct(r['chance_to_win'])}" for r in rows)
        head = (f"Look {k} at {n} identities in the smallest arm ({t:.0%} of horizon). Flagsmith chance to win: "
                f"{each}. Each of the {contract.comparisons} comparisons has alpha "
                f"{contract.comparison_alpha:.4g} (Bonferroni); {needs}.")
    silent_all = [r for r in grows if r["state"] == "no_result"]
    gnote = "" if not grows else (
        " Guardrails: " + "; ".join(
            f"{r['name']}" + (f" ({r['arm']})" if multi else "") + " "
            + ("no Flagsmith result yet" if r["chance_worse"] is None else f"{_pct(r['chance_worse'])} chance worse")
            for r in grows) + ".")
    srm_bad = sr is not None and sr.blocks_win
    snote = (" Sample ratio: " + _srm_note(sr) + "; a win would be held.") if srm_bad else ""

    crossers = [r for r in rows if r["state"] == "above"]
    if crossers:
        win = max(crossers, key=lambda r: raw[r["arm"]])
        winner = win["arm"]
        lead_win = f" {winner} crosses its boundary (z {win['z']:.2f})." if multi else ""
        silent = [r["name"] for r in silent_all if not multi or r.get("arm") == winner]
        common_w = {**common, "z": win["z"], "chance_to_win": win["chance_to_win"]}
        if silent and contract.require_guardrail_results_to_ship:
            if final:
                return LookDecision(
                    outcome="horizon", action=contract.actions.horizon, next_look_at_identities=None,
                    reason=(f"{head}{lead_win} The win boundary is crossed, but Flagsmith never produced a result for "
                            f"guardrail {', '.join(silent)} (it needs 5 conversions per arm), and the contract "
                            "does not ship unchecked. No decision; pre-signed action: "
                            f"{contract.actions.horizon.replace('_', ' ')}."),
                    **common_w,
                )
            following = next_planned_fraction(h.looks, t)
            return LookDecision(
                outcome="hold", action=None,
                next_look_at_identities=int(math.ceil(following * h.identities_per_arm - 1e-9)),
                reason=(f"{head}{lead_win} The win boundary is crossed, but guardrail {', '.join(silent)} has no "
                        "Flagsmith result yet (it needs 5 conversions per arm), and the contract does not ship "
                        "unchecked. Hold."),
                **common_w,
            )
        if srm_bad:
            why = (f" The win boundary is crossed, but the traffic split is broken: {_srm_note(sr)}. When the arms "
                   "did not get the identities the contract signed for, they are not comparable, so the contract "
                   "does not ship.")
            if final:
                return LookDecision(
                    outcome="horizon", action=contract.actions.horizon, next_look_at_identities=None,
                    reason=(f"{head}{lead_win}{why} No decision; pre-signed action: "
                            f"{contract.actions.horizon.replace('_', ' ')}."),
                    **common_w,
                )
            following = next_planned_fraction(h.looks, t)
            return LookDecision(
                outcome="hold", action=None,
                next_look_at_identities=int(math.ceil(following * h.identities_per_arm - 1e-9)),
                reason=f"{head}{lead_win}{why} Hold.",
                **common_w,
            )
        return LookDecision(
            outcome="win", action=contract.actions.win, next_look_at_identities=None, winner=winner,
            reason=(f"{head}{lead_win} Boundary crossed: win.{gnote} Pre-signed action: "
                    f"{contract.actions.win.replace('_', ' ')}" + (f" ({winner})" if multi else "") + "."),
            **common_w,
        )
    if b is not None and rows and all(r["state"] == "below" for r in rows):
        return LookDecision(
            outcome="loss", action=contract.actions.loss, next_look_at_identities=None,
            reason=(f"{head} Boundary crossed: loss" + (" for every treatment" if multi else "")
                    + f". Pre-signed action: {contract.actions.loss.replace('_', ' ')}."),
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
        reason=f"{head} Hold.{gnote}{snote}",
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

    def primary(arm: str) -> float | None:
        name = contract.primary_metric.name
        m = snap.metrics.get(f"{name}@{arm}")
        if m is None and arm == ref.treatment and contract.comparisons == 1:
            m = snap.metrics.get(name)
        inc = None if m is None else m.chance_of_increase()
        if inc is None:
            return None
        return inc if contract.primary_metric.direction == "increase" else 1.0 - inc

    def worse(arm: str) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for g in contract.guardrails:
            m = snap.metrics.get(f"{g.name}@{arm}")
            if m is None and arm == ref.treatment and contract.comparisons == 1:
                m = snap.metrics.get(g.name)
            inc = None if m is None else m.chance_of_increase()
            out[g.name] = None if inc is None else (inc if g.direction == "not_increase" else 1.0 - inc)
        return out

    nc = int(snap.identities.get(ref.control, 0))
    nt = int(snap.identities.get(ref.treatment, 0))
    ids = {a: int(snap.identities.get(a, 0)) for a in ref.arms}
    if contract.comparisons == 1:
        return Observation(nc, nt, primary(ref.treatment), worse(ref.treatment), identities=ids)
    arm_chance = {a: primary(a) for a in ref.treatments}
    arm_worse = {a: worse(a) for a in ref.treatments}
    return Observation(nc, nt, arm_chance[ref.treatment], arm_worse[ref.treatment], identities=ids,
                       arm_chance=arm_chance, arm_guardrail_worse=arm_worse)
