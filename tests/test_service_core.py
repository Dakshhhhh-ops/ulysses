"""The authorisation boundary, exercised through the service core with a fake Flagsmith."""

import json
from statistics import NormalDist

import pytest

from ulysses import contract as ct
from ulysses import signing
from ulysses.canonical import content_hash
from ulysses.core import Conflict, Invalid, Refused
from ulysses.flagsmith.gateway import AuditEntry, FlagsmithError

from .conftest import make_contract, sign_as

PHI = NormalDist().cdf


def submit(svc, keys, **kw):
    return svc.submit(make_contract(keys, **kw))["hash"]


def confirm(svc, keys, look_or_pid_view, who="maya"):
    p = look_or_pid_view["proposal"]
    msg = signing.confirm_message(p["contract"], p["look_id"] or p["id"], p["action"])
    return svc.confirm(p["id"], who, keys[who].sign(msg))


# ---- signing and starting ---------------------------------------------------------


def test_unsigned_contract_cannot_start(world, keys):
    svc, fs, store = world
    h = submit(svc, keys)
    with pytest.raises(Refused, match="no valid owner signature"):
        svc.start_under_contract(h)
    assert fs.writes == []
    assert fs.experiments[1].status == "created"
    assert store.refusals()[-1]["what"] == "start"


def test_signed_contract_starts_and_writes_hash_into_hypothesis(world, keys):
    svc, fs, _ = world
    h = submit(svc, keys)
    sign_as(svc, keys, h)
    out = svc.start_under_contract(h)
    assert out["state"] == "active"
    assert fs.experiments[1].status == "running"
    assert h in fs.experiments[1].hypothesis
    assert [w[0] for w in fs.writes] == ["update_hypothesis", "start"]


def test_signature_by_the_wrong_person_is_refused(world, keys):
    svc, fs, _ = world
    h = submit(svc, keys)
    forged = keys["mallory"].sign(signing.contract_message(h, "owner"))
    with pytest.raises(Refused, match="signature rejected"):
        svc.sign(h, "owner", "maya", forged)
    with pytest.raises(Refused, match="not 'sam'"):
        svc.sign(h, "owner", "sam", keys["sam"].sign(signing.contract_message(h, "owner")))
    with pytest.raises(Refused):
        svc.start_under_contract(h)


def test_contract_naming_an_unregistered_key_is_rejected(world, keys):
    svc, _, _ = world
    c = make_contract(keys)
    data = c.canonical()
    data["owner"]["public_key"] = keys["mallory"].public
    with pytest.raises(Invalid, match="registered key"):
        svc.submit(ct.from_canonical(data))


def test_tampered_stored_contract_fails_its_hash(world, keys):
    svc, fs, store = world
    h = submit(svc, keys)
    sign_as(svc, keys, h)
    # Someone edits the stored contract to make the final boundary easier.
    row = store.contract_row(h)
    data = row["canonical"]
    data["actions"]["horizon"] = "ship_treatment"
    store._exec("UPDATE contracts SET canonical=? WHERE hash=?", (json.dumps(data), h))
    with pytest.raises(Refused, match="does not match its hash"):
        svc.start_under_contract(h)
    assert fs.writes == []


def test_tampered_contract_file_gets_a_new_hash_and_no_signature(world, keys):
    svc, fs, _ = world
    c = make_contract(keys)
    h = svc.submit(c)["hash"]
    sign_as(svc, keys, h)
    text = ct.to_toml(c).replace('horizon = "keep_control"', 'horizon = "ship_treatment"')
    h2 = svc.submit(text)["hash"]
    assert h2 != h and h2 == content_hash(ct.from_toml(text).canonical())
    with pytest.raises(Refused, match="no valid owner signature"):
        svc.start_under_contract(h2)


def test_expired_contract_cannot_start(world, keys, clock):
    svc, fs, _ = world
    h = svc.submit(make_contract(keys, validity=ct.Validity(start_within_days=1)))["hash"]
    sign_as(svc, keys, h)
    clock.advance(2 * 86400)
    with pytest.raises(Refused, match="within 1 days"):
        svc.start_under_contract(h)
    assert fs.writes == []


def test_contract_must_match_the_flagsmith_experiment(world, keys):
    svc, fs, _ = world
    c = make_contract(keys)
    data = c.canonical()
    data["experiment"]["treatment"] = "nonexistent"
    with pytest.raises(Invalid, match="no variation keyed"):
        svc.submit(ct.from_canonical(data))
    data = c.canonical()
    data["primary_metric"]["name"] = "refunds"
    data["guardrails"] = []
    with pytest.raises(Invalid, match="guardrail in Flagsmith"):
        svc.submit(ct.with_regenerated_table(data))


def test_cannot_start_an_already_running_experiment(world, keys):
    svc, fs, _ = world
    fs.start("env-key", 1)
    fs.writes.clear()
    h = submit(svc, keys)
    sign_as(svc, keys, h)
    with pytest.raises(Conflict, match="running"):
        svc.start_under_contract(h)
    assert fs.writes == []


# ---- looks and execution -------------------------------------------------------------


def started(svc, keys, **kw):
    h = submit(svc, keys, **kw)
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    return h


def test_hold_then_win_then_one_tap_ships_on_the_same_flag(world, keys, clock):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 300, 305, 0.97)  # the banner would say Winning
    out = svc.look(h)
    assert out["decision"]["outcome"] == "hold"
    assert fs.served == {}
    clock.advance(301)
    fs.set_results(1, 400, 402, PHI(4.0))
    out = svc.look(h)
    assert out["decision"]["outcome"] == "win"
    assert out["proposal"]["action"] == "ship_treatment" and out["proposal"]["state"] == "pending"
    # The agent cannot execute before the owner's tap.
    with pytest.raises(Refused, match="one-tap"):
        svc.execute(out["proposal"]["id"])
    res = confirm(svc, keys, out)
    assert res["state"] == "executed"
    assert fs.experiments[1].status == "completed"
    assert fs.served["checkout_button"] == "big_cta"
    assert "fired: win" in fs.experiments[1].hypothesis


def test_action_outside_the_contract_is_refused(world, keys, clock):
    svc, fs, store = world
    h = started(svc, keys, n=1000, looks=10)
    # No look has fired: nothing authorises shipping.
    with pytest.raises(Refused, match="nothing authorises"):
        svc.execute_for_contract(h, "ship_treatment")
    fs.set_results(1, 1000, 1000, 0.96)  # horizon: banner says Winning, contract says no decision
    out = svc.look(h)
    assert out["decision"]["outcome"] == "horizon"
    assert out["proposal"]["action"] == "keep_control"
    confirm_needed = out["proposal"]["id"]
    with pytest.raises(Refused, match="authorises 'keep_control'"):
        svc.execute(confirm_needed, requested_action="ship_treatment")
    assert fs.served == {}
    assert any(r["what"] == "execute" for r in store.refusals())


def test_confirm_must_be_signed_by_the_owner(world, keys):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 1000, 1000, PHI(-3))
    out = svc.look(h)
    p = out["proposal"]
    msg = signing.confirm_message(h, p["look_id"], p["action"])
    with pytest.raises(Refused, match="only the owner"):
        svc.confirm(p["id"], "sam", keys["sam"].sign(msg))
    with pytest.raises(Refused, match="rejected"):
        svc.confirm(p["id"], "maya", keys["mallory"].sign(msg))
    # A confirmation for a different action does not verify.
    wrong = signing.confirm_message(h, p["look_id"], "ship_treatment")
    with pytest.raises(Refused):
        svc.confirm(p["id"], "maya", keys["maya"].sign(wrong))
    assert fs.experiments[1].status == "running"


def test_guardrail_breach_stops_and_keeps_control(world, keys):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 120, 118, 0.99, guardrails={"refunds": 0.005})  # refunds very likely up
    out = svc.look(h)
    assert out["decision"]["outcome"] == "guardrail_breach"
    confirm(svc, keys, out)
    assert fs.served["checkout_button"] == "control"


def test_refresh_floor_is_respected(world, keys, clock):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 50, 50, 0.5)
    svc.look(h)
    out = svc.look(h)
    assert out.get("too_soon") and out["retry_after_seconds"] == 300
    assert fs.refresh_calls == 1
    clock.advance(300)
    svc.look(h)
    assert fs.refresh_calls == 2


def test_looks_after_a_fire_do_not_reopen_the_decision(world, keys, clock):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 1000, 1000, 0.5)
    svc.look(h)
    clock.advance(600)
    fs.set_results(1, 1100, 1100, PHI(5))
    again = svc.look(h)
    assert again["already_decided"] and again["proposals"][0]["outcome"] == "horizon"


# ---- amendments ----------------------------------------------------------------------


def test_amendment_needs_both_signers_and_is_written_on_the_experiment(world, keys, clock):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 300, 300, 0.97)
    svc.look(h)
    out = svc.amend(h, {"horizon": {"identities_per_arm": 2000}}, reason="traffic lower than planned")
    h2 = out["hash"]
    sign_as(svc, keys, h2, "maya", "owner")
    assert svc.contract_status(h2)["state"] == "draft"  # owner alone is not enough
    sign_as(svc, keys, h2, "sam", "second_signer")
    assert svc.contract_status(h2)["state"] == "active"
    assert svc.contract_status(h)["state"] == "superseded"
    assert f"Amendment {h2}" in fs.experiments[1].hypothesis
    # Looks continue under the amendment with the new horizon.
    clock.advance(300)
    fs.set_results(1, 400, 400, 0.6)
    look = svc.look(h2)
    assert look["decision"]["fraction"] == pytest.approx(0.2)
    with pytest.raises(Refused):
        svc.look(h)


def test_ship_early_amendment_executes_only_after_both_signatures_and_a_tap(world, keys):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    out = svc.amend(h, {}, reason="legal requires the new button now", action_now="ship_treatment")
    h2 = out["hash"]
    sign_as(svc, keys, h2, "maya", "owner")
    with pytest.raises(Refused):
        svc.execute_for_contract(h2, "ship_treatment")
    sign_as(svc, keys, h2, "sam", "second_signer")
    p = svc.store.proposals_for(h2)[0]
    with pytest.raises(Refused, match="one-tap"):
        svc.execute(p["id"])
    msg = signing.confirm_message(h2, p["id"], "ship_treatment")
    svc.confirm(p["id"], "maya", keys["maya"].sign(msg))
    assert fs.served["checkout_button"] == "big_cta"


def test_second_signer_cannot_be_swapped_by_amendment(world, keys):
    svc, fs, _ = world
    svc.register_signer("mallory", keys["mallory"].public)
    h = started(svc, keys)
    with pytest.raises(Invalid, match="cannot be amended"):
        svc.amend(h, {"second_signer": {"name": "mallory", "public_key": keys["mallory"].public}}, reason="x")


# ---- deviations and audit replay -------------------------------------------------------


def test_dashboard_completion_is_recorded_as_a_deviation(world, keys):
    svc, fs, store = world
    h = started(svc, keys, n=1000, looks=10)
    fs.dashboard_complete(1, user="pm@example.com", ship="big_cta")
    verdicts = svc.sync_audit(1)
    devs = store.deviations()
    # The completion itself, and the flag-state row the person's completion wrote on the governed flag.
    assert sorted(d["kind"] for d in devs) == ["complete", "flag_change"]
    assert all(d["author"] == "pm@example.com" for d in devs)
    assert "Deviation (audit webhook): complete by pm@example.com" in fs.experiments[1].hypothesis
    assert svc.contract_status(h)["state"] == "deviated"
    # Processing the same audit entries again changes nothing.
    again = svc.sync_audit(1)
    assert all(v.get("duplicate") or v["verdict"] in ("authorised", "ignored") for v in again)
    assert len(store.deviations()) == 2


def test_audit_replay_maps_every_lifecycle_change_to_a_signed_contract(world, keys):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 1000, 1000, PHI(3))
    out = svc.look(h)
    confirm(svc, keys, out)
    rep = svc.audit_replay(1)
    assert rep["governed_lifecycle_changes"] == 2
    assert rep["mapped_to_signed_contract"] == 2
    assert rep["deviations"] == 0
    done = [r for r in rep["rows"] if r["kind"] == "complete"][0]
    assert done["confirmed_by"] == "maya" and done["outcome"] == "win"


def test_api_key_change_not_made_by_the_service_is_a_deviation(world, keys):
    svc, fs, store = world
    started(svc, keys)
    e = AuditEntry(id=999, log="Experiment 'exp-1' paused for environment Test", author=None,
                   related_object_type="EXPERIMENT", related_object_id=1)
    # Flagsmith does not show a pause: the entry is recorded as unverified and nothing changes.
    assert svc.handle_audit(e)["verdict"] == "unverified"
    fs.experiments[1] = fs._transition(fs.experiments[1], "paused")
    e2 = AuditEntry(id=1000, log="Experiment 'exp-1' paused for environment Test", author=None,
                    related_object_type="EXPERIMENT", related_object_id=1)
    assert svc.handle_audit(e2)["verdict"] == "deviation"


def test_complete_whose_response_is_lost_is_reconciled_not_failed(world, keys):
    """Flagsmith applied the completion but the response timed out: read back, do not mark failed."""
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 1000, 1000, PHI(3.3))
    out = svc.look(h)
    fs.fail_complete_after_applying = True
    res = confirm(svc, keys, out)
    assert res["state"] == "executed" and res["result"]["flagsmith"]["reconciled"]
    assert fs.served["checkout_button"] == "big_cta"
    rep = svc.audit_replay(1)
    assert rep["mapped_to_signed_contract"] == 2 and rep["deviations"] == 0


def test_failed_execution_can_be_retried(world, keys, monkeypatch):
    svc, fs, store = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 1000, 1000, PHI(-3.3))
    out = svc.look(h)
    real = fs.complete

    def down(*a, **kw):
        raise FlagsmithError("cannot reach Flagsmith: connection refused")

    monkeypatch.setattr(fs, "complete", down)
    with pytest.raises(FlagsmithError):
        confirm(svc, keys, out)
    assert store.proposal(out["proposal"]["id"])["state"] == "failed"
    monkeypatch.setattr(fs, "complete", real)
    assert svc.execute(out["proposal"]["id"])["state"] == "executed"
    assert fs.served["checkout_button"] == "control"


def test_webhook_for_a_write_whose_response_was_lost_is_not_a_deviation(world, keys, monkeypatch):
    """complete landed, the response and the read-back both failed, then Flagsmith's webhook arrives."""
    svc, fs, store = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 1000, 1000, PHI(3.3))
    out = svc.look(h)
    fs.fail_complete_after_applying = True
    real_get = fs.get_experiment
    calls = {"n": 0}

    def flaky_get(env, eid):
        calls["n"] += 1
        if calls["n"] == 2:  # the read-back right after the lost response
            raise FlagsmithError("timed out")
        return real_get(env, eid)

    monkeypatch.setattr(fs, "get_experiment", flaky_get)
    with pytest.raises(FlagsmithError):
        confirm(svc, keys, out)
    verdicts = svc.sync_audit(1)
    assert not store.deviations()
    assert all(v["verdict"] != "deviation" for v in verdicts)
    monkeypatch.setattr(fs, "get_experiment", real_get)
    assert svc.execute(out["proposal"]["id"])["state"] == "executed"
