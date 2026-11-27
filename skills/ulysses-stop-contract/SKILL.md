---
name: ulysses-stop-contract
description: Runs a Flagsmith experiment under a Ulysses stop contract - the owner signs the stop rule before the start, the agent takes looks and may ship only when the signed rule fires and the owner confirms. Sets up the flag, metrics and experiment with the Flagsmith CLI, then drives Ulysses (MCP tools or the ulysses CLI) and says exactly where to stop and ask a person. Use when the user wants an agent to run or ship a Flagsmith experiment without being able to ship early, or mentions Ulysses, stop contracts or signed rollouts.
license: MIT
compatibility: Needs the Flagsmith CLI v2, a running Ulysses contract service (github.com/Dakshhhhh-ops/ulysses, `uv run ulysses serve`) and, for the agent, only its URL and agent token. Self-hosted Flagsmith with Experimentation enabled.
metadata:
  version: "1.0"
---

# Run a Flagsmith experiment under a signed stop contract

The `flagsmith-experiment-rollout` skill records a stop rule in the experiment and trusts the agent
to follow it. Ulysses enforces it: the contract service holds the Flagsmith key, the owner signs the
rule before any data exists, and the service refuses to start without that signature, refuses to
ship before the rule fires, and refuses any action other than the one the contract assigns.

## Who does what

| Step | Who | Why |
|---|---|---|
| 1. Set up flag, metrics, experiment (not started) | agent, with the person's Flagsmith credentials, after they say yes | needs an admin key |
| 2. Hand over | person | removes Flagsmith credentials from the agent's shell |
| 3. Draft the contract | agent | the service sizes it; nothing is binding yet |
| 4. **Sign** | **owner only** | the agent must never run `ulysses sign` or open a signing key |
| 5. Start, look | agent | refused without step 4 |
| 6. **Confirm** the fired action | **owner only** | one signed tap on the card; never `ulysses confirm` |
| 7. Execute | agent | runs only the action the owner confirmed |
| Amend anything | agent drafts, **owner and second signer** sign | one signature authorises nothing |

## 1. Set up with the Flagsmith CLI

Follow steps 0 and 2 of `flagsmith-experiment-rollout` (connect, check the warehouse, create the
keyed variations, the primary metric first, guardrails as `not_increase` / `not_decrease`, create the
experiment). Skip its stop-rule tag: Ulysses writes the contract hash into the hypothesis at the
start. Do not start the experiment. Preview the size (no service needed):

```sh
uv run ulysses horizon --min-lift 0.10 --baseline-rate 0.10
```

## 2. Hand over

The operator runs the service where the agent cannot read its `.env` or the signer keys
(`uv run ulysses init`, then `uv run ulysses serve`). The agent's environment gets only
`ULYSSES_URL` and `ULYSSES_AGENT_TOKEN`. Check, and stop if either shows a credential:

```sh
flagsmith auth status            # must fail: no admin credential here
env | grep -c '^FLAGSMITH_API_KEY'   # must print 0
```

A client-side key for `flagsmith eval` is fine: it reads flags, it cannot change them.

## 3. Draft

MCP tool `sign_contract`, or:

```sh
uv run ulysses contract draft --environment "$E" --experiment "$X" --owner <owner> --second-signer <name> \
  --min-lift 0.10 --baseline-rate 0.10
```

It prints the contract hash `H` and a signing link. Give the owner the link and **stop**. Say what
they are signing: the horizon, the number of looks, the guardrails, and that rolling back can be made
autonomous (`without_confirmation=["keep_control"]`) but shipping never can.

## 4-5. Start and look

After the owner says they signed:

```sh
uv run ulysses start "$H"     # MCP: start_under_contract; 403 means not signed yet: ask again, do not retry
uv run ulysses look "$H"      # MCP: look; one refresh and read, HOLD or FIRE with a card
uv run ulysses watch "$H"     # looks on schedule (every 310 s) until it fires
```

A HOLD while Flagsmith reads over 95% is the point: report both numbers and keep going. Read the
experiment with `read_experiment` (MCP); chance to win is withheld between looks on purpose.

## 6-7. Fire, confirm, execute

When a look fires, the card names the outcome and the one action the contract assigns. Send the
owner the card's confirm link and **stop**. After they confirm:

```sh
uv run ulysses execute "$H" --action ship_treatment   # or keep_control, whichever the card names
```

A 403 is the contract working; quote its `detail` and do not look for another way. If the owner
signed `keep_control` as autonomous, a fired guardrail, loss or horizon runs it without a tap.

## If the person wants to change course

Shipping early, a bigger horizon, a different guardrail: draft an amendment and send both links.

```sh
uv run ulysses amend "$H" --reason "<why, in their words>" [--action-now keep_control]
```

## Afterwards

```sh
flagsmith eval <flag> --identity rollout-check-1 --jq '{enabled, value, variant}'   # what users get now
```

Ask the operator to run `uv run ulysses verify "$X"` where the service's store is (it re-checks the
hashes, signatures and every look against the signed rule), or `uv run ulysses export-bundle "$X"`
for a file anyone can check offline with `ulysses verify --bundle <file>`.

Report the decision, the card link and the `verify` result. Changes made outside the contract (in the
dashboard) are recorded as deviations; mention any the card shows.
