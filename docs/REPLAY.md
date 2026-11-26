# Replay traces

A replay trace is a finished Ulysses run written down so anyone can watch it again in about two minutes,
without Docker or a Flagsmith, through the real contract service and the real pages:

    uv run python scripts/judge_demo.py --replay data/replay-judge-demo.jsonl [--minutes 2] [--open] [--no-hold]

The replay builds a fresh store under `.run/replay/<trace name>/` (git-ignored) and serves the overview,
contract, decision-card and calibration pages from it on <http://127.0.0.1:8240/> (`--port` moves it).
What is real and what is not:

| Part | In a replay |
| --- | --- |
| Contract service (`core.py`), store, decision engine, signature checks, pages, three.js bell | the real code of this checkout |
| Contracts | submitted again from their canonical form; the service recomputes each hash and it must match |
| Owner signatures and confirmations | the recorded Ed25519 signatures, verified against the public keys named in the contract |
| Each look's decision | recomputed by this checkout's engine from the recorded Flagsmith numbers and compared with the recorded decision |
| Flagsmith | not contacted. A stand-in answers each refresh and read with the numbers Flagsmith returned in the recorded run, and records the `complete` call |
| Time | the recorded timeline, sped up so the whole run takes `--minutes` (default 2). The speed-up is printed on every terminal line and on every page |

Every page carries an amber "REPLAY" bar ("A recorded run played back at N x ...; not live"), the page title
starts with "Replay", and each experiment's data chip and hatched band read "Replay of a recorded run",
with what the recorded run's traffic was (for the shipped trace: synthetic identities from
`examples/shop/visitors.py`). Pages reload themselves while the replay plays, so the bell and cards
follow the run. The script exits non-zero if any recorded event does not reproduce: a hash that differs,
a signature that does not verify, a refused call the service now accepts or refuses for a different
reason, a decision that differs in any of `outcome, action, counted, look_number, identities_per_arm,
chance_to_win, z, boundary_z, fires`, an execution that ends differently, or a replayed service that logs
more service faults or deviations than the header's `not_replayed` counts.

## Format (version 1)

JSON Lines. No calendar date or clock time appears anywhere: every time is `t`, seconds since the first
recorded event, and Flagsmith's result timestamp becomes `as_of_t` on the same scale. The exporter
refuses to write anything that looks like a date or a clock time outside signatures, keys and hashes.

Line 1, the header:

```json
{"format": "ulysses-replay", "version": 1, "title": "...", "flagsmith": "which Flagsmith the run used",
 "recorded_span_s": 1604.425, "counts": {"look": 11, "...": 0},
 "not_replayed": {"deviations": 0, "service_faults": 0, "refusal_notes": 0}, "note": "..."}
```

Then one event per line, in recorded order, each with `t` and `event`:

| `event` | Fields | What the replay does |
| --- | --- | --- |
| `signer` | `name`, `public_key` | registers the signer's public key (no private key is ever in a trace) |
| `experiment` | `environment`, `id`, `kind`, `flag`, `project`, `feature_id`, `control`, `treatment`, `treatment_option_id`, `metrics` (name, Flagsmith polarity `direction`, `expected_direction`), `rollout`, `source` | describes the experiment to the stand-in Flagsmith; `source` is the run's data-source record (`live`, `synthetic`, `lab`) |
| `contract` | `hash`, `canonical` | `submit(canonical)`; the recomputed hash must equal `hash` |
| `attempt` | `call` (`start_under_contract` or `execute`), `hash`, `action`, `expect: "refused"`, `recorded_reason` | repeats an agent call the service refused; it must be refused again, and the reason the service logs for that contract must equal `recorded_reason` (an unknown contract or any other error does not count) |
| `refusal_note` | `what`, `hash`, `recorded_reason` | a refusal the format cannot repeat; printed, not replayed |
| `signature` | `hash`, `role`, `signer`, `signature` | `sign(...)`; must verify against the key in the contract |
| `start` | `hash` | `start_under_contract(hash)`; the contract must become active |
| `look` | `hash`, `look_id`, `refreshed`, `flagsmith` (`as_of_t`, `identities`, `metrics` with `chance_to_win`, `lift`, `direction`, `srm_p_value`), `decision` | the stand-in returns these numbers (and refuses the refresh if `refreshed` is false, as Flagsmith's five-minute floor did); `look(hash)` must give the same decision |
| `proposal` | `hash`, `proposal_id`, `look_id`, `outcome`, `action`, `authorised_by` (only `"signed_autonomy"`, when the contract's autonomy clause authorised it) | checks that the look that fired made this proposal, authorised the same way |
| `autonomous_execute` | `proposal_id`, `hash`, `action`, `recorded_state`, `recorded_result` | the agent's `execute` of a proposal the signed autonomy clause authorised (no tap); the served variation must match |
| `confirm` | `proposal_id`, `hash`, `signer`, `signature`, `recorded_state`, `recorded_result` | `confirm(...)` with the owner's recorded signature; the stand-in completes the experiment, and the served variation must match |

Look and proposal ids are kept from the recorded run, because the owner's confirmation signature covers
the look id.

Not in format 1: amendments (a contract that supersedes another, with the second signer's signature),
deviations and audit-webhook entries. The exporter refuses a store with amendments and counts the rest
under `not_replayed`. Calibration traces (`data/live-*.jsonl`) are a different format, scored with
`uv run ulysses calibrate summarise`; `--replay` refuses them.

## Writing a trace

From a finished run's store, read-only, with `data-sources.json` and `demo.json` next to it if present:

    uv run python scripts/export_replay.py --db <run dir>/judge.db --out data/replay-<name>.jsonl \
        --title "what this run was" --flagsmith "which Flagsmith it ran on" [--note "..."]

The shipped `data/replay-judge-demo.jsonl` was written this way from the store of one clean
`uv run python scripts/judge_demo.py` run from a fresh clone against the stock Flagsmith 2.295.2 that
`docker-compose.yml` starts: an A/A experiment and a real-effect experiment, both with simulated visitors
(synthetic identities). Its header carries the counts. The environment key inside the contracts is the
client-side key of that throwaway local Flagsmith; it stays because it is part of the signed contract.

`tests/test_replay.py` exports a run made over the test double of Flagsmith, replays it with no
difference, shows that an edited Flagsmith number or a signature by the wrong key does not reproduce, and
replays the shipped trace.
