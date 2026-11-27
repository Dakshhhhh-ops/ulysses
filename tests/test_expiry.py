"""Contract expiry: ``[validity] expires_after`` is a signed duration counted from the experiment's start.

A look after it with no decision fires ``expired`` (no decision, keep control). It never ships. Contracts
without the clause keep their hash and never expire. Every experiment here runs over the test double of
Flagsmith; the clock is synthetic.
"""

from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ulysses import cli
from ulysses import contract as ct
from ulysses import verify as vf
from ulysses.core import Refused
from ulysses.decision import FIRES, Observation, evaluate
from ulysses.mcp_server import card_text
from ulysses.service.app import create_app

from .conftest import make_contract, read_all, sign_as, tap

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400
EXPIRY = ct.Validity(expires_after="14d")
KEEP = ct.Autonomy(without_confirmation=["keep_control"])


def started(svc, keys, **kw) -> str:
    h = svc.submit(make_contract(keys, n=1000, looks=10, validity=EXPIRY, **kw))["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    return h


# ---- the clause -----------------------------------------------------------------------------


def test_expiry_is_optional_and_absent_contracts_hash_as_before(keys):
    plain = make_contract(keys)
    assert plain.validity.expires_after is None
    assert "expires_after" not in json.dumps(plain.canonical())
    assert "expires_after" not in ct.to_toml(plain)
    with_expiry = make_contract(keys, validity=EXPIRY)
    assert with_expiry.hash != plain.hash  # the clause is signed: it is in the hash
    assert ct.from_toml(ct.to_toml(with_expiry)).hash == with_expiry.hash
    assert 'expires_after = "14d"' in ct.to_toml(with_expiry)


def test_recorded_runs_still_hash_and_carry_no_expiry():
    for name in ("verify-judge-demo-cta.json", "verify-tempted-cta.json"):
        for entry in vf.load_bundle(ROOT / "data" / name)["contracts"]:
            c = ct.from_toml(entry["toml"])
            assert c.hash == entry["hash"] and c.validity.expires_after is None
    for name in ("replay-judge-demo.jsonl", "replay-tempted.jsonl"):
        for line in (ROOT / "data" / name).read_text(encoding="utf-8").splitlines():
            ev = json.loads(line)
            if ev.get("event") == "contract":
                assert ct.from_canonical(ev["canonical"]).hash == ev["hash"]


@pytest.mark.parametrize("text, seconds", [("14d", 14 * DAY), ("36h", 36 * 3600), ("1h", 3600), ("365d", 365 * DAY)])
def test_durations_in_days_or_hours(text, seconds):
    assert ct.Validity(expires_after=text).expires_after_seconds == seconds


@pytest.mark.parametrize("bad", ["", "14", "2w", "0d", "-3d", "1.5d", "366d", "9000h", "2001-02-03", "14 d"])
def test_bad_durations_are_rejected(bad):
    with pytest.raises(ValidationError, match="expires_after"):
        ct.Validity(expires_after=bad)


def test_duration_words():
    assert ct.duration_words("1d") == "1 day"
    assert ct.duration_words("14d") == "14 days"
    assert ct.duration_words("36h") == "36 hours"


def test_init_template_signs_a_default_expiry(tmp_path):
    from argparse import Namespace

    cli.cmd_init(Namespace(dir=str(tmp_path), flagsmith_url="http://localhost:8000", port=8200,
                           with_flagsmith_mcp=False, uvx=None))
    data = tomllib.loads((tmp_path / "ulysses.contract.example.toml").read_text(encoding="utf-8"))
    assert data["validity"]["expires_after"] == "30d"
    assert data["ulysses_contract"] == ct.SCHEMA_VERSION
    ct.Validity.model_validate(data["validity"])


# ---- the rule -------------------------------------------------------------------------------


def _hold_obs() -> Observation:
    return Observation(100, 100, 0.9, {"refunds": 0.5})  # look 1 of 10, far from any boundary


def test_rule_without_time_or_clause_is_unchanged(keys):
    c = make_contract(keys, validity=EXPIRY)
    plain = make_contract(keys)
    base = evaluate(plain, [], _hold_obs())
    assert evaluate(c, [], _hold_obs()).outcome == base.outcome == "hold"
    assert evaluate(plain, [], _hold_obs(), 100 * DAY).to_dict() == base.to_dict()
    assert "expiry" not in base.to_dict()


def test_hold_before_expiry_records_the_time_and_holds(keys):
    d = evaluate(make_contract(keys, validity=EXPIRY), [], _hold_obs(), 13 * DAY)
    assert d.outcome == "hold" and not d.fires
    assert d.expiry == {"expires_after": "14d", "expires_after_seconds": 14 * DAY,
                        "elapsed_seconds": 13 * DAY, "expired": False}


@pytest.mark.parametrize("obs, was", [
    (_hold_obs(), "hold"),
    (Observation(50, 50, 0.9, {"refunds": 0.5}), "watch"),       # between looks
    (Observation(100, 100, None, {"refunds": None}), "not_ready"),  # no Flagsmith result yet
])
def test_no_decision_after_expiry_fires_expired_and_keeps_control(keys, obs, was):
    c = make_contract(keys, validity=EXPIRY, actions=ct.Actions(horizon="ship_treatment"))
    assert evaluate(c, [], obs).outcome == was
    d = evaluate(c, [], obs, 14 * DAY)
    assert "expired" in FIRES
    assert d.outcome == "expired" and d.fires
    assert d.action == "keep_control"  # never ships, even when the horizon is signed to ship
    assert d.expiry["expired"] and d.next_look_at_identities is None
    assert "Expired" in d.reason and "keep control" in d.reason


def test_a_decision_at_the_expired_look_stands(keys):
    c = make_contract(keys, validity=EXPIRY)
    obs = Observation(500, 500, 0.5, {"refunds": 1 - 1e-12})  # refunds surely worse at look 5
    assert evaluate(c, [], obs).outcome == "guardrail_breach"
    guard = evaluate(c, [], obs, 20 * DAY)
    assert guard.outcome == "guardrail_breach" and guard.expiry["expired"]
    final = evaluate(c, [], Observation(1000, 1000, 0.6, {"refunds": 0.5}), 20 * DAY)
    assert final.outcome == "horizon"


def test_contract_action_for_expired(keys):
    assert make_contract(keys, validity=EXPIRY).action_for("expired") == "keep_control"
    assert make_contract(keys).action_for("expired") is None
    assert "expired" in make_contract(keys, validity=EXPIRY, autonomy=KEEP).autonomous_outcomes()
    assert "expired" not in make_contract(keys, autonomy=KEEP).autonomous_outcomes()


# ---- the service ----------------------------------------------------------------------------


def test_a_look_after_expiry_fires_expired_and_executes_keep_control(world, keys, clock):
    svc, fs, store = world
    h = started(svc, keys)
    fs.set_results(1, 100, 100, 0.97, guardrails={"refunds": 0.5})
    first = svc.look(h)
    assert first["decision"]["outcome"] == "hold"
    assert first["decision"]["expiry"]["elapsed_seconds"] == 0
    clock.advance(14 * DAY + 60)
    fs.set_results(1, 150, 150, 0.98, guardrails={"refunds": 0.5})  # between looks: no number shown
    out = svc.look(h)
    d = out["decision"]
    assert d["outcome"] == "expired" and d["action"] == "keep_control" and d["counted"] is False
    assert out["proposal"]["outcome"] == "expired" and out["proposal"]["state"] == "pending"
    assert "EXPIRED" in card_text(out)
    assert svc.look(h)["already_decided"]
    with pytest.raises(Refused):
        svc.execute_for_contract(h, "ship_treatment")
    svc.confirm(out["proposal"]["id"], "maya", tap(svc, keys, out["proposal"]))
    done = svc.execute_for_contract(h, "keep_control")
    assert done["state"] == "executed"
    assert fs.get_experiment("env-key", 1).status == "completed"


def test_expired_keep_control_runs_under_signed_autonomy(world, keys, clock):
    svc, fs, _ = world
    h = started(svc, keys, autonomy=KEEP)
    clock.advance(15 * DAY)
    fs.set_results(1, 100, 100, 0.9, guardrails={"refunds": 0.5})
    out = svc.look(h)
    assert out["decision"]["outcome"] == "expired"
    assert out["proposal"]["authorised_by"] == "signed_autonomy"
    assert svc.execute_for_contract(h, "keep_control")["state"] == "executed"


def test_status_shows_time_left_from_the_start(world, keys, clock):
    svc, _, _ = world
    h = started(svc, keys)
    clock.advance(2 * DAY)
    st = svc.contract_status(h)
    assert st["expiry"]["counted_from"] == "the experiment's start"
    assert st["expiry"]["expires_in_hours"] == 12 * 24 and not st["expiry"]["expired"]
    assert "14 days after the experiment starts" in st["summary"]["expiry"]["text"]


def test_starting_under_an_already_expired_contract_is_refused(world, keys, clock):
    svc, _, store = world
    c = make_contract(keys, validity=ct.Validity(start_within_days=30, expires_after="36h"))
    h = svc.submit(c)["hash"]
    sign_as(svc, keys, h)
    clock.advance(36 * 3600 + 1)
    st = svc.contract_status(h)
    assert st["expired"] and st["expiry"]["expired"]
    with pytest.raises(Refused, match="expired"):
        svc.start_under_contract(h)
    assert svc.contract_status(h)["state"] == "signed"
    assert not [a for a in store.actions("env-key", 1) if a["kind"] == "start"]


def test_contract_without_expiry_never_expires(world, keys, clock):
    svc, fs, _ = world
    h = svc.submit(make_contract(keys, n=1000, looks=10))["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    clock.advance(400 * DAY)
    fs.set_results(1, 100, 100, 0.9, guardrails={"refunds": 0.5})
    out = svc.look(h)
    assert out["decision"]["outcome"] == "hold" and "expiry" not in out["decision"]
    assert svc.contract_status(h)["expiry"] is None


# ---- pages ----------------------------------------------------------------------------------


def test_signing_page_and_card_say_it_in_plain_words(world, keys, clock, monkeypatch, tmp_path):
    svc, fs, _ = world
    monkeypatch.setenv("ULYSSES_CALIBRATION_DIR", str(tmp_path / "none"))
    client = read_all(TestClient(create_app(svc, agent_token="t" * 20)), svc)
    h = started(svc, keys)
    page = client.get(f"/sign/{h}").text
    assert "expires 14 days after the experiment starts" in page
    assert "Expiry with no decision: keep control" in page
    plain = svc.submit(make_contract(keys, experiment=1, n=1200))["hash"]
    assert "has no expiry" in client.get(f"/sign/{plain}").text
    clock.advance(14 * DAY)
    fs.set_results(1, 100, 100, 0.9, guardrails={"refunds": 0.5})
    out = svc.look(h)
    read_all(client, svc)  # the read link minted fourteen days ago has expired
    card = client.get(f"/card/{out['look_id']}")
    assert card.status_code == 200
    assert "expired 14d after the experiment started" in card.text
    assert "EXPIRED: NO DECISION, KEEP CONTROL" in card.text or "Expired" in card.text


# ---- verify ---------------------------------------------------------------------------------


def _expired_bundle(world, keys, clock):
    svc, fs, store = world
    h = started(svc, keys)
    fs.set_results(1, 100, 100, 0.97, guardrails={"refunds": 0.5})
    svc.look(h)
    clock.advance(14 * DAY + 5)
    fs.set_results(1, 150, 150, 0.9, guardrails={"refunds": 0.5})
    out = svc.look(h)
    svc.confirm(out["proposal"]["id"], "maya", tap(svc, keys, out["proposal"]))
    svc.execute_for_contract(h, "keep_control")
    return h, vf.export_bundle(store, "env-key", 1, flagsmith=vf.flagsmith_state(fs, "env-key", 1))


def test_verify_rechecks_an_expired_outcome_offline(world, keys, clock):
    h, bundle = _expired_bundle(world, keys, clock)
    assert vf.find_timestamps(bundle) == []
    assert bundle["looks"][-1]["recorded"]["expiry"]["expired"] is True
    checks = vf.verify_bundle(bundle, expect_hash=h)
    fails = [c.line() for c in checks if c.status == "FAIL"]
    assert not fails, fails
    assert any("past the signed expiry" in c.reason for c in checks if c.status == "PASS")
    assert any(c.name.endswith(" action") and "expired" in c.reason for c in checks if c.status == "PASS")


def test_verify_fails_a_moved_clock(world, keys, clock):
    h, bundle = _expired_bundle(world, keys, clock)
    early = copy.deepcopy(bundle)
    early["looks"][-1]["recorded"]["expiry"]["elapsed_seconds"] = 3 * DAY  # not yet expired: the rule holds
    checks = vf.verify_bundle(early, expect_hash=h)
    assert any(c.status == "FAIL" and "the signed rule gives" in c.reason for c in checks)
    dropped = copy.deepcopy(bundle)
    del dropped["looks"][-1]["recorded"]["expiry"]
    checks = vf.verify_bundle(dropped, expect_hash=h)
    assert any(c.status == "FAIL" and "records no time since the start" in c.reason for c in checks)
    backwards = copy.deepcopy(bundle)
    backwards["looks"][0]["recorded"]["expiry"]["elapsed_seconds"] = 20 * DAY
    checks = vf.verify_bundle(backwards, expect_hash=h)
    assert any(c.status == "FAIL" and "runs backwards" in c.reason for c in checks)
