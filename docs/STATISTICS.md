# What the contract guarantees, and what it does not

## The number Ulysses thresholds

Flagsmith's engine (`api/experimentation/stats.py`, `compare_to_control`) computes, for each treatment and metric:

```
lift           = (mean_t - mean_c) / mean_c
SE             = sqrt(var_t / (n_t mean_c^2) + mean_t^2 var_c / (n_c mean_c^4))   # delta method
chance_to_win  = Phi(lift / SE)                    # flipped to 1 - Phi(...) for lower-is-better metrics
```

Flagsmith's statistics glossary calls a variation Winning above 95% and Losing below 5%, and says it is safe to check whenever you like. The API has no verdict field, and the dashboard's "Winning variation" card names the leading variant as soon as inference exists, with no threshold (`frontend/web/components/experiments/results/derive.ts`). The calibration therefore scores the glossary's rule: stop the first time the chance is over 95% or under 5%.

Ulysses inverts that number, `z = Phi^-1(chance_to_win)`, which gives back Flagsmith's own `lift / SE` (undoing the flip for lower-is-better metrics). It adds no estimator of its own. The only statistics in Ulysses are the stopping boundary and the horizon calculation.

## The boundary

Before the experiment starts, the contract fixes a horizon N (identities in the smaller arm), a maximum number of looks K at N/K, 2N/K, ..., N, and a two-sided level alpha (default 0.05). The boundary comes from the Lan-DeMets alpha-spending function of O'Brien-Fleming type, applied to each side at alpha/2:

```
a(t) = 2 - 2 Phi( z_{1 - alpha/4} / sqrt(t) )      per side, t = information fraction
```

At each look the boundary b_k solves P(first crossing at look k | no effect) = spent(t_k) - spent(t_{k-1}), using the canonical joint distribution of sequential Wald statistics (independent increments on the score scale), computed by numerical integration. A look that lands late gets the boundary for the fraction it actually has; the last look spends what is left. Win if z >= b_k, loss if z <= -b_k, no decision if the horizon arrives first.

Checks in `tests/test_boundary.py`:

- Classical O'Brien-Fleming constants C_B(K, 0.05) match Jennison and Turnbull (2000), Table 2.3: 1.977, 2.004, 2.024, 2.040, 2.087, 2.126 for K = 2, 3, 4, 5, 10, 20.
- Lan-DeMets boundaries for K = 5 equally spaced looks match gsDesign `sfLDOF` and ldbounds: 4.8769, 3.3569, 2.6803, 2.2898, 2.0310.
- The total null crossing probability for 30 looks is 0.025 per side to 1e-6, and a 200,000-run Monte Carlo of the joint distribution agrees with the integration.

## What it guarantees

If the z values Ulysses reads behave like the sequential Wald statistics they are (asymptotically normal, independent increments, information proportional to identities), then across all looks the chance of declaring a winner when there is no effect is at most alpha/2, and the chance of any call is at most alpha. This holds however often the agent looks, because looks between the planned ones are not analyses: they show Flagsmith's number and spend nothing.

The offline calibration runs Flagsmith's unmodified `stats.py` on 2,000 simulated A/A experiments with 30 looks. Stopping at the first look over 95% or under 5% made a false call in 45.0% of them. The contract at alpha 0.05 made one in 4.9% (`ulysses calibrate offline`; the full table is in the README).

## What it does not guarantee

- It is an asymptotic guarantee. With small counts the delta-method z on relative lift is skewed: a treatment that happens to fall behind gets a smaller SE, so large negative z values come more often than large positive ones. In the offline run this shows up as 1.4% false Winning against 3.5% false Losing at alpha 0.05. The costly error (shipping a variation with no effect) stays under alpha/2; the cheap one (keeping control) runs a little over.
- Flagsmith calls chance_to_win a Bayesian posterior. Under a flat prior it is one, and a posterior read at a fixed time is well calibrated. Ulysses does not dispute that. It adds a frequentist property for the case the dashboard invites: shipping the first time the number crosses 95%.
- Guardrails are not alpha-controlled. A guardrail stop only ever keeps control, so it cannot raise the false-ship rate, but a sensitive guardrail (default: 97.5% chance worse) will stop some experiments that were fine.
- It covers one treatment against control and one primary metric. Several treatments or several goal metrics need a correction the contract does not apply; the service refuses multi-arm experiments.
- It does not fix bad data. Sample-ratio mismatch, quarantined identities and event loss are Flagsmith's to detect; the card shows Flagsmith's SRM p-value when there is one.
- An amendment that changes the horizon after looking at the data is a data-dependent change. The second signature and the line written on the experiment make it visible; they do not make it statistically free.
- It binds the agent, not the people with dashboard rights. Anyone who can press Complete in Flagsmith still can. Ulysses records that as a deviation on the experiment.

## Horizon helper

`ulysses horizon` uses the same delta-method variance as Flagsmith to turn a baseline and the smallest relative lift you care about into identities per arm. It solves for the drift at which the signed boundary is crossed with the requested power, so the answer includes the sequential design's small inflation over a one-look test (2-4% for 5 to 10 looks at 80-90% power), and spaces looks over the exposure rate read from Flagsmith's exposures timeseries.
