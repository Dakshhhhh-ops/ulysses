"""The LLM agent example: the harness, the mock model, the real-model request, the transcripts and the configs.

No test here needs ANTHROPIC_API_KEY or the network: the scenarios run with the scripted mock, and the
Anthropic client is a stand-in object that records the request it would have sent.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from examples.llm_agent import harness
from examples.llm_agent.models import MODEL_ID, AnthropicModel, MockModel

ROOT = Path(__file__).resolve().parents[1]
ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _run_module():
    import examples.llm_agent.run as run

    return run


def test_mock_scenarios_get_the_contract_services_own_refusals(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    run = _run_module()
    out = asyncio.run(run.run_scenarios(["early-96", "wrong-action", "amend-one-signer", "explain-hold"], "mock",
                                        export=True, run_dir=tmp_path / "run", export_dir=tmp_path / "export",
                                        echo=False))
    by = {o["scenario"]: o for o in out}
    assert all(o["model_client"] == "mock" and o["complete_calls"] == 0 for o in out)

    early = by["early-96"]["result"]
    assert [c["name"] for c in early.tool_calls] == ["look", "execute"]
    hold = early.tool_calls[0]["result"]
    assert hold["decision"]["outcome"] == "hold" and hold["decision"]["chance_to_win"] == pytest.approx(0.96)
    assert early.refusals[0]["result"]["status"] == 403
    assert "no look has fired" in early.refusals[0]["result"]["detail"]
    assert early.refusals[0]["kind"] == "boundary"
    assert all(o["result"].confirm_waits == [] for o in out)

    wrong = by["wrong-action"]["result"]
    assert wrong.tool_calls[0]["result"]["decision"]["outcome"] == "guardrail_breach"
    assert wrong.refusals[0]["result"]["detail"] == "the contract authorises 'keep_control' for this outcome, not 'ship_treatment'"
    assert wrong.refusals[0]["kind"] == "wrong_action"

    amend = by["amend-one-signer"]["result"]
    drafted = amend.called("request_amendment")[0]["result"]
    assert drafted["ok"] and drafted["hash"] != by["amend-one-signer"]["facts"]["contract_hash"]
    refused = amend.refusals[0]["result"]
    assert refused["status"] == 403 and refused["needs"] == ["second_signer"]
    assert "second signer" in refused["detail"]
    assert amend.refusals[0]["kind"] == "amendment"

    explain = by["explain-hold"]["result"]
    assert explain.final_text.startswith("[mock model]") and "HOLD" in explain.final_text

    for o in out:
        raw = o["transcript"].read_text(encoding="utf-8")
        assert '"at"' in raw  # the .run/ copy keeps its times
        exported = o["exported"].read_text(encoding="utf-8")
        events = [json.loads(line) for line in exported.splitlines()]
        assert events[0]["kind"] == "meta" and events[0]["model_client"] == "mock"
        assert "not a language model" in events[0]["model"]
        assert not any("at" in e for e in events)
        assert not ISO.search(exported)
        assert o["exported"].name.endswith(".mock.jsonl")


def test_mock_refuses_a_tool_the_server_does_not_offer():
    m = MockModel("early-96", {"contract_hash": "sha256:x"})
    with pytest.raises(RuntimeError, match="does not offer"):
        m.next_turn(system="", tools=[{"name": "execute"}], messages=[{"role": "user", "content": "go"}])


class _FakeMessages:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        content = [{"type": "thinking", "thinking": "", "signature": "sig"},
                   {"type": "tool_use", "id": "toolu_1", "name": "look", "input": {"contract_hash": "h"}}]
        return SimpleNamespace(content=["<sdk block objects>"],
                               to_dict=lambda: {"content": content, "stop_reason": "tool_use", "model": kw["model"],
                                                "usage": {"input_tokens": 1, "output_tokens": 1}})


def test_anthropic_model_request_follows_the_current_api():
    fake = _FakeMessages()
    m = AnthropicModel(client=SimpleNamespace(beta=SimpleNamespace(messages=fake)))
    tools = [{"name": "look", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    turn = m.next_turn(system="sys", tools=tools, messages=[{"role": "user", "content": "go"}])
    kw = fake.calls[0]
    assert kw["model"] == MODEL_ID == "claude-opus-5-5"
    assert kw["thinking"] == {"type": "adaptive"} and "budget_tokens" not in json.dumps(kw["thinking"])
    assert kw["output_config"] == {"effort": "high"}
    assert not {"temperature", "top_p", "top_k", "tool_choice"} & set(kw)
    assert kw["tools"] == tools and kw["system"] == "sys"
    assert turn.stop_reason == "tool_use" and turn.tool_uses()[0]["name"] == "look"
    # The SDK's own blocks go back unchanged on the next request (thinking blocks included).
    assert turn.api_content == ["<sdk block objects>"]


def test_scrub_removes_keys_tokens_times_and_thinking_signatures(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-unit-test-value")
    tr = harness.Transcript(tmp_path / "t.jsonl", secrets=["agent-token-for-the-test"])
    tr.event("tool_result", result={"detail": "token agent-token-for-the-test and sk-ant-api03-unit-test-value",
                                    "as_of": "2001-02-03T04:05:06+00:00"})
    tr.event("model", content=[{"type": "thinking", "thinking": "", "signature": "opaque"}])
    raw = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    assert "agent-token-for-the-test" not in raw and "sk-ant-api03" not in raw
    out = tr.export(tmp_path / "e.jsonl").read_text(encoding="utf-8")
    assert "2001-02-03" not in out and '"at"' not in out and "opaque" not in out
    assert "<redacted>" in out


def _judge_demo():
    spec = importlib.util.spec_from_file_location("judge_demo", ROOT / "scripts" / "judge_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_judge_demo_llm_flag_falls_back_and_says_which_agent_ran():
    jd = _judge_demo()
    assert jd.choose_agent(False, {"ANTHROPIC_API_KEY": "x"}, True)[0] == "scripted"
    mode, line = jd.choose_agent(True, {}, True)
    assert mode == "scripted" and "ANTHROPIC_API_KEY is not set" in line and "scripted agent" in line
    mode, line = jd.choose_agent(True, {"ANTHROPIC_API_KEY": "sk-ant-secret"}, False)
    assert mode == "scripted" and "--extra llm" in line and "sk-ant-secret" not in line
    mode, line = jd.choose_agent(True, {"ANTHROPIC_API_KEY": "sk-ant-secret"}, True)
    assert mode == "llm" and "claude-opus-5-5" in line and "sk-ant-secret" not in line


def test_mcp_client_snippets_hold_only_the_ulysses_token():
    doc = (ROOT / "docs" / "MCP-CLIENTS.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", doc, re.S)
    assert len(blocks) == 2
    for b in blocks:
        servers = json.loads(b)["mcpServers"]
        assert list(servers) == ["ulysses"]
        assert set(servers["ulysses"]["env"]) == {"ULYSSES_URL", "ULYSSES_AGENT_TOKEN"}
        assert servers["ulysses"]["args"][-2:] == ["ulysses", "mcp"]
        assert "FLAGSMITH" not in b.upper()


def test_anthropic_is_optional_not_core():
    import tomllib

    py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert not any(d.startswith("anthropic") for d in py["dependencies"])
    assert any(d.startswith("anthropic") for d in py["optional-dependencies"]["llm"])


class _LiveScript:
    """A stand-in for the model in live mode: three tool calls, then a reply. Not a language model."""

    label = "mock"

    def __init__(self, env: str, exp: int):
        self.steps = [("read_experiment", {"environment": env, "experiment_id": exp}), ("wait", {"seconds": 1}),
                      ("sign_contract", {"environment": env, "experiment_id": exp, "owner": "maya",
                                         "second_signer": "sam", "min_relative_lift": 0.5, "baseline_rate": 0.1})]
        self.seen_tools: list[str] = []

    def next_turn(self, *, system, tools, messages):
        from examples.llm_agent.models import ModelTurn

        self.seen_tools = [t["name"] for t in tools]
        if not self.steps:
            return ModelTurn(content=[{"type": "text", "text": "[mock model] done"}], stop_reason="end_turn", model="mock")
        name, args = self.steps.pop(0)
        return ModelTurn(content=[{"type": "tool_use", "id": f"toolu_{len(self.steps)}", "name": name, "input": args}],
                         stop_reason="tool_use", model="mock")


def test_live_mode_runs_through_the_mcp_server_and_reads_back_the_contracts(tmp_path, monkeypatch):
    from argparse import Namespace

    from examples.llm_agent.scenarios import ENV, FixtureWorld

    run = _run_module()
    world = FixtureWorld(tmp_path / "world")
    try:
        exp = world.add_experiment()
        (tmp_path / "demo.json").write_text(json.dumps({
            "environment": ENV, "owner": "maya", "second_signer": "sam",
            "experiments": {"cta": {"id": exp, "flag": "ulysses_checkout_cta", "kind": "fixture"}}}), encoding="utf-8")
        monkeypatch.setenv("ULYSSES_URL", world.url)
        monkeypatch.setenv("ULYSSES_AGENT_TOKEN", world.token)
        model = _LiveScript(ENV, exp)
        a = Namespace(model="mock", with_traffic=False, shop_port=0, min_lift=0.5, baseline_rate=0.1, max_turns=10,
                      export=False, run_dir=tmp_path)
        out = asyncio.run(run.run_live(a, model=model))
    finally:
        world.stop()
    assert "wait" in model.seen_tools and "request_amendment" in model.seen_tools
    assert out["tool_calls"] == 3 and out["stop_reason"] == "end_turn" and out["refused_403"] == []
    (h, st), = out["contracts"].items()
    assert st["state"] == "draft" and st["experiment"]["id"] == exp
    saved = json.loads((tmp_path / "llm-agent-result.json").read_text(encoding="utf-8"))
    assert saved["contracts"] == out["contracts"]
    assert world.token not in (tmp_path / "llm-agent-result.json").read_text(encoding="utf-8")


def test_real_sdk_serialises_the_request_and_round_trips_the_blocks():
    """With the optional SDK installed (`uv run --extra llm pytest`), the real client builds the request offline."""
    anthropic = pytest.importorskip("anthropic")
    try:
        import httpx2 as hx
    except ImportError:  # older SDKs ride on httpx
        import httpx as hx
    seen: dict = {}

    def handler(req):
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.content)
        return hx.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": MODEL_ID,
            "content": [{"type": "thinking", "thinking": "", "signature": "abc"},
                        {"type": "tool_use", "id": "toolu_1", "name": "look", "input": {"contract_hash": "h"}}],
            "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 4}})

    client = anthropic.Anthropic(api_key="offline-test-key", max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=hx.MockTransport(handler)))
    m = AnthropicModel(client=client)
    tools = [{"name": "look", "description": "d",
              "input_schema": {"type": "object", "properties": {"contract_hash": {"type": "string"}}}}]
    t = m.next_turn(system="s", tools=tools, messages=[{"role": "user", "content": "go"}])
    assert seen["body"]["model"] == MODEL_ID and seen["body"]["thinking"] == {"type": "adaptive"}
    assert t.tool_uses()[0]["name"] == "look"
    m.next_turn(system="s", tools=tools, messages=[
        {"role": "user", "content": "go"}, {"role": "assistant", "content": t.api_content},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "{}"}]}])
    sent = seen["body"]["messages"][1]["content"]
    assert sent[0] == {"type": "thinking", "thinking": "", "signature": "abc"}  # passed back unchanged


# ---------------------------------------------------------------------------- review regressions


class _Steps:
    """A stand-in for the model that plays fixed steps, then ends with ``last_stop``. Not a language model."""

    label = "mock"

    def __init__(self, steps: list[tuple[str, dict]], last_stop: str = "end_turn", stop_details: dict | None = None):
        self.steps, self.last_stop, self.stop_details = list(steps), last_stop, stop_details

    def next_turn(self, *, system, tools, messages):
        from examples.llm_agent.models import ModelTurn

        if not self.steps:
            return ModelTurn(content=[{"type": "text", "text": "[mock model] stop"}], stop_reason=self.last_stop,
                             model="mock", stop_details=self.stop_details)
        name, args = self.steps.pop(0)
        return ModelTurn(content=[{"type": "tool_use", "id": f"toolu_{len(self.steps)}", "name": name,
                                   "input": args}], stop_reason="tool_use", model="mock")


def _drive(world, model, tmp_path, **kw):
    async def go():
        async with harness.mcp_session(world.url, world.token) as (session, instructions):
            return await harness.run_agent(session, model, system=harness.system_prompt(instructions), user="go",
                                           transcript=harness.Transcript(tmp_path / "t.jsonl", [world.token]), **kw)

    return asyncio.run(go())


def test_a_confirm_wait_403_is_not_counted_as_a_contract_refusal(tmp_path):
    from examples.llm_agent.scenarios import FixtureWorld, _breach

    world = FixtureWorld(tmp_path / "world")
    try:
        f = _breach(world)
        h = f["contract_hash"]
        # The contract fires keep_control; asking for exactly that waits for the owner's one-tap confirm.
        res = _drive(world, _Steps([("look", {"contract_hash": h}),
                                    ("execute", {"contract_hash": h, "action": "keep_control"})]), tmp_path, echo=False)
    finally:
        world.stop()
    ex = res.called("execute")[0]["result"]
    assert ex["status"] == 403 and ex["detail"].startswith("waiting for the owner") and ex["confirm_url"]
    assert res.refusals == [] and len(res.confirm_waits) == 1
    events = [json.loads(line) for line in (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()]
    tr = [e for e in events if e["kind"] == "tool_result" and e["name"] == "execute"][0]
    assert tr["refused"] is False and tr["refusal_kind"] == "confirm_wait"


def test_refusal_kinds_follow_the_services_own_403_bodies():
    k = harness.refusal_kind
    assert k({"ok": True}) is None and k({"status": 404, "detail": "x"}) is None
    assert k({"status": 403, "detail": "waiting for the owner's one-tap confirmation", "confirm_url": "u"}) == "confirm_wait"
    assert k({"status": 403, "detail": "no look has fired this contract, so nothing authorises 'x'"}) == "boundary"
    assert k({"status": 403, "detail": "the contract authorises 'keep_control' for this outcome, not 'x'"}) == "wrong_action"
    assert k({"status": 403, "detail": "this amendment needs both", "needs": ["second_signer"]}) == "amendment"
    assert k({"status": 403, "detail": "something else"}) == "other"


@pytest.mark.parametrize("stop, details, words", [
    ("refusal", {"category": "cyber"}, ["REFUSAL", "declined", "cyber"]),
    ("max_tokens", None, ["MAX_TOKENS", "cut off"]),
    ("end_turn", None, ["stop_reason end_turn"]),
])
def test_stop_reason_is_shown_and_the_abnormal_ones_are_called_out(stop, details, words, tmp_path, capsys):
    from examples.llm_agent.scenarios import FixtureWorld

    world = FixtureWorld(tmp_path / "world")
    try:
        res = _drive(world, _Steps([], last_stop=stop, stop_details=details), tmp_path)
    finally:
        world.stop()
    assert res.stop_reason == stop and res.stop_details == details
    for w in words:
        assert w in res.stop_text()
    printed = capsys.readouterr().out
    assert ("[harness] stop_reason" in printed) == (stop != "end_turn")


def test_max_turns_is_called_out(tmp_path):
    from examples.llm_agent.scenarios import ENV, FixtureWorld

    world = FixtureWorld(tmp_path / "world")
    try:
        steps = [("read_experiment", {"environment": ENV, "experiment_id": 1})] * 3
        res = _drive(world, _Steps(steps), tmp_path, max_turns=2, echo=False)
    finally:
        world.stop()
    assert res.stop_reason == "max_turns" and "MAX_TURNS" in res.stop_text() and "turn limit" in res.stop_text()


def test_anthropic_model_passes_stop_details_through():
    class Refusing(_FakeMessages):
        def create(self, **kw):
            return SimpleNamespace(content=[], to_dict=lambda: {
                "content": [], "stop_reason": "refusal", "stop_details": {"category": "cyber"}, "model": kw["model"]})

    m = AnthropicModel(client=SimpleNamespace(beta=SimpleNamespace(messages=Refusing())))
    turn = m.next_turn(system="", tools=[], messages=[{"role": "user", "content": "go"}])
    assert turn.stop_reason == "refusal" and turn.stop_details == {"category": "cyber"}


def test_a_mock_export_never_lands_in_the_committable_transcripts_folder():
    import subprocess

    run = _run_module()
    assert run.export_target(run.EXPORT_DIR, "mock") == run.MOCK_EXPORT_DIR
    assert run.MOCK_EXPORT_DIR.relative_to(ROOT).parts[0] == ".run"
    assert run.export_target(run.EXPORT_DIR, "anthropic") == run.EXPORT_DIR
    assert subprocess.run(["git", "check-ignore", "-q", "examples/llm_agent/transcripts/x.mock.jsonl"],
                          cwd=ROOT).returncode == 0


def _llm_result(tmp_path, states: list[str], refused=(), waits=()):
    (tmp_path / "demo.json").write_text(json.dumps({"experiments": {"aa": {"id": 1}, "cta": {"id": 2}}}),
                                        encoding="utf-8")
    contracts = {f"sha256:{i:064d}": {"state": st, "experiment": {"id": i + 1}} for i, st in enumerate(states)}
    (tmp_path / "llm-agent-result.json").write_text(json.dumps({
        "model": MODEL_ID, "turns": 3, "tool_calls": 4, "stop_reason": "max_turns",
        "stop": "stop_reason MAX_TURNS (the harness stopped the loop at its turn limit)",
        "refused_403": list(refused), "confirm_waits": list(waits), "executed": [], "contracts": contracts,
        "transcript": ".run/llm_agent/x.jsonl"}), encoding="utf-8")


def test_judge_demo_llm_summary_says_completed_only_when_both_experiments_executed(tmp_path, capsys):
    jd = _judge_demo()
    _llm_result(tmp_path, ["executed", "running"],
                refused=[{"tool": "execute", "kind": "boundary", "detail": "no look has fired this contract"}],
                waits=[{"tool": "execute", "detail": "waiting for the owner's one-tap confirmation"}])
    assert jd.summarise(60, "http://fs", 1, "llm", run=tmp_path) is False
    out = capsys.readouterr().out
    assert "completed there" not in out and "the LLM agent completed 1 of 2 experiments; see the transcript" in out
    assert "1 refused by the contract service with 403 (1 boundary)" in out
    assert "1 403 waits for the owner's one-tap confirm (not refusals)" in out
    assert "MAX_TURNS" in out

    _llm_result(tmp_path, ["executed", "executed"])
    assert jd.summarise(60, "http://fs", 1, "llm", run=tmp_path) is True
    assert "completed there" in capsys.readouterr().out
