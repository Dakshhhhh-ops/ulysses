"""Flagsmith Feature Health: reporting breaches out, holding a ship on an unhealthy flag."""

import json
from statistics import NormalDist

import httpx
import pytest

from ulysses import contract as ct
from ulysses import signing, verify
from ulysses.core import Refused
from ulysses.flagsmith import health as fh
from ulysses.flagsmith.rest import RestFlagsmith

from .conftest import make_contract, sign_as

PHI = NormalDist().cdf


def started(svc, keys, **kw):
    h = svc.submit(make_contract(keys, **kw))["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    return h


def confirm(svc, keys, out, who="maya"):
    p = out["proposal"]
    msg = signing.confirm_message(p["contract"], p["look_id"] or p["id"], p["action"])
    return svc.confirm(p["id"], who, keys[who].sign(msg))


def wire(svc, fs):
    """Create the Webhook provider as `ulysses lab setup` does and point the service at it."""
    prov = fs.create_health_provider(1, "Webhook")
    svc.health.webhook_url = prov.webhook_url
    svc.health.post = fs.receive_webhook
    return prov


# ---- contract field ------------------------------------------------------------------


def test_new_contracts_sign_the_health_rule_and_it_is_in_the_hash(keys):
    c = make_contract(keys)
    assert c.health == ct.Health(block_ship_when_unhealthy=True)
    assert c.canonical()["health"] == {"block_ship_when_unhealthy": True}
    off = make_contract(keys, health=ct.Health(block_ship_when_unhealthy=False))
    none = make_contract(keys, health=None)
    assert len({c.hash, off.hash, none.hash}) == 3
    # A contract signed before the field existed has no health key, so its hash is unchanged.
    assert "health" not in none.canonical()
    assert ct.from_toml(ct.to_toml(c)).hash == c.hash


def test_unknown_health_keys_are_rejected(keys):
    data = make_contract(keys).canonical()
    data["health"] = {"block_ship_when_unhealthy": True, "ignore_grafana": True}
    with pytest.raises(Exception, match="ignore_grafana"):
        ct.from_canonical(data)


# ---- reporting out ---------------------------------------------------------------------


def test_guardrail_breach_posts_unhealthy_with_the_webhook_schema(world, keys):
    svc, fs, _ = world
    wire(svc, fs)
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 120, 118, 0.99, guardrails={"refunds": 0.00001})
    out = svc.look(h)
    assert out["decision"]["outcome"] == "guardrail_breach"
    assert out["feature_health"]["reported"] == "unhealthy" and out["feature_health"]["delivered"] is True
    assert out["feature_health"]["flagsmith_status"] == 200
    [body] = fs.webhook_posts
    assert body["feature"] == "checkout_button" and body["environment"] == "Test" and body["status"] == "unhealthy"
    assert set(body["reason"]) == {"text_blocks", "url_blocks"}
    [tb] = body["reason"]["text_blocks"]
    assert tb["title"].startswith("Ulysses") and "refunds" in tb["title"]
    assert "guardrail" in tb["text"] and h[7:19] in tb["text"]  # the short contract hash
    [ub] = body["reason"]["url_blocks"]
    assert ub["url"] == f"http://test/card/{out['look_id']}"
    # Flagsmith stored it against the flag's feature in that environment, from the Webhook provider.
    [ev] = fs.health_events(1)
    assert (ev.type, ev.provider_name, ev.feature, ev.environment) == ("UNHEALTHY", "Webhook", 101, 1)
    # The breach keeps control; that is never blocked, and completing clears the alert.
    res = confirm(svc, keys, out)
    assert res["state"] == "executed" and fs.served["checkout_button"] == "control"
    assert fs.webhook_posts[-1]["status"] == "healthy"
    assert [e.type for e in fs.health_events(1)] == ["HEALTHY"]
    assert [r["status"] for r in svc.health.reports()] == ["unhealthy", "healthy"]


def test_sample_ratio_mismatch_reports_unhealthy_once_then_healthy_when_it_clears(world, keys, clock):
    svc, fs, _ = world
    wire(svc, fs)
    h = started(svc, keys, n=10000, looks=10)
    fs.set_results(1, 1000, 1300, 0.6)  # a 50/50 split read as 1000 vs 1300: chi-square p far below 0.001
    out = svc.look(h)
    assert out["decision"]["sample_ratio"]["state"] == "mismatch"
    assert out["feature_health"]["reported"] == "unhealthy" and out["feature_health"]["causes"] == ["sample_ratio"]
    assert "sample-ratio" in fs.webhook_posts[-1]["reason"]["text_blocks"][0]["title"]
    clock.advance(301)
    fs.set_results(1, 1400, 1700, 0.6)  # still broken: not sent again
    out = svc.look(h)
    assert "feature_health" not in out or out["feature_health"]["reported"] is None
    assert len(fs.webhook_posts) == 1
    clock.advance(301)
    fs.set_results(1, 3000, 3010, 0.6)
    out = svc.look(h)
    assert out["feature_health"]["reported"] == "healthy"
    assert fs.webhook_posts[-1]["status"] == "healthy"


def test_deviation_reports_unhealthy(world, keys):
    svc, fs, _ = world
    wire(svc, fs)
    started(svc, keys, n=1000, looks=10)
    fs.dashboard_complete(1, user="pm@example.com", ship="big_cta")
    svc.sync_audit(1)
    assert fs.webhook_posts and all(b["status"] == "unhealthy" for b in fs.webhook_posts)
    assert "outside the signed contract" in fs.webhook_posts[0]["reason"]["text_blocks"][0]["title"]
    assert "/sign/" in fs.webhook_posts[0]["reason"]["url_blocks"][0]["url"]


def test_webhook_down_does_not_break_a_look_and_is_sent_later(world, keys, clock):
    svc, fs, _ = world
    wire(svc, fs)
    h = started(svc, keys, n=1000, looks=10)
    fs.webhook_down = True
    fs.set_results(1, 120, 118, 0.99, guardrails={"refunds": 0.00001})
    out = svc.look(h)
    # The look is recorded and fires as usual; the report waits.
    assert out["decision"]["outcome"] == "guardrail_breach" and out["proposal"]["state"] == "pending"
    fhr = out["feature_health"]
    assert fhr["delivered"] is False and fhr["state"] == "pending" and fhr["attempts"] == 3
    assert "cannot reach" in fhr["detail"] and "signedpart" not in fhr["detail"]
    assert fs.health_events(1) == []
    # Flagsmith is back: the pending unhealthy goes first, then the healthy for the completion.
    fs.webhook_down = False
    confirm(svc, keys, out)
    assert [b["status"] for b in fs.webhook_posts] == ["unhealthy", "healthy"]
    assert [r["state"] for r in svc.health.reports()] == ["sent", "sent"]


def test_webhook_that_raises_anything_never_breaks_a_look(world, keys):
    svc, fs, _ = world
    wire(svc, fs)

    def boom(url, body):
        raise RuntimeError("unexpected")

    svc.health.post = boom
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 120, 118, 0.99, guardrails={"refunds": 0.00001})
    out = svc.look(h)
    assert out["decision"]["outcome"] == "guardrail_breach"
    assert out["feature_health"]["delivered"] is False


def test_no_provider_configured_sends_nothing(world, keys):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 120, 118, 0.99, guardrails={"refunds": 0.00001})
    out = svc.look(h)
    assert "feature_health" not in out and fs.webhook_posts == []


# ---- the signed rule at execution ---------------------------------------------------------


def _win(svc, fs, keys, h):
    fs.set_results(1, 1000, 1000, PHI(5.0))
    out = svc.look(h)
    assert out["decision"]["outcome"] in ("win", "horizon")
    return out


def test_unhealthy_from_another_provider_blocks_the_ship(world, keys, clock):
    svc, fs, store = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 400, 402, PHI(4.0))
    out = svc.look(h)
    assert out["decision"]["outcome"] == "win"
    fs.external_health("checkout_button", "unhealthy", provider="Grafana", environment="env-key",
                       title="p95 latency over SLO")
    with pytest.raises(Refused, match="unhealthy") as exc:
        confirm(svc, keys, out)
    assert "Grafana" in str(exc.value) and "p95 latency over SLO" in str(exc.value)
    assert exc.value.to_dict()["feature_health"]["state"] == "unhealthy"
    assert fs.experiments[1].status == "running" and fs.served == {}
    assert any("Feature Health" in r["reason"] for r in store.refusals())
    # The alert clears: the same confirmed proposal now ships, and the record says what it relied on.
    fs.external_health("checkout_button", "healthy", provider="Grafana", environment="env-key")
    res = svc.execute(out["proposal"]["id"])
    assert res["state"] == "executed" and fs.served["checkout_button"] == "big_cta"
    rec = json.loads(store.proposal(out["proposal"]["id"])["result"])["feature_health"]
    assert rec["state"] == "healthy" and rec["environment"] == "Test" and rec["events_for_flag"] == 1


def test_unhealthy_in_another_environment_does_not_block(world, keys):
    svc, fs, _ = world
    fs.add_environment("prod-key", name="Production", project=1)
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 400, 402, PHI(4.0))
    out = svc.look(h)
    fs.external_health("checkout_button", "unhealthy", environment="prod-key", external_id="prod")
    p = confirm(svc, keys, out)  # unhealthy only in another environment: ships
    assert p["state"] == "executed"


def test_event_with_no_environment_blocks(world, keys):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 400, 402, PHI(4.0))
    out = svc.look(h)
    fs.external_health("checkout_button", "unhealthy", environment=None)
    with pytest.raises(Refused, match="unhealthy"):
        confirm(svc, keys, out)


def test_unhealthy_never_blocks_keeping_control(world, keys):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    fs.external_health("checkout_button", "unhealthy")
    fs.set_results(1, 1000, 1000, PHI(-3))
    out = svc.look(h)
    assert out["proposal"]["action"] == "keep_control"
    assert confirm(svc, keys, out)["state"] == "executed"


def test_unreadable_health_holds_a_ship(world, keys, monkeypatch):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 400, 402, PHI(4.0))
    out = svc.look(h)

    def down(project):
        from ulysses.flagsmith.gateway import FlagsmithError

        raise FlagsmithError("cannot reach Flagsmith: connection refused")

    monkeypatch.setattr(fs, "health_events", down)
    with pytest.raises(Refused, match="could not be read"):
        confirm(svc, keys, out)
    assert fs.served == {}


def test_rule_off_or_absent_ships_while_unhealthy(world, keys):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10, health=None)
    fs.external_health("checkout_button", "unhealthy")
    fs.set_results(1, 400, 402, PHI(4.0))
    out = svc.look(h)
    assert confirm(svc, keys, out)["state"] == "executed" and fs.served["checkout_button"] == "big_cta"


def test_verify_reports_the_health_state_relied_on(world, keys):
    svc, fs, store = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 400, 402, PHI(4.0))
    confirm(svc, keys, svc.look(h))
    bundle = verify.export_bundle(store, "env-key", 1, flagsmith=verify.flagsmith_state(fs, "env-key", 1))
    checks = verify.verify_bundle(bundle)
    [c] = [c for c in checks if c.name.endswith("feature health")]
    assert c.status == "PASS" and "healthy" in c.reason and "'Test'" in c.reason
    # An executed ship with an unhealthy record fails the audit.
    bundle["outcomes"][0]["result"]["feature_health"]["state"] = "unhealthy"
    checks = verify.verify_bundle(bundle)
    assert [c.status for c in checks if c.name.endswith("feature health")] == ["FAIL"]


# ---- REST shapes (against the serializers in features/feature_health/) -------------------


def test_rest_client_paths_and_parsing():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, req.content))
        if req.url.path == "/api/v1/environments/envk/":
            return httpx.Response(200, json={"id": 5, "name": "ulysses-demo", "project": 3, "api_key": "envk"})
        if req.url.path.endswith("/feature-health/providers/") and req.method == "POST":
            return httpx.Response(201, json={"created_by": None, "name": "Webhook", "project": 3,
                                             "webhook_url": "http://x/api/v1/feature-health/abc/def"})
        if req.url.path.endswith("/feature-health/events/"):
            return httpx.Response(200, json=[
                {"id": 1, "created_at": "x", "environment": 5, "feature": 109, "provider_name": "Webhook",
                 "reason": {"text_blocks": [{"title": "Ulysses: t", "text": "x"}], "url_blocks": []},
                 "type": "UNHEALTHY"},
                {"id": 2, "created_at": "x", "environment": None, "feature": 7, "provider_name": "Grafana",
                 "reason": None, "type": "HEALTHY"}])
        return httpx.Response(404)

    rest = RestFlagsmith("http://fs", "k", transport=httpx.MockTransport(handler))
    assert rest.environment_info("envk") == fh.EnvironmentInfo(5, "ulysses-demo", 3)
    prov = rest.create_health_provider(3)
    assert prov.webhook_url.endswith("/feature-health/abc/def")
    assert seen[-1] == ("POST", "/api/v1/projects/3/feature-health/providers/", b'{"name":"Webhook"}')
    evs = rest.health_events(3)
    assert [e.unhealthy for e in evs] == [True, False]
    assert fh.unhealthy_for(evs, 109, 5)[0].titles() == ["Ulysses: t"]
    assert fh.unhealthy_for(evs, 109, 6) == []


def test_payload_and_redaction():
    body = fh.webhook_payload(feature="f", environment="e", status="unhealthy", title="T", text="x",
                              url="http://u", url_title="card")
    assert body == {"feature": "f", "environment": "e", "status": "unhealthy",
                    "reason": {"text_blocks": [{"title": "T", "text": "x"}],
                               "url_blocks": [{"url": "http://u", "title": "card"}]}}
    with pytest.raises(ValueError):
        fh.webhook_payload(feature="f", environment=None, status="sick", title="T", text="x")
    red = fh.redact_webhook("http://localhost:8000/api/v1/feature-health/0123456789abcdef/sig")
    assert red == "http://localhost:8000/api/v1/feature-health/012345..."


def test_set_env_value_keeps_other_lines(tmp_path):
    from ulysses import lab

    p = tmp_path / ".env"
    p.write_text("A=1\nULYSSES_HEALTH_WEBHOOK_URL=old\nB=2\n", encoding="utf-8")
    lab.set_env_value(p, "ULYSSES_HEALTH_WEBHOOK_URL", "new")
    assert p.read_text(encoding="utf-8") == "A=1\nULYSSES_HEALTH_WEBHOOK_URL=new\nB=2\n"
    lab.set_env_value(tmp_path / "fresh.env", "K", "v")
    assert (tmp_path / "fresh.env").read_text(encoding="utf-8") == "K=v\n"
