"""One short governed experiment on a live Flagsmith, then `ulysses verify` on it.

Needs a running self-hosted Flagsmith with Experimentation on and the lab
objects (`ulysses lab setup`), with FLAGSMITH_API_URL / FLAGSMITH_ADMIN_KEY /
FLAGSMITH_ORGANISATION_ID in `.env`. It creates a fresh experiment on the
integration-test flag `ulysses_integration_cta`, signs a two-look contract with
two throwaway in-memory signers, sends synthetic identities through the official
Python SDK with a planted effect (purchase 20% after classic, 70% after bold;
refund requests 20% in both arms), looks until the contract fires, confirms the
pre-signed action, and then:

    ulysses export-bundle <id> --db .run/verify-live/ulysses.db --out .run/verify-live/bundle.json
    ulysses verify <id> --db .run/verify-live/ulysses.db          (live: reads Flagsmith now)
    ulysses verify --bundle .run/verify-live/bundle.json          (offline, unpinned: exit 2)
    ulysses verify --bundle .run/verify-live/bundle.json --expect-hash <the contract hash>   (offline, pinned)

The identities are synthetic and the effect is planted by this script; the
design (100 identities per arm, two looks) is far below the service's policy and
is signed with an override reason saying so. It demonstrates verification on
Flagsmith's real endpoints, not an experiment result.

    uv run python scripts/verify_live_run.py
"""

from __future__ import annotations

import random
import subprocess
import sys
import time
from pathlib import Path

from ulysses import lab, signing
from ulysses.core import Settings, Ulysses
from ulysses.flagsmith.rest import RestFlagsmith
from ulysses.service import sources
from ulysses.service.runtime import load_dotenv
from ulysses.store import Store

FLAG = "ulysses_integration_cta"
RUN = Path(".run/verify-live")
RATES = {"control": 0.20, "bold": 0.70}
REFUND = 0.20


def send(sdk, eid: int, start: int, count: int, rng: random.Random) -> dict:
    served = {"control": 0, "bold": 0}
    for i in range(start, start + count):
        ident = f"verify-{eid}-{i}"
        fl = sdk.get_experiment_flag(FLAG, identifier=ident)
        v = getattr(fl, "variant", None)
        if v not in served:
            continue
        served[v] += 1
        if rng.random() < RATES[v]:
            sdk.track_event(lab.PURCHASE_EVENT, identifier=ident)
        if rng.random() < REFUND:
            sdk.track_event(lab.REFUND_EVENT, identifier=ident)
    sdk.flush_events()
    return served


def main() -> int:
    load_dotenv()
    cfg = lab.LabConfig.from_env()
    demo = lab.setup_demo(cfg)
    admin = lab.Admin(cfg)
    fid, oid = admin.mv_flag(demo["project_id"], FLAG, "classic", "bold", "bold", "Ulysses integration test flag")
    admin.clear_active(demo["environment_key"], fid, replace=True)
    exp = admin.create_experiment(
        demo["environment_key"], feature_id=fid, option_id=oid, name="ulysses verify demo",
        hypothesis="Verification demo: synthetic identities with a planted effect.", control_value="classic",
        metrics=[(demo["metrics"]["purchase"], "increase"), (demo["metrics"]["refund"], "not_increase")])
    env, eid = demo["environment_key"], int(exp["id"])
    RUN.mkdir(parents=True, exist_ok=True)
    db = RUN / "ulysses.db"
    for p in (db, RUN / sources.FILE):
        if p.exists():
            p.unlink()
    store = Store(db)
    gw = RestFlagsmith(cfg.api_url, cfg.org_api_key)
    svc = Ulysses(store, gw, Settings(public_url="http://127.0.0.1:8200", refresh_wait_seconds=180))
    sources.mark(db, env, eid, "synthetic",
                 "identities sent by scripts/verify_live_run.py with a planted effect; Flagsmith assigns the variations")
    owner, second = signing.Keypair.generate(), signing.Keypair.generate()
    svc.register_signer("verify-owner", owner.public)
    svc.register_signer("verify-second", second.public)
    drafted = svc.draft_for_experiment(
        environment=env, experiment_id=eid, owner="verify-owner", second_signer="verify-second",
        min_relative_lift=1.0, baseline_rate=0.2, looks=2, identities_per_arm=100,
        override_reason=("Verification demo only: 100 synthetic identities per arm, two looks, a planted effect. "
                         "Far below the service's policy on purpose; not an experiment design."))
    h = drafted["hash"]
    svc.sign(h, "owner", "verify-owner", owner.sign(signing.contract_message(h, "owner")))
    svc.start_under_contract(h)
    print(f"experiment {eid} started under {h}")

    from flagsmith import Flagsmith
    from flagsmith.analytics import EventProcessorConfig

    sdk = Flagsmith(environment_key=demo["server_key"], api_url=cfg.api_url + "/api/v1/",
                    enable_local_evaluation=True, environment_refresh_interval_seconds=5, enable_events=True,
                    event_processor_config=EventProcessorConfig(events_api_url=cfg.api_url + "/api/",
                                                                flush_interval_seconds=1.0))
    time.sleep(3)
    rng = random.Random(eid)
    sent = 0
    fired = None
    try:
        for attempt in range(6):
            served = send(sdk, eid, sent, 140, rng)
            sent += 140
            time.sleep(8)
            out = svc.look(h)
            if out.get("too_soon"):
                time.sleep(out.get("retry_after_seconds", 300) + 5)
                out = svc.look(h)
            d = out.get("decision") or {}
            print(f"look {attempt + 1}: sent {served}, outcome {d.get('outcome')}, "
                  f"Flagsmith chance to win {d.get('chance_to_win')}, boundary z {d.get('boundary_z')}")
            if out.get("proposal"):
                fired = out["proposal"]
                break
            time.sleep(305)
    finally:
        sdk.close()
    if not fired:
        print("the contract did not fire; nothing to verify")
        return 1
    # The owner signs exactly the text the card shows: the contract, the look, the action, and a hash chain over
    # every look up to this one.
    msg = svc.proposal_card(fired["id"])["confirm_message"].encode()
    res = svc.confirm(fired["id"], "verify-owner", owner.sign(msg))
    print(f"executed {res['action']}: {res['state']}")

    bundle = RUN / "bundle.json"
    cli = [sys.executable, "-m", "ulysses.cli"]
    subprocess.run([*cli, "export-bundle", str(eid), "--db", str(db), "--out", str(bundle)], check=True)
    live = subprocess.run([*cli, "verify", str(eid), "--db", str(db)])
    # Offline, a bundle is only verified when something outside it pins it: here the contract hash this script
    # started the experiment under. Unpinned, the same bundle must come out CONSISTENT (unpinned), exit 2.
    unpinned = subprocess.run([*cli, "verify", "--bundle", str(bundle)])
    offline = subprocess.run([*cli, "verify", "--bundle", str(bundle), "--expect-hash", h])
    print(f"live verify exit {live.returncode}; offline unpinned exit {unpinned.returncode}; "
          f"offline pinned exit {offline.returncode}")
    return 0 if live.returncode == 0 and unpinned.returncode == 2 and offline.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
