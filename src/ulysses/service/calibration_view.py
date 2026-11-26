"""The calibration page's data, read from the calibration scripts' own output files.

Nothing here is typed in. The lab tab reads ``offline.json`` written by
``ulysses calibrate offline`` and the hero its per-run export ``paths-offline.json`` (``two_ships``); the live tab reads the newest ``live-*.jsonl``
trace and recomputes its table with ``calibrate.live.summarise_trace``. The
per-run paths for the fan come from the same trace, with each run's first
banner exit and contract call found by the same rules ``summarise_trace``
uses, and a test holds the two counts equal.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..boundary import chance_to_z
from ..calibrate.live import LOSE, WIN, _contract, read_trace, summarise_trace
from ..calibrate.offline import wilson
from ..decision import Observation, evaluate
from . import fmt
from . import scene as sc
from . import two_ships


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
    rows = [_row("banner", "Ship at the first look over 95%", first, n)]
    if final is not None:
        rows.append(_row("final", "Ship if over 95% at the planned final look", final, n))
    for a in _alphas(contract):
        rows.append(_row("contract", f"Ship only when the signed contract fires (alpha {a})", contract[a], n,
                         level=a))
    return rows


def _any_rows(first: int, final: int, contract: dict, n: int) -> list[dict]:
    rows = [_row("banner", FIRST, first, n)]
    if final is not None:
        rows.append(_row("final", FINAL, final, n))
    for a in _alphas(contract):
        k, w, l = contract[a]
        rows.append(_row("contract", f"Act only when the signed contract fires (alpha {a})", k, n, level=a,
                         split=f"{fmt.count(w)} false wins · {fmt.count(l)} false losses"))
    return rows


def _fan(run_dir: Path, aa: dict, alpha: str) -> dict:
    """The hero and, from the same per-run export, the lab tab's fan (paths as z per look)."""
    hero = two_ships.build(run_dir, aa)
    found = two_ships.load(run_dir, aa) if hero and not hero.get("error") else None
    if not found:
        return {"two_ships": hero, "paths": None}
    export = found[0]
    runs = []
    for r in export["runs"]:
        pts = [[k, None if c is None else round(chance_to_z(c), 4)] for k, c in enumerate(r["chance_to_win"], start=1)]
        z_at = lambda look: pts[look - 1][1]  # noqa: E731
        e = r.get("first_exit")
        c = (r.get("contract") or {}).get(alpha)
        runs.append({"experiment": r["run"], "points": pts,
                     "banner": {"look": e["look"], "z": z_at(e["look"]), "side": e["side"]} if e else None,
                     "contract": {"look": c["look"], "z": z_at(c["look"]), "outcome": c["outcome"]} if c else None,
                     "horizon": c is None})
    return {"two_ships": hero, "paths": runs,
            "counts": {"banner": sum(1 for r in runs if r["banner"]), "contract": sum(1 for r in runs if r["contract"])},
            "paths_note": f"The drawing and its counter show the {len(runs)} exported runs; the tables count all "
                          f"{fmt.count(aa['banner_every_look']['runs'])}."}


COMMITTED_OFFLINE = two_ships.SAMPLE.parent / "offline.json"


def offline_view(run_dir: Path) -> dict | None:
    """The run's own offline.json, else the committed copy in data/ made by the same command."""
    p = run_dir / "offline.json"
    origin = "run"
    committed = COMMITTED_OFFLINE
    if not p.exists():
        if committed is None or not committed.exists():
            return None
        p, origin = committed, "committed"
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
        "origin": origin,
        "source_note": None if origin == "run" else
            "Read from data/offline.json, the committed output of the command below; rerun it to replace it.",
        "facts": [f"{fmt.count(par['runs'])} runs", f"{looks} looks",
                  f"{fmt.count(par['identities_per_arm_per_look'])} per arm per look",
                  f"base rate {par['base_rate']:.0%}", "seed " + (", ".join(str(x) for x in par["seed"]) if isinstance(par["seed"], list) else str(par["seed"])),
                  f"Flagsmith stats.py {stats.get('tag', '?')} sha256 {str(stats.get('sha256', ''))[:12]}"],
        "ship_rows": _ship_rows(aa["banner_every_look"]["winning"], aa["banner_final_look_only"]["winning"],
                                {k: v["winning"] for k, v in aa["contract"].items()}, n),
        "rows": _any_rows(aa["banner_every_look"]["any_call"], aa["banner_final_look_only"]["any_call"],
                          {k: (v["any_call"], v["winning"], v["losing"]) for k, v in aa["contract"].items()}, n),
        "looks": looks,
        "alpha": float(alpha),
        "planned": planned,
        **_fan(run_dir, aa, alpha),
        "command": "uv run ulysses calibrate offline --flagsmith-src ../_shared/reference/flagsmith",
    }


def _traces(run_dir: Path) -> list[Path]:
    return sorted(run_dir.glob("live-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def trace_paths(path: Path) -> dict:
    """Per-run paths (z per look) and each run's first exit past 95% / 5% and contract call.

    Same design and rules as ``summarise_trace``: the design comes from the trace's header, every
    experiment is kept, and a read that errored is a look not taken.
    """
    design, rows = read_trace(path)
    looks, per_arm, alpha = design["looks"], design["per_arm_per_look"], design.get("alpha", 0.05)
    contract = _contract(alpha, looks, looks * per_arm)
    by_exp: dict[int, list[dict]] = {}
    for r in rows:
        by_exp.setdefault(r["experiment"], []).append(r)
    runs = []
    for eid, rs in sorted(by_exp.items()):
        rs = sorted(rs, key=lambda x: x["look"])
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
    return {"looks": looks, "runs": runs,
            "planned": sc.planned_rows(contract.model_dump()["boundary"]["planned"])}


def live_view(run_dir: Path) -> dict | None:
    """The newest trace that can be scored under its own signed design; notes for the others."""
    traces = _traces(run_dir)
    if not traces:
        return None
    notes: list[str] = []
    for path in traces:
        try:
            design, rows = read_trace(path)
        except (OSError, ValueError) as exc:
            notes.append(f"{path.name} could not be read ({exc}).")
            continue
        if not design:
            notes.append(f"{path.name} has no design header, so it is not scored here: score it with the looks and "
                         f"identities per arm it was started with, `uv run ulysses calibrate summarise "
                         f".run/calibration/{path.name} --looks <signed looks> --per-arm <signed per arm>`.")
            continue
        if not rows:
            notes.append(f"Batch {design.get('batch', path.stem)} is running: {design.get('experiments', '?')} experiments "
                         f"x {design['looks']} looks signed, no reads yet.")
            continue
        try:
            return _live_from(path, design, notes)
        except (OSError, ValueError, KeyError) as exc:
            notes.append(f"{path.name} could not be scored ({exc}).")
    return {"empty": True, "notes": notes}


def _live_from(path: Path, design: dict, notes: list[str]) -> dict:
    alpha = design.get("alpha", 0.05)
    s = summarise_trace(path)
    s10 = s if alpha == 0.1 else summarise_trace(path, alpha=0.1)
    tp = trace_paths(path)
    m = s["experiments"]
    bw, bl = s["banner_any_look"]["winning"], s["banner_any_look"]["losing"]
    final = s.get("banner_final_look")
    cw, cl = s["contract"]["win"], s["contract"]["loss"]
    ch, holding = s["contract"].get("horizon", 0), s["contract"].get("holding", 0)
    contract = {"0.1": (s10["contract"]["win"] + s10["contract"]["loss"], s10["contract"]["win"], s10["contract"]["loss"]),
                f"{alpha:g}": (cw + cl, cw, cl)}
    ship = _ship_rows(bw, final["winning"] if final else None, {k: v[1] for k, v in contract.items()}, m)
    anyr = _any_rows(bw + bl, (final["winning"] + final["losing"]) if final else None, contract, m)
    line = f"Contract outcomes: {cw} wins, {cl} losses, {ch} at the horizon with no decision, {holding} still holding"
    line += f" when the run stopped at look {s['looks_read']} of {s['looks']}." if s.get("truncated") else "."
    return {
        "label": s["label"],
        "batch": s["batch"],
        "trace": path.name,
        "facts": [f"batch {s['batch']}", f"{m} A/A experiments",
                  f"signed design: {s['looks']} looks, {fmt.count(design['per_arm_per_look'])} per arm per look, alpha {alpha:g}",
                  "every number read from Flagsmith's results endpoint",
                  f"{s['skipped_reads']} reads not returned in time"],
        "ship_rows": ship,
        "rows": anyr,
        "horizon_line": line,
        "truncated": s.get("truncated"),
        "notes": notes,
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
