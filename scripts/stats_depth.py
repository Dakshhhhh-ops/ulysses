"""Figures behind the "Several treatments", "Sample ratio" and "Power curves" parts of docs/STATISTICS.md.

Everything here is simulated synthetic units or numerical integration, never user traffic. Fixed seeds,
so a re-run prints the same numbers.

    uv run python scripts/stats_depth.py --flagsmith-src ../_shared/reference/flagsmith [--runs 2000]

Sections:

1. Boundaries against Monte Carlo (canonical sequential z model, 200,000 paths each).
2. Several treatments, canonical model: chance of shipping any do-nothing treatment when none does
   anything, with the Bonferroni split and without it.
3. Several treatments, Flagsmith's own stats.py (pinned by SHA-256) on simulated 0/1 conversions, run
   through ``decision.evaluate``: the same question end to end, at low counts where its z is skewed.
4. Sample-ratio check: how often a correct split would hold a win by chance, and how fast a broken one
   is caught.
5. Power and expected sample size at the design lift for example contracts (``ulysses.power``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy.stats import chi2

from ulysses import boundary as bd
from ulysses import contract as ct
from ulysses import power
from ulysses.calibrate import flagsmith_source
from ulysses.calibrate.offline import wilson
from ulysses.decision import Observation, evaluate
from ulysses.horizon import plan
from ulysses.signing import Keypair

MC_PATHS = 200_000


def pct(x: float, places: int = 2) -> str:
    return f"{x * 100:.{places}f}%"


def ci(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {pct(k / n, 1)} ({pct(lo, 1)}-{pct(hi, 1)})"


def mc_paths(rng, ts, reps):
    ts = np.asarray(ts, dtype=float)
    dt = np.diff(np.concatenate([[0.0], ts]))
    return np.cumsum(rng.standard_normal((reps, len(ts))) * np.sqrt(dt), axis=1) / np.sqrt(ts)


def section_boundaries(out: list[str]) -> None:
    out.append("## 1. Boundaries against Monte Carlo\n")
    out.append(f"Null hypothesis, {MC_PATHS:,} simulated paths per row (seed 101). 'Integration' is the "
               "engine's own number.\n")
    out.append("| Boundary | Looks | Level | Upper, integration | Upper, Monte Carlo | Lower, Monte Carlo |")
    out.append("|---|---|---|---|---|---|")
    rows = [("Lan-DeMets O'Brien-Fleming, two-sided", 5, 0.05), ("Lan-DeMets O'Brien-Fleming, two-sided", 30, 0.05),
            ("Lan-DeMets O'Brien-Fleming, two-sided", 12, 0.10),
            ("Same, one comparison of three (alpha/3)", 10, 0.05 / 3)]
    for name, k, a in rows:
        ts = [i / k for i in range(1, k + 1)]
        bs = bd.spending_boundaries(ts, a)
        up, _ = bd.crossing_probabilities(ts, bs, 0.0)
        z = mc_paths(np.random.default_rng(101), ts, MC_PATHS)
        b = np.array([np.inf if x is None else x for x in bs])
        hit = np.abs(z) >= b
        stopped = hit.any(axis=1)
        first = np.argmax(hit, axis=1)
        sign = z[np.arange(MC_PATHS), first] > 0
        out.append(f"| {name} | {k} | {a:.4g} two-sided | {pct(sum(up), 3)} | {pct(float(np.mean(stopped & sign)), 3)} "
                   f"| {pct(float(np.mean(stopped & ~sign)), 3)} |")
    for k, a in ((6, 0.01), (30, 0.01)):
        ts = [i / k for i in range(1, k + 1)]
        bs = bd.one_sided_boundaries(ts, a)
        z = mc_paths(np.random.default_rng(101), ts, MC_PATHS)
        b = np.array([np.inf if x is None else x for x in bs])
        out.append(f"| Lan-DeMets Pocock-type guardrail, one-sided | {k} | {a:g} one-sided | "
                   f"{pct(bd.one_sided_crossing(ts, bs), 3)} | {pct(float(np.mean((z >= b).any(axis=1))), 3)} | n/a |")
    ts = [i / 12 for i in range(1, 13)]
    bs = bd.spending_boundaries(ts, 0.05 / 3)
    out.append("")
    out.append(f"With several treatments an arm below its lower boundary keeps running, so its lower boundary is "
               f"non-binding. Its chance of crossing the upper boundary at some look, ignoring the lower one, is "
               f"{pct(bd.one_sided_crossing(ts, bs), 4)} for one comparison of three over 12 looks, against the "
               f"{pct(0.05 / 6, 4)} it is built to spend (integration): paths that fall below the lower boundary "
               "and later climb above the upper one are too rare to show at four decimal places.\n")


def section_multi_canonical(out: list[str]) -> None:
    out.append("## 2. Several treatments, canonical model\n")
    out.append(f"No treatment does anything; {MC_PATHS:,} simulated experiments per row (seed 11), 10 looks, "
               "treatments share one control (z correlation 1/2). A win ships the crossing treatment with the "
               "largest z. Contract alpha 0.05, so the promise is at most 2.5% false ships.\n")
    out.append("| Treatments | Ships a do-nothing treatment, Bonferroni (alpha/m each) | Without the split (0.05 each) |")
    out.append("|---|---|---|")
    ts = [i / 10 for i in range(1, 11)]
    for m in (1, 2, 3, 5):
        s = power.simulate(ts, bd.spending_boundaries(ts, 0.05 / m), [0.0], m, runs=MC_PATHS, seed=11)[0]
        u = power.simulate(ts, bd.spending_boundaries(ts, 0.05), [0.0], m, runs=MC_PATHS, seed=11)[0]
        out.append(f"| {m} | {pct(s['win_effective'] + s['win_other'])} | "
                   f"{pct(u['win_effective'] + u['win_other'])} |")
    out.append("")


def _keypair_signers():
    a, b = Keypair.generate(), Keypair.generate()
    return ct.Signer(name="sim-owner", public_key=a.public), ct.Signer(name="sim-second", public_key=b.public)


def _multi_contract(m: int, alpha: float, looks: int, horizon: int) -> ct.Contract:
    o, s = _keypair_signers()
    arms = [f"t{i}" for i in range(1, m + 1)]
    return ct.draft(
        experiment=ct.ExperimentRef(project=0, environment="simulation", experiment=0, flag="aa",
                                    treatment=arms[0], more_treatments=arms[1:] or None),
        hypothesis="calibration (simulated synthetic units)",
        primary_metric=ct.Metric(name="conversion", direction="increase"),
        horizon=ct.Horizon(identities_per_arm=horizon, looks=looks), owner=o, second_signer=s, alpha=alpha,
    )


def section_multi_flagsmith(out: list[str], runs: int, src: str | None) -> None:
    stats, consts, prov = flagsmith_source.load(src)
    out.append("## 3. Several treatments, Flagsmith's own statistics code\n")
    looks, per = 30, 100
    out.append(f"Flagsmith `stats.py` {prov['stats.py']['tag']} (sha256 {prov['stats.py']['sha256'][:12]}) on "
               f"{runs} simulated A/A experiments per row (seed 4242): control and every treatment at 10% "
               f"conversion, {looks} looks of {per} synthetic identities per arm, Flagsmith's own collecting-data "
               "floors, each treatment compared with the shared control by `compare_to_control`, decided by "
               "`decision.evaluate`. 95% Wilson intervals. Simulated units, not user traffic.\n")
    out.append("| Treatments | Contract | Ships a do-nothing treatment | Keeps control on a false loss |")
    out.append("|---|---|---|---|")
    for m in (1, 2, 3):
        for label, alpha in (("alpha 0.05, Bonferroni", 0.05), ("no split: each comparison at 0.05", 0.05 * m)):
            if m == 1 and label.startswith("no split"):
                continue
            c = _multi_contract(m, alpha, looks, looks * per)
            arms = c.experiment.treatments
            rng = np.random.default_rng(4242)
            wins = losses = 0
            for _ in range(runs):
                conv = rng.binomial(per, 0.10, size=(m + 1, looks)).cumsum(axis=1)
                prior: list[float] = []
                for k in range(looks):
                    n = per * (k + 1)
                    chances = {}
                    for j, a in enumerate(arms, start=1):
                        sc, st = float(conv[0, k]), float(conv[j, k])
                        if n < consts.RESULTS_MIN_IDENTITIES_PER_VARIANT or \
                                min(sc, st) < consts.RESULTS_MIN_CONVERSIONS_PER_VARIANT:
                            chances[a] = None
                            continue
                        inf = stats.compare_to_control(stats.VariantStats(n=n, sum=sc, sum_squares=sc),
                                                       stats.VariantStats(n=n, sum=st, sum_squares=st))
                        chances[a] = None if inf is None else inf.chance_to_win
                    ids = {"control": n, **{a: n for a in arms}}
                    d = evaluate(c, prior, Observation(n, n, chances[arms[0]], identities=ids,
                                                       arm_chance=chances if m > 1 else None))
                    if d.counted:
                        prior.append(d.fraction)
                    if d.outcome == "win":
                        wins += 1
                        break
                    if d.outcome == "loss":
                        losses += 1
                        break
                    if d.outcome == "horizon":
                        break
            out.append(f"| {m} | {label} | {ci(wins, runs)} | {ci(losses, runs)} |")
    out.append("")


def section_srm(out: list[str], runs: int) -> None:
    out.append("## 4. Sample-ratio check\n")
    looks, per = 30, 200
    out.append(f"Two arms signed 50/50, {looks} looks, {per} new synthetic identities per look assigned at random "
               f"with the true share below, {runs * 5:,} simulated experiments per row (seed 77). The check runs "
               f"at every look with the default threshold p < {ct.SRM_P_THRESHOLD:g} once 100 identities exist. "
               "'Any look' is how often at least one look reads a mismatch; a mismatch only ever holds a win.\n")
    out.append("| True share of control | Any look reads a mismatch | Mismatch at the final look | First look caught (median, of runs caught) |")
    out.append("|---|---|---|---|")
    reps = runs * 5
    for share in (0.50, 0.51, 0.52, 0.55):
        rng = np.random.default_rng(77)
        ctrl = rng.binomial(per, share, size=(reps, looks)).cumsum(axis=1)
        tot = per * np.arange(1, looks + 1)
        stat = (ctrl - tot / 2) ** 2 / (tot / 2) + ((tot - ctrl) - tot / 2) ** 2 / (tot / 2)
        p = chi2.sf(stat, df=1)
        bad = p < ct.SRM_P_THRESHOLD
        anyb = bad.any(axis=1)
        first = np.argmax(bad, axis=1)[anyb] + 1
        med = f"look {int(np.median(first))}" if len(first) else "n/a"
        out.append(f"| {share:.2f} | {ci(int(anyb.sum()), reps)} | {ci(int(bad[:, -1].sum()), reps)} | {med} |")
    out.append("")


def section_power(out: list[str]) -> None:
    out.append("## 5. Power and expected sample size at the design lift\n")
    out.append("From `ulysses.power` (analytic for two arms; simulation with 4,000 runs per point, seed "
               f"{power.SIM_SEED}, for several). Baseline 10%, smallest lift +25%, alpha 0.05, 80% power asked "
               "of `ulysses horizon`.\n")
    out.append("| Treatments | Looks | Horizon per arm | Method | Power at +25% | Expected identities per arm at +25% "
               "| One read at the end, same power | Expected per arm with no effect |")
    out.append("|---|---|---|---|---|---|---|---|")
    for m, k in ((1, 12), (1, 30), (3, 12)):
        hp = plan(min_relative_lift=0.25, baseline_rate=0.10, looks=k, comparisons=m)
        o, s = _keypair_signers()
        arms = [f"t{i}" for i in range(1, m + 1)]
        c = ct.draft(
            experiment=ct.ExperimentRef(project=0, environment="simulation", experiment=0, flag="f",
                                        treatment=arms[0], more_treatments=arms[1:] or None),
            hypothesis="power example", primary_metric=ct.Metric(name="conversion", direction="increase"),
            horizon=ct.Horizon(identities_per_arm=hp.identities_per_arm, looks=k), owner=o, second_signer=s,
            design=ct.Design(baseline_rate=0.10, min_relative_lift=0.25, power=0.8),
        )
        cur = power.curves_for_contract(c)
        zero = next(p for p in cur["points"] if p["x"] == 0)
        out.append(f"| {m} | {k} | {hp.identities_per_arm:,} | {cur['method']} | {pct(cur['design']['win'], 1)} | "
                   f"{cur['design']['expected_identities_per_arm']:,.0f} | {cur['fixed_sample_identities_per_arm']:,} | "
                   f"{zero['expected_identities_per_arm']:,.0f} |")
    out.append("")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--flagsmith-src", default=None)
    ap.add_argument("--runs", type=int, default=2000)
    ap.add_argument("--out", default=".run/stats-depth.md")
    a = ap.parse_args(argv)
    out = ["# Statistics depth: simulated synthetic units and numerical integration, not user traffic\n",
           "Command: `uv run python scripts/stats_depth.py --flagsmith-src <Flagsmith checkout> "
           f"--runs {a.runs}`\n"]
    section_boundaries(out)
    section_multi_canonical(out)
    section_multi_flagsmith(out, a.runs, a.flagsmith_src)
    section_srm(out, a.runs)
    section_power(out)
    text = "\n".join(out)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text, encoding="utf-8")
    sys.stdout.write(text + f"\nwritten: {a.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
