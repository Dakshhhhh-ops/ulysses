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
import json
from dataclasses import replace

from . import health as fh
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
        # Feature Health, as api/features/feature_health/ models it.
        self.environments: dict[str, fh.EnvironmentInfo] = {}
        self.features: dict[tuple[int, str], int] = {}  # (project, flag name) -> feature id
        self.health_providers_by_project: dict[int, list[fh.HealthProvider]] = {}
        self._webhooks: dict[str, tuple[int, str]] = {}  # webhook url -> (project, provider name)
        self.health_history: list[dict] = []  # every stored event, oldest first
        self.webhook_down = False  # the feature-health webhook endpoint is unreachable
        self.webhook_posts: list[dict] = []  # every body that reached the webhook endpoint

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
        if environment not in self.environments:
            self.add_environment(environment, name="Test" if environment == "env-key" else environment,
                                 project=project)
        self.features[(project, flag)] = info.feature_id
        return info

    def add_environment(self, key: str, *, name: str, project: int = 1) -> fh.EnvironmentInfo:
        info = fh.EnvironmentInfo(id=len(self.environments) + 1, name=name, project=project)
        self.environments[key] = info
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

    # ---- Feature Health ------------------------------------------------------

    def environment_info(self, environment: str) -> fh.EnvironmentInfo:
        self._check_up()
        e = self.environments.get(environment)
        if e is None:
            raise FlagsmithError("Not found.", status=404)
        return e

    def health_providers(self, project: int) -> list[fh.HealthProvider]:
        self._check_up()
        return list(self.health_providers_by_project.get(project, []))

    def create_health_provider(self, project: int, name: str = fh.WEBHOOK_PROVIDER) -> fh.HealthProvider:
        self._check_up()
        if name not in ("Webhook", "Grafana"):
            raise FlagsmithError(f'"{name}" is not a valid choice.', status=400)
        rows = self.health_providers_by_project.setdefault(project, [])
        if any(r.name == name for r in rows):
            # unique_together (name, project) with no serializer check: the database refuses it.
            raise FlagsmithError("duplicate key value violates unique constraint", status=500)
        url = f"http://flagsmith.test/api/v1/feature-health/p{project}{name.lower()}/signedpart"
        prov = fh.HealthProvider(name=name, project=project, webhook_url=url)
        rows.append(prov)
        self._webhooks[url] = (project, name)
        return prov

    def health_events(self, project: int) -> list[fh.HealthEvent]:
        """The latest event per (provider, environment, external id, feature), as get_latest_by_project."""
        self._check_up()
        latest: dict[tuple, dict] = {}
        for ev in self.health_history:
            if ev["_project"] == project:
                latest[(ev["provider_name"], ev["environment"], ev["_external_id"], ev["feature"])] = ev
        return [fh.parse_event({k: v for k, v in ev.items() if not k.startswith("_")}) for ev in latest.values()]

    def _store_health(self, project: int, feature_id: int, environment_id: int | None, provider: str,
                      kind: str, reason: dict | None, external_id: str | None = None) -> dict:
        ev = {"id": len(self.health_history) + 1, "feature": feature_id, "environment": environment_id,
              "provider_name": provider, "type": kind, "reason": reason, "_project": project,
              "_external_id": external_id}
        self.health_history.append(ev)
        return ev

    def receive_webhook(self, url: str, body: str | bytes | dict) -> int:
        """POST to a provider's webhook_url, as feature_health_webhook handles it. Returns the HTTP status."""
        if self.webhook_down or self.down:
            raise FlagsmithError("cannot reach Flagsmith: connection refused")
        target = self._webhooks.get(url)
        if target is None:
            return 404
        project, provider = target
        try:
            data = json.loads(body) if isinstance(body, (str, bytes)) else body
            feature, status = data["feature"], data["status"]
            if not isinstance(feature, str) or status not in ("healthy", "unhealthy"):
                return 400
            reason = data.get("reason")
            if reason is not None:
                for b in reason["text_blocks"]:
                    if not isinstance(b["text"], str):
                        return 400
                for b in reason["url_blocks"]:
                    if not isinstance(b["url"], str):
                        return 400
        except (ValueError, KeyError, TypeError):
            return 400
        self.webhook_posts.append(data)
        fid = self.features.get((project, feature))
        if fid is None:
            return 400  # no event created for an unknown feature, so the view answers 400
        env = next((e for e in self.environments.values()
                    if e.project == project and e.name == data.get("environment")), None)
        self._store_health(project, fid, env.id if env else None, provider,
                           fh.UNHEALTHY if status == "unhealthy" else fh.HEALTHY, reason)
        return 200

    def external_health(self, flag: str, status: str, *, provider: str = "Grafana", environment: str | None = None,
                        project: int = 1, title: str = "Alert firing", external_id: str | None = "alert-1") -> None:
        """Another provider (Grafana by default) reports on a flag. ``environment`` is an environment key."""
        env_id = self.environments[environment].id if environment else None
        self._store_health(project, self.features[(project, flag)], env_id, provider,
                           fh.UNHEALTHY if status == "unhealthy" else fh.HEALTHY,
                           {"text_blocks": [{"title": title, "text": f"{provider} says {status}"}], "url_blocks": []},
                           external_id=external_id)

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
