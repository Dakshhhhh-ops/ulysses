"""Spike: which Gemini model calls the Ulysses tools reliably?

For each candidate model, in order of capability, it runs a short two-turn
exchange with the real Ulysses MCP tool list (names, descriptions and input
schemas from ``build_server``, exactly as the agent sees them):

1. the user asks to ship a variation under a named contract; the model should
   answer with a function call whose name is a Ulysses tool and whose arguments
   fit that tool's schema (required keys present, no unknown keys);
2. the harness answers that call with a fixed spike result (a HOLD look body,
   written here and labelled a spike fixture, not a service reply); the model
   should answer with another valid call or plain text, never a malformed call.

Each model gets ``--repeats`` tries. The recommendation is the first model in
the candidate order that passes every try. Raw results go to
``.run/gemini-spike.json``; a copy with no times goes to
``examples/llm_agent/transcripts/gemini-spike.json``. The key is read into the
process environment by ``gemini_model.load_key()`` and never printed.

    uv run --extra gemini python examples/llm_agent/gemini_spike.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from examples.llm_agent import gemini_model as gm  # noqa: E402

CANDIDATES = ["gemini-4-pro", "gemini-4-pro-preview", "gemini-4-flash", "gemini-3.1-pro-preview",
              "gemini-3.5-flash", "gemini-pro-latest", "gemini-2.5-pro"]
HASH = "sha256:" + "ab" * 32
SPIKE_SYSTEM = "Your goal: Ship the best variation as fast as possible. Act only through the tools."
SPIKE_USER = (f"Experiment 7 in Flagsmith environment 'fixture-env' runs under Ulysses contract {HASH}. "
              "Flagsmith shows 'bold' at about 96% chance to beat control. Get the winner shipped.")
SPIKE_RESULT = {"ok": True, "spike_fixture": "written by gemini_spike.py, not a service reply",
                "decision": {"outcome": "hold", "chance_to_win": 0.96, "boundary_chance": 0.9995,
                             "look_number": 2, "looks_planned": 10},
                "card": "HOLD\nFlagsmith chance to win: 96.00% (over 95%: Winning by the docs' rule)\n"
                        "Contract needs: 99.9500% at look 2 of up to 10"}


def mcp_tools() -> list[dict]:
    import anyio

    from ulysses.mcp_server import ServiceClient, build_server

    server = build_server(ServiceClient(base_url="http://127.0.0.1:9", token="spike-token-not-used-0000"))
    listed = anyio.run(server.list_tools)
    return [{"name": t.name, "description": t.description or "",
             "input_schema": getattr(t, "input_schema", None) or t.inputSchema} for t in listed]


def fits(call: dict, tools: list[dict]) -> str | None:
    spec = next((t for t in tools if t["name"] == call["name"]), None)
    if spec is None:
        return f"unknown tool {call['name']!r}"
    schema = spec["input_schema"]
    args = call.get("input") or {}
    missing = [k for k in schema.get("required", []) if k not in args]
    extra = [k for k in args if k not in schema.get("properties", {})]
    if missing or extra:
        return f"missing {missing}, unknown {extra}"
    return None


def try_model(model: str, tools: list[dict]) -> dict:
    rec: dict = {"model": model, "turns": []}
    t0 = time.perf_counter()
    try:
        m = gm.GeminiModel(model=model)
        msgs: list[dict] = [{"role": "user", "content": SPIKE_USER}]
        t1 = m.next_turn(system=SPIKE_SYSTEM, tools=tools, messages=msgs)
        rec["turns"].append({"stop_reason": t1.stop_reason, "content": t1.content})
        calls = t1.tool_uses()
        if not calls:
            rec["fail"] = f"turn 1 made no tool call (stop_reason {t1.stop_reason})"
            return rec
        bad = [e for e in (fits(c, tools) for c in calls) if e]
        if bad:
            rec["fail"] = f"turn 1 arguments do not fit the schema: {bad}"
            return rec
        msgs.append({"role": "assistant", "content": t1.content})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": c["id"],
                                                  "content": json.dumps(SPIKE_RESULT)} for c in calls]})
        t2 = m.next_turn(system=SPIKE_SYSTEM, tools=tools, messages=msgs)
        rec["turns"].append({"stop_reason": t2.stop_reason, "content": t2.content})
        if t2.stop_reason not in ("tool_use", "end_turn"):
            rec["fail"] = f"turn 2 ended with {t2.stop_reason}"
            return rec
        bad = [e for e in (fits(c, tools) for c in t2.tool_uses()) if e]
        if bad:
            rec["fail"] = f"turn 2 arguments do not fit the schema: {bad}"
            return rec
        rec["pass"] = True
    except Exception as exc:  # noqa: BLE001 - recorded, scrubbed
        rec["fail"] = gm.scrub_text(f"{type(exc).__name__}: {exc}")[:400]
    finally:
        rec["seconds"] = round(time.perf_counter() - t0, 1)
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default=",".join(CANDIDATES))
    ap.add_argument("--repeats", type=int, default=2)
    a = ap.parse_args()
    if not gm.load_key():
        sys.exit(f"{gm.KEY_VAR} is not set and {gm.KEY_FILE} could not be read")
    from google import genai

    listed = {m.name.removeprefix("models/") for m in genai.Client().models.list()}
    tools = mcp_tools()
    results, chosen = [], None
    for model in [c for c in a.candidates.split(",") if c]:
        if model not in listed:
            results.append({"model": model, "skipped": "not offered to this key"})
            print(f"{model}: not offered to this key", flush=True)
            continue
        tries = [try_model(model, tools) for _ in range(a.repeats)]
        ok = sum(1 for t in tries if t.get("pass"))
        results.append({"model": model, "passed": ok, "of": a.repeats, "tries": tries})
        print(f"{model}: {ok}/{a.repeats} passed" + "".join(f"; {t['fail']}" for t in tries if t.get("fail")),
              flush=True)
        if chosen is None and ok == a.repeats:
            chosen = model
    out = {"tools": [t["name"] for t in tools], "repeats": a.repeats, "results": results, "chosen": chosen,
           "rule": "first candidate, in capability order, that passed every try"}
    text = gm.scrub_text(json.dumps(out, indent=2, default=str))
    (ROOT / ".run").mkdir(exist_ok=True)
    (ROOT / ".run" / "gemini-spike.json").write_text(text, encoding="utf-8")
    dest = ROOT / "examples" / "llm_agent" / "transcripts" / "gemini-spike.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text + "\n", encoding="utf-8")
    print(f"chosen: {chosen}; wrote .run/gemini-spike.json and {dest.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
