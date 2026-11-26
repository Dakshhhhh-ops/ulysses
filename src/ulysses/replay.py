"""Replay a recorded Ulysses run through the real service, store and pages.

A replay trace (``data/replay-*.jsonl``, format in ``docs/REPLAY.md``) holds what
a finished run's store recorded: the signers' public keys, each signed contract
in canonical form, the owner's signatures, every look's Flagsmith numbers and
the decision the service made, the refused agent calls, and the owner's signed
confirmation with what Flagsmith reported after the ``complete`` call. Times are
seconds since the first recorded event; the trace carries no calendar date or
clock time.

``Replayer`` feeds those events, in order, to a real ``Ulysses`` over a fresh
store. Nothing is copied into the store directly: contracts are submitted and
hashed again, signatures are verified against the keys named in the contract,
each look's decision is recomputed by the current engine from the recorded
Flagsmith numbers and compared with the recorded one, and the confirmation is
verified before the stand-in Flagsmith "completes" the experiment. The only
stand-in is Flagsmith itself (``ReplayFlagsmith``), which answers with the
recorded numbers and is never contacted over the network.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .core import Refused, Settings, Ulysses, UlyssesError
from .flagsmith.fake import FakeFlagsmith
from .flagsmith.gateway import (
    CONTROL_KEY,
    ExperimentInfo,
    MetricInfo,
    MetricResult,
    ResultsSnapshot,
    Throttled,
    Variation,
)
from .service import sources
from .store import Store

FORMAT = "ulysses-replay"
VERSION = 1

# Anything that looks like a calendar date or a clock time. The exporter refuses to write one.
_DATEISH = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}:\d{2}|\b\d{10}(\.\d+)?\b")

# Flagsmith metric polarity from the direction the contract signs, when no snapshot names it.
_POLARITY = {"increase": "up", "decrease": "down", "not_increase": "down", "not_decrease": "up"}


class TraceError(ValueError):
    pass


# =============================================================================== export


def _rows(db: sqlite3.Connection, sql: str) -> list[dict]:
    try:
        return [dict(r) for r in db.execute(sql)]
    except sqlite3.OperationalError:  # an older store without that table
        return []


def _iso_ts(value: Any) -> float | None:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def export_store(db_path: str | Path, *, title: str, flagsmith: str, note: str | None = None) -> list[dict]:
    """Read a finished run's store (read-only) and return the replay trace as a list of JSON lines.

    ``title`` and ``flagsmith`` (which Flagsmith the run used) go in the header as given. Sources come
    from ``data-sources.json`` next to the store, and experiment kinds from ``demo.json`` if present.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise TraceError(f"no store at {db_path}")
    db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    signers = _rows(db, "SELECT name, public_key, added_at FROM signers")
    contracts = _rows(db, "SELECT * FROM contracts")
    sigs = _rows(db, "SELECT * FROM signatures")
    looks = _rows(db, "SELECT * FROM looks")
    props = _rows(db, "SELECT * FROM proposals")
    refusals = _rows(db, "SELECT * FROM refusals")
    starts = _rows(db, "SELECT * FROM actions WHERE kind='start' AND ok=1")
    governed = {(r["environment"], r["experiment_id"]): r for r in _rows(db, "SELECT * FROM governed")}
    deviations = _rows(db, "SELECT id FROM deviations")
    faults = _rows(db, "SELECT id FROM service_faults")
    db.close()
    if not contracts:
        raise TraceError("the store holds no contracts")
    if any(c["supersedes"] for c in contracts) or any(s["role"] != "owner" for s in sigs):
        raise TraceError("replay format 1 does not carry amendments (a contract here supersedes another)")

    side = db_path.parent
    src_map = json.loads((side / sources.FILE).read_text(encoding="utf-8")) if (side / sources.FILE).exists() else {}
    demo = json.loads((side / "demo.json").read_text(encoding="utf-8")) if (side / "demo.json").exists() else {}
    kinds = {int(v["id"]): v.get("kind") for v in (demo.get("experiments") or {}).values() if "id" in v}

    times = [r["added_at"] for r in signers] + [r["created_at"] for r in contracts] + \
            [r["received_at"] for r in sigs] + [r["at"] for r in looks] + [r["at"] for r in refusals] + \
            [r["at"] for r in starts] + [r["updated_at"] for r in props]
    t0 = min(times)

    def t(x: float) -> float:
        return round(x - t0, 3)

    events: list[tuple[float, int, dict]] = []  # (time, tie-break order, event)
    for s in signers:
        events.append((s["added_at"], 0, {"event": "signer", "name": s["name"], "public_key": s["public_key"]}))

    first_look: dict[str, dict] = {}
    for lk in sorted(looks, key=lambda r: r["at"]):
        if lk["snapshot"] and lk["contract_hash"] not in first_look:
            first_look[lk["contract_hash"]] = json.loads(lk["snapshot"])
    seen_exp: set[tuple[str, int]] = set()
    for c in sorted(contracts, key=lambda r: r["created_at"]):
        canon = json.loads(c["canonical"])
        ref = canon["experiment"]
        key = (c["environment"], c["experiment_id"])
        if key not in seen_exp:
            seen_exp.add(key)
            gov = governed.get(key) or {}
            snap_metrics = (first_look.get(c["hash"]) or {}).get("metrics") or {}
            metrics = [{"name": canon["primary_metric"]["name"],
                        "direction": (snap_metrics.get(canon["primary_metric"]["name"]) or {}).get("direction")
                        or _POLARITY[canon["primary_metric"]["direction"]],
                        "expected_direction": canon["primary_metric"]["direction"]}]
            for g in canon.get("guardrails") or []:
                metrics.append({"name": g["name"],
                                "direction": (snap_metrics.get(g["name"]) or {}).get("direction")
                                or _POLARITY[g["direction"]],
                                "expected_direction": g["direction"]})
            option_ids = json.loads(gov["option_ids"]) if gov.get("option_ids") else [None]
            rollout = json.loads(gov["rollout"]) if gov.get("rollout") else None
            src = src_map.get(f"{c['environment']}:{c['experiment_id']}")
            events.append((c["created_at"], 1, {
                "event": "experiment", "environment": c["environment"], "id": c["experiment_id"],
                "kind": kinds.get(int(c["experiment_id"])), "flag": ref["flag"], "project": ref.get("project"),
                "feature_id": gov.get("feature_id"), "control": CONTROL_KEY, "treatment": ref["treatment"],
                "treatment_option_id": option_ids[0] if option_ids else None,
                "metrics": metrics, "rollout": rollout,
                "source": {"kind": src["kind"], "detail": src.get("detail")} if src else None,
            }))
        events.append((c["created_at"], 2, {"event": "contract", "hash": c["hash"], "canonical": canon}))

    for r in refusals:
        h = r["contract_hash"]
        m = re.fullmatch(r"asked for (\w+), contract says (\w+)", r["reason"] or "")
        if r["what"] == "start" and r["reason"] == "no valid owner signature":
            ev = {"event": "attempt", "call": "start_under_contract", "hash": h, "expect": "refused",
                  "recorded_reason": r["reason"]}
        elif r["what"] == "execute" and m:
            ev = {"event": "attempt", "call": "execute", "hash": h, "action": m.group(1), "expect": "refused",
                  "recorded_reason": r["reason"]}
        else:
            ev = {"event": "refusal_note", "what": r["what"], "hash": h, "recorded_reason": r["reason"]}
        events.append((r["at"], 3, ev))
    for s in sigs:
        events.append((s["received_at"], 4, {"event": "signature", "hash": s["contract_hash"], "role": s["role"],
                                             "signer": s["signer"], "signature": s["signature"]}))
    for a in starts:
        events.append((a["at"], 5, {"event": "start", "hash": a["contract_hash"]}))
    for lk in looks:
        snap = json.loads(lk["snapshot"]) if lk["snapshot"] else None
        flagsmith_view = None
        if snap is not None:
            as_of = _iso_ts(snap.get("as_of"))
            flagsmith_view = {"as_of_t": None if as_of is None else t(as_of), "identities": snap.get("identities") or {},
                              "metrics": snap.get("metrics") or {}, "srm_p_value": snap.get("srm_p_value")}
        events.append((lk["at"], 6, {"event": "look", "hash": lk["contract_hash"], "look_id": lk["id"],
                                     "refreshed": bool(lk["refreshed"]), "flagsmith": flagsmith_view,
                                     "decision": json.loads(lk["decision"])}))
    for p in props:
        ev = {"event": "proposal", "hash": p["contract_hash"], "proposal_id": p["id"], "look_id": p["look_id"],
              "outcome": p["outcome"], "action": p["action"]}
        autonomous = p.get("authorised_by") == "autonomy"
        if autonomous:
            ev["authorised_by"] = "signed_autonomy"
        events.append((p["created_at"], 7, ev))
        if autonomous and p["state"] in ("executed", "failed"):
            # No tap: the agent's execute call ran it under the contract's signed autonomy clause.
            result = json.loads(p["result"]) if p["result"] else None
            events.append((p["updated_at"], 8, {"event": "autonomous_execute", "proposal_id": p["id"],
                                                "hash": p["contract_hash"], "action": p["action"],
                                                "recorded_state": p["state"], "recorded_result": result}))
        elif p["confirm_signature"]:
            result = json.loads(p["result"]) if p["result"] else None
            events.append((p["updated_at"], 8, {"event": "confirm", "proposal_id": p["id"], "hash": p["contract_hash"],
                                                "signer": p["confirm_signer"], "signature": p["confirm_signature"],
                                                "recorded_state": p["state"], "recorded_result": result}))
    events.sort(key=lambda e: (e[0], e[1]))
    # Proposals are made by the look that fires; keep them right after it in the file.
    out_events = [{"t": t(at), **ev} for at, _, ev in events]
    span = out_events[-1]["t"] if out_events else 0.0
    counts = {k: sum(1 for e in out_events if e["event"] == k)
              for k in ("signer", "experiment", "contract", "signature", "start", "look", "attempt", "confirm")}
    if any(e["event"] == "autonomous_execute" for e in out_events):
        counts["autonomous_execute"] = sum(1 for e in out_events if e["event"] == "autonomous_execute")
    header = {
        "format": FORMAT, "version": VERSION, "title": title, "flagsmith": flagsmith,
        "recorded_span_s": span,
        "counts": counts,
        "not_replayed": {"deviations": len(deviations), "service_faults": len(faults),
                         "refusal_notes": sum(1 for e in out_events if e["event"] == "refusal_note")},
        "note": note,
    }
    lines = [header, *out_events]
    for line in lines:
        text = json.dumps(line)
        # Signatures, keys and hashes are base64/hex; only look for dates in the human-readable fields.
        scrub = {k: v for k, v in line.items() if k not in ("signature", "public_key", "canonical", "hash")}
        if _DATEISH.search(json.dumps(scrub)):
            raise TraceError(f"refusing to write a date or clock time into the trace: {text[:200]}")
    return lines


def write_trace(lines: list[dict], path: str | Path) -> None:
    Path(path).write_text("".join(json.dumps(x, sort_keys=False) + "\n" for x in lines), encoding="utf-8")


def load_trace(path: str | Path) -> tuple[dict, list[dict]]:
    text = Path(path).read_text(encoding="utf-8").splitlines()
    rows = [json.loads(x) for x in text if x.strip()]
    if not rows or rows[0].get("format") != FORMAT:
        raise TraceError(f"{path} is not a Ulysses replay trace (first line must carry \"format\": \"{FORMAT}\"). "
                         "Calibration traces (data/live-*.jsonl) are scored with `ulysses calibrate summarise`.")
    if rows[0].get("version") != VERSION:
        raise TraceError(f"{path} is replay format {rows[0].get('version')}; this checkout reads format {VERSION}")
    return rows[0], rows[1:]


# =============================================================================== replay


class ReplayFlagsmith(FakeFlagsmith):
    """The stand-in for Flagsmith in a replay: it answers with the recorded numbers, offline."""

    def __init__(self, clock: Callable[[], float]):
        super().__init__(clock=clock)
        self.next_refresh_ok = True

    def add_recorded(self, ev: dict) -> None:
        metrics = [MetricInfo(id=i, name=m["name"], direction=m["direction"], expected_direction=m["expected_direction"])
                   for i, m in enumerate(ev["metrics"], start=1)]
        r = ev.get("rollout") or {}
        raw = {"experiment_rollout": {
            "enabled": False, "rollout_percentage": r.get("rollout_percentage", 100.0),
            "feature_state_value": {"type": "string", "value": r.get("value")},
            "multivariate_feature_state_values": [{"multivariate_feature_option": o, "percentage_allocation": w}
                                                  for o, w in (r.get("weights") or [])]}}
        if ev["id"] in self.experiments:
            raise TraceError(f"two experiments with id {ev['id']} in one trace")
        self.experiments[ev["id"]] = ExperimentInfo(
            id=ev["id"], name=f"experiment {ev['id']}", status="created", hypothesis="",
            environment=ev["environment"], flag=ev["flag"], feature_id=ev.get("feature_id"),
            variations=[Variation(CONTROL_KEY, None), Variation(ev["treatment"], ev.get("treatment_option_id"))],
            metrics=metrics, project=ev.get("project"), raw=raw)
        # Feature Health: a recorded ship was made while Flagsmith reported the flag healthy (the service
        # refuses otherwise), so the stand-in answers with no health events. The trace carries no
        # environment name, so the key stands in for it.
        if ev["environment"] not in self.environments:
            self.add_environment(ev["environment"], name=ev["environment"], project=ev.get("project") or 0)

    def set_recorded(self, experiment_id: int, view: dict | None, base: float) -> None:
        if view is None:
            self.results.pop(experiment_id, None)
            return
        as_of = None if view.get("as_of_t") is None else \
            _dt.datetime.fromtimestamp(base + view["as_of_t"], _dt.timezone.utc).isoformat()
        self.results[experiment_id] = ResultsSnapshot(
            identities=dict(view.get("identities") or {}),
            metrics={k: MetricResult(k, m.get("direction") or "up", m.get("chance_to_win"), m.get("lift"))
                     for k, m in (view.get("metrics") or {}).items()},
            as_of=as_of, srm_p_value=view.get("srm_p_value"))

    def refresh_results(self, environment: str, experiment_id: int) -> None:
        self._exp(environment, experiment_id)
        if not self.next_refresh_ok:  # the recorded look found Flagsmith's five-minute floor in force
            raise Throttled("A refresh was requested recently. Try again later.")
        self.refresh_calls += 1


class ReplayStore(Store):
    """A store that keeps the recorded look and proposal ids, so recorded confirmations verify."""

    def __init__(self, path):
        super().__init__(path)
        self.want_look_id: str | None = None
        self.want_proposal_id: str | None = None

    def add_look(self, **kw) -> str:
        lid = super().add_look(**kw)
        if self.want_look_id:
            self._exec("UPDATE looks SET id=? WHERE id=?", (self.want_look_id, lid))
            lid, self.want_look_id = self.want_look_id, None
        return lid

    def add_proposal(self, **kw) -> str:
        pid = super().add_proposal(**kw)
        if self.want_proposal_id:
            self._exec("UPDATE proposals SET id=? WHERE id=?", (self.want_proposal_id, pid))
            pid, self.want_proposal_id = self.want_proposal_id, None
        return pid


DECISION_FIELDS = ("outcome", "action", "counted", "look_number", "identities_per_arm", "chance_to_win", "z",
                   "boundary_z", "fires")


@dataclass
class Step:
    t: float  # recorded seconds since the first event
    kind: str
    text: str
    ok: bool = True  # False: the service did not do what the recording says
    detail: dict = field(default_factory=dict)


class Replayer:
    """Feeds a trace to a real Ulysses over a fresh store in ``run_dir``; the replay clock starts at ``base``."""

    def __init__(self, header: dict, events: list[dict], run_dir: str | Path, *, public_url: str, speed: float,
                 base: float):
        self.header, self.events = header, events
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.base = base
        self.now = base
        self.speed = speed
        self.store = ReplayStore(self.run_dir / "replay.db")
        self.fs = ReplayFlagsmith(clock=lambda: self.now)
        # A recording can hold owner taps signed before the confirmation carried the looks' evidence chain;
        # the replay checks those against the earlier message. The live service never accepts that form.
        self.svc = Ulysses(self.store, self.fs, Settings(public_url=public_url, refresh_wait_seconds=0.0,
                                                         replay_accepts_earlier_tap_form=True),
                           clock=lambda: self.now, sleep=lambda s: None)
        self.mismatches: list[Step] = []
        self.done = 0
        self._finished = False

    def _source(self, ev: dict) -> None:
        rec = ev.get("source") or {}
        was = sources.KINDS.get(rec.get("kind"), "source not recorded")
        detail = (f"run recorded on {self.header.get('flagsmith')}, played back at {self.speed:.1f}x; "
                  f"Flagsmith is not contacted. Its traffic was: {was.lower()}"
                  + (f" ({rec['detail']})" if rec.get("detail") else ""))
        sources.mark(self.store.path, ev["environment"], ev["id"], "replay", detail)

    def _expect_refused(self, ev: dict, fn, *args) -> tuple[bool, str]:
        """True only when the service refuses with the reason the recording logged for this contract.

        Any other error (an unknown contract, a conflict, a different refusal) is not the recorded refusal.
        """
        before = len(self.store.refusals())
        try:
            fn(*args)
        except Refused as exc:
            logged = [r for r in self.store.refusals()[before:] if r["contract_hash"] == ev["hash"]]
            reason = logged[-1]["reason"] if logged else None
            if reason != ev.get("recorded_reason"):
                return False, (f"refused for a different reason: {reason or exc.message!r} "
                               f"(recorded: {ev.get('recorded_reason')!r})")
            return True, exc.message
        except UlyssesError as exc:
            return False, f"{type(exc).__name__}, not the recorded refusal: {exc.message}"
        return False, "the service accepted it"

    def play(self, ev: dict) -> Step | None:
        """Apply one recorded event at its recorded time on the replay clock."""
        self.now = self.base + ev["t"]
        try:
            step = self._apply(ev)
        except UlyssesError as exc:
            step = Step(ev["t"], ev["event"], f"{ev['event']}: the service refused what the recording did: "
                        f"{getattr(exc, 'message', exc)}", ok=False)
        self.done += 1
        if step is not None and not step.ok:
            self.mismatches.append(step)
        return step

    def steps(self) -> Iterator[Step]:
        for ev in self.events:
            step = self.play(ev)
            if step is not None:
                yield step
        step = self.finish()
        if step is not None:
            yield step

    def finish(self) -> Step | None:
        """After the last event: the replayed service must not have logged more service faults or deviations
        than the recorded run did (``header["not_replayed"]``). Call once, after playing every event."""
        if self._finished:
            return None
        self._finished = True
        rec = self.header.get("not_replayed") or {}
        got = {"service_faults": len(self.store.service_faults()), "deviations": len(self.store.deviations())}
        extra = {k: (int(rec.get(k) or 0), n) for k, n in got.items() if n > int(rec.get(k) or 0)}
        t = self.events[-1]["t"] if self.events else 0.0
        if not extra:
            return None
        detail = "; ".join(f"{n} {k.replace('_', ' ')} (recorded run: {r})" for k, (r, n) in extra.items())
        step = Step(t, "integrity", f"the replayed service logged more than the recording did: {detail}", ok=False,
                    detail={"service_faults": self.store.service_faults(), "deviations": self.store.deviations()})
        self.mismatches.append(step)
        return step

    def _short(self, h: str) -> str:
        return h.split(":", 1)[-1][:12]

    def _apply(self, ev: dict) -> Step | None:
        k, t = ev["event"], ev["t"]
        svc = self.svc
        if k == "signer":
            svc.register_signer(ev["name"], ev["public_key"])
            return Step(t, k, f"signer {ev['name']} registered (public key {ev['public_key'][:20]}...)")
        if k == "experiment":
            self.fs.add_recorded(ev)
            self._source(ev)
            return Step(t, k, f"experiment {ev['id']} on flag {ev['flag']}: control vs {ev['treatment']}"
                        + (f" ({ev['kind']})" if ev.get("kind") else ""))
        if k == "contract":
            out = svc.submit(ev["canonical"])
            ok = out["hash"] == ev["hash"]
            return Step(t, k, f"contract {self._short(out['hash'])} drafted; hash recomputed "
                        + ("matches the recording" if ok else f"DIFFERS from the recording ({self._short(ev['hash'])})"),
                        ok=ok)
        if k == "attempt":
            if ev["call"] == "start_under_contract":
                refused, why = self._expect_refused(ev, svc.start_under_contract, ev["hash"])
                label = "agent tries to start before the owner signs"
            else:
                refused, why = self._expect_refused(ev, svc.execute_for_contract, ev["hash"], ev["action"])
                label = f"agent asks to execute {ev['action']}"
            verdict = "refused" if refused else ("ACCEPTED" if why == "the service accepted it" else "NOT AS RECORDED")
            return Step(t, k, f"{label} -> {verdict}: {why}", ok=refused)
        if k == "refusal_note":
            return Step(t, k, f"(recorded refusal, not replayed) {ev['what']}: {ev['recorded_reason']}")
        if k == "signature":
            out = svc.sign(ev["hash"], ev["role"], ev["signer"], ev["signature"])
            ok = bool((out["signatures"].get(ev["role"]) or {}).get("verified"))
            return Step(t, k, f"{ev['signer']} signs contract {self._short(ev['hash'])} as {ev['role']}; "
                        f"Ed25519 signature {'verifies' if ok else 'DOES NOT VERIFY'} against the key in the contract",
                        ok=ok)
        if k == "start":
            out = svc.start_under_contract(ev["hash"])
            return Step(t, k, f"contract {self._short(ev['hash'])} starts its experiment (state {out['state']})",
                        ok=out["state"] == "active")
        if k == "look":
            c = svc.contract(ev["hash"])
            self.fs.set_recorded(c.experiment.experiment, ev["flagsmith"], self.base)
            self.fs.next_refresh_ok = ev["refreshed"]
            self.store.want_look_id = ev["look_id"]
            nxt = next((e for e in self.events if e["event"] == "proposal" and e["look_id"] == ev["look_id"]), None)
            self.store.want_proposal_id = nxt["proposal_id"] if nxt else None
            out = svc.look(ev["hash"])
            got = out.get("decision") or {}
            rec = ev["decision"]
            diff = {f: (rec.get(f), got.get(f)) for f in DECISION_FIELDS if rec.get(f) != got.get(f)}
            # A final read past the horizon now gets its boundary on the fractions as they landed
            # (docs/STATISTICS.md, "Looks that land late"). A recording made under the earlier rule differs in
            # that boundary alone; it is labelled, not hidden, and counts as reproduced only if the outcome
            # is the same.
            overrun = (set(diff) == {"boundary_z"} and got.get("counted") and got.get("fraction") == 1.0
                       and (got.get("identities_per_arm") or 0) > c.horizon.identities_per_arm)
            ok = (not diff or overrun) and out.get("look_id") == ev["look_id"]
            ctw = got.get("chance_to_win")
            same = "; same decision as recorded"
            if overrun:
                same = (f"; same outcome as recorded. This final read landed past the horizon, so its boundary is "
                        f"now computed on the fractions as they landed: z {diff['boundary_z'][1]}, where the "
                        f"recording, made under the earlier rule, has z {diff['boundary_z'][0]}")
            text = (f"look {out.get('look_id')}: Flagsmith chance to win "
                    f"{'n/a' if ctw is None else f'{ctw * 100:.2f}%'} at {got.get('identities_per_arm')} per arm -> "
                    f"{str(got.get('outcome')).upper()}"
                    + (f" (z {got['z']}, contract boundary z {got['boundary_z']})" if got.get("boundary_z") else "")
                    + (same if ok else f"; DIFFERS from the recording: {diff}"))
            return Step(t, k, text, ok=ok, detail={"decision": got, "card": out.get("card_url")})
        if k == "proposal":
            p = self.store.proposal(ev["proposal_id"])
            want_auto = ev.get("authorised_by") == "signed_autonomy"
            ok = (bool(p) and p["outcome"] == ev["outcome"] and p["action"] == ev["action"]
                  and (p.get("authorised_by") == "autonomy") == want_auto)
            how = ("is authorised in advance by the signed autonomy clause (no tap)" if want_auto
                   else "waits for the owner's tap")
            return Step(t, k, f"contract fired: {ev['outcome']} -> pre-signed action {ev['action']} {how}"
                        + ("" if ok else " (NOT reproduced by the service)"), ok=ok)
        if k == "autonomous_execute":
            out = svc.execute(ev["proposal_id"], requested_action=ev["action"])
            rec = ev.get("recorded_result") or {}
            got = out.get("result") or {}
            ok = (out["state"] == ev["recorded_state"] and got.get("serving") == rec.get("serving")
                  and out.get("authorised_by") == "signed_autonomy")
            return Step(t, k, f"agent executes {out['action']} under the signed autonomy clause, no tap -> "
                        f"flag {got.get('flag')} serves {got.get('serving')}"
                        + ("" if ok else f" (recorded: {rec.get('serving')}, {ev['recorded_state']})"), ok=ok)
        if k == "confirm":
            out = svc.confirm(ev["proposal_id"], ev["signer"], ev["signature"])
            rec = ev.get("recorded_result") or {}
            got = out.get("result") or {}
            ok = out["state"] == ev["recorded_state"] and got.get("serving") == rec.get("serving")
            return Step(t, k, f"{ev['signer']} taps confirm (signature verifies) -> {out['action']} executed; "
                        f"flag {got.get('flag')} serves {got.get('serving')}"
                        + ("" if ok else f" (recorded: {rec.get('serving')})"), ok=ok)
        return Step(t, k, f"unknown event {k!r} skipped", ok=False)


# =============================================================================== pages

_BANNER_CSS = (
    "#ulysses-replay{position:sticky;top:0;z-index:50;background:var(--hatch-amber),var(--sheet);"
    "border-bottom:2px solid var(--amber);padding:8px 16px;font:500 13px/18px var(--f-mono);color:var(--ink)}"
    "#ulysses-replay strong{font:600 13px/18px var(--f-ui);letter-spacing:.08em;color:var(--amber);"
    "background:var(--sheet);padding:1px 6px;border:1px solid var(--amber);border-radius:2px;margin-right:8px}"
    "#ulysses-replay span{background:var(--sheet);padding:1px 4px}"
)


class ReplayBanner:
    """ASGI middleware: every HTML page says it is a replay; ``/replay/status`` reports progress.

    While the replay runs, a small script on each page polls the status and reloads the page when
    a new recorded event has been played, so the bell and the cards follow the run.
    """

    def __init__(self, app, status: Callable[[], dict]):
        self.app = app
        self.status = status

    def banner(self) -> bytes:
        st = self.status()
        import html as _html

        text = _html.escape(st["label"])
        follow = "" if st.get("finished") else (
            "<script>(()=>{let seen=%d;const k='ulysses-replay-y';try{const y=sessionStorage.getItem(k);"
            "if(y!==null){scrollTo(0,+y);sessionStorage.removeItem(k)}}catch(e){}"
            "setInterval(async()=>{try{const r=await fetch('/replay/status');const s=await r.json();"
            "if(s.played!==seen){try{sessionStorage.setItem(k,String(scrollY))}catch(e){}location.reload()}}"
            "catch(e){}},2500)})()</script>" % st["played"])
        return (f'<style>{_BANNER_CSS}</style><div id="ulysses-replay" role="note" aria-label="Replay notice">'
                f'<strong>REPLAY</strong><span>{text}</span></div>{follow}').encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("path") == "/replay/status":
            body = json.dumps(self.status()).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        start: dict[str, Any] = {}
        chunks: list[bytes] = []
        is_html = False

        async def capture(message):
            nonlocal is_html
            if message["type"] == "http.response.start":
                start.update(message)
                ctype = dict((k.lower(), v) for k, v in message.get("headers", [])).get(b"content-type", b"")
                is_html = ctype.startswith(b"text/html")
                if not is_html:
                    await send(message)
                return
            if message["type"] == "http.response.body" and is_html:
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = b"".join(chunks)
                i = body.find(b"<body")
                j = body.find(b">", i) + 1 if i >= 0 else 0
                body = body[:j] + self.banner() + body[j:]
                body = body.replace(b"<title>", b"<title>Replay \xc2\xb7 ", 1)
                headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
                headers.append((b"content-length", str(len(body)).encode()))
                await send({**start, "headers": headers})
                await send({"type": "http.response.body", "body": body})
                return
            await send(message)

        return await self.app(scope, receive, capture)
