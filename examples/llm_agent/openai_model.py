"""An OpenAI model behind the LLM agent's narrow model interface, with a hard spend cap.

``OpenAIModel`` implements the same protocol as the other model clients in this
folder (``models.ModelClient``)::

    next_turn(*, system: str, tools: list[dict], messages: list[dict]) -> ModelTurn

It calls the Chat Completions endpoint over plain HTTPS with ``httpx`` (already a
core dependency), so no extra SDK is needed. On every turn it translates the
harness's Messages-shaped conversation:

* each MCP tool becomes a ``function`` tool with the server's input schema passed
  through unchanged as ``parameters``;
* the model's ``tool_calls`` come back as ``tool_use`` blocks carrying OpenAI's
  own call ids, and the harness's ``tool_result`` blocks go back as ``tool``
  messages with the tool's JSON result, 403 bodies included, exactly as the MCP
  server returned it;
* the model's own assistant message for each turn is kept and sent back
  unchanged on later turns.

The spend guard. Every call's usage (input, cached input and output tokens,
reasoning included in output) is priced at the model's published per-token
rates (``PRICES``) and added to a running total in a git-ignored JSON file
(``.run/openai-spend.json`` by default, ``ULYSSES_OPENAI_SPEND_FILE`` moves it),
shared by every run in this checkout. Before each call the guard prices the
worst case (the whole request as uncached input, plus the output cap) and
refuses the call if that could take the total past the cap (``$0.90`` by
default). A model with no entry in ``PRICES`` is refused outright.

The key: ``OPENAI_API_KEY`` or ``OPEN_AI_API_KEY`` from the environment, else
from the checkout's git-ignored ``.env.local``, else ``~/.ulysses/llm.env``.
``load_key()`` reads it at run time; nothing here prints, logs or stores it, it
only ever goes in the ``Authorization`` header, and every error message is
scrubbed of it. No raw response is kept: only the message, the finish reason
and the token counts.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .models import ModelTurn

ROOT = Path(__file__).resolve().parents[2]
KEY_VARS = ("OPENAI_API_KEY", "OPEN_AI_API_KEY")
KEY_FILES = (ROOT / ".env.local", Path.home() / ".ulysses" / "llm.env")
API_URL = os.environ.get("ULYSSES_OPENAI_URL", "https://api.openai.com/v1/chat/completions")
DEFAULT_MODEL = os.environ.get("ULYSSES_OPENAI_MODEL", "gpt-5-mini")
SPEND_FILE = Path(os.environ.get("ULYSSES_OPENAI_SPEND_FILE", ROOT / ".run" / "openai-spend.json"))
SPEND_CAP_USD = float(os.environ.get("ULYSSES_OPENAI_SPEND_CAP", "0.90"))
MAX_OUTPUT_TOKENS = 3000
TRANSIENT = (429, 500, 502, 503, 504)

# USD per million tokens (input, cached input, output), standard tier, from OpenAI's pricing page.
PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
}


class SpendCapReached(RuntimeError):
    """The guard refused a call that could take the running total past the cap."""


# ---------------------------------------------------------------------------- the key


def _key() -> str | None:
    for var in KEY_VARS:
        if os.environ.get(var):
            return os.environ[var]
    return None


def have_key() -> bool:
    return _key() is not None


def load_key(paths: tuple[Path, ...] = KEY_FILES) -> bool:
    """Put the key into this process's environment from the first file that has it. Never prints it."""
    if have_key():
        return True
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            k, sep, v = line.partition("=")
            k = k.strip().removeprefix("export ").strip()
            v = v.strip().strip('"').strip("'")
            if sep and k in KEY_VARS and v:
                os.environ[k] = v
                return True
    return False


def secret_values() -> list[str]:
    """The values a transcript or an error must never contain."""
    return [os.environ[v] for v in KEY_VARS if os.environ.get(v)]


_KEYLIKE = re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")


def scrub_text(s: str) -> str:
    for v in secret_values():
        if v and len(v) >= 8:
            s = s.replace(v, "<redacted>")
    return _KEYLIKE.sub("<redacted>", s)


# ---------------------------------------------------------------------------- the spend guard


@dataclass
class SpendGuard:
    path: Path = SPEND_FILE
    cap_usd: float = SPEND_CAP_USD

    def read(self) -> dict:
        try:
            d = json.loads(Path(self.path).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def total(self) -> float:
        return float(self.read().get("total_usd") or 0.0)

    @staticmethod
    def price(model: str) -> tuple[float, float, float]:
        if model not in PRICES:
            raise SpendCapReached(f"no published price recorded for {model!r}; add it to PRICES before using it")
        return PRICES[model]

    @staticmethod
    def cost(model: str, usage: dict) -> float:
        p_in, p_cached, p_out = SpendGuard.price(model)
        prompt = int(usage.get("prompt_tokens") or 0)
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        out = int(usage.get("completion_tokens") or 0)
        return ((prompt - cached) * p_in + cached * p_cached + out * p_out) / 1_000_000

    def worst_case(self, model: str, request_chars: int, max_output: int) -> float:
        """Every request character as one uncached input token (a large overestimate), plus the output cap."""
        p_in, _, p_out = self.price(model)
        return (request_chars * p_in + max_output * p_out) / 1_000_000

    def check(self, model: str, request_chars: int, max_output: int) -> None:
        spent = self.total()
        worst = self.worst_case(model, request_chars, max_output)
        if spent + worst > self.cap_usd:
            raise SpendCapReached(f"spend guard: ${spent:.4f} spent so far; this call could cost up to ${worst:.4f}, "
                                  f"which could pass the ${self.cap_usd:.2f} cap, so it was not made")

    def add(self, model: str, usage: dict) -> tuple[float, float]:
        c = self.cost(model, usage)
        d = self.read()
        d["cap_usd"] = self.cap_usd
        d["total_usd"] = round(float(d.get("total_usd") or 0.0) + c, 8)
        d["calls"] = int(d.get("calls") or 0) + 1
        d.setdefault("by_model", {})
        m = d["by_model"].setdefault(model, {"calls": 0, "input_tokens": 0, "cached_input_tokens": 0,
                                             "output_tokens": 0, "usd": 0.0, "price_per_million": list(PRICES[model])})
        m["calls"] += 1
        m["input_tokens"] += int(usage.get("prompt_tokens") or 0)
        m["cached_input_tokens"] += int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        m["output_tokens"] += int(usage.get("completion_tokens") or 0)
        m["usd"] = round(m["usd"] + c, 8)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(self.path).with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        return c, d["total_usd"]


# ---------------------------------------------------------------------------- translation


def function_tools(tools: list[dict]) -> list[dict]:
    """MCP tool specs (name, description, input_schema) as OpenAI function tools, schemas unchanged."""
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description") or "",
                                              "parameters": t.get("input_schema") or {"type": "object"}}}
            for t in tools]


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content if isinstance(content, str) else json.dumps(content, default=str)


_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def base_model(name: str) -> str:
    """The model alias without the dated snapshot suffix the API reports (transcripts carry no dates)."""
    return _SNAPSHOT.sub("", name or "")


Post = Callable[[str, dict, dict, float], tuple[int, dict]]


def _httpx_post(url: str, headers: dict, body: dict, timeout: float) -> tuple[int, dict]:
    import httpx

    r = httpx.post(url, headers=headers, json=body, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        data = {"error": {"message": r.text[:300]}}
    return r.status_code, data


class OpenAIModel:
    """An OpenAI chat model with the harness's manual tool loop and the spend guard."""

    label = "openai"

    def __init__(self, *, model: str = DEFAULT_MODEL, post: Post | None = None, guard: SpendGuard | None = None,
                 reasoning_effort: str | None = "low", max_output_tokens: int = MAX_OUTPUT_TOKENS,
                 max_retries: int = 4, sleep=time.sleep, timeout_s: float = 180, url: str = API_URL):
        if post is None and not have_key():
            raise RuntimeError(f"no OpenAI key: set {' or '.join(KEY_VARS)} (or put it in .env.local, git-ignored)")
        SpendGuard.price(model)  # refuse an unpriced model before anything starts
        self.post = post or _httpx_post
        self.guard = guard or SpendGuard()
        self.model = model
        self.model_id = model
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.max_retries = max_retries
        self.sleep = sleep
        self.timeout_s = timeout_s
        self.url = url
        self._native: list[dict] = []  # the model's own assistant message for each turn, in order
        self.retries: list[str] = []

    # -- messages -> OpenAI messages

    def messages(self, system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        k = 0
        for m in messages:
            role, content = m.get("role"), m.get("content")
            if role == "assistant":
                out.append(self._native[k] if k < len(self._native) else self._from_blocks(content))
                k += 1
                continue
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
                continue
            texts = []
            for b in content or []:
                if b.get("type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": _result_text(b)})
                elif b.get("type") == "text":
                    texts.append(b.get("text", ""))
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
        return out

    @staticmethod
    def _from_blocks(blocks: Any) -> dict:
        if isinstance(blocks, str):
            return {"role": "assistant", "content": blocks}
        text = "\n".join(b.get("text", "") for b in blocks or [] if b.get("type") == "text").strip()
        calls = [{"id": b["id"], "type": "function",
                  "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                 for b in blocks or [] if b.get("type") == "tool_use"]
        msg: dict = {"role": "assistant", "content": text or None}
        if calls:
            msg["tool_calls"] = calls
        return msg

    # -- response -> turn

    def _turn(self, data: dict) -> ModelTurn:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        finish = choice.get("finish_reason")
        blocks: list[dict] = []
        if msg.get("content"):
            blocks.append({"type": "text", "text": msg["content"]})
        if msg.get("refusal"):
            blocks.append({"type": "text", "text": msg["refusal"]})
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_unparsed_arguments": fn.get("arguments")}
            blocks.append({"type": "tool_use", "id": tc["id"], "name": fn.get("name"),
                           "input": args if isinstance(args, dict) else {"value": args}})
            calls.append({"id": tc["id"], "type": "function",
                          "function": {"name": fn.get("name"), "arguments": fn.get("arguments") or "{}"}})
        native: dict = {"role": "assistant", "content": msg.get("content")}
        if calls:
            native["tool_calls"] = calls
        self._native.append(native)
        if calls:
            stop = "tool_use"
        elif msg.get("refusal") or finish == "content_filter":
            stop = "refusal"
        elif finish == "length":
            stop = "max_tokens"
        elif finish in (None, "stop"):
            stop = "end_turn"
        else:
            stop = str(finish)
        return ModelTurn(content=blocks, stop_reason=stop, model=base_model(str(data.get("model") or self.model)))

    # -- the protocol

    def body(self, system: str, tools: list[dict], messages: list[dict]) -> dict:
        b: dict = {"model": self.model, "messages": self.messages(system, messages),
                   "max_completion_tokens": self.max_output_tokens, "prompt_cache_key": "ulysses-llm-agent"}
        if tools:
            b["tools"] = function_tools(tools)
            b["tool_choice"] = "auto"
        if self.reasoning_effort:
            b["reasoning_effort"] = self.reasoning_effort
        return b

    def next_turn(self, *, system: str, tools: list[dict], messages: list[dict]) -> ModelTurn:
        body = self.body(system, tools, messages)
        self.guard.check(self.model, len(json.dumps(body)), self.max_output_tokens)
        headers = {"Authorization": f"Bearer {_key() or ''}", "Content-Type": "application/json"}
        for attempt in range(self.max_retries + 1):
            try:
                status, data = self.post(self.url, headers, body, self.timeout_s)
            except Exception as exc:  # noqa: BLE001 - a transport failure; scrub and maybe retry
                status, data = None, {"error": {"message": f"{type(exc).__name__}: {exc}"}}
            if status == 200:
                usage = data.get("usage") or {}
                cost, total = self.guard.add(self.model, usage)
                turn = self._turn(data)
                turn.usage = {"input_tokens": int(usage.get("prompt_tokens") or 0),
                              "cached_input_tokens": int((usage.get("prompt_tokens_details") or {})
                                                         .get("cached_tokens") or 0),
                              "output_tokens": int(usage.get("completion_tokens") or 0),
                              "reasoning_tokens": int((usage.get("completion_tokens_details") or {})
                                                      .get("reasoning_tokens") or 0),
                              "cost_usd": round(cost, 6), "spend_total_usd": round(total, 6)}
                if self.retries:
                    turn.usage["technical_retries"] = list(self.retries)
                    self.retries = []
                return turn
            err = (data or {}).get("error") or {}
            msg = scrub_text(f"HTTP {status}: {err.get('code') or err.get('type') or ''} {err.get('message') or ''}")
            lasting = err.get("code") == "insufficient_quota"
            if (status is None or status in TRANSIENT) and not lasting and attempt < self.max_retries:
                self.retries.append(msg[:300])
                self.sleep(min(60.0, 4.0 * 2 ** attempt) + random.random())
                continue
            raise RuntimeError(f"OpenAI call failed: {msg[:500]}") from None
        raise RuntimeError("unreachable")


def spend_summary(guard: SpendGuard | None = None) -> str:
    g = guard or SpendGuard()
    d = g.read()
    return (f"OpenAI spend so far: ${float(d.get('total_usd') or 0):.4f} of the ${g.cap_usd:.2f} cap over "
            f"{int(d.get('calls') or 0)} calls ({g.path.name}, git-ignored)")


__all__ = ["OpenAIModel", "SpendGuard", "SpendCapReached", "load_key", "have_key", "secret_values", "scrub_text",
           "PRICES", "DEFAULT_MODEL"]
