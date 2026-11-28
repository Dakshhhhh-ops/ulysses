# The demo on Flagsmith Cloud

Every other recording in this repository ran on self-hosted Flagsmith. This one is the full judge demo run once on Flagsmith Cloud (`https://api.flagsmith.com`), free plan, in an organisation that already existed: two experiments, synthetic visitors, the stand-in owner, the scripted agent, a decision on each.

```sh
uv run python scripts/judge_demo.py --flagsmith https://api.flagsmith.com \
    --org-key-env FLAGSMITH_CLOUD_ADMIN_KEY --organisation <organisation id>
```

`--org-key-env` names the variable that holds the organisation's admin API key, in the environment or the git-ignored `.env.local` / `.env`. With it, `judge_demo.py` signs nobody up, never starts Docker, checks that the key can see the organisation (one call), and keeps its state in `.run/judge-cloud.env` and `.run/judge-cloud.db`, apart from the self-hosted `.run/judge.env`. The state file records the variable's name, not the key. `--project` picks the project (default: the organisation's first) and `--webhook-url` registers the audit-log webhook at a public URL. The run took 29.2 minutes from launch to the end of the summary.

Replay it, and check both decisions:

```sh
uv run python scripts/judge_demo.py --replay data/replay-cloud.jsonl --no-hold
# 10 of 10 recorded decisions recomputed identically by this checkout's engine; 0 event(s) did not reproduce
# 6 of 6 recorded refusals refused again for the recorded reason (1 on Flagsmith's 'Winning' reading, 95% or more, while the contract held)
uv run ulysses verify --bundle data/verify-cloud-cta.json   # CONSISTENT (unpinned): 20 checks passed
uv run ulysses verify --bundle data/verify-cloud-aa.json    # CONSISTENT (unpinned): 22 checks passed
```

With `--check-flagsmith`, run against Cloud right after the run, the bundles ended `VERIFIED: all 22 checks passed` (real effect) and `VERIFIED: all 24 checks passed` (A/A). The live form, `ulysses verify <id> --db .run/judge-cloud.db`, ended `VERIFIED: all 21 checks passed` and `VERIFIED: all 23 checks passed`. These read the hypothesis and the final state from Cloud itself.

## The looks

Visitors were simulated by `examples/shop/visitors.py` at 90 a minute (synthetic identities: purchase 10% after classic, 17% after bold, refunds 30% in both), through Flagsmith's Python SDK with local evaluation. Cloud assigned the variations and computed every chance to win. Both contracts: 1,088 identities per arm at the horizon, up to 30 looks, Lan-DeMets O'Brien-Fleming, alpha 0.05 two-sided. "Needs" is the chance to win the contract required at that look to call a win.

| Experiment | Look | Identities per arm (smaller) | Flagsmith chance to win | Contract needs | Verdict |
|---|---|---|---|---|---|
| Real effect, `ulysses_checkout_cta` (experiment 1696) | 1 | 225 | 93.51% | 99.9999% | HOLD |
| | 2 | 449 | 98.67% | 99.95% | HOLD, while Flagsmith's docs would call it Winning |
| | 3 | 693 | 99.77% | 99.52% | WIN: ship bold |
| A/A, `ulysses_headline_aa` (experiment 1697) | 1 | 222 | 75.57% | 99.9999% | HOLD |
| | 2 | 457 | 69.91% | 99.95% | HOLD |
| | 3 | 683 | 26.77% | 99.55% | HOLD |
| | 4 | 916 | 54.98% | 98.69% | HOLD |
| | 5 | 1144 | 55.60% | 98.05% | HORIZON: keep control |

The refund guardrail passed at every look (highest reading 95.36% chance worse, against a stop rule near 99.7%). Each experiment also has a first look at 0 identities, taken right after the start, which counts nothing.

## What the agent did

The scripted agent (`scripts/agent_session.py`, no model) held only the Ulysses agent token. On the real-effect experiment:

- it called `start_under_contract` before maya had signed and was refused ("this contract has no valid owner signature"). On the A/A experiment the stand-in owner signed before the agent's first start call, so that start went through;
- at look 2, Flagsmith read 98.67% while the contract held. The agent said "Flagsmith says Winning at 98.67%. Shipping." and called `execute ship_treatment`, which was refused ("no look has fired this contract");
- after the win at look 3 it asked to execute before maya's tap ("waiting for the owner's one-tap confirmation") and asked for `keep_control` instead ("the contract authorises 'ship_treatment' for this outcome, not 'keep_control'"). Both were refused. Bold shipped after maya's signed tap, 16.6 minutes after the start.

On the A/A experiment the same two calls after the horizon were refused, and control was kept after maya's tap, 26.7 minutes after the start. Cloud shows both experiments completed, 1696 with rollout decision `variant` (serving bold) and 1697 with `control`, each with the contract hash and the outcome line in its hypothesis. No experiment was left running in the organisation.

## Cost

**Model spend: none.** The default agent is the scripted one.

**Flagsmith API calls: about 1,250, under 2,000 at most**, against the free plan's 50,000 a month. This is an estimate from the run's settings and log, not a meter reading:

- the shop's SDK fetched the environment document every 10 seconds for about 29 minutes: about 175 requests;
- it sent events (exposures and `track_event`) to `events.api.flagsmith.com` in batches every 2 seconds: at most about 870 requests, plus 11 retried sends;
- the contract service made roughly 10 to 15 admin API calls per look (the refresh, the results reads, experiment reads, Feature Health reads), plus the start, the hypothesis writes and the completions: about 150 over the 10 looks. Cloud recomputed results within about 2 seconds of each refresh, so the service's results polling stopped after one or two reads;
- setup (`ulysses lab setup`, `scripts/demo_setup.py`, the key check): about 40.

About 2,400 synthetic visitors went through the shop. Flagsmith's own usage endpoint (`/api/v1/organisations/<id>/usage-data/total-count/`) still read 0 right after the run.

No call was refused with 429 and every refresh was accepted. Twice the events host could not be resolved from this machine for a minute or so (8 `NameResolutionError` and 3 read timeouts). The SDK kept those batches and sent them on a later try, and every look's identity counts kept rising.

## Differences from self-hosted, and what was not exercised on Cloud

- **Events go to a different host.** Cloud takes SDK events on `https://events.api.flagsmith.com/`. Its core API answers `/api/v1/events` with 404 "This environment does not store events in Flagsmith". `ulysses lab setup` now writes the right events URL into `.run/lab.json` (`lab.events_api_url`; `FLAGSMITH_EVENTS_API_URL` overrides it), and the demo shop uses it.
- **The audit-log webhook was not registered.** Cloud cannot reach a service on 127.0.0.1, so without `--webhook-url` the demo skips it and says so. Deviation detection on Cloud therefore rested on the state check at each look and at execute. Neither the webhook's deliveries nor its signature check ran on Cloud.
- **The audit-log API was unavailable.** On this free-plan organisation `GET /api/v1/audit/?project=<id>` returned 500; the plan's subscription metadata lists `audit_log_visibility_days` 0. The service logs that and goes on with the webhook copies it holds, of which there were none.
- **No deviation was staged.** Nobody changed an experiment from the dashboard during the run, so the deviation path is untested on Cloud.
- **Feature Health did run.** `ulysses lab setup` created the project's "Webhook" provider on Cloud. Before shipping, the service read Cloud's Feature Health for the flag (healthy, no events). That is the bundle's `feature health` line. No breach was reported to it, because the guardrail never fired.
- **Not run on Cloud:** the language-model agent, the signed-autonomy clause, the agent container and the calibration batches. One run is one run: it says nothing about Cloud's rate limits or results latency under heavier use.

The environment key inside the two contracts is the client-side key of the run's `ulysses-demo` environment on Cloud. It is part of the signed contract text, so it cannot be removed without breaking the hash. It is a client key, not an API key, but delete or rotate that environment if it should stop working (`--check-flagsmith` on the bundles then fails, as it should).
