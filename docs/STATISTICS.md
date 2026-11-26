# What the contract guarantees, and what it does not

Every figure here was printed by `uv run ulysses calibrate offline` (Flagsmith's own `stats.py`, pinned by SHA-256, on simulated synthetic units), by `uv run ulysses horizon` / `uv run ulysses boundary`, or by `uv run python scripts/stats_depth.py --flagsmith-src <Flagsmith checkout> --runs 2000` (the Monte Carlo checks, several treatments, the sample-ratio check and the power curves; fixed seeds, writes `.run/stats-depth.md`). None of it is user traffic.

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

`tests/test_monte_carlo.py` checks every boundary family a contract can sign against Monte Carlo of that joint distribution with fixed seeds (20 cases, 14.9 seconds in one run of `uv run pytest -q tests/test_monte_carlo.py`): two-sided boundaries at 2, 5, 12 and 30 looks, alpha 0.05 and 0.10, one comparison of a three-treatment contract (alpha 0.05/3), looks landing off schedule, the rounded z values written into a contract table, power and expected stop fraction at three drifts, one-sided guardrail boundaries (alpha 0.01, 0.01/3 and 0.05), the non-binding lower boundary, the Bonferroni split with a shared control, the engine itself (`decision.evaluate`) on 3,000 null paths of three treatments sharing a control, which must give the same outcome, winner and stopping look as the simulation's rule on every path and ship at most alpha/2 plus four standard errors, and the sample-ratio test. Each must agree within four standard errors plus 5e-4. From `scripts/stats_depth.py`, no effect, 200,000 paths per row:

| Boundary | Looks | Level | Upper, integration | Upper, Monte Carlo | Lower, Monte Carlo |
|---|---|---|---|---|---|
| Lan-DeMets O'Brien-Fleming, two-sided | 5 | 0.05 | 2.500% | 2.486% | 2.492% |
| Lan-DeMets O'Brien-Fleming, two-sided | 30 | 0.05 | 2.500% | 2.467% | 2.521% |
| Lan-DeMets O'Brien-Fleming, two-sided | 12 | 0.10 | 5.000% | 4.950% | 5.064% |
| One comparison of three (alpha/3) | 10 | 0.01667 | 0.833% | 0.816% | 0.829% |
| Pocock-type guardrail, one-sided | 6 | 0.01 | 1.000% | 1.011% | n/a |
| Pocock-type guardrail, one-sided | 30 | 0.01 | 1.000% | 0.984% | n/a |

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
- One primary metric. Several goal metrics would need a correction the contract does not apply; a contract names one, and other goal metrics attached in Flagsmith are not read. Several treatments are covered (below).
- It does not fix bad data beyond the split. Quarantined identities and event loss are Flagsmith's to detect; the sample-ratio check below catches arms that got the wrong share of identities, not events lost in both arms alike.
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

## Several treatments against one control

Flagsmith's results payload carries one `inference` entry per non-control variant (`api/experimentation/services.py`, `_metric_inference`: every key in `variants` except `control` is compared with control by `compare_to_control`), each with its own `chance_to_win`, and `variants[key].n` per arm. The REST parser keeps each one as `<metric>@<variant>`; a contract with several treatments reads those and never the single unsuffixed copy.

A contract lists the extra arms in `experiment.more_treatments` and must then sign `boundary.multiplicity = "bonferroni"`. With m treatments each treatment-vs-control comparison gets its own Lan-DeMets O'Brien-Fleming boundary at alpha/m (two-sided), and the signed table is that per-comparison boundary. At a counted look (the smallest arm past the planned count, every treatment with a Flagsmith result):

- win if any treatment crosses its upper boundary; the one shipped is the crossing treatment with the largest z, and the look records it (`winner`), so the owner's tap and the single Flagsmith `complete` call ship exactly that variant;
- loss only if every treatment is below its lower boundary at the same look;
- each guardrail is judged per treatment at its alpha/m, and a breach in any arm keeps control;
- an amendment cannot authorise "ship treatment now" for such a contract, because it cannot say which.

The decision card names the arm it is about: the winner when there is one, otherwise the treatment with the highest chance to win, for the header, the lift line and the plate's "ahead" label, and it labels each guardrail row with its arm.

What it guarantees: the chance of shipping a treatment that does nothing is at most alpha/2 (2.5% at the default), whatever the other arms do and however their z values correlate through the shared control. Bonferroni needs no assumption about that correlation: a false ship needs some null arm to cross its own boundary, each does so with probability at most alpha/(2m), and there are at most m of them. A lower boundary does not stop a single arm, so it is non-binding for that arm; its chance of a later upper crossing is still what it is built to spend (0.8333% against 0.8333% for one comparison of three at 12 looks, by integration).

Why Bonferroni and not Holm: Holm gains power by re-spending the alpha of a rejected comparison on the others, but the contract stops and ships at the first crossing, so there is nothing left to re-spend it on; at the stopping look Holm could only declare further winners, and only one is shipped. Dunnett-type boundaries, which use the correlation, would be less conservative; they are not implemented.

What it costs, measured. No treatment does anything, contract alpha 0.05, 10 looks, canonical model with a shared control, 200,000 runs per row:

| Treatments | Ships a do-nothing treatment, Bonferroni | Without the split (0.05 per comparison) |
|---|---|---|
| 1 | 2.52% | 2.52% |
| 2 | 2.34% | 4.58% |
| 3 | 2.26% | 6.28% |
| 5 | 2.13% | 9.18% |

Without the split the false-ship rate grows with every arm. With it, it stays under 2.5%, a little below because the comparisons are positively correlated.

The same question end to end, with Flagsmith's own `stats.py` (v2.295.2, pinned by SHA-256) on simulated 0/1 conversions at 10% in every arm, 30 looks of 100 synthetic identities per arm, Flagsmith's collecting-data floors, decided by `decision.evaluate`, 2,000 A/A experiments per row, 95% Wilson intervals:

| Treatments | Contract | Ships a do-nothing treatment | Keeps control on a false loss |
|---|---|---|---|
| 1 | alpha 0.05 | 20/2000 = 1.0% (0.6-1.5) | 79/2000 = 4.0% (3.2-4.9) |
| 2 | alpha 0.05, Bonferroni | 18/2000 = 0.9% (0.6-1.4) | 4/2000 = 0.2% (0.1-0.5) |
| 2 | no split | 43/2000 = 2.1% (1.6-2.9) | 8/2000 = 0.4% (0.2-0.8) |
| 3 | alpha 0.05, Bonferroni | 12/2000 = 0.6% (0.3-1.0) | 1/2000 = 0.1% (0.0-0.3) |
| 3 | no split | 54/2000 = 2.7% (2.1-3.5) | 6/2000 = 0.3% (0.1-0.7) |

At these low counts Flagsmith's relative-lift z is skewed (see above), so every false-win rate here is below its nominal level, and false losses are rare with several arms because all of them must fall below at once. The price is power: a three-treatment contract needs a larger horizon per arm (see the last table).

## Sample-ratio check

Flagsmith computes a sample-ratio p-value (`srm_p_value`), but against the environment-default weights, which are all zero once the experiment's rollout is applied; on running experiments it was `null` (field guide). A contract therefore signs its own check: `sample_ratio.split` (percent per arm, control included), `p_threshold` (default 0.001, the level Flagsmith's own docstring calls a broken split) and `min_total` (default 100, Flagsmith's own floor). The contract itself does not validate unless the split names exactly the experiment's arms, and the service refuses a contract whose split does not match the experiment's rollout weights in Flagsmith, and drafts it from those weights. Contracts signed before the check existed carry none; the signing page says so.

At every read Ulysses runs Pearson's chi-square of Flagsmith's exposure counts (`variants[key].n`, quarantined identities already removed) against the signed split, k - 1 degrees of freedom: the same formula as Flagsmith's `srm_p_value` (a test compares the two to 1e-9 when `FLAGSMITH_SRC` points at a checkout). A p-value under the threshold holds a win, with the counts and p-value in the reason and on the card; at the horizon it becomes no decision, keep control. It never ships, never stops on its own, and never blocks a loss or a guardrail stop.

What it costs and what it catches. Two arms signed 50/50, 30 looks, 200 new synthetic identities per look assigned at random with the true share below, 10,000 simulated experiments per row, 95% Wilson intervals:

| True share of control | Any look reads a mismatch | Mismatch at the final look | First look caught (median) |
|---|---|---|---|
| 0.50 (split intact) | 1.1% (0.9-1.3) | 0.1% (0.0-0.2) | look 10 |
| 0.51 | 8.5% (8.0-9.1) | 4.6% (4.2-5.0) | look 18 |
| 0.52 | 52.5% (51.5-53.5) | 42.9% (41.9-43.9) | look 19 |
| 0.55 | 100.0% (100.0-100.0) | 100.0% (100.0-100.0) | look 6 |

Read at every look, a correct split shows a mismatch at some look in about 1% of experiments; that matters only if a win is crossed at the same look, and then it costs power, not a false ship. A split 5 points off is caught within a few looks; one 1 point off mostly is not, at these counts. The check says the arms did not get the traffic signed for. It cannot say why, and it does not make a biased comparison unbiased.

## Power and expected sample size, for the signer

The signing page draws, from the contract itself, the chance it ships the treatment, the chance it keeps control on a loss, and the expected identities per arm before it stops, for each true effect (`ulysses.power`, also served as JSON at `/human/contract/<hash>/curves`). With a recorded baseline the x axis is the relative lift, turned into an expected z with Flagsmith's delta-method variance as in `ulysses horizon`; without one it is the expected z at the horizon. For two arms the curves are computed exactly by the same integration that builds the boundary; for several treatments they are simulated (4,000 runs per point, fixed seed, one treatment with the effect and the others with none). Examples, baseline 10%, smallest lift +25%, alpha 0.05, 80% power asked of `ulysses horizon`:

| Treatments | Looks | Horizon per arm | Method | Power at +25% | Expected per arm at +25% | One read at the end, same power | Expected per arm with no effect |
|---|---|---|---|---|---|---|---|
| 1 | 12 | 3,267 | analytic | 80.0% | 2,480 | 3,141 | 3,237 |
| 1 | 30 | 3,296 | analytic | 80.0% | 2,438 | 3,141 | 3,262 |
| 3 | 12 | 4,307 | simulation | 79.1% | 3,398 | 4,111 | 4,292 |

The curves describe the stop rule under the normal approximation with looks on their planned counts. They leave out the low-count skew of Flagsmith's z measured above, event loss and looks that land late. With no effect the contract runs to near its horizon, so then it is not cheaper than a single read at the end.

A contract signed with a negative smallest lift (a metric that should go down) is planned by `ulysses horizon` and drawn by `ulysses.power` on the size of that lift, so the chart's axis then reads "True relative improvement" and the page states the signed lift next to it; the marked +25% stands for a signed -25%.
