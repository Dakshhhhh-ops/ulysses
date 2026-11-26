# What the contract guarantees, and what it does not

Every figure here was printed by `uv run ulysses calibrate offline` (Flagsmith's own `stats.py`, pinned by SHA-256, on simulated synthetic units) or by `uv run ulysses horizon` / `uv run ulysses boundary`. None of it is user traffic.

## The number Ulysses thresholds

Flagsmith's engine (`api/experimentation/stats.py`, `compare_to_control`) computes, for each treatment and metric:

```
lift           = (mean_t - mean_c) / mean_c
SE             = sqrt(var_t / (n_t mean_c^2) + mean_t^2 var_c / (n_c mean_c^4))   # delta method
chance_to_win  = Phi(lift / SE)                    # 1 - Phi(...) for lower-is-better metrics
```

Flagsmith's statistics glossary calls a variation Winning above 95% and Losing below 5%, and says it is safe to check whenever you like. The API has no verdict field, and the dashboard's "Winning variation" card names the leading variant as soon as inference exists (`frontend/web/components/experiments/results/derive.ts`). The calibration therefore scores the glossary's rule: stop the first time the chance is over 95% or under 5%. That is a two-sided rule at the 10% level.

Ulysses inverts the number, `z = Phi^-1(chance_to_win)`, which gives back Flagsmith's own `lift / SE` (undoing the flip for lower-is-better metrics). It adds no estimator of its own. The only statistics in Ulysses are the stopping boundaries and the horizon calculation.

## The boundary

Before the experiment starts, the contract fixes a horizon N (identities in the smaller arm), a maximum number of looks K at N/K, 2N/K, ..., N, and a two-sided level alpha (default 0.05). The boundary comes from the Lan-DeMets alpha-spending function of O'Brien-Fleming type, applied to each side at alpha/2:

```
a(t) = 2 - 2 Phi( z_{1 - alpha/4} / sqrt(t) )      per side, t = information fraction
```

At each look the boundary b_k solves P(first crossing at look k | no effect) = spent(t_k) - spent(t_{k-1}), using the canonical joint distribution of sequential Wald statistics (independent increments on the score scale), computed by numerical integration. A look that lands late gets the boundary for the fraction it actually has; only the look at the horizon spends what is left. Win if z >= b_k, loss if z <= -b_k, no decision if the horizon arrives first.

Checks in `tests/test_boundary.py`:

- Classical O'Brien-Fleming constants C_B(K, 0.05) match Jennison and Turnbull (2000), Table 2.3: 1.977, 2.004, 2.024, 2.040, 2.087, 2.126 for K = 2, 3, 4, 5, 10, 20.
- Lan-DeMets boundaries for K = 5 equally spaced looks match gsDesign `sfLDOF` and ldbounds: 4.8769, 3.3569, 2.6803, 2.2898, 2.0310.
- The total null crossing probability for 30 looks is 0.025 per side to 1e-6, and a 200,000-run Monte Carlo of the joint distribution agrees with the integration.

## What the calibration measured

Pooled over five seeds, 10,000 simulated A/A experiments, 30 looks of 100 synthetic identities per arm, 10% conversion in both arms (`.run/calibration/offline.md`, 95% Wilson intervals):

| Rule | Level | Ships a do-nothing variation (false Winning) | Any false call |
|---|---|---|---|
| Read once at the final look (95%/5%) | 10% two-sided | 3.9% (3.5-4.3) | 9.8% (9.3-10.4) |
| Stop at the first look over 95% or under 5% | 10% two-sided, read 30 times | 13.4% (12.7-14.1) | 44.7% (43.7-45.6) |
| Ulysses contract, alpha 0.10 | 10% two-sided | 3.0% (2.7-3.4) | 10.1% (9.5-10.7) |
| Ulysses contract, alpha 0.05 (default) | 5% two-sided | 1.3% (1.1-1.6) | 5.3% (4.9-5.8) |

Compare like with like. The 95%/5% thresholds are a 10%-level rule, so the fair comparison is the contract at alpha 0.10: reading the 95% rule at every one of 30 looks raises false ships from 3.9% to 13.4%; the contract at the same level keeps them at 3.0%. The default contract is stricter still (alpha 0.05).

With a true relative lift of +25% (10% to 12.5%), 10,000 runs: the first-95% rule calls the win in 91.0% of runs (and calls it the wrong way in 5.0%); the contract calls it in 88.4% at alpha 0.10 and 79.3% at alpha 0.05, never the wrong way, at mean look 19.9 and 21.8 of 30. Against a fixed-horizon test with the same false-ship level and the same 79.3% power, which needs 2,858 identities per arm, the default contract used 2,350.2 on average: 17.8% fewer.

## What it guarantees

If the z values Ulysses reads behave like the sequential Wald statistics they are (asymptotically normal, independent increments, information proportional to identities), then across all looks the chance of declaring a winner when there is no effect is at most alpha/2. This holds however often the agent looks, because refreshes between the planned looks are not analyses: they show Flagsmith's number and spend nothing.

The measured false-win rate stayed at or below alpha/2 in every design tested, including the low-count ones below.

## Where it fails, and by how much

The guarantee is asymptotic and Flagsmith's z is on relative lift. At small counts that z is skewed: a treatment that happens to fall behind gets a smaller SE, so large negative z values come more often than large positive ones. The costly error (shipping a variation that does nothing) stays under alpha/2; the cheap one (keeping control after a false loss) runs over its nominal alpha/2. Contract at alpha 0.05, A/A, 2,000 runs per row:

| Base rate | Identities per arm per look x looks | Expected conversions per arm at the horizon | False Winning (nominal 2.5%) | False Losing (nominal 2.5%) |
|---|---|---|---|---|
| 2% | 100 x 30 | 60 | 0.1% (0.1-0.4) | 5.6% (4.7-6.7) |
| 5% | 100 x 30 | 150 | 0.8% (0.5-1.3) | 5.9% (4.9-7.0) |
| 10% | 100 x 30 | 300 | 1.6% (1.1-2.2) | 4.0% (3.2-5.0) |
| 2%, horizon from `ulysses horizon` for a 25% lift | 605 x 30 | 363 | 1.2% (0.8-1.8) | 3.6% (2.9-4.6) |

So "at most alpha in total" is not what the contract gives at these counts: the total false-call rate measured 5.3% at the 10% base rate and up to about 6.7% at 2% and 5% with a small horizon, almost all of it false losses. The service turns this into policy: a contract whose design gives fewer than 100 expected conversions per arm at the horizon is refused unless the signers record an override reason, and below 400 the signing page carries a warning. A horizon smaller than the one `ulysses horizon` gives for the stated baseline and lift is also refused without an override.

## Guardrails

Guardrails used to stop on a fixed rule: 97.5% chance the metric moved the wrong way, at any refresh. That is the peeking problem again. In A/A (a 10% guardrail metric with no effect in either arm, same design as above) it stopped 6.0% of experiments for a not-increase guardrail and 22.0% for a not-decrease guardrail; the asymmetry is the same delta-method skew.

The contract now gives each guardrail its own one-sided budget, spent with a Lan-DeMets Pocock-type function, alpha * ln(1 + (e - 1) t), and judged only at the counted looks, on z = Phi^-1(Flagsmith's chance the metric got worse). Pocock-type spending keeps early stops possible, which a safety rule needs. Between looks the guardrail's number is shown and nothing stops. The default budget is 0.01, chosen from measurement:

| Guardrail budget | not increase: false stops in A/A | not decrease: false stops in A/A | Runs |
|---|---|---|---|
| 0.05 | 1.6% (1.4-1.9) | 10.7% (10.1-11.4) | 10,000 |
| 0.025 | 0.4% (0.2-0.6) | 7.3% (6.5-8.1) | 4,000 |
| 0.01 (default) | 0.1% (0.1-0.2) | 4.4% (4.0-4.8) | 10,000 |

A guardrail stop only ever keeps control, so it cannot raise the false-ship rate; it costs power on experiments that were fine. By default a win also does not ship while Flagsmith has no result for a guardrail ("collecting data": fewer than 5 conversions per arm for an occurrence metric); at the horizon that becomes "no decision, keep control". A contract can turn this off with `require_guardrail_results_to_ship = false`, and the signing page shows the setting.

## What else it does not cover

- Flagsmith calls chance_to_win a Bayesian posterior. Under a flat prior it is one, and a posterior read at a fixed time is well calibrated. Ulysses does not dispute that. It adds a frequentist property for the case the docs invite: acting the first time the number crosses 95%.
- One treatment against control and one primary metric. Several treatments or several goal metrics need a correction the contract does not apply; the service refuses multi-arm experiments.
- It does not fix bad data. Quarantined identities and event loss are Flagsmith's to detect. Flagsmith's sample-ratio check returns no value once an experiment's rollout is applied (see the field notes), so the card cannot show it.
- An amendment that changes the horizon after looking at the data is a data-dependent change. The second signature and the line written on the experiment make it visible; they do not make it statistically free.
- The live calibration on a running Flagsmith is a small sample (tens of experiments) and confirms the direction of the offline result; the offline table carries the large-sample claim.

## Horizon helper

`ulysses horizon` uses the same delta-method variance as Flagsmith to turn a baseline and the smallest relative lift you care about into identities per arm, then solves for the drift at which the signed boundary is crossed with the requested power.

It asks for more identities than a difference-in-proportions calculator (Evan Miller's, Optimizely's), because Flagsmith's z is on relative lift and its control-variance term carries an extra (1 + lift)^2. For a one-look test at alpha 0.05 and 80% power:

| Baseline, smallest lift | Helper, one look | Difference-in-proportions formula | Ratio | Helper, 30-look contract |
|---|---|---|---|---|
| 5%, +10% | 34,363 | 31,231 | 1.100 | 36,067 |
| 2%, +25% | 17,268 | 13,807 | 1.251 | 18,124 |
| 10%, +25% | 3,140 | 2,504 | 1.254 | 3,296 |

The sequential design then adds a small inflation over one look, at 80% power: 1.025 for 5 looks, 1.038 for 10, 1.040 for 12 and 1.050 for 30 (`ulysses.boundary.inflation_factor`). Looks are spaced over the exposure rate read from Flagsmith's exposures timeseries.
