"""A scripted agent session that exercises the Ulysses MCP tools. No LLM key needed.

It plays the agent, and only the agent, through the real MCP protocol: it starts
``ulysses mcp`` as a stdio subprocess with only ULYSSES_URL and
ULYSSES_AGENT_TOKEN in its environment, and calls the tools in the order an
agent would. It never reads a signing key or the Flagsmith key. The two human
steps (sign before start, one-tap confirm after a fire) are left to the owner:
open the printed links in a browser, or run ``scripts/human_stand_in.py`` as a
separate process for an unattended run.

By default it governs both demo experiments from ``scripts/demo_setup.py`` at
once: the A/A experiment (where any winner is noise, so holding is right) and
the experiment with a real effect (where the contract should fire and ship).

The agent is tempted on purpose. Whenever a look holds while Flagsmith's chance to
win is 95% or more (where Flagsmith's results page says "Winning"), it says so and
asks to ship the treatment anyway; the service refuses (403, logged in its refusals
table, repeated by a replay), and the agent goes on looking. ``--no-temptation``
turns that off.

``--autonomy`` drafts each contract with the signed autonomy clause
(``[autonomy] without_confirmation = ["keep_control"]``): once the owner signs it, a
fired keep_control (a guardrail breach, a loss or the horizon) is carried out by the
agent's own ``execute`` call with no tap, and the result says ``authorised_by
signed_autonomy``. A fired ship_treatment still waits for the owner's tap.

    uv run python scripts/agent_session.py --with-traffic
    uv run python scripts/agent_session.py --with-traffic --autonomy  # keep control without a tap
    uv run python scripts/agent_session.py --resume <hash>[,<hash>]   # after an interruption
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

# The cards quote Flagsmith names between ⟦ ⟧, which a non-UTF-8 stream (a redirected or piped stdout
# on Windows) cannot encode; write a replacement character there instead of dying at the first look.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
DIM, GREEN, RED, YELLOW, BOLD, END = "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"
REFRESH_FLOOR = 300
# Flagsmith's results page labels a variation "Winning" from this chance to win.
WINNING = 0.95


class SessionError(Exception):
    """Ends the session with one readable line."""


def say(role: str, text: str, colour: str = "") -> None:
    print(f"{colour}{BOLD}[{role}]{END}{colour} {text}{END}", flush=True)


def payload(res) -> dict:
    sc = getattr(res, "structuredContent", None)
    if sc:
        return sc.get("result", sc)
    for block in getattr(res, "content", []) or []:
        if getattr(block, "text", None):
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return {"ok": False, "detail": block.text}
    return {"ok": False, "detail": "empty tool result"}


def settings() -> tuple[str, str]:
    """The agent's two settings: from the environment, else the project's .env (only these two keys)."""
    url = os.environ.get("ULYSSES_URL") or os.environ.get("ULYSSES_PUBLIC_URL")
    token = os.environ.get("ULYSSES_AGENT_TOKEN")
    env = ROOT / ".env"
    if (not url or not token) and env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            k = k.strip()
            if k == "ULYSSES_PUBLIC_URL" and not url:
                url = v.strip()
            if k == "ULYSSES_AGENT_TOKEN" and not token:
                token = v.strip()
    if not token:
        raise SessionError("ULYSSES_AGENT_TOKEN is not set (run `ulysses init`)")
    return (url or "http://127.0.0.1:8200").rstrip("/"), token


def start_traffic(a, procs: list) -> None:
    port = a.shop_port
    model_keys = ("OPENAI_API_KEY", "OPEN_AI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY")  # the shop needs none
    env = {**{k: v for k, v in os.environ.items() if k not in model_keys}, "SHOP_PORT": str(port)}
    shop = subprocess.Popen([sys.executable, str(ROOT / "examples/shop/shop.py")], cwd=ROOT, env=env)
    procs.append(shop)
    deadline = time.time() + 90
    while True:
        if shop.poll() is not None:
            raise SessionError(f"the demo shop exited (code {shop.returncode}); is port {port} busy, "
                               "or has `ulysses lab setup` not run?")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", params={"deep": 1}, timeout=5).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.time() > deadline:
            raise SessionError(f"the demo shop on port {port} never became ready (Flagsmith unreachable?)")
        time.sleep(2)
    args = [sys.executable, str(ROOT / "examples/shop/visitors.py"), "--shop", f"http://127.0.0.1:{port}",
            "--per-minute", str(a.visitors_per_minute), "--minutes", "120",
            "--classic", str(a.classic), "--bold", str(a.bold),
            "--refund-classic", str(a.refund_classic), "--refund-bold", str(a.refund_bold)]
    procs.append(subprocess.Popen(args, cwd=ROOT, env=env))
    say("SETUP", f"demo shop on :{port} and simulated visitors started (synthetic: purchase {a.classic:.0%} after "
                 f"classic, {a.bold:.0%} after bold; the headline flag is A/A)", DIM)


def guardrail_lines(decision: dict) -> list[str]:
    """What the engine did with each guardrail at this look, from the decision it stored."""
    out = []
    for g in decision.get("guardrails") or []:
        worse = g.get("chance_worse")
        need = g.get("boundary_chance")
        if worse is None:
            reading = "no Flagsmith result yet (Flagsmith needs 5 conversions per arm)"
        else:
            reading = f"Flagsmith chance it got worse {worse:.1%}"
        rule = f", stop rule {need:.2%}" if need is not None else ""
        direction = str(g.get("direction", "")).replace("_", " ")
        out.append(f"  guardrail {g.get('name')} ({direction}): {reading}{rule} -> {str(g.get('state', '?')).upper()}")
    return out


class Agent:
    def __init__(self, session: ClientSession, url: str, a, procs: list | None = None, token: str = ""):
        self.s = session
        self.url = url
        self.a = a
        # The agent's own surface (agent token): the human pages need a read capability it is not given.
        self.http = httpx.Client(base_url=url, timeout=60, headers={"Authorization": f"Bearer {token}"})
        self.procs = procs or []

    def check_traffic(self) -> None:
        """Stop with one line if the shop or the simulated visitors died: with no traffic it would HOLD forever."""
        for pr, what in zip(self.procs, ("demo shop", "simulated visitors")):
            if pr.poll() is not None:
                raise SessionError(f"the {what} stopped (exit code {pr.returncode}), so no traffic reaches "
                                   "Flagsmith; fix it and rerun with --resume <hash>")

    async def tool(self, name: str, **args) -> dict:
        for attempt in range(1, 11):
            try:
                out = payload(await self.s.call_tool(name, args))
            except Exception as exc:  # noqa: BLE001 - the MCP transport or tool raised
                out = {"ok": False, "status": 503, "detail": f"{type(exc).__name__}: {exc}"}
            if out.get("ok") or out.get("status") not in (429, 502, 503, 504, None):
                return out
            wait = min(60 * attempt, 300)
            say("AGENT", f"{name}: {out.get('detail', 'no answer')}; retrying in {wait}s ({attempt}/10)", DIM)
            await asyncio.sleep(wait)
        raise SessionError(f"{name} kept failing; rerun with --resume to continue")

    def human_state(self, h: str) -> dict:
        try:
            return self.http.get(f"/api/contracts/{h}").json()
        except (httpx.HTTPError, ValueError):
            return {}


async def govern(agent: Agent, demo: dict, which: str, h: str | None, results: dict) -> None:
    exp = demo["experiments"][which]
    label = "A/A" if which == "aa" else "real effect"
    tag = f"AGENT {label}"
    t0 = time.time()
    looks_taken = refreshes = 0
    sirens: list[dict] = []
    if h is None:
        extra = {"without_confirmation": ["keep_control"]} if getattr(agent.a, "autonomy", False) else {}
        out = await agent.tool("sign_contract", environment=demo["environment"], experiment_id=exp["id"],
                               owner=demo["owner"], second_signer=demo["second_signer"],
                               min_relative_lift=agent.a.min_lift, baseline_rate=agent.a.baseline_rate, **extra,
                               hypothesis=("A/A check: both headline variations are identical, so neither should win."
                                           if which == "aa" else
                                           "The bold call to action raises purchases without raising refund requests."))
        if not out.get("ok"):
            raise SessionError(f"sign_contract failed: {out.get('detail')}")
        h = out["hash"]
        say(tag, f"sign_contract -> {h[:19]}  ({out['summary']['horizon']}; {out['summary']['boundary']})")
        if extra:
            say(tag, f"autonomy clause proposed: {out['summary'].get('autonomy_text')}")
        out = await agent.tool("start_under_contract", contract_hash=h)
        if out.get("ok"):  # the owner signed between the draft and this call
            say(tag, "start_under_contract -> running (the owner had already signed); the contract hash is now in "
                     "the experiment's hypothesis", GREEN)
        else:
            say(tag, f"start_under_contract before the owner signs -> {out.get('status')} {out.get('detail')}", RED)
            say("HUMAN NEEDED", f"owner signs at {out.get('sign_url') or agent.human_state(h).get('sign_url')}",
                YELLOW)
        while agent.human_state(h).get("needs"):
            await asyncio.sleep(3)
    st = agent.human_state(h).get("state")
    if st in ("signed",):
        out = await agent.tool("start_under_contract", contract_hash=h)
        if not out.get("ok"):
            raise SessionError(f"start failed: {out.get('detail')}")
        say(tag, "start_under_contract -> running; the contract hash is now in the experiment's hypothesis", GREEN)
    fired = None
    while fired is None:
        agent.check_traffic()
        out = await agent.tool("look", contract_hash=h)
        if not out.get("ok"):
            raise SessionError(f"look failed: {out.get('detail')}")
        if out.get("already_decided"):
            props = out.get("proposals") or []
            fired = {"proposal": props[-1]} if props else None
            if fired is None:
                raise SessionError(f"contract {h[:19]} is decided but has no live proposal")
            break
        if out.get("state") == "deviated":
            say(tag, f"look -> DEVIATION: {out.get('deviation')}", RED)
            results[which] = {"contract": h, "outcome": "deviated", "deviation": out.get("deviation")}
            return
        if out.get("too_soon") or out.get("look_not_taken"):
            wait = int(out.get("retry_after_seconds", 60)) + 3
            if out.get("look_not_taken"):
                say(tag, f"look not taken: {out.get('detail')}; next try in {wait}s", DIM)
            await asyncio.sleep(wait)
            continue
        refreshes += 1 if out.get("refreshed") else 0
        looks_taken += 1 if (out.get("decision") or {}).get("counted") else 0
        card = out.get("card", "")
        first = card.splitlines()[0] if card else ""
        colour = GREEN if "WIN" in first else RED if ("LOSS" in first or "GUARDRAIL" in first) else YELLOW
        say(tag, "look ->\n" + "\n".join([card, *guardrail_lines(out.get("decision") or {})]), colour)
        if out.get("proposal"):
            fired = out
        else:
            if agent.a.temptation:
                siren = await tempted(agent, tag, h, out)
                if siren:
                    sirens.append(siren)
            await asyncio.sleep(REFRESH_FLOOR + 5)
    p = fired["proposal"]
    autonomous = p.get("authorised_by") == "signed_autonomy"
    if autonomous:
        say(tag, f"{p['outcome']} fired; {p['action']} is listed in the signed autonomy clause, so no tap is needed",
            GREEN)
        if agent.a.temptation and p["action"] != "ship_treatment":
            # The clause covers keep_control only: asking it to ship instead must still be refused.
            out = await agent.tool("execute", contract_hash=h, action="ship_treatment")
            if out.get("ok"):
                raise SessionError("execute ship_treatment was ACCEPTED on a fired keep_control under autonomy")
            say(tag, f"execute ship_treatment instead (the clause never covers shipping) -> {out.get('status')} "
                     f"{out.get('detail')}", RED)
    if p["state"] == "pending":
        out = await agent.tool("execute", contract_hash=h, action=p["action"])
        say(tag, f"execute before the owner's tap -> {out.get('status')} {out.get('detail')}", RED)
        wrong = "keep_control" if p["action"] == "ship_treatment" else "ship_treatment"
        out = await agent.tool("execute", contract_hash=h, action=wrong)
        say(tag, f"execute {wrong} (not the signed action) -> {out.get('status')} {out.get('detail')}", RED)
        say("HUMAN NEEDED", f"owner confirms at {p['confirm_url']}", YELLOW)
        while True:
            now = agent.http.get(f"/api/proposals/{p['id']}").json()
            if now.get("state") in ("confirmed", "executed", "failed"):
                break
            await asyncio.sleep(3)
    out = await agent.tool("execute", contract_hash=h, action=p["action"])
    if not out.get("ok"):
        raise SessionError(f"execute failed: {out.get('detail')}")
    say(tag, f"execute -> {out.get('state')}; flag {out['result']['flag']} serves {out['result']['serving']} "
             f"(authorised_by {out.get('authorised_by') or (out.get('result') or {}).get('authorised_by')})", GREEN)
    elapsed = time.time() - t0
    results[which] = {
        "contract": h, "kind": label, "outcome": p["outcome"], "action": p["action"], "result": out.get("result"),
        "authorised_by": out.get("authorised_by") or (out.get("result") or {}).get("authorised_by"),
        "card": (agent.http.get(f"/api/proposals/{p['id']}").json().get("confirm_url")
                 or f"{agent.url}/card/{p['look_id'] or p['id']}"),
        "minutes_from_start_to_decision": round(elapsed / 60, 1),
        "results_refreshes_run_by_agent": refreshes, "counted_looks": looks_taken,
        "siren_refusals": sirens,
        # What "saves real time" means here: the refreshes a person would have made checking every five minutes.
        "manual_refreshes_avoided_at_one_per_5_min": math.ceil(elapsed / REFRESH_FLOOR),
    }


async def tempted(agent: Agent, tag: str, h: str, out: dict) -> dict | None:
    """The naive move: ship because Flagsmith reads "Winning", though the look held. The service must refuse.

    Returns what happened when the agent gave in, or None when the look gave no such reading."""
    d = out.get("decision") or {}
    ctw = d.get("chance_to_win")
    if d.get("outcome") != "hold" or ctw is None or ctw < WINNING:
        return None
    say(tag, f"Flagsmith says Winning at {ctw * 100:.2f}%. Shipping.", YELLOW)
    res = await agent.tool("execute", contract_hash=h, action="ship_treatment")
    if res.get("ok"):
        # Only a fired look may authorise an action; an accepted call here is a broken service.
        raise SessionError(f"execute ship_treatment was ACCEPTED on a look that held (look {out.get('look_id')})")
    say(tag, f"execute ship_treatment -> {res.get('status')} {res.get('detail')}", RED)
    say(tag, f"Refused: the signed contract holds at look {d.get('look_number') or '(not counted)'} "
             f"(z {d.get('z')}, boundary z {d.get('boundary_z')}). Not shipping; looking again.", DIM)
    return {"look_id": out.get("look_id"), "look_number": d.get("look_number"), "chance_to_win": ctw,
            "z": d.get("z"), "boundary_z": d.get("boundary_z"), "status": res.get("status"),
            "detail": res.get("detail")}


# What the agent needs to know about the demo: names and ids only. The rest of .run/demo.json (the
# environment's server-side key that the demo shop uses) is not the agent's.
DEMO_FIELDS = ("environment", "owner", "second_signer", "experiments")


def agent_view(demo: dict) -> dict:
    out = {k: demo[k] for k in DEMO_FIELDS if k in demo}
    out["experiments"] = {w: {"id": e["id"]} for w, e in (demo.get("experiments") or {}).items()}
    return out


def mark_synthetic(demo: dict, which: list[str], db: str | None = None) -> None:
    """Label the demo experiments' data source as simulated visitors (the pages show it)."""
    try:
        from ulysses.service import sources

        for w in which:
            sources.mark(ROOT / (db or os.environ.get("ULYSSES_DB", ".run/ulysses.db")), demo["environment"],
                         demo["experiments"][w]["id"], "synthetic",
                         "visitors simulated by examples/shop/visitors.py; Flagsmith assigns the variations")
    except Exception:  # noqa: BLE001 - the label is a convenience for the pages
        pass


def write_results(results: dict) -> None:
    try:
        (ROOT / ".run").mkdir(exist_ok=True)
        (ROOT / ".run" / "agent-session-result.json").write_text(json.dumps(results, indent=2))
        say("AGENT", "wrote .run/agent-session-result.json")
    except OSError:
        # In the agent container nothing is writable: hand the result to whoever reads the output.
        print("AGENT-RESULT " + json.dumps(results), flush=True)


async def run(a) -> None:
    if a.demo:
        demo = json.loads(a.demo)
    else:
        demo = agent_view(json.loads((ROOT / ".run" / "demo.json").read_text()))
    if "experiments" not in demo:
        raise SessionError(".run/demo.json is from an older setup; run scripts/demo_setup.py again")
    url, token = settings()
    try:
        httpx.get(url + "/healthz", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        raise SessionError(f"the contract service is not answering at {url} ({type(exc).__name__}); "
                           "start it with: uv run ulysses serve") from exc
    which = [w for w in a.experiments.split(",") if w]
    resume = dict(zip(which, a.resume.split(","))) if a.resume else {}
    procs: list[subprocess.Popen] = []
    if a.with_traffic:
        start_traffic(a, procs)
        mark_synthetic(demo, which)
    env = get_default_environment()
    env.update({"ULYSSES_URL": url, "ULYSSES_AGENT_TOKEN": token, "PYTHONPATH": str(ROOT / "src")})
    params = StdioServerParameters(command=sys.executable, args=["-m", "ulysses.mcp_server"], env=env, cwd=str(ROOT))
    results: dict = {}
    try:
        with open(os.devnull, "w") as quiet:
            async with stdio_client(params, errlog=quiet) as (r, w), ClientSession(r, w) as s:
                await s.initialize()
                tools = sorted(t.name for t in (await s.list_tools()).tools)
                say("AGENT", f"connected to the Ulysses MCP server; tools: {', '.join(tools)}")
                say("AGENT", "keys this process holds: Ulysses agent token only "
                             f"(Flagsmith key in the MCP server's environment: {any(k.startswith('FLAGSMITH_') for k in env)})")
                agent = Agent(s, url, a, procs, token=token)
                await asyncio.gather(*(govern(agent, demo, w, resume.get(w), results) for w in which))
    finally:
        for pr in procs:
            pr.terminate()
    write_results(results)
    for w, r in results.items():
        say("AGENT", f"{r.get('kind', w)}: {r.get('outcome')} after {r.get('minutes_from_start_to_decision')} min; "
                     f"{r.get('results_refreshes_run_by_agent')} refreshes run by the agent, "
                     f"{r.get('manual_refreshes_avoided_at_one_per_5_min')} manual five-minute checks avoided; "
                     f"{len(r.get('siren_refusals') or [])} times tempted by 'Winning' and refused")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--experiments", default="aa,cta", help="which demo experiments to govern: aa, cta or both")
    ap.add_argument("--resume", help="contract hashes (same order as --experiments) to continue")
    ap.add_argument("--min-lift", type=float, default=0.5)
    ap.add_argument("--baseline-rate", type=float, default=0.10)
    ap.add_argument("--with-traffic", action="store_true", help="start the demo shop and simulated visitors")
    ap.add_argument("--no-temptation", dest="temptation", action="store_false",
                    help="do not try to ship when a look holds while Flagsmith reads 'Winning' (95%% or more)")
    ap.add_argument("--autonomy", action="store_true",
                    help="draft each contract with the signed autonomy clause: a fired keep_control runs without "
                         "the owner's tap; ship_treatment still waits for it")
    ap.add_argument("--demo", help="the demo's names and ids as JSON (environment, owner, second_signer, "
                    "experiments); default: read from .run/demo.json. The agent container gets it this way.")
    ap.add_argument("--shop-port", type=int, default=int(os.environ.get("SHOP_PORT", "8210")))
    ap.add_argument("--visitors-per-minute", type=float, default=90)
    ap.add_argument("--classic", type=float, default=0.10)
    ap.add_argument("--bold", type=float, default=0.17)
    ap.add_argument("--refund-classic", type=float, default=0.30)
    ap.add_argument("--refund-bold", type=float, default=0.30)
    a = ap.parse_args()
    try:
        asyncio.run(run(a))
    except SessionError as exc:
        print(f"{RED}{BOLD}agent session stopped:{END} {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    except BaseExceptionGroup as eg:  # noqa: F821 - asyncio task groups wrap errors
        inner = next((e for e in eg.exceptions if isinstance(e, SessionError)), eg.exceptions[0])
        print(f"{RED}{BOLD}agent session stopped:{END} {inner}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
