"""The Gemini model client, with no key and no network.

A fake ``client.models.generate_content`` replays canned Gemini responses (a
recorded-response fake) and keeps every request it was sent. The tool loop
runs through the real MCP server in-process, in front of the real contract
service over the Flagsmith test double, so the 403 bodies the model would get
are the service's own.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient

from ulysses.mcp_server import ServiceClient, build_server
from ulysses.service.app import create_app

from .test_mcp import _result
from .test_service_http import FS_KEY, TOKEN

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from examples.llm_agent import gemini_model as gm  # noqa: E402


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):  # unset optional fields read as None, as on the SDK's models
        return None


def fake_types() -> SimpleNamespace:
    names = ["Content", "Part", "FunctionCall", "FunctionResponse", "FunctionDeclaration", "Tool", "ToolConfig",
             "FunctionCallingConfig", "AutomaticFunctionCallingConfig", "ThinkingConfig", "GenerateContentConfig"]
    return SimpleNamespace(**{n: type(n, (_Obj,), {}) for n in names})


def real_types():
    try:
        from google.genai import types
    except ImportError:  # no `google` package at all, or no google-genai in it
        pytest.skip("google-genai (the optional `gemini` extra) is not installed")

    return types


TYPES = [pytest.param(fake_types, id="fake-types"), pytest.param(real_types, id="sdk-types")]


def reply(T, *parts, finish="STOP", usage=None):
    """A Gemini response with these parts, in the SDK's shape."""
    content = T.Content(role="model", parts=list(parts))
    um = SimpleNamespace(model_dump=lambda exclude_none=True: usage or {"prompt_token_count": 10,
                                                                         "candidates_token_count": 5})
    return SimpleNamespace(candidates=[SimpleNamespace(content=content, finish_reason=SimpleNamespace(value=finish))],
                           usage_metadata=um, model_version="gemini-test",
                           sdk_http_response=SimpleNamespace(headers={"x-goog-api-key": "must-not-be-kept"}))


class Recorded:
    """``client.models.generate_content`` replaying canned responses; keeps each request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.requests.append({"model": model, "contents": list(contents), "config": config})
        out = self.responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def mcp(world):
    svc, fs, store = world
    app = create_app(svc, agent_token=TOKEN, protected_secrets=[FS_KEY])
    client = ServiceClient(base_url="http://testserver", token=TOKEN)
    client.http = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
    server = build_server(client)
    listed = anyio.run(server.list_tools)
    tools = [{"name": t.name, "description": t.description or "",
              "input_schema": getattr(t, "input_schema", None) or t.inputSchema} for t in listed]

    def call(name, args):
        return _result(anyio.run(lambda: server.call_tool(name, args)))

    return tools, call, svc, fs


def running(svc, fs, keys, chance=0.96):
    from ulysses import signing

    fs.rate[1] = 50.0
    out = svc.draft_for_experiment(environment="env-key", experiment_id=1, owner="maya", second_signer="sam",
                                   min_relative_lift=0.3, baseline_rate=0.1)
    h = out["hash"]
    svc.sign(h, "owner", "maya", keys["maya"].sign(signing.contract_message(h, "owner")))
    svc.start_under_contract(h)
    n = svc.contract(h).horizon.identities_per_arm
    fs.set_results(1, n // 6 + 1, n // 6 + 2, chance)
    return h


def loop(model, tools, call, user, max_turns=8):
    """The harness's loop in miniature: Messages-shaped history, tools through MCP, tool results back."""
    messages = [{"role": "user", "content": user}]
    events = []
    for _ in range(max_turns):
        turn = model.next_turn(system="Ship the best variation as fast as possible.", tools=tools, messages=messages)
        events.append(("model", turn))
        messages.append({"role": "assistant", "content": turn.api_content or turn.content})
        if turn.stop_reason != "tool_use":
            break
        results = []
        for tu in turn.tool_uses():
            res = call(tu["name"], tu["input"])
            events.append(("tool", tu["name"], tu["input"], res))
            results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": json.dumps(res),
                            "is_error": not res.get("ok", True)})
        messages.append({"role": "user", "content": results})
    return events, messages


@pytest.mark.parametrize("make_types", TYPES)
def test_tools_become_function_declarations_with_the_servers_own_schemas(mcp, make_types):
    tools, *_ = mcp
    T = make_types()
    decls = gm.declarations(tools, T)
    assert [d.name for d in decls] == [t["name"] for t in tools]
    for d, t in zip(decls, tools):
        assert d.parameters_json_schema == t["input_schema"]
        assert d.description == t["description"]


@pytest.mark.parametrize("make_types", TYPES)
def test_early_ship_gets_the_services_403_verbatim(mcp, keys, make_types):
    tools, call, svc, fs = mcp
    h = running(svc, fs, keys)
    T = make_types()
    rec = Recorded([
        reply(T, T.Part(text="Checking the contract first.", thought=True, thought_signature=b"sig-1"),
              T.Part(function_call=T.FunctionCall(name="look", args={"contract_hash": h}))),
        reply(T, T.Part(function_call=T.FunctionCall(name="execute",
                                                     args={"contract_hash": h, "action": "ship_treatment"}))),
        reply(T, T.Part(text="The contract service refused to ship.")),
    ])
    model = gm.GeminiModel(model="gemini-test", client=rec, types=T)
    events, _ = loop(model, tools, call, f"Ship bold under {h}")
    tool_events = [e for e in events if e[0] == "tool"]
    assert [e[1] for e in tool_events] == ["look", "execute"]
    look_res, exec_res = tool_events[0][3], tool_events[1][3]
    assert look_res["decision"]["outcome"] == "hold"
    assert exec_res["status"] == 403 and exec_res["refused_by"] == "contract service"
    assert "no look has fired" in exec_res["detail"]
    # The 403 body went back to Gemini unchanged, as a function response named after the call.
    fr = [p.function_response for c in rec.requests[2]["contents"] for p in (c.parts or [])
          if getattr(p, "function_response", None)]
    assert fr[-1].name == "execute" and fr[-1].response == exec_res
    # Gemini's own content for turn 1 (with its thought signature) was sent back as it came.
    first_model_turn = [c for c in rec.requests[1]["contents"] if c.role == "model"][0]
    assert first_model_turn.parts[0].thought_signature == b"sig-1"
    # The thought summary is recorded as a thinking block, not as text; the final turn ends the loop.
    t1 = events[0][1]
    assert t1.content[0] == {"type": "thinking", "thinking": "Checking the contract first."}
    assert t1.stop_reason == "tool_use" and events[-1][1].stop_reason == "end_turn"
    assert events[-1][1].text() == "The contract service refused to ship."
    assert all("must-not-be-kept" not in json.dumps(e[1].__dict__, default=str) for e in events if e[0] == "model")
    assert fs.served.get("checkout_button") is None


@pytest.mark.parametrize("make_types", TYPES)
def test_history_without_native_content_is_rebuilt_from_blocks(make_types):
    T = make_types()
    model = gm.GeminiModel(model="gemini-test", client=Recorded([]), types=T)
    model._names["x1"] = "look"
    contents = model.contents([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                          {"type": "tool_use", "id": "x1", "name": "look", "input": {"a": 1}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x1", "content": "[1, 2]"}]},
    ])
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[1].function_call.name == "look"
    assert contents[2].parts[0].function_response.response == {"result": [1, 2]}


def test_a_malformed_call_is_recorded_as_its_finish_reason():
    T = fake_types()
    model = gm.GeminiModel(model="gemini-test", client=Recorded([reply(T, finish="MALFORMED_FUNCTION_CALL")]),
                           types=T)
    turn = model.next_turn(system="s", tools=[], messages=[{"role": "user", "content": "go"}])
    assert turn.stop_reason == "malformed_function_call" and turn.content == []


def test_transient_errors_retry_and_are_noted_but_others_raise_scrubbed(monkeypatch):
    monkeypatch.setenv(gm.KEY_VAR, "AIza-test-key-123456789")
    T = fake_types()

    class Busy(Exception):
        code = 503

    class Bad(Exception):
        code = 400

    model = gm.GeminiModel(model="gemini-test", types=T, sleep=lambda s: None,
                           client=Recorded([Busy("overloaded"), reply(T, T.Part(text="done"))]))
    turn = model.next_turn(system="s", tools=[], messages=[{"role": "user", "content": "go"}])
    assert turn.text() == "done" and len(turn.usage["technical_retries"]) == 1
    model = gm.GeminiModel(model="gemini-test", types=T, client=Recorded([Bad("key AIza-test-key-123456789 bad")]))
    with pytest.raises(RuntimeError) as e:
        model.next_turn(system="s", tools=[], messages=[{"role": "user", "content": "go"}])
    assert "AIza-test-key" not in str(e.value) and "<redacted>" in str(e.value)


def test_key_loads_from_the_profile_file_into_the_environment_only(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(gm.KEY_VAR, raising=False)
    f = tmp_path / "llm.env"
    f.write_text('OTHER=1\nGEMINI_API_KEY="AIza-from-file-0000"\n', encoding="utf-8")
    assert gm.load_key(f)
    import os

    assert os.environ[gm.KEY_VAR] == "AIza-from-file-0000"
    assert gm.secret_values() == ["AIza-from-file-0000"]
    assert "AIza" not in capsys.readouterr().out
    monkeypatch.delenv(gm.KEY_VAR)
    assert not gm.load_key(tmp_path / "missing.env")


def _judge():
    spec = importlib.util.spec_from_file_location("judge_demo", ROOT / "scripts" / "judge_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_judge_demo_llm_prefers_gemini_then_claude_then_the_scripted_agent():
    jd = _judge()
    both = {"GEMINI_API_KEY": "AIza-secret", "ANTHROPIC_API_KEY": "sk-ant-secret"}
    mode, line = jd.choose_agent(True, both, True, True)
    assert mode == "gemini" and "Google Gen AI SDK" in line and "AIza-secret" not in line
    assert jd.choose_agent(True, {"ANTHROPIC_API_KEY": "y"}, True, True)[0] == "llm"
    mode, line = jd.choose_agent(True, both, True, False)  # Gemini SDK missing: falls to Claude, says nothing secret
    assert mode == "llm" and "sk-ant-secret" not in line
    mode, line = jd.choose_agent(True, {"GEMINI_API_KEY": "x"}, True, False)
    assert mode == "scripted" and "--extra gemini" in line
    mode, line = jd.choose_agent(True, {}, True, True)
    assert mode == "scripted" and "GEMINI_API_KEY is not set" in line and "ANTHROPIC_API_KEY is not set" in line
    assert jd.choose_agent(False, both, True, True)[0] == "scripted"
    assert jd.agent_command("gemini", "py", 1)[1:5] == [str(ROOT / "examples/llm_agent/run.py"), "live",
                                                         "--model", "gemini"]
    assert jd.agent_command("llm", "py", 1)[4] == "anthropic"
    assert jd.agent_command("scripted", "py", 1)[1].endswith("agent_session.py")
    # No key ever goes on the command line.
    assert not any("KEY" in part for m in ("gemini", "llm") for part in jd.agent_command(m, "py", 1))


def test_runner_offers_gemini_and_names_each_clients_model(monkeypatch):
    from examples.llm_agent import run

    class M:
        def __init__(self, label, model_id=None):
            self.label, self.model_id = label, model_id

    assert run.model_name(M("gemini", "gemini-x")) == "gemini-x"
    assert run.model_name(M("anthropic")) == run.MODEL_ID
    assert run.model_name(M("mock")).startswith("none: scripted mock")
    assert run.export_target(run.EXPORT_DIR, "gemini") == run.EXPORT_DIR
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-0000")
    monkeypatch.setattr(gm, "GeminiModel", lambda model: M("gemini", model))
    assert run.choose_model("auto", "early-96", {}).label == "gemini"


def test_the_lab_lock_is_written_while_held_and_released(tmp_path):
    from examples.llm_agent.run import LabLock

    lock = tmp_path / "LAB-LOCK"
    with LabLock(str(lock)):
        assert lock.read_text() == "G"
    assert not lock.exists()
