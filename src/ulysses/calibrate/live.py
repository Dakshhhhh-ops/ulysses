"""Live engine calibration: A/A experiments on a running self-hosted Flagsmith.

Every number in the output comes from Flagsmith's results endpoint. The harness
only creates traffic and reads:

1. One A/A experiment per flag (Flagsmith allows one active experiment per
   flag), both variations serving the identical value ``same``, 50/50 split,
   one Occurrence metric. All experiments start together.
2. Before each look, synthetic identities are assigned by Flagsmith's own engine
   through the official Python SDK in local-evaluation mode
   (``get_experiment_flag`` records the exposure) until every arm has at least
   ``per_arm_per_look * look`` identities; each converts with the same
   probability, sent with ``track_event``.
3. Each look is a results refresh per experiment, respecting Flagsmith's
   five-minute floor, then a read of ``/results/``.
4. At each look the harness scores the banner (Winning over 95% or Losing under
   5%, as the dashboard shows it) and the Ulysses contract
   (``decision.evaluate``) on the same chance-to-win.

The identities are synthetic and their conversions are drawn by this script.
This measures the engine's behaviour under repeated looks; it is not user
traffic and is never presented as such. Experiments are completed (keeping
control) at the end.
"""

from __future__ import annotations

import json
import logging
import re
import random
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import contract as ct
from .. import lab
from ..decision import Observation, evaluate
from ..signing import Keypair
from .offline import LOSE, WIN, wilson

log = logging.getLogger("ulysses.calibrate.live")
REFRESH_GAP = 5 * 60 + 10  # Flagsmith's floor plus a margin
READ_DEADLINE = 420  # Flagsmith's task processor recomputes queued results one by one


def _retrying(admin, method: str, path: str, tries: int = 6, **kw):
    """Admin call that rides out a Flagsmith restart or a slow moment."""
    for i in range(tries):
        try:
            return admin.call(method, path, **kw)
        except Exception as exc:  # noqa: BLE001 - connection errors, 5xx, and 429 at the window's edge
            if i == tries - 1:
                raise
            m = re.search(r"available in (\d+) second", str(exc))
            time.sleep(int(m.group(1)) + 2 if m else 5 * (i + 1))
    return None


@dataclass
class ExpState:
    index: int
    flag: str
    feature_id: int
    option_id: int
    experiment_id: int | None = None
    served: dict[str, int] = field(default_factory=lambda: {"control": 0, lab.CAL_VARIATION: 0})
    sent: int = 0
    banner_call: str | None = None
    banner_look: int | None = None
    contract_call: str | None = None
    contract_look: int | None = None
    prior: list[float] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    last_as_of: str | None = None


def _contract(alpha: float, looks: int, horizon: int) -> ct.Contract:
    a, b = Keypair.generate(), Keypair.generate()
    return ct.draft(
        experiment=ct.ExperimentRef(project=0, environment="calibration", experiment=0, flag="aa",
                                    treatment=lab.CAL_VARIATION),
        hypothesis="A/A engine calibration (synthetic identities)",
        primary_metric=ct.Metric(name=lab.CAL_METRIC, direction="increase"),
        horizon=ct.Horizon(identities_per_arm=horizon, looks=looks),
        owner=ct.Signer(name="calibration-owner", public_key=a.public),
        second_signer=ct.Signer(name="calibration-second", public_key=b.public),
        alpha=alpha,
    )


def run(
    *,
    experiments: int = 100,
    looks: int = 12,
    per_arm_per_look: int = 100,
    rate: float = 0.10,
    alpha: float = 0.05,
    out_dir: str | Path = ".run/calibration",
    seed: int | None = None,
    progress=print,
) -> dict:
    cfg = lab.LabConfig.from_env()
    batch = secrets.token_hex(3)
    rng = random.Random(seed if seed is not None else int(batch, 16))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    trace = out / f"live-{batch}.jsonl"
    horizon = looks * per_arm_per_look
    contract = _contract(alpha, looks, horizon)

    progress(f"batch {batch}: {experiments} A/A experiments x {looks} looks, "
             f"{per_arm_per_look} synthetic identities per arm per look")
    cal = lab.setup_calibration(cfg, experiments)
    admin = lab.Admin(cfg)
    env = cal["environment_key"]
    states = [ExpState(i, f["flag"], f["feature_id"], f["option_id"]) for i, f in enumerate(cal["flags"])]

    for s in states:
        admin.clear_active(env, s.feature_id, replace=True)
        e = admin.create_experiment(
            env, feature_id=s.feature_id, option_id=s.option_id,
            name=f"ulysses A/A {s.index:03d} batch {batch}",
            hypothesis="A/A engine calibration: both variations serve the same value. Synthetic identities.",
            control_value=lab.CAL_VALUE, metrics=[(cal["metric_id"], "increase")],
        )
        s.experiment_id = int(e["id"])
    for s in states:
        admin.call("POST", f"/api/v1/environments/{env}/experiments/{s.experiment_id}/start/")
    progress(f"started {len(states)} experiments in environment {lab.CAL_ENV}")

    from flagsmith import Flagsmith
    from flagsmith.analytics import EventProcessorConfig

    time.sleep(3)
    sdk = Flagsmith(
        environment_key=cal["server_key"], api_url=cfg.api_url + "/api/v1/",
        enable_local_evaluation=True, environment_refresh_interval_seconds=30,
        enable_events=True,
        event_processor_config=EventProcessorConfig(events_api_url=cfg.api_url + "/api/", flush_interval_seconds=1.0),
    )
    time.sleep(2)

    def send_until(s: ExpState, target: int) -> None:
        guard = 0
        while min(s.served.values()) < target and guard < target * 10:
            ident = f"aa-{batch}-{s.index:03d}-{s.sent:06d}"
            s.sent += 1
            guard += 1
            fl = sdk.get_experiment_flag(s.flag, identifier=ident)
            v = getattr(fl, "variant", None)
            if not (getattr(fl, "experiment", None) and fl.experiment.in_experiment) or v not in s.served:
                continue
            s.served[v] += 1
            if rng.random() < rate:
                sdk.track_event(lab.CAL_EVENT, identifier=ident)

    last_refresh = 0.0
    with trace.open("a", encoding="utf-8") as tf:
        # The design is written down before any traffic, and the summary always scores under it.
        tf.write(json.dumps({"design": {"batch": batch, "experiments": experiments, "looks": looks,
                                        "per_arm_per_look": per_arm_per_look, "horizon": horizon,
                                        "alpha": alpha, "rate": rate}}) + chr(10))
        tf.flush()
        for k in range(1, looks + 1):
            t0 = time.time()
            for s in states:
                send_until(s, per_arm_per_look * k)
            sdk.flush_events()
            time.sleep(10)  # let the last event batch land before Flagsmith recomputes
            progress(f"look {k}: traffic sent in {time.time() - t0:.0f}s")
            wait = last_refresh + REFRESH_GAP - time.time()
            if wait > 0:
                progress(f"look {k}: waiting {wait:.0f}s for Flagsmith's refresh floor")
                time.sleep(wait)
            last_refresh = time.time()
            for s in states:
                _retrying(admin, "POST", f"/api/v1/environments/{env}/experiments/{s.experiment_id}/results/refresh/")
            deadline = time.time() + READ_DEADLINE
            pending = list(states)
            fresh: dict[int, dict] = {}
            while pending and time.time() < deadline:
                still = []
                for s in pending:
                    r = _retrying(admin, "GET", f"/api/v1/environments/{env}/experiments/{s.experiment_id}/results/")
                    res = (r or {}).get("results") or {}
                    if res.get("payload") and res.get("as_of") != s.last_as_of and \
                            (res.get("refresh_requested_at") or "") <= (res.get("as_of") or ""):
                        fresh[s.index] = res
                        s.last_as_of = res.get("as_of")
                    else:
                        still.append(s)
                pending = still
                if pending:
                    time.sleep(2)
            for s in states:
                res = fresh.get(s.index)
                row: dict[str, Any] = {"batch": batch, "experiment": s.experiment_id, "look": k}
                if res is None:
                    row["error"] = "results not recomputed in time"
                    s.rows.append(row)
                    tf.write(json.dumps(row) + "\n")
                    continue
                m = res["payload"]["metrics"][0]
                nc = int(m["variants"].get("control", {}).get("n", 0))
                nt = int(m["variants"].get(lab.CAL_VARIATION, {}).get("n", 0))
                inf = (m.get("inference") or {}).get(lab.CAL_VARIATION)
                ch = None if not inf else float(inf["chance_to_win"])
                row.update(n_control=nc, n_treatment=nt, chance_to_win=ch, as_of=res.get("as_of"),
                           served=dict(s.served))
                if s.banner_call is None and ch is not None and (ch > WIN or ch < LOSE):
                    s.banner_call = "winning" if ch > WIN else "losing"
                    s.banner_look = k
                if s.contract_call is None:
                    d = evaluate(contract, s.prior, Observation(nc, nt, ch))
                    if d.counted:
                        s.prior.append(d.fraction)
                    row["contract"] = {"outcome": d.outcome, "z": d.z, "boundary_z": d.boundary_z,
                                       "look_number": d.look_number}
                    if d.outcome in ("win", "loss", "horizon"):
                        s.contract_call = d.outcome
                        s.contract_look = k
                row["banner_verdict"] = None if ch is None else ("winning" if ch > WIN else "losing" if ch < LOSE else "inconclusive")
                s.rows.append(row)
                tf.write(json.dumps(row) + "\n")
            tf.flush()
            n_b = sum(1 for s in states if s.banner_call)
            n_c = sum(1 for s in states if s.contract_call in ("win", "loss"))
            progress(f"look {k}: refreshed {len(fresh)}/{len(states)}; banner has fired on {n_b}, contract on {n_c}")

    sdk.close()
    for s in states:
        try:
            admin.call("POST", f"/api/v1/environments/{env}/experiments/{s.experiment_id}/complete/",
                       json={"rollout_decision": "control"})
        except RuntimeError as exc:
            log.warning("could not complete %s: %s", s.experiment_id, exc)
    summary = summarise_trace(trace)
    (out / f"live-{batch}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out / "live.md").write_text(summary["markdown"], encoding="utf-8")
    return summary


def read_trace(path: str | Path) -> tuple[dict, list[dict]]:
    """(signed design, rows). The first line of a trace written by ``run`` is its design header."""
    lines = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
    if lines and "design" in lines[0]:
        return lines[0]["design"], lines[1:]
    return {}, lines


def summarise_trace(path: str | Path, *, signed_looks: int | None = None, signed_per_arm: int | None = None,
                    alpha: float | None = None, rate: float | None = None) -> dict:
    """The live table from a captured trace, scored under the design that was signed before the run.

    Every number comes from the Flagsmith reads stored in the trace. The design (looks, identities per
    arm per look, alpha) is read from the trace's header line; an older trace without a header needs it
    stated with ``signed_looks`` and ``signed_per_arm``, and those must be the values the run was started
    with. Nothing is re-fitted afterwards: if the run stopped before its last planned look, experiments
    the contract had not decided are reported as still holding, and the final look's "spend everything
    left" rule is never applied early.

    A look whose read did not come back in time counts as a look not taken (nothing shown, nothing spent).
    """
    design, rows = read_trace(path)
    if design:
        for name, given, signed in (("looks", signed_looks, design["looks"]),
                                    ("identities per arm per look", signed_per_arm, design["per_arm_per_look"])):
            if given is not None and given != signed:
                raise ValueError(f"the trace was signed with {name} = {signed}; refusing to score it as {given}")
        looks, per_arm = design["looks"], design["per_arm_per_look"]
        alpha = design.get("alpha", 0.05) if alpha is None else alpha
        rate = design.get("rate", 0.10) if rate is None else rate
    else:
        if signed_looks is None or signed_per_arm is None:
            raise ValueError("this trace has no design header; pass the looks and identities per arm per look "
                             "it was started with (never values chosen after the run)")
        looks, per_arm = signed_looks, signed_per_arm
        alpha = 0.05 if alpha is None else alpha
        rate = 0.10 if rate is None else rate
    if not rows:
        raise ValueError(f"{path} has no reads")
    batch = rows[0]["batch"]
    horizon = looks * per_arm
    contract = _contract(alpha, looks, horizon)
    by_exp: dict[int, list[dict]] = {}
    for r in rows:
        by_exp.setdefault(r["experiment"], []).append(r)
    last_look = max(r["look"] for r in rows)
    truncated = last_look < looks
    n = len(by_exp)
    bw = bl = fw = fl = cw = cl = ch = holding = skipped = 0
    for eid, rs in by_exp.items():
        rs = sorted(rs, key=lambda x: x["look"])
        banner = contract_call = None
        prior: list[float] = []
        for r in rs:
            if "error" in r:
                skipped += 1
                continue
            c = r.get("chance_to_win")
            if banner is None and c is not None and (c > WIN or c < LOSE):
                banner = "winning" if c > WIN else "losing"
            if contract_call is None:
                d = evaluate(contract, prior, Observation(r["n_control"], r["n_treatment"], c))
                if d.counted:
                    prior.append(d.fraction)
                if d.outcome in ("win", "loss", "horizon"):
                    contract_call = d.outcome
        bw += banner == "winning"
        bl += banner == "losing"
        last = rs[-1]
        if not truncated and "error" not in last:
            c = last.get("chance_to_win")
            fw += c is not None and c > WIN
            fl += c is not None and c < LOSE
        cw += contract_call == "win"
        cl += contract_call == "loss"
        ch += contract_call == "horizon"
        holding += contract_call is None

    def pct(k: int) -> str:
        lo, hi = wilson(k, n)
        return f"{k}/{n} = {100 * k / n:.1f}% ({100 * lo:.0f}-{100 * hi:.0f})"

    head = (f"Live engine calibration on self-hosted Flagsmith (batch `{batch}`): {n} A/A experiments, every "
            f"number read from Flagsmith's results endpoint. Signed design: {looks} looks at least five minutes "
            f"apart, horizon {horizon} identities per arm, contract alpha {alpha}. At least {per_arm} synthetic "
            f"identities per arm were sent per look, assigned by Flagsmith's engine (Python SDK, local "
            f"evaluation), conversion probability {rate:.0%} in both arms. Synthetic identities, not user "
            f"traffic. 95% Wilson intervals.")
    if truncated:
        head += (f" The run stopped after look {last_look} of {looks} (see the batch notes); the table covers the "
                 f"looks that were read, and the contract is scored under its signed design without re-fitting.")
    if skipped:
        head += f" {skipped} reads did not return in time and count as looks not taken."
    md = [head, "",
          "| Rule (A/A, no real difference) | False Winning (ships a do-nothing variation) | False Losing | Any false call |",
          "|---|---|---|---|",
          f"| Stop at the first look over 95% or under 5% (looks 1-{last_look}) | {pct(bw)} | {pct(bl)} | {pct(bw + bl)} |"]
    if not truncated:
        md.append(f"| Read once at the final look | {pct(fw)} | {pct(fl)} | {pct(fw + fl)} |")
    md.append(f"| Ulysses contract, alpha {alpha} two-sided | {pct(cw)} | {pct(cl)} | {pct(cw + cl)} |")
    md += ["", (f"Contract outcomes: {cw} wins, {cl} losses, {ch} at the horizon with no decision, {holding} still "
                f"holding" + (f" when the run stopped at look {last_look} of {looks}." if truncated else "."))]
    return {"label": "Live engine calibration on self-hosted Flagsmith with synthetic identities (not user traffic)",
            "batch": batch, "experiments": n, "design": {"looks": looks, "per_arm_per_look": per_arm,
                                                          "horizon": horizon, "alpha": alpha, "rate": rate},
            "looks": looks, "looks_read": last_look, "truncated": truncated, "skipped_reads": skipped,
            "banner_any_look": {"winning": bw, "losing": bl},
            "banner_final_look": None if truncated else {"winning": fw, "losing": fl},
            "contract": {"win": cw, "loss": cl, "horizon": ch, "holding": holding},
            "markdown": "\n".join(md) + "\n"}


def strip_trace(src: str | Path, dst: str | Path) -> Path:
    """A shareable copy of a trace with no timestamps (Flagsmith's ``as_of`` removed); same numbers."""
    design, rows = read_trace(src)
    out = []
    if design:
        out.append(json.dumps({"design": design}))
    for r in rows:
        out.append(json.dumps({k: v for k, v in r.items() if k != "as_of"}))
    p = Path(dst)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return p
