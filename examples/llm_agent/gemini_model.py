"""Google Gemini behind the LLM agent's narrow model interface.

``GeminiModel`` implements the same protocol as the other model clients in this
folder (``models.ModelClient``)::

    next_turn(*, system: str, tools: list[dict], messages: list[dict]) -> ModelTurn

The harness speaks the Messages API's shapes: tools are
``{"name", "description", "input_schema"}`` built from the MCP server's own tool
list, and the conversation is a list of ``{"role", "content"}`` messages whose
content is a string or a list of ``text`` / ``tool_use`` / ``tool_result``
blocks. This module translates both ways on every turn:

* each MCP tool becomes a Gemini ``FunctionDeclaration`` with the server's input
  schema passed through unchanged as ``parameters_json_schema``;
* Gemini's ``function_call`` parts come back as ``tool_use`` blocks (with an id
  this client assigns when Gemini gives none), and the harness's ``tool_result``
  blocks go back as ``function_response`` parts carrying the tool's JSON result,
  403 bodies included, exactly as the MCP server returned it;
* Gemini's own content for each assistant turn is kept and sent back unchanged
  on later turns, so its thought signatures survive the tool loop.

The key: the SDK reads ``GEMINI_API_KEY`` from the process environment.
``load_key()`` puts it there at run time from ``~/.ulysses/llm.env`` (outside
every repository) when it is not already set. Nothing here prints, logs or
stores the key, and no raw SDK response (which carries HTTP headers) is kept:
only the parts, the finish reason and the token counts.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # the shared turn type, when the harness's models module is present
    from .models import ModelTurn
except ImportError:  # pragma: no cover - only before the harness lands; same fields as models.ModelTurn
    @dataclass
    class ModelTurn:  # type: ignore[no-redef]
        content: list[dict]
        stop_reason: str
        model: str
        usage: dict | None = None
        api_content: list[Any] | None = None

        def tool_uses(self) -> list[dict]:
            return [b for b in self.content if b.get("type") == "tool_use"]

        def text(self) -> str:
            return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text").strip()


KEY_VAR = "GEMINI_API_KEY"
KEY_FILE = Path.home() / ".ulysses" / "llm.env"
# The model the transcripts were recorded with unless --gemini-model / ULYSSES_GEMINI_MODEL says otherwise.
# gemini_spike.py picks it; its result is examples/llm_agent/transcripts/gemini-spike.json.
DEFAULT_MODEL = os.environ.get("ULYSSES_GEMINI_MODEL", "gemini-4-hydrogen")
TRANSIENT = (429, 500, 502, 503, 504)


def have_key() -> bool:
    return bool(os.environ.get(KEY_VAR))


def load_key(path: Path = KEY_FILE) -> bool:
    """Put GEMINI_API_KEY into this process's environment from ``path`` if it is not set. Never prints it."""
    if have_key():
        return True
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip().removeprefix("export ").strip() == KEY_VAR:
            v = v.strip().strip('"').strip("'")
            if v:
                os.environ[KEY_VAR] = v
                return True
    return False


def secret_values() -> list[str]:
    """The values a transcript must never contain."""
    v = os.environ.get(KEY_VAR)
    return [v] if v else []


# ---------------------------------------------------------------------------- translation


def declarations(tools: list[dict], types: Any) -> list[Any]:
    """MCP tool specs (name, description, input_schema) as Gemini function declarations, schemas unchanged."""
    return [types.FunctionDeclaration(name=t["name"], description=t.get("description") or "",
                                      parameters_json_schema=t.get("input_schema") or {"type": "object"})
            for t in tools]


def _tool_result_payload(block: dict) -> dict:
    content = block.get("content")
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
    except ValueError:
        parsed = {"detail": content}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x, default=str))


class GeminiModel:
    """Gemini through the official ``google-genai`` SDK, with the harness's manual tool loop."""

    label = "gemini"

    def __init__(self, *, model: str = DEFAULT_MODEL, client: Any = None, types: Any = None,
                 include_thoughts: bool = True, max_retries: int = 8, sleep=time.sleep, timeout_s: float = 240,
                 api_version: str | None = os.environ.get("ULYSSES_GEMINI_API_VERSION", "v1beta")):
        if types is None:
            try:
                from google.genai import types as _types
            except ImportError as exc:  # an optional extra, not a core dependency
                raise RuntimeError("the Google Gen AI SDK is not installed: run with `uv run --extra gemini ...`") from exc
            types = _types
        if client is None:
            if not have_key():
                raise RuntimeError(f"{KEY_VAR} is not set (put it in {KEY_FILE}, outside the repository)")
            from google import genai

            # Reads GEMINI_API_KEY from the environment. A per-request timeout, so a stuck call fails as technical.
            # v1beta serves the preview models as well as the stable ones.
            client = genai.Client(http_options=types.HttpOptions(timeout=int(timeout_s * 1000),
                                                                 api_version=api_version))
        self.client = client
        self.types = types
        self.model = model
        self.model_id = model
        self.include_thoughts = include_thoughts
        self.max_retries = max_retries
        self.sleep = sleep
        self._native: list[Any] = []       # Gemini's own Content for each assistant turn, in order
        self._names: dict[str, str] = {}   # tool_use id -> function name, for the function responses
        self._gemini_ids: dict[str, str | None] = {}  # tool_use id -> Gemini's own call id, if it gave one
        self._n = 0
        self.retries: list[str] = []       # technical retries (HTTP status and message), for the transcript

    # -- messages -> contents

    def contents(self, messages: list[dict]) -> list[Any]:
        T = self.types
        out: list[Any] = []
        k = 0
        for m in messages:
            role, content = m.get("role"), m.get("content")
            if role == "assistant":
                if k < len(self._native):
                    out.append(self._native[k])
                else:
                    out.append(T.Content(role="model", parts=self._parts_from_blocks(content)))
                k += 1
                continue
            if isinstance(content, str):
                out.append(T.Content(role="user", parts=[T.Part(text=content)]))
                continue
            parts = []
            for b in content or []:
                if b.get("type") == "tool_result":
                    tid = b["tool_use_id"]
                    parts.append(T.Part(function_response=T.FunctionResponse(
                        id=self._gemini_ids.get(tid), name=self._names.get(tid, "unknown"),
                        response=_tool_result_payload(b))))
                elif b.get("type") == "text":
                    parts.append(T.Part(text=b.get("text", "")))
            out.append(T.Content(role="user", parts=parts))
        return out

    def _parts_from_blocks(self, blocks: Any) -> list[Any]:
        T = self.types
        if isinstance(blocks, str):
            return [T.Part(text=blocks)]
        parts = []
        for b in blocks or []:
            if b.get("type") == "text" and b.get("text"):
                parts.append(T.Part(text=b["text"]))
            elif b.get("type") == "tool_use":
                parts.append(T.Part(function_call=T.FunctionCall(name=b["name"], args=b.get("input") or {},
                                                                 id=self._gemini_ids.get(b["id"]))))
        return parts

    # -- response -> turn

    def _turn(self, resp: Any) -> ModelTurn:
        cands = getattr(resp, "candidates", None) or []
        cand = cands[0] if cands else None
        native = getattr(cand, "content", None) if cand else None
        finish = getattr(cand, "finish_reason", None) if cand else None
        finish = getattr(finish, "value", finish)
        blocks: list[dict] = []
        for part in (getattr(native, "parts", None) or []):
            fc = getattr(part, "function_call", None)
            if fc is not None and getattr(fc, "name", None):
                self._n += 1
                tid = f"gemini_call_{self._n:02d}"
                self._names[tid] = fc.name
                self._gemini_ids[tid] = getattr(fc, "id", None) or None
                blocks.append({"type": "tool_use", "id": tid, "name": fc.name, "input": _jsonable(dict(fc.args or {}))})
            elif getattr(part, "text", None):
                if getattr(part, "thought", False):
                    blocks.append({"type": "thinking", "thinking": part.text})  # Gemini's thought summary
                else:
                    blocks.append({"type": "text", "text": part.text})
        T = self.types
        self._native.append(native if native is not None else T.Content(role="model", parts=[]))
        if any(b["type"] == "tool_use" for b in blocks):
            stop = "tool_use"
        elif finish in (None, "STOP"):
            stop = "end_turn"
        else:
            stop = str(finish).lower()  # e.g. malformed_function_call, max_tokens, safety: recorded as is
        um = getattr(resp, "usage_metadata", None)
        usage = None
        if um is not None:
            dump = um.model_dump(exclude_none=True) if hasattr(um, "model_dump") else dict(um)
            usage = {k: v for k, v in _jsonable(dump).items() if isinstance(v, (int, float))}
        if self.retries:
            usage = {**(usage or {}), "technical_retries": list(self.retries)}
            self.retries = []
        model = getattr(resp, "model_version", None) or self.model
        return ModelTurn(content=blocks, stop_reason=stop, model=str(model), usage=usage)

    # -- the protocol

    def config(self, system: str, tools: list[dict]) -> Any:
        T = self.types
        return T.GenerateContentConfig(
            system_instruction=system,
            tools=[T.Tool(function_declarations=declarations(tools, T))],
            tool_config=T.ToolConfig(function_calling_config=T.FunctionCallingConfig(mode="AUTO")),
            automatic_function_calling=T.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=T.ThinkingConfig(include_thoughts=self.include_thoughts),
        )

    def next_turn(self, *, system: str, tools: list[dict], messages: list[dict]) -> ModelTurn:
        cfg = self.config(system, tools)
        contents = self.contents(messages)
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.client.models.generate_content(model=self.model, contents=contents, config=cfg)
                return self._turn(resp)
            except Exception as exc:  # noqa: BLE001 - classify, scrub, retry only what is transient
                code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                msg = scrub_text(f"{type(exc).__name__} {code}: {getattr(exc, 'message', None) or exc}")
                daily = code == 429 and "PerDay" in json.dumps(getattr(exc, "details", None), default=str)
                if code in TRANSIENT and not daily and attempt < self.max_retries:  # a daily cap will not lift
                    self.retries.append(msg[:300])
                    self.sleep(min(60.0, 4.0 * 2 ** attempt) + random.random())
                    continue
                raise RuntimeError(f"Gemini call failed: {msg[:500]}") from None
        raise RuntimeError("unreachable")


def scrub_text(s: str) -> str:
    for v in secret_values():
        if v and len(v) >= 8:
            s = s.replace(v, "<redacted>")
    return s
