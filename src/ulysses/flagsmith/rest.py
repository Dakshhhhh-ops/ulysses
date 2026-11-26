"""Flagsmith over its own REST API (the endpoints the dashboard and MCP server use).

Paths, verified against the Flagsmith source (``api/environments/urls.py`` and
``api/experimentation/views.py``):

    GET    /api/v1/environments/<env>/experiments/<id>/            get_experiment (MCP)
    PATCH  /api/v1/environments/<env>/experiments/<id>/            update_experiment (MCP: name, hypothesis)
    POST   /api/v1/environments/<env>/experiments/<id>/start/
    POST   /api/v1/environments/<env>/experiments/<id>/results/refresh/   202, or 429 inside 5 minutes
    GET    /api/v1/environments/<env>/experiments/<id>/results/    get_experiment_results (MCP)
    GET    /api/v1/environments/<env>/experiments/<id>/exposures/  get_experiment_exposures (MCP)
    POST   /api/v1/environments/<env>/experiments/<id>/complete/   {"rollout_decision": "variant"|"control", "rollout_variant": <option id>}
    GET    /api/v1/audit/?project=<id>

Start, refresh and complete are deliberately not exposed by Flagsmith's MCP
server; only the contract service, which holds the write key, calls them.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import httpx

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


class RestFlagsmith:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        auth_scheme: str = "Api-Key",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/api/v1"):
            self.base_url = self.base_url[: -len("/api/v1")]
        self._client = httpx.Client(
            base_url=self.base_url + "/api/v1",
            headers={"Authorization": f"{auth_scheme} {api_key}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    # ---- plumbing ---------------------------------------------------------

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self._client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise FlagsmithError(f"cannot reach Flagsmith at {self.base_url}: {exc}") from exc
        if r.status_code == 429:
            retry = r.headers.get("Retry-After")
            raise Throttled(
                _detail(r) or "Flagsmith throttled the request",
                retry_after_seconds=float(retry) if retry else None,
            )
        if r.status_code >= 400:
            raise FlagsmithError(
                f"{method} {path} -> {r.status_code}: {_detail(r)}", status=r.status_code, body=_body(r)
            )
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    @staticmethod
    def _exp_path(environment: str, experiment_id: int, suffix: str = "") -> str:
        return f"/environments/{environment}/experiments/{int(experiment_id)}/{suffix}"

    # ---- reads -------------------------------------------------------------

    def get_experiment(self, environment: str, experiment_id: int) -> ExperimentInfo:
        data = self._request("GET", self._exp_path(environment, experiment_id))
        return parse_experiment(data, environment)

    def get_results(self, environment: str, experiment_id: int) -> ResultsSnapshot | None:
        exp = self.get_experiment(environment, experiment_id)
        data = self._request("GET", self._exp_path(environment, experiment_id, "results/"))
        return parse_results(data, exp)

    def exposure_rate_per_hour(self, environment: str, experiment_id: int) -> float | None:
        data = self._request("GET", self._exp_path(environment, experiment_id, "exposures/"))
        return parse_exposure_rate(data)

    def audit_log(self, project: int) -> list[AuditEntry]:
        out: list[AuditEntry] = []
        url: str | None = f"/audit/?project={int(project)}&page_size=100"
        pages = 0
        while url and pages < 20:
            data = self._request("GET", url)
            pages += 1
            rows = data.get("results", data) if isinstance(data, dict) else data
            for row in rows or []:
                out.append(parse_audit(row))
            nxt = data.get("next") if isinstance(data, dict) else None
            url = nxt.split("/api/v1", 1)[1] if nxt and "/api/v1" in nxt else None
        return out

    # ---- writes (only the contract service calls these) --------------------

    def update_hypothesis(self, environment: str, experiment_id: int, text: str) -> None:
        self._request("PATCH", self._exp_path(environment, experiment_id), json={"hypothesis": text})

    def start(self, environment: str, experiment_id: int) -> None:
        self._request("POST", self._exp_path(environment, experiment_id, "start/"))

    def refresh_results(self, environment: str, experiment_id: int) -> None:
        self._request("POST", self._exp_path(environment, experiment_id, "results/refresh/"))

    def complete(self, environment: str, experiment_id: int, ship: str | None) -> dict:
        body: dict[str, Any] = {"rollout_decision": "control"}
        if ship is not None and ship != CONTROL_KEY:
            exp = self.get_experiment(environment, experiment_id)
            v = exp.variation(ship)
            if v is None or v.option_id is None:
                raise FlagsmithError(f"variation {ship!r} is not on flag {exp.flag!r}")
            body = {"rollout_decision": "variant", "rollout_variant": v.option_id}
        data = self._request("POST", self._exp_path(environment, experiment_id, "complete/"), json=body)
        return {"request": body, "response_status": (data or {}).get("status")}


# ---- payload parsing (pure, unit-tested) --------------------------------------


def _detail(r: httpx.Response) -> str:
    try:
        b = r.json()
    except Exception:  # noqa: BLE001
        return r.text[:300]
    if isinstance(b, dict) and "detail" in b:
        return str(b["detail"])
    return str(b)[:300]


def _body(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return r.text[:1000]


def parse_experiment(data: dict, environment: str) -> ExperimentInfo:
    feature = data.get("feature") or {}
    if not isinstance(feature, dict):
        feature = {"id": feature}
    variations = [Variation(key=CONTROL_KEY, option_id=None)]
    for opt in feature.get("multivariate_options") or []:
        key = opt.get("key")
        if key and key != CONTROL_KEY:
            variations.append(Variation(key=key, option_id=opt.get("id")))
    metrics = [
        MetricInfo(
            id=m.get("metric"),
            name=m.get("metric_name") or str(m.get("metric")),
            direction=m.get("direction") or "up",
            expected_direction=m.get("expected_direction") or "increase",
        )
        for m in data.get("metrics") or []
    ]
    return ExperimentInfo(
        id=int(data["id"]),
        name=data.get("name", ""),
        status=data.get("status", ""),
        hypothesis=data.get("hypothesis") or "",
        environment=environment,
        flag=feature.get("name", ""),
        feature_id=feature.get("id"),
        variations=variations,
        metrics=metrics,
        raw=data,
    )


def parse_results(data: dict | None, exp: ExperimentInfo) -> ResultsSnapshot | None:
    res = (data or {}).get("results")
    if not res or not res.get("payload"):
        return None
    payload = res["payload"]
    by_id = {m.id: m for m in exp.metrics}
    identities: dict[str, int] = {}
    metrics: dict[str, MetricResult] = {}
    for m in payload.get("metrics") or []:
        info = by_id.get(m.get("metric_id"))
        name = info.name if info else str(m.get("metric_id"))
        direction = info.direction if info else "up"
        for key, stats in (m.get("variants") or {}).items():
            n = int(stats.get("n", 0))
            identities[key] = max(identities.get(key, 0), n)
        inf = m.get("inference") or {}
        # Two-arm contracts: take the single treatment's inference.
        treat = next((v for k, v in inf.items() if k != CONTROL_KEY), None)
        metrics[name] = MetricResult(
            name=name,
            direction=direction,
            chance_to_win=None if not treat else float(treat["chance_to_win"]),
            lift=None if not treat else float(treat["lift"]),
        )
        # Keep per-variant inference for multi-arm callers.
        for k, v in inf.items():
            if k != CONTROL_KEY:
                metrics[f"{name}@{k}"] = MetricResult(
                    name=name,
                    direction=direction,
                    chance_to_win=None if not v else float(v["chance_to_win"]),
                    lift=None if not v else float(v["lift"]),
                )
    return ResultsSnapshot(
        identities=identities,
        metrics=metrics,
        as_of=res.get("as_of"),
        srm_p_value=payload.get("srm_p_value"),
        raw=data or {},
    )


def _parse_time(s: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def parse_exposure_rate(data: dict | None) -> float | None:
    exp = (data or {}).get("exposures")
    if not exp or not exp.get("payload"):
        return None
    ts = exp["payload"].get("timeseries") or {}
    points = ts.get("points") or []
    if not points:
        return None
    step = _dt.timedelta(hours=1) if ts.get("granularity", "hour") == "hour" else _dt.timedelta(days=1)
    times = [t for t in (_parse_time(p.get("bucket", "")) for p in points) if t]
    if not times:
        return None
    hours = ((max(times) - min(times)) + step).total_seconds() / 3600
    per_arm: dict[str, int] = {}
    for p in points:
        for k, n in (p.get("new_identities") or {}).items():
            per_arm[k] = per_arm.get(k, 0) + int(n)
    if not per_arm or hours <= 0:
        return None
    return min(per_arm.values()) / hours


def parse_audit(row: dict) -> AuditEntry:
    author = row.get("author")
    if isinstance(author, dict):
        author = author.get("email") or str(author.get("id"))
    env = row.get("environment")
    if isinstance(env, dict):
        env = env.get("api_key") or env.get("name")
    return AuditEntry(
        id=row.get("id"),
        log=row.get("log", ""),
        author=author,
        related_object_type=row.get("related_object_type"),
        related_object_id=row.get("related_object_id"),
        environment=env,
        raw=row,
    )
