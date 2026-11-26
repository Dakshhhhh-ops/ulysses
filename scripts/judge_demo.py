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

    uv run python scripts/judge_demo.py [--flagsmith-port 8280] [--port 8240] [--shop-port 8241] [--down]

Two minutes, no Docker: ``--replay data/replay-judge-demo.jsonl`` plays a recorded run (format in
docs/REPLAY.md) through the real contract service, a fresh store under ``.run/replay/`` and the real
pages, sped up and labelled as a replay on every page and every terminal line. Flagsmith is not
contacted; each look's decision is recomputed from the recorded Flagsmith numbers and checked against
the recorded one, and the script exits non-zero if any differs.

    uv run python scripts/judge_demo.py --replay data/replay-judge-demo.jsonl [--minutes 2] [--open] [--no-hold]
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flagsmith", help="use this running Flagsmith instead of starting docker-compose.yml")
    ap.add_argument("--flagsmith-port", type=int, default=int(os.environ.get("ULYSSES_FLAGSMITH_PORT", "8280")),
                    help="host port for the Flagsmith that docker-compose.yml starts")
    ap.add_argument("--port", type=int, default=8240, help="contract service port (8200-8299)")
    ap.add_argument("--shop-port", type=int, default=8241)
    ap.add_argument("--down", action="store_true", help="remove the Flagsmith containers at the end")
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
        step(f"starting the contract service on port {a.port} (it holds the Flagsmith admin key; the agent never sees it)")
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
        summarise(time.time() - started, api, a.port)
    finally:
        for p in procs:
            p.terminate()
        if a.down and not a.flagsmith:
            subprocess.run(["docker", "compose", "-p", "ulysses", "down", "-v"], cwd=ROOT)


def summarise(seconds: float, api: str, port: int) -> None:
    """One table from what the agent session wrote, so the outcome is on screen at the end."""
    path = RUN / "agent-session-result.json"
    results = json.loads(path.read_text()) if path.exists() else {}
    step(f"done in {seconds / 60:.1f} minutes")
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
    print(f"  Flagsmith dashboard: {api} (the experiments are completed there with the contract in the hypothesis)")
    print("  Browse the cards again:  uv run python scripts/judge_demo.py --serve-only", flush=True)
    print(f"  Remove Flagsmith:        docker compose -p ulysses down -v", flush=True)


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
