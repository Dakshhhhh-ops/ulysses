"""The MCP tools against the service (in-process), and the boundary they inherit."""

import json
from statistics import NormalDist

import anyio
import pytest
from fastapi.testclient import TestClient

from ulysses import signing
from ulysses.mcp_server import ServiceClient, build_server
from ulysses.service.app import create_app

from .conftest import tap
from .test_service_http import FS_KEY, TOKEN

PHI = NormalDist().cdf


def _result(res) -> dict:
    """Pull the JSON payload out of an MCP tool result, whatever shape the SDK returns."""
    if isinstance(res, dict):
        return res
    if isinstance(res, tuple):
        for part in res:
            if isinstance(part, dict):
                return part.get("result", part)
        res = res[0]
    if hasattr(res, "structuredContent") and res.structuredContent:
        sc = res.structuredContent
        return sc.get("result", sc)
    content = getattr(res, "content", res)
    for block in content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"no JSON in {res!r}")


@pytest.fixture
def tools(world):
    svc, fs, store = world
    app = create_app(svc, agent_token=TOKEN, protected_secrets=[FS_KEY])
    client = ServiceClient(base_url="http://testserver", token=TOKEN)
    client.http = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
    server = build_server(client)

    def call(name: str, **args) -> dict:
        return _result(anyio.run(lambda: server.call_tool(name, args)))

    return call, server, svc, fs


def test_five_action_tools_and_one_read_tool(tools):
    _, server, _, _ = tools
    listed = anyio.run(server.list_tools)
    names = sorted(t.name for t in listed)
    assert names == ["execute", "look", "read_experiment", "request_amendment", "sign_contract", "start_under_contract"]
    sign = next(t for t in listed if t.name == "sign_contract")
    # The agent does not choose how strict the rule is.
    assert not {"alpha", "looks", "identities_per_arm", "power"} & set((getattr(sign, "input_schema", None) or sign.inputSchema)["properties"])


def test_agent_session_through_the_tools(tools, keys):
    call, _, svc, fs = tools
    fs.rate[1] = 50.0
    drafted = call("sign_contract", environment="env-key", experiment_id=1, owner="maya", second_signer="sam",
                   min_relative_lift=0.3, baseline_rate=0.1)
    read = call("read_experiment", environment="env-key", experiment_id=1)
    assert read["ok"] and read["experiment"]["flag"] == "checkout_button"
    assert drafted["ok"], drafted
    h = drafted["hash"]
    assert f"/sign/{h}?cap=" in drafted["sign_url"]
    refused = call("start_under_contract", contract_hash=h)
    assert not refused["ok"] and refused["status"] == 403
    svc.sign(h, "owner", "maya", keys["maya"].sign(signing.contract_message(h, "owner")))
    assert call("start_under_contract", contract_hash=h)["ok"]
    n = svc.contract(h).horizon.identities_per_arm
    fs.set_results(1, n // 6 + 1, n // 6 + 2, 0.97)
    hold = call("look", contract_hash=h)
    assert hold["decision"]["outcome"] == "hold" and hold["card"].startswith("HOLD")
    assert "Winning" in hold["card"]
    fs.clock.advance(400)
    fs.set_results(1, n, n + 3, PHI(3.5))
    fire = call("look", contract_hash=h)
    assert fire["decision"]["outcome"] == "win" and fire["card"].startswith("FIRE: WIN")
    early = call("execute", contract_hash=h, action="ship_treatment")
    assert not early["ok"] and "confirm" in early["detail"]
    wrong = call("execute", contract_hash=h, action="keep_control")
    assert not wrong["ok"] and wrong["status"] == 403
    p = fire["proposal"]
    svc.confirm(p["id"], "maya", tap(svc, keys, p))
    done = call("execute", contract_hash=h, action="ship_treatment")
    assert done["ok"] and done["state"] == "executed"
    assert fs.served["checkout_button"] == "big_cta"
    assert all(FS_KEY not in json.dumps(x) for x in (drafted, refused, hold, fire, early, wrong, done))


def _servers(path):
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def _no_flagsmith_key(servers: dict) -> None:
    assert list(servers) == ["ulysses"], servers
    assert set(servers["ulysses"]["env"]) == {"ULYSSES_URL", "ULYSSES_AGENT_TOKEN"}
    assert "flagsmith-mcp" not in json.dumps(servers)


def test_shipped_and_generated_mcp_configs_hold_no_flagsmith_key(tmp_path):
    """Findings 2 and 21: the agent's config names only the Ulysses server and its token."""
    from argparse import Namespace
    from pathlib import Path

    from ulysses import cli

    shipped = Path(__file__).resolve().parents[1] / "mcp" / "ulysses.mcp.json"
    _no_flagsmith_key(_servers(shipped))
    cli.cmd_init(Namespace(dir=str(tmp_path), flagsmith_url="http://localhost:8000", port=8200,
                           with_flagsmith_mcp=False, uvx=False))
    _no_flagsmith_key(_servers(tmp_path / "ulysses.mcp.json"))


def test_agent_process_drops_the_flagsmith_key_even_from_a_project_env(tmp_path, monkeypatch):
    import os
    from argparse import Namespace

    from ulysses import cli, mcp_server

    (tmp_path / ".env").write_text("FLAGSMITH_ADMIN_KEY=ser.secret-admin-key\nULYSSES_AGENT_TOKEN=t" + "x" * 20
                                   + "\nULYSSES_URL=http://127.0.0.1:8200\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLAGSMITH_API_TOKEN", "inherited-from-the-shell")
    monkeypatch.delenv("ULYSSES_AGENT_TOKEN", raising=False)
    seen: dict = {}
    monkeypatch.setattr(mcp_server, "main", lambda: seen.update(os.environ))
    cli.cmd_mcp(Namespace())
    assert seen["ULYSSES_AGENT_TOKEN"].startswith("t")
    assert not [k for k in seen if k.startswith("FLAGSMITH_")]
    assert "secret-admin-key" not in json.dumps(seen)


def test_read_proxy_is_get_only(tools):
    _, _, svc, _ = tools
    c = TestClient(create_app(svc, agent_token=TOKEN, protected_secrets=[FS_KEY]),
                   headers={"Authorization": f"Bearer {TOKEN}"})
    assert c.get("/api/flagsmith/experiments/env-key/1").status_code == 200
    for method in ("post", "put", "patch", "delete"):
        assert getattr(c, method)("/api/flagsmith/experiments/env-key/1").status_code == 405


def test_amendment_tool_drafts_only_and_execute_names_the_missing_signer(tools, keys):
    call, _, svc, fs = tools
    drafted = call("sign_contract", environment="env-key", experiment_id=1, owner="maya", second_signer="sam",
                   min_relative_lift=0.3, baseline_rate=0.1)
    h = drafted["hash"]
    svc.sign(h, "owner", "maya", keys["maya"].sign(signing.contract_message(h, "owner")))
    assert call("start_under_contract", contract_hash=h)["ok"]
    amend = call("request_amendment", contract_hash=h, reason="ship now", action_now="ship_treatment")
    assert amend["ok"] and set(amend["sign_urls"]) == {"owner", "second_signer"}
    a = amend["hash"]
    svc.sign(a, "owner", "maya", keys["maya"].sign(signing.contract_message(a, "owner")))
    refused = call("execute", contract_hash=a, action="ship_treatment")
    assert refused["status"] == 403 and refused["needs"] == ["second_signer"]
    assert call("start_under_contract", contract_hash=a)["status"] == 403
    assert svc.store.contract_row(h)["state"] == "active"  # the signed contract still governs
    assert not [w for w in fs.writes if w[0] == "complete"]
    svc.sign(a, "second_signer", "sam", keys["sam"].sign(signing.contract_message(a, "second_signer")))
    assert svc.store.contract_row(h)["state"] == "superseded"


def test_every_tool_carries_mcp_annotations(tools):
    _, server, _, _ = tools
    listed = {t.name: t.annotations for t in anyio.run(server.list_tools)}
    want = {  # (readOnly, destructive, idempotent, openWorld)
        "read_experiment": (True, False, True, True),
        "execute": (False, True, True, True),
        "sign_contract": (False, False, True, False),
        "start_under_contract": (False, False, False, True),
        "look": (False, False, False, True),
        "request_amendment": (False, False, True, False),
    }
    assert set(listed) == set(want)
    for name, (ro, de, idem, ow) in want.items():
        a = listed[name]
        assert a is not None and a.title, name
        assert (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint) == (ro, de, idem, ow), name


def test_signing_link_goes_by_url_elicitation_when_the_client_supports_it(tools):
    from mcp import Client
    from mcp.types import ElicitResult

    _, server, _, fs = tools
    fs.rate[1] = 50.0
    asked = []

    async def on_elicit(context, params):
        asked.append(params)
        return ElicitResult(action="accept")

    args = {"environment": "env-key", "experiment_id": 1, "owner": "maya", "second_signer": "sam",
            "min_relative_lift": 0.3, "baseline_rate": 0.1}

    async def run(**kw):
        async with Client(server, mode="legacy", **kw) as c:
            return _result(await c.call_tool("sign_contract", args))

    out = anyio.run(lambda: run(elicitation_callback=on_elicit))
    assert out["ok"] and len(asked) == 1
    assert asked[0].mode == "url" and asked[0].url == out["sign_url"] and "cap=" in asked[0].url
    assert out["links_offered"] == [{"url": out["sign_url"], "via": "elicitation:accept"}]
    # A client without elicitation gets the same link in the result, and nothing is asked.
    plain = anyio.run(run)
    assert plain["ok"] and plain["sign_url"] and len(asked) == 1
    assert plain["links_offered"] == [{"url": plain["sign_url"], "via": "link"}]
