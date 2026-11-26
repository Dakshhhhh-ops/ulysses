"""The calibration page's data, read from the calibration scripts' own output files.

Nothing here is typed in. The lab tab reads ``offline.json`` written by
``ulysses calibrate offline``; the live tab reads the newest ``live-*.jsonl``
trace and recomputes its table with ``calibrate.live.summarise_trace``. The
per-run paths for the fan come from the same trace, with each run's first
banner exit and contract call found by the same rules ``summarise_trace``
uses, and a test holds the two counts equal.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..boundary import chance_to_z
from ..calibrate.live import LOSE, WIN, _contract, summarise_trace
from ..calibrate.offline import wilson
from ..decision import Observation, evaluate
from . import fmt
from . import scene as sc


def _interval(k: int, n: int) -> str:
    if not n:
        return ""
    lo, hi = wilson(k, n)
    return f"{100 * lo:.1f}–{100 * hi:.1f}%"


def _row(key: str, label: str, k: int, n: int, **extra) -> dict:
    return {"key": key, "label": label, "k": k, "n": n, "k_text": fmt.count(k), "n_text": fmt.count(n),
            "rate": fmt.rate(k, n), "interval": _interval(k, n), **extra}


FIRST = "Act at the first look over 95% (Winning) or under 5% (Losing)"
FINAL = "Read once, at the planned final look"


def _alphas(d: dict) -> list[str]:
    return sorted(d, key=lambda a: -float(a))  # 0.1 first: the like-for-like level for a 95% / 5% rule


def _ship_rows(first: int, final: int, contract: dict, n: int) -> list[dict]:
    rows = [_row("banner", "Ship at the first look over 95%", first, n),
            _row("final", "Ship if over 95% at the planned final look", final, n)]
    for a in _alphas(contract):
        rows.append(_row("contract", f"Ship only when the signed contract fires (alpha {a})", contract[a], n,
                         level=a))
    return rows


def _any_rows(first: int, final: int, contract: dict, n: int) -> list[dict]:
    rows = [_row("banner", FIRST, first, n), _row("final", FINAL, final, n)]
    for a in _alphas(contract):
        k, w, l = contract[a]
        rows.append(_row("contract", f"Act only when the signed contract fires (alpha {a})", k, n, level=a,
                         split=f"{fmt.count(w)} false wins · {fmt.count(l)} false losses"))
    return rows


def offline_view(run_dir: Path) -> dict | None:
    p = run_dir / "offline.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        aa = data["aa"]
        par = aa["parameters"]
        n = aa["banner_every_look"]["runs"]
        con = aa["contract"].get("0.05") or next(iter(aa["contract"].values()))
        alpha = "0.05" if "0.05" in aa["contract"] else next(iter(aa["contract"]))
    except (OSError, ValueError, KeyError, StopIteration):
        return {"error": f"{p.name} could not be read; rerun the offline calibration."}
    stats = aa.get("engine", {}).get("stats.py", {})
    looks = int(par["looks"])
    planned = sc.planned_rows(_contract(float(alpha), looks, looks * int(par["identities_per_arm_per_look"]))
                              .model_dump()["boundary"]["planned"])
    return {
        "label": aa.get("label", ""),
        "facts": [f"{fmt.count(par['runs'])} runs", f"{looks} looks",
                  f"{fmt.count(par['identities_per_arm_per_look'])} per arm per look",
                  f"base rate {par['base_rate']:.0%}", f"seed {par['seed']}",
                  f"Flagsmith stats.py {stats.get('tag', '?')} sha256 {str(stats.get('sha256', ''))[:12]}"],
        "ship_rows": _ship_rows(aa["banner_every_look"]["winning"], aa["banner_final_look_only"]["winning"],
                                {k: v["winning"] for k, v in aa["contract"].items()}, n),
        "rows": _any_rows(aa["banner_every_look"]["any_call"], aa["banner_final_look_only"]["any_call"],
                          {k: (v["any_call"], v["winning"], v["losing"]) for k, v in aa["contract"].items()}, n),
        "looks": looks,
        "alpha": float(alpha),
        "planned": planned,
        "paths": None,
        "command": "uv run ulysses calibrate offline --flagsmith-src ../_shared/reference/flagsmith",
    }


def _newest_trace(run_dir: Path) -> Path | None:
    traces = sorted(run_dir.glob("live-*.jsonl"), key=lambda p: p.stat().st_mtime)
    return traces[-1] if traces else None


def trace_paths(path: Path, alpha: float = 0.05, per_arm_per_look: int = 100) -> dict:
    """Per-run paths (z per look) and each run's first banner exit and contract call.

    Same selection and rules as ``summarise_trace``: runs with fewer rows than
    the last look are left out; a read that errored is a look not taken.
    """
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    k_max = max(r["look"] for r in rows)
    contract = _contract(alpha, k_max, k_max * per_arm_per_look)
    by_exp: dict[int, list[dict]] = {}
    for r in rows:
        by_exp.setdefault(r["experiment"], []).append(r)
    runs = []
    dropped = 0
    for eid, rs in sorted(by_exp.items()):
        rs = sorted(rs, key=lambda x: x["look"])
        if len(rs) < k_max:
            dropped += 1
            continue
        prior: list[float] = []
        banner = contract_call = None
        pts = []
        for r in rs:
            if "error" in r:
                continue
            ch = r.get("chance_to_win")
            z = None if ch is None else round(chance_to_z(ch), 4)
            pts.append([r["look"], z])
            if banner is None and ch is not None and (ch > WIN or ch < LOSE):
                banner = {"look": r["look"], "z": z, "side": "winning" if ch > WIN else "losing"}
            if contract_call is None:
                d = evaluate(contract, prior, Observation(r["n_control"], r["n_treatment"], ch))
                if d.counted:
                    prior.append(d.fraction)
                if d.outcome in ("win", "loss", "horizon"):
                    contract_call = {"look": r["look"], "z": z, "outcome": d.outcome}
        runs.append({"experiment": eid, "points": pts, "banner": banner,
                     "contract": contract_call if contract_call and contract_call["outcome"] in ("win", "loss") else None,
                     "horizon": bool(contract_call and contract_call["outcome"] == "horizon")})
    return {"looks": k_max, "runs": runs, "dropped": dropped,
            "planned": sc.planned_rows(contract.model_dump()["boundary"]["planned"])}


def live_view(run_dir: Path, alpha: float = 0.05) -> dict | None:
    path = _newest_trace(run_dir)
    if path is None:
        return None
    try:
        s = summarise_trace(path, alpha=alpha)
        s10 = summarise_trace(path, alpha=0.1)
        tp = trace_paths(path, alpha=alpha)
    except (OSError, ValueError, KeyError) as exc:
        return {"error": f"{path.name} could not be read ({exc})."}
    m = s["experiments"]
    bw, bl = s["banner_any_look"]["winning"], s["banner_any_look"]["losing"]
    fw, fl = s["banner_final_look"]["winning"], s["banner_final_look"]["losing"]
    cw, cl, ch = s["contract"]["win"], s["contract"]["loss"], s["contract"]["horizon"]
    contract = {"0.1": (s10["contract"]["win"] + s10["contract"]["loss"], s10["contract"]["win"], s10["contract"]["loss"]),
                str(alpha): (cw + cl, cw, cl)}
    return {
        "label": s["label"],
        "batch": s["batch"],
        "trace": path.name,
        "facts": [f"batch {s['batch']}", f"{m} A/A experiments", f"{s['looks']} looks",
                  "every number read from Flagsmith's results endpoint",
                  f"{s['skipped_reads']} of {m * s['looks']} reads not returned in time"],
        "ship_rows": _ship_rows(bw, fw, {k: v[1] for k, v in contract.items()}, m),
        "rows": _any_rows(bw + bl, fw + fl, contract, m),
        "horizon_line": f"The contract reached the horizon with no decision in {ch} of {m}.",
        "dropped": tp["dropped"],
        "looks": s["looks"],
        "alpha": alpha,
        "planned": tp["planned"],
        "paths": tp["runs"],
        "counts": {"banner": bw + bl, "contract": cw + cl},
        "command": f"uv run ulysses calibrate summarise .run/calibration/{path.name}",
    }


def calibration_page(run_dir: Path) -> dict:
    off = offline_view(run_dir)
    live = live_view(run_dir)
    return {"offline": off, "live": live, "run_dir": str(run_dir)}
