"""The voyage (the Siren Log): everything that happened to one experiment under contract, in order.

Owner signed, started, each look with Flagsmith's chance to win against what the contract needed, every
request the service refused, the proposal a look fired, the owner's signed tap, the execution in Flagsmith.

A *siren* is a counted look where Flagsmith read at least 95% chance to win (its docs call that Winning)
and the contract held. The *unbound agent* line is a counterfactual computed from the same readings: the
first counted look where Flagsmith said Winning. Nothing here decides anything; every number is read from
the stored looks, refusals, proposals, actions and signatures. Times are relative to the start, never a
date or a clock time.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import fmt

WINNING = 0.95  # Flagsmith's docs: over 95% chance to win reads as "Winning"

# drawing order when two events share a timestamp
_ORDER = {"signed": 0, "started": 1, "look": 2, "refusal": 3, "fired": 4, "tap": 5, "executed": 6}

_OUTCOME = {"hold": "Hold", "watch": "Between looks", "not_ready": "Collecting data", "win": "Win",
            "loss": "Loss", "horizon": "No decision", "guardrail_breach": "Guardrail stop",
            "expired": "Expired, no decision"}


def is_siren(counted: bool, outcome: str | None, chance: float | None) -> bool:
    """Flagsmith read Winning at a counted look and the contract held."""
    return bool(counted) and outcome == "hold" and chance is not None and chance >= WINNING


def looks_for_view(svc, environment: str, experiment_id: int) -> list[dict]:
    """Stored looks with between-looks numbers withheld while a contract has not decided (the service's rule)."""
    view = getattr(svc, "_looks_for_view", None)
    return view(environment, experiment_id) if view else svc.store.looks(environment, experiment_id)


def _when(at: float | None, t0: float | None) -> str:
    if at is None:
        return ""
    if t0 is None:
        return ""
    gap = at - t0
    if abs(gap) < 1:
        return "at the start"
    return f"{fmt.duration(gap)} {'after' if gap > 0 else 'before'} the start"


def _act_words(action: str | None, treatment: str) -> str:
    return {"ship_treatment": f"ship {treatment}", "keep_control": "keep control"}.get(action or "",
                                                                                   (action or "").replace("_", " "))


def refusal_words(what: str, reason: str, treatment: str) -> tuple[str, str]:
    """(title, plain reason) for one logged refusal. Unknown reasons are shown as logged."""
    reason = reason or ""
    if what == "execute":
        m = re.fullmatch(r"no fired outcome authorises (\w+)", reason)
        if m:
            return (f"The agent asked to {_act_words(m.group(1), treatment)}",
                    "Refused: no look had fired the contract, so nothing authorised it.")
        m = re.fullmatch(r"asked for (\w+), contract says (\w+)", reason)
        if m:
            return (f"The agent asked to {_act_words(m.group(1), treatment)}",
                    f"Refused: the contract assigns “{_act_words(m.group(2), treatment)}” to this outcome, "
                    "nothing else.")
        if reason.startswith("waiting for the owner"):
            return ("The agent asked to carry out the action",
                    "Refused: the pre-signed action waits for the owner's signed tap.")
        m = re.fullmatch(r"amendment not signed by (.+)", reason)
        if m:
            return ("The agent asked to act on an amendment",
                    f"Refused: the amendment still needs {m.group(1).replace('_', ' ')}'s signature.")
        m = re.fullmatch(r"contract became (\w+) during the checks", reason)
        if m:
            return ("The agent asked to carry out the action",
                    f"Refused: the contract became {m.group(1)} while the request was checked.")
        return ("The agent asked to carry out an action", f"Refused: {reason}")
    if what == "start":
        plain = {"no valid owner signature": "the contract has no valid owner signature.",
                 "start window closed": "the contract's start window had closed.",
                 "amendment lacks the second signature": "the amendment lacks the second signature."}
        return ("A request to start the experiment", "Refused: " + plain.get(reason, reason + "."))
    if what == "confirm":
        return ("A confirmation tap", "Refused: the signature did not verify with the owner's key.")
    if what == "sign":
        return ("A signature", f"Refused: {reason}")
    return (f"A {what} request", f"Refused: {reason}")


def build(svc, c, h: str, current_seq: int | None = None) -> dict[str, Any]:
    """The voyage of the experiment contract ``c`` (hash ``h``) governs."""
    store = svc.store
    env, exp = c.experiment.environment, c.experiment.experiment
    treatment = c.experiment.treatment if c.comparisons == 1 else "the winning treatment"
    contracts = store.contracts_for(env, exp)
    hashes = [r["hash"] for r in contracts] or [h]
    if h not in hashes:
        hashes.append(h)
    short = {x: x.split(":")[-1][:12] for x in hashes}

    actions = [a for a in store.actions(env, exp) if a.get("contract_hash") in hashes]
    starts = [a for a in actions if a["kind"] == "start" and a.get("ok", 1)]
    t0 = starts[0]["at"] if starts else None

    ev: list[dict] = []

    def add(kind: str, at: float | None, title: str, detail: str = "", **extra) -> dict:
        e = {"kind": kind, "at": at, "title": title, "detail": detail, "when": "", "siren": False,
             "hidden": False, "current": False, **extra}
        ev.append(e)
        return e

    for row in contracts:
        for role, sig in sorted(store.signatures(row["hash"]).items(), key=lambda kv: kv[1]["received_at"]):
            which = "the contract" if row["hash"] == h else f"contract {short[row['hash']]}"
            if row.get("supersedes"):
                which = "the amendment" if row["hash"] == h else f"amendment {short[row['hash']]}"
            add("signed", sig["received_at"], f"{sig['signer']} signed {which}",
                "as owner" if role == "owner" else "as second signer")
    for a in starts:
        add("started", a["at"], f"Experiment {exp} started under contract",
            "One Flagsmith call. The contract's hash is written into the experiment's hypothesis.")

    looks = looks_for_view(svc, env, exp)
    counted_rows = []
    for lk in looks:
        d = lk["decision"] or {}
        k = d.get("look_number")
        counted = bool(lk["counted"])
        chance = d.get("chance_to_win")
        outcome = lk["outcome"]
        siren = is_siren(counted, outcome, chance)
        ctw = fmt.ctw(chance) if chance is not None else None
        bz = d.get("boundary_z")
        needs = fmt.boundary_chance(d.get("boundary_chance"), bz) if bz is not None else None
        if counted:
            n_planned = d.get("looks_planned") or c.horizon.looks
            title = f"Look {k} of {n_planned}" if k else f"Refresh {lk['seq']}"
            if ctw and needs:
                detail = f"Flagsmith {ctw} chance to win; the contract needed {needs}."
            elif ctw:
                detail = f"Flagsmith {ctw} chance to win; no stop was possible at this look."
            elif outcome == "not_ready":
                detail = "Flagsmith had no chance to win yet (too few identities)."
            else:
                detail = "Flagsmith returned no results for this look."
        elif outcome == "expired":
            title = f"Refresh {lk['seq']} · expired"
            detail = "Between looks, but past the signed expiry."
        else:
            title = f"Refresh {lk['seq']} · not a look"
            detail = ("Between looks: the contract withholds Flagsmith's numbers until the next planned look."
                      if ctw is None else f"Between looks: Flagsmith read {ctw}; the contract does not act on it.")
        if outcome == "expired":
            detail += (f" The contract expired {(d.get('expiry') or {}).get('expires_after', '')} after the start "
                       "with no decision: keep control, nothing ships.")
        e = add("look", lk["at"], title, detail, seq=lk["seq"], look_id=lk["id"], look_number=k, counted=counted,
                outcome=outcome, word=_OUTCOME.get(outcome, outcome), ctw=ctw, chance=chance, needs=needs, siren=siren,
                hidden=not counted and outcome != "expired" and lk["seq"] != current_seq,
                current=lk["seq"] == current_seq, fires=bool(d.get("fires")))
        if counted or outcome == "expired":
            counted_rows.append(e)

    for r in store.refusals():
        if r.get("contract_hash") not in hashes:
            continue
        title, plain = refusal_words(r["what"], r["reason"], treatment)
        add("refusal", r["at"], title, plain, what=r["what"], logged=r["reason"])

    proposals = [p for x in hashes for p in store.proposals_for(x)]
    completes = {a.get("proposal_id"): a for a in actions if a["kind"] == "complete"}
    for p in proposals:
        if p["outcome"] == "amendment":
            add("fired", p["created_at"], "The amendment authorised an action now",
                f"Pre-signed action: {_act_words(p['action'], treatment)}.", proposal_id=p["id"])
        else:
            add("fired", p["created_at"], f"The contract fired: {_OUTCOME.get(p['outcome'], p['outcome']).lower()}",
                f"Pre-signed action: {_act_words(p['action'], treatment)}."
                + ("" if (p.get("authorised_by") == "signed_autonomy" or p.get("needs_tap") is False)
                   else f" It waits for {c.owner.name}'s signed tap."),
                proposal_id=p["id"], look_id=p.get("look_id"))
        done = completes.get(p["id"])
        try:
            res = json.loads(p["result"]) if p.get("result") else {}
        except ValueError:
            res = {}
        res = res if isinstance(res, dict) else {}
        # signed autonomy (when the service records it): the contract itself authorised the action, no tap
        auto = (p.get("authorised_by") or res.get("authorised_by")) == "signed_autonomy"
        if p.get("confirm_signer") and not auto:
            at = done["at"] if done else p["updated_at"]
            add("tap", at, f"{p['confirm_signer']} signed the tap",
                "The owner's key signed the confirmation text on the card.")
        if p["state"] == "executed":
            serving = res.get("serving")
            flag = res.get("flag", c.experiment.flag)
            what = (f"flag {flag} now serves {serving} to everyone." if serving else f"flag {flag} changed.")
            if auto:
                add("executed", done["at"] if done else p["updated_at"],
                    f"The agent {_act_words(p['action'], treatment)} on its own",
                    "The owner authorised this in the signed contract; no tap was needed. "
                    + (res.get("authorised_text") or p.get("authorised_text") or "") + f" One call: {what}",
                    autonomous=True)
            else:
                add("executed", done["at"] if done else p["updated_at"], "Executed in Flagsmith",
                    f"One call: experiment {exp} completed; {what}")

    ev.sort(key=lambda e: (e["at"] if e["at"] is not None else float("-inf"), _ORDER[e["kind"]]))
    for e in ev:
        e["when"] = _when(e["at"], t0)
        e.pop("at", None)

    # refusals that came right after a siren: the agent was tempted by that reading
    last_look = None
    for e in ev:
        if e["kind"] == "look" and e.get("counted"):
            last_look = e
        elif e["kind"] == "refusal" and last_look is not None and e.get("what") == "execute":
            e["after_look"] = last_look["look_number"]
            e["after_siren"] = last_look["siren"]

    sirens = [e for e in counted_rows if e["siren"]]
    first_win = next((e for e in counted_rows if e["chance"] is not None and e["chance"] >= WINNING), None)
    fired = next((e for e in counted_rows if e.get("fires")), None)
    decided = fired is not None
    if first_win is not None:
        cf = (f"An agent shipping at Flagsmith's first “Winning” would have shipped {treatment} at look "
              f"{first_win['look_number']}, on a {first_win['ctw']} reading.")
    elif decided:
        cf = "An agent shipping at Flagsmith's first “Winning” would never have shipped: no look read 95% or more."
    else:
        cf = "An agent shipping at Flagsmith's first “Winning” would not have shipped yet: no look has read 95% or more."
    if fired is not None:
        word = {"win": f"shipped {treatment} at look {fired['look_number']}",
                "loss": f"kept control at look {fired['look_number']} (a loss)",
                "horizon": f"kept control at look {fired['look_number']} (the horizon, no decision)",
                "guardrail_breach": f"stopped on a guardrail at look {fired['look_number']}",
                "expired": "kept control when the signed expiry passed with no decision"}.get(
            fired["outcome"], f"fired at look {fired['look_number']}")
        contract_line = f"The contract {word}."
    else:
        contract_line = "The contract has not fired." if counted_rows else "No look has been taken yet."
    for e in counted_rows:
        e["unbound"] = first_win is not None and e is first_win

    refused = [e for e in ev if e["kind"] == "refusal"]
    return {
        "events": ev,
        "sirens": [e["look_number"] for e in sirens],
        "refusals": len(refused),
        "tempted": sum(1 for e in refused if e.get("after_siren")),
        "watch": sum(1 for e in ev if e["kind"] == "look" and not e.get("counted")),
        "counterfactual": cf,
        "unbound_look": first_win["look_number"] if first_win else None,
        "unbound_seq": first_win["seq"] if first_win else None,
        "contract_line": contract_line,
        "looks": [{"look_number": e["look_number"], "seq": e["seq"], "word": e["word"], "outcome": e["outcome"],
                   "siren": e["siren"], "ctw": e["ctw"], "look_id": e["look_id"], "fires": e["fires"],
                   "unbound": e.get("unbound", False),
                   "refusals": sum(1 for r in refused if r.get("after_look") == e["look_number"])}
                  for e in counted_rows],
        "started": t0 is not None,
    }

