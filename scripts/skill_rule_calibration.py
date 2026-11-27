"""Score the agent skill's stop rule on Flagsmith's own stats.py, on the same draws as `ulysses calibrate offline`.

The rule is the one in skills/flagsmith-experiment-rollout/scripts/stop_rule.py, called through its
`check` function on Flagsmith-shaped experiment and results bodies, so this measures the shipped code.
It reuses the offline calibration's seeds and random-number order (primary draws, then the guardrail
draws it skips), so the first-95% count printed here must equal the one in data/offline.json; the
script checks that.

    uv run python scripts/skill_rule_calibration.py --flagsmith-src <Flagsmith checkout>

Simulated synthetic identities, not user traffic. Writes data/skill-rule-offline.json.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ulysses.calibrate import flagsmith_source  # noqa: E402
from ulysses.calibrate.offline import DEFAULT_SEEDS, LOSE, WIN, _chances, wilson  # noqa: E402

RULE_PATH = ROOT / "skills" / "flagsmith-experiment-rollout" / "scripts" / "stop_rule.py"
OUT = ROOT / "data" / "skill-rule-offline.json"
# A finished refresh: results computed one second after they were requested.
REQUESTED = datetime.fromtimestamp(0, timezone.utc).isoformat()
AS_OF = (datetime.fromtimestamp(0, timezone.utc) + timedelta(seconds=1)).isoformat()


def load_rule():
    spec = importlib.util.spec_from_file_location("stop_rule", RULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def bodies(rule, chance: float | None, n: int, planned: int) -> tuple[dict, dict]:
    exp = {
        "status": "running",
        "hypothesis": "A/A calibration " + rule.make_tag(planned),
        "feature": {"multivariate_options": [{"id": 1, "key": "b"}]},
        "experiment_rollout": {"multivariate_feature_state_values": [{"multivariate_feature_option": 1, "percentage_allocation": 50}]},
        "metrics": [{"metric": 1, "metric_name": "conversion", "direction": "up", "expected_direction": "increase"}],
    }
    inference = {} if chance is None else {"b": {"lift": 0.0, "ci_low": 0.0, "ci_high": 0.0, "chance_to_win": chance}}
    res = {"results": {"as_of": AS_OF, "refresh_requested_at": REQUESTED, "last_error_at": None,
                       "payload": {"metrics": [{"metric_id": 1, "variants": {"control": {"n": n}, "b": {"n": n}},
                                                "inference": inference}]}}}
    return exp, res


def simulate(rule, stats, consts, *, seed: int, runs: int, looks: int, per_look: int, base: float, lift: float,
             guardrail_draws: bool) -> dict:
    rng = np.random.default_rng(seed)
    planned = looks * per_look
    c = {"skill_ship": 0, "skill_keep_early": 0, "skill_ship_early": 0, "first95_ship": 0, "final95_ship": 0,
         "stop_looks": []}
    for _ in range(runs):
        chances = _chances(stats, consts, rng.binomial(per_look, base, size=looks).cumsum(),
                           rng.binomial(per_look, base * (1 + lift), size=looks).cumsum(), per_look, looks)
        if guardrail_draws:  # the offline calibration draws a guardrail next; keep the stream aligned
            rng.binomial(per_look, 0.10, size=looks)
            rng.binomial(per_look, 0.10, size=looks)
        for ch in chances:
            if ch is not None and (ch > WIN or ch < LOSE):
                c["first95_ship"] += ch > WIN
                break
        if chances[-1] is not None and chances[-1] > WIN:
            c["final95_ship"] += 1
        for k, ch in enumerate(chances):
            v = rule.check(*bodies(rule, ch, per_look * (k + 1), planned))
            if v["verdict"] in ("SHIP", "KEEP_CONTROL"):
                early = v["read"] == "interim"
                if v["verdict"] == "SHIP":
                    c["skill_ship"] += 1
                    c["skill_ship_early"] += early
                elif early:
                    c["skill_keep_early"] += 1
                c["stop_looks"].append(k + 1)
                break
    return c


def rate(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"count": k, "runs": n, "rate": round(k / n, 4), "wilson95": [round(lo, 4), round(hi, 4)]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--flagsmith-src", help="path to a Flagsmith checkout (else downloaded at the pinned tag)")
    ap.add_argument("--runs", type=int, default=2000, help="runs per seed (5 seeds)")
    a = ap.parse_args()
    stats, consts, provenance = flagsmith_source.load(a.flagsmith_src)
    rule = load_rule()
    looks, per_look, base = 30, 100, 0.10
    out = {"label": "Agent skill stop rule on Flagsmith's stats.py, simulated synthetic identities (not user traffic)",
           "rule": {"early": rule.EARLY, "final": rule.FINAL, "planned_identities_per_arm": looks * per_look,
                    "reads": looks, "identities_per_arm_per_read": per_look, "base_rate": base},
           "engine": {k: {"tag": v.get("tag"), "sha256": v.get("sha256")} for k, v in provenance.items()},
           "command": "uv run python scripts/skill_rule_calibration.py --flagsmith-src <Flagsmith checkout>"}
    for name, lift, seeds, gdraws in (("aa", 0.0, DEFAULT_SEEDS, True),
                                      ("lift_25pct", 0.25, tuple(s + 1 for s in DEFAULT_SEEDS), False)):
        tot: dict = {}
        for s in seeds:
            part = simulate(rule, stats, consts, seed=s, runs=a.runs, looks=looks, per_look=per_look, base=base,
                            lift=lift, guardrail_draws=gdraws)
            for k, v in part.items():
                tot[k] = tot.get(k, [] if isinstance(v, list) else 0) + v
        n = a.runs * len(seeds)
        out[name] = {"relative_lift": lift, "seeds": list(seeds),
                     "skill_rule_ships": rate(tot["skill_ship"], n),
                     "skill_rule_ships_before_planned_size": rate(tot["skill_ship_early"], n),
                     "skill_rule_keeps_control_before_planned_size": rate(tot["skill_keep_early"], n),
                     "first_read_over_95_ships": rate(tot["first95_ship"], n),
                     "one_read_at_planned_size_ships": rate(tot["final95_ship"], n),
                     "mean_stop_read": round(float(np.mean(tot["stop_looks"])), 2) if tot["stop_looks"] else None}
    ref = json.loads((ROOT / "data" / "offline.json").read_text(encoding="utf-8"))
    if a.runs == 2000:
        want = ref["aa"]["banner_every_look"]["winning"]
        got = out["aa"]["first_read_over_95_ships"]["count"]
        if want != got:
            sys.exit(f"draws are not aligned with data/offline.json: first-95% ships {got}, offline.json has {want}")
        out["aligned_with"] = "data/offline.json (same draws: first-95% ships match)"
    dest = OUT if a.runs == 2000 else ROOT / ".run" / "calibration" / OUT.name  # only the full run replaces data/
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    aa, pw = out["aa"], out["lift_25pct"]
    print(f"A/A, {looks} reads: first read over 95% ships {aa['first_read_over_95_ships']['rate']:.1%}; "
          f"one read at the planned size {aa['one_read_at_planned_size_ships']['rate']:.1%}; "
          f"the skill's rule {aa['skill_rule_ships']['rate']:.1%}")
    print(f"+25% lift: the skill's rule ships {pw['skill_rule_ships']['rate']:.1%}, "
          f"{pw['skill_rule_ships_before_planned_size']['rate']:.1%} before the planned size; wrote {dest.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
