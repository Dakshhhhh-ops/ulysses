# Feature Health

Flagsmith's Feature Health (open source, `api/features/feature_health/` in 2.295.2) lets a monitoring tool mark a flag unhealthy. Ulysses uses it in both directions:

- **Out.** When a look records a guardrail breach or a sample-ratio mismatch, or the service records a deviation, the contract service tells Flagsmith the experiment's flag is `unhealthy`, with the reason and a link to the decision card. Flagsmith tags the flag Unhealthy.
- **In.** A contract can sign `[health] block_ship_when_unhealthy = true`. While Flagsmith reports the flag unhealthy in the experiment's environment, from any provider, `execute` refuses to ship the treatment (403). So an outside alert, for example from Flagsmith's Grafana provider, holds a ship that the statistics alone would allow.

## Setup

```sh
uv run ulysses lab setup            # also creates the project's Feature Health "Webhook" provider
```

`lab setup` lists the project's providers and creates `{"name": "Webhook"}` only if it is missing. Flagsmith allows one provider of each name per project (`unique_together = ("name", "project")`), and the create endpoint does not check for that first, so a second POST is a database error: we saw a 500 on the lab. The response's `webhook_url` is written to `.env` as `ULYSSES_HEALTH_WEBHOOK_URL` (or to the file given by `--env-file`; `scripts/judge_demo.py` passes `.run/judge.env`). Both files are git-ignored. The URL is a signed path that Flagsmith accepts with no key, so treat it as a secret. Ulysses prints it cut short (`.../feature-health/IjUyYj...`), the service's response guard withholds any response that contains it, and the agent never receives it. `--no-health` skips this step.

Without `ULYSSES_HEALTH_WEBHOOK_URL`, the service sends no reports. The execute check still runs for contracts that sign the rule, because reading events needs only the admin key the service already holds.

## Endpoints (checked against the 2.295.2 source)

| Call | Source | Used for |
|---|---|---|
| `GET /api/v1/environments/<env key>/` | `environments/views.py` (`lookup_field = "api_key"`) | the environment's id, name and project. Contracts drafted live record project 0, so the project comes from here |
| `GET`, `POST /api/v1/projects/<p>/feature-health/providers/` | `projects/urls.py:89-92`, `FeatureHealthProviderViewSet` | find or create the Webhook provider. Create needs project admin |
| `POST /api/v1/feature-health/<signed path>` | `api/urls/v1.py:58-62`, `feature_health_webhook` (`AllowAny`) | report. Returns 200 when an event was stored, 400 when the body does not parse or names no flag in the provider's project, 404 for an unknown path |
| `GET /api/v1/projects/<p>/feature-health/events/` | `projects/urls.py:94-97`, `FeatureHealthEventViewSet` (`VIEW_PROJECT`, unpaginated) | the execute check. It returns the latest event per (provider, environment, external id, feature): `get_latest_by_project` in `models.py` |

The webhook body is `providers/webhook/types.py`'s `WebhookEvent`:

```json
{"feature": "ulysses_checkout_cta", "environment": "ulysses-demo", "status": "unhealthy",
 "reason": {"text_blocks": [{"title": "Ulysses: guardrail 'ulysses_refund_request' breached", "text": "Look 3: ..."}],
            "url_blocks": [{"url": "http://127.0.0.1:8200/card/<look id>", "title": "Ulysses decision card"}]}}
```

`environment` is the environment's name, not its key. Flagsmith looks the name up in the project and stores the event with `environment: null` if it finds nothing. A null environment means the event covers every environment.

## What is reported, and when

| Event at the service | Status sent | Link |
|---|---|---|
| A look fires `guardrail_breach` | `unhealthy`, with the decision's reason | the decision card |
| A look's sample-ratio check is `mismatch` | `unhealthy` once. It is not sent again while the cause is the same | the decision card |
| A later look finds the split consistent again, and the last report was a sample-ratio `unhealthy` | `healthy` | the decision card |
| A deviation is recorded (audit webhook or look-time state check) | `unhealthy` | the contract page |
| `execute` completes the experiment, and the last report for it was a breach or mismatch `unhealthy` | `healthy`: the arm behind the alert is no longer served | the decision card |

Ulysses sends `healthy` only to clear an `unhealthy` it sent itself. Webhook events carry no external id, so everything that posts to a project's Webhook provider shares one latest-event slot per flag and environment, and a `healthy` sent on its own could hide another caller's alert. A deviation is never cleared automatically. Someone has to look at it and dismiss it in Flagsmith (`POST .../feature-health/events/<id>/dismiss/`).

**Flagsmith down.** A report never breaks the look or execution it belongs to. Each report is kept in the service database (`health_reports`), tried three times with backoff (0.5 s, then 1 s), and left `pending` if it still fails. Pending reports go out in order before the next report, at the next look or execution. A report Flagsmith answers with 400 or 404 is marked `rejected` and not retried. After 20 tries a report is marked `gave_up`. The look's response carries `feature_health: {reported, causes, delivered, state, attempts, flagsmith_status}` and never the URL.

## The signed rule

```toml
[health]
block_ship_when_unhealthy = true
```

New contracts get it by default: `contract.draft`, the agent's `sign_contract` through `draft_for_experiment`, and the `ulysses init` template. It is part of the canonical form, so it is in the hash. A contract signed before the field existed has no `health` key, so its hash and its behaviour do not change. `block_ship_when_unhealthy = false` signs that health is not checked.

At `execute`, for a contract that signs the rule:

- `ship_treatment` is refused (403, `Refused`) while any latest event for the flag is `UNHEALTHY` and its environment is this experiment's environment or null. The detail names the providers and their reason titles. The refusal is logged, and the confirmed proposal stays confirmed, so `execute` succeeds once the alert clears.
- If the events cannot be read, `ship_treatment` is refused too, because the signed rule could not be checked.
- `keep_control` is never blocked. It is the rollback.

An executed outcome stores the check in its result (`feature_health: {state, environment, feature_id, events_for_flag, unhealthy: [{provider, scope}]}`, with no event times). `ulysses verify` prints it as `outcome <id> feature health`. It passes for a ship recorded as healthy and fails for a ship recorded as unhealthy or unreadable, or with no record. This is the service's own record of what Flagsmith said at that moment. The events are not in the bundle, so verify shows what the service relied on but cannot prove it.

A replay (`--replay`) answers with no health events. Every recorded ship was made while the flag was healthy, because the service refuses otherwise.

## Live check (shared self-hosted lab, Flagsmith 2.295.2, project 3, flag `ulysses_checkout_cta`)

Run with the worktree's code against `http://localhost:8000`. Responses are copied as returned, with event times left out and the signed path cut short.

1. `ensure_health_provider`: the provider list was `[]`, so POST `{"name": "Webhook"}` created one: `{"project_id": 3, "name": "Webhook", "webhook_url": "http://localhost:8000/api/v1/feature-health/IjUyYj...", "created": true}`.
2. A second POST of `{"name": "Webhook"}`: `500` (Django's "Server Error (500)" page), the duplicate provider.
3. `GET .../providers/`: `200 [{"created_by": null, "name": "Webhook", "project": 3, "webhook_url": ".../feature-health/IjUyYj..."}]`. `created_by` is null for an API key.
4. `GET /environments/<key>/`: `{"id": 5, "name": "ulysses-demo", "project": 3}`.
5. Webhook POST with `status: "unhealthy"`: `200`, empty body. With an unknown feature: `400`. With `status: "sick"`: `400`.
6. `GET .../events/`: `200 [{"id": 1, "environment": 5, "feature": 109, "provider_name": "Webhook", "reason": {"text_blocks": [{"text": "...", "title": "Ulysses: live Feature Health check"}], "url_blocks": [{"url": "http://127.0.0.1:8200/", "title": "Ulysses"}]}, "type": "UNHEALTHY"}]`.
7. The execute check read it as `{"state": "unhealthy", "environment": "ulysses-demo", "feature_id": 109, "events_for_flag": 1, "unhealthy": [{"provider": "Webhook", "scope": "this environment"}]}`.
8. Seconds later, through the task processor, the project had a system tag `{"id": 1, "label": "Unhealthy", "color": "#FFC0CB", "is_system_tag": true, "type": "UNHEALTHY"}` and the feature's `tags` were `[1]`.
9. Webhook POST with `status: "healthy"`: `200`. Events: `[{"id": 2, "environment": 5, "feature": 109, "provider_name": "Webhook", "type": "HEALTHY", ...}]`. The latest event replaces the earlier one. The check read `healthy`, and the feature's `tags` went back to `[]`.
10. The service's reporter (`FeatureHealth.after_look` with a synthetic `guardrail_breach` decision, then `after_execute`): `{'reported': 'unhealthy', 'causes': ['guardrail_breach'], 'delivered': True, 'state': 'sent', 'attempts': 1, 'flagsmith_status': 200}`, an event `UNHEALTHY / Webhook / feature 109 / environment 5` titled `Ulysses: guardrail 'ulysses_refund_request' breached`. Then `healthy`, delivered, 200. The flag was left HEALTHY.

No experiment was started, ended or changed for this check. The Webhook provider stays on project 3.

## Seeing it in the dashboard

The API stores events and tags whatever the dashboard shows. But the dashboard hides health unless its own `feature_health` flag is on. `frontend/web/components/tags/Tag.tsx` (about lines 101-106) returns nothing for an `UNHEALTHY` tag when `Utils.getFlagsmithHasFeature('feature_health')` is false. The same check gates the Feature Health tab in project settings, the tag filter and the environment warnings. That flag comes from "Flagsmith on Flagsmith": the dashboard reads it from a Flagsmith environment set by two variables on the Flagsmith container, which `api/app/views.py:project_overrides` sends to the browser as `window.projectOverrides`:

- `FLAGSMITH_ON_FLAGSMITH_API_URL`: the API the dashboard asks for its own flags, for example `http://localhost:8000/api/v1/`;
- `FLAGSMITH_ON_FLAGSMITH_API_KEY`: the client key of an environment that has a flag named `feature_health`, enabled.

For a demo: create that environment and flag. On open-source self-hosted Flagsmith that means a separate organisation, because each organisation gets one project. Set the two variables on the `flagsmith` service, restart it and reload the dashboard. The flag then shows the Unhealthy tag and the reason. We have not done this on the shared lab, because it needs a container restart.
