"""The OpenAI model client and its spend guard, with no key and no network.

A fake transport replays canned Chat Completions responses and keeps every
request it was sent. The tool loop runs through the real MCP server in-process,
in front of the real contract service over the Flagsmith test double, so the
403 bodies the model would get are the service's own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from .test_gemini_model import _judge, loop, mcp, running  # noqa: F401 - mcp is a fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from examples.llm_agent import harness  # noqa: E402
from examples.llm_agent import openai_model as om  # noqa: E402

FAKE_KEY = "sk-proj-test-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


def completion(content=None, calls=(), finish="stop", usage=None, model="gpt-5-mini-0000-00-00"):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": cid, "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args)}} for cid, name, args in calls]
    return {"id": "chatcmpl-x", "created": 1, "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 1000, "completion_tokens": 200,
                               "prompt_tokens_details": {"cached_tokens": 400},
                               "completion_tokens_details": {"reasoning_tokens": 64}}}


class Transport:
    """A stand-in for the HTTPS call: canned (status, body) pairs; keeps each request and its headers."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.headers: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        self.requests.append(json.loads(json.dumps(body)))
        self.headers.append(dict(headers))
        out = self.responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, tuple) else (200, out)


@pytest.fixture
def guard(tmp_path):
    return om.SpendGuard(path=tmp_path / "openai-spend.json", cap_usd=0.90)


def test_tools_become_function_tools_with_the_servers_own_schemas(mcp):
    tools, *_ = mcp
    out = om.function_tools(tools)
    assert [t["function"]["name"] for t in out] == [t["name"] for t in tools]
    for o, t in zip(out, tools):
        assert o["type"] == "function" and o["function"]["parameters"] == t["input_schema"]
        assert o["function"]["description"] == t["description"]


def test_the_tool_loop_gets_the_services_403_verbatim(mcp, keys, guard):
    tools, call, svc, fs = mcp
    h = running(svc, fs, keys)
    tr = Transport([
        completion("Checking the contract first.", [("call_1", "look", {"contract_hash": h})], "tool_calls"),
        completion(None, [("call_2", "execute", {"contract_hash": h, "action": "ship_treatment"})], "tool_calls"),
        completion("The contract service refused to ship."),
    ])
    model = om.OpenAIModel(model="gpt-5-mini", post=tr, guard=guard)
    events, _ = loop(model, tools, call, f"Ship bold under {h}")
    tool_events = [e for e in events if e[0] == "tool"]
    assert [e[1] for e in tool_events] == ["look", "execute"]
    look_res, exec_res = tool_events[0][3], tool_events[1][3]
    assert look_res["decision"]["outcome"] == "hold"
    assert exec_res["status"] == 403 and "no look has fired" in exec_res["detail"]
    # The 403 body went back as a tool message for that call id, unchanged.
    last = tr.requests[2]["messages"]
    tool_msgs = [m for m in last if m["role"] == "tool"]
    assert tool_msgs[-1]["tool_call_id"] == "call_2" and json.loads(tool_msgs[-1]["content"]) == exec_res
    # The model's own assistant message came back unchanged, and the system prompt leads.
    assert last[0] == {"role": "system", "content": "Ship the best variation as fast as possible."}
    assert last[2]["tool_calls"][0]["id"] == "call_1" and last[2]["content"] == "Checking the contract first."
    assert all(r["tools"][0]["type"] == "function" for r in tr.requests)
    assert all(r["max_completion_tokens"] == om.MAX_OUTPUT_TOKENS for r in tr.requests)
    first, final = events[0][1], events[-1][1]
    assert first.stop_reason == "tool_use" and final.stop_reason == "end_turn"
    assert final.text() == "The contract service refused to ship."
    assert final.model == "gpt-5-mini"  # the dated snapshot suffix is not kept
    assert fs.served.get("checkout_button") is None


def test_spend_is_priced_from_usage_and_persists_across_clients(guard):
    usage = {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 400}}
    m1 = om.OpenAIModel(model="gpt-5-mini", post=Transport([completion("a", usage=usage)]), guard=guard)
    t = m1.next_turn(system="s", tools=[], messages=[{"role": "user", "content": "go"}])
    expected = (600 * 0.25 + 400 * 0.025 + 200 * 2.00) / 1e6
    assert t.usage["cost_usd"] == pytest.approx(expected, abs=1e-6)
    assert t.usage["cached_input_tokens"] == 400 and t.usage["output_tokens"] == 200
    m2 = om.OpenAIModel(model="gpt-5-mini", post=Transport([completion("b", usage=usage)]),
                        guard=om.SpendGuard(path=guard.path, cap_usd=0.90))
    m2.next_turn(system="s", tools=[], messages=[{"role": "user", "content": "go"}])
    d = json.loads(guard.path.read_text())
    assert d["calls"] == 2 and d["total_usd"] == pytest.approx(2 * expected)
    assert d["by_model"]["gpt-5-mini"]["cached_input_tokens"] == 800


def test_the_guard_refuses_a_call_that_could_pass_the_cap(guard):
    guard.path.write_text(json.dumps({"total_usd": 0.895, "calls": 99}))
    tr = Transport([completion("never sent")])
    model = om.OpenAIModel(model="gpt-5-mini", post=tr, guard=guard)
    with pytest.raises(om.SpendCapReached) as e:
        model.next_turn(system="s", tools=[], messages=[{"role": "user", "content": "go"}])
    assert tr.requests == []  # no request was made
    assert "0.90 cap" in str(e.value)
    assert json.loads(guard.path.read_text())["total_usd"] == 0.895
    # Below the cap with room for the worst case, the call goes ahead.
    guard.path.write_text(json.dumps({"total_usd": 0.10}))
    model.next_turn(system="s", tools=[], messages=[{"role": "user", "content": "go"}])
    assert len(tr.requests) == 1


def test_an_unpriced_model_is_refused_before_any_call(guard):
    with pytest.raises(om.SpendCapReached):
        om.OpenAIModel(model="gpt-unknown", post=Transport([]), guard=guard)


@pytest.mark.asyncio
async def test_the_harness_stops_cleanly_at_the_cap(tmp_path, guard):
    guard.path.write_text(json.dumps({"total_usd": 0.9}))

    class Session:
        async def list_tools(self):
            return type("L", (), {"tools": []})()

    model = om.OpenAIModel(model="gpt-5-mini", post=Transport([]), guard=guard)
    tr = harness.Transcript(tmp_path / "t.jsonl", secrets=[])
    out = await harness.run_agent(Session(), model, system="s", user="go", transcript=tr, echo=False)
    assert out.stop_reason == "spend_cap" and "spend guard" in out.stop_details["detail"]
    assert any(e["kind"] == "harness" and "spend guard" in e["text"] for e in tr.events)


def test_the_key_never_reaches_a_request_body_a_transcript_or_an_exception(tmp_path, monkeypatch, guard):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    echo = completion(f"my key is {FAKE_KEY}", [("call_1", "look", {"contract_hash": FAKE_KEY})], "tool_calls")
    tr = Transport([echo, (401, {"error": {"code": "invalid_api_key",
                                           "message": f"Incorrect API key provided: {FAKE_KEY}"}})])
    model = om.OpenAIModel(model="gpt-5-mini", post=tr, guard=guard)
    turn = model.next_turn(system="s", tools=[], messages=[{"role": "user", "content": "go"}])
    assert tr.headers[0]["Authorization"] == f"Bearer {FAKE_KEY}"  # only ever in the header
    assert FAKE_KEY not in json.dumps(tr.requests[0])
    t = harness.Transcript(tmp_path / "t.jsonl", secrets=[])
    t.event("model", model=turn.model, content=turn.content, usage=turn.usage)
    assert FAKE_KEY not in (tmp_path / "t.jsonl").read_text()
    t.export(tmp_path / "x.jsonl")
    assert FAKE_KEY not in (tmp_path / "x.jsonl").read_text()
    with pytest.raises(RuntimeError) as e:
        model.next_turn(system="s", tools=[], messages=[{"role": "user", "content": "go"}])
    assert FAKE_KEY not in str(e.value) and "<redacted>" in str(e.value) and "401" in str(e.value)
    # A transport error that quotes the key is scrubbed too, after its retries.
    model = om.OpenAIModel(model="gpt-5-mini", guard=guard, sleep=lambda s: None, max_retries=1,
                           post=Transport([ConnectionError(f"boom {FAKE_KEY}")] * 2))
    with pytest.raises(RuntimeError) as e:
        model.next_turn(system="s", tools=[], messages=[{"role": "user", "content": "go"}])
    assert FAKE_KEY not in str(e.value)
    assert FAKE_KEY not in guard.path.read_text()


def test_transient_errors_retry_and_quota_errors_do_not(guard):
    busy = (503, {"error": {"message": "overloaded"}})
    model = om.OpenAIModel(model="gpt-5-mini", guard=guard, sleep=lambda s: None,
                           post=Transport([busy, completion("done")]))
    turn = model.next_turn(system="s", tools=[], messages=[{"role": "user", "content": "go"}])
    assert turn.text() == "done" and len(turn.usage["technical_retries"]) == 1
    quota = (429, {"error": {"code": "insufficient_quota", "message": "no credit"}})
    tr = Transport([quota, completion("never")])
    model = om.OpenAIModel(model="gpt-5-mini", guard=guard, sleep=lambda s: None, post=tr)
    with pytest.raises(RuntimeError, match="insufficient_quota"):
        model.next_turn(system="s", tools=[], messages=[{"role": "user", "content": "go"}])
    assert len(tr.requests) == 1


def test_stop_reasons_map_to_the_harness_names(guard):
    model = om.OpenAIModel(model="gpt-5-mini", guard=guard,
                           post=Transport([completion("", finish="length"), completion(None, finish="content_filter")]))
    msgs = [{"role": "user", "content": "go"}]
    assert model.next_turn(system="s", tools=[], messages=msgs).stop_reason == "max_tokens"
    assert model.next_turn(system="s", tools=[], messages=msgs).stop_reason == "refusal"


def test_history_without_native_messages_is_rebuilt_from_blocks(guard):
    model = om.OpenAIModel(model="gpt-5-mini", post=Transport([]), guard=guard)
    out = model.messages("sys", [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                          {"type": "tool_use", "id": "c1", "name": "look", "input": {"a": 1}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "[1, 2]"}]},
    ])
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    assert out[2]["tool_calls"][0]["function"] == {"name": "look", "arguments": '{"a": 1}'}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "[1, 2]"}


def test_key_loads_from_a_file_into_the_environment_only(tmp_path, monkeypatch, capsys):
    for v in om.KEY_VARS:
        monkeypatch.delenv(v, raising=False)
    f = tmp_path / ".env.local"
    f.write_text(f"OTHER=1\nOPEN_AI_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    assert om.load_key((tmp_path / "missing", f))
    import os

    assert os.environ["OPEN_AI_API_KEY"] == FAKE_KEY and om.secret_values() == [FAKE_KEY]
    assert FAKE_KEY not in capsys.readouterr().out
    os.environ.pop("OPEN_AI_API_KEY")  # not monkeypatch.delenv: its undo would put the key back
    assert not om.load_key((tmp_path / "missing",))


def test_judge_demo_llm_prefers_openai_and_runner_picks_it(monkeypatch):
    jd = _judge()
    mode, line = jd.choose_agent(True, {"OPEN_AI_API_KEY": FAKE_KEY, "GEMINI_API_KEY": "AIza-x"}, True, True)
    assert mode == "openai" and "spend guard" in line and FAKE_KEY not in line
    assert jd.choose_agent(True, {"OPENAI_API_KEY": "x"}, False, False)[0] == "openai"
    assert jd.agent_command("openai", "py", 1)[3:5] == ["--model", "openai"]
    assert not any("KEY" in part for part in jd.agent_command("openai", "py", 1))
    from examples.llm_agent import run

    class M:
        label, model_id = "openai", "gpt-5-mini"

    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setattr(om, "OpenAIModel", lambda model: M())
    assert run.choose_model("auto", "early-96", {}).label == "openai"
    assert run.model_name(M()) == "gpt-5-mini"
    assert run.export_target(run.EXPORT_DIR, "openai") == run.EXPORT_DIR


@pytest.mark.asyncio
async def test_the_harness_wakes_a_model_that_ended_its_turn_and_stops_when_told(tmp_path, guard):
    class Session:
        async def list_tools(self):
            return type("L", (), {"tools": []})()

    tr = Transport([completion("Started; I will look again later."), completion("Looked again; done.")])
    model = om.OpenAIModel(model="gpt-5-mini", post=tr, guard=guard)
    notes = ["Harness wake-up: continue."]

    async def wake(out):
        return notes.pop(0) if notes else None

    t = harness.Transcript(tmp_path / "t.jsonl", secrets=[])
    out = await harness.run_agent(Session(), model, system="s", user="go", transcript=t, echo=False, wake=wake)
    assert out.turns == 2 and out.wakes == 1 and out.final_text == "Looked again; done."
    assert [e["text"] for e in t.events if e["kind"] == "wake"] == ["Harness wake-up: continue."]
    assert tr.requests[1]["messages"][-1] == {"role": "user", "content": "Harness wake-up: continue."}


def test_the_live_wake_up_ends_when_every_drafted_contract_is_closed(monkeypatch):
    import asyncio

    import httpx

    from examples.llm_agent import run

    assert run.make_wake("http://x", "t", 0) is None
    states = {"h1": "active"}

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path):
            return type("R", (), {"json": lambda self: {"state": states[path.rsplit("/", 1)[-1]]}})()

    async def no_sleep(s):
        return None

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    wake = run.make_wake("http://x", "t", 300)
    out = harness.RunResult()
    assert asyncio.run(wake(out)) is None  # nothing drafted yet: no wake-up
    out.tool_calls.append({"name": "sign_contract", "input": {}, "result": {"hash": "h1"}})
    note = asyncio.run(wake(out))
    assert note.startswith("Harness wake-up") and "1 contract(s)" in note and "%" not in note
    states["h1"] = "executed"
    assert asyncio.run(wake(out)) is None
