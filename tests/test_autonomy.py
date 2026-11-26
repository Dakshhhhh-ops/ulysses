"""Signed autonomy: the owner decides, inside the signed contract, which pre-assigned actions run without a tap.

Only ``keep_control`` may be listed. Every experiment here runs over the test double of Flagsmith.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
from pathlib import Path
from statistics import NormalDist

import anyio
import pytest
from pydantic import ValidationError

from ulysses import contract as ct
from ulysses import verify as vf
from ulysses.core import Invalid, Refused, Settings, Ulysses, autonomy_text, summarise
from ulysses.flagsmith.fake import FakeFlagsmith
from ulysses.mcp_server import card_text
from ulysses.replay import Replayer, export_store, load_trace, write_trace
from ulysses.store import Store

from .conftest import Clock, make_contract, sign_as, tap

PHI = NormalDist().cdf
ROOT = Path(__file__).resolve().parents[1]
KEEP = ct.Autonomy(without_confirmation=["keep_control"])
T0 = _dt.datetime(2001, 2, 3, 4, 5, 6, tzinfo=_dt.timezone.utc).timestamp()  # synthetic


def started(svc, keys, **kw) -> str:
    h = svc.submit(make_contract(keys, n=1000, looks=10, **kw))["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    return h


def breach(svc, fs, h) -> dict:
    fs.set_results(1, 120, 118, 0.99, guardrails={"refunds": 0.00001})  # refunds almost surely up
    out = svc.look(h)
    assert out["decision"]["outcome"] == "guardrail_breach"
    return out


# ---- contract validation ------------------------------------------------------------------


def test_absent_autonomy_keeps_every_existing_hash(keys):
    c = make_contract(keys)
    assert c.autonomy is None and "autonomy" not in c.canonical()
    assert "[autonomy]" not in ct.to_toml(c)
    assert ct.from_toml(ct.to_toml(c)).hash == c.hash
    with_clause = make_contract(keys, autonomy=KEEP)
    assert with_clause.hash != c.hash  # the clause is signed: it is in the hash
    assert ct.from_toml(ct.to_toml(with_clause)).hash == with_clause.hash


def test_recorded_contracts_in_data_still_hash_the_same():
    bundle = vf.load_bundle(ROOT / "data" / "verify-judge-demo-cta.json")
    for entry in bundle["contracts"]:
        assert ct.from_toml(entry["toml"]).hash == entry["hash"]
    for line in (ROOT / "data" / "replay-judge-demo.jsonl").read_text(encoding="utf-8").splitlines():
        ev = json.loads(line)
        if ev.get("event") == "contract":
            assert ct.from_canonical(ev["canonical"]).hash == ev["hash"]


def test_ship_treatment_can_never_be_autonomous(keys):
    with pytest.raises(ValidationError, match="ship_treatment can never run without the owner's tap"):
        ct.Autonomy(without_confirmation=["ship_treatment"])
    with pytest.raises(ValidationError, match="ship_treatment can never"):
        ct.Autonomy(without_confirmation=["keep_control", "ship_treatment"])
    text = ct.to_toml(make_contract(keys, autonomy=KEEP)).replace('"keep_control"', '"ship_treatment"')
    with pytest.raises(ValidationError, match="ship_treatment can never"):
        ct.from_toml(text)


def test_unknown_or_empty_autonomy_is_refused():
    with pytest.raises(ValidationError, match="unknown action 'stop_everything'"):
        ct.Autonomy(without_confirmation=["stop_everything"])
    with pytest.raises(ValidationError, match="at least one action"):
        ct.Autonomy(without_confirmation=[])
    with pytest.raises(ValidationError, match="twice"):
        ct.Autonomy(without_confirmation=["keep_control", "keep_control"])
    with pytest.raises(ValidationError):
        ct.Autonomy.model_validate({"without_confirmation": ["keep_control"], "also": True})


def test_autonomy_text_in_the_contract_summary(world, keys):
    svc, _, _ = world
    h = svc.submit(make_contract(keys, autonomy=KEEP))["hash"]
    s = summarise(svc.contract(h))
    assert s["autonomy"] == {"without_confirmation": ["keep_control"],
                             "outcomes": ["guardrail_breach", "loss", "horizon"]}
    assert s["autonomy_text"] == ("The agent may keep control on its own when a guardrail breaks, the experiment "
                                  "loses or the experiment reaches its horizon without a decision; shipping always "
                                  "waits for maya's tap.")
    plain = summarise(make_contract(keys))
    assert plain["autonomy"] == {"without_confirmation": [], "outcomes": []}
    assert "waits for maya's signed tap" in plain["autonomy_text"]
    assert autonomy_text(make_contract(keys)) == plain["autonomy_text"]


def test_autonomy_clause_on_an_unsigned_contract_cannot_start(world, keys):
    svc, fs, _ = world
    h = svc.submit(make_contract(keys, autonomy=KEEP))["hash"]
    with pytest.raises(Refused):
        svc.start_under_contract(h)
    with pytest.raises(Refused):
        svc.execute_for_contract(h, "keep_control")
    assert fs.experiments[1].status == "created"


# ---- the service --------------------------------------------------------------------------


def test_guardrail_breach_keeps_control_without_a_tap(world, keys):
    svc, fs, store = world
    h = started(svc, keys, autonomy=KEEP)
    out = breach(svc, fs, h)
    p = out["proposal"]
    assert p["authorised_by"] == "signed_autonomy" and p["state"] == "confirmed" and not p["needs_tap"]
    assert p["authorised_text"] == "authorised in advance by clause autonomy, signed by maya"
    assert "Signed autonomy" in card_text(out)
    res = svc.execute_for_contract(h, "keep_control")
    assert res["state"] == "executed" and res["authorised_by"] == "signed_autonomy"
    assert res["result"]["authorised_by"] == "signed_autonomy"
    assert res["result"]["authorised_text"].startswith("authorised in advance by clause autonomy")
    assert fs.experiments[1].status == "completed" and fs.served["checkout_button"] == "control"
    assert "under the autonomy clause the owner signed (no tap needed)" in fs.experiments[1].hypothesis
    assert store.proposal(p["id"])["confirm_signature"] is None
    # Idempotent: a second execute sends nothing more.
    assert svc.execute(p["id"])["state"] == "executed"
    rep = svc.audit_replay(1)
    done = [r for r in rep["rows"] if r["kind"] == "complete"][0]
    assert done["verdict"] == "signed" and done["authorised_by"] == "signed_autonomy"


def test_without_the_clause_keep_control_still_waits_for_the_tap(world, keys):
    svc, fs, _ = world
    h = started(svc, keys)
    out = breach(svc, fs, h)
    assert out["proposal"]["authorised_by"] == "owner_tap" and out["proposal"]["needs_tap"]
    with pytest.raises(Refused, match="one-tap"):
        svc.execute_for_contract(h, "keep_control")
    res = svc.confirm(out["proposal"]["id"], "maya", tap(svc, keys, out["proposal"]))
    assert res["state"] == "executed" and res["result"]["authorised_by"] == "owner_tap"


def test_win_still_waits_for_the_tap_under_autonomy(world, keys):
    svc, fs, _ = world
    h = started(svc, keys, autonomy=KEEP)
    fs.set_results(1, 400, 402, PHI(4.0))
    out = svc.look(h)
    assert out["decision"]["outcome"] == "win"
    assert out["proposal"]["authorised_by"] == "owner_tap" and out["proposal"]["state"] == "pending"
    with pytest.raises(Refused, match="one-tap"):
        svc.execute_for_contract(h, "ship_treatment")
    assert fs.served == {}
    svc.confirm(out["proposal"]["id"], "maya", tap(svc, keys, out["proposal"]))
    assert fs.served["checkout_button"] == "big_cta"


def test_wrong_action_is_refused_under_autonomy(world, keys):
    svc, fs, store = world
    h = started(svc, keys, autonomy=KEEP)
    breach(svc, fs, h)
    with pytest.raises(Refused, match="authorises 'keep_control'"):
        svc.execute_for_contract(h, "ship_treatment")
    assert fs.experiments[1].status == "running" and fs.served == {}
    assert any("asked for ship_treatment" in r["reason"] for r in store.refusals())


def test_store_edit_marking_a_ship_autonomous_is_refused(world, keys):
    svc, fs, store = world
    h = started(svc, keys, autonomy=KEEP)
    fs.set_results(1, 400, 402, PHI(4.0))
    out = svc.look(h)
    store.update_proposal(out["proposal"]["id"], 0.0, state="confirmed", authorised_by="autonomy")
    with pytest.raises(Refused, match="without the owner's tap"):
        svc.execute(out["proposal"]["id"])
    assert fs.served == {}


def test_store_edit_marking_keep_control_autonomous_without_the_clause_is_refused(world, keys):
    svc, fs, store = world
    h = started(svc, keys)
    out = breach(svc, fs, h)
    store.update_proposal(out["proposal"]["id"], 0.0, state="confirmed", authorised_by="autonomy")
    with pytest.raises(Refused, match="without the owner's tap"):
        svc.execute(out["proposal"]["id"])
    assert fs.experiments[1].status == "running"


def test_amendment_adding_ship_treatment_to_autonomy_is_refused(world, keys):
    svc, _, _ = world
    h = started(svc, keys)
    with pytest.raises(Invalid, match="does not validate") as exc:
        svc.amend(h, {"autonomy": {"without_confirmation": ["keep_control", "ship_treatment"]}},
                  reason="ship by itself at night")
    assert "ship_treatment can never" in json.dumps(exc.value.to_dict())


def test_adding_autonomy_needs_both_signers(world, keys, clock):
    svc, fs, _ = world
    h = started(svc, keys)
    out = svc.amend(h, {"autonomy": {"without_confirmation": ["keep_control"]}},
                    reason="stop a harmful variation at night without waking anyone")
    h2 = out["hash"]
    sign_as(svc, keys, h2, "maya", "owner")
    assert svc.contract_status(h2)["state"] == "draft"
    with pytest.raises(Refused, match="both the owner and the second signer"):
        svc.execute_for_contract(h2, "keep_control")
    sign_as(svc, keys, h2, "sam", "second_signer")
    assert svc.contract_status(h2)["state"] == "active"
    look = breach(svc, fs, h2)
    assert look["proposal"]["authorised_by"] == "signed_autonomy"
    assert svc.execute_for_contract(h2, "keep_control")["state"] == "executed"


# ---- verify ---------------------------------------------------------------------------------


def _bundle(world, keys):
    svc, fs, store = world
    h = started(svc, keys, autonomy=KEEP)
    breach(svc, fs, h)
    svc.execute_for_contract(h, "keep_control")
    return h, vf.export_bundle(store, "env-key", 1, flagsmith=vf.flagsmith_state(fs, "env-key", 1))


def test_verify_passes_an_autonomous_keep_control(world, keys):
    h, bundle = _bundle(world, keys)
    assert bundle["outcomes"][0]["authorised_by"] == "signed_autonomy"
    checks = vf.verify_bundle(bundle, trusted_keys={"maya": keys["maya"].public, "sam": keys["sam"].public})
    fails = [c.line() for c in checks if c.status == "FAIL"]
    assert not fails, fails
    auto = [c for c in checks if c.name.endswith(" autonomy")]
    assert auto and auto[0].status == "PASS" and "clause autonomy" in auto[0].reason
    assert not [c for c in checks if c.name.endswith(" confirmation")]
    assert vf.summary(checks)[1] == 0


def test_verify_fails_an_autonomous_execution_the_clause_does_not_list(world, keys):
    svc, fs, store = world
    h = started(svc, keys)  # no autonomy clause
    out = breach(svc, fs, h)
    svc.confirm(out["proposal"]["id"], "maya", tap(svc, keys, out["proposal"]))
    bundle = vf.export_bundle(store, "env-key", 1, flagsmith=vf.flagsmith_state(fs, "env-key", 1))
    assert vf.summary(vf.verify_bundle(bundle, expect_hash=h))[1] == 0
    forged = copy.deepcopy(bundle)
    forged["outcomes"][0].update(authorised_by="signed_autonomy", confirm_signer=None, confirm_signature=None)
    checks = vf.verify_bundle(forged, expect_hash=h)
    bad = [c for c in checks if c.name.endswith(" autonomy")]
    assert bad and bad[0].status == "FAIL" and "lists nothing" in bad[0].reason
    assert vf.summary(checks)[1] == 1


def test_verify_fails_an_autonomous_ship(world, keys):
    _, bundle = _bundle(world, keys)
    forged = copy.deepcopy(bundle)
    forged["outcomes"][0]["action"] = "ship_treatment"
    checks = vf.verify_bundle(forged)
    bad = [c for c in checks if c.name.endswith(" autonomy")]
    assert bad and bad[0].status == "FAIL" and "shipping always needs the tap" in bad[0].reason


def test_verify_rejects_an_unknown_authorisation(world, keys):
    _, bundle = _bundle(world, keys)
    bundle["outcomes"][0]["authorised_by"] = "the agent"
    checks = vf.verify_bundle(bundle)
    assert checks[0].status == "FAIL" and "authorised_by" in checks[0].reason


# ---- replay ---------------------------------------------------------------------------------


def test_replay_reproduces_an_autonomous_run(tmp_path, keys):
    clock = Clock(T0)
    fs = FakeFlagsmith(clock=clock)
    fs.add_experiment(1)
    db = tmp_path / "run" / "judge.db"
    svc = Ulysses(Store(db), fs, Settings(public_url="http://test", refresh_wait_seconds=0, poll_seconds=0),
                  clock=clock, sleep=lambda s: clock.advance(s))
    svc.register_signer("maya", keys["maya"].public)
    svc.register_signer("sam", keys["sam"].public)
    h = svc.submit(make_contract(keys, n=1000, looks=10, autonomy=KEEP))["hash"]
    clock.advance(3)
    sign_as(svc, keys, h)
    clock.advance(4)
    svc.start_under_contract(h)
    clock.advance(400)
    breach(svc, fs, h)
    clock.advance(2)
    assert svc.execute_for_contract(h, "keep_control")["state"] == "executed"
    lines = export_store(db, title="autonomy run", flagsmith="the test double of Flagsmith")
    assert lines[0]["counts"]["autonomous_execute"] == 1 and lines[0]["counts"]["confirm"] == 0
    prop = next(e for e in lines if e.get("event") == "proposal")
    assert prop["authorised_by"] == "signed_autonomy"
    path = tmp_path / "t.jsonl"
    write_trace(lines, path)
    header, events = load_trace(path)
    r = Replayer(header, events, tmp_path / "replay", public_url="http://127.0.0.1:1", speed=10.0, base=T0 + 10**6)
    steps = list(r.steps())
    assert r.mismatches == [], [s.text for s in r.mismatches]
    auto = [s for s in steps if s.kind == "autonomous_execute"]
    assert auto and "signed autonomy clause, no tap" in auto[0].text
    assert r.fs.served["checkout_button"] == "control"
    # A recording that claims a tap-free run of a contract without the clause does not reproduce.
    plain = [dict(e) for e in events]
    for e in plain:
        if e.get("event") == "contract":
            e["canonical"] = {k: v for k, v in e["canonical"].items() if k != "autonomy"}
    r2 = Replayer(header, plain, tmp_path / "replay2", public_url="http://127.0.0.1:1", speed=10.0, base=T0 + 10**6)
    list(r2.steps())
    assert r2.mismatches


# ---- MCP ------------------------------------------------------------------------------------


def test_mcp_execute_says_it_ran_under_signed_autonomy(world, keys):
    from fastapi.testclient import TestClient

    from ulysses.mcp_server import ServiceClient, build_server
    from ulysses.service.app import create_app

    from .test_mcp import _result
    from .test_service_http import FS_KEY, TOKEN

    svc, fs, _ = world
    app = create_app(svc, agent_token=TOKEN, protected_secrets=[FS_KEY])
    client = ServiceClient(base_url="http://testserver", token=TOKEN)
    client.http = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
    server = build_server(client)

    def call(name: str, **args) -> dict:
        return _result(anyio.run(lambda: server.call_tool(name, args)))

    listed = anyio.run(server.list_tools)
    desc = next(t for t in listed if t.name == "execute").description
    assert "Signed autonomy" in desc and "ship_treatment always waits" in desc
    fs.rate[1] = 50.0
    refused = call("sign_contract", environment="env-key", experiment_id=1, owner="maya", second_signer="sam",
                   min_relative_lift=0.3, baseline_rate=0.1, without_confirmation=["ship_treatment"])
    assert not refused["ok"] and "ship_treatment can never" in json.dumps(refused)
    drafted = call("sign_contract", environment="env-key", experiment_id=1, owner="maya", second_signer="sam",
                   min_relative_lift=0.3, baseline_rate=0.1, without_confirmation=["keep_control"])
    assert drafted["ok"], drafted
    h = drafted["hash"]
    assert "keep control on its own" in drafted["summary"]["autonomy_text"]
    sign_as(svc, keys, h)
    assert call("start_under_contract", contract_hash=h)["ok"]
    fs.set_results(1, 120, 118, 0.99, guardrails={"refunds": 0.00001})
    fire = call("look", contract_hash=h)
    assert fire["decision"]["outcome"] == "guardrail_breach", fire
    assert fire["proposal"]["authorised_by"] == "signed_autonomy" and "call execute now" in fire["card"]
    assert not fire.get("links_offered")
    wrong = call("execute", contract_hash=h, action="ship_treatment")
    assert not wrong["ok"] and wrong["status"] == 403
    done = call("execute", contract_hash=h, action="keep_control")
    assert done["ok"] and done["state"] == "executed" and done["authorised_by"] == "signed_autonomy"
    assert done["authorised"].startswith("Ran 'keep control' under signed autonomy")
    assert "No tap was needed; flag serves control." in done["authorised"]
