"""One command: a stock Flagsmith, bootstrapped, and the whole Ulysses demo against it.

Needs only Docker and uv. It:

1. starts ``docker-compose.yml`` (project ``ulysses``): pinned open-source Flagsmith 2.295.2 with
   Experimentation on, on http://localhost:8280 (``--flagsmith-port`` moves it; ``--flagsmith URL`` uses
   a Flagsmith you already run);
2. signs up a first user, creates an organisation and an admin API key on that fresh instance;
3. writes ``.run/judge.env`` (git-ignored) with those keys, a new agent token and a webhook secret;
4. creates the demo flags, metrics and the signed audit webhook (``ulysses lab setup``), two
   experiments (A/A and real effect) and two signers with encrypted keys kept in your user profile,
   outside this folder (``scripts/demo_setup.py``);
5. starts the contract service, the demo shop with simulated visitors, a stand-in for the owner's
   browser taps (a separate process that holds the owner's key), and the scripted agent, which holds
   only the Ulysses token;
6. prints each look and the outcome, then stops what it started. The Flagsmith containers keep running
   so you can open the dashboard; ``docker compose -p ulysses down -v`` removes them.

Flagsmith refreshes results at most every five minutes; a measured run from a fresh clone took about half an hour.

With ``--agent-in-container`` the agent side (the scripted agent and the Ulysses MCP server) runs in its
own container instead (``docker-compose.yml``, profile ``agent``): it is given only the contract
service's URL and the agent token, has no volume and its own network, and cannot read ``.env``, the
service database, the signer keys or the demo shop's server-side key. The shop and the simulated
visitors then run here, on the service's side, since they need that key.

    uv run python scripts/judge_demo.py [--flagsmith-port 8280] [--port 8240] [--shop-port 8241] [--down]
                                        [--agent-in-container] [--llm]

Two minutes, no Docker: ``--replay data/replay-judge-demo.jsonl`` plays a recorded run (format in
docs/REPLAY.md) through the real contract service, a fresh store under ``.run/replay/`` and the real
pages, sped up and labelled as a replay on every page and every terminal line. Flagsmith is not
contacted; each look's decision is recomputed from the recorded Flagsmith numbers and checked against
the recorded one, and the script exits non-zero if any differs.

    uv run python scripts/judge_demo.py --replay data/replay-judge-demo.jsonl [--minutes 2] [--open] [--no-hold]

``--llm`` puts a real language model in place of the scripted agent (``examples/llm_agent/``), driving only the
Ulysses MCP tools with the same agent token: Gemini through the Google Gen AI SDK when GEMINI_API_KEY is set, else
Claude through the Anthropic SDK when ANTHROPIC_API_KEY is set (``uv run --extra gemini --extra llm python
scripts/judge_demo.py --llm`` installs both SDKs). Without a key or its SDK it says so and runs the scripted agent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

# Keep the demo's output readable on a Windows console that is not UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
RUN = ROOT / ".run"
JUDGE_ENV = RUN / "judge.env"
# The live calibration batch the README reports; the replay serves it on its calibration page.
REPLAY_CALIBRATION_TRACE = "live-72e1bc.jsonl"
# Signer keys and their passphrases live in the user profile, not in this folder, so nothing the agent
# process can find by reading the project tree signs for the owner. One directory per checkout.
JUDGE_KEYS = Path.home() / ".ulysses" / "judge" / hashlib.sha256(str(ROOT).encode()).hexdigest()[:10]


def step(text: str) -> None:
    print(f"\033[1;36m==> {text}\033[0m", flush=True)


def stop(msg: str) -> None:
    print(f"\033[1;31mjudge demo stopped:\033[0m {msg}", file=sys.stderr)
    sys.exit(1)


def wait_healthy(url: str, minutes: float) -> None:
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(3)
    stop(f"{url} did not answer within {minutes:.0f} minutes")


def port_free(port: int) -> bool:
    import socket

    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def key_works(api: str, key: str) -> bool:
    try:
        return httpx.get(f"{api}/api/v1/organisations/", headers={"Authorization": f"Api-Key {key}"},
                         timeout=15).status_code == 200
    except httpx.HTTPError:
        return False


def read_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


LLM_MODEL = "claude-opus-5-5"


def _gemini_model() -> str:
    import sys as _sys

    _sys.path.insert(0, str(ROOT))
    from examples.llm_agent.gemini_model import DEFAULT_MODEL

    return DEFAULT_MODEL


def choose_agent(want_llm: bool, environ: dict | None = None, sdk_installed: bool | None = None,
                 gemini_sdk_installed: bool | None = None) -> tuple[str, str]:
    """Which agent runs and the line that says so: "gemini", "llm" (Claude) or "scripted".

    Gemini when GEMINI_API_KEY is set, else Claude when ANTHROPIC_API_KEY is set, else the scripted agent. A key
    whose SDK is not installed is skipped, and the line says why. Never reads a key's value."""
    import importlib.util

    env = os.environ if environ is None else environ
    if not want_llm:
        return "scripted", "running the scripted agent (Ulysses agent token only)"
    skipped = []
    if env.get("GEMINI_API_KEY"):
        if gemini_sdk_installed is None:
            try:
                gemini_sdk_installed = importlib.util.find_spec("google.genai") is not None
            except ModuleNotFoundError:
                gemini_sdk_installed = False
        if gemini_sdk_installed:
            return "gemini", (f"running the LLM agent: {_gemini_model()} through the Google Gen AI SDK "
                              "(GEMINI_API_KEY is set), acting only through the Ulysses MCP tools "
                              "(Ulysses agent token only)")
        skipped.append("GEMINI_API_KEY is set but the Google Gen AI SDK is not installed (--extra gemini)")
    if not env.get("ANTHROPIC_API_KEY"):
        why = "; ".join(skipped or ["GEMINI_API_KEY is not set"]) + " and ANTHROPIC_API_KEY is not set"
        return "scripted", (f"--llm asked for the LLM agent, but {why}: running the scripted agent instead "
                            "(Ulysses agent token only)")
    if sdk_installed is None:
        sdk_installed = importlib.util.find_spec("anthropic") is not None
    if not sdk_installed:
        return "scripted", ("--llm asked for the LLM agent, but the Anthropic SDK is not installed (run "
                            "`uv run --extra llm python scripts/judge_demo.py --llm`): running the scripted agent "
                            "instead (Ulysses agent token only)")
    return "llm", (f"running the LLM agent: {LLM_MODEL} through the Anthropic SDK (ANTHROPIC_API_KEY is set), "
                   "acting only through the Ulysses MCP tools (Ulysses agent token only)")


def agent_command(mode: str, py: str, shop_port: int) -> list[str]:
    """The agent process for a mode from ``choose_agent``. No key ever goes on the command line."""
    if mode in ("gemini", "llm"):
        return [py, str(ROOT / "examples/llm_agent/run.py"), "live", "--model",
                "gemini" if mode == "gemini" else "anthropic", "--with-traffic", "--shop-port", str(shop_port)]
    return [py, str(ROOT / "scripts/agent_session.py"), "--with-traffic", "--shop-port", str(shop_port)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flagsmith", help="use this running Flagsmith instead of starting docker-compose.yml")
    ap.add_argument("--flagsmith-port", type=int, default=int(os.environ.get("ULYSSES_FLAGSMITH_PORT", "8280")),
                    help="host port for the Flagsmith that docker-compose.yml starts")
    ap.add_argument("--port", type=int, default=8240, help="contract service port (8200-8299)")
    ap.add_argument("--shop-port", type=int, default=8241)
    ap.add_argument("--down", action="store_true", help="remove the Flagsmith containers at the end")
    ap.add_argument("--agent-in-container", action="store_true",
                    help="run the agent and its MCP server in their own container (URL and token only)")
    ap.add_argument("--llm", action="store_true",
                    help="use the LLM agent (examples/llm_agent): Gemini when GEMINI_API_KEY is set, else Claude "
                         "when ANTHROPIC_API_KEY is set; else the scripted one")
    ap.add_argument("--serve-only", action="store_true",
                    help="after a run: serve the cards and the signing pages again (Ctrl+C to stop)")
    ap.add_argument("--replay", metavar="TRACE", help="play a recorded run (data/replay-*.jsonl) instead of a live one")
    ap.add_argument("--minutes", type=float, default=2.0, help="with --replay: how long the playback takes")
    ap.add_argument("--lead", type=float, default=6.0, help="with --replay: seconds to open the browser first")
    ap.add_argument("--open", action="store_true", help="with --replay: open the overview in your browser")
    ap.add_argument("--no-hold", action="store_true", help="with --replay: stop serving when the playback ends")
    a = ap.parse_args()
    if a.replay:
        replay(a)
        return
    if a.serve_only:
        if not JUDGE_ENV.exists():
            stop("no earlier run here (.run/judge.env is missing); run without --serve-only first")
        vals = read_env(JUDGE_ENV)
        port = vals["ULYSSES_PUBLIC_URL"].rsplit(":", 1)[-1]
        step(f"serving the cards at {vals['ULYSSES_PUBLIC_URL']}/ (Ctrl+C to stop)")
        try:
            subprocess.run([sys.executable, "-m", "ulysses.cli", "serve", "--port", port], cwd=ROOT,
                           env={**os.environ, **vals, "PYTHONPATH": str(ROOT / "src")})
        except KeyboardInterrupt:
            pass
        return
    RUN.mkdir(exist_ok=True)
    started = time.time()
    api = (a.flagsmith or f"http://localhost:{a.flagsmith_port}").rstrip("/")
    for label, port in (("contract service", a.port), ("demo shop", a.shop_port)):
        if not port_free(port):
            stop(f"port {port} for the {label} is in use; pick another with --port / --shop-port")

    if not a.flagsmith:
        if not shutil.which("docker"):
            stop("Docker is needed (or pass --flagsmith URL of a Flagsmith you run)")
        step("starting a stock Flagsmith (docker compose -p ulysses up -d)")
        compose_env = {**os.environ, "ULYSSES_FLAGSMITH_PORT": str(a.flagsmith_port)}
        r = subprocess.run(["docker", "compose", "-p", "ulysses", "-f", str(ROOT / "docker-compose.yml"), "up", "-d"],
                           cwd=ROOT, env=compose_env)
        if r.returncode != 0:
            stop("docker compose failed; is Docker running?")
    step(f"waiting for Flagsmith at {api}")
    wait_healthy(f"{api}/health", 10)

    from ulysses import lab

    old = read_env(JUDGE_ENV) if JUDGE_ENV.exists() else {}
    if old.get("FLAGSMITH_API_URL") == api and key_works(api, old.get("FLAGSMITH_ADMIN_KEY", "")):
        env_vals = old
        step("reusing the organisation and keys in .run/judge.env")
    else:
        step("bootstrapping the fresh instance: first user, organisation, admin API key")
        try:
            b = lab.bootstrap_stock(api, "owner@ulysses.demo", secrets.token_urlsafe(18))
        except (RuntimeError, httpx.HTTPError) as exc:
            stop(f"could not bootstrap Flagsmith at {api}: {exc}. If an earlier run used this instance, remove it "
                 "with `docker compose -p ulysses down -v` and run this again")
        env_vals = {
            "FLAGSMITH_API_URL": api,
            "FLAGSMITH_ADMIN_KEY": b["admin_key"],
            "FLAGSMITH_ORGANISATION_ID": str(b["organisation_id"]),
            "ULYSSES_AGENT_TOKEN": secrets.token_urlsafe(24),
            "ULYSSES_PUBLIC_URL": f"http://127.0.0.1:{a.port}",
            "ULYSSES_WEBHOOK_URL": f"http://host.docker.internal:{a.port}/webhooks/flagsmith",
            "ULYSSES_WEBHOOK_SECRET": secrets.token_urlsafe(32),
            "ULYSSES_DB": ".run/judge.db",
            "ULYSSES_KEY_DIR": str(JUDGE_KEYS / "keys"),
        }
        # A new organisation means new signers: drop keys and the store left by an earlier instance.
        shutil.rmtree(JUDGE_KEYS, ignore_errors=True)
        (RUN / "judge.db").unlink(missing_ok=True)
        JUDGE_ENV.write_text("".join(f"{k}={v}\n" for k, v in env_vals.items()), encoding="utf-8")
    # The two demo signers' passphrases exist only in this process and the stand-in human's.
    passphrases = {"ULYSSES_KEY_PASSPHRASE_MAYA": secrets.token_urlsafe(16),
                   "ULYSSES_KEY_PASSPHRASE_SAM": secrets.token_urlsafe(16)}
    keyfile = JUDGE_KEYS / "passphrases.env"
    if keyfile.exists():
        passphrases = read_env(keyfile)
    else:
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        keyfile.write_text("".join(f"{k}={v}\n" for k, v in passphrases.items()), encoding="utf-8")

    # Only the agent's process needs a model key; the service and the stand-in human never get one.
    llm_keys = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY")
    operator = {**{k: v for k, v in os.environ.items() if k not in llm_keys}, **env_vals}
    py = sys.executable
    step("creating demo flags, metrics and the signed audit webhook")
    if subprocess.run([py, "-m", "ulysses.cli", "lab", "setup"], cwd=ROOT,
                      env={**operator, "PYTHONPATH": str(ROOT / "src")}).returncode != 0:
        stop("ulysses lab setup failed")
    step("creating the A/A and real-effect experiments and the two signers")
    if subprocess.run([py, str(ROOT / "scripts/demo_setup.py"), "--replace"], cwd=ROOT,
                      env={**operator, **passphrases}).returncode != 0:
        stop("demo_setup.py failed")

    procs: list[subprocess.Popen] = []
    try:
        step(f"starting the contract service on port {a.port} (it holds the Flagsmith admin key; the agent never sees it)")
        host = "0.0.0.0" if sys.platform.startswith("linux") else "127.0.0.1"  # Linux containers reach the bridge IP
        procs.append(subprocess.Popen([py, "-m", "ulysses.cli", "serve", "--host", host, "--port", str(a.port)],
                                      cwd=ROOT, env={**operator, "PYTHONPATH": str(ROOT / "src")}))
        wait_healthy(f"http://127.0.0.1:{a.port}/healthz", 3)
        step("starting the stand-in for the owner's browser taps (separate process; holds maya's key)")
        human_env = {k: v for k, v in os.environ.items() if not k.startswith("FLAGSMITH_") and k not in llm_keys}
        human_env.update({**passphrases, "ULYSSES_PUBLIC_URL": env_vals["ULYSSES_PUBLIC_URL"],
                          "ULYSSES_DB": env_vals["ULYSSES_DB"], "ULYSSES_KEY_DIR": env_vals["ULYSSES_KEY_DIR"]})
        procs.append(subprocess.Popen([py, str(ROOT / "scripts/human_stand_in.py"), "--owner", "maya"],
                                      cwd=ROOT, env=human_env))
        mode, line = choose_agent(a.llm)
        if a.agent_in_container:
            if mode != "scripted":
                step("--agent-in-container runs the scripted agent; the LLM agent runs on this machine only")
            elif a.llm:
                step(line)  # --llm with no usable key or SDK: say why, as the on-host branch does
            mode = "scripted"
            rc = run_agent_container(a, env_vals, procs)
        else:
            step(line)
            agent_env = {k: v for k, v in os.environ.items()
                         if not k.startswith(("FLAGSMITH_", "ULYSSES_KEY"))}
            agent_env.update({"ULYSSES_URL": env_vals["ULYSSES_PUBLIC_URL"],
                              "ULYSSES_AGENT_TOKEN": env_vals["ULYSSES_AGENT_TOKEN"],
                              "ULYSSES_DB": env_vals["ULYSSES_DB"], "SHOP_PORT": str(a.shop_port)})
            if mode != "scripted":
                (RUN / "llm-agent-result.json").unlink(missing_ok=True)
            rc = subprocess.run(agent_command(mode, py, a.shop_port), cwd=ROOT, env=agent_env).returncode
        if rc != 0:
            stop("the agent session did not finish (see above); rerun this command to continue from a fresh setup")
        if not summarise(time.time() - started, api, a.port, "scripted" if mode == "scripted" else "llm"):
            stop("the LLM agent did not complete every experiment (see the summary and transcript above)")
    finally:
        for p in procs:
            p.terminate()
        if a.down and not a.flagsmith:
            subprocess.run(["docker", "compose", "-p", "ulysses", "down", "-v"], cwd=ROOT)


def _agent_session_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("agent_session", ROOT / "scripts" / "agent_session.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_agent_container(a, env_vals: dict, procs: list) -> int:
    """The agent in its own container; the shop and visitors (they hold the server-side key) stay here."""
    agent_session = _agent_session_module()
    demo = json.loads((RUN / "demo.json").read_text())
    view = agent_session.agent_view(demo)
    traffic = argparse.Namespace(shop_port=a.shop_port, visitors_per_minute=90, classic=0.10, bold=0.17,
                                 refund_classic=0.30, refund_bold=0.30)
    step("starting the demo shop and simulated visitors on the service's side (they hold the server-side key)")
    os.environ.update({"ULYSSES_PUBLIC_URL": env_vals["ULYSSES_PUBLIC_URL"], "ULYSSES_DB": env_vals["ULYSSES_DB"]})
    agent_session.start_traffic(traffic, procs)
    agent_session.mark_synthetic(view, sorted(view["experiments"]), db=env_vals["ULYSSES_DB"])
    step("running the scripted agent in its own container (it gets the Ulysses URL and agent token only)")
    compose_env = {**os.environ, "ULYSSES_AGENT_URL": f"http://host.docker.internal:{a.port}",
                   "ULYSSES_AGENT_TOKEN": env_vals["ULYSSES_AGENT_TOKEN"]}
    compose_env = {k: v for k, v in compose_env.items() if not k.startswith("FLAGSMITH_")}
    cmd = ["docker", "compose", "-p", "ulysses", "-f", str(ROOT / "docker-compose.yml"), "--profile", "agent",
           "run", "--rm", "--build", "--no-deps", "agent", "--demo", json.dumps(view)]
    proc = subprocess.Popen(cmd, cwd=ROOT, env=compose_env, stdout=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    result = None
    for line in proc.stdout:  # type: ignore[union-attr]
        if line.startswith("AGENT-RESULT "):
            result = json.loads(line[len("AGENT-RESULT "):])
            continue
        print(line, end="", flush=True)
    rc = proc.wait()
    if result is not None:
        (RUN / "agent-session-result.json").write_text(json.dumps(result, indent=2))
    return rc


def llm_completed(r: dict) -> int:
    """How many experiments the LLM agent's contracts ended executed on, as read back from the service."""
    done = {(st.get("experiment") or {}).get("id") or h for h, st in (r.get("contracts") or {}).items()
            if st.get("state") == "executed"}
    return len(done)


def expected_experiments(run: Path = RUN) -> int:
    try:
        return len(json.loads((run / "demo.json").read_text(encoding="utf-8"))["experiments"]) or 2
    except (OSError, ValueError, KeyError, TypeError):
        return 2


def summarise_llm(run: Path = RUN) -> tuple[int, int]:
    """What the LLM agent did, from the result file its harness wrote (read back from the service).

    Returns (experiments the agent completed, experiments in the demo)."""
    expected = expected_experiments(run)
    path = run / "llm-agent-result.json"
    if not path.exists():
        print("  the LLM agent wrote no .run/llm-agent-result.json", flush=True)
        return 0, expected
    r = json.loads(path.read_text(encoding="utf-8"))
    refused = r.get("refused_403") or []
    waits = r.get("confirm_waits") or []
    kinds: dict[str, int] = {}
    for ref in refused:
        kinds[ref.get("kind") or "other"] = kinds.get(ref.get("kind") or "other", 0) + 1
    by_kind = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(kinds.items()))
    print(f"  agent: LLM ({r.get('model')}), {r.get('turns')} model turns, {r.get('tool_calls')} tool calls, "
          f"{r.get('stop') or 'stop_reason ' + str(r.get('stop_reason'))}", flush=True)
    print(f"  {len(refused)} refused by the contract service with 403" + (f" ({by_kind})" if by_kind else "")
          + f"; {len(waits)} 403 waits for the owner's one-tap confirm (not refusals)", flush=True)
    for ref in refused:
        print(f"    403 {ref.get('kind') or 'other'} on {ref.get('tool')}: {ref.get('detail')}", flush=True)
    for ex in r.get("executed") or []:
        res = ex.get("result") or {}
        print(f"    executed {ex.get('action')}: flag {res.get('flag')} serves {res.get('serving')}", flush=True)
    for h, st in (r.get("contracts") or {}).items():
        print(f"    contract {h[:19]} on experiment {(st.get('experiment') or {}).get('id')}: {st.get('state')}",
              flush=True)
    print(f"  transcript: {r.get('transcript')}", flush=True)
    return llm_completed(r), expected


def summarise(seconds: float, api: str, port: int, mode: str = "scripted", run: Path = RUN) -> bool:
    """One table from what the agent session wrote, so the outcome is on screen at the end.

    Returns False when the LLM agent did not complete every experiment (the scripted agent's own exit code
    already covers it)."""
    path = run / "agent-session-result.json"
    results = json.loads(path.read_text()) if (path.exists() and mode == "scripted") else {}
    step(f"done in {seconds / 60:.1f} minutes")
    done, expected = (summarise_llm(run) if mode == "llm" else (None, None))
    if mode != "llm":
        print("  agent: scripted (scripts/agent_session.py)", flush=True)
    for which in ("aa", "cta"):
        r = results.get(which)
        if not r:
            continue
        res = r.get("result") or {}
        print(f"  {r.get('kind', which):<12} {str(r.get('outcome')).upper():<10} -> {r.get('action')}, "
              f"flag {res.get('flag')} serves {res.get('serving')}; {r.get('counted_looks')} counted looks, "
              f"{r.get('results_refreshes_run_by_agent')} refreshes run by the agent in "
              f"{r.get('minutes_from_start_to_decision')} min "
              f"({r.get('manual_refreshes_avoided_at_one_per_5_min')} five-minute manual checks nobody made)",
              flush=True)
    complete = mode != "llm" or done == expected
    if complete:
        print(f"  Flagsmith dashboard: {api} (the experiments are completed there with the contract in the hypothesis)")
    else:
        print(f"  Flagsmith dashboard: {api}")
        print(f"[1;33m  warning: the LLM agent completed {done} of {expected} experiments; see the transcript[0m",
              flush=True)
    print("  Browse the cards again:  uv run python scripts/judge_demo.py --serve-only", flush=True)
    print(f"  Remove Flagsmith:        docker compose -p ulysses down -v", flush=True)
    return complete


def replay(a) -> None:
    """Play a recorded run through the real service and pages, labelled as a replay throughout."""
    import secrets as _secrets
    import threading

    import uvicorn

    from ulysses.replay import ReplayBanner, Replayer, TraceError, load_trace
    from ulysses.service.app import create_app

    trace = Path(a.replay)
    if not trace.is_absolute():
        trace = (Path.cwd() / trace) if (Path.cwd() / trace).exists() else ROOT / trace
    try:
        header, events = load_trace(trace)
    except (OSError, TraceError) as exc:
        stop(str(exc))
    if not port_free(a.port):
        stop(f"port {a.port} for the replayed contract service is in use; pick another with --port")
    span = float(header.get("recorded_span_s") or 0.0)
    speed = max(span / max(a.minutes * 60, 1.0), 1.0)
    tag = f"[1;33m[REPLAY {speed:.1f}x][0m"
    run_dir = RUN / "replay" / trace.stem
    shutil.rmtree(run_dir, ignore_errors=True)
    _copy_calibration(run_dir / "calibration")
    url = f"http://127.0.0.1:{a.port}"
    r = Replayer(header, events, run_dir, public_url=url, speed=speed, base=time.time())
    finished = threading.Event()

    def status() -> dict:
        return {"played": r.done, "total": len(events), "finished": finished.is_set(), "speed": round(speed, 1),
                "trace": trace.name,
                "label": (f"A recorded run played back at {speed:.1f}x from data/{trace.name}; not live. "
                          "Flagsmith is not contacted: every number is Flagsmith's own, as recorded. "
                          + (f"Finished: all {len(events)} recorded events played." if finished.is_set()
                             else f"Event {r.done} of {len(events)}."))}

    app = ReplayBanner(create_app(r.svc, agent_token=_secrets.token_urlsafe(24)), status)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=a.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait_healthy(f"{url}/healthz", 1)

    step(f"{tag} REPLAY of a recorded run, not live: {trace.name}")
    print(f"{tag} {header.get('title')}")
    print(f"{tag} recorded on: {header.get('flagsmith')}")
    if header.get("note"):
        print(f"{tag} note: {header['note']}")
    print(f"{tag} {span / 60:.1f} recorded minutes played in about {span / speed / 60:.1f} (speed-up {speed:.1f}x). "
          "The contract service, its store, signature checks, decision engine and pages are the real ones; "
          "only Flagsmith is replaced by the recorded answers.")
    print(f"{tag} pages: {url}/  (store: {run_dir.relative_to(ROOT)})", flush=True)
    if a.open:
        import webbrowser

        webbrowser.open(f"{url}/")
    time.sleep(max(a.lead, 0.0))
    began = time.monotonic()
    for ev in events:
        # Keep to the recorded timeline at the chosen speed; time the service spends is not added on top.
        time.sleep(max(began + ev["t"] / speed - time.monotonic(), 0.0))
        s = r.play(ev)
        if s is None:
            continue
        colour = "" if s.ok else "[1;31m"
        print(f"{tag} recorded +{s.t / 60:5.1f} min  {colour}{s.text}[0m", flush=True)
        if s.kind == "look" and (s.detail.get("decision") or {}).get("fires"):
            print(f"{tag}   card: {s.detail.get('card')}", flush=True)
    # steps() is bypassed here (the loop keeps the recorded timeline), so run its end-of-trace check too.
    s = r.finish()
    if s is not None:
        print(f"{tag} [1;31m{s.text}[0m", flush=True)
    finished.set()
    looks = [e for e in events if e["event"] == "look"]
    bad_looks = sum(1 for m in r.mismatches if m.kind == "look")
    step(f"{tag} replay finished: {len(events)} recorded events played at {speed:.1f}x")
    print(f"{tag} {len(looks) - bad_looks} of {len(looks)} recorded decisions recomputed identically by this checkout's "
          f"engine; {len(r.mismatches)} event(s) did not reproduce", flush=True)
    for m in r.mismatches:
        print(f"{tag} [1;31mdid not reproduce:[0m {m.text}", flush=True)
    if not a.no_hold:
        print(f"{tag} still serving the replayed pages at {url}/ (Ctrl+C to stop)", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    server.should_exit = True
    if r.mismatches:
        sys.exit(1)


def _copy_calibration(dest: Path) -> None:
    """The calibration page reads the newest live trace next to the store: give it the batch the README reports."""
    src = ROOT / "data" / REPLAY_CALIBRATION_TRACE
    if src.exists():
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest / src.name)


if __name__ == "__main__":
    main()
