import pytest
from pydantic import ValidationError

from ulysses import contract as ct
from ulysses import signing
from ulysses.canonical import canonical_bytes, content_hash

from .conftest import make_contract


def test_canonical_form_is_sorted_compact_utf8():
    assert canonical_bytes({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()


def test_canonical_rejects_nan():
    with pytest.raises(ValueError):
        canonical_bytes({"x": float("nan")})


def test_hash_ignores_formatting_and_comments(keys):
    c = make_contract(keys)
    text = ct.to_toml(c)
    reformatted = "# a different comment\n\n" + text.replace(" = ", "   =   ")
    assert ct.from_toml(reformatted).hash == c.hash


def test_any_value_change_changes_the_hash(keys):
    c = make_contract(keys)
    text = ct.to_toml(c)
    tampered = text.replace('hypothesis = "The big button lifts purchases"', 'hypothesis = "The big button lifts revenue"')
    assert tampered != text
    assert ct.from_toml(tampered).hash != c.hash


def test_toml_round_trip_is_lossless(keys, tmp_path):
    c = make_contract(keys)
    p = ct.save(c, tmp_path / "c.toml")
    back = ct.load(p)
    assert back == c
    assert back.hash == c.hash
    assert c.hash.startswith("sha256:") and len(c.hash) == 7 + 64


def test_edited_boundary_table_is_rejected(keys):
    c = make_contract(keys)
    data = c.canonical()
    data["boundary"]["planned"][-1]["z"] = 1.5  # make the final look easier to pass
    with pytest.raises(ValidationError, match="does not match the signed spending rule"):
        ct.from_canonical(data)


def test_contract_rejects_bad_shapes(keys):
    with pytest.raises(ValidationError):
        make_contract(keys, n=10)
    with pytest.raises(ValidationError):
        ct.Horizon(identities_per_arm=1000, looks=10, min_minutes_between_looks=1)
    with pytest.raises(ValidationError, match="guardrail breach must keep control"):
        ct.Actions(guardrail_breach="ship_treatment")
    with pytest.raises(ValidationError, match="different person"):
        ct.draft(
            experiment=ct.ExperimentRef(project=1, environment="e", experiment=1, flag="f", treatment="t"),
            hypothesis="h", primary_metric=ct.Metric(name="m", direction="increase"),
            horizon=ct.Horizon(identities_per_arm=1000, looks=5),
            owner=ct.Signer(name="maya", public_key=keys["maya"].public),
            second_signer=ct.Signer(name="maya", public_key=keys["maya"].public),
        )


def test_no_calendar_dates_in_a_contract(keys):
    text = ct.to_toml(make_contract(keys))
    import re

    assert not re.search(r"\b(19|20)\d\d-\d\d-\d\d", text)
    assert "start_within_days" in text


def test_signature_round_trip_and_domain_separation(keys):
    h = make_contract(keys).hash
    sig = keys["maya"].sign(signing.contract_message(h, "owner"))
    signing.verify(keys["maya"].public, signing.contract_message(h, "owner"), sig)
    # The same signature is not valid for another role, another hash, or another purpose.
    assert not signing.is_valid(keys["maya"].public, signing.contract_message(h, "second_signer"), sig)
    assert not signing.is_valid(keys["maya"].public, signing.contract_message(content_hash({"x": 1}), "owner"), sig)
    assert not signing.is_valid(keys["maya"].public, signing.confirm_message(h, "L1", "ship_treatment"), sig)
    # Nor is it valid under someone else's key.
    assert not signing.is_valid(keys["sam"].public, signing.contract_message(h, "owner"), sig)


def test_signature_rejects_garbage(keys):
    msg = signing.contract_message("sha256:" + "0" * 64, "owner")
    assert not signing.is_valid(keys["maya"].public, msg, "not base64!!")
    assert not signing.is_valid(keys["maya"].public, msg, "AAAA")
    with pytest.raises(signing.SignatureError):
        signing.decode_public("rsa:abc")


def test_keys_persist_and_refuse_overwrite(tmp_path):
    kp = signing.Keypair.generate()
    signing.save_keypair("maya", kp, tmp_path)
    assert signing.load_keypair("maya", tmp_path).public == kp.public
    with pytest.raises(FileExistsError):
        signing.save_keypair("maya", signing.Keypair.generate(), tmp_path)


def test_key_files_can_be_passphrase_encrypted(tmp_path):
    kp = signing.Keypair.generate()
    path = signing.save_keypair("maya", kp, tmp_path, passphrase="correct horse")
    assert b"ENCRYPTED" in path.read_bytes()
    with pytest.raises(signing.SignatureError, match="encrypted"):
        signing.load_keypair("maya", tmp_path)
    with pytest.raises(signing.SignatureError, match="wrong passphrase"):
        signing.load_keypair("maya", tmp_path, passphrase="nope")
    assert signing.load_keypair("maya", tmp_path, passphrase="correct horse").public == kp.public
