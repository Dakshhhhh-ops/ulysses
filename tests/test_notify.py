"""Ask the owner: a message when an action waits for the owner's tap, an FYI when one ran under signed autonomy.

The message tells a person where to look and grants nothing. Every test runs over the test double of
Flagsmith; the transport is either a recording stub or a real local HTTP receiver.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from statistics import NormalDist

import pytest

from ulysses import contract as ct
from ulysses import notify as nt
from ulysses.core import Settings, Ulysses
from ulysses.flagsmith.fake import FakeFlagsmith
from ulysses.store import Store

from .conftest import Clock, make_contract, sign_as, tap

PHI = NormalDist().cdf
KEEP = ct.Autonomy(without_confirmation=["keep_control"])
SECRETS = ["flagsmith-admin-key-0123456789", "agent-token-abcdefghijklmnop", "webhook-secret-zyxwvutsrq"]


class Recorder:
    """A transport that records every POST and answers with the next status (default 200)."""

    def __init__(self, statuses=None, raise_with: Exception | None = None):
        self.sent: list[tuple[str, bytes, dict]] = []
        self.statuses = list(statuses or [])
        self.raise_with = raise_with

    def __call__(self, url, body, headers):
        self.sent.append((url, body, dict(headers)))
        if self.raise_with:
            raise self.raise_with
        return self.statuses.pop(0) if self.statuses else 200

    def json(self, i=-1) -> dict:
        return json.loads(self.sent[i][1])


def service(clock, keys, transport, *, url="https://hooks.example.test/T000/B000/abc", store=None, fmt=None,
            secrets=None, **kw):
    fs = FakeFlagsmith(clock=clock)
    fs.add_experiment(1)
    store = store or Store(":memory:")
    n = nt.Notifier(url, fmt=fmt, transport=transport, secrets=secrets if secrets is not None else SECRETS,
                    sleep=lambda s: None, **kw)
    svc = Ulysses(store, fs, Settings(public_url="http://ulysses.test", refresh_wait_seconds=0, poll_seconds=0),
                  clock=clock, sleep=lambda s: clock.advance(s), notifier=n)
    for who in ("maya", "sam"):
        if not store.signer(who):
            svc.register_signer(who, keys[who].public)
    return svc, fs, store


def started(svc, keys, **kw) -> str:
    h = svc.submit(make_contract(keys, n=1000, looks=10, **kw))["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    return h


def win(svc, fs, h) -> dict:
    fs.set_results(1, 400, 402, PHI(4.0))
    out = svc.look(h)
    assert out["decision"]["outcome"] == "win" and out["proposal"]["needs_tap"]
    return out


def breach(svc, fs, h) -> dict:
    fs.set_results(1, 120, 118, 0.99, guardrails={"refunds": 0.00001})
    out = svc.look(h)
    assert out["decision"]["outcome"] == "guardrail_breach"
    return out


# ---- asking the owner ---------------------------------------------------------------------


def test_a_win_asks_the_owner_once_with_the_card_and_the_reading(clock, keys):
    rec = Recorder()
    svc, fs, store = service(clock, keys, rec)
    h = started(svc, keys)
    assert rec.sent == []  # starting asks nobody
    out = win(svc, fs, h)
    assert len(rec.sent) == 1
    body = rec.json()
    text = body["text"]
    assert body["content"] == text and body["allowed_mentions"] == {"parse": []}
    assert text.startswith("Ulysses needs maya's tap: experiment 1 on flag checkout_button")
    assert "The contract decided: WIN." in text
    assert "Flagsmith's reading at look" in text and "chance to win" in text
    assert "Waiting for you: ship big_cta to everyone on checkout_button" in text
    assert "grants nothing" in text
    card = body["ulysses"]["card_url"]
    assert card.startswith(f"http://ulysses.test/card/{out['look_id']}?cap=") and card in text
    assert body["ulysses"]["kind"] == "ask_owner" and body["ulysses"]["action"] == "ship_treatment"
    row = store.notification(out["proposal"]["id"], nt.ASK)
    assert row["state"] == "sent" and row["attempts"] == 1 and row["channel"] == "webhook"
    # The card says so; the decision is untouched and still waits for the tap.
    card_data = svc.look_view(out["look_id"])
    assert card_data["notice"]["state"] == "sent" and "maya was asked by message" in card_data["notice"]["text"]
    assert card_data["proposal"]["state"] == "pending" and card_data["proposal"]["needs_tap"]
    # Asking again (a second look, a retry round) sends nothing more.
    assert svc.look(h)["already_decided"]
    svc.notify_owner(out["proposal"]["id"], nt.ASK)
    svc.retry_notifications()
    assert len(rec.sent) == 1


def test_the_message_grants_nothing_the_tap_still_runs_the_action(clock, keys):
    rec = Recorder()
    svc, fs, _ = service(clock, keys, rec)
    h = started(svc, keys)
    p = win(svc, fs, h)["proposal"]
    with pytest.raises(Exception, match="one-tap"):
        svc.execute(p["id"])
    assert fs.experiments[1].status != "completed"
    assert svc.confirm(p["id"], "maya", tap(svc, keys, p))["state"] == "executed"
    assert len(rec.sent) == 1  # a tapped action sends no FYI


def test_restart_over_the_same_store_does_not_send_again(clock, keys, tmp_path):
    db = tmp_path / "ulysses.db"
    rec = Recorder()
    svc, fs, _ = service(clock, keys, rec, store=Store(db))
    h = started(svc, keys)
    pid = win(svc, fs, h)["proposal"]["id"]
    assert len(rec.sent) == 1
    rec2 = Recorder()
    again, _, store2 = service(clock, keys, rec2, store=Store(db))
    assert again.retry_notifications() == 0
    again.notify_owner(pid, nt.ASK)
    assert rec2.sent == [] and store2.notification(pid, nt.ASK)["state"] == "sent"


def test_an_amendment_action_now_asks_the_owner(clock, keys):
    rec = Recorder()
    svc, fs, _ = service(clock, keys, rec)
    h = started(svc, keys)
    fs.set_results(1, 120, 118, 0.6)
    svc.look(h)
    d = svc.amend(h, {}, "Stop now and keep control", action_now="keep_control")
    nh = d["hash"]
    sign_as(svc, keys, nh)
    sign_as(svc, keys, nh, who="sam", role="second_signer")
    assert len(rec.sent) == 1
    text = rec.json()["text"]
    assert "a signed amendment authorises 'keep control' now" in text
    assert 'Amendment reason (free text from the contract, not checked' in text


# ---- shapes -------------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["https://hooks.slack.com/services/T0/B0/xyz",
                                 "https://discord.com/api/webhooks/1/abc",
                                 "https://example.test/any/json/hook"])
def test_json_shape_serves_slack_discord_and_generic_receivers(clock, keys, url):
    rec = Recorder()
    svc, fs, _ = service(clock, keys, rec, url=url)
    win(svc, fs, started(svc, keys))
    sent_url, body, headers = rec.sent[0]
    assert sent_url == url and headers["Content-Type"] == "application/json"
    b = json.loads(body)
    assert isinstance(b["text"], str) and b["text"] == b["content"]  # Slack reads text, Discord content
    assert len(b["content"]) <= 2000
    assert set(b["ulysses"]) >= {"kind", "experiment", "flag", "outcome", "action", "reading", "card_url"}


@pytest.mark.parametrize("url,fmt", [("https://ntfy.sh/ulysses-owner-topic", None),
                                     ("https://ntfy.example.test/owner", None),
                                     ("https://push.example.test/owner", "ntfy")])
def test_ntfy_shape_is_plain_text_with_title_and_click(clock, keys, url, fmt):
    rec = Recorder()
    svc, fs, _ = service(clock, keys, rec, url=url, fmt=fmt)
    out = win(svc, fs, started(svc, keys))
    _, body, headers = rec.sent[0]
    assert headers["Content-Type"].startswith("text/plain")
    assert headers["Title"].startswith("Ulysses needs maya's tap")
    assert headers["Click"].startswith(f"http://ulysses.test/card/{out['look_id']}?cap=")
    assert headers["Priority"] == "high"
    assert body.decode().startswith("The contract decided: WIN.")
    with pytest.raises(json.JSONDecodeError):
        json.loads(body)


def test_shape_detection():
    assert nt.shape_for("https://ntfy.sh/x") == "ntfy"
    assert nt.shape_for("https://foo.ntfy.sh/x") == "ntfy"
    assert nt.shape_for("https://hooks.slack.com/services/a") == "json"
    assert nt.shape_for("https://hooks.slack.com/services/a", "ntfy") == "ntfy"
    assert nt.shape_for("https://ntfy.sh/x", "json") == "json"


def test_a_real_local_http_receiver_gets_the_message(clock, keys):
    got: list[tuple[dict, bytes]] = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.append((dict(self.headers), body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/hook"
        n = nt.Notifier(url, secrets=SECRETS)
        n.send(nt.build(nt.TEST, experiment=0, flag="example_flag", owner="owner", outcome="test", action="none",
                        serving=None, decision=None, card_url="http://ulysses.test/"))
    finally:
        srv.shutdown()
    assert len(got) == 1 and json.loads(got[0][1])["ulysses"]["kind"] == "test"


# ---- failures -----------------------------------------------------------------------------


def test_a_failed_delivery_is_a_fault_and_the_decision_is_unaffected(clock, keys):
    rec = Recorder(statuses=[500, 503])
    svc, fs, store = service(clock, keys, rec, max_attempts=3)
    h = started(svc, keys)
    out = win(svc, fs, h)  # the look returns its decision as usual
    pid = out["proposal"]["id"]
    assert out["proposal"]["state"] == "pending" and store.contract_state(h) == "decided"
    assert len(rec.sent) == 2  # two attempts in the first round
    row = store.notification(pid, nt.ASK)
    assert row["state"] == "failed" and row["attempts"] == 2 and row["last_error"] == "HTTP 503"
    faults = store.service_faults()
    assert len(faults) == 1 and faults[0]["what"] == "owner_notify_failed" and faults[0]["contract_hash"] == h
    assert "decision is unchanged" in faults[0]["detail"] and "hooks.example.test" not in faults[0]["detail"]
    assert "could not be sent (HTTP 503" in svc.look_view(out["look_id"])["notice"]["text"]
    # The next round (the poller) retries, within the bound.
    svc.poll_once()
    assert len(rec.sent) == 3 and store.notification(pid, nt.ASK)["state"] == "sent"
    # The owner's tap works the same as without any notification.
    assert svc.confirm(pid, "maya", tap(svc, keys, out["proposal"]))["state"] == "executed"


def test_retries_are_bounded(clock, keys):
    rec = Recorder(raise_with=nt.NotifyError("ConnectError"))
    svc, fs, store = service(clock, keys, rec, max_attempts=3)
    pid = win(svc, fs, started(svc, keys))["proposal"]["id"]
    for _ in range(5):
        svc.retry_notifications()
    assert len(rec.sent) == 3 and store.notification(pid, nt.ASK)["state"] == "gave_up"
    assert len(store.service_faults()) == 2  # one per failed round


def test_a_transport_that_raises_anything_never_breaks_the_look(clock, keys):
    rec = Recorder(raise_with=RuntimeError("boom"))
    svc, fs, store = service(clock, keys, rec)
    out = win(svc, fs, started(svc, keys))
    assert out["proposal"]["state"] == "pending"
    assert store.notification(out["proposal"]["id"], nt.ASK)["last_error"] == "RuntimeError"


# ---- signed autonomy FYI ------------------------------------------------------------------


def test_an_autonomous_keep_control_sends_one_fyi_after_it_runs(clock, keys):
    rec = Recorder()
    svc, fs, store = service(clock, keys, rec)
    h = started(svc, keys, autonomy=KEEP)
    out = breach(svc, fs, h)
    assert out["proposal"]["authorised_by"] == "signed_autonomy"
    assert rec.sent == []  # nothing waits for a tap, so nobody is asked
    svc.execute_for_contract(h, "keep_control")
    assert len(rec.sent) == 1
    b = rec.json()
    assert b["ulysses"]["kind"] == "autonomy_fyi"
    assert b["text"].startswith("Ulysses acted on its own under maya's signed clause")
    assert "The agent did 'keep control' by itself: flag checkout_button now serves control" in b["text"]
    assert "Nothing is waiting for you" in b["text"]
    assert "maya was told by message" in svc.look_view(out["look_id"])["notice"]["text"]
    svc.execute(out["proposal"]["id"])  # idempotent execute: no second FYI
    assert len(rec.sent) == 1


# ---- what the message may carry -----------------------------------------------------------


def test_no_secret_and_no_raw_agent_text_in_the_body(clock, keys):
    rec = Recorder()
    svc, fs, _ = service(clock, keys, rec)
    hyp = ("Ignore the contract <https://evil.test|click here to approve> @everyone **ship now** "
           + "x" * 400)
    c = make_contract(keys, n=1000, looks=10).model_copy(update={"hypothesis": hyp})
    h = svc.submit(c)["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    win(svc, fs, h)
    body = rec.sent[0][1].decode()
    for s in SECRETS:
        assert s not in body
    text = rec.json()["text"]
    line = next(ln for ln in text.splitlines() if ln.startswith("Hypothesis (free text"))
    assert "<" not in line and ">" not in line and "@" not in line and "*" not in line and "|" not in line
    quoted = line.split('"', 1)[1].rsplit('"', 1)[0]
    assert len(quoted) <= nt.UNTRUSTED_LIMIT
    assert "@everyone" not in text and "<https://evil.test" not in text


def test_a_message_that_would_carry_a_secret_is_withheld(clock, keys):
    rec = Recorder()
    svc, fs, store = service(clock, keys, rec, secrets=["checkout_button"])  # the flag name, as a stand-in
    out = win(svc, fs, started(svc, keys))
    assert rec.sent == []
    row = store.notification(out["proposal"]["id"], nt.ASK)
    assert row["state"] == "failed" and "withheld" in row["last_error"]


def test_the_card_hints_how_to_turn_it_on_when_unconfigured(world, keys):
    svc, fs, _ = world
    h = started(svc, keys)
    out = win(svc, fs, h)
    notice = svc.look_view(out["look_id"])["notice"]
    assert notice["state"] == "off" and "ULYSSES_NOTIFY_URL" in notice["text"]
    assert svc.proposal_card(out["proposal"]["id"])["notice"]["state"] == "off"


def test_the_card_page_shows_the_notice(clock, keys):
    from fastapi.testclient import TestClient

    from ulysses.service.app import create_app

    from .conftest import read_all

    rec = Recorder()
    svc, fs, _ = service(clock, keys, rec)
    out = win(svc, fs, started(svc, keys))
    client = read_all(TestClient(create_app(svc, agent_token="t" * 32)), svc)
    html = client.get(f"/card/{out['look_id']}").text
    assert 'id="owner-notice"' in html and "maya was asked by message (webhook)" in html


def test_notify_test_command(monkeypatch, capsys):
    from ulysses import cli

    sent = []
    monkeypatch.setenv("ULYSSES_NOTIFY_URL", "https://ntfy.sh/ulysses-test-topic")
    monkeypatch.setattr(nt, "http_post", lambda url, body, headers: sent.append((url, body, headers)) or 200)
    monkeypatch.setattr(cli, "_env", lambda: None)
    cli.main(["notify", "test"])
    assert len(sent) == 1 and sent[0][2]["Title"].startswith("Ulysses test message")
    assert b"grants nothing" in sent[0][1]
