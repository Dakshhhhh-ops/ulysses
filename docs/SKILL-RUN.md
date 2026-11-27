# A language model runs the skill

The skill `skills/flagsmith-experiment-rollout` was given to a language model (OpenAI `gpt-5-mini`,
reasoning effort low) as its only instructions. The run used the shared self-hosted Flagsmith lab
(open-source Flagsmith with Experimentation on, demo environment `ulysses-demo`). The model's only tool
was a shell that runs the official Flagsmith CLI v2 and the skill's `scripts/stop_rule.py`, nothing else.
It never saw a key. Every number below is what Flagsmith returned in that run.

Said plainly:

- **The visitors were synthetic.** `examples/skill_run/traffic.py` sent new identities through the
  Flagsmith Python SDK at 100 a minute. Purchase was 10% on control and 15% on `bold` (a real +50%
  effect), and `checkout_error` was 2% in both arms (a guardrail with no real difference).
- **A harness stood in for the owner.** Where the skill says to ask the person, the model ended its
  turn with the question, and `examples/skill_run/run.py` answered "Yes, go ahead." It logs each answer
  as `[stand-in owner]`. It never read the command it was approving. That mattered at the end (below).
- **Wake-ups.** Flagsmith refreshes results at most every 5 minutes. When the model ended a turn to
  wait, the harness slept 300 s and sent one fixed message with no numbers in it. No model tokens were
  spent while waiting.

Transcript, scrubbed of keys, times and dates:
[`skills/flagsmith-experiment-rollout/examples/live-run.jsonl`](../skills/flagsmith-experiment-rollout/examples/live-run.jsonl).
Its last row holds the per-call token counts.

## What the model did (experiment 396)

The person's request gave the planning answers the skill asks for: the flag and values, the events,
control at about 10%, the smallest lift worth shipping (40% relative), and about 100 visitors a minute.

| Step | What the model ran |
|---|---|
| Plan | `python scripts/stop_rule.py plan --baseline-rate 0.10 --min-lift 0.40 --variants 1` → `{"identities_per_arm": 1147, "tag": "[stop-rule v1 n=1147 early=0.9987 final=0.95 guard=0.95]"}`. It told the person "~22.9 minutes", then listed all six set-up commands and asked "May I run them?" (one yes for the whole set-up) |
| Flag | `flagsmith feature create ulysses_skill_buy_flag --value classic --enabled`, then `flagsmith feature variant add ulysses_skill_buy_flag --value bold --key bold --weight 0` |
| Metrics | `flagsmith api "api/v1/environments/$E/experiment-metrics/" --jq .id --input - <<'EOF'` with `{"name": "ulysses_skill_buy_purchase", "aggregation": "occurrence", "direction": "up", ...}` → `21`. The guardrail `ulysses_skill_buy_checkout_error` (`"direction": "down"`) → `22` |
| Experiment | `flagsmith api "api/v1/environments/$E/experiments/" --jq '{id, status}' --input -`. The hypothesis ended `[stop-rule v1 n=1147 early=0.9987 final=0.95 guard=0.95]`. Metrics: 21 `increase`, 22 `not_increase`. 50% to variant 220 → `{"id":396,"status":"created"}` |
| Start | It asked first, then ran `flagsmith api "api/v1/environments/$E/experiments/396/start/" -X POST --jq '{status, started_at}'` → `"status":"running"` |
| Looks | Each time: `flagsmith api ".../396/results/refresh/" -X POST && { flagsmith api ".../396/" ; flagsmith api ".../396/results/" ; } \| python scripts/stop_rule.py check -` |
| Decide | It showed the verdict in plain words and the exact command, and asked "May I run it?" |
| Complete | `flagsmith api ".../396/complete/" -F rollout_decision=variant -F rollout_variant=220 --jq '{status, rollout_decision, rollout_variant}'` → `{"rollout_decision":"variant","rollout_variant":220,"status":"completed"}` |
| Verify | The final refresh first got 429, and the stand-in owner waited the seconds Flagsmith asked for. Then `flagsmith eval ulysses_skill_buy_flag --identity rollout-check-1 --jq '{enabled, value, variant}'` → `{"enabled":true,"value":"bold","variant":"bold"}` |

Along the way the shell refused 5 commands, and nothing ran for any of them. Three used `echo` (two
with `jq`) to pass ids between commands; the model then wrote the ids in literally. One used `$E` before
it was set; the model then set `E=$(flagsmith config --jq .environment.value)`. One began a line with `&&`.

## The stop rule at each look

`n` is Flagsmith's identities per arm, chance and lift are Flagsmith's own for `bold` on purchases, and
"worse" is Flagsmith's chance that `bold` raised checkout errors.

| Look | n (bold / control) | Chance to win | Lift (95% interval) | Flagsmith's label | Guardrail worse | Verdict |
|---|---|---|---|---|---|---|
| 1-5 | none | - | - | - | - | `COLLECTING` (no exposures; see the correction below) |
| 6 | 66 / 74 | 0.8919 | +169% (-99% to +437%) | Inconclusive | - | `HOLD` |
| 7 | 320 / 339 | 0.8819 | +37% (-24% to +97%) | Inconclusive | - | `HOLD` |
| 8 | 581 / 593 | 0.9976 | +91% (+28% to +155%) | **Winning** | 0.34 | `HOLD`: under the early bar 0.9987 |
| 9 | 853 / 832 | 0.9988 | +72% (+26% to +119%) | Winning | 0.18 | **`SHIP`** `bold`, early, before 1,147 |

At look 8 Flagsmith's results page would have said Winning. The model reported the hold ("99.76% (just
under early bar 99.87%)") and kept looking. Sample ratio checks: p = 0.50, 0.46, 0.73, 0.61. The
estimated lift at the stop (+72%) is above the true +50%, as an early stop's estimate tends to be.

**Correction during the run.** The request named the flag `ulysses_skill_buy`. The model created
`ulysses_skill_buy_flag`. The harness had pointed the visitors at the requested name, so for looks 1-5
nobody was exposed. The operator started a second visitors process on the real flag before look 6. The
transcript marks the point. The harness now reads the flag name from the experiment it starts.

## Final state in Flagsmith

After call 39, experiment 396 was `completed` with `rollout_decision: variant` and `rollout_variant: 220`,
and an identity was served `bold`. The job was done there.

Then the model went on. Asked whether to "remove the losing variation and the experiment code path", the
stand-in owner said yes. The model got `bold` and control the wrong way round. It proposed
`flagsmith flag update ulysses_skill_buy_flag --weight bold=0` ("serves control again if needed") and
`flagsmith api ".../396/" -X DELETE`. It showed both and asked, and the stand-in said yes again. The
CLI's own confirmation stopped the first try ("pass --yes to confirm"). The model added `--yes` and ran
both. The operator stopped the run after that turn. As left in the lab: experiment 396 is deleted (GET
returns 404), and the flag serves `classic` / `control` to a new identity.

This is the safety finding of the run. The skill asked first, every time. But a yes from someone who
does not read the command is no protection, and the skill's step 6 and its "Undo" line invited the
mix-up. SKILL.md now says the undo commands are only for a person who asks to reverse the decision. It
says cleanup is a later code change, never a weight change or a DELETE of the experiment, and that the
job ends at step 6.

## Tokens and cost (experiment 396 run)

| | Calls | Input tokens | of which cached | Output tokens (reasoning) | Cost |
|---|---|---|---|---|---|
| Job, plan to verified rollout | 39 | 249,641 | 237,696 | 5,209 (576) | $0.0193 |
| Whole run, with the reversal | 43 | 290,448 | 276,992 | 5,520 (576) | $0.0213 |

Priced at `gpt-5-mini`'s published rates ($0.25 input, $0.025 cached input, $2.00 output per million),
from the usage the API reported for each call. 95% of input tokens were cached prompt prefix. The five
empty looks (calls 18-27) cost 10 of the 39 calls.

## The other attempts

Eight live attempts were made; each ran until something stopped it. What each found:

| Attempt | Calls | Cost | What stopped it | What changed |
|---|---|---|---|---|
| 1 | 3 | $0.0019 | The harness crashed printing a `→` on a Windows console | Console output is UTF-8 |
| 2 (exp. 393) | 20 | $0.0090 | The model asked leave to refresh; the stand-in only answered questions about completing | The stand-in answers other questions after the wait |
| 3 | 25 | $0.0100 | `$E` was never set, so a POST went to `environments//`. The model then asked the owner for keys, which a yes cannot answer | The shell refuses unset variables; the run stops after 3 unanswered asks |
| 4 (exp. 394) | 18, plus 10 by a leftover process | $0.0139 | `--jq` on the refresh and on the bodies piped to `check`; `$A` without its `/` returned the dashboard's HTML | SKILL.md: no `--jq` on the look's lines; keep the `/`. `stop_rule.py` now names both mistakes |
| 5 (exp. 395) | 70 | $0.0288 | Shipped at the final read (chance 0.9923 at 1,396 / 1,411). The stand-in then said yes at once to each "retry the refresh?" during the 429, until the 70-call cap | The stand-in waits the seconds the 429 states |
| 6 (exp. 396) | 43 | $0.0213 | Recorded above | Traffic follows the experiment's flag; per-command exit status; SKILL.md step 6 |
| 7 (exp. 397) | 25 | $0.0116 | `A=.../397/` with a slash, so `.../397//start/` got 405 | SKILL.md: `A` has no final `/`; each path adds one |
| 8 (exp. 398) | 18 | $0.0078 | `A` with a slash again, then the OpenAI call failed with a DNS error | Stopped here |

Attempts 1 to 4 also misread the planning step: `--variants 2` for one variation (1,456 per arm
instead of 1,147), from the skill's own example `[--variants 2]`. SKILL.md now writes `[--variants 1]   # variations besides control`.
The operator completed experiments 393, 394 and 398 keeping control, and deleted 397, which had not
started. Experiment 395 stays completed with `bold` rolled out.

All eight together cost $0.1043. The OpenAI spend file shared by every run in this checkout
(`.run/openai-spend.json`, git-ignored) stands at $0.1473 of its $0.90 cap.

**Not yet shown.** No live run of the final SKILL.md has gone from plan to verified rollout. Attempts
7 and 8 used the latest wording except the last `A` fix, and both failed on `A`.

## Run it

```
uv run python examples/skill_run/run.py --export
```

It needs a lab Flagsmith with an Experimentation environment and a connected warehouse,
`FLAGSMITH_API_URL` and `FLAGSMITH_ADMIN_KEY` in `.env`, and an OpenAI key in `.env.local` (both
git-ignored). It takes the shared lab lock and releases it at the end. Every OpenAI call goes through the
spend guard in `examples/llm_agent/openai_model.py`. The run also stops at 60 model calls (`--max-calls`),
80 minutes, a $0.30 run budget, or 3 unanswered asks in a row.

`tests/test_skill_run.py` covers the shell allowlist (other programs, redirection, credential commands
and API-key paths are refused before anything runs), key scrubbing, run-state tracking and the token
accounting, with a fake CLI and a mocked OpenAI transport.
