---
name: flagsmith-experiment-rollout
description: Runs a Flagsmith A/B experiment from plan to rollout with the Flagsmith CLI - sizes it, records a stop rule in the experiment before it starts, reads results on Flagsmith's five-minute refresh without peeking, checks guardrails, then completes it and rolls the winner (or control) out to everyone. Use when the user wants to set up, check on, decide or end a Flagsmith experiment, asks "is it winning?", "can we ship the variant?", or mentions chance to win, guardrails or rollout_decision.
license: MIT
compatibility: Needs the Flagsmith CLI v2 (`flagsmith`), Python 3.8+ for scripts/stop_rule.py, and a Flagsmith environment with Experimentation and a connected warehouse.
metadata:
  version: "1.0"
---

# Run a Flagsmith experiment to a rollout

Flagsmith's docs call a variation Winning when its chance to beat control passes 95%. That is fair
for one read. Checking every refresh and shipping at the first Winning ships a variation that does
nothing far more often: on Flagsmith's own `stats.py`, 30 reads of an A/A test shipped a do-nothing
variation in **13.4%** of 10,000 simulated runs, against **3.9%** for one read at the planned size.
The rule below (`scripts/stop_rule.py`) also shipped **3.9%**, kept the same power on a real +25%
lift (91.0%), and stopped early in 41.1% of those runs. Source: [references/REFERENCE.md](references/REFERENCE.md).

So: plan the size first, write the rule into the experiment, and let the script, not the label,
decide each read. (`python3` below is `python` on Windows.)

## Ground rules

- **Ask the person before every production write**: creating objects, `start`, `complete`, any
  flag or rollout change. Show the exact command. A yes for one step is not a yes for the next.
- Never decide from the dashboard's "Winning variation" card or a single `chance_to_win`. Run `check`.
- Never change the recorded plan (`[stop-rule ...]` in the hypothesis) after the start.
- Never edit the flag, its variations or the rollout while the experiment runs. The API does not
  lock them; changing them invalidates the result.
- Refresh results at most once per 5 minutes per experiment. A 429 means wait, not retry.
- Never print or commit API keys. Use `flagsmith login`, or the person's env file.
- Use `--jq` on reads you look at, so only the needed fields come back. Never on the two bodies piped
  into `check` (it needs them whole) or on a POST that answers with an empty body (`refresh`).
- If a command fails, show the error and stop; do not work around it.

## 0. Connect

```sh
flagsmith --version                     # v2 or later
flagsmith auth status
flagsmith config --jq '{p: .project.value, e: .environment.value, api: .apiUrl.value}'
E=$(flagsmith config --jq .environment.value)
flagsmith environment get "$E" --jq .name     # name has "prod"? every write below is production
flagsmith api "api/v1/environments/$E/warehouse-connections/?exclude_event_stats=true" --jq '[.[] | {id, status}]'
```

No binding: ask which project and environment, then `flagsmith init --project <p> --environment <key>`.
No warehouse connection: send the person to docs.flagsmith.com/experimentation/connect-a-warehouse and stop.
Below, `X` is the experiment id and `A=api/v1/environments/$E/experiments/$X`, with no `/` at the
end: each path adds its own (`$A/`, `$A/start/`). Set `E` first: every path uses it. A path without
its final `/` returns the dashboard's HTML, and a `//` returns 405.

## 1. Plan (before anything exists)

Ask: what changes, the success event, a guardrail event (errors, refunds), control's current
conversion rate, and the smallest relative lift worth shipping. Then:

```sh
python3 scripts/stop_rule.py plan --baseline-rate 0.10 --min-lift 0.10 [--variants 1]   # variations besides control
# {"identities_per_arm": 12786, "tag": "[stop-rule v1 n=12786 early=0.9987 final=0.95 guard=0.95]"}
```

Tell the person the identities per arm and how long that takes at their traffic. Too long: they
pick a bigger `--min-lift`, never a smaller sample for the same claim.

## 2. Set up (skip what exists; ask first)

```sh
flagsmith feature create <flag> --value <control value> --enabled
flagsmith feature variant add <flag> --value <value> --key <key> --weight 0   # every variation needs a key; "control" is reserved
flagsmith feature get <flag> --jq '{id, variants: [.variants[] | {id, key}]}'
flagsmith api "api/v1/environments/$E/experiment-metrics/" --jq .id --input - <<'EOF'
{"name": "purchase", "aggregation": "occurrence", "direction": "up", "definition": {"version": 1, "event": "purchase"}}
EOF
```

Create a guardrail metric the same way (`"direction": "down"` for errors). Then the experiment, with
the tag at the end of the hypothesis. The primary metric goes **first**; guardrails use
`not_increase` / `not_decrease`; list every variation in the rollout; control gets the remainder.

```sh
flagsmith api "api/v1/environments/$E/experiments/" --jq '{id, status}' --input - <<'EOF'
{"feature": <flag id>, "name": "<name>",
 "hypothesis": "<hypothesis> [stop-rule v1 n=12786 early=0.9987 final=0.95 guard=0.95]",
 "metrics": [{"metric": <primary id>, "expected_direction": "increase"},
             {"metric": <guardrail id>, "expected_direction": "not_increase"}],
 "experiment_rollout": {"enabled": true, "rollout_percentage": 100,
   "feature_state_value": {"type": "string", "value": "<control value>"},
   "multivariate_feature_state_values": [{"multivariate_feature_option": <variant id>, "percentage_allocation": 50}]}}
EOF
```

App code (SDK with events on, `getExperimentFlag` where the user sees it, `trackEvent` at success,
flush before exit): follow docs.flagsmith.com/experimentation/set-up-with-a-coding-agent. Show the
diff and wait for approval.

## 3. Start (ask first)

```sh
flagsmith api "$A/start/" -X POST --jq '{status, started_at}'
```

## 4. Look (each time, at most every 5 minutes)

```sh
flagsmith api "$A/results/refresh/" -X POST          # 202, empty body; 429 = too soon, wait
{ flagsmith api "$A/"; flagsmith api "$A/results/"; } | python3 scripts/stop_rule.py check -
```

Run these two lines as written: no `--jq` on them, and `$A/` with its slash.

`check` prints one JSON line. Act on `verdict` only:

| verdict | do |
|---|---|
| `COMPUTING` / `NO_RESULTS` | GET again in a minute; do not refresh again |
| `REFRESH_FAILED` | refresh after the window; tell the person if it fails twice |
| `COLLECTING` | under 50 identities or 5 conversions per arm; wait |
| `HOLD` | keep running. If `docs_label` is `Winning`, say so and say why it is not a decision |
| `STOP_AND_ASK` | sample ratio mismatch: show `n`, ship nothing, ask the person |
| `SHIP` / `KEEP_CONTROL` | the rule fired: go to 5 |
| `REFUSED` | no recorded plan, or the plan was changed: show `why` and stop |

Between looks, do not wait in a loop: estimate from `n` when the planned size arrives and tell the
person when to ask again, or schedule the commands above (cron or CI).

## 5. Decide and roll out (ask first)

Show the person the verdict line in plain words: arm, chance, lift and interval, each guardrail's
`worse`, and what the command does. On a yes, send `complete_body` exactly:

```sh
flagsmith api "$A/complete/" -F rollout_decision=variant -F rollout_variant=<id> --jq '{status, rollout_decision, rollout_variant}'
flagsmith api "$A/complete/" -F rollout_decision=control --jq '{status, rollout_decision}'   # KEEP_CONTROL
```

`complete` ends the experiment and serves the decision to everyone in one call. Then:

```sh
flagsmith api "$A/results/refresh/" -X POST                    # one final refresh freezes is_final
flagsmith eval <flag> --identity rollout-check-1 --jq '{enabled, value, variant}'
```

A rolled-out variant shows as `variant: <key>`; `flagsmith flag get` still shows control's `value`
with that variant at weight 100. Only if the person asks to reverse the decision (ask first):
`flagsmith flag update <flag> --weight <key>=0` serves control again; `flagsmith flag disable <flag>`
is the kill switch. Neither is cleanup: run neither after a rollout unless the person asked to undo it.

## 6. Leave notes, then stop

Report the decision, the verdict line, the dashboard link
(`<dashboard>/project/<project id>/environment/$E/experiments/$X`) and the follow-up for later, in
the code, not now with the CLI: once the result is permanent, delete the code path that lost (control's,
after a variant rollout) and the experiment code. Never set the rolled-out variation's weight to 0 and
never delete the experiment: it is the record of the decision. The job ends here.

For a stop rule the agent cannot bypass (signed before the start and enforced by a service that
holds the Flagsmith key), see the `ulysses-stop-contract` skill.
