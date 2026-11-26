"""Set up Flagsmith objects for the demo and the calibration (setup tooling only).

Uses the organisation admin API key. Everything is created idempotently and
named with the ``ulysses`` prefix:

* environment ``ulysses-demo`` with the "Flagsmith database" warehouse, flag
  ``ulysses_checkout_cta`` (control "classic", variation ``bold``), metrics
  for purchases (goal) and refund requests (guardrail, should not increase),
  and experiments on that flag;
* environment ``ulysses-calibration`` with A/A flags whose two variations
  carry identical values, for the engine calibration.

Both environments live in one Flagsmith project (``FLAGSMITH_PROJECT_ID``, or
the organisation's first project, created as ``ulysses`` if there is none).
Self-hosted Flagsmith without a licence allows one project per organisation
(``MAX_PROJECTS_IN_FREE_PLAN = 1`` in ``api/app/settings/common.py``).

The ids and keys it creates are written to ``.run/lab.json`` (git-ignored).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEMO_ENV = "ulysses-demo"
AA_FLAG = "ulysses_headline_aa"  # an A/A flag: both variations carry the same value
AA_VALUE = "Your basket"
AA_VARIATION = "twin"
DEMO_FLAG = "ulysses_checkout_cta"
DEMO_CONTROL_VALUE = "classic"
DEMO_VARIATION = "bold"
DEMO_VARIATION_VALUE = "bold"
PURCHASE_METRIC, PURCHASE_EVENT = "ulysses_purchase", "purchase"
REFUND_METRIC, REFUND_EVENT = "ulysses_refund_request", "refund_requested"

CAL_ENV = "ulysses-calibration"
CAL_METRIC, CAL_EVENT = "ulysses_aa_conversion", "aa_convert"
CAL_FLAG_PREFIX = "ulysses_aa_"
CAL_VALUE = "same"
CAL_VARIATION = "twin"

STATE_PATH = Path(".run/lab.json")


def results_list(data: Any) -> list:
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    return data or []


@dataclass
class LabConfig:
    api_url: str
    org_api_key: str
    organisation_id: int
    project_id: int | None = None

    @classmethod
    def from_env(cls) -> "LabConfig":
        url = os.environ.get("FLAGSMITH_API_URL", "http://localhost:8000")
        key = os.environ.get("FLAGSMITH_ADMIN_KEY") or os.environ.get("FLAGSMITH_ORG_API_KEY")
        org = os.environ.get("FLAGSMITH_ORGANISATION_ID")
        if not key or not org:
            raise SystemExit(
                "Set FLAGSMITH_ADMIN_KEY (an organisation admin API key) and FLAGSMITH_ORGANISATION_ID. "
                "See .env.example."
            )
        proj = os.environ.get("FLAGSMITH_PROJECT_ID")
        return cls(url.rstrip("/"), key, int(org), int(proj) if proj else None)


class Admin:
    def __init__(self, cfg: LabConfig):
        self.cfg = cfg
        self.http = httpx.Client(base_url=cfg.api_url, timeout=60,
                                 headers={"Authorization": f"Api-Key {cfg.org_api_key}"})

    def call(self, method: str, path: str, ok=(200, 201, 202, 204), **kw) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.content else None

    # ---- containers -----------------------------------------------------

    def project(self) -> int:
        if self.cfg.project_id:
            return self.cfg.project_id
        ps = results_list(self.call("GET", "/api/v1/projects/", params={"organisation": self.cfg.organisation_id}))
        if not ps:
            # A fresh organisation: its admin key may create the one project allowed.
            return int(self.call("POST", "/api/v1/projects/",
                                 json={"name": "ulysses", "organisation": self.cfg.organisation_id})["id"])
        return int(sorted(ps, key=lambda p: p["id"])[0]["id"])

    def environment(self, project_id: int, name: str) -> dict:
        es = results_list(self.call("GET", "/api/v1/environments/", params={"project": project_id}))
        e = next((x for x in es if x["name"] == name), None)
        if e is None:
            e = self.call("POST", "/api/v1/environments/", json={"name": name, "project": project_id})
        return {"id": int(e["id"]), "key": e["api_key"]}

    def server_key(self, env_key: str, name: str = "ulysses-server-key") -> str:
        ks = results_list(self.call("GET", f"/api/v1/environments/{env_key}/api-keys/"))
        k = next((x for x in ks if x["name"] == name), None)
        if k is None:
            k = self.call("POST", f"/api/v1/environments/{env_key}/api-keys/", json={"name": name})
        return k["key"]

    def warehouse(self, env_key: str) -> dict:
        conns = self.call("GET", f"/api/v1/environments/{env_key}/warehouse-connections/")
        if conns:
            return conns[0]
        return self.call("POST", f"/api/v1/environments/{env_key}/warehouse-connections/",
                         json={"warehouse_type": "postgres", "config": {"source": "flagsmith_database"}})

    # ---- flags and metrics ----------------------------------------------

    def mv_flag(self, project_id: int, name: str, control_value: str, variation_key: str, variation_value: str,
                description: str) -> tuple[int, int]:
        fs = results_list(self.call("GET", f"/api/v1/projects/{project_id}/features/", params={"search": name}))
        f = next((x for x in fs if x["name"] == name), None)
        if f is None:
            f = self.call("POST", f"/api/v1/projects/{project_id}/features/", json={
                "name": name, "type": "MULTIVARIATE", "initial_value": control_value,
                "default_enabled": True, "description": description})
        fid = int(f["id"])
        opts = results_list(self.call("GET", f"/api/v1/projects/{project_id}/features/{fid}/mv-options/"))
        o = next((x for x in opts if x.get("key") == variation_key), None)
        if o is None:
            o = self.call("POST", f"/api/v1/projects/{project_id}/features/{fid}/mv-options/", json={
                "feature": fid, "type": "unicode", "string_value": variation_value,
                "default_percentage_allocation": 0, "key": variation_key})
        return fid, int(o["id"])

    def metric(self, env_key: str, name: str, aggregation: str, direction: str, event: str, description: str) -> int:
        ms = results_list(self.call("GET", f"/api/v1/environments/{env_key}/experiment-metrics/", params={"q": name}))
        m = next((x for x in ms if x["name"] == name), None)
        if m is None:
            m = self.call("POST", f"/api/v1/environments/{env_key}/experiment-metrics/", json={
                "name": name, "description": description, "aggregation": aggregation,
                "direction": direction, "definition": {"version": 1, "event": event}})
        return int(m["id"])

    # ---- experiments ------------------------------------------------------

    def experiments(self, env_key: str, q: str | None = None) -> list[dict]:
        params = {"q": q} if q else {}
        params["page_size"] = 100
        return results_list(self.call("GET", f"/api/v1/environments/{env_key}/experiments/", params=params))

    def clear_active(self, env_key: str, feature_id: int, *, governed: set[int] | None = None,
                     replace: bool = False) -> list[dict]:
        """End the flag's unfinished experiment so a new one can be created (Flagsmith allows one per flag).

        A ``created`` experiment is deleted. A running or paused one is completed keeping control, but only
        with ``replace=True``, and never one a Ulysses contract governs (finish that under its contract).
        Returns what was done, so callers can print it.
        """
        done = []
        for e in self.experiments(env_key):
            feat = e["feature"]["id"] if isinstance(e["feature"], dict) else e["feature"]
            if feat != feature_id or e["status"] == "completed":
                continue
            base = f"/api/v1/environments/{env_key}/experiments/{e['id']}"
            if e["status"] == "created":
                self.call("DELETE", f"{base}/")
                done.append({"id": e["id"], "was": "created", "action": "deleted"})
                continue
            if int(e["id"]) in (governed or set()):
                raise RuntimeError(
                    f"experiment {e['id']} ({e['status']}) on this flag is governed by a Ulysses contract; finish "
                    "it under its contract (ulysses watch / execute) before starting a new demo")
            if not replace:
                raise RuntimeError(
                    f"experiment {e['id']} on this flag is {e['status']}; pass --replace to complete it keeping "
                    "control, or finish it first")
            self.call("POST", f"{base}/complete/", json={"rollout_decision": "control"})
            done.append({"id": e["id"], "was": e["status"], "action": "completed, kept control"})
        return done

    def create_experiment(self, env_key: str, *, feature_id: int, option_id: int, name: str, hypothesis: str,
                          control_value: str, metrics: list[tuple[int, str]], split: int = 50) -> dict:
        return self.call("POST", f"/api/v1/environments/{env_key}/experiments/", json={
            "feature": feature_id,
            "name": name,
            "hypothesis": hypothesis,
            "metrics": [{"metric": m, "expected_direction": d} for m, d in metrics],
            "experiment_rollout": {
                "enabled": False,
                "rollout_percentage": 100,
                "feature_state_value": {"type": "string", "value": control_value},
                "multivariate_feature_state_values": [
                    {"multivariate_feature_option": option_id, "percentage_allocation": split}],
            },
        })


def ensure_audit_webhook(cfg: LabConfig, url: str, secret: str | None) -> dict:
    """Point Flagsmith's organisation audit-log webhook at the contract service."""
    a = Admin(cfg)
    path = f"/api/v1/organisations/{cfg.organisation_id}/webhooks/"
    hooks = results_list(a.call("GET", path))
    hook = next((h for h in hooks if h.get("url") == url), None)
    body = {"url": url, "enabled": True, "secret": secret or ""}
    if hook is None:
        hook = a.call("POST", path, json=body)
    elif not hook.get("enabled") or (secret and hook.get("secret") != secret):
        hook = a.call("PUT", f"{path}{hook['id']}/", json=body)
    return {"id": hook["id"], "url": hook["url"], "enabled": hook.get("enabled")}


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def setup_demo(cfg: LabConfig) -> dict:
    a = Admin(cfg)
    pid = a.project()
    env = a.environment(pid, DEMO_ENV)
    a.warehouse(env["key"])
    fid, oid = a.mv_flag(pid, DEMO_FLAG, DEMO_CONTROL_VALUE, DEMO_VARIATION, DEMO_VARIATION_VALUE,
                         "Ulysses demo: the checkout call-to-action style")
    m_buy = a.metric(env["key"], PURCHASE_METRIC, "occurrence", "up", PURCHASE_EVENT,
                     "Identity completed a purchase")
    m_ref = a.metric(env["key"], REFUND_METRIC, "occurrence", "down", REFUND_EVENT,
                     "Identity asked for a refund (guardrail)")
    aa_fid, aa_oid = a.mv_flag(pid, AA_FLAG, AA_VALUE, AA_VARIATION, AA_VALUE,
                               "Ulysses demo: an A/A flag; both variations show the same headline")
    state = load_state()
    state["demo"] = {
        "api_url": cfg.api_url, "organisation_id": cfg.organisation_id, "project_id": pid,
        "environment_id": env["id"], "environment_key": env["key"], "server_key": a.server_key(env["key"]),
        "flag": DEMO_FLAG, "feature_id": fid, "variation": DEMO_VARIATION, "variation_option_id": oid,
        "metrics": {"purchase": m_buy, "refund": m_ref},
        "aa": {"flag": AA_FLAG, "feature_id": aa_fid, "variation": AA_VARIATION, "variation_option_id": aa_oid},
    }
    save_state(state)
    return state["demo"]


def new_demo_experiment(cfg: LabConfig, name: str, hypothesis: str, *, which: str = "cta",
                        replace: bool = False, governed: set[int] | None = None) -> dict:
    """A fresh experiment (status created) on the demo flag (``cta``) or the A/A flag (``aa``)."""
    d = load_state().get("demo") or setup_demo(cfg)
    if which == "aa" and "aa" not in d:
        d = setup_demo(cfg)
    f = d["aa"] if which == "aa" else {"feature_id": d["feature_id"], "variation_option_id": d["variation_option_id"],
                                        "flag": d["flag"]}
    control_value = AA_VALUE if which == "aa" else DEMO_CONTROL_VALUE
    a = Admin(cfg)
    cleared = a.clear_active(d["environment_key"], f["feature_id"], governed=governed, replace=replace)
    e = a.create_experiment(
        d["environment_key"], feature_id=f["feature_id"], option_id=f["variation_option_id"], name=name,
        hypothesis=hypothesis, control_value=control_value,
        metrics=[(d["metrics"]["purchase"], "increase"), (d["metrics"]["refund"], "not_increase")],
    )
    return {"experiment_id": e["id"], "status": e["status"], "environment_key": d["environment_key"],
            "project_id": d["project_id"], "flag": f["flag"], "cleared": cleared}


def bootstrap_stock(api_url: str, email: str, password: str, org_name: str = "ulysses") -> dict:
    """On a fresh self-hosted Flagsmith: first user, an organisation and an organisation admin API key.

    The first user to sign up on a new instance becomes its superuser. Used by the one-command judge demo
    against its own throwaway Flagsmith; never against a shared instance.
    """
    http = httpx.Client(base_url=api_url.rstrip("/"), timeout=60)
    r = http.post("/api/v1/auth/users/", json={"email": email, "password": password, "re_password": password,
                                               "first_name": "Demo", "last_name": "Owner"})
    if r.status_code == 201 and r.json().get("key"):
        token = r.json()["key"]
    else:
        r = http.post("/api/v1/auth/login/", json={"email": email, "password": password})
        if r.status_code != 200:
            raise RuntimeError(f"cannot sign up or log in on {api_url}: {r.status_code} {r.text[:200]}")
        token = r.json()["key"]
    auth = {"Authorization": f"Token {token}"}
    orgs = results_list(http.get("/api/v1/organisations/", headers=auth).json())
    org = next((o for o in orgs if o["name"] == org_name), None)
    if org is None:
        org = http.post("/api/v1/organisations/", json={"name": org_name}, headers=auth).json()
    k = http.post(f"/api/v1/organisations/{org['id']}/master-api-keys/",
                  json={"name": "ulysses-service", "is_admin": True}, headers=auth)
    if k.status_code not in (200, 201):
        raise RuntimeError(f"cannot create an organisation API key: {k.status_code} {k.text[:200]}")
    return {"organisation_id": int(org["id"]), "admin_key": k.json()["key"]}


def setup_calibration(cfg: LabConfig, flags: int) -> dict:
    a = Admin(cfg)
    pid = a.project()
    env = a.environment(pid, CAL_ENV)
    a.warehouse(env["key"])
    mid = a.metric(env["key"], CAL_METRIC, "occurrence", "up", CAL_EVENT,
                   "A/A calibration conversion (synthetic identities)")
    out = []
    for i in range(flags):
        name = f"{CAL_FLAG_PREFIX}{i:03d}"
        fid, oid = a.mv_flag(pid, name, CAL_VALUE, CAL_VARIATION, CAL_VALUE,
                             "Ulysses A/A calibration: both variations serve the same value")
        out.append({"flag": name, "feature_id": fid, "option_id": oid})
    state = load_state()
    state["calibration"] = {
        "api_url": cfg.api_url, "project_id": pid, "environment_id": env["id"], "environment_key": env["key"],
        "server_key": a.server_key(env["key"]), "metric_id": mid, "flags": out,
    }
    save_state(state)
    return state["calibration"]
