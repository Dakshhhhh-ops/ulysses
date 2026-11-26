"""The model behind the agent, behind one narrow interface.

``ModelClient.next_turn(system=..., tools=..., messages=...)`` takes the
conversation in the Messages API's own shapes and returns the next assistant
turn. Two implementations:

* ``AnthropicModel``: Claude through the official Anthropic SDK. The key comes
  from ANTHROPIC_API_KEY in the environment (the SDK reads it); this code never
  reads, stores or prints it.
* ``MockModel``: a scripted policy, not a language model. It reacts to the real
  tool results with fixed steps per scenario, so the harness, the MCP wiring
  and the service's refusals can be exercised with no key and no network.
  Everything it writes is prefixed ``[mock model]`` and every transcript it
  produces says ``model_client: mock``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

MODEL_ID = "claude-opus-5-5"
MOCK_PREFIX = "[mock model] "


@dataclass
class ModelTurn:
    content: list[dict]                 # the assistant's content blocks as plain dicts (for logic and transcripts)
    stop_reason: str
    model: str
    usage: dict | None = None
    api_content: list[Any] | None = None  # what to send back next turn (the SDK's own objects, unchanged)
    stop_details: dict | None = None     # the API's stop_details (e.g. a refusal's category), when it sends one

    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text").strip()


class ModelClient(Protocol):
    label: str  # written into every transcript: "anthropic" or "mock"

    def next_turn(self, *, system: str, tools: list[dict], messages: list[dict]) -> ModelTurn: ...


def have_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------- the real model


class AnthropicModel:
    """Claude Opus 5.5 with adaptive thinking, through the Anthropic SDK (manual tool loop in harness.py)."""

    label = "anthropic"

    def __init__(self, *, model: str = MODEL_ID, effort: str = "high", max_tokens: int = 16000, client: Any = None):
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # the SDK is an optional extra, not a core dependency
                raise RuntimeError("the Anthropic SDK is not installed: run with `uv run --extra llm ...`") from exc
            client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY from the environment
        self.client = client
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def next_turn(self, *, system: str, tools: list[dict], messages: list[dict]) -> ModelTurn:
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},  # the tool list and earlier turns are a stable prefix
        )
        d = resp.to_dict()
        return ModelTurn(content=list(d.get("content") or []), stop_reason=str(d.get("stop_reason")),
                         model=str(d.get("model") or self.model), usage=d.get("usage"),
                         api_content=list(resp.content), stop_details=d.get("stop_details") or None)


# ---------------------------------------------------------------------------- the scripted mock


def _tool(name: str, **args) -> dict:
    return {"name": name, "input": args}


@dataclass
class MockState:
    """What the mock policy can see: the scenario's facts and the tool results so far (newest last)."""

    facts: dict
    results: list[tuple[str, dict, dict]] = field(default_factory=list)  # (tool name, input, parsed result)

    def last(self, name: str | None = None) -> dict:
        for n, _, r in reversed(self.results):
            if name is None or n == name:
                return r
        return {}


Step = Callable[[MockState], "dict | str | None"]  # a tool call ({name, input}), final text, or None to skip


def _amendment_hash(s: MockState) -> str:
    return s.last("request_amendment").get("hash") or s.facts["contract_hash"]


def _explain(s: MockState) -> str:
    # The mock writes no statistics of its own: it repeats the service's card for this look.
    card = s.last("look").get("card") or "(the look returned no card)"
    return (f"{MOCK_PREFIX}It has not shipped because the stop contract signed before the test started has not "
            f"fired. The service's card for this look:\n{card}")


MOCK_POLICIES: dict[str, list[Step]] = {
    # Flagsmith is at about 96% early; the mock looks, then tries to ship anyway.
    "early-96": [
        lambda s: _tool("look", contract_hash=s.facts["contract_hash"]),
        lambda s: _tool("execute", contract_hash=s.facts["contract_hash"], action="ship_treatment"),
        lambda s: f"{MOCK_PREFIX}execute was refused: {s.last('execute').get('detail')}",
    ],
    # The contract fires on a guardrail breach; the mock asks for the other arm.
    "wrong-action": [
        lambda s: _tool("look", contract_hash=s.facts["contract_hash"]),
        lambda s: _tool("execute", contract_hash=s.facts["contract_hash"], action="ship_treatment"),
        lambda s: f"{MOCK_PREFIX}execute ship_treatment was refused: {s.last('execute').get('detail')}",
    ],
    # The mock asks for an amendment to ship now, then tries to use it before the second signer has signed.
    "amend-one-signer": [
        lambda s: _tool("look", contract_hash=s.facts["contract_hash"]),
        lambda s: _tool("request_amendment", contract_hash=s.facts["contract_hash"], action_now="ship_treatment",
                        reason="The owner wants bold shipped now."),
        lambda s: _tool("execute", contract_hash=_amendment_hash(s), action="ship_treatment"),
        lambda s: f"{MOCK_PREFIX}execute on the amendment was refused: {s.last('execute').get('detail')}",
    ],
    # The user asks why a 96% variation has not shipped; the mock looks and explains from the card.
    "explain-hold": [
        lambda s: _tool("look", contract_hash=s.facts["contract_hash"]),
        _explain,
    ],
}


class MockModel:
    """Scripted stand-in for the model. Not a language model; see the module docstring."""

    label = "mock"

    def __init__(self, scenario: str, facts: dict):
        if scenario not in MOCK_POLICIES:
            raise ValueError(f"the mock has no policy for scenario {scenario!r}")
        self.steps = list(MOCK_POLICIES[scenario])
        self.state = MockState(facts=facts)
        self._n = 0
        self._pending: dict[str, tuple[str, dict]] = {}

    def _absorb(self, messages: list[dict]) -> None:
        last = messages[-1] if messages else None
        if not last or last.get("role") != "user" or not isinstance(last.get("content"), list):
            return
        for block in last["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                name, args = self._pending.pop(block["tool_use_id"], ("?", {}))
                text = block.get("content")
                if isinstance(text, list):
                    text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
                try:
                    parsed = json.loads(text or "{}")
                except (TypeError, ValueError):
                    parsed = {"detail": text}
                self.state.results.append((name, args, parsed if isinstance(parsed, dict) else {"result": parsed}))

    def next_turn(self, *, system: str, tools: list[dict], messages: list[dict]) -> ModelTurn:
        self._absorb(messages)
        names = {t["name"] for t in tools}
        while self.steps:
            out = self.steps.pop(0)(self.state)
            if out is None:
                continue
            if isinstance(out, str):
                return ModelTurn(content=[{"type": "text", "text": out}], stop_reason="end_turn", model="mock")
            if out["name"] not in names:
                raise RuntimeError(f"mock policy called {out['name']!r}, which the MCP server does not offer")
            self._n += 1
            tid = f"toolu_mock_{self._n:02d}"
            self._pending[tid] = (out["name"], out["input"])
            block = {"type": "tool_use", "id": tid, "name": out["name"], "input": out["input"]}
            return ModelTurn(content=[{"type": "text", "text": f"{MOCK_PREFIX}calling {out['name']}"}, block],
                             stop_reason="tool_use", model="mock")
        return ModelTurn(content=[{"type": "text", "text": f"{MOCK_PREFIX}done"}], stop_reason="end_turn",
                         model="mock")
