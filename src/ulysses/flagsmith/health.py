"""Flagsmith Feature Health: the REST shapes Ulysses reads and writes (pure, unit-tested).

Verified against the Flagsmith source (``api/features/feature_health/``, ``api/projects/urls.py``,
``api/api/urls/v1.py``):

    GET    /api/v1/environments/<env key>/                       {"id", "name", "project", ...}
    GET    /api/v1/projects/<p>/feature-health/providers/        [{"name", "project", "webhook_url", "created_by"}]
    POST   /api/v1/projects/<p>/feature-health/providers/        {"name": "Webhook"} -> 201, same shape
    GET    /api/v1/projects/<p>/feature-health/events/           the latest event per (provider, environment,
                                                                 external id, feature): [{"id", "created_at",
                                                                 "environment", "feature", "provider_name",
                                                                 "reason", "type"}], unpaginated
    POST   /api/v1/feature-health/<signed path>                  the provider's webhook_url; no auth header (the
                                                                 signed path is the credential). 200 when an event
                                                                 was stored, 400 when the payload does not parse
                                                                 or names no feature in the provider's project.

Provider names are ``Webhook`` and ``Grafana`` (``FeatureHealthProviderName``), one of each per project
(``unique_together = ("name", "project")``), so setup lists before it creates. Event types are
``UNHEALTHY`` and ``HEALTHY``. The Webhook payload (``providers/webhook/types.py``) is::

    {"feature": <flag name>, "environment": <environment *name*, optional>,
     "status": "healthy" | "unhealthy",
     "reason": {"text_blocks": [{"text", "title"?}], "url_blocks": [{"url", "title"?}]}}

An event with no environment (or an environment name Flagsmith cannot find in the project) is stored with
``environment: null`` and applies to the feature in every environment. Webhook events carry no external
id, so every Webhook caller in a project shares one latest-event slot per feature and environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

WEBHOOK_PROVIDER = "Webhook"
UNHEALTHY = "UNHEALTHY"
HEALTHY = "HEALTHY"
WEBHOOK_PATH_MARK = "/feature-health/"


@dataclass(frozen=True)
class EnvironmentInfo:
    id: int
    name: str
    project: int


@dataclass(frozen=True)
class HealthProvider:
    name: str
    project: int | None
    webhook_url: str | None
    created_by: str | None = None


@dataclass(frozen=True)
class HealthEvent:
    id: Any
    feature: int | None
    environment: int | None  # None: the event applies to the feature in every environment
    provider_name: str | None
    type: str  # UNHEALTHY / HEALTHY
    reason: dict | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def unhealthy(self) -> bool:
        return self.type == UNHEALTHY

    def titles(self) -> list[str]:
        r = self.reason or {}
        return [b.get("title") or b.get("text", "")[:80] for b in r.get("text_blocks") or [] if isinstance(b, dict)]


def parse_environment(data: dict) -> EnvironmentInfo:
    project = data.get("project")
    if isinstance(project, dict):
        project = project.get("id")
    return EnvironmentInfo(id=int(data["id"]), name=str(data.get("name") or ""), project=int(project))


def parse_provider(data: dict) -> HealthProvider:
    return HealthProvider(name=str(data.get("name") or ""), project=data.get("project"),
                          webhook_url=data.get("webhook_url"), created_by=data.get("created_by"))


def parse_event(data: dict) -> HealthEvent:
    feature = data.get("feature")
    if isinstance(feature, dict):
        feature = feature.get("id")
    env = data.get("environment")
    if isinstance(env, dict):
        env = env.get("id")
    reason = data.get("reason")
    return HealthEvent(id=data.get("id"), feature=feature, environment=env, provider_name=data.get("provider_name"),
                       type=str(data.get("type") or "").upper(), reason=reason if isinstance(reason, dict) else None,
                       raw=data)


def parse_events(data: Any) -> list[HealthEvent]:
    rows = data.get("results", data) if isinstance(data, dict) else data
    return [parse_event(r) for r in rows or [] if isinstance(r, dict)]


def unhealthy_for(events: list[HealthEvent], feature_id: int | None, environment_id: int | None) -> list[HealthEvent]:
    """The latest events that say this feature is unhealthy in this environment.

    Flagsmith already returns only the latest event per provider, environment, external id and feature, so an
    UNHEALTHY row here has not been followed by a HEALTHY one from the same source. An event with no
    environment covers every environment.
    """
    return [e for e in events
            if e.unhealthy and e.feature == feature_id and (e.environment is None or e.environment == environment_id)]


def webhook_payload(*, feature: str, environment: str | None, status: str, title: str, text: str,
                    url: str | None = None, url_title: str | None = None) -> dict:
    """The body Flagsmith's Webhook provider accepts (``providers/webhook/types.py``)."""
    if status not in ("healthy", "unhealthy"):
        raise ValueError(f"status must be 'healthy' or 'unhealthy', not {status!r}")
    body: dict[str, Any] = {"feature": feature, "status": status,
                            "reason": {"text_blocks": [{"title": title, "text": text}],
                                       "url_blocks": [{"url": url, "title": url_title or "Ulysses"}] if url else []}}
    if environment:
        body["environment"] = environment
    return body


def redact_webhook(url: str | None) -> str | None:
    """A webhook URL with its signed path cut down: the path is the credential, so it is never shown in full."""
    if not url:
        return url
    i = url.find(WEBHOOK_PATH_MARK)
    if i < 0:
        return url[: max(12, len(url) // 3)] + "..."
    head = url[: i + len(WEBHOOK_PATH_MARK)]
    return head + url[i + len(WEBHOOK_PATH_MARK):][:6] + "..."
