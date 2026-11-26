"""Page context for the signing surface: scenes, data sources and the calibration page.

``app.py`` keeps its routes and JSON shapes; each page route asks this module
for the extra context its template draws from. Everything here is a read of
data the service already holds (contracts, stored looks and decisions, the
calibration scripts' output files). Nothing here decides or signs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse

from .. import contract as ct
from ..decision import observation_from_snapshot
from ..flagsmith.gateway import MetricResult, ResultsSnapshot
from . import calibration_view, fmt, sources
from . import scene as sc

_ORDER = {"decided": 0, "active": 1, "signed": 2, "draft": 3, "executed": 4, "deviated": 5, "superseded": 6, "void": 7}


def setup(templates) -> None:
    env = templates.env
    env.filters.update(fmt.FILTERS)
    env.globals["scene_json"] = sc.to_json
    env.globals["elevation"] = sc.elevation
    env.globals["envelope_path"] = sc.envelope_path
    env.globals["svg_path"] = sc.svg_path
    env.globals["aria_label"] = sc.aria_label
    env.globals["WORDS"] = sc.WORDS
    env.globals["SHORT"] = sc.SHORT
    env.globals["fan_flat"] = sc.fan_flat


def _api_host() -> str:
    u = os.environ.get("FLAGSMITH_API_URL", "")
    return urlparse(u).netloc if u else ""


def source_for(svc, environment: str, experiment_id: int) -> dict:
    return sources.lookup(getattr(svc.store, "path", None), environment, experiment_id, _api_host())


def _contract_facts(svc, h: str) -> tuple[ct.Contract, dict, float]:
    c = svc.contract(h)
    return c, {"identities_per_arm": c.horizon.identities_per_arm, "looks": c.horizon.looks}, c.boundary.alpha


def _fractions(svc, environment: str, experiment_id: int) -> dict[int, dict]:
    out = {}
    for lk in svc.store.looks(environment, experiment_id):
        d = lk["decision"] or {}
        out[lk["seq"]] = {"fraction": d.get("fraction"), "look_number": d.get("look_number"), "id": lk["id"],
                          "as_of": fmt.age((lk["snapshot"] or {}).get("as_of"))}
    return out


def card_context(svc, d: dict) -> dict:
    """Extra context for card.html (look cards); proposal cards get the contract's bell only."""
    h = d["contract"]["hash"]
    c, horizon, alpha = _contract_facts(svc, h)
    env, exp = c.experiment.environment, c.experiment.experiment
    src = source_for(svc, env, exp)
    if d.get("kind") != "look":
        planned = [p.model_dump() for p in c.boundary.planned]
        scene = sc.build_sign({"hash": h, "summary": d["contract"]["summary"], "planned": planned,
                               "state": d["contract"]["state"], "needs": []},
                              source=src, horizon=horizon, alpha=alpha,
                              superseded=_superseded_planned(svc, c))
        return {"scene": scene, "source": src, "later": None, "guardrails": _guardrails(c), "keys": signer_keys(c)}
    fr = _fractions(svc, env, exp)
    scene = sc.build_card(d, source=src, horizon=horizon, alpha=alpha, fractions=fr)
    per_look = {}
    for lk in svc.store.looks(env, exp):
        per_look[lk["seq"]] = guardrail_rows(c, lk["snapshot"], lk["decision"])
    later = [v for s, v in sorted(fr.items()) if s > d["look"]["seq"]]
    snap = d.get("flagsmith") or {}
    prim = None
    for name, m in (snap.get("metrics") or {}).items():
        if c.primary_metric.name == name:
            prim = m
    return {
        "scene": scene, "source": src,
        "later": later[-1] if later else None,
        "as_of": fmt.age(snap.get("as_of")),
        "lift": fmt.lift((prim or {}).get("lift")),
        "arms_n": snap.get("identities") or {},
        "guardrails": _guardrails(c),
        "metric": c.primary_metric.name,
        "look_numbers": {seq: v.get("look_number") for seq, v in fr.items()},
        "guardrail_rows": guardrail_rows(c, d.get("flagsmith"), d.get("decision")),
        "guardrails_by_seq": per_look,
        "deviations": deviations(svc, env, exp, c),
        "keys": signer_keys(c),
    }


def _worse(c: ct.Contract, snap: dict | None, dec: dict | None) -> dict[str, float | None]:
    """Flagsmith's chance that treatment moved each guardrail the wrong way, as the engine reads it.

    Uses the decision's stored values when the look carries them; otherwise maps the stored
    snapshot with the engine's own ``observation_from_snapshot`` (orientation only, no statistics).
    """
    stored = (dec or {}).get("guardrail_chance_worse")
    if isinstance(stored, dict):
        return stored
    if not snap:
        return {g.name: None for g in c.guardrails}
    metrics = {k: MetricResult(k, m.get("direction") or "up", m.get("chance_to_win"), m.get("lift"))
               for k, m in (snap.get("metrics") or {}).items()}
    obs = observation_from_snapshot(c, ResultsSnapshot(identities=snap.get("identities") or {}, metrics=metrics))
    return obs.guardrail_chance_worse


def guardrail_rows(c: ct.Contract, snap: dict | None, dec: dict | None) -> list[dict]:
    """One row per guardrail. Uses the engine's own rows on the decision when the look has them."""
    words = {"pass": "within rule", "breach": "breach", "no_result": "no reading", "watch": "between looks"}
    engine = (dec or {}).get("guardrails")
    rows = []
    if engine:
        for r in engine:
            bz = r.get("boundary_z")
            rows.append({"name": r["name"], "direction": str(r.get("direction", "")).replace("_", " "),
                         "rule": _rule(r.get("alpha")),
                         "worse": None if r.get("chance_worse") is None else fmt.pct(r["chance_worse"], 2),
                         "worse_z": fmt.zval(r.get("z")) if r.get("z") is not None else None,
                         "stop_at": fmt.boundary_with_z(r.get("boundary_chance"), bz) if bz is not None else None,
                         "state": r.get("state", "no_result"), "word": words.get(r.get("state"), r.get("state"))})
        return rows
    worse = _worse(c, snap, dec)
    for g in c.guardrails:
        w = worse.get(g.name)
        state = "breach" if (dec or {}).get("guardrail") == g.name else ("no_result" if w is None else "unrecorded")
        rows.append({"name": g.name, "direction": g.direction.replace("_", " "), "rule": _rule(getattr(g, "alpha", None)),
                     "worse": None if w is None else fmt.pct(w, 2), "worse_z": None, "stop_at": None, "state": state,
                     "word": {"breach": "breach", "no_result": "no reading"}.get(state, "not checked by this look")})
    return rows


def _rule(alpha) -> str:
    return "one-sided false-stop budget " + (f"{alpha:g}" if isinstance(alpha, (int, float)) else "–") + ", spent at the counted looks"


def deviations(svc, environment: str, experiment_id: int, c: ct.Contract | None = None) -> list[dict]:
    """Lifecycle changes recorded outside the contract, placed against the contract's looks."""
    looks = svc.store.looks(environment, experiment_id)
    out = []
    for dv in svc.store.deviations(environment, experiment_id):
        before = [lk for lk in looks if lk["at"] <= dv["at"]]
        counted = [lk for lk in before if lk["counted"]]
        if not before:
            when = "before the first look"
        else:
            last = before[-1]
            k = (last["decision"] or {}).get("look_number") or (counted[-1]["decision"] or {}).get("look_number") if counted else None
            when = f"after refresh {last['seq']}" + (f" (look {k} of {c.horizon.looks})" if k and c else "")
        out.append({"log": dv["log"], "author": dv.get("author"), "audit_id": dv.get("audit_id"), "when": when,
                    "contract": dv.get("contract_hash")})
    return out


def signer_keys(c: ct.Contract) -> dict:
    def fp(k: str) -> str:
        body = k.split(":", 1)[-1]
        return f"{body[:8]}…{body[-8:]}"
    return {"owner": {"name": c.owner.name, "key": c.owner.public_key, "fp": fp(c.owner.public_key)},
            "second_signer": {"name": c.second_signer.name, "key": c.second_signer.public_key, "fp": fp(c.second_signer.public_key)}}


def _guardrails(c: ct.Contract) -> list[dict]:
    return [{"name": g.name, "direction": g.direction.replace("_", " "), "rule": _rule(getattr(g, "alpha", None))}
            for g in c.guardrails]


def _superseded_planned(svc, c: ct.Contract) -> list[dict] | None:
    if not c.amendment:
        return None
    try:
        old = svc.contract(c.amendment.supersedes)
    except Exception:  # noqa: BLE001 - the superseded contract may live in another store
        return None
    return [p.model_dump() for p in old.boundary.planned]


def sign_context(svc, data: dict) -> dict:
    h = data["hash"]
    c, horizon, alpha = _contract_facts(svc, h)
    src = source_for(svc, c.experiment.environment, c.experiment.experiment)
    sup = _superseded_planned(svc, c)
    changed: list[str] = []
    if c.amendment and sup is not None:
        try:
            old = svc.contract(c.amendment.supersedes)
            for field in ("hypothesis", "primary_metric", "guardrails", "horizon", "actions", "validity"):
                if getattr(old, field) != getattr(c, field):
                    changed.append(field)
            if [p.z for p in old.boundary.planned] != [p.z for p in c.boundary.planned] or old.boundary.alpha != alpha:
                changed.append("boundary")
            old_summary = svc.contract_status(c.amendment.supersedes)["summary"]
        except Exception:  # noqa: BLE001
            old_summary = None
    else:
        old_summary = None
    scene = sc.build_sign(data, source=src, horizon=horizon, alpha=alpha, superseded=sup)
    looks = svc.store.looks(c.experiment.environment, c.experiment.experiment)
    return {"scene": scene, "source": src, "changed": changed, "old": old_summary,
            "has_looks": bool(looks), "guardrails": _guardrails(c), "keys": signer_keys(c),
            "deviations": deviations(svc, c.experiment.environment, c.experiment.experiment, c)}


def index_context(svc, rows: list[dict]) -> dict:
    latest = sc.latest_looks(svc.store, rows)
    items = []
    for r in rows:
        lk = latest.get(r["hash"])
        dec = (lk or {}).get("decision") or {}
        c, error = None, None
        try:
            c = svc.contract(r["hash"])
            flag, looks = c.experiment.flag, c.horizon.looks
            planned = [p.model_dump() for p in c.boundary.planned]
        except Exception as exc:  # noqa: BLE001 - one unreadable contract must not take the list down
            flag, looks, planned = None, None, []
            error = str(getattr(exc, "message", None) or exc)
        mini = {"planned": sc.planned_rows(planned), "history": [], "decision": None}
        if lk:
            mini["history"] = [{"seq": 1, "t": dec.get("fraction") or 0.0,
                                "z": dec.get("z") if dec.get("chance_to_win") is not None else None,
                                "counted": dec.get("counted")}]
            mini["current_seq"] = 1
        try:
            src_row = source_for(svc, r["environment"], r["experiment_id"])
            devs = deviations(svc, r["environment"], r["experiment_id"], c)
            devs = [x for x in devs if x["contract"] in (None, r["hash"])]
        except Exception:  # noqa: BLE001
            src_row, devs = None, []
        items.append({
            **r, "flag": flag, "looks": looks, "source": src_row, "deviations": devs if c else [], "error": error,
            "look": None if not lk else {
                "id": lk["id"], "look_number": dec.get("look_number"), "outcome": dec.get("outcome"),
                "word": sc.SHORT.get(dec.get("outcome"), dec.get("outcome")),
                "ctw": fmt.ctw(dec.get("chance_to_win")) if dec.get("chance_to_win") is not None else None,
                "needs": (fmt.boundary_chance(dec.get("boundary_chance"), dec.get("boundary_z"))
                          if dec.get("boundary_z") is not None else None),
                "bz": fmt.zval(dec.get("boundary_z")),
                "at": lk["at"],
            },
            "glyph": sc.elevation(mini),
        })
    items.sort(key=lambda i: (0 if (i["look"] and i["state"] == "decided") else _ORDER.get(i["state"], 9) + 1,
                              -((i["look"] or {}).get("at") or 0)))
    hero = None
    readable = [i for i in items if not i["error"]]
    with_look = sorted((i for i in readable if i["look"]), key=lambda i: -i["look"]["at"])
    for cand in with_look:
        try:
            d = svc.look_view(cand["look"]["id"])
            hero = {"d": d, **card_context(svc, {"kind": "look", **d})}
            break
        except Exception:  # noqa: BLE001
            continue
    if hero is None and readable:
        newest_signed = next((i for i in readable if i["state"] in ("signed", "active")), readable[0])
        try:
            data = svc.contract_status(newest_signed["hash"])
            data["planned"] = [p.model_dump() for p in svc.contract(newest_signed["hash"]).boundary.planned]
            hero = {"sign": data, **sign_context(svc, data)}
        except Exception:  # noqa: BLE001
            hero = None
    if hero is not None:
        pass
    elif items:
        hero = {"unreadable": True, "source": None}
    else:
        horizon = ct.Horizon(identities_per_arm=1000, looks=10)
        planned = [p.model_dump() for p in ct.planned_table(horizon, 0.05)]
        hero = {"scene": sc.build_example(planned, alpha=0.05, looks=10), "example": True,
                "source": None}
    return {"items": items, "hero": hero, "unreadable": [i for i in items if i["error"]]}


def calibration_dir(svc) -> Path:
    env = os.environ.get("ULYSSES_CALIBRATION_DIR")
    if env:
        return Path(env)
    p = getattr(svc.store, "path", None)
    if p and p != ":memory:":
        return Path(p).resolve().parent / "calibration"
    return Path(".run/calibration")


def register(app, svc, templates) -> None:
    @app.get("/calibration", response_class=HTMLResponse)
    def calibration(request: Request, tab: str | None = None):
        data = calibration_view.calibration_page(calibration_dir(svc))
        if tab not in ("lab", "live"):
            tab = "live" if data["live"] and not data["live"].get("error") else "lab"
        active = data["live"] if tab == "live" else data["offline"]
        planned = (active or {}).get("planned")
        if not planned:
            planned = [p.model_dump() for p in ct.planned_table(ct.Horizon(identities_per_arm=3000, looks=30), 0.05)]
            planned = sc.planned_rows(planned)
        scene: dict[str, Any] = {
            "kind": "calibration", "tab": tab,
            "source": {"kind": "lab" if tab == "lab" else "synthetic", "simulated": True,
                       "chip": "Lab simulation" if tab == "lab" else "Synthetic identities",
                       "band": None, "detail": None},
            "planned": planned, "signed": True, "wire": True, "history": [], "current_seq": None, "decision": None,
            "proposal": None, "money": None,
            "contract": {"looks": len(planned), "alpha": (active or {}).get("alpha", 0.05)},
            "paths": (active or {}).get("paths"),
            "looks": len(planned),
        }
        return templates.TemplateResponse(request, "calibration.html", {"cal": data, "tab": tab, "scene": scene,
                                                                        "active": active})
