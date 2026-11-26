"""Smoke test against a real Flagsmith: sign a contract, start it, take one look. Exits non-zero on failure.

Runs the real contract service (in this process, on 127.0.0.1) over the real REST gateway, and drives
it the way the agent and the owner do: the agent surface with the agent token over HTTP, the owner's
Ed25519 signature on the signing surface. Checks, in order:

1. a contract drafted from Flagsmith's own experiment is refused a start while unsigned (403);
2. the owner's signature verifies and the start lands: Flagsmith shows the experiment running with the
   contract hash in its hypothesis;
3. one look runs a real results refresh and read, and the service records a decision.

Then it completes the smoke experiment keeping control, so nothing is left running. It uses its own flag
``ulysses_smoke_cta`` in the ``ulysses-demo`` environment and its own store under ``.run/smoke/``.

    # a fresh, empty Flagsmith (CI: the repo's docker-compose.yml): create the first user, org and key
    uv run python scripts/smoke.py --flagsmith http://localhost:8280 --bootstrap
    # a Flagsmith you already have keys for (FLAGSMITH_ADMIN_KEY / FLAGSMITH_ORGANISATION_ID, or .env)
    uv run python scripts/smoke.py --flagsmith http://localhost:8000
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ulysses import lab, signing  # noqa: E402
from ulysses.core import Settings, Ulysses  # noqa: E402
from ulysses.flagsmith.rest import RestFlagsmith  # noqa: E402
from ulysses.service.app import create_app  # noqa: E402
from ulysses.service.runtime import load_dotenv  # noqa: E402
from ulysses.store import Store  # noqa: E402

FLAG = "ulysses_smoke_cta"
RUN = ROOT / ".run" / "smoke"


def say(text: str) -> None:
    print(f"smoke: {text}", flush=True)


def fail(text: str) -> None:
    print(f"smoke FAILED: {text}", file=sys.stderr, flush=True)
    sys.exit(1)


def wait_for(url: str, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(3)
    fail(f"{url} did not answer within {seconds:.0f} s")


def main() -> None:
    ap = argparse.ArgumentParser(description="sign, start and look once against a real Flagsmith")
    ap.add_argument("--flagsmith", default=os.environ.get("FLAGSMITH_API_URL", "http://localhost:8280"))
    ap.add_argument("--bootstrap", action="store_true",
                    help="fresh instance: sign up the first user and create an organisation and admin key")
    ap.add_argument("--port", type=int, default=8259, help="port for this run's contract service")
    ap.add_argument("--wait", type=float, default=600, help="seconds to wait for Flagsmith to answer /health")
    a = ap.parse_args()
    api = a.flagsmith.rstrip("/")
    wait_for(f"{api}/health", a.wait)
    say(f"Flagsmith answers at {api}")

    if a.bootstrap:
        try:
            b = lab.bootstrap_stock(api, "smoke-owner@ulysses.test", secrets.token_urlsafe(18))
        except (RuntimeError, httpx.HTTPError) as exc:
            fail(f"bootstrap: {exc}")
        cfg = lab.LabConfig(api, b["admin_key"], b["organisation_id"])
        say(f"bootstrapped organisation {b['organisation_id']} with an admin API key")
    else:
        load_dotenv(ROOT / ".env")
        os.environ["FLAGSMITH_API_URL"] = api
        cfg = lab.LabConfig.from_env()

    shutil.rmtree(RUN, ignore_errors=True)
    RUN.mkdir(parents=True)
    admin = lab.Admin(cfg)
    pid = admin.project()
    env = admin.environment(pid, lab.DEMO_ENV)["key"]
    admin.warehouse(env)
    fid, oid = admin.mv_flag(pid, FLAG, "classic", "bold", "bold", "Ulysses smoke test flag")
    m_buy = admin.metric(env, lab.PURCHASE_METRIC, "occurrence", "up", lab.PURCHASE_EVENT, "Identity completed a purchase")
    m_ref = admin.metric(env, lab.REFUND_METRIC, "occurrence", "down", lab.REFUND_EVENT,
                         "Identity asked for a refund (guardrail)")
    for c in admin.clear_active(env, fid, replace=True):
        say(f"earlier smoke experiment {c['id']} ({c['was']}) {c['action']}")
    exp = admin.create_experiment(env, feature_id=fid, option_id=oid, name="ulysses smoke test",
                                  hypothesis="Smoke test: no traffic is sent.", control_value="classic",
                                  metrics=[(m_buy, "increase"), (m_ref, "not_increase")])
    eid = int(exp["id"])
    say(f"experiment {eid} created on flag {FLAG} (status {exp['status']})")

    gw = RestFlagsmith(api, cfg.org_api_key)
    svc = Ulysses(Store(RUN / "smoke.db"), gw, Settings(public_url=f"http://127.0.0.1:{a.port}",
                                                        refresh_wait_seconds=150))
    owner, second = signing.Keypair.generate(), signing.Keypair.generate()
    svc.register_signer("smoke-owner", owner.public)
    svc.register_signer("smoke-second", second.public)
    token = secrets.token_urlsafe(24)
    app = create_app(svc, agent_token=token, protected_secrets=[cfg.org_api_key])
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=a.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{a.port}"
    wait_for(f"{base}/healthz", 30)
    agent = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=300)
    human = httpx.Client(base_url=base, timeout=60)
    completed = False
    try:
        r = agent.post("/api/contracts/draft", json={
            "environment": env, "experiment_id": eid, "owner": "smoke-owner", "second_signer": "smoke-second",
            "min_relative_lift": 0.5, "baseline_rate": 0.1, "looks": 6})
        if r.status_code != 200:
            fail(f"draft -> {r.status_code} {r.text[:300]}")
        h = r.json()["hash"]
        say(f"contract {h[:19]} drafted from Flagsmith's experiment")

        r = agent.post(f"/api/contracts/{h}/start")
        if r.status_code != 403:
            fail(f"an unsigned contract was not refused: {r.status_code} {r.text[:300]}")
        say("start refused while unsigned (403)")

        sig = owner.sign(signing.contract_message(h, "owner"))
        r = human.post("/human/sign", json={"hash": h, "role": "owner", "signer": "smoke-owner", "signature": sig})
        if r.status_code != 200 or not r.json()["signatures"]["owner"]["verified"]:
            fail(f"owner signature -> {r.status_code} {r.text[:300]}")
        say("owner signature verified")

        r = agent.post(f"/api/contracts/{h}/start")
        if r.status_code != 200 or r.json().get("state") != "active":
            fail(f"signed start -> {r.status_code} {r.text[:300]}")
        r = agent.get(f"/api/flagsmith/experiments/{env}/{eid}")
        e = r.json()["experiment"] if r.status_code == 200 else {}
        if e.get("status") != "running" or h not in (e.get("hypothesis") or ""):
            fail(f"Flagsmith does not show the started experiment with the hash: {r.status_code} {r.text[:300]}")
        say("started: Flagsmith shows the experiment running with the contract hash in its hypothesis")

        r = agent.post(f"/api/contracts/{h}/look")
        body = r.json() if r.status_code == 200 else {}
        if r.status_code != 200 or not body.get("look_id") or body.get("look_not_taken"):
            fail(f"look -> {r.status_code} {r.text[:400]}")
        d = body["decision"]
        say(f"look {body['look_id']} recorded: refreshed={body['refreshed']}, outcome {d['outcome']}, "
            f"Flagsmith identities {(body.get('flagsmith') or {}).get('identities')}")
        if cfg.org_api_key in r.text:
            fail("the Flagsmith key appeared in an agent response")
        gw.complete(env, eid, None)
        completed = True
        say(f"experiment {eid} completed keeping control (smoke clean-up)")
        say("PASS")
    finally:
        if not completed:
            try:
                gw.complete(env, eid, None)
            except Exception:  # noqa: BLE001 - best effort clean-up after a failure
                pass
        server.should_exit = True


if __name__ == "__main__":
    main()
