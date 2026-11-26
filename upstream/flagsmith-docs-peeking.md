# Drafts for Flagsmith/flagsmith (not posted)

Two short issues, written to be filed separately. Both were found while building on Flagsmith v2.295.2 self-hosted with `EXPERIMENTATION_ENABLED=true`.

---

## Issue 1. Statistics glossary: "peeking doesn't inflate your error rate" holds for the posterior, not for stopping at the first 95%

Hi, thanks for open-sourcing the experimentation engine. It made this easy to check.

The statistics glossary (`docs/docs/experimentation/statistics.md`, "Why Bayesian?") says:

> It's also safe to check whenever you like: peeking doesn't inflate your error rate the way repeated p-value checks do.

That is true of the chance to beat control as a description of the evidence at the moment you read it. It is not true of a decision rule a reader is likely to take from the same page: stop and ship once the chance to beat control is over 95% (the glossary's "Winning"). `compare_to_control` in `api/experimentation/stats.py` computes `chance_to_win = Phi(lift / SE)` with the delta-method SE and a flat prior, so stopping the first time it crosses 95% is a repeated one-sided z-test, and the false-call rate grows with the number of looks.

What we measured with the engine unmodified (`stats.py` loaded by hash, the 50-identity and 5-conversion floors applied). The first figure in each line is the one that matters for shipping: how often a variation that does nothing would have been shipped.

- Offline, simulated A/A experiments, 30 looks of 100 synthetic identities per arm, 10% conversion in both arms: stopping at the first look over 95% shipped a do-nothing variation in [pending: `ulysses calibrate offline`] of them, against [pending] when reading once at the end. Counting false "Losing" calls as well, [pending] made some false call. False Losing runs higher than false Winning at these counts because the delta-method z on relative lift is skewed when conversions are few, not only because of peeking.
- Live, on a self-hosted instance, A/A experiments with synthetic identities assigned by the engine through the Python SDK and read back from the results endpoint: [pending: `ulysses calibrate summarise <trace>`, with the batch size and its interval]. At this sample size the live false-Winning rate alone may not be distinguishable from 5%; the offline run is the precise one.

Scripts to reproduce both: https://github.com/Dakshhhhh-ops/ulysses (`ulysses calibrate offline`, `ulysses calibrate live`).

A smaller related point, from reading the source rather than the running dashboard: the "Winning variation" card names the variant with the highest chance to be best, shown next to that chance, as soon as inference exists (`getWinningVariant` in `frontend/web/components/experiments/results/derive.ts`). A reader who wants the 95% rule applies it themselves.

Suggested wording for the note: "The chance to beat control is a fair summary of the evidence whenever you look. If you plan to stop the experiment as soon as it passes 95%, decide the sample size and the stopping rule before you start; otherwise checking often will produce more false winners than the 95% suggests."

Happy to open a docs PR with that wording. We also built a small tool that keeps the engine as it is and adds a pre-signed stopping boundary on top (https://github.com/Dakshhhhh-ops/ulysses), in case it is a useful reference.

---

## Issue 2. MCP `update_experiment` sends PUT, which the experiments endpoint rejects without (and with) `feature`

The MCP server maps `update_experiment` to `PUT /api/v1/environments/{env}/experiments/{id}/` (operation `update_experiment` in `mcp/src/flagsmith_mcp/openapi.json`). On v2.295.2:

- PUT with only `name` or `hypothesis` returns 400 `{"feature": ["This field is required."]}`.
- PUT including the current `feature` returns 400 `{"feature": ["Cannot change the feature of an existing experiment."]}`.

So the tool cannot change the hypothesis or the name. `PATCH` on the same path with `{"hypothesis": "..."}` works and returns 200. Mapping the tool to `partial_update` (PATCH), or making `feature` read-only on update, would fix it. We use the PATCH form meanwhile.

Two smaller things from the same work, in case they are useful:

- `GET /api/v1/audit/` with an organisation API key returns 500 (`int()` of `APIKeyUser` in `_get_base_filters`, `api/audit/views.py`). The organisation-scoped route works.
- On an unlicensed self-hosted instance the audit-log list is always empty (free-plan `audit_log_visibility_days` is 0), while the organisation webhook still delivers every entry. A line in the self-hosting docs would save people some digging.
