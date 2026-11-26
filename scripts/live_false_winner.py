"""Live false winner: a batch of A/A experiments on Flagsmith, each governed by a signed Ulysses contract.

The product's core moment, on real Flagsmith: an experiment whose two arms serve the identical value
reads "Winning" (chance to win over 95%) at an early look, and the contract holds. This script runs that
for a batch of experiments at once and records every look the contract service took.

``run`` does, in order:

1. writes the design (experiments, looks, identities per arm per look, alpha, conversion rate) to the
   trace before any experiment or traffic exists, and never changes it;
2. operator setup with the Flagsmith admin key from .env: one fresh multivariate flag per experiment
   (both variations serve ``same``), one A/A experiment on each, in the lab's calibration environment;
   two demo signers (owner ``maya``, second signer ``sam``) with passphrase-encrypted keys kept in the
   user profile; marks every experiment's traffic as synthetic identities for the pages;
3. starts the contract service (it alone holds the Flagsmith key) on its own store, and
   ``scripts/human_stand_in.py`` as a separate process in place of the owner's browser taps;
4. plays the agent through the service's agent API with the Ulysses token only (the endpoints the MCP
   tools ``sign_contract``, ``start_under_contract``, ``look`` and ``execute`` call): drafts a contract
   per experiment, waits for the owner's signature, starts each under its contract;
5. for each scheduled look: sends synthetic identities until every arm has ``per_arm_per_look * look``
   (Flagsmith's engine assigns them through the Python SDK in local evaluation, flushing events every 100
   identities as ``ulysses.calibrate.live`` does), then calls ``look`` on every contract. When a look fires,
   it waits for the stand-in owner's confirmation and calls ``execute`` with the pre-signed action.

Synthetic identities, not user traffic. Run output (logs, the service's store, the raw trace with
Flagsmith's timestamps) goes under ``.run/false-winner/<batch>/``; ``strip`` writes the timestamp-free
copy for ``data/``; ``count`` prints the numbers from a trace.

    uv run python scripts/live_false_winner.py run [--experiments 30 --looks 10 --per-arm-per-look 108]
    uv run python scripts/live_false_winner.py strip .run/false-winner/<batch>/trace.jsonl data/live-fw-<batch>.jsonl
    uv run python scripts/live_false_winner.py count data/live-fw-<batch>.jsonl
    uv run python scripts/live_false_winner.py serve <batch>     # the cards again, from that batch's store
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import secrets
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ulysses.calibrate import false_winner  # noqa: E402

RUNS = ROOT / ".run" / "false-winner"
# Signer keys and passphrases live in the user profile, not in this folder (as in judge_demo.py).
KEYS = Path.home() / ".ulysses" / "false-winner" / hashlib.sha256(str(ROOT).encode()).hexdigest()[:10]
FLUSH_EVERY = 100
SOURCE_DETAIL = ("A/A on live self-hosted Flagsmith: both arms serve the same value; synthetic identities "
                 "assigned by Flagsmith's engine (Python SDK), conversions drawn by scripts/live_false_winner.py")


def log(text: str) -> None:
    print(text, flush=True)


def read_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        k, sep, v = line.partition("=")
        if sep and not k.strip().startswith("#"):
            out[k.strip()] = v.strip()
    return out


def wait_healthy(url: str, seconds: float) -> None:
    import httpx

    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise SystemExit(f"{url} did not answer within {seconds:.0f}s")


def signer_passphrases() -> dict[str, str]:
    f = KEYS / "passphrases.env"
    if f.exists():
        return read_env(f)
    KEYS.mkdir(parents=True, exist_ok=True)
    p = {"ULYSSES_KEY_PASSPHRASE_MAYA": secrets.token_urlsafe(16), "ULYSSES_KEY_PASSPHRASE_SAM": secrets.token_urlsafe(16)}
    f.write_text("".join(f"{k}={v}\n" for k, v in p.items()), encoding="utf-8")
    return p


def ensure_signers(db: Path, passphrases: dict[str, str]) -> None:
    from ulysses import signing
    from ulysses.store import Store

    store = Store(db)
    for name in ("maya", "sam"):
        pw = passphrases[f"ULYSSES_KEY_PASSPHRASE_{name.upper()}"]
        path = signing.key_path(name, KEYS / "keys")
        if path.exists():
            kp = signing.load_keypair(name, KEYS / "keys", passphrase=pw)
        else:
            kp = signing.Keypair.generate()
            signing.save_keypair(name, kp, KEYS / "keys", passphrase=pw)
        store.add_signer(name, kp.public, time.time())


class Run:
    def __init__(self, a, design: dict, out: Path, url: str, token: str):
        from ulysses.mcp_server import ServiceClient

        self.a = a
        self.design = design
        self.out = out
        self.url = url
        self.svc = ServiceClient(url, token)  # the agent's only credential is the Ulysses token
        self.trace = (out / "trace.jsonl").open("a", encoding="utf-8")
        self.lock = threading.Lock()
        self.rng = random.Random(int(design["batch"], 16))
        self.rng_lock = threading.Lock()

    def write(self, row: dict) -> None:
        with self.lock:
            self.trace.write(json.dumps(row) + "\n")
            self.trace.flush()

    # ------------------------------------------------------------- the agent's side

    def draft_and_start(self, exps: list[dict], env_key: str) -> None:
        d = self.design
        for e in exps:
            out = self.svc.call("POST", "/api/contracts/draft", json={
                "environment": env_key, "experiment_id": e["id"], "owner": "maya", "second_signer": "sam",
                "min_relative_lift": d["min_relative_lift"], "baseline_rate": d["baseline_rate"],
                "alpha": d["alpha"], "looks": d["looks"], "identities_per_arm": d["horizon"],
                "hypothesis": "A/A: both variations serve the identical value, so neither should win.",
            })
            if not out.get("ok"):
                raise SystemExit(f"draft for experiment {e['id']} refused: {out.get('detail')}")
            e["hash"] = out["hash"]
        log(f"drafted {len(exps)} contracts ({out['summary']['horizon']}; {out['summary']['boundary']}); "
            "waiting for the owner's signatures")
        deadline = time.time() + 600
        pending = list(exps)
        while pending and time.time() < deadline:
            pending = [e for e in pending if not self._signed(e["hash"])]
            if pending:
                time.sleep(3)
        if pending:
            raise SystemExit(f"{len(pending)} contracts were not signed in time (is the stand-in running?)")
        for e in exps:
            for attempt in range(5):
                out = self.svc.call("POST", f"/api/contracts/{e['hash']}/start")
                if out.get("ok"):
                    break
                time.sleep(5 * (attempt + 1))
            else:
                raise SystemExit(f"start of experiment {e['id']} failed: {out.get('detail')}")
        log(f"started {len(exps)} experiments under their signed contracts")

    def _signed(self, h: str) -> bool:
        st = self.svc.call("GET", f"/api/contracts/{h}")
        return bool(st.get("ok")) and st.get("state") == "signed" and not st.get("needs")

    def look_round(self, e: dict, k: int) -> None:
        """One scheduled look on one contract, with the retries the service asks for."""
        attempt = 0
        end = time.time() + 20 * 60
        while time.time() < end:
            out = self.svc.call("POST", f"/api/contracts/{e['hash']}/look")
            if not out.get("ok"):
                if out.get("status") in (429, 502, 503, 504, None):
                    time.sleep(30)
                    continue
                self.write({"batch": self.design["batch"], "experiment": e["id"], "look": k,
                            "error": f"look refused: {out.get('status')} {out.get('detail')}"})
                return
            if out.get("too_soon") or out.get("look_not_taken"):
                time.sleep(int(out.get("retry_after_seconds", 60)) + 3)
                continue
            if out.get("already_decided"):
                e["done"] = True
                return
            if out.get("state") == "deviated":
                self.write({"batch": self.design["batch"], "experiment": e["id"], "look": k,
                            "error": f"deviation: {out.get('deviation')}"})
                e["done"] = True
                return
            attempt += 1
            d = out["decision"]
            fs = out.get("flagsmith") or {}
            ids = fs.get("identities") or {}
            as_of = fs.get("as_of")
            ch = d.get("chance_to_win")
            row = {
                "batch": self.design["batch"], "experiment": e["id"], "look": k, "attempt": attempt,
                "n_control": int(ids.get("control", 0)), "n_treatment": int(ids.get("twin", 0)),
                "chance_to_win": ch, "as_of": as_of, "stale": as_of is not None and as_of == e.get("as_of"),
                "served": dict(e["served"]), "look_id": out.get("look_id"), "refreshed": out.get("refreshed"),
                "banner_verdict": None if ch is None else ("winning" if ch > 0.95 else "losing" if ch < 0.05
                                                           else "inconclusive"),
                "contract": {key: d.get(key) for key in ("outcome", "counted", "look_number", "fraction",
                                                         "identities_per_arm", "z", "boundary_z",
                                                         "boundary_chance")},
            }
            e["as_of"] = as_of
            if out.get("proposal"):
                row["fired"] = self.carry_out(e, out["proposal"])
                e["done"] = True
            self.write(row)
            tag = "FIRE " + d["outcome"].upper() if d.get("fires") else d["outcome"].upper()
            if ch is not None and ch > 0.95 and not d.get("fires"):
                tag += "  <- Flagsmith reads Winning; the contract holds"
            log(f"  exp {e['id']} look {k}: n={d.get('identities_per_arm')} chance {ch if ch is None else f'{ch:.4f}'}"
                f" needs {d.get('boundary_chance')} -> {tag}")
            if d.get("fires") or d.get("counted") or attempt >= 2:
                return
            # Not counted (Flagsmith had not recomputed, or counted fewer identities than the step): one retry.

    def carry_out(self, e: dict, p: dict) -> dict:
        """A fired look: the owner confirms on the card (stand-in), then the agent calls execute."""
        pid = p["id"]
        deadline = time.time() + 600
        state = p["state"]
        while state == "pending" and time.time() < deadline:
            time.sleep(3)
            state = self.svc.call("GET", f"/api/proposals/{pid}").get("state", state)
        out = self.svc.call("POST", f"/api/contracts/{e['hash']}/execute", json={"action": p["action"]})
        res = out.get("result") or {}
        return {"outcome": p["outcome"], "action": p["action"], "proposal": pid,
                "state": out.get("state") if out.get("ok") else f"execute refused: {out.get('detail')}",
                "serving": res.get("serving")}


def cmd_run(a) -> None:
    from ulysses import lab
    from ulysses.service import sources
    from ulysses.service.runtime import load_dotenv

    os.chdir(ROOT)
    load_dotenv(ROOT / ".env")
    batch = secrets.token_hex(3)
    out = RUNS / batch
    out.mkdir(parents=True, exist_ok=True)
    design = {"batch": batch, "kind": "false-winner A/A under signed contracts", "experiments": a.experiments,
              "looks": a.looks, "per_arm_per_look": a.per_arm_per_look, "horizon": a.looks * a.per_arm_per_look,
              "alpha": a.alpha, "rate": a.rate, "min_relative_lift": a.min_lift, "baseline_rate": a.rate,
              "top_up_looks": a.top_up}
    # The design is fixed here, before any experiment or identity exists.
    (out / "trace.jsonl").write_text(json.dumps({"design": design}) + "\n", encoding="utf-8")
    (out / "design.json").write_text(json.dumps(design, indent=2), encoding="utf-8")
    log(f"batch {batch}: design written before any traffic: {json.dumps(design)}")

    cfg = lab.LabConfig.from_env()
    admin = lab.Admin(cfg)
    pid = admin.project()
    env = admin.environment(pid, lab.CAL_ENV)
    admin.warehouse(env["key"])
    mid = admin.metric(env["key"], lab.CAL_METRIC, "occurrence", "up", lab.CAL_EVENT,
                       "A/A calibration conversion (synthetic identities)")
    server_key = admin.server_key(env["key"])
    db = out / "ulysses.db"
    exps = []
    for i in range(a.experiments):
        name = f"ulysses_fw_{batch}_{i:02d}"
        fid, oid = admin.mv_flag(pid, name, lab.CAL_VALUE, lab.CAL_VARIATION, lab.CAL_VALUE,
                                 "Ulysses live false-winner A/A: both variations serve the same value")
        x = admin.create_experiment(env["key"], feature_id=fid, option_id=oid,
                                    name=f"ulysses false-winner A/A {i:02d} batch {batch}",
                                    hypothesis="A/A: both variations serve the identical value. Synthetic identities.",
                                    control_value=lab.CAL_VALUE, metrics=[(mid, "increase")])
        exps.append({"index": i, "flag": name, "id": int(x["id"]),
                     "served": {"control": 0, lab.CAL_VARIATION: 0}, "sent": 0})
        sources.mark(db, env["key"], int(x["id"]), "synthetic", SOURCE_DETAIL)
    log(f"created {len(exps)} A/A experiments on fresh flags in environment {lab.CAL_ENV}")

    passphrases = signer_passphrases()
    ensure_signers(db, passphrases)
    url = f"http://127.0.0.1:{a.port}"
    token = os.environ["ULYSSES_AGENT_TOKEN"]
    svc_env = {**os.environ, "ULYSSES_DB": str(db), "ULYSSES_PUBLIC_URL": url, "PYTHONPATH": str(ROOT / "src"),
               "ULYSSES_REFRESH_WAIT": str(a.refresh_wait)}
    human_env = {k: v for k, v in os.environ.items() if not k.startswith("FLAGSMITH_")}
    human_env.update({**passphrases, "ULYSSES_KEY_DIR": str(KEYS / "keys"), "ULYSSES_DB": str(db),
                      "PYTHONPATH": str(ROOT / "src")})
    procs: list[subprocess.Popen] = []
    sdk = None
    run = None
    try:
        with (out / "service.log").open("w") as sl:
            procs.append(subprocess.Popen([sys.executable, "-m", "ulysses.cli", "serve", "--port", str(a.port)],
                                          cwd=ROOT, env=svc_env, stdout=sl, stderr=subprocess.STDOUT))
        wait_healthy(f"{url}/healthz", 60)
        with (out / "human.log").open("w") as hl:
            procs.append(subprocess.Popen([sys.executable, str(ROOT / "scripts/human_stand_in.py"), "--owner", "maya",
                                           "--service", url, "--db", str(db), "--minutes", "300"],
                                          cwd=ROOT, env=human_env, stdout=hl, stderr=subprocess.STDOUT))
        run = Run(a, design, out, url, token)
        run.draft_and_start(exps, env["key"])

        from flagsmith import Flagsmith
        from flagsmith.analytics import EventProcessorConfig

        time.sleep(3)
        sdk = Flagsmith(environment_key=server_key, api_url=cfg.api_url + "/api/v1/", enable_local_evaluation=True,
                        environment_refresh_interval_seconds=30, enable_events=True,
                        event_processor_config=EventProcessorConfig(events_api_url=cfg.api_url + "/api/",
                                                                     flush_interval_seconds=1.0, max_buffer_items=50_000))
        time.sleep(2)

        def send_until(e: dict, target: int) -> None:
            guard = 0
            while min(e["served"].values()) < target and guard < target * 10:
                ident = f"fw-{batch}-{e['index']:02d}-{e['sent']:06d}"
                e["sent"] += 1
                guard += 1
                fl = sdk.get_experiment_flag(e["flag"], identifier=ident)
                v = getattr(fl, "variant", None)
                if not (getattr(fl, "experiment", None) and fl.experiment.in_experiment) or v not in e["served"]:
                    continue
                e["served"][v] += 1
                if run.rng.random() < a.rate:
                    sdk.track_event(lab.CAL_EVENT, identifier=ident)
                if e["sent"] % FLUSH_EVERY == 0:
                    sdk.flush_events()

        schedule = [(k, k * a.per_arm_per_look) for k in range(1, a.looks + 1)]
        # After the last look, a contract Flagsmith has not yet counted to the horizon gets a top-up look.
        schedule += [(a.looks + j, a.looks * a.per_arm_per_look + j * a.per_arm_per_look // 10)
                     for j in range(1, a.top_up + 1)]
        for k, target in schedule:
            live = [e for e in exps if not e.get("done")]
            if not live:
                break
            t0 = time.time()
            for e in live:
                send_until(e, target)
            for _ in range(3):
                sdk.flush_events()
            time.sleep(10)
            log(f"look {k}: traffic to {target} per arm sent to {len(live)} experiments in {time.time() - t0:.0f}s")
            with ThreadPoolExecutor(max_workers=len(live)) as pool:
                list(pool.map(lambda e: run.look_round(e, k), live))
            s = false_winner.count(out / "trace.jsonl")
            log(f"look {k}: Flagsmith over 95% while the contract held so far on "
                f"{s['over_95_while_holding']['experiments']} experiments; contract outcomes {s['contract_outcomes']}")
    finally:
        if sdk is not None:
            sdk.close()
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(20)
            except subprocess.TimeoutExpired:
                p.kill()
    leftover = [e for e in exps if not e.get("done")]
    for e in leftover:
        # Not decided by the contract within the run: ended by the operator, keeping control, and said so.
        try:
            admin.call("POST", f"/api/v1/environments/{env['key']}/experiments/{e['id']}/complete/",
                       json={"rollout_decision": "control"})
            if run is not None:
                run.write({"batch": batch, "experiment": e["id"], "operator": "completed after the run, kept control"})
        except RuntimeError as exc:
            log(f"could not complete {e['id']}: {exc}")
    s = false_winner.count(out / "trace.jsonl")
    (out / "count.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    log(s["text"])
    log(f"strip for data/: uv run python scripts/live_false_winner.py strip {out / 'trace.jsonl'} "
        f"data/live-fw-{batch}.jsonl")


def cmd_strip(a) -> None:
    from ulysses.calibrate.live import strip_trace

    p = strip_trace(a.src, a.dst)
    print(f"wrote {p} (Flagsmith's as_of timestamps removed; same numbers)")


def cmd_count(a) -> None:
    s = false_winner.count(a.trace)
    if a.json:
        print(json.dumps({k: v for k, v in s.items() if k != "text"}, indent=2))
    else:
        print(s["text"], end="")
        for c in s["hold_cards_over_95"]:
            print(f"  experiment {c['experiment']} {false_winner.where(c)}: Flagsmith {c['chance_to_win']:.4f}, contract "
                  f"{c['outcome']} (needs {c['boundary_chance']}), look record {c['look_id']}")


def cmd_serve(a) -> None:
    from ulysses.service.runtime import load_dotenv

    os.chdir(ROOT)
    load_dotenv(ROOT / ".env")
    db = RUNS / a.batch / "ulysses.db"
    if not db.exists():
        raise SystemExit(f"no store for batch {a.batch} at {db}")
    url = f"http://127.0.0.1:{a.port}"
    env = {**os.environ, "ULYSSES_DB": str(db), "ULYSSES_PUBLIC_URL": url, "PYTHONPATH": str(ROOT / "src")}
    print(f"serving batch {a.batch}'s cards at {url}/ (Ctrl+C to stop)", flush=True)
    try:
        subprocess.run([sys.executable, "-m", "ulysses.cli", "serve", "--port", str(a.port)], cwd=ROOT, env=env)
    except KeyboardInterrupt:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a batch on the Flagsmith in .env")
    r.add_argument("--experiments", type=int, default=30)
    r.add_argument("--looks", type=int, default=10)
    r.add_argument("--per-arm-per-look", type=int, default=108)
    r.add_argument("--alpha", type=float, default=0.05)
    r.add_argument("--rate", type=float, default=0.10, help="conversion probability in both arms (and the baseline)")
    r.add_argument("--min-lift", type=float, default=0.5, help="the smallest relative lift the contract is sized for")
    r.add_argument("--top-up", type=int, default=2, help="extra looks for contracts Flagsmith has not counted to the horizon")
    r.add_argument("--port", type=int, default=8250)
    r.add_argument("--refresh-wait", type=float, default=270, help="seconds the service waits for Flagsmith to recompute")
    s = sub.add_parser("strip", help="timestamp-free copy of a trace")
    s.add_argument("src")
    s.add_argument("dst")
    c = sub.add_parser("count", help="the batch's numbers from a trace")
    c.add_argument("trace")
    c.add_argument("--json", action="store_true")
    v = sub.add_parser("serve", help="serve a batch's cards again")
    v.add_argument("batch")
    v.add_argument("--port", type=int, default=8250)
    a = ap.parse_args()
    {"run": cmd_run, "strip": cmd_strip, "count": cmd_count, "serve": cmd_serve}[a.cmd](a)


if __name__ == "__main__":
    main()
