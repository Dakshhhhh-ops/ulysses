"""Offline engine calibration: Flagsmith's unmodified statistics code on simulated experiments.

Each simulated experiment has two arms. At every look, ``per_arm_per_look`` more synthetic identities
arrive in each arm; their 0/1 conversions are summed into Flagsmith's ``VariantStats`` and passed to
Flagsmith's own ``compare_to_control`` (loaded from ``stats.py`` by hash, see ``flagsmith_source.py``).
Flagsmith's own "collecting data" floors (from its ``constants.py``) decide whether a result is shown.

Rules scored on the same chance-to-win values:

* first look over 95% or under 5%: stop the first time the chance to beat control is over 95%
  (Flagsmith's docs call that Winning) or under 5% (Losing). This is a two-sided 10%-level rule;
* the same thresholds read once, at the final look (its single-look error rate);
* the Ulysses contract (``decision.evaluate``) at alpha 0.05 and at alpha 0.10. Alpha 0.10 is the
  like-for-like comparison with the 95%/5% thresholds.

A false Winning is the costly error: shipping a variation that does nothing. A false Losing keeps
control. Guardrails are simulated as a second, independent 0/1 metric with no effect, scored under
the old fixed rule (stop when the chance worse reaches 97.5% at any refresh) and the signed one-sided
spending boundary the contract now uses.

Simulated synthetic units, not user traffic.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .. import boundary as bd
from .. import contract as ct
from ..decision import Observation, evaluate
from ..horizon import plan as horizon_plan
from ..signing import Keypair
from . import flagsmith_source

WIN, LOSE = 0.95, 0.05  # the levels Flagsmith's docs call Winning and Losing
OLD_GUARDRAIL_RULE = 0.975  # the fixed per-refresh threshold the first version used
DEFAULT_SEEDS = (62944, 101, 202, 303, 404)


@dataclass
class RuleCount:
    runs: int = 0
    winning: int = 0
    losing: int = 0
    stop_looks: list[int] = field(default_factory=list)
    used: list[int] = field(default_factory=list)  # identities per arm used, no-call runs at the horizon

    @property
    def any_call(self) -> int:
        return self.winning + self.losing

    def rate(self, n: int) -> float:
        return n / self.runs if self.runs else float("nan")

    def merge(self, o: "RuleCount") -> None:
        self.runs += o.runs
        self.winning += o.winning
        self.losing += o.losing
        self.stop_looks += o.stop_looks
        self.used += o.used

    def summary(self) -> dict:
        return {
            "runs": self.runs, "winning": self.winning, "losing": self.losing, "any_call": self.any_call,
            "winning_rate": round(self.rate(self.winning), 4), "losing_rate": round(self.rate(self.losing), 4),
            "any_call_rate": round(self.rate(self.any_call), 4),
            "mean_stop_look": round(float(np.mean(self.stop_looks)), 2) if self.stop_looks else None,
            "mean_identities_per_arm_used": round(float(np.mean(self.used)), 1) if self.used else None,
        }


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _contract(alpha: float, looks: int, horizon: int, guardrail: str | None = None) -> ct.Contract:
    a, b = Keypair.generate(), Keypair.generate()
    return ct.draft(
        experiment=ct.ExperimentRef(project=0, environment="simulation", experiment=0, flag="aa", treatment="b"),
        hypothesis="calibration (simulated synthetic units)",
        primary_metric=ct.Metric(name="conversion", direction="increase"),
        guardrails=[ct.Guardrail(name="guard", direction=guardrail)] if guardrail else [],
        horizon=ct.Horizon(identities_per_arm=horizon, looks=looks),
        owner=ct.Signer(name="calibration-owner", public_key=a.public),
        second_signer=ct.Signer(name="calibration-second", public_key=b.public),
        alpha=alpha,
    )


def _chances(stats, consts, conv_c, conv_t, per_arm: int, looks: int) -> list[float | None]:
    out: list[float | None] = []
    for k in range(looks):
        n = per_arm * (k + 1)
        sc, st = float(conv_c[k]), float(conv_t[k])
        if n < consts.RESULTS_MIN_IDENTITIES_PER_VARIANT or sc < consts.RESULTS_MIN_CONVERSIONS_PER_VARIANT \
                or st < consts.RESULTS_MIN_CONVERSIONS_PER_VARIANT:
            out.append(None)
            continue
        inf = stats.compare_to_control(stats.VariantStats(n=n, sum=sc, sum_squares=sc),
                                       stats.VariantStats(n=n, sum=st, sum_squares=st))
        out.append(None if inf is None else inf.chance_to_win)
    return out


def simulate(
    *,
    runs: int = 2000,
    looks: int = 30,
    per_arm_per_look: int = 100,
    base_rate: float = 0.10,
    relative_lift: float = 0.0,
    alphas: tuple[float, ...] = (0.05, 0.10),
    seed: int = 62944,
    guardrail_rate: float | None = 0.10,
    flagsmith_src: str | None = None,
    keep_paths: int = 0,
    engine: tuple | None = None,
) -> dict:
    """``keep_paths`` keeps the first that many runs' chance-to-win per look and each rule's stop, for the
    per-run export (it reads the same draws, so the counts do not change). ``engine`` replaces the
    Flagsmith source with (stats, constants, provenance), for tests only."""
    stats, consts, provenance = engine or flagsmith_source.load(flagsmith_src)
    rng = np.random.default_rng(seed)
    horizon = looks * per_arm_per_look
    contracts = {a: _contract(a, looks, horizon) for a in alphas}
    guard_contracts = {d: _contract(0.05, looks, horizon, d) for d in ("not_increase", "not_decrease")}
    banner_any, banner_final = RuleCount(runs), RuleCount(runs)
    rule = {a: RuleCount(runs) for a in alphas}
    g_old = {d: 0 for d in guard_contracts}
    g_new = {d: 0 for d in guard_contracts}
    p_c, p_t = base_rate, base_rate * (1 + relative_lift)
    kept: list[dict] = []
    t0 = time.time()
    for run_i in range(runs):
        chances = _chances(stats, consts, rng.binomial(per_arm_per_look, p_c, size=looks).cumsum(),
                           rng.binomial(per_arm_per_look, p_t, size=looks).cumsum(), per_arm_per_look, looks)
        keep = run_i < keep_paths
        if keep:
            kept.append({"run": run_i + 1, "chance_to_win": [None if c is None else round(float(c), 6) for c in chances],
                         "first_exit": None, "contract": {str(a): None for a in alphas}})
        for k, ch in enumerate(chances):
            if ch is not None and (ch > WIN or ch < LOSE):
                if keep:
                    kept[-1]["first_exit"] = {"look": k + 1, "side": "winning" if ch > WIN else "losing"}
                if ch > WIN:
                    banner_any.winning += 1
                else:
                    banner_any.losing += 1
                banner_any.stop_looks.append(k + 1)
                banner_any.used.append(per_arm_per_look * (k + 1))
                break
        else:
            banner_any.used.append(horizon)
        last = chances[-1]
        if last is not None and last > WIN:
            banner_final.winning += 1
        elif last is not None and last < LOSE:
            banner_final.losing += 1
        for a, c in contracts.items():
            prior: list[float] = []
            stopped = False
            for k, ch in enumerate(chances):
                n = per_arm_per_look * (k + 1)
                d = evaluate(c, prior, Observation(n, n, ch))
                if d.counted:
                    prior.append(d.fraction)
                if d.outcome in ("win", "loss"):
                    if keep:
                        kept[-1]["contract"][str(a)] = {"look": k + 1, "outcome": d.outcome}
                    if d.outcome == "win":
                        rule[a].winning += 1
                    else:
                        rule[a].losing += 1
                    rule[a].stop_looks.append(k + 1)
                    rule[a].used.append(n)
                    stopped = True
                    break
                if d.outcome == "horizon":
                    break
            if not stopped:
                rule[a].used.append(horizon)
        if guardrail_rate is not None:
            # A guardrail metric with no effect: both arms at the same rate, independent of the primary.
            g = _chances(stats, consts, rng.binomial(per_arm_per_look, guardrail_rate, size=looks).cumsum(),
                         rng.binomial(per_arm_per_look, guardrail_rate, size=looks).cumsum(), per_arm_per_look, looks)
            for d_name, gc in guard_contracts.items():
                worse = [None if x is None else (x if d_name == "not_increase" else 1 - x) for x in g]
                if any(w is not None and w >= OLD_GUARDRAIL_RULE for w in worse):
                    g_old[d_name] += 1
                prior = []
                for k, w in enumerate(worse):
                    n = per_arm_per_look * (k + 1)
                    d = evaluate(gc, prior, Observation(n, n, 0.5, {"guard": w}))
                    if d.counted:
                        prior.append(d.fraction)
                    if d.outcome == "guardrail_breach":
                        g_new[d_name] += 1
                        break
                    if d.outcome == "horizon":
                        break
    return {
        "label": "Offline engine calibration on simulated synthetic units (not user traffic)",
        "engine": provenance,
        "parameters": {
            "runs": runs, "looks": looks, "identities_per_arm_per_look": per_arm_per_look,
            "base_rate": base_rate, "relative_lift": relative_lift, "seed": seed, "guardrail_rate": guardrail_rate,
            "floors": {"min_identities_per_variant": consts.RESULTS_MIN_IDENTITIES_PER_VARIANT,
                       "min_conversions_per_variant": consts.RESULTS_MIN_CONVERSIONS_PER_VARIANT},
            "banner_thresholds": {"winning_over": WIN, "losing_under": LOSE},
        },
        "banner_every_look": banner_any.summary(),
        "banner_final_look_only": banner_final.summary(),
        "contract": {str(a): rule[a].summary() for a in alphas},
        "guardrail_false_stops": None if guardrail_rate is None else {
            d: {"old_fixed_97_5_every_look": g_old[d], "signed_pocock": g_new[d], "guardrail_alpha": ct.GUARDRAIL_ALPHA, "runs": runs}
            for d in g_old},
        "seconds": round(time.time() - t0, 1),
        "_paths": kept,
        "_counts": {"banner_any": banner_any, "banner_final": banner_final, "rule": rule,
                    "g_old": g_old, "g_new": g_new},
    }


def pooled(seeds: tuple[int, ...], keep_paths: int = 0, **kw) -> dict:
    """The same design over several seeds, counts added up. ``keep_paths`` keeps the first runs of the first seed."""
    parts = [simulate(seed=s, keep_paths=keep_paths if i == 0 else 0, **kw) for i, s in enumerate(seeds)]
    first = parts[0]
    ba, bf = RuleCount(), RuleCount()
    rule = {a: RuleCount() for a in first["_counts"]["rule"]}
    g_old = {d: 0 for d in first["_counts"]["g_old"]}
    g_new = {d: 0 for d in first["_counts"]["g_new"]}
    for p in parts:
        c = p["_counts"]
        ba.merge(c["banner_any"])
        bf.merge(c["banner_final"])
        for a in rule:
            rule[a].merge(c["rule"][a])
        for d in g_old:
            g_old[d] += c["g_old"][d]
            g_new[d] += c["g_new"][d]
    n = sum(p["parameters"]["runs"] for p in parts)
    out = {k: v for k, v in first.items() if k not in ("_counts", "_paths")}
    out["_paths"] = first["_paths"]
    out["parameters"] = {**first["parameters"], "runs": n, "seed": list(seeds)}
    out["banner_every_look"] = ba.summary()
    out["banner_final_look_only"] = bf.summary()
    out["contract"] = {str(a): rule[a].summary() for a in rule}
    out["guardrail_false_stops"] = None if first["parameters"]["guardrail_rate"] is None else {
        d: {"old_fixed_97_5_every_look": g_old[d], "signed_pocock": g_new[d], "guardrail_alpha": ct.GUARDRAIL_ALPHA, "runs": n} for d in g_old}
    out["seconds"] = round(sum(p["seconds"] for p in parts), 1)
    return out


PATHS_FILE = "paths-offline.json"
DEFAULT_PATHS = 100
PATHS_COMMAND = "uv run ulysses calibrate offline --flagsmith-src ../_shared/reference/flagsmith"


def paths_export(sim: dict) -> dict:
    """The per-run export: for the kept runs, Flagsmith's chance to win at every look and where each rule stops.

    ``sim`` is a ``simulate`` result (or the first seed of ``pooled``) made with ``keep_paths``. The kept runs
    are the first runs of that seed, so they are the opening runs of the pooled table, drawn from the same
    random numbers. ``counts`` are recounted from the runs themselves; a test holds them equal to
    ``simulate``'s own summary for the same number of runs.
    """
    runs = sim["_paths"]
    par = sim["parameters"]
    seed = par["seed"][0] if isinstance(par["seed"], list) else par["seed"]
    alphas = list(runs[0]["contract"]) if runs else []
    first = {"winning": sum(1 for r in runs if r["first_exit"] and r["first_exit"]["side"] == "winning"),
             "losing": sum(1 for r in runs if r["first_exit"] and r["first_exit"]["side"] == "losing")}
    contract = {a: {"win": sum(1 for r in runs if (r["contract"][a] or {}).get("outcome") == "win"),
                    "loss": sum(1 for r in runs if (r["contract"][a] or {}).get("outcome") == "loss")}
                for a in alphas}
    return {
        "label": "Per-run paths from the offline engine calibration, simulated synthetic units (not user traffic)",
        "engine": {k: {"tag": v.get("tag"), "sha256": v.get("sha256")} for k, v in sim["engine"].items()},
        "parameters": {"runs": len(runs), "seed": seed, "first_run": 1, "looks": par["looks"],
                       "identities_per_arm_per_look": par["identities_per_arm_per_look"],
                       "base_rate": par["base_rate"], "relative_lift": par["relative_lift"],
                       "alphas": [float(a) for a in alphas], "banner_thresholds": par["banner_thresholds"],
                       "note": f"runs 1-{len(runs)} of seed {seed}: the opening runs of the pooled A/A table"},
        "rules": {"first_exit": "stop at the first look over 95% (Winning: ship) or under 5% (Losing: keep control)",
                  "contract": "Ulysses contract (decision.evaluate) at each alpha: win ships, loss keeps control"},
        "counts": {"first_exit": first, "contract": contract},
        "runs": runs,
        "command": PATHS_COMMAND,
    }


def dump_export(export: dict) -> str:
    """JSON with one line per run, so a committed copy diffs run by run."""
    head = {k: v for k, v in export.items() if k != "runs"}
    body = ",\n".join("  " + json.dumps(r, separators=(",", ":")) for r in export["runs"])
    text = json.dumps(head, indent=1)
    return text[:-2] + ',\n "runs": [\n' + body + "\n ]\n}\n"


def export_paths(out_dir: str | Path = ".run/calibration", *, runs: int = DEFAULT_PATHS, seed: int = DEFAULT_SEEDS[0],
                 looks: int = 30, per_arm_per_look: int = 100, flagsmith_src: str | None = None,
                 engine: tuple | None = None) -> Path:
    """Only the per-run export, in seconds: the same runs ``run`` keeps (same seed, same draws)."""
    sim = simulate(runs=runs, looks=looks, per_arm_per_look=per_arm_per_look, seed=seed, keep_paths=runs,
                   flagsmith_src=flagsmith_src, engine=engine)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / PATHS_FILE
    path.write_text(dump_export(paths_export(sim)), encoding="utf-8")
    return path


def _pct(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{100 * k / n:.1f}% ({100 * lo:.1f}-{100 * hi:.1f})"


def markdown(r: dict) -> str:
    aa, pw = r["aa"], r["power"]
    p = aa["parameters"]
    n = p["runs"]
    c05, c10 = aa["contract"]["0.05"], aa["contract"]["0.1"]
    lines = [
        f"Flagsmith `stats.py` ({aa['engine']['stats.py']['tag']}, sha256 `{aa['engine']['stats.py']['sha256'][:12]}`) "
        f"on {n} simulated A/A experiments ({len(p['seed'])} seeds pooled: {', '.join(map(str, p['seed']))}), "
        f"{p['looks']} looks of {p['identities_per_arm_per_look']} synthetic identities per arm, base rate "
        f"{p['base_rate']:.0%}. Simulated units, not user traffic. 95% Wilson intervals.",
        "",
        "False ships: how often a variation that does nothing would be shipped.",
        "",
        "| Rule | Level | Ships a do-nothing variation (false Winning) |",
        "|---|---|---|",
        f"| Read once at the final look (95%/5%) | 10% two-sided | {_pct(aa['banner_final_look_only']['winning'], n)} |",
        f"| Stop at the first look over 95% or under 5% | 10% two-sided, read {p['looks']} times | {_pct(aa['banner_every_look']['winning'], n)} |",
        f"| Ulysses contract, alpha 0.10 | 10% two-sided | {_pct(c10['winning'], n)} |",
        f"| Ulysses contract, alpha 0.05 (default) | 5% two-sided | {_pct(c05['winning'], n)} |",
        "",
        "Any false call, counting false Losing too (a false Losing keeps control, so it ships nothing):",
        "",
        "| Rule | False Winning | False Losing | Any false call |",
        "|---|---|---|---|",
    ]
    for name, s in (("Read once at the final look", aa["banner_final_look_only"]),
                    ("Stop at the first look over 95% or under 5%", aa["banner_every_look"]),
                    ("Ulysses contract, alpha 0.10", c10), ("Ulysses contract, alpha 0.05", c05)):
        lines.append(f"| {name} | {_pct(s['winning'], n)} | {_pct(s['losing'], n)} | {_pct(s['any_call'], n)} |")
    pp = pw["parameters"]
    m = pp["runs"]
    sv = r["savings"]
    lines += [
        "",
        f"Same design with a true relative lift of {pp['relative_lift']:+.0%} ({pp['base_rate']:.0%} to "
        f"{pp['base_rate'] * (1 + pp['relative_lift']):.1%}), {m} runs:",
        "",
        "| Rule | Calls the win | Wrong-way call | Mean look of the first call | Mean identities per arm used (all runs) |",
        "|---|---|---|---|---|",
    ]
    for name, s in (("Stop at the first look over 95% or under 5%", pw["banner_every_look"]),
                    ("Ulysses contract, alpha 0.10", pw["contract"]["0.1"]),
                    ("Ulysses contract, alpha 0.05", pw["contract"]["0.05"])):
        lines.append(f"| {name} | {_pct(s['winning'], m)} | {_pct(s['losing'], m)} | {s['mean_stop_look']} | "
                     f"{s['mean_identities_per_arm_used']} |")
    lines += [
        "",
        f"Against a fixed-horizon test with the same false-ship level and the same power "
        f"({sv['power']:.1%}): that test needs {sv['fixed_horizon_identities_per_arm']} identities per arm "
        f"(the contract's horizon {sv['contract_horizon']} divided by the sequential inflation "
        f"{sv['inflation']}). The contract at alpha 0.05 used {sv['contract_mean_used']} on average, "
        f"{sv['saving_pct']}% fewer.",
        "",
        "Design sensitivity, contract at alpha 0.05, A/A (the guarantee holds for false Winning; false Losing "
        "runs over its nominal 2.5% when conversions are few):",
        "",
        "| Base rate | Identities per arm per look x looks | Expected conversions per arm at the horizon | False Winning | False Losing |",
        "|---|---|---|---|---|",
    ]
    for row in r["base_rates"]:
        q = row["parameters"]
        k = q["runs"]
        s = row["contract"]["0.05"]
        lines.append(f"| {q['base_rate']:.0%} | {q['identities_per_arm_per_look']} x {q['looks']} | "
                     f"{q['base_rate'] * q['identities_per_arm_per_look'] * q['looks']:.0f} | "
                     f"{_pct(s['winning'], k)} | {_pct(s['losing'], k)} |")
    g = aa["guardrail_false_stops"]
    lines += [
        "",
        f"Guardrails in A/A (a guardrail metric at {p['guardrail_rate']:.0%} in both arms, no effect), share of "
        "experiments stopped for a guardrail breach that is not there:",
        "",
        f"| Guardrail direction | Old rule: 97.5% chance worse at any look | Signed rule: one-sided Pocock-type spending, alpha {ct.GUARDRAIL_ALPHA}, counted looks |",
        "|---|---|---|",
    ]
    for d, v in g.items():
        lines.append(f"| {d.replace('_', ' ')} | {_pct(v['old_fixed_97_5_every_look'], v['runs'])} | "
                     f"{_pct(v['signed_pocock'], v['runs'])} |")
    return "\n".join(lines) + "\n"


def run(out_dir: str | Path = ".run/calibration", runs: int = 2000, flagsmith_src: str | None = None,
        power_lift: float = 0.25, looks: int = 30, per_arm_per_look: int = 100,
        seeds: tuple[int, ...] = DEFAULT_SEEDS, base_rate_runs: int = 2000, paths: int = DEFAULT_PATHS) -> dict:
    kw = dict(runs=runs, looks=looks, per_arm_per_look=per_arm_per_look, flagsmith_src=flagsmith_src)
    aa = pooled(seeds, keep_paths=min(paths, runs), **kw)
    export = paths_export({**aa, "parameters": {**aa["parameters"], "seed": seeds[0]}, "_paths": aa.pop("_paths")})
    pw = pooled(tuple(s + 1 for s in seeds), relative_lift=power_lift, guardrail_rate=None, **kw)
    pw.pop("_paths", None)
    # Sizing: the contract's horizon against a fixed-horizon test with the same level and the same power.
    power = pw["contract"]["0.05"]["winning_rate"]
    horizon = looks * per_arm_per_look
    infl = bd.inflation_factor(looks, 0.05, power)
    fixed = math.ceil(horizon / infl)
    used = pw["contract"]["0.05"]["mean_identities_per_arm_used"]
    savings = {"power": power, "inflation": round(infl, 4), "contract_horizon": horizon,
               "fixed_horizon_identities_per_arm": fixed, "contract_mean_used": used,
               "saving_pct": round(100 * (1 - used / fixed), 1)}
    base_rows = []
    for base in (0.02, 0.05, 0.10):
        base_rows.append(simulate(runs=base_rate_runs, looks=looks, per_arm_per_look=per_arm_per_look,
                                  base_rate=base, alphas=(0.05,), seed=7000 + int(base * 1000),
                                  guardrail_rate=None, flagsmith_src=flagsmith_src))
    hp = horizon_plan(min_relative_lift=0.25, baseline_rate=0.02, alpha=0.05, power=0.8, looks=looks)
    sized = simulate(runs=base_rate_runs, looks=looks, per_arm_per_look=math.ceil(hp.identities_per_arm / looks),
                     base_rate=0.02, alphas=(0.05,), seed=7777, guardrail_rate=None, flagsmith_src=flagsmith_src)
    sized["parameters"]["note"] = "horizon from `ulysses horizon` for 2% and a 25% lift"
    base_rows.append(sized)
    for row in base_rows:
        row.pop("_counts", None)
        row.pop("_paths", None)
    result = {"aa": aa, "power": pw, "savings": savings, "base_rates": base_rows}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "offline.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    md = markdown(result)
    (out / "offline.md").write_text(md, encoding="utf-8")
    written = [str(out / "offline.json"), str(out / "offline.md")]
    if export["runs"]:
        (out / PATHS_FILE).write_text(dump_export(export), encoding="utf-8")
        written.append(str(out / PATHS_FILE))
    return {**result, "markdown": md, "paths": written}


def guardrail_sweep(alphas: tuple[float, ...] = (0.025, 0.01), *, runs: int = 2000, seeds: tuple[int, ...] = (11, 12),
                    looks: int = 30, per_arm_per_look: int = 100, rate: float = 0.10,
                    flagsmith_src: str | None = None) -> str:
    """False-stop rate of a guardrail with no effect, for several one-sided budgets (how the default was chosen).

        uv run python -c "from ulysses.calibrate import offline; print(offline.guardrail_sweep())"
    """
    stats, consts, _ = flagsmith_source.load(flagsmith_src)
    lines = [f"Guardrail false stops in A/A: {rate:.0%} in both arms, {looks} looks of {per_arm_per_look} per arm, "
             f"seeds {', '.join(map(str, seeds))}. Simulated synthetic units.", "",
             "| Budget | not increase | not decrease | Runs |", "|---|---|---|---|"]
    for alpha in alphas:
        cs = {}
        for d in ("not_increase", "not_decrease"):
            data = _contract(0.05, looks, looks * per_arm_per_look, d).canonical()
            data["guardrails"][0]["alpha"] = alpha
            cs[d] = ct.from_canonical(data)
        stops = {d: 0 for d in cs}
        n = 0
        for seed in seeds:
            rng = np.random.default_rng(seed)
            for _ in range(runs):
                n += 1
                g = _chances(stats, consts, rng.binomial(per_arm_per_look, rate, size=looks).cumsum(),
                             rng.binomial(per_arm_per_look, rate, size=looks).cumsum(), per_arm_per_look, looks)
                for d, gc in cs.items():
                    worse = [None if x is None else (x if d == "not_increase" else 1 - x) for x in g]
                    prior: list[float] = []
                    for k, w in enumerate(worse):
                        m = per_arm_per_look * (k + 1)
                        dd = evaluate(gc, prior, Observation(m, m, 0.5, {"guard": w}))
                        if dd.counted:
                            prior.append(dd.fraction)
                        if dd.outcome == "guardrail_breach":
                            stops[d] += 1
                            break
                        if dd.outcome == "horizon":
                            break
        lines.append(f"| {alpha} | {_pct(stops['not_increase'], n)} | {_pct(stops['not_decrease'], n)} | {n} |")
    return "\n".join(lines) + "\n"
