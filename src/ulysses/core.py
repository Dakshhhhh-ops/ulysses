"""The contract service's rules, independent of HTTP.

This is the gate. It holds the only Flagsmith client the agent's actions reach
and makes exactly three kinds of Flagsmith write, each only when a check passes:

* ``update_hypothesis``: to record the governing contract hash, amendments,
  deviations and the executed outcome on the experiment itself;
* ``start``: only for a contract whose owner signature verifies, inside its
  start window, and whose experiment is still in ``created``;
* ``complete`` (with Flagsmith's rollout decision): only for an outcome that a
  look produced from Flagsmith's results, whose action is the one the signed
  contract (or a doubly signed amendment) assigns to that outcome, after the
  owner's signed one-tap confirmation, and only while that contract still
  governs (not superseded, voided or deviated).

Before each write the service records an expected write: the experiment, the
kind of change and what Flagsmith must show afterwards, valid for a short
window. An author-less entry in Flagsmith's audit log (every API-key write has
no author) counts as the service's own only if it claims one such record, once,
inside its window, and Flagsmith shows the intended effect. Everything else on
a governed experiment or its flag is a deviation.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import ValidationError

from . import contract as ct
from . import signing
from .canonical import content_hash, short_hash
from .decision import FIRES, LookDecision, evaluate, observation_from_snapshot
from .flagsmith.gateway import (
    CONTROL_KEY,
    AuditEntry,
    ExperimentInfo,
    FlagsmithError,
    FlagsmithGateway,
    ResultsSnapshot,
    Throttled,
)
from .horizon import plan as horizon_plan
from .store import Store

log = logging.getLogger("ulysses")

ROLES = ("owner", "second_signer")
MARK = "[Ulysses]"

# Service policy. A contract outside it needs a signed design.override_reason.
POLICY_MAX_ALPHA = 0.10
POLICY_MAX_GUARDRAIL_ALPHA = 0.10
MIN_CONVERSIONS_AT_HORIZON = 100
SIDE_EFFECT_WINDOW = 60.0  # seconds after a start or complete in which Flagsmith's own extra rows are expected
WARN_CONVERSIONS_AT_HORIZON = 400
LOW_COUNT_WARNING = (
    "About {conv:.0f} expected conversions per arm at the horizon. At counts this low Flagsmith's relative-lift "
    "z is skewed: in the offline calibration the false-loss rate (keep control when there was no effect) ran "
    "above its nominal share, while the false-win rate stayed within it. See docs/STATISTICS.md."
)


class UlyssesError(Exception):
    status = 400

    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        return {"error": type(self).__name__, "detail": self.message, **self.extra}


class Refused(UlyssesError):
    """The authorisation boundary said no."""

    status = 403


class NotFound(UlyssesError):
    status = 404


class Invalid(UlyssesError):
    status = 400


class Conflict(UlyssesError):
    status = 409


@dataclass
class Settings:
    public_url: str = "http://127.0.0.1:8200"
    refresh_wait_seconds: float = 120.0
    poll_seconds: float = 3.0
    slack_webhook: str | None = None
    expected_write_window_seconds: float = 600.0


def _rollout_of(exp: ExperimentInfo) -> dict | None:
    """The part of Flagsmith's experiment rollout that decides who sees what."""
    r = (exp.raw or {}).get("experiment_rollout")
    if not isinstance(r, dict):
        return None
    out = {
        "enabled": r.get("enabled"),
        "rollout_percentage": r.get("rollout_percentage"),
        "weights": sorted([[m.get("multivariate_feature_option"), m.get("percentage_allocation")]
                           for m in r.get("multivariate_feature_state_values") or []], key=lambda x: str(x[0])),
        "value": (r.get("feature_state_value") or {}).get("value"),
    }
    return json.loads(json.dumps(out, sort_keys=True, default=str))  # the same shape it has after storage


def flagsmith_split(exp: ExperimentInfo) -> dict[str, float] | None:
    """The split Flagsmith's experiment rollout serves, in percent per variation key, control included.

    Each multivariate option carries its ``percentage_allocation`` of the enrolled identities and
    control takes the remainder (field guide 3.4). None when the rollout cannot be read.
    """
    r = (exp.raw or {}).get("experiment_rollout")
    if not isinstance(r, dict):
        return None
    by_id = {v.option_id: v.key for v in exp.variations if v.option_id is not None}
    out: dict[str, float] = {}
    for m in r.get("multivariate_feature_state_values") or []:
        key = by_id.get(m.get("multivariate_feature_option"))
        if key is None:
            return None
        out[key] = float(m.get("percentage_allocation") or 0)
    if not out or sum(out.values()) > 100:
        return None
    out[CONTROL_KEY] = 100.0 - sum(out.values())
    return out


class Ulysses:
    def __init__(
        self,
        store: Store,
        gateway: FlagsmithGateway,
        settings: Settings | None = None,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        notify: Callable[[str, dict], None] | None = None,
    ):
        self.store = store
        self._fs = gateway  # the write-capable client; never returned from any method
        self.settings = settings or Settings()
        self.clock = clock
        self.sleep = sleep
        self._notify = notify
        self._look_locks: dict[tuple[str, str, int], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # Lifecycle writes (start, complete) the service has sent and not yet seen return, per experiment.
        # Flagsmith run synchronously delivers its audit webhook inside the open write transaction, so
        # anything read back meanwhile shows the state from before the write.
        self._in_flight: dict[tuple[str, int], int] = {}
        self._deferred: dict[tuple[str, int], dict[str, AuditEntry]] = {}
        self._flight_guard = threading.Lock()
        self._repairing = threading.local()

    # ================================================================ signers

    def register_signer(self, name: str, public_key: str) -> dict:
        signing.decode_public(public_key)
        existing = self.store.signer(name)
        if existing and existing["public_key"] != public_key:
            raise Conflict(f"signer {name!r} is already registered with a different key")
        other = self.store.signer_by_key(public_key)
        if other and other["name"] != name:
            raise Conflict(f"that public key already belongs to signer {other['name']!r}")
        self.store.add_signer(name, public_key, self.clock())
        return {"name": name, "public_key": public_key}

    def enrol_token(self, name: str) -> str:
        return self.store.new_enrol_token(name, self.clock())

    def enrol_with_token(self, token: str, public_key: str) -> dict:
        name = self.store.peek_enrol_token(token)
        if not name:
            raise Refused("this enrolment link is not valid or has been used")
        try:
            signing.decode_public(public_key)
        except signing.SignatureError as exc:
            raise Invalid(str(exc)) from exc
        other = self.store.signer_by_key(public_key)
        if other and other["name"] != name:
            # Checked before the token is spent, so a bad request does not burn the link.
            raise Conflict(f"that public key already belongs to signer {other['name']!r}")
        with self.store.transaction():
            if not self.store.use_enrol_token(token):
                raise Refused("this enrolment link is not valid or has been used")
            # An enrolment link issued by the operator may rotate a signer's key.
            # Contracts naming the old key stop verifying, which is intended.
            self.store.add_signer(name, public_key, self.clock())
        return {"name": name, "public_key": public_key}

    # ============================================================== contracts

    def _load(self, h: str) -> tuple[ct.Contract, dict]:
        row = self.store.contract_row(h)
        if not row:
            raise NotFound(f"no contract {h}")
        recomputed = content_hash(row["canonical"])
        if recomputed != h:
            raise Refused(
                "stored contract does not match its hash; it was altered after submission",
                expected=h,
                actual=recomputed,
            )
        try:
            c = ct.from_canonical(row["canonical"])
        except ValidationError as exc:
            raise Refused(f"stored contract no longer validates: {exc.errors()[0]['msg']}") from exc
        if c.hash != h:
            raise Refused("stored contract does not round-trip to its hash")
        return c, row

    def _check_signers_registered(self, c: ct.Contract) -> None:
        for role, s in (("owner", c.owner), ("second_signer", c.second_signer)):
            reg = self.store.signer(s.name)
            if not reg:
                raise Invalid(f"{role} {s.name!r} is not a registered signer. Enrol them first: ulysses signer enrol {s.name}")
            if reg["public_key"] != s.public_key:
                raise Invalid(f"the {role} key in the contract is not {s.name!r}'s registered key")

    def _check_experiment_matches(self, c: ct.Contract, exp: ExperimentInfo) -> None:
        ref = c.experiment
        if exp.flag != ref.flag:
            raise Invalid(f"experiment {exp.id} runs on flag {exp.flag!r}, the contract says {ref.flag!r}")
        if ref.control != CONTROL_KEY:
            raise Invalid("Flagsmith's control variation is always keyed 'control'")
        for t in ref.treatments:
            if exp.variation(t) is None:
                raise Invalid(f"flag {exp.flag!r} has no variation keyed {t!r}")
        others = [v.key for v in exp.variations if v.key not in (CONTROL_KEY, *ref.treatments)]
        if others:
            raise Invalid(f"this flag also has {others}; a contract must name every treatment arm "
                          "(experiment.more_treatments), so the error is split across all of them")
        if c.sample_ratio is not None:
            if set(c.sample_ratio.split) != set(ref.arms):
                raise Invalid(f"sample_ratio.split must name exactly the arms {ref.arms}")
            live = flagsmith_split(exp)
            if live is not None:
                weighted = {k: v for k, v in live.items() if v > 0}
                if set(weighted) != set(ref.arms):
                    raise Invalid(f"Flagsmith's rollout gives traffic to {sorted(weighted)}; the contract's arms "
                                  f"are {ref.arms}")
                for k in ref.arms:
                    if abs(live[k] - c.sample_ratio.split[k]) > 0.05:
                        raise Invalid(
                            f"the signed split gives {k} {c.sample_ratio.split[k]}%, but Flagsmith's rollout "
                            f"gives it {live[k]}%. The sample-ratio check would test against the wrong split.")
        m = exp.metric(c.primary_metric.name)
        if m is None:
            raise Invalid(f"primary metric {c.primary_metric.name!r} is not attached to experiment {exp.id}")
        if m.expected_direction not in ("increase", "decrease"):
            raise Invalid(f"{m.name!r} is attached as a guardrail in Flagsmith, not a goal")
        if m.expected_direction != c.primary_metric.direction:
            raise Invalid(
                f"Flagsmith expects {m.name!r} to {m.expected_direction}; the contract says {c.primary_metric.direction}"
            )
        for g in c.guardrails:
            gm = exp.metric(g.name)
            if gm is None:
                raise Invalid(f"guardrail {g.name!r} is not attached to experiment {exp.id}")
            if gm.expected_direction != g.direction:
                raise Invalid(
                    f"Flagsmith has guardrail {g.name!r} as {gm.expected_direction}; the contract says {g.direction}"
                )

    def policy_findings(self, c: ct.Contract) -> tuple[list[str], list[str]]:
        """(problems that need a signed override_reason, warnings shown to the signers)."""
        problems: list[str] = []
        warnings: list[str] = []
        if c.boundary.alpha > POLICY_MAX_ALPHA:
            problems.append(f"alpha {c.boundary.alpha} is above the policy maximum of {POLICY_MAX_ALPHA}")
        if c.horizon.looks < 2:
            problems.append("a single look is not a sequential contract")
        for g in c.guardrails:
            if g.alpha > POLICY_MAX_GUARDRAIL_ALPHA:
                problems.append(f"guardrail {g.name} alpha {g.alpha} is above {POLICY_MAX_GUARDRAIL_ALPHA}")
        d = c.design
        if d and d.baseline_rate and d.min_relative_lift:
            try:
                hp = horizon_plan(min_relative_lift=d.min_relative_lift, baseline_rate=d.baseline_rate,
                                  alpha=c.boundary.alpha, power=d.power or 0.8, looks=c.horizon.looks,
                                  comparisons=c.comparisons)
                if c.horizon.identities_per_arm < hp.identities_per_arm:
                    problems.append(
                        f"horizon {c.horizon.identities_per_arm} per arm is below the {hp.identities_per_arm} "
                        f"needed to detect a {d.min_relative_lift:.0%} lift from {d.baseline_rate:.1%}")
            except ValueError as exc:
                problems.append(f"design cannot be checked: {exc}")
            conv = d.baseline_rate * c.horizon.identities_per_arm
            if conv < MIN_CONVERSIONS_AT_HORIZON:
                problems.append(
                    f"only about {conv:.0f} expected conversions per arm at the horizon; below "
                    f"{MIN_CONVERSIONS_AT_HORIZON} Flagsmith's relative-lift z is too skewed for the guarantee")
            elif conv < WARN_CONVERSIONS_AT_HORIZON:
                warnings.append(LOW_COUNT_WARNING.format(conv=conv))
        elif c.design is None or not c.design.baseline_rate:
            warnings.append("No baseline was recorded, so the service cannot check the horizon against the "
                            "smallest lift you care about.")
        if c.sample_ratio is None:
            warnings.append("No traffic split is signed, so a win can ship even if Flagsmith's exposure counts "
                            "show the split is broken (sample-ratio mismatch).")
        return problems, warnings

    def submit(self, contract: ct.Contract | dict | str) -> dict:
        """Store a draft contract. Accepts a Contract, its canonical dict, or TOML text."""
        try:
            if isinstance(contract, str):
                c = ct.from_toml(contract)
            elif isinstance(contract, dict):
                c = ct.from_canonical(contract)
            else:
                c = contract
        except ValidationError as exc:
            raise Invalid("contract does not validate", errors=_errors(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - TOML errors
            raise Invalid(f"contract could not be read: {exc}") from exc
        self._check_signers_registered(c)
        problems, _ = self.policy_findings(c)
        if problems and not (c.design and c.design.override_reason):
            raise Invalid("this design is outside the service's policy: " + "; ".join(problems)
                          + ". To proceed anyway, put the reason in design.override_reason; the signers will see it.",
                          problems=problems)
        exp = self._fs.get_experiment(c.experiment.environment, c.experiment.experiment)
        self._check_experiment_matches(c, exp)
        supersedes = None
        if c.amendment:
            supersedes = c.amendment.supersedes
            old, old_row = self._load(supersedes)
            if old.experiment != c.experiment:
                raise Invalid("an amendment must govern the same experiment")
            if old.owner != c.owner or old.second_signer != c.second_signer:
                raise Invalid("an amendment keeps the same owner and second signer")
            if old_row["state"] in ("superseded", "executed", "void"):
                raise Conflict(f"contract {short_hash(supersedes)} is {old_row['state']} and cannot be amended")
        h = c.hash
        created = self.store.put_contract(
            h, c.canonical(), c.experiment.environment, c.experiment.experiment, supersedes, self.clock()
        )
        return {**self.contract_status(h), "created": created}

    def draft_for_experiment(
        self,
        *,
        environment: str,
        experiment_id: int,
        owner: str,
        second_signer: str,
        min_relative_lift: float,
        baseline_rate: float | None = None,
        baseline_mean: float | None = None,
        baseline_sd: float | None = None,
        primary_metric: str | None = None,
        hypothesis: str | None = None,
        alpha: float = 0.05,
        power: float = 0.8,
        looks: int | None = None,
        identities_per_arm: int | None = None,
        expected_rate_per_hour: float | None = None,
        start_within_days: int = 30,
        guardrail_alpha: float = ct.GUARDRAIL_ALPHA,
        horizon_action: str = "keep_control",
        override_reason: str | None = None,
    ) -> dict:
        """Draft a contract from the experiment as Flagsmith describes it, and store it."""
        exp = self._fs.get_experiment(environment, experiment_id)
        treatments = [v.key for v in exp.variations if v.key != CONTROL_KEY]
        if not treatments:
            raise Invalid(f"flag {exp.flag!r} has no treatment variation")
        if len(treatments) > ct.MAX_TREATMENTS:
            raise Invalid(f"flag {exp.flag!r} has {len(treatments)} treatments; a contract covers at most "
                          f"{ct.MAX_TREATMENTS}")
        goals = [m for m in exp.metrics if not m.is_guardrail]
        if primary_metric:
            pm = exp.metric(primary_metric)
            if pm is None:
                raise Invalid(f"metric {primary_metric!r} is not attached to experiment {experiment_id}")
        elif goals:
            pm = goals[0]
        else:
            raise Invalid(f"experiment {experiment_id} has no goal metric attached in Flagsmith")
        o, s = self.store.signer(owner), self.store.signer(second_signer)
        if not o or not s:
            missing = owner if not o else second_signer
            raise Invalid(f"{missing!r} is not a registered signer. Enrol them first: ulysses signer enrol {missing}")
        rate = expected_rate_per_hour
        if rate is None:
            try:
                rate = self._fs.exposure_rate_per_hour(environment, experiment_id)
            except FlagsmithError:
                rate = None
        try:
            hp = horizon_plan(
                min_relative_lift=min_relative_lift, baseline_rate=baseline_rate, baseline_mean=baseline_mean,
                baseline_sd=baseline_sd, alpha=alpha, power=power, looks=looks, exposure_rate_per_hour=rate,
                comparisons=len(treatments),
            )
        except ValueError as exc:
            raise Invalid(str(exc)) from exc
        horizon = ct.Horizon(identities_per_arm=identities_per_arm or hp.identities_per_arm, looks=hp.looks)
        c = ct.draft(
            experiment=ct.ExperimentRef(
                project=exp.project or 0,
                environment=environment,
                experiment=experiment_id,
                flag=exp.flag,
                treatment=treatments[0],
                more_treatments=treatments[1:] or None,
            ),
            hypothesis=hypothesis or exp.hypothesis.split(MARK)[0].strip() or f"{treatments[0]} beats control on {pm.name}",
            primary_metric=ct.Metric(name=pm.name, direction=pm.expected_direction),
            guardrails=[
                ct.Guardrail(name=m.name, direction=m.expected_direction, alpha=guardrail_alpha)
                for m in exp.metrics
                if m.is_guardrail
            ],
            horizon=horizon,
            owner=ct.Signer(name=owner, public_key=o["public_key"]),
            second_signer=ct.Signer(name=second_signer, public_key=s["public_key"]),
            alpha=alpha,
            actions=ct.Actions(horizon=horizon_action),
            validity=ct.Validity(start_within_days=start_within_days),
            design=ct.Design(baseline_rate=baseline_rate, min_relative_lift=min_relative_lift, power=power,
                             override_reason=override_reason),
            sample_ratio=_signed_split(exp, [CONTROL_KEY, *treatments]),
        )
        out = self.submit(c)
        out["horizon_plan"] = hp.to_dict()
        return out

    def contract_status(self, h: str) -> dict:
        c, row = self._load(h)
        sigs = self.store.signatures(h)
        signed = {}
        for role in ROLES:
            sig = sigs.get(role)
            signed[role] = None if not sig else {
                "signer": sig["signer"],
                "verified": self._signature_valid(c, h, role),
            }
        start_by = self._start_deadline(h, c)
        unstarted = row["state"] in ("draft", "signed")
        problems, warnings = self.policy_findings(c)
        return {
            "hash": h,
            "state": row["state"],
            "experiment": {"environment": c.experiment.environment, "id": c.experiment.experiment,
                           "flag": c.experiment.flag},
            "signatures": signed,
            "needs": self._needs(c, sigs) if row["state"] in ("draft", "signed") else [],
            # The window only bounds the start; once running, the contract governs until it ends.
            "expired": unstarted and start_by is not None and self.clock() > start_by,
            "expires_in_hours": (None if (start_by is None or not unstarted)
                                 else round((start_by - self.clock()) / 3600, 1)),
            "supersedes": row["supersedes"],
            "sign_url": f"{self.settings.public_url}/sign/{h}",
            "summary": summarise(c),
            "policy": {"problems": problems, "warnings": warnings,
                       "override_reason": c.design.override_reason if c.design else None},
        }

    def contract(self, h: str) -> ct.Contract:
        return self._load(h)[0]

    def _needs(self, c: ct.Contract, sigs: dict) -> list[str]:
        roles = ROLES if c.amendment else ("owner",)
        return [r for r in roles if r not in sigs]

    def _signature_valid(self, c: ct.Contract, h: str, role: str) -> bool:
        sig = self.store.signatures(h).get(role)
        if not sig:
            return False
        named = c.owner if role == "owner" else c.second_signer
        reg = self.store.signer(named.name)
        if not reg or reg["public_key"] != named.public_key or sig["signer"] != named.name:
            return False
        return signing.is_valid(named.public_key, signing.contract_message(h, role), sig["signature"])

    def _fully_signed(self, c: ct.Contract, h: str) -> bool:
        roles = ROLES if c.amendment else ("owner",)
        return all(self._signature_valid(c, h, r) for r in roles)

    def _start_deadline(self, h: str, c: ct.Contract) -> float | None:
        root = self._root_hash(h)
        sig = self.store.signatures(root).get("owner")
        if not sig:
            return None
        root_c = self._load(root)[0] if root != h else c
        return sig["received_at"] + root_c.validity.start_within_days * 86400

    def _root_hash(self, h: str) -> str:
        seen = set()
        while True:
            row = self.store.contract_row(h)
            if not row or not row["supersedes"] or h in seen:
                return h
            seen.add(h)
            h = row["supersedes"]

    def sign(self, h: str, role: str, signer: str, signature: str) -> dict:
        if role not in ROLES:
            raise Invalid(f"role must be one of {ROLES}")
        c, row = self._load(h)
        if row["state"] not in ("draft", "signed"):
            raise Conflict(f"contract is {row['state']}; it can no longer be signed")
        named = c.owner if role == "owner" else c.second_signer
        if signer != named.name:
            raise Refused(f"the {role} of this contract is {named.name!r}, not {signer!r}")
        if role == "second_signer" and not c.amendment:
            raise Invalid("the second signer signs amendments; the original contract needs only the owner")
        reg = self.store.signer(signer)
        if not reg or reg["public_key"] != named.public_key:
            raise Refused(f"{signer!r} is not registered with the key named in the contract")
        try:
            signing.verify(named.public_key, signing.contract_message(h, role), signature)
        except signing.SignatureError as exc:
            self.store.log_refusal("sign", str(exc), h, self.clock())
            raise Refused(f"signature rejected: {exc}") from exc
        self.store.add_signature(h, role, signer, signature, self.clock())  # first signature's time is kept
        if self._fully_signed(c, h) and row["state"] == "draft":
            if c.amendment:
                self._activate_amendment(c, h)
            else:
                self.store.cas_contract_state(h, ("draft",), "signed")
        return self.contract_status(h)

    # ============================================================== writes to Flagsmith

    @contextmanager
    def _write_in_flight(self, ref):
        key = (ref.environment, ref.experiment)
        with self._flight_guard:
            self._in_flight[key] = self._in_flight.get(key, 0) + 1
        try:
            yield
        finally:
            with self._flight_guard:
                self._in_flight[key] -= 1
                if not self._in_flight[key]:
                    del self._in_flight[key]

    def write_in_flight(self, environment: str, experiment_id: int) -> bool:
        with self._flight_guard:
            return self._in_flight.get((environment, int(experiment_id)), 0) > 0

    def _judge_deferred(self, ref) -> list[dict]:
        """Judge the audit entries held back while a write was in flight, now that it has returned."""
        with self._flight_guard:
            held = self._deferred.pop((ref.environment, ref.experiment), {})
        out = []
        for entry in held.values():
            try:
                out.append(self.handle_audit(entry, _attempts=3))
            except Exception as exc:  # noqa: BLE001 - the entry is kept and judged again by sync_audit
                log.warning("deferred audit entry %s not judged yet: %s", entry.id, exc)
        try:
            self.drain_notes()
        except Exception as exc:  # noqa: BLE001
            log.warning("deviation notes not drained: %s", exc)
        return out

    def _fault(self, ref, h: str | None, what: str, detail: str, repaired: bool) -> int:
        fid = self.store.add_service_fault(environment=ref.environment, experiment_id=ref.experiment,
                                           contract_hash=h, what=what, detail=detail, repaired=repaired,
                                           now=self.clock())
        log.error("service fault on experiment %s: %s (%s)", ref.experiment, detail,
                  "repaired" if repaired else "NOT repaired")
        self._emit("service_fault", {"hash": h, "experiment": ref.experiment, "what": what, "detail": detail,
                                     "repaired": repaired})
        return fid

    def _expected_status(self, h: str) -> tuple[str, ...] | None:
        row = self.store.contract_row(h)
        st = row["state"] if row else None
        if st in ("active", "decided"):
            return ("running", "paused")
        if st == "executing":
            # The outcome note is written after the pre-signed complete returned, before the state moves on.
            return ("running", "paused", "completed")
        if st == "executed":
            return ("completed",)
        return None

    def _repair_revert(self, c: ct.Contract, h: str, exp: ExperimentInfo, cause: str) -> bool:
        """Flagsmith shows 'created' for an experiment the service started: a lost update, not a deviation.

        Nobody can move an experiment back to 'created' through Flagsmith's API, so this can only be a
        write that saved a stale copy of the row (a PATCH that read it before the start committed).
        Start it again under the same contract and record the fault.
        """
        ref = c.experiment
        if getattr(self._repairing, "on", False):
            return False  # a repair is already running in this thread; it records the outcome
        self._repairing.on = True
        try:
            return self._repair_revert_once(c, h, exp, cause)
        finally:
            self._repairing.on = False

    def _repair_revert_once(self, c: ct.Contract, h: str, exp: ExperimentInfo, cause: str) -> bool:
        ref = c.experiment
        repaired = False
        try:
            self._expect(ref, "start", h, expect={"status": "running"}, also=["rollout", "flag_change"])
            with self._write_in_flight(ref):
                self._fs.start(ref.environment, ref.experiment)
            repaired = self._fs.get_experiment(ref.environment, ref.experiment).status in ("running", "paused")
        except FlagsmithError as exc:
            log.error("could not restore the start of experiment %s: %s", ref.experiment, exc)
        self._fault(ref, h, "status_reverted",
                    f"experiment {ref.experiment} showed status {exp.status!r} after {cause}; the contract had "
                    "started it", repaired)
        self._judge_deferred(ref)
        return repaired

    def _expect(self, ref, kind: str, h: str | None, *, expect: dict, also: list[str] | None = None,
                proposal_id: str | None = None) -> int:
        """Record, before sending it, exactly which write the service is about to make."""
        return self.store.add_expected_write(
            environment=ref.environment, experiment_id=ref.experiment, kind=kind, contract_hash=h,
            proposal_id=proposal_id, expect=expect, also=also or [], now=self.clock(),
            window=self.settings.expected_write_window_seconds,
        )

    def _hypothesis_block(self, c: ct.Contract, h: str) -> str:
        return (
            f"{MARK} Governed by stop contract {h} (owner {c.owner.name}; amendments need {c.second_signer.name}). "
            f"Stop rule: Lan-DeMets O'Brien-Fleming, alpha {c.boundary.alpha} two-sided, up to {c.horizon.looks} looks, "
            f"horizon {c.horizon.identities_per_arm} identities per arm."
        )

    def _append_hypothesis(self, c: ct.Contract, h: str, line: str, *, base: str | None = None) -> None:
        """Append one line to the experiment's hypothesis, idempotently."""
        ref = c.experiment
        if self.write_in_flight(ref.environment, ref.experiment):
            # A PATCH now could save a stale copy of the row over the lifecycle write in flight.
            raise FlagsmithError("a lifecycle write to this experiment is in flight; the note waits")
        exp = self._fs.get_experiment(ref.environment, ref.experiment)
        current = exp.hypothesis if base is None else base
        if base is None and line in current:
            return
        text = (current.rstrip() + "\n" + line) if current.strip() else line
        aid = self.store.log_action(environment=ref.environment, experiment_id=ref.experiment,
                                    kind="update_hypothesis", contract_hash=h, proposal_id=None,
                                    detail={"line": line}, now=self.clock())
        ew = self._expect(ref, "update_hypothesis", h, expect={"contains": line})
        try:
            self._fs.update_hypothesis(ref.environment, ref.experiment, text)
        except FlagsmithError:
            self.store.mark_action_failed(aid)
            try:
                landed = line in self._fs.get_experiment(ref.environment, ref.experiment).hypothesis
            except FlagsmithError:
                landed = None
            if landed is not None:
                self.store.set_expected_landed(ew, landed)
            if not landed:
                raise
        self._check_after_note(c, h, exp)

    def _check_after_note(self, c: ct.Contract, h: str, before: ExperimentInfo) -> None:
        """A note PATCH saves the whole row in Flagsmith: confirm it changed nothing but the hypothesis."""
        ref = c.experiment
        try:
            after = self._fs.get_experiment(ref.environment, ref.experiment)
        except FlagsmithError:
            return  # the next look's state check reads it again
        want = self._expected_status(h)
        if after.status != before.status or (want and after.status not in want):
            if after.status == "created" and want == ("running", "paused"):
                self._repair_revert(c, h, after, "a hypothesis note")
            else:
                self._fault(ref, h, "status_changed_by_note",
                            f"status {before.status!r} became {after.status!r} across a hypothesis note", False)
        if _rollout_of(after) != _rollout_of(before):
            self._fault(ref, h, "rollout_changed_by_note",
                        f"rollout {_rollout_of(before)} became {_rollout_of(after)} across a hypothesis note", False)

    def start_under_contract(self, h: str) -> dict:
        c, row = self._load(h)
        now = self.clock()
        ref = c.experiment
        if not self._signature_valid(c, h, "owner"):
            self.store.log_refusal("start", "no valid owner signature", h, now)
            raise Refused("this contract has no valid owner signature; it cannot start an experiment",
                          sign_url=f"{self.settings.public_url}/sign/{h}")
        if not self._fully_signed(c, h):
            self.store.log_refusal("start", "amendment lacks the second signature", h, now)
            raise Refused("this amendment needs both the owner and the second signer")
        if row["state"] == "starting":
            # An earlier start may have landed without its response: reconcile from Flagsmith.
            return self._reconcile_start(c, h, first_error=None, raise_if_not=True)
        start_by = self._start_deadline(h, c)
        if start_by is not None and now > start_by:
            self.store.log_refusal("start", "start window closed", h, now)
            raise Refused(f"this contract had to start within {c.validity.start_within_days} days of signing; "
                          "draft and sign a new one")
        if row["state"] != "signed":
            raise Conflict(f"contract is {row['state']}; only a signed, unused contract can start an experiment")
        gov = self.store.governing_contract(ref.environment, ref.experiment)
        if gov:
            raise Conflict(f"experiment {ref.experiment} is already governed by {short_hash(gov['hash'])}")
        exp = self._fs.get_experiment(ref.environment, ref.experiment)
        self._check_experiment_matches(c, exp)
        if exp.status != "created":
            self.store.log_refusal("start", f"experiment is {exp.status}", h, now)
            raise Conflict(f"experiment {exp.id} is {exp.status}; a contract can only start a created experiment")
        if not self.store.cas_contract_state(h, ("signed",), "starting"):
            raise Conflict("another request is starting this contract")
        # From here the experiment is watched: a crash leaves the contract 'starting', which the
        # audit listener treats as governed and a retried start reconciles.
        self.store.set_governed(environment=ref.environment, experiment_id=ref.experiment, contract_hash=h,
                                feature_id=exp.feature_id, flag=exp.flag,
                                option_ids=[v.option_id for v in exp.variations if v.option_id is not None],
                                rollout=None)
        human = exp.hypothesis.strip() or c.hypothesis
        try:
            self._append_hypothesis(c, h, self._hypothesis_block(c, h), base=human)
        except FlagsmithError:
            self.store.cas_contract_state(h, ("starting",), "signed")
            raise
        aid = self.store.log_action(environment=ref.environment, experiment_id=ref.experiment, kind="start",
                                    contract_hash=h, proposal_id=None, detail=None, now=self.clock())
        self._expect(ref, "start", h, expect={"status": "running"}, also=["rollout", "flag_change"])
        try:
            with self._write_in_flight(ref):
                self._fs.start(ref.environment, ref.experiment)
        except FlagsmithError as exc:
            self.store.mark_action_failed(aid)
            try:
                return self._reconcile_start(c, h, first_error=str(exc), raise_if_not=True, exc=exc)
            finally:
                self._judge_deferred(ref)
        out = self._activate_started(c, h)
        self._judge_deferred(ref)
        return out

    def _reconcile_start(self, c: ct.Contract, h: str, *, first_error: str | None, raise_if_not: bool,
                         exc: Exception | None = None) -> dict:
        ref = c.experiment
        try:
            exp = self._fs.get_experiment(ref.environment, ref.experiment)
        except FlagsmithError:
            exp = None
        if exp is not None and exp.status in ("running", "paused") and h in exp.hypothesis:
            out = self._activate_started(c, h)
            out["reconciled"] = True
            if first_error:
                out["first_error"] = first_error
            return out
        if exp is not None and exp.status == "created":
            self.store.cas_contract_state(h, ("starting",), "signed")
        if exc is not None:
            raise exc
        raise Conflict("the start did not land in Flagsmith; the contract is back to 'signed', so start again")

    def _activate_started(self, c: ct.Contract, h: str) -> dict:
        ref = c.experiment
        exp = self._fs.get_experiment(ref.environment, ref.experiment)
        if exp.status == "created":
            # The start returned but Flagsmith shows it undone: a stale row was saved over it.
            self.store.cas_contract_state(h, ("starting", "signed"), "active")
            self._repair_revert(c, h, exp, "the start returned")
            exp = self._fs.get_experiment(ref.environment, ref.experiment)
        self.store.set_governed(environment=ref.environment, experiment_id=ref.experiment, contract_hash=h,
                                feature_id=exp.feature_id, flag=exp.flag,
                                option_ids=[v.option_id for v in exp.variations if v.option_id is not None],
                                rollout=_rollout_of(exp))
        for ew in self.store.expected_writes(ref.environment, ref.experiment):
            if ew["kind"] == "start" and ew["contract_hash"] == h:
                self.store.set_expected_landed(ew["id"], True)
        self.store.cas_contract_state(h, ("starting", "signed"), "active")
        self._emit("started", {"hash": h, "experiment": ref.experiment, "flag": ref.flag})
        return {**self.contract_status(h), "started": True}

    # ------------------------------------------------------------------ looks

    def _prior_fractions(self, c: ct.Contract, ref) -> list[float]:
        out: list[float] = []
        for lk in self.store.looks(ref.environment, ref.experiment):
            if not lk["counted"]:
                continue
            t = round(min(lk["identities_per_arm"] / c.horizon.identities_per_arm, 1.0), 6)
            if not out or t > out[-1]:
                out.append(t)
        return out

    def _refresh_and_read(self, ref, prev_as_of: str | None) -> tuple[ResultsSnapshot | None, bool, str]:
        note = ""
        try:
            self._fs.refresh_results(ref.environment, ref.experiment)
            refreshed = True
        except Throttled as exc:
            refreshed = False
            wait = exc.retry_after_seconds
            note = "Flagsmith's five-minute refresh floor applies" + (f" (about {wait:.0f}s left)" if wait else "") + \
                   "; read the latest computed results instead."
        snap = self._fs.get_results(ref.environment, ref.experiment)
        if refreshed:
            deadline = self.clock() + self.settings.refresh_wait_seconds
            while (snap is None or snap.as_of == prev_as_of) and self.clock() < deadline:
                self.sleep(self.settings.poll_seconds)
                snap = self._fs.get_results(ref.environment, ref.experiment)
            if snap is not None and prev_as_of is not None and snap.as_of == prev_as_of:
                note = "Flagsmith had not finished recomputing; this look uses its previous results."
        return snap, refreshed, note

    def _lock_for(self, ref, kind: str = "look") -> threading.Lock:
        with self._locks_guard:
            return self._look_locks.setdefault((kind, ref.environment, ref.experiment), threading.Lock())

    def look(self, h: str) -> dict:
        c, _ = self._load(h)
        lock = self._lock_for(c.experiment)
        if not lock.acquire(blocking=False):
            return {"hash": h, "state": "active", "too_soon": True, "in_progress": True, "retry_after_seconds": 30,
                    "detail": "A look on this experiment is already running."}
        try:
            return self._look_locked(h)
        finally:
            lock.release()

    def _look_locked(self, h: str) -> dict:
        c, row = self._load(h)
        ref = c.experiment
        now = self.clock()
        self.drain_notes()
        if row["state"] in ("decided", "executing", "executed"):
            fired = [p for p in self.store.proposals_for(h) if p["state"] != "void"]
            return {"hash": h, "state": row["state"], "already_decided": True,
                    "proposals": [self._proposal_view(p) for p in fired]}
        if row["state"] != "active":
            raise Refused(f"contract is {row['state']}; looks run only on an experiment started under this contract")
        last = self.store.last_refreshed_look_at(ref.environment, ref.experiment)
        gap = c.horizon.min_minutes_between_looks * 60
        if last is not None and now - last < gap:
            return {"hash": h, "state": "active", "too_soon": True,
                    "retry_after_seconds": round(gap - (now - last)),
                    "detail": f"The contract allows a refresh every {c.horizon.min_minutes_between_looks} minutes."}
        try:
            drift = self._state_check(c, h)
            if drift:
                return drift
            looks = self.store.looks(ref.environment, ref.experiment)
            prev_as_of = next((lk["snapshot"].get("as_of") for lk in reversed(looks) if lk["snapshot"]), None)
            snap, refreshed, note = self._refresh_and_read(ref, prev_as_of)
        except FlagsmithError as exc:
            # Flagsmith did not answer: no look is recorded and nothing is spent.
            return {"hash": h, "state": "active", "look_not_taken": True, "retry_after_seconds": 60,
                    "detail": f"Flagsmith did not answer ({exc}); no look was recorded. Try again shortly."}
        obs = observation_from_snapshot(c, snap)
        d = evaluate(c, self._prior_fractions(c, ref), obs)
        snap_view = None if snap is None else {
            "as_of": snap.as_of,
            "identities": snap.identities,
            # Per-treatment entries ("<metric>@<arm>") are kept: a several-treatment card reads its arm's lift.
            "metrics": {k: {"chance_to_win": m.chance_to_win, "lift": m.lift, "direction": m.direction}
                        for k, m in snap.metrics.items()},
            "srm_p_value": snap.srm_p_value,
        }
        out: dict[str, Any] = {"hash": h, "refreshed": refreshed, "note": note, "decision": d.to_dict(),
                               "flagsmith": snap_view}
        with self.store.transaction():
            look_id = self.store.add_look(
                contract_hash=h, environment=ref.environment, experiment_id=ref.experiment, counted=d.counted,
                identities_per_arm=d.identities_per_arm, outcome=d.outcome, decision=d.to_dict(), snapshot=snap_view,
                refreshed=refreshed, now=self.clock(),
            )
            pid = None
            if d.fires and self.store.cas_contract_state(h, ("active",), "decided"):
                pid = self.store.add_proposal(contract_hash=h, look_id=look_id, outcome=d.outcome,
                                              action=d.action or "keep_control", state="pending", now=self.clock())
        out["look_id"] = look_id
        out["card_url"] = f"{self.settings.public_url}/card/{look_id}"
        if pid:
            out["proposal"] = self._proposal_view(self.store.proposal(pid))
            self._emit("fired", {"hash": h, "outcome": d.outcome, "action": d.action, "reason": d.reason,
                                 "card_url": out["card_url"]})
        return out

    def _state_check(self, c: ct.Contract, h: str) -> dict | None:
        """Compare Flagsmith's experiment with what the service started; record drift as a deviation."""
        ref = c.experiment
        exp = self._fs.get_experiment(ref.environment, ref.experiment)
        gov = self.store.governed_row(ref.environment, ref.experiment)
        problems = []
        if exp.status == "created":
            if self._repair_revert(c, h, exp, "the service's own writes"):
                return None
            return {"hash": h, "state": "active", "look_not_taken": True, "retry_after_seconds": 60,
                    "detail": "Flagsmith shows the experiment as not started and restarting it failed. "
                              "Recorded as a service fault, not a deviation."}
        if exp.status not in ("running", "paused"):
            problems.append(("complete" if exp.status == "completed" else "status",
                             f"experiment is {exp.status} in Flagsmith, but the contract never completed it"))
        snap = gov.get("rollout") if gov else None
        now_rollout = _rollout_of(exp)
        if snap is not None and now_rollout is not None and now_rollout != snap:
            problems.append(("rollout", f"rollout changed outside the contract: {snap} -> {now_rollout}"))
            self.store.set_governed(environment=ref.environment, experiment_id=ref.experiment, contract_hash=h,
                                    feature_id=gov["feature_id"], flag=gov["flag"], option_ids=gov["option_ids"],
                                    rollout=now_rollout)
        if not problems:
            return None
        for kind, text in problems:
            self._record_deviation(c, h, audit_id=None, log=text, author=None, kind=kind, source="state check",
                                   freeze=kind in ("complete", "status"))
        frozen = any(k in ("complete", "status") for k, _ in problems)
        if frozen:
            return {"hash": h, "state": "deviated", "deviation": [t for _, t in problems],
                    "detail": "Flagsmith shows a lifecycle change the contract did not make; recorded as a deviation."}
        return None

    # -------------------------------------------------------------- execution

    def _proposal_view(self, p: dict) -> dict:
        return {
            "id": p["id"], "outcome": p["outcome"], "action": p["action"], "state": p["state"],
            "look_id": p["look_id"], "contract": p["contract_hash"],
            "confirm_url": f"{self.settings.public_url}/card/{p['look_id'] or p['id']}",
            "result": json.loads(p["result"]) if p["result"] else None,
        }

    def proposal(self, pid: str) -> dict:
        p = self.store.proposal(pid)
        if not p:
            raise NotFound(f"no proposal {pid}")
        return self._proposal_view(p)

    def _authorised_action(self, c: ct.Contract, outcome: str) -> str:
        if outcome == "amendment":
            if not c.amendment or not c.amendment.action_now:
                raise Refused("this amendment authorises no immediate action")
            return c.amendment.action_now
        if outcome not in FIRES:
            raise Refused(f"{outcome!r} is not an outcome the contract acts on")
        return getattr(c.actions, outcome)

    def _governing(self, p: dict) -> tuple[ct.Contract, dict]:
        """The contract behind a proposal, refusing unless it still governs right now."""
        c, row = self._load(p["contract_hash"])
        if p["state"] == "void":
            raise Refused("this proposal was voided by an amendment; it authorises nothing")
        if row["state"] in ("superseded", "void", "deviated"):
            raise Refused(f"contract {short_hash(p['contract_hash'])} is {row['state']}; "
                          "its proposals no longer authorise anything")
        # 'executing' is left behind only by an execution that crashed mid-write; a retry reconciles it.
        expected = ("decided", "executing") if p["outcome"] != "amendment" else ("decided", "signed", "executing")
        if row["state"] not in expected:
            raise Refused(f"contract is {row['state']}; this proposal cannot run now")
        return c, row

    def confirm_message(self, p: dict) -> bytes:
        """What the owner signs to confirm proposal ``p`` (the text the card shows).

        For an action a look fired it covers, besides the contract, the look and the action, a hash chain over
        every look on the experiment up to and including that look (``signing.evidence_hash``), so a later edit
        to any of those looks' numbers, or a dropped look, breaks the owner's signature.
        """
        if not p["look_id"]:
            return self._legacy_confirm_message(p)
        lk = self.store.look(p["look_id"])
        looks = [] if not lk else [x for x in self.store.looks(lk["environment"], lk["experiment_id"])
                                   if x["seq"] <= lk["seq"]]
        evidence = signing.evidence_hash((x["seq"], x["id"], x["contract_hash"], x["snapshot"]) for x in looks)
        return signing.confirm_message(p["contract_hash"], p["look_id"], p["action"], evidence)

    @staticmethod
    def _legacy_confirm_message(p: dict) -> bytes:
        """The earlier confirmation text: contract, look or proposal, and action, without the looks' numbers."""
        return signing.confirm_message(p["contract_hash"], p["look_id"] or p["id"], p["action"])

    def _confirm_valid(self, c: ct.Contract, p: dict, signature: str | None) -> bool:
        """The owner's tap verifies, in the current form or the earlier one (``ulysses verify`` marks the latter)."""
        return any(signing.is_valid(c.owner.public_key, m, signature or "")
                   for m in (self.confirm_message(p), self._legacy_confirm_message(p)))

    def confirm(self, pid: str, signer: str, signature: str) -> dict:
        p = self.store.proposal(pid)
        if not p:
            raise NotFound(f"no proposal {pid}")
        if p["state"] == "executed":
            return self._proposal_view(p)
        c, _ = self._governing(p)
        if p["state"] not in ("pending", "failed"):
            raise Refused(f"this proposal is {p['state']}; there is nothing to confirm")
        if signer != c.owner.name:
            raise Refused(f"only the owner ({c.owner.name}) confirms the pre-signed action")
        msg = self.confirm_message(p)
        try:
            if not signing.is_valid(c.owner.public_key, self._legacy_confirm_message(p), signature):
                signing.verify(c.owner.public_key, msg, signature)
        except signing.SignatureError as exc:
            self.store.log_refusal("confirm", str(exc), p["contract_hash"], self.clock())
            raise Refused(f"confirmation signature rejected: {exc}") from exc
        # Only from the state checked above: an execution that finished meanwhile is never set back.
        if not self.store.cas_proposal(pid, ("pending", "failed"), self.clock(), state="confirmed",
                                       confirm_signer=signer, confirm_signature=signature):
            now = self.store.proposal(pid)
            if now and now["state"] == "executed":
                return self._proposal_view(now)
            raise Conflict(f"this proposal became {now['state'] if now else 'missing'} while the confirmation "
                           "was checked; nothing was changed")
        return self.execute(pid)

    def execute(self, pid: str, requested_action: str | None = None) -> dict:
        p = self.store.proposal(pid)
        if not p:
            raise NotFound(f"no proposal {pid}")
        h = p["contract_hash"]
        now = self.clock()
        if requested_action is not None and requested_action != p["action"]:
            self.store.log_refusal("execute", f"asked for {requested_action}, contract says {p['action']}", h, now)
            raise Refused(f"the contract authorises {p['action']!r} for this outcome, not {requested_action!r}")
        if p["state"] == "executed":
            return self._proposal_view(p)
        # One execution per experiment at a time in this process; the contract-state claim below
        # covers anything else (an amendment or a second worker).
        ref = self._load(h)[0].experiment
        with self._lock_for(ref, kind="write"):
            try:
                return self._execute_locked(pid)
            finally:
                self._judge_deferred(ref)

    def _execute_locked(self, pid: str) -> dict:
        p = self.store.proposal(pid)
        h = p["contract_hash"]
        if p["state"] == "executed":
            return self._proposal_view(p)
        # Re-check everything at the moment of execution, from stored evidence.
        c, row = self._governing(p)
        if p["state"] not in ("confirmed", "failed"):
            raise Refused("waiting for the owner's one-tap confirmation",
                          confirm_url=f"{self.settings.public_url}/card/{p['look_id'] or p['id']}")
        if not self._fully_signed(c, h):
            raise Refused("the governing contract's signatures no longer verify")
        if p["confirm_signer"] != c.owner.name or not self._confirm_valid(c, p, p["confirm_signature"]):
            raise Refused("the confirmation signature does not verify")
        if self._authorised_action(c, p["outcome"]) != p["action"]:
            raise Refused("this action is not the one the contract assigns to the outcome")
        lk = None
        if p["look_id"]:
            lk = self.store.look(p["look_id"])
            if not lk or lk["outcome"] != p["outcome"] or lk["contract_hash"] != h:
                raise Refused("the look behind this proposal does not show this outcome")
        ref = c.experiment
        ship = None
        if p["action"] == "ship_treatment":
            winner = (lk or {}).get("decision", {}).get("winner") if lk else None
            if c.comparisons > 1:
                # Several treatments: ship exactly the one the look found crossing its boundary.
                if winner not in ref.treatments:
                    raise Refused("this contract has several treatments and the look behind the proposal names "
                                  "no winning treatment, so there is nothing it can ship")
                ship = winner
            else:
                ship = ref.treatment
        exp = self._fs.get_experiment(ref.environment, ref.experiment)
        earlier = [ew for ew in self.store.expected_writes(ref.environment, ref.experiment)
                   if ew["kind"] == "complete" and ew["proposal_id"] == pid]
        reconcile = exp.status not in ("running", "paused")
        if reconcile and not (earlier and self._completed_as(exp, ship)):
            raise Conflict(f"experiment is {exp.status}; nothing to complete")
        # Claim the contract for this write. The claim fails if an amendment superseded it, or a
        # deviation froze it, since the checks above ran; an amendment cannot supersede it after.
        prior = row["state"] if row["state"] != "executing" else "decided"
        if not self.store.cas_contract_state(h, (row["state"],), "executing"):
            now_state = (self.store.contract_row(h) or {}).get("state")
            self.store.log_refusal("execute", f"contract became {now_state} during the checks", h, self.clock())
            raise Refused(f"contract is now {now_state}; it changed while this execution was being checked, "
                          "so nothing was sent to Flagsmith")
        try:
            res = self._send_complete(c, h, p, exp, ship, earlier, reconcile)
        except BaseException:
            self.store.cas_contract_state(h, ("executing",), prior)
            raise
        line = (f"{MARK} Contract {short_hash(h)} fired: {p['outcome'].replace('_', ' ')}. "
                f"Executed '{p['action'].replace('_', ' ')}' after the owner's signed confirmation; "
                f"flag {ref.flag} now serves {ship or CONTROL_KEY} to everyone.")
        try:
            self._append_hypothesis(c, h, line)
        except FlagsmithError as exc:
            log.warning("could not record the outcome on the experiment: %s", exc)
        result = {"flagsmith": res, "serving": ship or CONTROL_KEY, "flag": ref.flag}
        with self.store.transaction():
            self.store.update_proposal(pid, self.clock(), state="executed", result=json.dumps(result))
            self.store.set_contract_state(h, "executed")
        self._emit("executed", {"hash": h, "action": p["action"], "serving": ship or CONTROL_KEY})
        return self._proposal_view(self.store.proposal(pid))

    def _send_complete(self, c: ct.Contract, h: str, p: dict, exp: ExperimentInfo, ship: str | None,
                       earlier: list[dict], reconcile: bool) -> dict:
        ref = c.experiment
        pid = p["id"]
        if reconcile:
            # A previous attempt landed even though its response never came back.
            self.store.set_expected_landed(earlier[-1]["id"], True)
            return {"reconciled": True, "response_status": exp.status}
        aid = self.store.log_action(environment=ref.environment, experiment_id=ref.experiment, kind="complete",
                                    contract_hash=h, proposal_id=pid, detail={"ship": ship or CONTROL_KEY},
                                    now=self.clock())
        ew = self._expect(ref, "complete", h, proposal_id=pid,
                          expect={"status": "completed", "ship": ship or CONTROL_KEY,
                                  "rollout_variant": (exp.variation(ship).option_id if ship else None)},
                          also=["flag_change", "rollout"])
        try:
            with self._write_in_flight(ref):
                res = self._fs.complete(ref.environment, ref.experiment, ship)
            self.store.set_expected_landed(ew, True)
            return res
        except FlagsmithError as exc:
            # The request may have reached Flagsmith; read back before calling it failed.
            try:
                after = self._fs.get_experiment(ref.environment, ref.experiment)
            except FlagsmithError:
                after = None
            if after is not None and self._completed_as(after, ship):
                self.store.set_expected_landed(ew, True)
                return {"reconciled": True, "response_status": after.status, "first_error": str(exc)}
            if after is not None and after.status in ("running", "paused"):
                self.store.set_expected_landed(ew, False)
            self.store.mark_action_failed(aid)
            self.store.update_proposal(pid, self.clock(), state="failed",
                                       result=json.dumps({"error": str(exc), "retry": "call execute again"}))
            raise

    @staticmethod
    def _completed_as(exp: ExperimentInfo, ship: str | None) -> bool:
        """Did Flagsmith complete this experiment with exactly the decision we sent?"""
        if exp.status != "completed":
            return False
        decision = exp.raw.get("rollout_decision")
        if ship is None or ship == CONTROL_KEY:
            return decision == "control"
        v = exp.variation(ship)
        return decision == "variant" and v is not None and exp.raw.get("rollout_variant") == v.option_id

    def execute_for_contract(self, h: str, action: str) -> dict:
        """Agent path: run ``action`` for whatever outcome this contract has fired."""
        _, row = self._load(h)
        props = [p for p in self.store.proposals_for(h) if p["state"] in ("pending", "confirmed", "executed", "failed")]
        if not props:
            self.store.log_refusal("execute", f"no fired outcome authorises {action}", h, self.clock())
            raise Refused(f"no look has fired this contract, so nothing authorises {action!r}")
        return self.execute(props[-1]["id"], requested_action=action)

    # -------------------------------------------------------------- amendments

    AMENDABLE = {"horizon", "primary_metric", "guardrails", "actions", "validity", "boundary", "design",
                 "require_guardrail_results_to_ship"}

    def amend(self, h: str, changes: dict, reason: str, action_now: str | None = None) -> dict:
        old, row = self._load(h)
        if row["state"] in ("superseded", "executed", "void"):
            raise Conflict(f"contract is {row['state']} and cannot be amended")
        if row["state"] == "executing":
            raise Conflict("this contract is carrying out its action right now; amend it once that finishes")
        if row["state"] == "deviated":
            # Lets two signers recover a contract frozen by a deviation, if the experiment is still running.
            exp = self._fs.get_experiment(old.experiment.environment, old.experiment.experiment)
            if exp.status not in ("running", "paused"):
                raise Conflict(f"contract is deviated and the experiment is {exp.status}; nothing left to govern")
        if not reason.strip():
            raise Invalid("an amendment needs a reason; it is written on the experiment")
        if action_now == "ship_treatment" and old.comparisons > 1:
            raise Invalid("this contract has several treatments; an amendment cannot say which one to ship now. "
                          "Only a look whose boundary crossing names the winner can ship a treatment.")
        bad = set(changes) - self.AMENDABLE
        if bad:
            raise Invalid(f"these fields cannot be amended: {sorted(bad)}")
        data = old.canonical()
        for k, v in changes.items():
            if isinstance(v, dict) and isinstance(data.get(k), dict):
                data[k] = {**data[k], **v}
            else:
                data[k] = v
        data["amendment"] = {"supersedes": h, "reason": reason}
        if action_now:
            data["amendment"]["action_now"] = action_now
        try:
            new = ct.with_regenerated_table(data)
        except ValidationError as exc:
            raise Invalid("amended contract does not validate", errors=_errors(exc)) from exc
        out = self.submit(new)
        out["sign_urls"] = {
            "owner": f"{self.settings.public_url}/sign/{new.hash}?role=owner",
            "second_signer": f"{self.settings.public_url}/sign/{new.hash}?role=second_signer",
        }
        return out

    def _activate_amendment(self, new: ct.Contract, h: str) -> None:
        old_h = new.amendment.supersedes  # type: ignore[union-attr]
        with self.store.transaction():
            _, old_row = self._load(old_h)
            if old_row["state"] in ("superseded", "executed", "void"):
                raise Conflict(f"contract {short_hash(old_h)} is already {old_row['state']}; "
                               "this amendment stays a draft")
            if old_row["state"] == "executing":
                # Its pre-signed action is being sent to Flagsmith; superseding now would race it.
                raise Conflict(f"contract {short_hash(old_h)} is carrying out its action right now; this "
                               "amendment stays a draft (sign again once that finishes)")
            started = old_row["state"] in ("active", "decided", "deviated", "starting")
            # Every outstanding proposal of the superseded contract dies with it, whatever its state.
            for p in self.store.proposals_for(old_h):
                if p["state"] in ("pending", "confirmed", "failed"):
                    self.store.update_proposal(p["id"], self.clock(), state="void")
            self.store.set_contract_state(old_h, "superseded")
            self.store.set_contract_state(h, "active" if started else "signed")
            gov = self.store.governed_row(new.experiment.environment, new.experiment.experiment)
            if gov:
                self.store.set_governed(environment=gov["environment"], experiment_id=gov["experiment_id"],
                                        contract_hash=h, feature_id=gov["feature_id"], flag=gov["flag"],
                                        option_ids=gov["option_ids"], rollout=gov["rollout"])
            pid = None
            if new.amendment.action_now:  # type: ignore[union-attr]
                pid = self.store.add_proposal(contract_hash=h, look_id=None, outcome="amendment",
                                              action=new.amendment.action_now, state="pending",  # type: ignore[union-attr]
                                              now=self.clock())
                if started:
                    self.store.set_contract_state(h, "decided")
        if started:
            line = (f"{MARK} Amendment {h} supersedes {short_hash(old_h)}: {new.amendment.reason} "  # type: ignore[union-attr]
                    f"(signed by {new.owner.name} and {new.second_signer.name}).")
            try:
                self._append_hypothesis(new, h, line)
            except FlagsmithError as exc:
                log.warning("could not record the amendment on the experiment: %s", exc)
        if pid:
            self._emit("amended", {"hash": h, "proposal": pid})

    # ------------------------------------------------------- deviations and audit

    # Flagsmith writes "Experiment '<name>' <verb> for environment <env>". The name is free text, so it
    # can itself contain "' completed for": the verb is the one followed only by the environment.
    _LIFECYCLE_LOG = re.compile(
        r"^Experiment '(?P<name>.*)' (?P<verb>created|updated|running|paused|completed|deleted) "
        r"for environment (?P<env>[^']*)$", re.S)
    _ROLLOUT_LOG = re.compile(r"^Experiment '(?P<name>.*)' rollout set ", re.S | re.I)
    _LIFECYCLE_KIND = {"created": None, "updated": "update_hypothesis", "running": "start", "paused": "pause",
                       "completed": "complete", "deleted": "delete"}
    _VERBS = [
        (re.compile(r"' running for"), "start"),
        (re.compile(r"' completed for"), "complete"),
        (re.compile(r"' paused for"), "pause"),
        (re.compile(r"' updated for"), "update_hypothesis"),
        (re.compile(r"' deleted for"), "delete"),
        (re.compile(r"' rollout set", re.I), "rollout"),
    ]
    _FLAG_TYPES = {"FEATURE", "FEATURE_STATE", "EF_VERSION", "SEGMENT"}

    def classify(self, entry: AuditEntry) -> tuple[str | None, dict | None]:
        """(kind, governed row) for an audit entry about a governed experiment or its flag."""
        rtype = (entry.related_object_type or "").upper().replace(" ", "_")
        if rtype == "EXPERIMENT":
            return self._experiment_kind(entry.log or ""), self._governed_for_id(entry.related_object_id)
        if rtype in self._FLAG_TYPES:
            for g in self.store.governed_rows():
                if entry.environment and str(entry.environment) != str(g["environment"]):
                    continue  # the same flag name in another environment or project is not governed
                named = re.search(rf"(?<![\w-]){re.escape(g['flag'])}(?![\w-])", entry.log or "")
                same_feature = rtype == "FEATURE" and str(entry.related_object_id) == str(g["feature_id"])
                if named or same_feature:
                    return "flag_change", g
        return None, None

    def _experiment_kind(self, log_text: str) -> str | None:
        m = self._LIFECYCLE_LOG.match(log_text)
        if m:
            return self._LIFECYCLE_KIND[m.group("verb")]
        if self._ROLLOUT_LOG.match(log_text):
            return "rollout"
        # Another shape (an environment name with a quote in it): the last verb in the line is Flagsmith's.
        found = [(m.start(), k) for rx, k in self._VERBS for m in rx.finditer(log_text)]
        return max(found)[1] if found else None

    def _governed_for_id(self, experiment_id: Any) -> dict | None:
        try:
            eid = int(experiment_id)
        except (TypeError, ValueError):
            return None
        for g in self.store.governed_rows():
            if g["experiment_id"] == eid:
                return g
        for env, e in self.store.governed_experiments():
            if e == eid:
                rows = [r for r in self.store.contracts_for(env, e) if r["state"] != "draft"]
                if rows:
                    return {"environment": env, "experiment_id": e, "contract_hash": rows[-1]["hash"],
                            "feature_id": None, "flag": "", "option_ids": [], "rollout": None}
        return None

    def _entry_time(self, entry: AuditEntry) -> float:
        raw = (entry.raw or {}).get("created_date")
        if isinstance(raw, str):
            try:
                return _dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        return self.clock()

    def _read_back_matches(self, ew: dict, attempts: int = 1) -> bool | None:
        """Does Flagsmith now show what this expected write was meant to do? None if it cannot be read.

        ``attempts`` > 1 reads again a few times, for entries judged just after a write returned."""
        ok = self._read_back_once(ew)
        for _ in range(attempts - 1):
            if ok:
                break
            self.sleep(self.settings.poll_seconds)
            ok = self._read_back_once(ew)
        return ok

    def _read_back_once(self, ew: dict) -> bool | None:
        if ew.get("landed") == 1:
            return True
        try:
            exp = self._fs.get_experiment(ew["environment"], ew["experiment_id"])
        except FlagsmithError:
            return None
        ex = ew["expect"]
        if ew["kind"] == "complete":
            return self._completed_as(exp, None if ex.get("ship") == CONTROL_KEY else ex.get("ship"))
        if ew["kind"] == "start":
            return exp.status in ("running", "paused", "completed")
        if ew["kind"] == "update_hypothesis":
            return ex.get("contains", "") in exp.hypothesis
        return False

    def _match_expected(self, kind: str, gov: dict, entry: AuditEntry, attempts: int = 1) -> dict | None:
        """The service's own expected write this author-less entry corresponds to, if any.

        A primary entry (start, complete, hypothesis update) claims one expected write of the same kind,
        once, inside its validity window, and only if Flagsmith shows the intended effect. Side-effect
        entries (the rollout and flag-state rows Flagsmith writes while starting or completing) are
        accepted inside the window of a landed start or complete without consuming it.
        """
        t = self._entry_time(entry)
        skew = 60.0
        for ew in self.store.expected_writes(gov["environment"], gov["experiment_id"]):
            in_window = ew["created_at"] - skew <= t <= ew["valid_until"]
            if not in_window:
                continue
            if ew["kind"] == kind and ew["matched_audit_id"] is None:
                if ew["landed"] == 0:
                    continue
                ok = self._read_back_matches(ew, attempts)
                if ok and self.store.claim_expected_write(ew["id"], entry.id):
                    self.store.set_expected_landed(ew["id"], True)
                    return ew
            elif kind in ew["also"] and ew["landed"] != 0 and t <= ew["created_at"] + SIDE_EFFECT_WINDOW:
                # Flagsmith writes these rows inside the same request as the start or complete.
                if ew["landed"] == 1 or self._read_back_matches(ew, attempts):
                    return ew
        return None

    def _verify_lifecycle(self, kind: str, gov: dict) -> bool | None:
        """Before freezing a contract on a webhook's word, check Flagsmith really changed."""
        try:
            exp = self._fs.get_experiment(gov["environment"], gov["experiment_id"])
        except FlagsmithError as exc:
            if exc.status == 404:
                return kind == "delete"
            return None
        if kind == "complete":
            return exp.status == "completed"
        if kind == "pause":
            return exp.status == "paused"
        if kind == "start":
            return exp.status in ("running", "paused", "completed")
        if kind == "delete":
            return False
        return True

    def _rollout_changed(self, gov: dict) -> bool | None:
        """Does Flagsmith's rollout differ from the one recorded at the start? Moves the record on if so."""
        snap = gov.get("rollout")
        try:
            exp = self._fs.get_experiment(gov["environment"], gov["experiment_id"])
        except FlagsmithError:
            return None
        now_rollout = _rollout_of(exp)
        if snap is None or now_rollout is None:
            return True  # nothing to compare against: treat the entry as the evidence
        if now_rollout == snap:
            return False
        # Recorded once here, so the next look's state check does not count it again.
        self.store.set_governed(environment=gov["environment"], experiment_id=gov["experiment_id"],
                                contract_hash=gov["contract_hash"], feature_id=gov["feature_id"], flag=gov["flag"],
                                option_ids=gov["option_ids"], rollout=now_rollout)
        return True

    def _record_deviation(self, c: ct.Contract, h: str, *, audit_id: Any, log: str, author: str | None,
                          kind: str, source: str, freeze: bool) -> int:
        ref = c.experiment
        who = author or "an API key that is not the contract service"
        note = (f"{MARK} Deviation ({source}): {kind.replace('_', ' ')} by {who}, outside contract {short_hash(h)}."
                if kind != "delete" else None)
        did = self.store.add_deviation(environment=ref.environment, experiment_id=ref.experiment, contract_hash=h,
                                       audit_id=audit_id, log=log, author=author, now=self.clock(), kind=kind,
                                       source=source, note=note)
        if freeze:
            with self.store.transaction():
                # A contract that is mid-execution is not frozen under it: the write in flight finds
                # Flagsmith's real state for itself.
                if self.store.cas_contract_state(h, ("starting", "active", "decided"), "deviated"):
                    for p in self.store.proposals_for(h):
                        if p["state"] in ("pending", "confirmed", "failed"):
                            self.store.update_proposal(p["id"], self.clock(), state="void")
        self.drain_notes()
        self._emit("deviation", {"hash": h, "log": log, "author": author, "kind": kind, "source": source})
        return did

    def drain_notes(self) -> int:
        """Write pending deviation notes onto their experiments; retried until they land."""
        written = 0
        for dv in self.store.pending_notes():
            if dv.get("experiment_id") is not None and self.write_in_flight(dv["environment"], dv["experiment_id"]):
                continue  # never PATCH over a lifecycle write in flight
            try:
                c, _ = self._load(dv["contract_hash"])
                self._append_hypothesis(c, dv["contract_hash"], dv["note"])
                self.store.set_note_state(dv["id"], "written")
                written += 1
            except FlagsmithError as exc:
                log.warning("deviation note still pending (%s)", exc)
                break
            except UlyssesError as exc:
                log.warning("deviation note cannot be written: %s", exc)
                self.store.set_note_state(dv["id"], "failed")
        return written

    def handle_audit(self, entry: AuditEntry, _attempts: int = 1) -> dict:
        """Classify one Flagsmith audit-log entry; record a deviation if it is not the service's own write."""
        self.store.keep_audit_entry(entry.id, {
            "id": entry.id, "log": entry.log, "author": entry.author,
            "related_object_type": entry.related_object_type, "related_object_id": entry.related_object_id,
            "environment": entry.environment, "created_date": (entry.raw or {}).get("created_date")}, self.clock())
        seen = self.store.audit_verdict(entry.id)
        if seen and seen["verdict"] != "unverified":
            self.drain_notes()
            return {"audit_id": entry.id, "verdict": seen["verdict"], "duplicate": True}
        kind, gov = self.classify(entry)
        if not kind or not gov:
            self.store.set_audit_verdict(entry.id, "ignored", None)
            return {"audit_id": entry.id, "verdict": "ignored"}
        if seen and self._overtaken(entry, gov):
            # A replay (or late retry) of an entry Flagsmith's state did not bear out, sent after the
            # service's own start or complete changed that state: today's state is evidence of the
            # service's write, not of this entry, so it stays unverified.
            return {"audit_id": entry.id, "verdict": "unverified", "duplicate": True,
                    "reason": "the service changed this experiment after the entry; its state no longer tests it"}
        if self.write_in_flight(gov["environment"], gov["experiment_id"]):
            # Judged once the service's own write returns; a read now would see the old state.
            with self._flight_guard:
                self._deferred.setdefault((gov["environment"], int(gov["experiment_id"])), {})[str(entry.id)] = entry
            return {"audit_id": entry.id, "verdict": "deferred",
                    "reason": "a service write to this experiment is in flight; judged when it returns"}
        h = gov["contract_hash"]
        c_row = self.store.contract_row(h)
        if kind == "flag_change" and c_row and c_row["state"] in ("executed", "void", "superseded"):
            # After the contract's own completion the flag is the team's again.
            ew = self._match_expected(kind, gov, entry, _attempts) if entry.author is None else None
            if ew is None:
                self.store.set_audit_verdict(entry.id, "ignored", None, detail={"reason": "after the contract ended"})
                return {"audit_id": entry.id, "verdict": "ignored"}
        if entry.author is None:
            ew = self._match_expected(kind, gov, entry, _attempts)
            if ew is not None:
                self.store.set_audit_verdict(entry.id, "authorised", None, expected_id=ew["id"],
                                             detail={"kind": kind, "contract": ew["contract_hash"]})
                return {"audit_id": entry.id, "verdict": "authorised", "expected_write": ew["id"],
                        "contract": ew["contract_hash"]}
        if kind == "rollout" and self._rollout_changed(gov) is not True:
            # Flagsmith's rollout still matches what the contract started (or cannot be read): the
            # entry alone is not evidence. A real change is still caught by the state check at each look.
            self.store.set_audit_verdict(entry.id, "unverified", None,
                                         detail={"kind": kind, "reason": "Flagsmith's rollout is unchanged"})
            return {"audit_id": entry.id, "verdict": "unverified", "contract": h}
        freeze = kind in ("complete", "start", "delete", "pause")
        if freeze:
            real = self._verify_lifecycle(kind, gov)
            if not real:
                # Flagsmith does not show the change (or cannot be read): record, change nothing.
                self.store.set_audit_verdict(entry.id, "unverified", None,
                                             detail={"kind": kind, "reason": "Flagsmith does not show this change"})
                return {"audit_id": entry.id, "verdict": "unverified", "contract": h}
        c, _ = self._load(h)
        did = self._record_deviation(c, h, audit_id=entry.id, log=entry.log, author=entry.author, kind=kind,
                                     source="audit webhook", freeze=freeze and kind != "pause")
        self.store.set_audit_verdict(entry.id, "deviation", None, detail={"kind": kind, "contract": h})
        return {"audit_id": entry.id, "verdict": "deviation", "deviation_id": did, "contract": h}

    def _overtaken(self, entry: AuditEntry, gov: dict) -> bool:
        """Did a lifecycle write of the service's own land after this entry was written?"""
        t = self._entry_time(entry)
        return any(ew["kind"] in ("start", "complete") and ew["landed"] == 1 and ew["created_at"] > t
                   for ew in self.store.expected_writes(gov["environment"], gov["experiment_id"]))

    def _audit_entries(self, project: int) -> list[AuditEntry]:
        """Flagsmith's audit log for the project, merged with the entries its webhook delivered.

        Open-source self-hosted Flagsmith returns no rows from the audit-log API
        (the free plan's audit_log_visibility_days is 0), but its organisation
        webhook still sends every entry, and the service keeps a copy of each.
        """
        merged: dict[str, AuditEntry] = {}
        try:
            for e in self._fs.audit_log(project):
                merged[str(e.id)] = e
        except FlagsmithError as exc:
            log.warning("audit-log API unavailable (%s); using webhook copies only", exc)
        for d in self.store.kept_audit_entries():
            merged.setdefault(str(d["id"]), AuditEntry(id=d["id"], log=d["log"], author=d["author"],
                                                      related_object_type=d["related_object_type"],
                                                      related_object_id=d["related_object_id"],
                                                      environment=d.get("environment"),
                                                      raw={"created_date": d.get("created_date")}))
        return sorted(merged.values(), key=lambda e: _sort_key(e.id))

    def sync_audit(self, project: int) -> list[dict]:
        out = [self.handle_audit(e) for e in self._audit_entries(project)]
        self.drain_notes()
        return out

    def audit_replay(self, project: int) -> dict:
        """Which lifecycle and flag changes on governed experiments a signed contract covers.

        Each entry is first attributed exactly once by ``handle_audit`` (an author-less entry must claim a
        specific expected write and Flagsmith must show its effect). The replay then reports from those
        stored verdicts and re-verifies the signatures behind each matched write.
        """
        self.sync_audit(project)
        verdicts = self.store.audit_verdicts()
        rows = []
        for e in self._audit_entries(project):
            kind, gov = self.classify(e)
            if kind not in ("start", "complete", "pause", "delete", "rollout", "flag_change"):
                continue
            v = verdicts.get(str(e.id)) or {}
            row: dict[str, Any] = {"audit_id": e.id, "log": e.log, "author": e.author, "kind": kind,
                                   "experiment": gov["experiment_id"] if gov else e.related_object_id}
            if not gov or v.get("verdict") == "ignored":
                row["verdict"] = "not governed"
                rows.append(row)
                continue
            if v.get("verdict") == "authorised" and v.get("expected_id"):
                ew = self.store.expected_write(v["expected_id"])
                h = ew["contract_hash"]
                c, _ = self._load(h)
                row["contract"] = h
                row["signed_by"] = c.owner.name
                if ew["kind"] != kind:
                    row["verdict"] = "side effect"
                    row["of"] = ew["kind"]
                    rows.append(row)
                    continue
                ok = self._signature_valid(c, h, "owner") and (kind != "complete" or self._fully_signed(c, h))
                if kind == "complete":
                    p = self.store.proposal(ew["proposal_id"]) if ew["proposal_id"] else None
                    conf_ok = bool(p) and self._confirm_valid(c, p, p["confirm_signature"])
                    row["confirmed_by"] = p["confirm_signer"] if p else None
                    row["outcome"] = p["outcome"] if p else None
                    row["shipped"] = ew["expect"].get("ship")
                    ok = ok and conf_ok
                row["verdict"] = "signed" if ok else "signature invalid"
            else:
                row["verdict"] = v.get("verdict") or "unattributed"
            rows.append(row)
        counted = [r for r in rows if r["verdict"] not in ("not governed", "side effect")]
        return {
            "project": project,
            "rows": rows,
            "governed_lifecycle_changes": len(counted),
            "mapped_to_signed_contract": sum(1 for r in counted if r["verdict"] == "signed"),
            "deviations": sum(1 for r in counted if r["verdict"] == "deviation"),
            "unverified": sum(1 for r in counted if r["verdict"] == "unverified"),
            "invalid": sum(1 for r in counted if r["verdict"] == "signature invalid"),
            "side_effects": sum(1 for r in rows if r["verdict"] == "side effect"),
            "not_governed": sum(1 for r in rows if r["verdict"] == "not governed"),
        }

    # ------------------------------------------------------------------ agent reads

    def read_experiment(self, environment: str, experiment_id: int) -> dict:
        """Flagsmith's own view of an experiment for the agent: reads only, no key, no write paths."""
        exp = self._fs.get_experiment(environment, experiment_id)
        try:
            snap = self._fs.get_results(environment, experiment_id)
        except FlagsmithError:
            snap = None
        try:
            rate = self._fs.exposure_rate_per_hour(environment, experiment_id)
        except FlagsmithError:
            rate = None
        return {
            "source": "Flagsmith REST API, read by the contract service",
            "experiment": {"id": exp.id, "name": exp.name, "status": exp.status, "hypothesis": exp.hypothesis,
                           "flag": exp.flag, "variations": [v.key for v in exp.variations],
                           "metrics": [{"name": m.name, "direction": m.direction,
                                        "expected_direction": m.expected_direction} for m in exp.metrics],
                           "rollout": _rollout_of(exp)},
            "results": None if snap is None else {
                "as_of": snap.as_of, "identities": snap.identities, "srm_p_value": snap.srm_p_value,
                "metrics": {k: {"chance_to_win": m.chance_to_win, "lift": m.lift, "direction": m.direction}
                            for k, m in snap.metrics.items() if "@" not in k},
            },
            "exposure_rate_per_arm_per_hour": rate,
        }

    # ------------------------------------------------------------------ views

    def experiment_view(self, environment: str, experiment_id: int) -> dict:
        rows = self.store.contracts_for(environment, experiment_id)
        return {
            "contracts": [{"hash": r["hash"], "state": r["state"], "supersedes": r["supersedes"]} for r in rows],
            "looks": [{"id": lk["id"], "seq": lk["seq"], "counted": bool(lk["counted"]), "outcome": lk["outcome"],
                       "look_number": lk["decision"].get("look_number"),
                       "identities_per_arm": lk["identities_per_arm"], "reason": lk["decision"]["reason"],
                       "guardrails": lk["decision"].get("guardrails", [])}
                      for lk in self.store.looks(environment, experiment_id)],
            "deviations": self.store.deviations(environment, experiment_id),
        }

    def look_view(self, look_id: str) -> dict:
        lk = self.store.look(look_id)
        if not lk:
            raise NotFound(f"no look {look_id}")
        c, row = self._load(lk["contract_hash"])
        props = [p for p in self.store.proposals_for(lk["contract_hash"]) if p["look_id"] == look_id]
        history = [
            {"seq": x["seq"], "counted": bool(x["counted"]), "outcome": x["outcome"],
             "look_number": x["decision"].get("look_number"),
             "identities_per_arm": x["identities_per_arm"], "chance_to_win": x["decision"]["chance_to_win"],
             "boundary_chance": x["decision"]["boundary_chance"], "z": x["decision"]["z"],
             "boundary_z": x["decision"]["boundary_z"], "guardrails": x["decision"].get("guardrails", [])}
            for x in self.store.looks(lk["environment"], lk["experiment_id"])
        ]
        return {
            "look": {k: lk[k] for k in ("id", "seq", "counted", "outcome", "identities_per_arm", "refreshed")},
            "decision": lk["decision"],
            "flagsmith": lk["snapshot"],
            "contract": {"hash": lk["contract_hash"], "state": row["state"], "summary": summarise(c),
                         "owner": c.owner.name, "second_signer": c.second_signer.name,
                         "planned": [p.model_dump() for p in c.boundary.planned]},
            "proposal": self._proposal_view(props[-1]) if props else None,
            "history": history,
            "confirm_message": None if not props else self.confirm_message(props[-1]).decode(),
        }

    def proposal_card(self, pid: str) -> dict:
        p = self.store.proposal(pid)
        if not p:
            raise NotFound(f"no proposal {pid}")
        c, row = self._load(p["contract_hash"])
        return {
            "proposal": self._proposal_view(p),
            # A proposal is either fired by a look (see its look card) or authorised by an amendment.
            "origin": "amendment" if p["outcome"] == "amendment" else "look",
            "contract": {"hash": p["contract_hash"], "state": row["state"], "summary": summarise(c),
                         "owner": c.owner.name, "amendment": c.amendment.model_dump() if c.amendment else None},
            "confirm_message": self.confirm_message(p).decode(),
        }

    def _emit(self, event: str, payload: dict) -> None:
        if self._notify:
            try:
                self._notify(event, payload)
            except Exception as exc:  # noqa: BLE001 - notifications never break the gate
                log.warning("notification failed: %s", exc)


def _sort_key(v: Any) -> tuple:
    try:
        return (0, int(v))
    except (TypeError, ValueError):
        return (1, str(v))


def _signed_split(exp: ExperimentInfo, arms: list[str]) -> ct.SampleRatio:
    """The split to sign: Flagsmith's rollout weights when they cover exactly these arms, else even."""
    live = flagsmith_split(exp)
    if live is not None and {k for k, v in live.items() if v > 0} == set(arms):
        return ct.SampleRatio(split={k: round(live[k], 4) for k in arms})
    return ct.SampleRatio(split=ct.equal_split(arms))


def _errors(exc: ValidationError) -> list[dict]:
    return [{"loc": ".".join(str(x) for x in e["loc"]), "msg": e["msg"]} for e in exc.errors()]


def summarise(c: ct.Contract) -> dict:
    act = c.actions
    return {
        "experiment": c.experiment.experiment,
        "environment": c.experiment.environment,
        "flag": c.experiment.flag,
        "arms": c.experiment.arms,
        "treatments": c.experiment.treatments,
        "comparisons": c.comparisons,
        "comparison_alpha": c.comparison_alpha,
        "multiplicity": (None if c.comparisons == 1 else
                         f"Bonferroni over {c.comparisons} treatment-vs-control comparisons: each has its own "
                         f"boundary at alpha {c.comparison_alpha:.4g}, so the chance of shipping any treatment "
                         f"that does nothing stays at most {c.boundary.alpha / 2:g}"),
        "sample_ratio": None if c.sample_ratio is None else {
            "split": c.sample_ratio.split, "p_threshold": c.sample_ratio.p_threshold,
            "min_total": c.sample_ratio.min_total,
            "text": ("A win is held while the chi-square test of Flagsmith's exposure counts against the split "
                     + ", ".join(f"{k} {v:g}%" for k, v in c.sample_ratio.split.items())
                     + f" gives p below {c.sample_ratio.p_threshold:g} (checked from "
                     f"{c.sample_ratio.min_total} identities)")},
        "hypothesis": c.hypothesis,
        "primary_metric": f"{c.primary_metric.name} should {c.primary_metric.direction}",
        "guardrails": [f"{g.name} {g.direction.replace('_', ' ')} (stop on a {g.alpha:.0%} one-sided "
                       "Pocock-type boundary at each look)" for g in c.guardrails],
        "require_guardrail_results_to_ship": c.require_guardrail_results_to_ship,
        "horizon": f"{c.horizon.identities_per_arm} identities per arm, up to {c.horizon.looks} looks",
        "boundary": f"Lan-DeMets O'Brien-Fleming, alpha {c.boundary.alpha} two-sided",
        "first_reachable_look": next((p.model_dump() for p in c.boundary.planned if p.z is not None), None),
        "final_look": c.boundary.planned[-1].model_dump(),
        "on_win": act.win, "on_loss": act.loss, "on_horizon": act.horizon,
        "on_guardrail_breach": act.guardrail_breach,
        "owner": c.owner.name, "second_signer": c.second_signer.name,
        "start_within_days": c.validity.start_within_days,
        "expires_after_days": c.validity.start_within_days,  # older name, kept for the pages
        "design": c.design.model_dump() if c.design else None,
        "amendment": c.amendment.model_dump() if c.amendment else None,
    }
