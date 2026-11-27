# Reference: Flagsmith experiment API through `flagsmith api`

Paths are relative to the API URL and need the `api/v1/` prefix (without it the dashboard HTML comes
back). `E` is the environment's client-side key, `X` the experiment id. All calls need an
environment admin credential (`flagsmith login`, or an organisation admin API key). Checked against
Flagsmith 2.295.2 (`api/experimentation/`) and Flagsmith CLI v2 (`internal/cmd/api.go`).

## `flagsmith api` in one paragraph

GET by default. `-F key=value` adds a typed JSON field (numbers, `true`, `false`, `null`) and implies
POST; `-f` adds a string field; `--input -` sends stdin as the body; `-X` sets the method. A non-2xx
status prints the response body to stderr and exits non-zero. `--jq` filters JSON output and prints
strings raw. Do not put `--jq` on a call that returns an empty body (the refresh calls).

## Endpoints

| Call | Notes |
|---|---|
| `GET .../environments/E/warehouse-connections/` | plain list; `?exclude_event_stats=true` keeps it light. Empty: no events are stored, stop |
| `POST .../environments/E/experiment-metrics/` | `{"name", "aggregation": occurrence\|count\|sum\|mean, "direction": up\|down\|informational, "definition": {"version": 1, "event"}}`. Metrics belong to the environment; list with `?q=` (paginated: `.results`) |
| `POST .../environments/E/experiments/` | flag must be multivariate; `experiment_rollout` required and must list every variation; one non-completed experiment per flag (409) |
| `PATCH .../experiments/X/` | only `name` and `hypothesis` (`-X PATCH -f hypothesis=...`); PUT is rejected |
| `POST .../experiments/X/start/` | created or paused -> running |
| `POST .../experiments/X/pause/` | running -> paused |
| `POST .../experiments/X/complete/` | `rollout_decision`: `control`, `variant` (+ `rollout_variant` = variation **id**), or `disable`. Ends the experiment and sets the environment default to the decision |
| `POST .../experiments/X/results/refresh/` | 202, empty body. 429 "A refresh was requested recently" inside 5 minutes; 400 before start or once results are final |
| `GET .../experiments/X/results/` | `{"results": null}` until the first refresh finishes |
| `GET .../experiments/X/` | `status`, `hypothesis`, `metrics[]` (`metric`, `metric_name`, `direction`, `expected_direction`), `feature.multivariate_options[]` (`id`, `key`), `experiment_rollout` |

Statuses: `created`, `running`, `paused`, `completed` (terminal). After `complete`, exactly one more
refresh is allowed; it freezes the results at the end time (`is_final: true`).

## Results payload

`results.as_of`, `refresh_requested_at`, `last_error_at`, `is_final`, and `payload.metrics[]`, one per
attached metric, keyed by `metric_id` (the metric's id, not the attachment's):

- `variants[<key or "control">].n`: exposed identities in that arm.
- `inference[<variation key>]`: `lift` (relative, 0.07 = +7%), `ci_low`/`ci_high` (95% interval of the
  lift), `chance_to_win`. `null` while either arm has under 50 identities, an occurrence metric has
  under 5 conversions in an arm, or control's mean is 0: the dashboard's "Collecting data".
- `chance_to_win` is `Phi(lift / SE)` in the metric's good direction: flipped for `direction: down`.
  There is no prior and no correction for repeated reads or several variations.
- `srm_p_value` is null for experiments created through this API (it reads the environment default
  split, which the rollout zeroes), so `stop_rule.py` runs the chi-square itself on `n` against the
  experiment's own split, flagging p < 0.001 once there are 100 identities.

A refresh that failed still uses its 5-minute window: trust a read only when `as_of` is later than
`refresh_requested_at` and `last_error_at` is not. "Winning" is not an API field; the dashboard's
"Winning variation" card names the highest chance on the first metric with no threshold at all.

## Guardrails

A guardrail is an attached metric with `expected_direction` `not_increase` or `not_decrease`. The
results carry no verdict for it. Its chance of getting worse is the chance it moved the forbidden way:
for `not_increase`, `chance_to_win` if the metric's direction is `up`, else `1 - chance_to_win`; the
reverse for `not_decrease`.

## Why the stop rule, measured

Flagsmith `stats.py` v2.295.2, unmodified, on 10,000 simulated A/A experiments (5 seeds), 30 reads of
100 synthetic identities per arm, base rate 10%; then 10,000 with a true +25% lift. 95% Wilson
intervals. Simulated identities, not user traffic.

| Rule | Ships a do-nothing variation (A/A) | Ships a real +25% lift | Ships before the planned size (+25%) |
|---|---|---|---|
| First read over 95% | 13.4% (12.7-14.1) | 91.0% | most runs (mean stop at read 9.6) |
| One read at the planned size, 95% | 3.9% (3.5-4.3) | 91.0% | never |
| `stop_rule.py` (early 0.9987, final 0.95) | 3.9% (3.5-4.3) | 91.0% (90.4-91.5) | 41.1% (40.2-42.1) |

Reproduce from the Ulysses repository (github.com/Dakshhhhh-ops/ulysses):
`uv run python scripts/skill_rule_calibration.py --flagsmith-src <Flagsmith checkout>` writes
`data/skill-rule-offline.json`; the first two rows are `uv run ulysses calibrate offline` on the same draws.

The 95%/5% labels are a 10%-level two-sided rule. For a stricter claim, plan with `--final 0.975`.
At low conversion counts Flagsmith's relative-lift z is skewed toward false losses, so a losing
primary metric never stops the experiment early under this rule; only a guardrail does.
