"""Parsing Flagsmith payloads, using the shapes recorded in the lab (timestamps removed)."""

import httpx
import pytest

from ulysses.flagsmith.gateway import Throttled
from ulysses.flagsmith.rest import RestFlagsmith, parse_audit, parse_experiment, parse_results

EXPERIMENT = {
    "id": 7, "name": "checkout", "hypothesis": "h", "status": "running",
    "feature": {"id": 3, "name": "ulysses_checkout_cta", "type": "MULTIVARIATE", "initial_value": "classic",
                "multivariate_options": [{"id": 4, "key": "bold", "string_value": "bold"}]},
    "metrics": [
        {"id": 11, "metric": 5, "metric_name": "ulysses_purchase", "aggregation": "occurrence",
         "direction": "up", "expected_direction": "increase"},
        {"id": 12, "metric": 6, "metric_name": "ulysses_refund_request", "aggregation": "occurrence",
         "direction": "down", "expected_direction": "not_increase"},
    ],
}
RESULTS = {"results": {"as_of": "T1", "is_final": False, "payload": {
    "srm_p_value": None,
    "metrics": [
        {"metric_id": 5, "variants": {"control": {"n": 142, "sum": 30.0, "sum_squares": 30.0},
                                      "bold": {"n": 158, "sum": 57.0, "sum_squares": 57.0}},
         "inference": {"bold": {"lift": 0.7076, "ci_low": 0.057, "ci_high": 1.358, "chance_to_win": 0.9835}}},
        {"metric_id": 6, "variants": {"control": {"n": 142, "sum": 9.0, "sum_squares": 9.0},
                                      "bold": {"n": 158, "sum": 3.0, "sum_squares": 3.0}},
         "inference": {"bold": None}},
    ]}}}


def test_parse_experiment_and_results():
    exp = parse_experiment(EXPERIMENT, "env")
    assert [v.key for v in exp.variations] == ["control", "bold"]
    assert exp.variation("bold").option_id == 4
    assert exp.metric("ulysses_refund_request").is_guardrail
    snap = parse_results(RESULTS, exp)
    assert snap.identities == {"control": 142, "bold": 158}
    assert snap.metrics["ulysses_purchase"].chance_to_win == pytest.approx(0.9835)
    assert snap.metrics["ulysses_refund_request"].chance_to_win is None  # collecting data
    assert parse_results({"results": None}, exp) is None


def test_parse_audit_webhook_entry():
    e = parse_audit({"id": 49, "author": None, "log": "Experiment 'x' completed for environment Dev",
                     "related_object_id": 4, "related_object_type": "EXPERIMENT",
                     "environment": {"api_key": "envkey"}})
    assert e.author is None and e.related_object_id == 4 and e.environment == "envkey"


def test_complete_sends_rollout_decision_with_option_id_and_429_is_throttled():
    sent = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append((req.method, req.url.path, req.content))
        if req.url.path.endswith("/results/refresh/"):
            return httpx.Response(429, json={"detail": "A refresh was requested recently."}, headers={"Retry-After": "120"})
        if req.method == "GET":
            return httpx.Response(200, json=EXPERIMENT)
        return httpx.Response(200, json={"status": "completed"})

    fs = RestFlagsmith("http://fs", "k", transport=httpx.MockTransport(handler))
    fs.complete("env", 7, "bold")
    assert sent[-1][1] == "/api/v1/environments/env/experiments/7/complete/"
    assert b'"rollout_variant":4' in sent[-1][2].replace(b" ", b"")
    fs.complete("env", 7, None)
    assert b'"control"' in sent[-1][2]
    with pytest.raises(Throttled) as ex:
        fs.refresh_results("env", 7)
    assert ex.value.retry_after_seconds == 120
