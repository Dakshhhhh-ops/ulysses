"""The four MCP tools against the service (in-process), and the boundary they inherit."""

import json
from statistics import NormalDist

import anyio
import pytest
from fastapi.testclient import TestClient

from ulysses import signing
from ulysses.mcp_server import ServiceClient, build_server
from ulysses.service.app import create_app

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


def test_four_action_tools_and_one_read_tool(tools):
    _, server, _, _ = tools
    listed = anyio.run(server.list_tools)
    names = sorted(t.name for t in listed)
    assert names == ["execute", "look", "read_experiment", "sign_contract", "start_under_contract"]
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
    assert drafted["sign_url"].endswith(h)
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
    svc.confirm(p["id"], "maya", keys["maya"].sign(signing.confirm_message(h, p["look_id"], p["action"])))
    done = call("execute", contract_hash=h, action="ship_treatment")
    assert done["ok"] and done["state"] == "executed"
    assert fs.served["checkout_button"] == "big_cta"
    assert all(FS_KEY not in json.dumps(x) for x in (drafted, refused, hold, fire, early, wrong, done))
