"""In-memory Flagsmith for tests. Records every write so tests can assert on them.

It mirrors the behaviour verified in Flagsmith's source and the shared lab:
status transitions (created -> running -> paused/completed), the five-minute
refresh floor, the reserved ``control`` key, ``complete`` taking a rollout
decision, API-key writes carrying no author in the audit log, and the extra
audit rows Flagsmith writes when it starts or completes an experiment (the
rollout row, and feature-state rows on the experiment's flag).
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import replace

from .gateway import (
    CONTROL_KEY,
    AuditEntry,
    ExperimentInfo,
    FlagsmithError,
    MetricInfo,
    MetricResult,
    ResultsSnapshot,
    Throttled,
    Variation,
)

_TRANSITIONS = {
    "created": {"running"},
    "running": {"paused", "completed"},
    "paused": {"running", "completed"},
    "completed": set(),
}


class FakeFlagsmith:
    REFRESH_FLOOR_SECONDS = 300
    SERVICE_AUTHOR = None  # API-key writes carry no user author, as in Flagsmith

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.experiments: dict[int, ExperimentInfo] = {}
        self.results: dict[int, ResultsSnapshot] = {}
        self.writes: list[tuple[str, int, object]] = []
        self.audit: list[AuditEntry] = []
        self._last_refresh: dict[int, float] = {}
        self.served: dict[str, str] = {}
        self.rate: dict[int, float] = {}
        self.refresh_calls = 0
        self.fail_complete_after_applying = False  # simulate a response lost after Flagsmith applied it
        self.fail_start_after_applying = False
        self.fail_hypothesis = 0  # number of update_hypothesis calls that will fail before applying
        self.down = False  # every call fails as if Flagsmith were unreachable

    # ---- setup helpers ---------------------------------------------------

    def add_experiment(
        self,
        experiment_id: int = 1,
        *,
        flag: str = "checkout_button",
        treatment: str = "big_cta",
        primary: str = "purchase",
        guardrails=(("refunds", "not_increase", "down"),),
        project: int = 1,
        environment: str = "env-key",
        more_treatments: tuple[str, ...] = (),
        weights: dict[str, float] | None = None,
    ) -> ExperimentInfo:
        """``more_treatments`` adds further treatment arms; ``weights`` are the rollout percentages per
        treatment key (control takes the rest). By default every arm gets an equal share."""
        metrics = [MetricInfo(id=1, name=primary, direction="up", expected_direction="increase")]
        for i, (name, exp_dir, polarity) in enumerate(guardrails, start=2):
            metrics.append(MetricInfo(id=i, name=name, direction=polarity, expected_direction=exp_dir))
        option = 500 + experiment_id
        keys = [treatment, *more_treatments]
        options = {k: option + 100 * i for i, k in enumerate(keys)}
        if weights is None:
            weights = {k: (50.0 if len(keys) == 1 else round(100 / (len(keys) + 1), 4)) for k in keys}
        info = ExperimentInfo(
            id=experiment_id,
            name=f"exp-{experiment_id}",
            status="created",
            hypothesis="",
            environment=environment,
            flag=flag,
            feature_id=100 + experiment_id,
            variations=[Variation(CONTROL_KEY, None), *(Variation(k, options[k]) for k in keys)],
            metrics=metrics,
            project=project,
            raw={"experiment_rollout": {
                "enabled": False, "rollout_percentage": 100.0,
                "feature_state_value": {"type": "string", "value": "classic"},
                "multivariate_feature_state_values": [
                    {"multivariate_feature_option": options[k], "percentage_allocation": weights[k]}
                    for k in keys]}},
        )
        self.experiments[experiment_id] = info
        return info

    def set_results(
        self,
        experiment_id: int,
        control_n: int,
        treatment_n: int,
        chance: float | None,
        guardrails: dict[str, float | None] | None = None,
        as_of: str | None = None,
    ) -> None:
        """``chance`` and guardrail values are Flagsmith's reported chance_to_win.

        Guardrails not given default to a neutral 0.5 so a test about the primary metric is not held
        by a guardrail with no result; pass None explicitly for "collecting data".
        """
        info = self.experiments[experiment_id]
        treat = info.variations[1].key
        metrics: dict[str, MetricResult] = {}
        for m in info.metrics:
            if m.is_guardrail:
                ctw = (guardrails or {}).get(m.name, 0.5)
            else:
                ctw = chance
            metrics[m.name] = MetricResult(m.name, m.direction, ctw)
        self.results[experiment_id] = ResultsSnapshot(
            identities={CONTROL_KEY: control_n, treat: treatment_n},
            metrics=metrics,
            as_of=as_of,
        )

    def set_arm_results(
        self,
        experiment_id: int,
        identities: dict[str, int],
        chances: dict[str, float | None],
        guardrails: dict[str, dict[str, float | None]] | None = None,
        as_of: str | None = None,
        lifts: dict[str, float | None] | None = None,
    ) -> None:
        """Several treatments: Flagsmith's chance_to_win per treatment key, keyed ``metric@arm`` as
        ``rest.parse_results`` keys each treatment's inference. Like ``parse_results``, the unsuffixed
        ``metric`` key also carries the first treatment's inference. ``lifts`` sets the primary lift per arm."""
        info = self.experiments[experiment_id]
        metrics: dict[str, MetricResult] = {}
        for arm, chance in chances.items():
            for m in info.metrics:
                ctw = ((guardrails or {}).get(arm) or {}).get(m.name, 0.5) if m.is_guardrail else chance
                lift = None if m.is_guardrail else (lifts or {}).get(arm)
                metrics[f"{m.name}@{arm}"] = MetricResult(m.name, m.direction, ctw, lift)
                metrics.setdefault(m.name, MetricResult(m.name, m.direction, ctw, lift))
        self.results[experiment_id] = ResultsSnapshot(identities=dict(identities), metrics=metrics, as_of=as_of)

    # ---- gateway ---------------------------------------------------------

    def _check_up(self) -> None:
        if self.down:
            raise FlagsmithError("cannot reach Flagsmith: connection refused")

    def _exp(self, environment: str, experiment_id: int) -> ExperimentInfo:
        self._check_up()
        e = self.experiments.get(experiment_id)
        if e is None or e.environment != environment:
            raise FlagsmithError(f"experiment {experiment_id} not found", status=404)
        return e

    def _now_iso(self) -> str:
        return _dt.datetime.fromtimestamp(self.clock(), _dt.timezone.utc).isoformat()

    def _audit(self, log: str, *, author: str | None, rtype: str, rid, environment: str) -> AuditEntry:
        entry = AuditEntry(id=len(self.audit) + 1, log=log, author=author, related_object_type=rtype,
                           related_object_id=rid, environment=environment, raw={"created_date": self._now_iso()})
        self.audit.append(entry)
        return entry

    def _log(self, kind: str, e: ExperimentInfo, payload: object = None, author: str | None = None,
             record: bool = True) -> None:
        if record:
            self.writes.append((kind, e.id, payload))
        verb = {"start": "running", "complete": "completed", "update_hypothesis": "updated"}.get(kind, kind)
        self._audit(f"Experiment '{e.name}' {verb} for environment Test", author=author, rtype="EXPERIMENT",
                    rid=e.id, environment=e.environment)

    def _flag_rows(self, e: ExperimentInfo, author: str | None, what: str) -> None:
        """The feature-state rows Flagsmith writes on the experiment's flag."""
        self._audit(f"Flag state / Remote config value {what} for feature '{e.flag}' and segment "
                    f"'experiment-{e.id}-rollout'", author=author, rtype="FEATURE_STATE", rid=900 + e.id,
                    environment=e.environment)

    def get_experiment(self, environment: str, experiment_id: int) -> ExperimentInfo:
        return self._exp(environment, experiment_id)

    def update_hypothesis(self, environment: str, experiment_id: int, text: str) -> None:
        e = self._exp(environment, experiment_id)
        if self.fail_hypothesis:
            self.fail_hypothesis -= 1
            raise FlagsmithError("cannot reach Flagsmith: connection refused")
        e = replace(e, hypothesis=text)
        self.experiments[experiment_id] = e
        self._log("update_hypothesis", e, text)

    def _transition(self, e: ExperimentInfo, target: str) -> ExperimentInfo:
        if target not in _TRANSITIONS[e.status]:
            raise FlagsmithError(f"Cannot transition from '{e.status}' to '{target}'.", status=400)
        e = replace(e, status=target)
        self.experiments[e.id] = e
        return e

    def start(self, environment: str, experiment_id: int) -> None:
        e = self._transition(self._exp(environment, experiment_id), "running")
        self._audit(f"Experiment '{e.name}' rollout set to 100.0% of all identities", author=None,
                    rtype="EXPERIMENT", rid=e.id, environment=e.environment)
        self._log("start", e)
        self._flag_rows(e, None, "updated")
        if self.fail_start_after_applying:
            self.fail_start_after_applying = False
            raise FlagsmithError("cannot reach Flagsmith: timed out")

    def refresh_results(self, environment: str, experiment_id: int) -> None:
        e = self._exp(environment, experiment_id)
        if e.status == "created":
            raise FlagsmithError("Cannot refresh results before the experiment starts.", status=400)
        now = self.clock()
        last = self._last_refresh.get(experiment_id)
        if last is not None and now - last < self.REFRESH_FLOOR_SECONDS:
            raise Throttled("A refresh was requested recently. Try again later.",
                            retry_after_seconds=self.REFRESH_FLOOR_SECONDS - (now - last))
        self._last_refresh[experiment_id] = now
        self.refresh_calls += 1

    def get_results(self, environment: str, experiment_id: int) -> ResultsSnapshot | None:
        self._exp(environment, experiment_id)
        return self.results.get(experiment_id)

    def _apply_complete(self, e: ExperimentInfo, ship: str | None) -> ExperimentInfo:
        e = self._transition(e, "completed")
        v = e.variation(ship) if ship and ship != CONTROL_KEY else None
        e = replace(e, raw={**e.raw, "rollout_decision": "variant" if v else "control",
                            "rollout_variant": v.option_id if v else None})
        self.experiments[e.id] = e
        self.served[e.flag] = v.key if v else CONTROL_KEY
        return e

    def complete(self, environment: str, experiment_id: int, ship: str | None) -> dict:
        e = self._exp(environment, experiment_id)
        if ship is not None and ship != CONTROL_KEY and e.variation(ship) is None:
            raise FlagsmithError(f"variation {ship!r} is not on flag {e.flag!r}")
        e = self._apply_complete(e, ship)
        serving = self.served[e.flag]
        self._flag_rows(e, None, "deleted")
        self._log("complete", e, serving)
        if self.fail_complete_after_applying:
            self.fail_complete_after_applying = False
            raise FlagsmithError("cannot reach Flagsmith: timed out")
        return {"request": {"rollout_decision": "control" if serving == CONTROL_KEY else "variant"},
                "response_status": "completed"}

    def exposure_rate_per_hour(self, environment: str, experiment_id: int) -> float | None:
        self._check_up()
        return self.rate.get(experiment_id)

    def audit_log(self, project: int) -> list[AuditEntry]:
        # Open-source self-hosted Flagsmith returns no rows here (visibility 0 days); tests that want
        # the API path can set ``audit_api_visible``.
        return list(self.audit) if getattr(self, "audit_api_visible", True) else []

    # ---- test helpers: writes that do not come from the contract service ----------

    def dashboard_complete(self, experiment_id: int, user: str | None = "admin@example.com",
                           ship: str | None = None) -> None:
        """A person in the dashboard (author set) or another API key (author None) completes."""
        e = self._apply_complete(self.experiments[experiment_id], ship)
        self._flag_rows(e, user, "deleted")
        self._log("complete", e, ship, author=user, record=False)

    def foreign_rollout(self, experiment_id: int, weight: float, author: str | None = None) -> None:
        """Re-weight a running experiment with PATCH .../rollout/ (Flagsmith allows it while running)."""
        e = self.experiments[experiment_id]
        r = dict(e.raw.get("experiment_rollout") or {})
        r["multivariate_feature_state_values"] = [
            {"multivariate_feature_option": e.variations[1].option_id, "percentage_allocation": weight}]
        self.experiments[experiment_id] = replace(e, raw={**e.raw, "experiment_rollout": r})
        self._audit(f"Experiment '{e.name}' rollout set to 100.0% of all identities", author=author,
                    rtype="EXPERIMENT", rid=e.id, environment=e.environment)

    def foreign_flag_edit(self, experiment_id: int, author: str | None = None) -> None:
        """Change what the flag serves (feature state or variation value) without any lifecycle call."""
        e = self.experiments[experiment_id]
        self._audit(f"Flag state updated for feature: {e.flag}", author=author, rtype="FEATURE_STATE",
                    rid=800 + e.id, environment=e.environment)
