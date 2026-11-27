#!/usr/bin/env python3
"""Pre-registered stop rule for a Flagsmith experiment. Python 3.8+, standard library only.

  plan   size the experiment before it starts and print the tag to record in its hypothesis
  check  read the experiment and its results (JSON from `flagsmith api`) and print one verdict line

The rule (Haybittle-Peto): before the planned size, ship only on overwhelming evidence (chance to
win at least `early`, default 0.9987, which is z = 3); at the planned size, read once against
`final` (default 0.95, the level Flagsmith's docs call Winning). A losing primary metric never
stops the experiment early: at low counts Flagsmith's lift is skewed toward false losses.
Guardrails stop it early (keep control) at the `early` level and block a ship at the planned size
at `guard`. With several
variations each comparison uses a Bonferroni share of the error.

Every number it reads is Flagsmith's own (`chance_to_win`, `lift`, `n`); the only arithmetic it
adds is the threshold for this read, the sample size, and a sample-ratio chi-square on Flagsmith's
arm counts against the experiment's own split.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime
from statistics import NormalDist

VERSION = "v1"
EARLY, FINAL, GUARD, POWER = 0.9987, 0.95, 0.95, 0.8
SRM_P, SRM_MIN_TOTAL = 0.001, 100
TAG_RE = re.compile(r"\[stop-rule (v\d+)((?: [a-z]+=[0-9.]+)+)\]")
_N = NormalDist()


class Refused(Exception):
    """Input the rule must not decide on; the message says what to do instead."""


# ---------------------------------------------------------------- plan

def identities_per_arm(baseline_rate: float, min_lift: float, final: float = FINAL, power: float = POWER,
                       variants: int = 1) -> int:
    """Identities per arm to detect a relative lift `min_lift` on a 0/1 metric with control rate
    `baseline_rate`, one read at `final` (one-sided, Bonferroni across `variants`), with `power`.
    Uses the variance of the relative lift that Flagsmith's stats.py estimates (delta method)."""
    if not 0 < baseline_rate < 1:
        raise ValueError("--baseline-rate must be between 0 and 1, e.g. 0.1 for 10%")
    if min_lift == 0:
        raise ValueError("--min-lift cannot be 0; give the smallest relative lift worth shipping, e.g. 0.1")
    if not 0.5 <= power < 1:
        raise ValueError("--power must be in [0.5, 1)")
    lift = abs(min_lift)
    p, pt = baseline_rate, baseline_rate * (1 + lift)
    if not 0 < pt < 1:
        raise ValueError("baseline rate times (1 + lift) must stay below 1")
    unit_var = pt * (1 - pt) / p ** 2 + pt ** 2 * p * (1 - p) / p ** 4
    z = _N.inv_cdf(1 - (1 - final) / max(1, variants)) + _N.inv_cdf(power)
    return max(100, math.ceil(unit_var * (z / lift) ** 2))


def make_tag(n: int, early: float = EARLY, final: float = FINAL, guard: float = GUARD) -> str:
    return f"[stop-rule {VERSION} n={n} early={early:g} final={final:g} guard={guard:g}]"


def parse_tag(text: str | None) -> dict | None:
    m = TAG_RE.search(text or "")
    if not m:
        return None
    if m.group(1) != VERSION:
        raise Refused(f"the hypothesis records stop rule {m.group(1)}; this script reads {VERSION}")
    vals = dict(kv.split("=") for kv in m.group(2).split())
    try:
        rule = {"n": int(vals["n"]), "early": float(vals.get("early", EARLY)),
                "final": float(vals.get("final", FINAL)), "guard": float(vals.get("guard", GUARD))}
    except (KeyError, ValueError) as exc:
        raise Refused(f"the stop-rule tag in the hypothesis is malformed ({exc}); ask the person to fix it")
    if not (0.5 < rule["final"] <= rule["early"] < 1 and 0.5 < rule["guard"] < 1 and rule["n"] > 0):
        raise Refused("the stop-rule tag in the hypothesis has out-of-range values; ask the person to fix it")
    return rule


# ---------------------------------------------------------------- check

def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _chance_up(direction: str, chance_to_win: float) -> float:
    """Flagsmith reports chance_to_win in the metric's good direction (flipped for `down`)."""
    return 1.0 - chance_to_win if direction == "down" else chance_to_win


def chi_square_sf(stat: float, df: int) -> float:
    """Upper tail of the chi-square distribution: regularised upper incomplete gamma Q(df/2, stat/2)."""
    a, x = df / 2.0, stat / 2.0
    if x <= 0:
        return 1.0
    lg = math.lgamma(a)
    if x < a + 1:  # series for P, then Q = 1 - P
        term = total = 1.0 / a
        ap = a
        for _ in range(500):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(-x + a * math.log(x) - lg))
    b, c, d = x + 1 - a, 1e300, 1 / (x + 1 - a)  # continued fraction for Q
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return min(1.0, math.exp(-x + a * math.log(x) - lg) * h)


def _split(experiment: dict) -> dict[str, float] | None:
    ro = experiment.get("experiment_rollout") or {}
    options = {o["id"]: o.get("key") for o in (experiment.get("feature") or {}).get("multivariate_options") or []}
    shares: dict[str, float] = {}
    for mv in ro.get("multivariate_feature_state_values") or []:
        key = options.get(mv.get("multivariate_feature_option"))
        if not key:
            return None
        shares[key] = float(mv.get("percentage_allocation") or 0)
    if not shares:
        return None
    shares["control"] = max(0.0, 100.0 - sum(shares.values()))
    return {k: v for k, v in shares.items() if v > 0}


def _srm(n: dict[str, int], split: dict[str, float] | None) -> dict | None:
    if not split or set(split) - set(n):
        return None
    total = sum(n[k] for k in split)
    if total < SRM_MIN_TOTAL:
        return None
    weight = sum(split.values())
    stat = sum((n[k] - total * w / weight) ** 2 / (total * w / weight) for k, w in split.items())
    return {"p": round(chi_square_sf(stat, len(split) - 1), 6)}


def _docs_label(chance: float) -> str:
    return "Winning" if chance > 0.95 else "Losing" if chance < 0.05 else "Inconclusive"


def check(experiment: dict, results: dict, planned: int | None = None) -> dict:
    """One verdict for one read. `results` is the body of GET .../results/ ({"results": ...})."""
    status = experiment.get("status")
    rule = parse_tag(experiment.get("hypothesis"))
    if rule and planned is not None and planned != rule["n"]:
        raise Refused(f"the plan recorded in Flagsmith says n={rule['n']} per arm; --planned {planned} would change "
                      "it after the start. Keep the recorded plan, or ask the person to amend the hypothesis.")
    if rule is None:
        if planned is None:
            raise Refused("no [stop-rule ...] tag in the experiment's hypothesis and no --planned given: there is "
                          "no pre-registered plan to decide against. Run `stop_rule.py plan` and record it first.")
        rule = {"n": planned, "early": EARLY, "final": FINAL, "guard": GUARD}
    out: dict = {"status": status, "planned": rule["n"]}
    if status == "created":
        return {**out, "verdict": "NOT_STARTED", "next": "ask the person, then POST .../start/"}
    if status == "completed":
        return {**out, "verdict": "DONE", "rollout_decision": experiment.get("rollout_decision"),
                "rollout_variant": experiment.get("rollout_variant")}

    r = (results or {}).get("results")
    if not r:
        return {**out, "verdict": "NO_RESULTS", "next": "POST .../results/refresh/ (once per 5 min), then GET .../results/"}
    requested, as_of, err = _ts(r.get("refresh_requested_at")), _ts(r.get("as_of")), _ts(r.get("last_error_at"))
    out["as_of"] = r.get("as_of")
    if err and requested and err >= requested:
        return {**out, "verdict": "REFRESH_FAILED", "next": "the last refresh failed and still used its 5-minute "
                "window; refresh again after it, and tell the person if it fails twice"}
    if requested and (as_of is None or as_of < requested):
        return {**out, "verdict": "COMPUTING", "next": "GET .../results/ again in a minute; do not refresh again"}

    metrics = experiment.get("metrics") or []
    payload = {m.get("metric_id"): m for m in (r.get("payload") or {}).get("metrics") or []}
    primary = next((m for m in metrics if m.get("expected_direction") in ("increase", "decrease")), None)
    guards = [m for m in metrics if m.get("expected_direction") in ("not_increase", "not_decrease")]
    if primary is None:
        raise Refused("the experiment has no metric with expected_direction increase or decrease to decide on")
    pres = payload.get(primary["metric"]) or {}
    variants = {k: int(v.get("n") or 0) for k, v in (pres.get("variants") or {}).items()}
    arms = [k for k in variants if k != "control"] or [o.get("key") for o in
                                                         (experiment.get("feature") or {}).get("multivariate_options") or []]
    m = max(1, len(arms))
    early, final, guard = 1 - (1 - rule["early"]) / m, 1 - (1 - rule["final"]) / m, rule["guard"]
    out.update(n=variants, needs={"early": round(early, 6), "final": round(final, 6)})
    at_size = bool(variants) and "control" in variants and min(variants.values()) >= rule["n"]
    out["read"] = "final" if at_size else "interim"

    srm = _srm(variants, _split(experiment))
    if srm:
        out["srm_p"] = srm["p"]
        if srm["p"] < SRM_P:
            return {**out, "verdict": "STOP_AND_ASK", "why": "sample ratio mismatch: traffic is not splitting as "
                    "configured, so no result can be trusted. Do not ship. Show the person the arm counts."}

    want_up = primary["expected_direction"] == "increase"
    pdir = primary.get("direction") or "up"
    prim: dict = {"metric": primary.get("metric_name")}
    best, best_chance = None, -1.0
    for arm in arms:
        inf = (pres.get("inference") or {}).get(arm)
        if not inf or inf.get("chance_to_win") is None:
            prim[arm] = None
            continue
        up = _chance_up(pdir, inf["chance_to_win"])
        good = up if want_up else 1 - up
        prim[arm] = {"chance": round(good, 4), "lift": round(inf.get("lift") or 0, 4),
                     "ci": [round(inf.get("ci_low") or 0, 4), round(inf.get("ci_high") or 0, 4)],
                     "docs_label": _docs_label(good)}
        if good > best_chance:
            best, best_chance = arm, good
    out["primary"] = prim

    grows, early_breach, final_block = [], None, None
    for g in guards:
        gres = payload.get(g["metric"]) or {}
        for arm in arms:
            inf = (gres.get("inference") or {}).get(arm)
            if not inf or inf.get("chance_to_win") is None:
                grows.append({"metric": g.get("metric_name"), "arm": arm, "worse": None})
                continue
            up = _chance_up(g.get("direction") or "up", inf["chance_to_win"])
            worse = up if g["expected_direction"] == "not_increase" else 1 - up
            grows.append({"metric": g.get("metric_name"), "arm": arm, "worse": round(worse, 4)})
            if worse >= early and early_breach is None:
                early_breach = (g.get("metric_name"), arm, worse)
            if arm == best and worse >= guard and final_block is None:
                final_block = (g.get("metric_name"), worse)
    if grows:
        out["guardrails"] = grows

    keep = {"rollout_decision": "control"}
    if early_breach:
        name, arm, w = early_breach
        return {**out, "verdict": "KEEP_CONTROL", "complete_body": keep,
                "why": f"guardrail {name} is worse on {arm} with chance {w:.4f}, past the early bar {early:.4f}"}
    if best is None:
        return {**out, "verdict": "COLLECTING", "next": "Flagsmith has no inference yet (under 50 identities or 5 "
                "conversions per arm); keep running and look again after the next refresh window"}
    ship = {"rollout_decision": "variant", "rollout_variant": _option_id(experiment, best)}
    if not at_size:
        if best_chance >= early:
            return {**out, "verdict": "SHIP", "arm": best, "complete_body": ship,
                    "why": f"{best} chance {best_chance:.4f} passed the early bar {early:.4f} before the planned size"}
        return {**out, "verdict": "HOLD", "why": f"interim read: {best} chance {best_chance:.4f} is under the early bar "
                f"{early:.4f}; a Flagsmith 'Winning' here is not a decision",
                "next": f"look again after the next refresh window; the final read is at n={rule['n']} per arm"}
    if final_block:
        return {**out, "verdict": "KEEP_CONTROL", "complete_body": keep,
                "why": f"planned size reached, but guardrail {final_block[0]} is worse on {best} with chance "
                       f"{final_block[1]:.4f} (bar {guard:.4f})"}
    if best_chance >= final:
        return {**out, "verdict": "SHIP", "arm": best, "complete_body": ship,
                "why": f"planned size reached and {best} chance {best_chance:.4f} >= {final:.4f}"}
    return {**out, "verdict": "KEEP_CONTROL", "complete_body": keep,
            "why": f"planned size reached and {best} chance {best_chance:.4f} is under {final:.4f}: no winner"}


def _option_id(experiment: dict, key: str) -> int | None:
    for o in (experiment.get("feature") or {}).get("multivariate_options") or []:
        if o.get("key") == key:
            return o.get("id")
    return None


# ---------------------------------------------------------------- io

def read_json_docs(text: str) -> list:
    """One or more JSON documents back to back, as `{ flagsmith api A; flagsmith api B; }` prints them."""
    dec, i, docs = json.JSONDecoder(), 0, []
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return docs
        doc, i = dec.raw_decode(text, i)
        docs.append(doc)


def _load(args) -> tuple[dict, dict]:
    try:
        if args.experiment == "-":
            docs = read_json_docs(sys.stdin.read())
        else:
            docs = []
            for p in (args.experiment, args.results):
                if p:
                    with open(p, encoding="utf-8") as fh:
                        docs += read_json_docs(fh.read())
    except ValueError:
        raise Refused("a body is not JSON. An HTML page means a path without its trailing / "
                      "(use \"$A/\", not \"$A\"); an error means the GET failed: show it to the person")
    exp = next((d for d in docs if isinstance(d, dict) and "status" in d and "metrics" in d), None)
    res = next((d for d in docs if isinstance(d, dict) and "results" in d), None)
    if exp is None or res is None:
        raise Refused("need two whole JSON bodies, unfiltered (no --jq): GET .../experiments/<id>/ and "
                      "GET .../experiments/<id>/results/")
    return exp, res


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan", help="identities per arm and the tag to record in the hypothesis")
    pl.add_argument("--baseline-rate", type=float, required=True, help="control conversion rate, e.g. 0.1")
    pl.add_argument("--min-lift", type=float, required=True, help="smallest relative lift worth shipping, e.g. 0.1")
    pl.add_argument("--variants", type=int, default=1, help="number of variations besides control")
    pl.add_argument("--power", type=float, default=POWER)
    pl.add_argument("--early", type=float, default=EARLY)
    pl.add_argument("--final", type=float, default=FINAL)
    pl.add_argument("--guard", type=float, default=GUARD)
    ck = sub.add_parser("check", help="one verdict from the experiment and its results")
    ck.add_argument("experiment", help="experiment JSON file, or - to read both bodies from stdin")
    ck.add_argument("results", nargs="?", help="results JSON file")
    ck.add_argument("--planned", type=int, help="identities per arm, only when the hypothesis has no tag")
    a = p.parse_args(argv)
    try:
        if a.cmd == "plan":
            if not (0.5 < a.final <= a.early < 1 and 0.5 < a.guard < 1):
                raise ValueError("need 0.5 < final <= early < 1 and 0.5 < guard < 1")
            n = identities_per_arm(a.baseline_rate, a.min_lift, a.final, a.power, a.variants)
            print(json.dumps({"identities_per_arm": n, "tag": make_tag(n, a.early, a.final, a.guard)}))
        else:
            exp, res = _load(a)
            print(json.dumps(check(exp, res, a.planned), separators=(",", ":")))
    except (Refused, ValueError, OSError) as exc:
        print(json.dumps({"verdict": "REFUSED", "why": str(exc)}), file=sys.stdout)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
