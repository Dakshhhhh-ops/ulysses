"""One command: a stock Flagsmith, bootstrapped, and the whole Ulysses demo against it.

Needs only Docker and uv. It:

1. starts ``docker-compose.yml`` (project ``ulysses``): pinned open-source Flagsmith 2.295.2 with
   Experimentation on, on http://localhost:8280 (or uses ``--flagsmith URL`` you already run);
2. signs up a first user, creates an organisation and an admin API key on that fresh instance;
3. writes ``.run/judge.env`` (git-ignored) with those keys, a new agent token and a webhook secret;
4. creates the demo flags, metrics and the signed audit webhook (``ulysses lab setup``), two
   experiments (A/A and real effect) and two signers with encrypted keys (``scripts/demo_setup.py``);
5. starts the contract service, the demo shop with simulated visitors, a stand-in for the owner's
   browser taps (a separate process that holds the owner's key), and the scripted agent, which holds
   only the Ulysses token;
6. prints each look and the outcome, then stops what it started. The Flagsmith containers keep running
   so you can open the dashboard; ``docker compose -p ulysses down -v`` removes them.

Flagsmith refreshes results at most every five minutes, so expect 20 to 40 minutes.

    uv run python scripts/judge_demo.py [--flagsmith http://localhost:8280] [--down]
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
RUN = ROOT / ".run"
JUDGE_ENV = RUN / "judge.env"


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


def read_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flagsmith", help="use this running Flagsmith instead of starting docker-compose.yml")
    ap.add_argument("--port", type=int, default=8240, help="contract service port (8200-8299)")
    ap.add_argument("--shop-port", type=int, default=8241)
    ap.add_argument("--down", action="store_true", help="remove the Flagsmith containers at the end")
    a = ap.parse_args()
    RUN.mkdir(exist_ok=True)
    api = (a.flagsmith or "http://localhost:8280").rstrip("/")

    if not a.flagsmith:
        if not shutil.which("docker"):
            stop("Docker is needed (or pass --flagsmith URL of a Flagsmith you run)")
        step("starting a stock Flagsmith (docker compose -p ulysses up -d)")
        r = subprocess.run(["docker", "compose", "-p", "ulysses", "-f", str(ROOT / "docker-compose.yml"), "up", "-d"],
                           cwd=ROOT)
        if r.returncode != 0:
            stop("docker compose failed; is Docker running?")
    step(f"waiting for Flagsmith at {api}")
    wait_healthy(f"{api}/health", 10)

    from ulysses import lab

    if JUDGE_ENV.exists() and read_env(JUDGE_ENV).get("FLAGSMITH_API_URL") == api:
        env_vals = read_env(JUDGE_ENV)
        step("reusing the organisation and keys in .run/judge.env")
    else:
        step("bootstrapping the fresh instance: first user, organisation, admin API key")
        b = lab.bootstrap_stock(api, "owner@ulysses.demo", secrets.token_urlsafe(18))
        env_vals = {
            "FLAGSMITH_API_URL": api,
            "FLAGSMITH_ADMIN_KEY": b["admin_key"],
            "FLAGSMITH_ORGANISATION_ID": str(b["organisation_id"]),
            "ULYSSES_AGENT_TOKEN": secrets.token_urlsafe(24),
            "ULYSSES_PUBLIC_URL": f"http://127.0.0.1:{a.port}",
            "ULYSSES_WEBHOOK_URL": f"http://host.docker.internal:{a.port}/webhooks/flagsmith",
            "ULYSSES_WEBHOOK_SECRET": secrets.token_urlsafe(32),
            "ULYSSES_DB": ".run/judge.db",
            "ULYSSES_KEY_DIR": str(RUN / "judge-keys"),
        }
        JUDGE_ENV.write_text("".join(f"{k}={v}\n" for k, v in env_vals.items()), encoding="utf-8")
    # The two demo signers' passphrases exist only in this process and the stand-in human's.
    passphrases = {"ULYSSES_KEY_PASSPHRASE_MAYA": secrets.token_urlsafe(16),
                   "ULYSSES_KEY_PASSPHRASE_SAM": secrets.token_urlsafe(16)}
    keyfile = RUN / "judge-keys" / ".passphrases"
    if keyfile.exists():
        passphrases = read_env(keyfile)
    else:
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        keyfile.write_text("".join(f"{k}={v}\n" for k, v in passphrases.items()), encoding="utf-8")

    operator = {**os.environ, **env_vals}
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
        step(f"starting the contract service on port {a.port} (it alone holds the Flagsmith key)")
        host = "0.0.0.0" if sys.platform.startswith("linux") else "127.0.0.1"  # Linux containers reach the bridge IP
        procs.append(subprocess.Popen([py, "-m", "ulysses.cli", "serve", "--host", host, "--port", str(a.port)],
                                      cwd=ROOT, env={**operator, "PYTHONPATH": str(ROOT / "src")}))
        wait_healthy(f"http://127.0.0.1:{a.port}/healthz", 3)
        step("starting the stand-in for the owner's browser taps (separate process; holds maya's key)")
        human_env = {k: v for k, v in os.environ.items() if not k.startswith("FLAGSMITH_")}
        human_env.update({**passphrases, "ULYSSES_PUBLIC_URL": env_vals["ULYSSES_PUBLIC_URL"],
                          "ULYSSES_DB": env_vals["ULYSSES_DB"], "ULYSSES_KEY_DIR": env_vals["ULYSSES_KEY_DIR"]})
        procs.append(subprocess.Popen([py, str(ROOT / "scripts/human_stand_in.py"), "--owner", "maya"],
                                      cwd=ROOT, env=human_env))
        step("running the scripted agent (Ulysses agent token only)")
        agent_env = {k: v for k, v in os.environ.items()
                     if not k.startswith(("FLAGSMITH_", "ULYSSES_KEY"))}
        agent_env.update({"ULYSSES_URL": env_vals["ULYSSES_PUBLIC_URL"],
                          "ULYSSES_AGENT_TOKEN": env_vals["ULYSSES_AGENT_TOKEN"],
                          "ULYSSES_DB": env_vals["ULYSSES_DB"], "SHOP_PORT": str(a.shop_port)})
        rc = subprocess.run([py, str(ROOT / "scripts/agent_session.py"), "--with-traffic",
                             "--shop-port", str(a.shop_port)], cwd=ROOT, env=agent_env).returncode
        if rc != 0:
            stop("the agent session did not finish (see above); rerun this command to continue from a fresh setup")
        step(f"done. Cards: http://127.0.0.1:{a.port}/  (service stopped now; rerun `ulysses serve` with "
             f".run/judge.env to browse them). Flagsmith dashboard: {api}")
    finally:
        for p in procs:
            p.terminate()
        if a.down and not a.flagsmith:
            subprocess.run(["docker", "compose", "-p", "ulysses", "down", "-v"], cwd=ROOT)


if __name__ == "__main__":
    main()
