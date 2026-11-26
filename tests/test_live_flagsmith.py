"""Integration test against a running Flagsmith (the shared lab or your own).

Skipped unless ULYSSES_LIVE=1 and FLAGSMITH_API_URL / FLAGSMITH_ADMIN_KEY /
FLAGSMITH_ORGANISATION_ID are set (``.env`` is read). It uses the real REST
gateway and the official Python SDK, and takes about a minute:

1. a fresh experiment on its own flag (status created);
2. an unsigned contract cannot start it; a signed one does, and the hash
   appears in the experiment's hypothesis in Flagsmith;
3. synthetic identities through the SDK, then one look: a real results
   refresh and read, and the decision is computed from Flagsmith's numbers;
4. a refresh inside five minutes is refused by Flagsmith and the service
   reports "too soon" without calling it;
5. a doubly signed "keep control now" amendment plus the owner's tap completes
   the experiment in Flagsmith with control;
6. Flagsmith's organisation audit-log webhook (pointed for the test at a copy
   of the service on port 8201) delivers the lifecycle entries, and the audit
   replay maps both lifecycle changes to signed contracts.

    ULYSSES_LIVE=1 uv run pytest tests/test_live_flagsmith.py -q
"""

from __future__ import annotations

import os
import secrets as _secrets
import threading
import time

import pytest

from ulysses import lab, signing
from ulysses.core import Refused, Settings, Ulysses
from ulysses.flagsmith.rest import RestFlagsmith
from ulysses.service.runtime import load_dotenv
from ulysses.store import Store

load_dotenv()
pytestmark = pytest.mark.live
LIVE = os.environ.get("ULYSSES_LIVE") == "1" and os.environ.get("FLAGSMITH_ADMIN_KEY")


@pytest.mark.skipif(not LIVE, reason="set ULYSSES_LIVE=1 with Flagsmith credentials to run")
def test_contract_lifecycle_on_real_flagsmith(tmp_path):
    cfg = lab.LabConfig.from_env()
    demo = lab.setup_demo(cfg)
    admin = lab.Admin(cfg)
    flag = "ulysses_integration_cta"  # its own flag, so it never disturbs a running demo
    fid, oid = admin.mv_flag(demo["project_id"], flag, "classic", "bold", "bold", "Ulysses integration test flag")
    admin.clear_active(demo["environment_key"], fid, replace=True)
    exp = admin.create_experiment(
        demo["environment_key"], feature_id=fid, option_id=oid, name="ulysses integration test",
        hypothesis="Integration test (synthetic identities).", control_value="classic",
        metrics=[(demo["metrics"]["purchase"], "increase"), (demo["metrics"]["refund"], "not_increase")])
    env, eid = demo["environment_key"], int(exp["id"])
    gw = RestFlagsmith(cfg.api_url, cfg.org_api_key)
    svc = Ulysses(Store(tmp_path / "it.db"), gw, Settings(public_url="http://test", refresh_wait_seconds=180))

    # Serve this test's own service so Flagsmith's audit webhook reaches it.
    import uvicorn

    from ulysses.service.app import create_app

    hook_secret = _secrets.token_urlsafe(16)
    app = create_app(svc, agent_token=_secrets.token_urlsafe(20), protected_secrets=[cfg.org_api_key],
                     webhook_secret=hook_secret)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8201, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    hook = lab.ensure_audit_webhook(cfg, "http://host.docker.internal:8201/webhooks/flagsmith", hook_secret)
    maya, sam = signing.Keypair.generate(), signing.Keypair.generate()
    svc.register_signer("it-maya", maya.public)
    svc.register_signer("it-sam", sam.public)

    drafted = svc.draft_for_experiment(environment=env, experiment_id=eid, owner="it-maya", second_signer="it-sam",
                                       min_relative_lift=0.5, baseline_rate=0.1, looks=6)
    h = drafted["hash"]
    with pytest.raises(Refused):
        svc.start_under_contract(h)
    svc.sign(h, "owner", "it-maya", maya.sign(signing.contract_message(h, "owner")))
    svc.start_under_contract(h)
    live = gw.get_experiment(env, eid)
    assert live.status == "running"
    assert h in live.hypothesis

    from flagsmith import Flagsmith
    from flagsmith.analytics import EventProcessorConfig

    sdk = Flagsmith(environment_key=demo["server_key"], api_url=cfg.api_url + "/api/v1/",
                    enable_local_evaluation=True, environment_refresh_interval_seconds=5, enable_events=True,
                    event_processor_config=EventProcessorConfig(events_api_url=cfg.api_url + "/api/",
                                                                flush_interval_seconds=1.0))
    time.sleep(3)
    served = {"control": 0, "bold": 0}
    for i in range(240):
        ident = f"it-{eid}-{i}"
        fl = sdk.get_experiment_flag(flag, identifier=ident)
        if getattr(fl, "variant", None) in served:
            served[fl.variant] += 1
            if i % 4 == 0:
                sdk.track_event("purchase", identifier=ident)
    sdk.flush_events()
    sdk.close()
    time.sleep(8)

    out = svc.look(h)
    assert out["refreshed"] is True
    snap = out["flagsmith"]
    assert snap and snap["identities"]["control"] > 0 and snap["identities"]["bold"] > 0
    d = out["decision"]
    assert d["identities_per_arm"] == min(snap["identities"]["control"], snap["identities"]["bold"])
    assert d["outcome"] in ("watch", "hold", "not_ready")  # far from the horizon with no planted effect

    again = svc.look(h)
    assert again.get("too_soon")

    am = svc.amend(h, {}, reason="integration test: stop now and keep control", action_now="keep_control")
    h2 = am["hash"]
    svc.sign(h2, "owner", "it-maya", maya.sign(signing.contract_message(h2, "owner")))
    svc.sign(h2, "second_signer", "it-sam", sam.sign(signing.contract_message(h2, "second_signer")))
    p = svc.store.proposals_for(h2)[0]
    res = svc.confirm(p["id"], "it-maya", maya.sign(signing.confirm_message(h2, p["id"], "keep_control")))
    assert res["state"] == "executed"
    done = gw.get_experiment(env, eid)
    assert done.status == "completed"
    assert done.raw.get("rollout_decision") == "control"
    assert f"Amendment {h2}" in done.hypothesis

    try:
        deadline = time.time() + 90
        mine: list = []
        while time.time() < deadline:
            rep = svc.audit_replay(demo["project_id"])
            mine = [r for r in rep["rows"] if str(r["experiment"]) == str(eid) and r["kind"] in ("start", "complete")]
            if len(mine) >= 2:
                break
            time.sleep(3)
        assert [r["verdict"] for r in mine] == ["signed", "signed"], mine
        assert not svc.store.deviations(env, eid)
    finally:
        lab.Admin(cfg).call("DELETE", f"/api/v1/organisations/{cfg.organisation_id}/webhooks/{hook['id']}/")
        server.should_exit = True
