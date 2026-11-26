"""The HTTP surfaces: agent auth, the key never leaving, signatures as auth, the audit webhook."""

import hashlib
import hmac
import json
from statistics import NormalDist

import pytest
from fastapi.testclient import TestClient

from ulysses import contract as ct
from ulysses import signing
from ulysses.flagsmith.gateway import FlagsmithError
from ulysses.service.app import create_app

from .conftest import make_contract, read_all

FS_KEY = "fs-admin-SECRET-key-0123456789"
TOKEN = "agent-token-0123456789abcdef"
HOOK = "webhook-secret-abcdef123456"
PHI = NormalDist().cdf


@pytest.fixture
def http(world):
    svc, fs, store = world
    app = create_app(svc, agent_token=TOKEN, protected_secrets=[FS_KEY, HOOK], webhook_secret=HOOK)
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    seen: list[str] = []
    orig = client.request

    def recording(*a, **kw):
        r = orig(*a, **kw)
        seen.append(r.text)
        return r

    client.request = recording  # type: ignore[method-assign]
    return client, svc, fs, seen


def test_agent_surface_requires_the_token(world):
    svc, _, _ = world
    c = TestClient(create_app(svc, agent_token=TOKEN))
    assert c.get("/api/contracts").status_code == 401
    assert c.get("/api/contracts", headers={"Authorization": "Bearer wrong-token-xxxxxxxx"}).status_code == 401
    assert c.get("/api/contracts", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_full_flow_over_http_and_the_key_never_appears(http, keys):
    client, svc, fs, seen = http
    c = make_contract(keys, n=1000, looks=10)
    r = client.post("/api/contracts", json={"toml": ct.to_toml(c)})
    assert r.status_code == 200, r.text
    h = r.json()["hash"]
    # Unsigned: start refused with 403 and a link to sign.
    r = client.post(f"/api/contracts/{h}/start")
    assert r.status_code == 403 and "sign_url" in r.json()
    # The owner signs on the human surface (no agent token involved).
    sig = keys["maya"].sign(signing.contract_message(h, "owner"))
    r = client.post("/human/sign", json={"hash": h, "role": "owner", "signer": "maya", "signature": sig},
                    headers={"Authorization": ""})
    assert r.status_code == 200 and r.json()["state"] == "signed"
    assert client.post(f"/api/contracts/{h}/start").status_code == 200
    fs.set_results(1, 1000, 1000, PHI(3.2))
    r = client.post(f"/api/contracts/{h}/look").json()
    assert r["decision"]["outcome"] == "win"
    pid = r["proposal"]["id"]
    # The agent asks to execute before the tap: refused.
    assert client.post(f"/api/proposals/{pid}/execute", json={}).status_code == 403
    # The card page renders the decision, from the link the service handed out (it carries a read capability).
    page = client.get(_path(r["card_url"]))
    assert page.status_code == 200 and "WIN" in page.text
    # One tap: the owner's confirmation signature.
    card = client.get(f"/human/card/{r['look_id']}").json()
    csig = keys["maya"].sign(card["confirm_message"].encode())
    done = client.post("/human/confirm", json={"proposal_id": pid, "signer": "maya", "signature": csig})
    assert done.status_code == 200 and done.json()["state"] == "executed"
    assert fs.served["checkout_button"] == "big_cta"
    # Every response on every surface so far: the Flagsmith key is never in it.
    for path in ("/api/contracts", f"/api/contracts/{h}", f"/api/contracts/{h}/toml", f"/api/looks/{r['look_id']}",
                 "/api/experiments/env-key/1", "/api/refusals", "/", f"/sign/{h}", "/api/docs", "/openapi.json"):
        client.get(path)
    assert seen and all(FS_KEY not in body for body in seen)


def test_guard_withholds_a_response_that_would_leak_the_key(http, keys, monkeypatch):
    client, svc, fs, _ = http

    def leaky(*a, **kw):
        raise FlagsmithError(f"upstream said: bad key {FS_KEY}", status=401)

    monkeypatch.setattr(fs, "get_experiment", leaky)
    r = client.post("/api/contracts", json={"toml": ct.to_toml(make_contract(keys))})
    assert r.status_code == 500
    assert FS_KEY not in r.text and "withheld" in r.text


def test_action_outside_contract_refused_over_http(http, keys):
    client, svc, fs, _ = http
    h = svc.submit(make_contract(keys, n=1000, looks=10))["hash"]
    svc.sign(h, "owner", "maya", keys["maya"].sign(signing.contract_message(h, "owner")))
    client.post(f"/api/contracts/{h}/start")
    r = client.post(f"/api/contracts/{h}/execute", json={"action": "ship_treatment"})
    assert r.status_code == 403
    assert fs.served == {}


def _hook_body(log: str, author=None, data=None) -> str:
    body = {"event_type": "AUDIT_LOG_CREATED", "data": data if data is not None else {
        "id": 501, "log": log, "author": author, "related_object_type": "EXPERIMENT", "related_object_id": 1}}
    return json.dumps(body, sort_keys=True)


def _signed(raw: str, secret: str = HOOK) -> dict:
    return {"X-Flagsmith-Signature": hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest(),
            "content-type": "application/json"}


def test_audit_webhook_checks_signature_and_records_deviation(http, keys):
    client, svc, fs, _ = http
    h = svc.submit(make_contract(keys))["hash"]
    svc.sign(h, "owner", "maya", keys["maya"].sign(signing.contract_message(h, "owner")))
    svc.start_under_contract(h)
    raw = _hook_body("Experiment 'exp-1' completed for environment Test", {"id": 3, "email": "pm@example.com"})
    bad = client.post("/webhooks/flagsmith", content=raw,
                      headers={"X-Flagsmith-Signature": "00", "content-type": "application/json"})
    assert bad.status_code == 401
    # Correctly signed, but Flagsmith still shows the experiment running: recorded, nothing frozen.
    ok = client.post("/webhooks/flagsmith", content=raw, headers=_signed(raw))
    assert ok.status_code == 200 and ok.json()["verdict"] == "unverified"
    assert svc.contract_status(h)["state"] == "active"
    # The person really completes it in the dashboard; the next delivery is a deviation.
    fs.dashboard_complete(1, user="pm@example.com")
    raw2 = _hook_body("Experiment 'exp-1' completed for environment Test", {"id": 3, "email": "pm@example.com"},
                      data={"id": 502, "log": "Experiment 'exp-1' completed for environment Test",
                            "author": {"id": 3, "email": "pm@example.com"}, "related_object_type": "EXPERIMENT",
                            "related_object_id": 1})
    ok2 = client.post("/webhooks/flagsmith", content=raw2, headers=_signed(raw2))
    assert ok2.status_code == 200 and ok2.json()["verdict"] == "deviation"
    assert svc.store.deviations()[0]["author"] == "pm@example.com"


def test_webhook_fails_closed_without_a_secret(world):
    svc, fs, _ = world
    c = TestClient(create_app(svc, agent_token=TOKEN))
    raw = _hook_body("Experiment 'exp-1' completed for environment Test", None)
    r = c.post("/webhooks/flagsmith", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 503
    dev = TestClient(create_app(svc, agent_token=TOKEN, webhook_insecure=True))
    assert dev.post("/webhooks/flagsmith", content=raw, headers={"content-type": "application/json"}).status_code == 200


def test_webhook_rejects_non_json_and_malformed_bodies(http):
    client, svc, fs, _ = http
    raw = _hook_body("x", None)
    plain = client.post("/webhooks/flagsmith", content=raw,
                        headers={**_signed(raw), "content-type": "text/plain"})
    assert plain.status_code == 415  # a browser cannot send this cross-site without a preflight
    bad = json.dumps({"event_type": "AUDIT_LOG_CREATED", "data": "notadict"})
    assert client.post("/webhooks/flagsmith", content=bad, headers=_signed(bad)).status_code == 400


def test_agent_read_proxy_returns_flagsmith_numbers_without_a_key(http):
    client, svc, fs, seen = http
    fs.set_results(1, 120, 130, 0.7)
    r = client.get("/api/flagsmith/experiments/env-key/1")
    assert r.status_code == 200 and r.json()["results"]["identities"]["big_cta"] == 130
    assert FS_KEY not in r.text
    assert client.post("/api/flagsmith/experiments/env-key/1").status_code == 405


def _path(url: str) -> str:
    from urllib.parse import urlparse

    u = urlparse(url)
    return u.path + (f"?{u.query}" if u.query else "")


def test_sign_page_and_enrolment(http, keys):
    client, svc, fs, _ = http
    h = svc.submit(make_contract(keys))["hash"]
    page = client.get(_path(svc.contract_status(h)["sign_url"]))
    assert page.status_code == 200 and "Stop contract for experiment 1" in page.text
    assert signing.contract_message(h, "owner").decode().splitlines()[1] in page.text
    tok = svc.enrol_token("ana")
    assert client.get(f"/enrol/{tok}").status_code == 200
    new = signing.Keypair.generate()
    r = client.post("/human/enrol", json={"token": tok, "public_key": new.public})
    assert r.status_code == 200
    assert client.post("/human/enrol", json={"token": tok, "public_key": new.public}).status_code == 403
    assert client.get("/sign/sha256:nothing").status_code == 401  # no overview grant: nothing says what exists
    assert read_all(client, svc).get("/sign/sha256:nothing").status_code == 404
