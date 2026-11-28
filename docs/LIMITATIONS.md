# Limitations

What Ulysses does not do, and what the evidence in this repository does not show. Each point is stated once here.

## The evidence

- **No user traffic.** Offline numbers are simulations on Flagsmith's own `stats.py`. Live numbers come from Flagsmith's results endpoint, but the identities are synthetic, sent through Flagsmith's Python SDK. See [STATISTICS.md](STATISTICS.md).
- **The live batches are small.** They cannot separate the rules, and false "Winning" moments were rare in them (1 of 60 experiments in the two signed-contract batches). The 10,000-run simulation carries the claim about how often a false "Winning" happens.
- **The language-model runs used a test double.** The Gemini transcripts are real model outputs against the real contract service, but Flagsmith was the test double with fixture results. Only three of the four scenarios were recorded, and no `--llm` run against a live Flagsmith was recorded: the Gemini free-tier quota ran out (`examples/llm_agent/transcripts/gemini-spike.json` shows the 429 quota errors).
- **The confirm-page screenshot is a UI fixture** over a test double of Flagsmith, not live data.
- **Recorded on self-hosted 2.295.2, and once on Flagsmith Cloud.** One full demo run (both experiments, to a decision each) ran on Flagsmith Cloud's free plan ([CLOUD.md](CLOUD.md)). Not exercised there: the audit-log webhook, which needs a public URL for the service (Cloud cannot reach 127.0.0.1), so deviation detection on Cloud rested on the state check at each look and at execute; the audit-log API, which returns nothing on the free plan; and a deliberate deviation. One run is not a soak test of Cloud's rate limits or results latency.
- **The agent container path is tested in parts** (image, compose config, a live container check), not yet end to end against Flagsmith.

## The statistics

- **The guarantee is asymptotic and covers one primary metric.** At low base rates and short horizons, false losses run over their nominal share (5.6% at a 2% base rate with 60 expected conversions per arm); false wins stay under it. The service refuses a design under 100 expected conversions per arm unless the signers record a reason, and warns below 400 (`MIN_CONVERSIONS_AT_HORIZON` and `WARN_CONVERSIONS_AT_HORIZON` in `src/ulysses/core.py`).
- **It waits longer than the first-95% rule.** That is the price of not shipping false winners; the measured delay is in [STATISTICS.md](STATISTICS.md#the-headline-run-as-the-readme-quotes-it).
- **Flagsmith's SDK can store an event batch twice after a timeout.** That cannot change exposures or occurrence metrics, which the demo and calibration use, but it can inflate Count, Sum and Mean metrics.

## The boundary

- **Ulysses binds the agent, not a dashboard admin.** Someone with dashboard rights can still complete an experiment or edit its flag. Ulysses records that as a deviation and writes it on the experiment; it cannot prevent it.
- **The agent boundary is a process and key boundary.** It holds as long as the agent's process cannot read the service's `.env` or the signer keys. In the default demo everything runs as one OS user on one machine. For real use, run the contract service, its `.env` and the signer keys where the agent cannot read them (another OS user, a container or another host). An agent that can read `.env` can call Flagsmith directly.
- **The agent gets Ulysses' six tools, not a Flagsmith key.** On open-source Flagsmith every key that can read experiments can also write flags, so handing the agent the official Flagsmith MCP server would hand it a way around the contract; the contract service holds the only write key. `ulysses init --with-flagsmith-mcp` still adds the official server for people and other uses, with a warning. Until our upstream fix ([#9138](https://github.com/Flagsmith/flagsmith/pull/9138)) lands, that server's `update_experiment` sends `PUT`, which 2.295.2 rejects, so Ulysses writes the hypothesis with `PATCH` itself.
- **Deviation replay depends on the webhook.** The audit-log list API returns nothing on an unlicensed self-hosted instance, so deviation replay works from the entries the webhook delivered.
- **A bundle verified offline is unpinned.** `ulysses verify --bundle` on a bundle whose Flagsmith is gone cannot run the hypothesis and final-state checks, and says so. What verify can and cannot prove is in [VERIFY.md](VERIFY.md).
