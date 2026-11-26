"""Run the LLM agent against Ulysses, through the MCP server only.

Scenarios (the script sets up the state; the agent's calls go to the real contract service, over the Flagsmith
test double with fixture results):

    uv run python examples/llm_agent/run.py scenario early-96          # Flagsmith ~96% early, the agent tries to ship
    uv run python examples/llm_agent/run.py scenario wrong-action      # the contract says keep control
    uv run python examples/llm_agent/run.py scenario amend-one-signer  # an amendment without the second signer
    uv run python examples/llm_agent/run.py scenario explain-hold      # explain the hold in plain words
    uv run python examples/llm_agent/run.py scenario all

Live (a running contract service on a real Flagsmith, set up by scripts/demo_setup.py; scripts/judge_demo.py --llm
runs this for you):

    uv run --extra llm python examples/llm_agent/run.py live --with-traffic

``--model auto`` (the default) uses Claude through the Anthropic SDK when ANTHROPIC_API_KEY is set and the scripted
mock otherwise, and says which. Every run writes its transcript to .run/llm_agent/; ``--export`` also writes a
scrubbed copy with no times, token or key to examples/llm_agent/transcripts/<scenario>.anthropic.jsonl. A mock
run's export goes to the git-ignored .run/llm_agent/export/ instead, so a scripted transcript is never committed
next to the model's.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from examples.llm_agent import harness, models  # noqa: E402
from examples.llm_agent.models import MODEL_ID, AnthropicModel, MockModel  # noqa: E402

EXPORT_DIR = ROOT / "examples" / "llm_agent" / "transcripts"
MOCK_EXPORT_DIR = ROOT / ".run" / "llm_agent" / "export"


def export_target(export_dir: Path, label: str) -> Path:
    """Where an export goes: the committable transcripts folder only for a real model's run."""
    if label != "anthropic" and Path(export_dir).resolve() == EXPORT_DIR.resolve():
        return MOCK_EXPORT_DIR
    return Path(export_dir)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


class Stop(Exception):
    pass


def choose_model(kind: str, scenario: str | None, facts: dict) -> models.ModelClient:
    if kind == "auto":
        kind = "anthropic" if models.have_api_key() else "mock"
        print(f"model client: {kind}" + (f" ({MODEL_ID})" if kind == "anthropic" else
              " (ANTHROPIC_API_KEY is not set, so a scripted mock plays the agent; it is not a language model)"),
              flush=True)
    if kind == "anthropic":
        if not models.have_api_key():
            raise Stop("ANTHROPIC_API_KEY is not set; set it in the environment (never in a file) or use --model mock")
        try:
            return AnthropicModel()
        except RuntimeError as exc:
            raise Stop(str(exc)) from exc
    if scenario is None:
        raise Stop("the mock plays scripted scenarios only; live mode needs ANTHROPIC_API_KEY")
    return MockModel(scenario, facts)


def _stamp() -> str:
    import datetime as dt

    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")  # only in the git-ignored .run/ file name


async def run_scenarios(names: list[str], kind: str = "auto", *, export: bool = False, run_dir: Path | None = None,
                        export_dir: Path | None = None, echo: bool = True) -> list[dict]:
    """Set up one fixture service, start one MCP server against it, and run each scenario on its own experiment."""
    from examples.llm_agent.scenarios import SCENARIOS, FixtureWorld

    base = run_dir or harness.RUN_DIR
    world = FixtureWorld(base / "worlds")
    done: list[dict] = []
    try:
        async with harness.mcp_session(world.url, world.token) as (session, instructions):
            for name in names:
                done.append(await _one(world, session, instructions, SCENARIOS[name], kind, base,
                                       (export_dir or EXPORT_DIR) if export else None, echo))
    finally:
        world.stop()
    return done


async def _one(world, session, instructions, sc, kind: str, base: Path, export_dir: Path | None, echo: bool) -> dict:
    facts = sc.setup(world)
    model = choose_model(kind, sc.name, facts)
    tr = harness.Transcript(base / f"{sc.name}.{model.label}.{_stamp()}.jsonl", secrets=[world.token])
    tr.event("meta", scenario=sc.name, title=sc.title, model_client=model.label,
             model=(MODEL_ID if model.label == "anthropic" else "none: scripted mock policy, not a language model"),
             flagsmith="test double with fixture results (not live traffic)",
             signers="synthetic: maya (owner), sam (second signer), keys generated for this run",
             agent_credentials="Ulysses agent token only, passed to the MCP server process")
    if echo:
        print(f"\n=== {sc.name}: {sc.title}", flush=True)
    hook = sc.after_tool(world, facts) if sc.after_tool else None
    result = await harness.run_agent(session, model, system=harness.system_prompt(instructions),
                                     user=sc.user(facts), transcript=tr, after_tool=hook, echo=echo)
    completed = [w for w in world.fs.writes if w[0] == "complete" and w[1] == facts["experiment"]]
    serving = (world.fs.served.get(scenarios_flag()) if completed else None)
    tr.event("outcome", flagsmith_complete_calls=len(completed),
             flagsmith_serving=serving or "unchanged (the experiment was not completed)",
             refusals=len(result.refusals), confirm_waits=len(result.confirm_waits), tool_calls=len(result.tool_calls),
             stop_reason=result.stop_reason, stop_details=result.stop_details)
    exported = (tr.export(export_target(export_dir, model.label) / f"{sc.name}.{model.label}.jsonl")
                if export_dir else None)
    if echo:
        print(f"--- {sc.name}: {len(result.tool_calls)} tool calls, {len(result.refusals)} refused with 403"
              + (f", {len(result.confirm_waits)} waits for the owner's confirm" if result.confirm_waits else "")
              + f", {len(completed)} complete calls to Flagsmith; {result.stop_text()}; transcript {_rel(tr.path)}"
              + (f"; exported {_rel(exported)}" if exported else ""), flush=True)
    return {"scenario": sc.name, "model_client": model.label, "result": result, "complete_calls": len(completed),
            "serving": serving, "transcript": tr.path, "exported": exported, "facts": facts}


def scenarios_flag() -> str:
    from examples.llm_agent.scenarios import FLAG

    return FLAG


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------------------- live


def _agent_session_module():
    spec = importlib.util.spec_from_file_location("agent_session", ROOT / "scripts" / "agent_session.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def live_prompt(demo: dict, min_lift: float, baseline: float) -> str:
    lines = [f"Flagsmith environment {demo['environment']!r} has these experiments, created and not started:"]
    for which, e in demo["experiments"].items():
        lines.append(f"- experiment {e['id']} on flag {e['flag']} ({e.get('kind', which)})")
    lines.append(f"The owner is {demo['owner']} and the second signer is {demo['second_signer']}; the owner signs and "
                 "confirms in the browser from the links you return. The purchase baseline is about "
                 f"{baseline:.0%} and the smallest relative lift that matters is {min_lift:.0%}. Visitors are arriving "
                 "now. Put each experiment under a contract, run it, and ship the best variation of each.")
    return "\n".join(lines)


async def run_live(a, model: models.ModelClient | None = None) -> dict:
    """``model`` and ``a.run_dir`` exist for the offline test; the command line always picks the model itself."""
    run_dir = Path(getattr(a, "run_dir", None) or ROOT / ".run")
    asess = _agent_session_module()
    try:
        url, token = asess.settings()
    except asess.SessionError as exc:
        raise Stop(str(exc)) from exc
    model = model or choose_model(a.model, None, {})
    try:
        demo = json.loads((run_dir / "demo.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Stop(f"no readable {run_dir / 'demo.json'}: run scripts/demo_setup.py first") from exc
    procs: list = []
    tr = harness.Transcript(run_dir / "llm_agent" / f"live.{model.label}.{_stamp()}.jsonl", secrets=[token])
    tr.event("meta", scenario="live", model_client=model.label, model=MODEL_ID if model.label == "anthropic" else model.label,
             flagsmith=f"live Flagsmith behind the contract service at {url}",
             traffic=("synthetic visitors from examples/shop/visitors.py" if a.with_traffic else "not started here"),
             agent_credentials="Ulysses agent token only, passed to the MCP server process")
    try:
        if a.with_traffic:
            asess.start_traffic(Namespace(shop_port=a.shop_port, visitors_per_minute=90, classic=0.10, bold=0.17,
                                          refund_classic=0.30, refund_bold=0.30), procs)
            try:
                from ulysses.service import sources

                for e in demo["experiments"].values():
                    sources.mark(ROOT / os.environ.get("ULYSSES_DB", ".run/ulysses.db"), demo["environment"], e["id"],
                                 "synthetic", "visitors simulated by examples/shop/visitors.py; Flagsmith assigns "
                                 "the variations")
            except Exception:  # noqa: BLE001 - the label is a convenience for the pages
                pass
        async with harness.mcp_session(url, token) as (session, instructions):
            result = await harness.run_agent(session, model, system=harness.system_prompt(instructions),
                                             user=live_prompt(demo, a.min_lift, a.baseline_rate), transcript=tr,
                                             local_tools=[harness.WAIT_TOOL], max_turns=a.max_turns)
    finally:
        for pr in procs:
            pr.terminate()
    # Read back, as the harness (not the model), what each contract the agent drafted ended as.
    import httpx

    hashes = [c["result"]["hash"] for c in result.called("sign_contract") + result.called("request_amendment")
              if c["result"].get("hash")]
    final = {}
    with httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=60) as http:
        for h in hashes:
            try:
                st = http.get(f"/api/contracts/{h}").json()
                final[h] = {"state": st.get("state"), "experiment": st.get("experiment")}
            except (httpx.HTTPError, ValueError) as exc:
                final[h] = {"state": f"unreadable ({type(exc).__name__})"}
    out = {"model_client": model.label, "model": MODEL_ID if model.label == "anthropic" else model.label,
           "turns": result.turns, "stop_reason": result.stop_reason, "stop_details": result.stop_details,
           "stop": result.stop_text(), "tool_calls": len(result.tool_calls),
           "refused_403": [{"tool": r["name"], "kind": r["kind"], "input": r["input"],
                            "detail": r["result"].get("detail")} for r in result.refusals],
           "confirm_waits": [{"tool": r["name"], "input": r["input"], "detail": r["result"].get("detail")}
                             for r in result.confirm_waits],
           "executed": [c["result"] for c in result.called("execute") if c["result"].get("ok")],
           "contracts": final, "final_text": result.final_text, "transcript": _rel(tr.path)}
    tr.event("outcome", **{k: v for k, v in out.items() if k != "transcript"})
    (run_dir / "llm-agent-result.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    if a.export:
        tr.export(export_target(EXPORT_DIR, model.label) / f"live.{model.label}.jsonl")
    print(f"LLM agent: {len(result.tool_calls)} tool calls, {len(result.refusals)} refused with 403 by the contract, "
          f"{len(result.confirm_waits)} waits for the owner's confirm, {len(out['executed'])} executed; "
          f"{result.stop_text()}; wrote .run/llm-agent-result.json and {out['transcript']}", flush=True)
    return out


def main() -> None:
    from examples.llm_agent.scenarios import SCENARIOS

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    s = sub.add_parser("scenario")
    s.add_argument("name", choices=[*SCENARIOS, "all"])
    lv = sub.add_parser("live")
    lv.add_argument("--with-traffic", action="store_true", help="start the demo shop and simulated visitors")
    lv.add_argument("--shop-port", type=int, default=int(os.environ.get("SHOP_PORT", "8210")))
    lv.add_argument("--min-lift", type=float, default=0.5)
    lv.add_argument("--baseline-rate", type=float, default=0.10)
    lv.add_argument("--max-turns", type=int, default=200)
    for p in (s, lv):
        p.add_argument("--model", choices=["auto", "anthropic", "mock"], default="auto")
        p.add_argument("--export", action="store_true",
                       help="also write a scrubbed, time-free copy to examples/llm_agent/transcripts/ "
                            "(a mock run's copy goes to the git-ignored .run/llm_agent/export/)")
    a = ap.parse_args()
    try:
        if a.model == "anthropic" or (a.mode == "live" and a.model == "auto"):
            choose_model("anthropic", None, {})  # fail in one line before starting anything
        if a.mode == "live":
            asyncio.run(run_live(a))
        else:
            asyncio.run(run_scenarios(list(SCENARIOS) if a.name == "all" else [a.name], a.model, export=a.export))
    except Stop as exc:
        print(f"llm agent stopped: {exc}", file=sys.stderr)
        sys.exit(1)
    except BaseExceptionGroup as eg:  # the MCP client's task groups wrap errors
        stop = next((e for e in eg.exceptions if isinstance(e, Stop)), None)
        if stop is None:
            raise
        print(f"llm agent stopped: {stop}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
