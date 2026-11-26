"""Count a live false-winner batch: A/A experiments run under signed contracts through the contract service.

``scripts/live_false_winner.py run`` writes one trace per batch. Its first line is the design, written
before any traffic; every other line is one look the contract service took (``POST /api/contracts/<hash>/look``)
as the service returned it: Flagsmith's identities per arm and chance to win, and the contract's decision
for that look (outcome, whether it counted, its boundary). A look that fired also carries what happened to
the pre-signed action (the owner's confirmation and the execute call).

This module only reads that trace. It recomputes nothing statistical: the outcome on each row is the one
the service recorded at the time. Both arms of every experiment serve the identical value, so every
Winning or Losing reading in it is noise.
"""

from __future__ import annotations

import json
from pathlib import Path

WIN, LOSE = 0.95, 0.05
FIRES = {"win", "loss", "horizon", "guardrail_breach"}


def read(path: str | Path) -> tuple[dict, list[dict]]:
    lines = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
    if not lines or "design" not in lines[0]:
        raise ValueError(f"{path} has no design header; a false-winner trace starts with the design")
    return lines[0]["design"], [r for r in lines[1:] if "experiment" in r]


def _looks(rows: list[dict]) -> list[dict]:
    return [r for r in rows if "look" in r]


def count(path: str | Path) -> dict:
    """What the batch shows, from the service's own look records in the trace."""
    design, every = read(path)
    rows = _looks(every)
    operator_ended = sorted({int(r["experiment"]) for r in every if "operator" in r})
    by_exp: dict[int, list[dict]] = {}
    for r in rows:
        by_exp.setdefault(int(r["experiment"]), []).append(r)
    # A row's "look" is the script's scheduled round; the contract's own look number is contract.look_number.
    # They differ once a round comes back not counted (e.g. not_ready): the card shows the contract's number.
    over: dict[int, list[dict]] = {}  # experiment -> readings where Flagsmith read > 95% and the contract held
    under: dict[int, list[dict]] = {}
    over_card: list[dict] = []        # the service's look ids for those readings (counted looks first)
    first_over: dict[int, tuple] = {}
    not_ready = 0
    outcomes: dict[str, int] = {}
    actions: dict[str, int] = {}
    shipped: list[int] = []
    errors = 0
    for eid, rs in sorted(by_exp.items()):
        rs = sorted(rs, key=lambda x: (x["look"], x.get("attempt", 1)))
        final = None
        for r in rs:
            if "error" in r:
                errors += 1
                continue
            if r.get("stale"):
                # Flagsmith had not recomputed: the service re-read the previous results. Same reading, not a new look.
                continue
            c = r.get("chance_to_win")
            d = r.get("contract") or {}
            if d.get("outcome") == "not_ready":
                not_ready += 1
            held = d.get("outcome") not in FIRES
            at = {"contract_look": d.get("look_number"), "scheduled_round": r["look"]}
            if c is not None and held and c > WIN:
                over.setdefault(eid, []).append(at)
                first_over.setdefault(eid, (d.get("look_number"), r["look"]))
                over_card.append({"experiment": eid, **at, "look_id": r.get("look_id"),
                                  "chance_to_win": c, "outcome": d.get("outcome"), "counted": d.get("counted"),
                                  "boundary_chance": d.get("boundary_chance")})
            if c is not None and held and c < LOSE:
                under.setdefault(eid, []).append(at)
            if d.get("outcome") in FIRES:
                final = r
        if final is None:
            outcomes["still holding"] = outcomes.get("still holding", 0) + 1
            continue
        d = final["contract"]
        outcomes[d["outcome"]] = outcomes.get(d["outcome"], 0) + 1
        f = final.get("fired") or {}
        key = f"{f.get('action')} ({f.get('state')})"
        actions[key] = actions.get(key, 0) + 1
        if f.get("action") == "ship_treatment" and f.get("state") == "executed":
            shipped.append(eid)
    n = len(by_exp)
    first_look_hist: dict[tuple, int] = {}
    for k in first_over.values():
        first_look_hist[k] = first_look_hist.get(k, 0) + 1
    hist_rows = [{"contract_look": cl, "scheduled_round": sr, "experiments": v}
                 for (cl, sr), v in sorted(first_look_hist.items(), key=lambda x: (x[0][0] is None, x[0]))]
    over_card.sort(key=lambda x: (not x["counted"], -x["chance_to_win"]))
    out = {
        "batch": design.get("batch"),
        "design": design,
        "experiments": n,
        "looks_recorded": len(rows) - errors,
        "looks_not_ready": not_ready,
        "looks_not_returned": errors,
        "over_95_while_holding": {"experiments": len(over), "looks": {str(k): v for k, v in sorted(over.items())},
                                  "first_look_histogram": hist_rows},
        "under_5_while_holding": {"experiments": len(under), "looks": {str(k): v for k, v in sorted(under.items())}},
        "either_while_holding": len(set(over) | set(under)),
        "contract_outcomes": dict(sorted(outcomes.items())),
        "pre_signed_actions": dict(sorted(actions.items())),
        "contract_shipped_treatment": len(shipped),
        "ended_by_operator_after_the_run": len(operator_ended),
        "hold_cards_over_95": over_card,
    }
    out["text"] = _text(out)
    return out


def where(a: dict) -> str:
    """Where a reading sits: the contract's look number (what the card shows) and the script's scheduled round."""
    cl, sr = a.get("contract_look"), a.get("scheduled_round")
    if cl == sr:
        return f"contract look {cl}"
    return f"contract look {cl} (scheduled round {sr})"


def _text(s: dict) -> str:
    d = s["design"]
    o, u = s["over_95_while_holding"], s["under_5_while_holding"]
    lines = [
        f"Live false-winner batch {s['batch']}: {s['experiments']} A/A experiments on self-hosted Flagsmith "
        f"(both arms serve the identical value), each under a signed Ulysses contract, every look taken by the "
        f"contract service. Signed design: {d['looks']} looks, {d['per_arm_per_look']} synthetic identities per arm "
        f"per look, horizon {d['horizon']} per arm, alpha {d['alpha']}, conversion probability {d['rate']:.0%} in "
        f"both arms. Synthetic identities assigned by Flagsmith's engine (Python SDK, local evaluation); not user "
        f"traffic.",
        f"Look records from the service: {s['looks_recorded']}"
        + (f" ({s['looks_not_ready']} of them not ready: Flagsmith gave no reading)" if s["looks_not_ready"] else "")
        + (f"; {s['looks_not_returned']} did not return" if s["looks_not_returned"] else "") + ".",
        f"Flagsmith read over 95% (Winning by the dashboard's rule) while the contract held: "
        f"{o['experiments']} of {s['experiments']} experiments"
        + (f"; first such look: " + ", ".join(where(h) + f": {h['experiments']}" for h in o["first_look_histogram"])
           if o["experiments"] else "") + ".",
        f"Flagsmith read under 5% (Losing) while the contract held: {u['experiments']} of {s['experiments']}.",
        f"Either, while the contract held: {s['either_while_holding']} of {s['experiments']}.",
        "Contract outcomes: " + ", ".join(f"{k} {v}" for k, v in s["contract_outcomes"].items()) + ".",
        "Pre-signed actions: " + (", ".join(f"{k} {v}" for k, v in s["pre_signed_actions"].items()) or "none") + ".",
        f"Do-nothing variations the contract shipped: {s['contract_shipped_treatment']}.",
        *([f"Not decided within the run, completed by the operator keeping control: "
           f"{s['ended_by_operator_after_the_run']}."] if s["ended_by_operator_after_the_run"] else []),
    ]
    return "\n".join(lines) + "\n"
