"""`ulysses verify`: a decision re-checked from a bundle alone, and each kind of tampering caught on its line.

Every experiment here runs over the test double of Flagsmith (``FakeFlagsmith``); nothing is live.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from statistics import NormalDist

import pytest

from ulysses import signing
from ulysses import verify as vf
from ulysses.cli import main
from ulysses.core import Settings, Ulysses
from ulysses.flagsmith.fake import FakeFlagsmith
from ulysses.store import Store

from .conftest import make_contract, sign_as, tap

PHI = NormalDist().cdf
ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "data" / "verify-judge-demo-cta.json"


def _confirm(svc, keys, view):
    """The owner signs exactly the text the card shows (it covers every look up to the one that fired)."""
    p = view["proposal"]
    msg = svc.proposal_card(p["id"])["confirm_message"].encode()
    return svc.confirm(p["id"], "maya", keys["maya"].sign(msg))


def _won(svc, fs, keys, clock) -> str:
    """Hold at look 3 (the banner would say Winning), then a win at look 4, confirmed and shipped."""
    h = svc.submit(make_contract(keys, n=1000, looks=10))["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    fs.set_results(1, 300, 305, 0.97, guardrails={"refunds": 0.4})
    assert svc.look(h)["decision"]["outcome"] == "hold"
    clock.advance(301)
    fs.set_results(1, 400, 402, PHI(4.0), guardrails={"refunds": 0.45})
    out = svc.look(h)
    assert out["decision"]["outcome"] == "win"
    assert _confirm(svc, keys, out)["state"] == "executed"
    return h


@pytest.fixture
def won(world, keys, clock):
    svc, fs, store = world
    h = _won(svc, fs, keys, clock)
    bundle = vf.export_bundle(store, "env-key", 1, flagsmith=vf.flagsmith_state(fs, "env-key", 1),
                              traffic={"kind": "fixture", "detail": "test double of Flagsmith"})
    return h, bundle


def _by_name(checks, name):
    """Checks called ``name``; a Flagsmith line in bundle mode carries a suffix saying it is the exporter's copy."""
    return [c for c in checks if c.name == name or c.name.startswith(name + " (")]


def _pinned(bundle, keys):
    """Every signer pinned with a key the auditor holds, so a clean bundle can come out VERIFIED."""
    return vf.verify_bundle(bundle, trusted_keys={"maya": keys["maya"].public, "sam": keys["sam"].public})


def _fails(checks):
    return [c for c in checks if c.status == "FAIL"]


def test_clean_bundle_passes_every_check(won, keys):
    h, bundle = won
    checks = _pinned(bundle, keys)
    assert not _fails(checks), [c.line() for c in _fails(checks)]
    assert not [c for c in checks if c.status == "SKIP"], [c.line() for c in checks if c.status == "SKIP"]
    text, code = vf.summary(checks)
    assert code == 0 and text.startswith("VERIFIED")
    names = {c.name for c in checks}
    assert {"contract 1 hash", f"contract {h[7:19]} owner signature", "trust anchor"} <= names
    for n in ("hypothesis in Flagsmith", "final state in Flagsmith", "outcome line in Flagsmith"):
        assert _by_name(checks, n)[0].name == n + " (as exported, not read from Flagsmith)"
    assert any(c.name.endswith("confirmation") and c.status == "PASS" for c in checks)
    assert any(c.name.endswith("evidence") and c.status == "PASS" for c in checks)
    # Survives a round trip through the file format.
    assert vf.summary(_pinned(json.loads(vf.dumps(bundle)), keys))[1] == 0
    # Pinned by the contract hash instead of the keys.
    assert vf.summary(vf.verify_bundle(bundle, expect_hash=h))[1] == 0
    bad = vf.verify_bundle(bundle, expect_hash="sha256:" + "1" * 64)
    assert _by_name(bad, "trust anchor")[0].status == "FAIL"


def test_unpinned_bundle_is_consistent_not_verified(won):
    _, bundle = won
    checks = vf.verify_bundle(bundle)
    assert not _fails(checks)
    assert _by_name(checks, "trust anchor")[0].status == "SKIP"
    text, code = vf.summary(checks)
    assert code == 2 and text.startswith("CONSISTENT (unpinned)")
    assert "the bundle's registry lists the same key" in _by_name(checks, "public key maya")[0].reason


def _evidence(looks, h, upto):
    ordered = sorted(looks, key=lambda lk: lk["seq"])
    return signing.evidence_hash((lk["seq"], lk["id"], h, lk["flagsmith"], bool(lk["recorded"].get("counted")),
                                  lk["recorded"]) for lk in ordered if lk["seq"] <= upto)


def _forge(bundle, h_old):
    """Rebuild the bundle end to end with fresh keys: new contract hash, new signatures, matching Flagsmith lines."""
    from ulysses import contract as ct

    m, s = signing.Keypair.generate(), signing.Keypair.generate()
    f = copy.deepcopy(bundle)
    toml = f["contracts"][0]["toml"].replace(bundle["public_keys"]["maya"], m.public)
    toml = toml.replace(bundle["public_keys"]["sam"], s.public)
    h = ct.from_toml(toml).hash
    f["contracts"][0] = {"hash": h, "toml": toml,
                         "signatures": [{"role": "owner", "signer": "maya",
                                         "signature": m.sign(signing.contract_message(h, "owner"))}]}
    f["public_keys"] = {"maya": m.public, "sam": s.public}
    for x in f["looks"] + f["outcomes"]:
        x["contract_hash"] = h
    for p in f["outcomes"]:
        upto = next(lk["seq"] for lk in f["looks"] if lk["id"] == p["look_id"])
        ev = _evidence(f["looks"], h, upto)
        p["confirm_signature"] = m.sign(signing.confirm_message(h, p["look_id"], p["action"], ev))
    hyp = f["flagsmith_final"]["hypothesis"]
    f["flagsmith_final"]["hypothesis"] = hyp.replace(h_old, h).replace(h_old[7:19], h[7:19])
    return f


def test_self_consistent_forgery_with_fresh_keys_is_not_verified(won, keys, capsys, tmp_path):
    h_old, bundle = won
    forged = _forge(bundle, h_old)
    checks = vf.verify_bundle(forged)
    assert not _fails(checks), [c.line() for c in _fails(checks)]  # nothing inside it disagrees
    text, code = vf.summary(checks)
    assert code != 0 and text.startswith("CONSISTENT (unpinned)")
    # Any outside anchor catches it: the real keys, or the hash on the real experiment.
    assert vf.summary(_pinned(forged, keys))[1] == 1
    assert vf.summary(vf.verify_bundle(forged, expect_hash=h_old))[1] == 1
    path = tmp_path / "forged.json"
    path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        main(["verify", "--bundle", str(path)])
    assert e.value.code == 2 and "CONSISTENT (unpinned)" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        main(["verify", "--bundle", str(path), "--expect-hash", h_old])
    assert e.value.code == 1


def test_bundle_holds_no_timestamps(won):
    _, bundle = won
    assert vf.find_timestamps(bundle) == []
    stamped = copy.deepcopy(bundle)
    stamped["looks"][0]["flagsmith"]["as_of"] = "2001-02-03T04:05:06Z"  # synthetic date
    assert vf.find_timestamps(stamped)
    assert _by_name(vf.verify_bundle(stamped), "no timestamps")[0].status == "FAIL"
    # Integer and millisecond Unix times, other date forms, and keys named as times (all synthetic values).
    for value in (1234567890, 1234567890123, 1234567890.5, "03/02/2001", "2001/02/03", "Feb 3 2001",
                  "3 February 2001", "February 3rd, 2001"):
        assert vf.find_timestamps({"x": value}), value
    for key in ("as_of", "time", "timestamp", "created_at"):
        assert vf.find_timestamps({key: None}), key
    stamped = copy.deepcopy(bundle)
    stamped["outcomes"][0]["result"]["completed"] = 1234567890  # an integer epoch inside free-form result JSON
    assert _by_name(vf.verify_bundle(stamped), "no timestamps")[0].status == "FAIL"
    # Ordinary numbers and words do not trip it.
    assert vf.find_timestamps({"n": 913, "p": 0.9964997314012384, "z": 2.2253, "r": "1/2", "s": "May we"}) == []


def test_edited_contract_fails_on_its_hash_line(won):
    _, bundle = won
    b = copy.deepcopy(bundle)
    b["contracts"][0]["toml"] = b["contracts"][0]["toml"].replace("The big button lifts purchases",
                                                                  "The big button lifts purchases a lot")
    checks = vf.verify_bundle(b)
    line = _by_name(checks, "contract 1 hash")[0]
    assert line.status == "FAIL" and "hashes to" in line.reason
    assert vf.summary(checks)[1] == 1


def test_edited_contract_with_a_rewritten_hash_fails_on_signature_and_hypothesis(won):
    _, bundle = won
    b = copy.deepcopy(bundle)
    b["contracts"][0]["toml"] = b["contracts"][0]["toml"].replace("The big button lifts purchases", "Something else")
    from ulysses import contract as ct

    new_h = ct.from_toml(b["contracts"][0]["toml"]).hash
    old_h = b["contracts"][0]["hash"]
    b["contracts"][0]["hash"] = new_h
    for part in b["looks"] + b["outcomes"]:
        part["contract_hash"] = new_h
    checks = vf.verify_bundle(b)
    assert _by_name(checks, "contract 1 hash")[0].status == "PASS"
    assert _by_name(checks, f"contract {new_h[7:19]} owner signature")[0].status == "FAIL"
    hyp = _by_name(checks, "hypothesis in Flagsmith")[0]
    assert hyp.status == "FAIL" and old_h in hyp.reason


def test_swapped_owner_signature_fails(won, keys):
    h, bundle = won
    b = copy.deepcopy(bundle)
    # A real signature over the right message, but by sam, who is not the owner.
    b["contracts"][0]["signatures"][0]["signature"] = keys["sam"].sign(signing.contract_message(h, "owner"))
    checks = vf.verify_bundle(b)
    line = _by_name(checks, f"contract {h[7:19]} owner signature")[0]
    assert line.status == "FAIL" and "does not match" in line.reason


def test_swapped_confirmation_signature_fails(won, keys):
    _, bundle = won
    b = copy.deepcopy(bundle)
    p = b["outcomes"][0]
    # The owner's genuine tap, but for the other action: it must not confirm this one.
    p["confirm_signature"] = keys["maya"].sign(signing.confirm_message(p["contract_hash"], p["look_id"], "keep_control"))
    line = _by_name(vf.verify_bundle(b), f"outcome {p['id']} confirmation")[0]
    assert line.status == "FAIL"


def test_edited_look_number_fails_on_that_look(won):
    _, bundle = won
    b = copy.deepcopy(bundle)
    firing = next(lk for lk in b["looks"] if lk["recorded"]["outcome"] == "win")
    firing["flagsmith"]["metrics"]["purchase"]["chance_to_win"] = 0.97  # under the boundary at that look
    checks = vf.verify_bundle(b)
    line = _by_name(checks, f"look {firing['seq']} ({firing['id']})")[0]
    assert line.status == "FAIL" and "the signed rule gives 'hold'" in line.reason
    fired = [c for c in checks if c.name.endswith(" fired")][0]
    assert fired.status == "FAIL"


def test_edited_recorded_number_alone_fails(won):
    _, bundle = won
    b = copy.deepcopy(bundle)
    b["looks"][0]["recorded"]["chance_to_win"] = 0.999
    line = _by_name(vf.verify_bundle(b), f"look 1 ({b['looks'][0]['id']})")[0]
    assert line.status == "FAIL" and "chance to win recorded" in line.reason


def test_shipped_outcome_pointing_at_a_look_that_held_fails(won):
    _, bundle = won
    b = copy.deepcopy(bundle)
    holding = next(lk for lk in b["looks"] if lk["recorded"]["outcome"] == "hold")
    p = b["outcomes"][0]
    p["look_id"] = holding["id"]
    line = _by_name(vf.verify_bundle(b), f"outcome {p['id']} fired")[0]
    assert line.status == "FAIL" and "gives 'hold'" in line.reason


def test_shipped_variation_the_rule_did_not_fire_fails(won):
    _, bundle = won
    b = copy.deepcopy(bundle)
    p = b["outcomes"][0]
    p["result"]["serving"] = "control"
    checks = vf.verify_bundle(b)
    assert _by_name(checks, f"outcome {p['id']} shipped")[0].status == "FAIL"
    # And on Flagsmith's side: the experiment says it serves control, the rule that fired ships big_cta.
    b = copy.deepcopy(bundle)
    b["flagsmith_final"]["rollout_decision"] = "control"
    b["flagsmith_final"]["rollout_variant"] = None
    line = _by_name(vf.verify_bundle(b), "final state in Flagsmith")[0]
    assert line.status == "FAIL" and "'big_cta'" in line.reason


def test_action_the_contract_does_not_assign_fails(world, keys, clock):
    svc, fs, store = world
    h = svc.submit(make_contract(keys, n=1000, looks=10))["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    fs.set_results(1, 1000, 1000, 0.96)  # horizon: the contract keeps control
    out = svc.look(h)
    _confirm(svc, keys, out)
    b = vf.export_bundle(store, "env-key", 1, flagsmith=vf.flagsmith_state(fs, "env-key", 1))
    assert not _fails(vf.verify_bundle(b))
    p = b["outcomes"][0]
    p["action"] = "ship_treatment"
    p["result"]["serving"] = "big_cta"
    checks = vf.verify_bundle(b)
    assert _by_name(checks, f"outcome {p['id']} action")[0].status == "FAIL"
    assert _by_name(checks, f"outcome {p['id']} confirmation")[0].status == "FAIL"


def test_hypothesis_hash_that_does_not_match_fails(won):
    h, bundle = won
    b = copy.deepcopy(bundle)
    other = "sha256:" + "0" * 64
    b["flagsmith_final"]["hypothesis"] = b["flagsmith_final"]["hypothesis"].replace(h, other)
    line = _by_name(vf.verify_bundle(b), "hypothesis in Flagsmith")[0]
    assert line.status == "FAIL" and other in line.reason


def test_bundle_without_flagsmith_state_is_incomplete_not_verified(world, keys, clock):
    svc, fs, store = world
    _won(svc, fs, keys, clock)
    b = vf.export_bundle(store, "env-key", 1, flagsmith_unavailable="test: not read")
    checks = vf.verify_bundle(b)
    assert not _fails(checks)
    assert [c.name.split(" (")[0] for c in checks if c.status == "SKIP"] == [
        "hypothesis in Flagsmith", "trust anchor", "final state in Flagsmith"]
    assert vf.summary(checks)[1] == 2


def test_amendment_needs_both_signatures_in_the_bundle(world, keys):
    svc, fs, store = world
    h = svc.submit(make_contract(keys, n=1000, looks=10))["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    h2 = svc.amend(h, {}, reason="legal requires the new button now", action_now="ship_treatment")["hash"]
    sign_as(svc, keys, h2, "maya", "owner")
    sign_as(svc, keys, h2, "sam", "second_signer")
    p = store.proposals_for(h2)[0]
    svc.confirm(p["id"], "maya", keys["maya"].sign(signing.confirm_message(h2, p["id"], "ship_treatment")))
    b = vf.export_bundle(store, "env-key", 1, flagsmith=vf.flagsmith_state(fs, "env-key", 1))
    checks = vf.verify_bundle(b)
    assert not _fails(checks), [c.line() for c in _fails(checks)]
    assert _by_name(checks, f"contract {h2[7:19]} chain")[0].status == "PASS"
    assert _by_name(checks, f"contract {h2[7:19]} second signer signature")[0].status == "PASS"
    b["contracts"][1]["signatures"] = [s for s in b["contracts"][1]["signatures"] if s["role"] == "owner"]
    line = _by_name(vf.verify_bundle(b), f"contract {h2[7:19]} second signer signature")[0]
    assert line.status == "FAIL"


def test_trusted_key_must_match_the_contract(won, keys):
    _, bundle = won
    ok = vf.verify_bundle(bundle, trusted_keys={"maya": keys["maya"].public})
    assert _by_name(ok, "public key maya")[0].status == "PASS"
    bad = vf.verify_bundle(bundle, trusted_keys={"maya": keys["mallory"].public})
    assert _by_name(bad, "public key maya")[0].status == "FAIL"


def test_deviation_fails_the_audit(world, keys):
    svc, fs, store = world
    h = svc.submit(make_contract(keys, n=1000, looks=10))["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    fs.dashboard_complete(1, user="pm@example.com", ship="big_cta")
    svc.sync_audit(1)
    checks = vf.verify_bundle(vf.export_bundle(store, "env-key", 1, flagsmith=vf.flagsmith_state(fs, "env-key", 1)))
    assert _by_name(checks, "deviations recorded")[0].status == "FAIL"
    assert _by_name(checks, "final state in Flagsmith")[0].status == "FAIL"


def test_cli_export_then_verify_bundle_offline(tmp_path, keys, clock, capsys, monkeypatch):
    db = tmp_path / "svc.db"
    fs = FakeFlagsmith(clock=clock)
    fs.add_experiment(1)
    store = Store(db)
    svc = Ulysses(store, fs, Settings(public_url="http://test", refresh_wait_seconds=0, poll_seconds=0),
                  clock=clock, sleep=lambda s: clock.advance(s))
    svc.register_signer("maya", keys["maya"].public)
    svc.register_signer("sam", keys["sam"].public)
    _won(svc, fs, keys, clock)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "bundle.json"
    main(["export-bundle", "1", "--db", str(db), "--out", str(out), "--no-flagsmith", "test double, not read"])
    assert vf.find_timestamps(json.loads(out.read_text(encoding="utf-8"))) == []
    with pytest.raises(SystemExit) as e:
        main(["verify", "--bundle", str(out)])
    text = capsys.readouterr().out
    assert e.value.code == 2 and "CONSISTENT (unpinned)" in text and "FAIL " not in text
    b = json.loads(out.read_text(encoding="utf-8"))
    b["looks"][-1]["flagsmith"]["metrics"]["purchase"]["chance_to_win"] = 0.5
    out.write_text(json.dumps(b), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        main(["verify", "--bundle", str(out)])
    assert e.value.code == 1 and "FAIL  look" in capsys.readouterr().out


@pytest.mark.skipif(not EXAMPLE.exists(), reason="example bundle not shipped")
def test_shipped_example_bundle_verifies_and_is_clean():
    text = EXAMPLE.read_text(encoding="utf-8")
    b = json.loads(text)
    assert vf.find_timestamps(b) == []
    assert vf.find_secrets(text, []) == []
    checks = vf.verify_bundle(b)
    assert not _fails(checks), [c.line() for c in _fails(checks)]
    skipped = {c.name.split(" (")[0] for c in checks if c.status == "SKIP"}
    allowed = {"hypothesis in Flagsmith", "final state in Flagsmith", "outcome line in Flagsmith", "trust anchor"}
    # The demo's tap was signed in the earlier form, which does not cover the looks' numbers.
    allowed |= {c.name for c in checks if c.name.endswith(" evidence")}
    assert skipped <= allowed
    assert vf.summary(checks)[1] == 2
    if b.get("flagsmith_final") is None:
        assert b["flagsmith_final_missing"]


def _rerecord(bundle):
    """Rewrite every look's recorded decision to what the signed rule gives on its numbers, as a careful forger would."""
    from ulysses import contract as ct
    from ulysses.decision import evaluate, observation_from_snapshot

    con = ct.from_toml(bundle["contracts"][0]["toml"])
    prior: list[float] = []
    for lk in sorted(bundle["looks"], key=lambda x: x["seq"]):
        d = evaluate(con, prior, observation_from_snapshot(con, vf._snapshot(lk["flagsmith"])))
        if d.counted:
            t = round(min(d.identities_per_arm / con.horizon.identities_per_arm, 1.0), 6)
            if not prior or t > prior[-1]:
                prior.append(t)
        dd = d.to_dict()
        lk["recorded"].update({k: dd.get(k) for k in ("outcome", "action", "counted", "look_number", "chance_to_win",
                                                      "boundary_z", "boundary_chance")})
        lk["recorded"]["guardrails"] = [{k: g.get(k) for k in ("name", "chance_worse", "boundary_z", "state")}
                                        for g in dd.get("guardrails") or []]


def test_consistent_rewrite_of_a_held_look_breaks_the_owners_tap(won, keys):
    _, bundle = won
    b = copy.deepcopy(bundle)
    held = next(lk for lk in b["looks"] if lk["recorded"]["outcome"] == "hold")
    held["flagsmith"]["metrics"]["purchase"]["chance_to_win"] = 0.9  # still a hold, so no decision changes
    _rerecord(b)
    checks = _pinned(b, keys)
    assert all(c.status == "PASS" for c in checks if c.name.startswith("look ")), "the numbers alone agree"
    line = [c for c in checks if c.name.endswith(" confirmation")][0]
    assert line.status == "FAIL" and "edited or dropped" in line.reason


def test_dropped_look_breaks_the_owners_tap(won, keys):
    _, bundle = won
    b = copy.deepcopy(bundle)
    b["looks"] = [lk for lk in b["looks"] if lk["recorded"]["outcome"] != "hold"]
    _rerecord(b)
    checks = _pinned(b, keys)
    assert all(c.status == "PASS" for c in checks if c.name.startswith("look "))
    assert [c for c in checks if c.name.endswith(" confirmation")][0].status == "FAIL"


def test_earlier_form_of_the_tap_is_refused_live_and_leaves_evidence_unchecked_in_a_bundle(world, keys, clock):
    """The service refuses a tap over the earlier message (contract, look, action). A bundle that holds one
    (data/verify-judge-demo-cta.json was recorded that way) still verifies the tap; the verifier says what it lacks."""
    from ulysses.core import Refused

    svc, fs, store = world
    h = svc.submit(make_contract(keys, n=1000, looks=10))["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    fs.set_results(1, 400, 402, PHI(4.0), guardrails={"refunds": 0.45})
    p = svc.look(h)["proposal"]
    legacy = keys["maya"].sign(signing.confirm_message(h, p["look_id"], p["action"]))
    with pytest.raises(Refused):
        svc.confirm(p["id"], "maya", legacy)
    assert svc.confirm(p["id"], "maya", tap(svc, keys, p))["state"] == "executed"
    b = vf.export_bundle(store, "env-key", 1, flagsmith=vf.flagsmith_state(fs, "env-key", 1))
    next(o for o in b["outcomes"] if o["id"] == p["id"])["confirm_signature"] = legacy
    checks = _pinned(b, keys)
    assert not _fails(checks)
    assert _by_name(checks, f"outcome {p['id']} evidence")[0].status == "SKIP"
    assert vf.summary(checks)[1] == 2


def test_card_message_covers_the_looks_up_to_the_fired_one(world, keys, clock):
    svc, fs, store = world
    h = svc.submit(make_contract(keys, n=1000, looks=10))["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    fs.set_results(1, 300, 305, 0.97, guardrails={"refunds": 0.4})
    svc.look(h)
    clock.advance(301)
    fs.set_results(1, 400, 402, PHI(4.0), guardrails={"refunds": 0.45})
    p = svc.look(h)["proposal"]
    msg = svc.proposal_card(p["id"])["confirm_message"]
    looks = store.looks("env-key", 1)
    assert len(looks) == 2
    ev = signing.evidence_hash((x["seq"], x["id"], x["contract_hash"], x["snapshot"], bool(x["counted"]),
                                x["decision"]) for x in looks)
    assert msg == signing.confirm_message(h, p["look_id"], p["action"], ev).decode()
    assert svc.look_view(p["look_id"])["confirm_message"] == msg
    # A signature over other evidence is refused.
    from ulysses.core import Refused

    with pytest.raises(Refused):
        svc.confirm(p["id"], "maya", keys["maya"].sign(signing.confirm_message(h, p["look_id"], p["action"], "x")))


def test_fired_look_whose_outcome_was_dropped_fails(won, keys):
    _, bundle = won
    b = copy.deepcopy(bundle)
    b["outcomes"] = []
    b["flagsmith_final"].update(status="running", rollout_decision=None, rollout_variant=None)
    b["flagsmith_final"]["hypothesis"] = "\n".join(ln for ln in b["flagsmith_final"]["hypothesis"].splitlines()
                                                   if "fired" not in ln)
    checks = _pinned(b, keys)
    fired = next(lk for lk in b["looks"] if lk["recorded"]["outcome"] == "win")
    line = _by_name(checks, f"look {fired['seq']} ({fired['id']}) outcome")[0]
    assert line.status == "FAIL" and "holds no outcome" in line.reason
    assert vf.summary(checks)[1] == 1


def test_counted_look_after_the_fire_fails(won, keys):
    _, bundle = won
    b = copy.deepcopy(bundle)
    fired = next(lk for lk in b["looks"] if lk["recorded"]["outcome"] == "win")
    extra = copy.deepcopy(fired)
    extra.update(id="L9999-synthetic", seq=fired["seq"] + 1)
    extra["flagsmith"]["identities"] = {k: v + 200 for k, v in extra["flagsmith"]["identities"].items()}
    b["looks"].append(extra)
    _rerecord(b)
    line = _by_name(_pinned(b, keys), f"look {extra['seq']} ({extra['id']})")[0]
    assert line.status == "FAIL" and f"after look {fired['id']} fired" in line.reason


@pytest.mark.parametrize("damage, expect", [
    (lambda b: b["looks"][0].update(seq=None), "integer seq"),
    (lambda b: b["looks"][0].update(recorded="bad"), "recorded must be an object"),
    (lambda b: b["looks"][1].update(seq=b["looks"][0]["seq"]), "repeats seq"),
    (lambda b: b.update(contracts={"not": "a list"}), "contracts must be a list"),
    (lambda b: b["outcomes"][0].update(result="x"), "result must be an object"),
    (lambda b: b.update(flagsmith_final=[1]), "flagsmith_final must be an object"),
    (lambda b: b["looks"][0]["flagsmith"].update(identities={"control": "many"}), "identities must map"),
    (lambda b: b.update(experiment=None), "experiment must be an object"),
])
def test_malformed_bundle_fails_on_the_format_line(won, damage, expect):
    _, bundle = won
    b = copy.deepcopy(bundle)
    damage(b)
    checks = vf.verify_bundle(b)  # no exception
    assert checks[-1].name == "bundle format" and checks[-1].status == "FAIL" and expect in checks[-1].reason
    assert vf.summary(checks)[1] == 1


def test_verify_rejects_an_id_that_is_not_the_bundles(won, tmp_path, capsys):
    _, bundle = won
    path = tmp_path / "b.json"
    path.write_text(vf.dumps(bundle), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        main(["verify", "5", "--bundle", str(path)])
    assert e.value.code == 1
    assert "experiment 5" in capsys.readouterr().err.replace("\n", " ")
    with pytest.raises(SystemExit) as e:  # the matching id is fine
        main(["verify", "1", "--bundle", str(path)])
    assert e.value.code == 2


def _cli_world(tmp_path, keys, clock):
    db = tmp_path / "svc.db"
    fs = FakeFlagsmith(clock=clock)
    fs.add_experiment(1)
    store = Store(db)
    svc = Ulysses(store, fs, Settings(public_url="http://test", refresh_wait_seconds=0, poll_seconds=0),
                  clock=clock, sleep=lambda s: clock.advance(s))
    svc.register_signer("maya", keys["maya"].public)
    svc.register_signer("sam", keys["sam"].public)
    return db, fs, svc


def test_cli_live_verify_reads_flagsmith(tmp_path, keys, clock, capsys, monkeypatch):
    from ulysses import cli

    db, fs, svc = _cli_world(tmp_path, keys, clock)
    h = _won(svc, fs, keys, clock)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_flagsmith_gateway", lambda: fs)
    with pytest.raises(SystemExit) as e:
        main(["verify", "1", "--db", str(db)])
    out = capsys.readouterr().out
    assert e.value.code == 0, out
    assert "PASS  hypothesis in Flagsmith: names " + h in out
    assert "PASS  trust anchor: Flagsmith's hypothesis, read just now" in out
    assert "the service registered the same key" in out
    assert "as exported" not in out and "VERIFIED" in out
    # --bundle --check-flagsmith re-reads Flagsmith and compares it with the exporter's copy.
    bundle = tmp_path / "b.json"
    main(["export-bundle", "1", "--db", str(db), "--out", str(bundle)])
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        main(["verify", "--bundle", str(bundle), "--check-flagsmith"])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "PASS  exported Flagsmith state" in out and "VERIFIED" in out
    b = json.loads(bundle.read_text(encoding="utf-8"))
    b["flagsmith_final"]["status"] = "running"
    bundle.write_text(json.dumps(b), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        main(["verify", "--bundle", str(bundle), "--check-flagsmith"])
    out = capsys.readouterr().out
    assert e.value.code == 1 and "FAIL  exported Flagsmith state" in out and "status" in out


def test_cli_live_verify_when_flagsmith_cannot_be_read(tmp_path, keys, clock, capsys, monkeypatch):
    from ulysses import cli
    from ulysses.flagsmith.gateway import FlagsmithError

    db, fs, svc = _cli_world(tmp_path, keys, clock)
    _won(svc, fs, keys, clock)
    monkeypatch.chdir(tmp_path)

    class Down:
        def get_experiment(self, environment, experiment_id):
            raise FlagsmithError("connection refused (test double)")

    monkeypatch.setattr(cli, "_flagsmith_gateway", lambda: Down())
    with pytest.raises(SystemExit) as e:
        main(["verify", "1", "--db", str(db)])
    err = capsys.readouterr().err.replace("\n", " ")
    assert e.value.code == 1 and "could not read experiment 1 from Flagsmith" in err
