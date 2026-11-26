"""The narrow interface between Ulysses and Flagsmith.

The contract service talks to Flagsmith only through this protocol. The real
implementation (``rest.py``) calls Flagsmith's own REST endpoints under
``/api/v1/environments/<env>/experiments/``, the same ones the official MCP
server and the dashboard use. ``fake.py`` is an in-memory implementation for
tests.

Every number Ulysses acts on comes back through ``get_results``: identities per
arm and Flagsmith's chance-to-win for each metric. Nothing here computes
statistics.

Shipping: Flagsmith's ``complete`` action takes a rollout decision. With
``rollout_decision=variant`` it sets the flag's environment default to serve
that variant to 100% of identities and removes the experiment's rollout
override; with ``control`` every variant weight goes to 0 so control is served.
So "complete and ship the winner on the same flag" is one Flagsmith call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

CONTROL_KEY = "control"  # Flagsmith reserves this variant key for control


class FlagsmithError(Exception):
    """A Flagsmith call failed. ``status`` is the HTTP status when there was one."""

    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class Throttled(FlagsmithError):
    """Flagsmith refused a refresh because the last one was under five minutes ago."""

    def __init__(self, message: str, retry_after_seconds: float | None = None):
        super().__init__(message, status=429)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class MetricInfo:
    id: int | None
    name: str
    direction: str  # Flagsmith metric polarity: "up", "down" or "informational"
    expected_direction: str  # "increase" / "decrease" (goal) or "not_increase" / "not_decrease" (guardrail)

    @property
    def is_guardrail(self) -> bool:
        return self.expected_direction in ("not_increase", "not_decrease")


@dataclass(frozen=True)
class Variation:
    key: str
    option_id: int | None  # Flagsmith multivariate option id; None for control


@dataclass(frozen=True)
class ExperimentInfo:
    id: int
    name: str
    status: str  # created / running / paused / completed
    hypothesis: str
    environment: str
    flag: str
    feature_id: int | None
    variations: list[Variation]
    metrics: list[MetricInfo] = field(default_factory=list)
    project: int | None = None
    raw: dict = field(default_factory=dict, repr=False)

    def variation(self, key: str) -> Variation | None:
        return next((v for v in self.variations if v.key == key), None)

    def metric(self, name: str) -> MetricInfo | None:
        return next((m for m in self.metrics if m.name == name), None)


@dataclass(frozen=True)
class MetricResult:
    name: str
    direction: str  # Flagsmith polarity of the metric ("up" / "down" / "informational")
    chance_to_win: float | None  # Flagsmith's own number for treatment vs control (None = collecting data)
    lift: float | None = None

    def chance_of_increase(self) -> float | None:
        """Undo Flagsmith's orientation: the chance that treatment raised the metric.

        Flagsmith reports ``chance_to_win = Phi(lift / SE)`` and, for lower-is-better
        metrics, ``1 - Phi(lift / SE)``. This inverts only that flip.
        """
        if self.chance_to_win is None:
            return None
        return 1.0 - self.chance_to_win if self.direction == "down" else self.chance_to_win


@dataclass(frozen=True)
class ResultsSnapshot:
    identities: dict[str, int]  # variation key -> identities Flagsmith counted
    metrics: dict[str, MetricResult]
    as_of: str | None = None
    srm_p_value: float | None = None
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class AuditEntry:
    id: Any
    log: str
    author: str | None  # a user's email; None when an API key acted
    related_object_type: str | None
    related_object_id: Any
    environment: str | None = None
    raw: dict = field(default_factory=dict, repr=False)


class FlagsmithGateway(Protocol):
    def get_experiment(self, environment: str, experiment_id: int) -> ExperimentInfo: ...

    def update_hypothesis(self, environment: str, experiment_id: int, text: str) -> None: ...

    def start(self, environment: str, experiment_id: int) -> None: ...

    def refresh_results(self, environment: str, experiment_id: int) -> None:
        """Ask Flagsmith to recompute results. Raises ``Throttled`` inside the five-minute floor."""
        ...

    def get_results(self, environment: str, experiment_id: int) -> ResultsSnapshot | None:
        """Flagsmith's latest computed results, or None if it has computed none yet."""
        ...

    def complete(self, environment: str, experiment_id: int, ship: str | None) -> dict:
        """Complete the experiment. ``ship`` is the variation key to serve to everyone,
        or None to keep control."""
        ...

    def exposure_rate_per_hour(self, environment: str, experiment_id: int) -> float | None:
        """New identities per arm per hour so far, from Flagsmith's exposures timeseries."""
        ...

    def audit_log(self, project: int) -> list[AuditEntry]: ...
