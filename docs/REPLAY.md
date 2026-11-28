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
| `attempt` | `call` (`start_under_contract` or `execute`), `hash`, `action`, `expect: "refused"`, `recorded_reason` | repeats an agent call the service refused; it must be refused again, and the reason the service logs for that contract must equal `recorded_reason` (an unknown contract or any other error does not count). Recorded reasons it repeats: `no valid owner signature` (start before signing), `asked for X, contract says Y` (wrong action after a fire) and `no fired outcome authorises X` (execute before any look fired, for example the scripted agent shipping on Flagsmith's "Winning" reading while the contract held; the replay labels these and counts them in its summary) and `waiting for the owner's one-tap confirmation` (execute of a fired action before the owner's tap; the trace names the action of the proposal that was waiting) |
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

## The signed-autonomy recording

`data/replay-autonomy.jsonl` is one live run on the shared self-hosted lab (open-source Flagsmith with
Experimentation on) in which both contracts carry the signed clause `[autonomy] without_confirmation =
["keep_control"]`. It was made with the contract service, `scripts/human_stand_in.py` and
`uv run python scripts/agent_session.py --with-traffic --autonomy`, then written with `scripts/export_replay.py`.
Visitors were simulated by `examples/shop/visitors.py` (synthetic identities). Every number in it is what
Flagsmith returned in that run.

- A/A (experiment 388): the contract held for four looks and fired a loss at look 5, at the horizon
  (Flagsmith chance to win 1.30% against the signed 1.94%). That is a false loss in an A/A, and its
  pre-signed action keeps control. The agent asked to ship instead and was refused. It then ran
  `keep_control` itself, with no tap, under the clause the owner signed (`authorised_by signed_autonomy`).
- Real effect (experiment 387): Flagsmith read "Winning" at look 3 (96.73%) while the contract held, and the
  agent's `ship_treatment` was refused. The contract fired a win at look 4 (98.95% against 98.76%). The clause
  never covers shipping, so the agent's execute before the tap was refused, and so was its `keep_control`.
  Bold shipped only after the owner's signed tap.

    uv run python scripts/judge_demo.py --replay data/replay-autonomy.jsonl --no-hold

The summary lines it prints:

```
[REPLAY 13.8x] 11 of 11 recorded decisions recomputed identically by this checkout's engine; 0 event(s) did not reproduce
[REPLAY 13.8x] 5 of 5 recorded refusals refused again for the recorded reason (1 on Flagsmith's 'Winning' reading, 95% or more, while the contract held)
```

and, for the A/A experiment:

```
[REPLAY 13.8x] recorded + 27.5 min  contract fired: loss -> pre-signed action keep_control is authorised in advance by the signed autonomy clause (no tap)
[REPLAY 13.8x] recorded + 27.5 min  agent asks to execute ship_treatment -> refused: the contract authorises 'keep_control' for this outcome, not 'ship_treatment'
[REPLAY 13.8x] recorded + 27.5 min  agent executes keep_control under the signed autonomy clause, no tap -> flag ulysses_headline_aa serves control
```

The two experiments' verification bundles are `data/verify-autonomy-aa.json` (the autonomous keep control: its
`outcome ... autonomy` line passes in place of a confirmation line) and `data/verify-autonomy-cta.json` (the
tapped ship). `uv run ulysses verify --bundle data/verify-autonomy-aa.json` ends `CONSISTENT (unpinned): 21
checks passed` (exit 2, nothing outside the file pins it); with `--check-flagsmith`, run while that lab was up,
both ended `VERIFIED: all 23 checks passed`.

## The language-model recording

`data/replay-llm-openai.jsonl` is one live run on the shared self-hosted lab in which a language model, not the
scripted agent, was the agent: `gpt-5-mini` through `uv run python examples/llm_agent/run.py live --model openai
--with-traffic --export --wake-every 300`, with the contract service and `scripts/human_stand_in.py` running,
then written with `scripts/export_replay.py`. The model drafted both contracts, started both experiments and
took every look through the MCP tools; its transcript is `examples/llm_agent/transcripts/live.openai.jsonl`.
Visitors were simulated by `examples/shop/visitors.py` (synthetic identities). Every number in it is what
Flagsmith returned in that run. The write-up is [LLM-AGENT.md](LLM-AGENT.md).

- Real effect (experiment 391): Flagsmith read "Winning" at look 1 (98.67% against the signed 99.9998%) and
  look 2 (99.70% against 99.94%) while the contract held. The model did not ask to ship, so the trace has no
  refused attempts. The contract fired a win at look 3 (99.93% against 99.49%) and `bold` shipped after the
  owner's signed tap.
- A/A (experiment 392): held at looks 1 to 3 and reached its horizon at look 4 (17.44%), keep control, after
  the owner's tap.

    uv run python scripts/judge_demo.py --replay data/replay-llm-openai.jsonl --no-hold

The summary lines it prints:

```
[REPLAY 27.6x] 9 of 9 recorded decisions recomputed identically by this checkout's engine; 0 event(s) did not reproduce
[REPLAY 27.6x] 0 of 0 recorded refusals refused again for the recorded reason (0 on Flagsmith's 'Winning' reading, 95% or more, while the contract held)
```

The verification bundles are `data/verify-llm-openai-cta.json` (`CONSISTENT (unpinned): 20 checks passed`) and
`data/verify-llm-openai-aa.json` (`CONSISTENT (unpinned): 21 checks passed`); with `--check-flagsmith`, run while
that lab was up, they ended `VERIFIED: all 22 checks passed` and `VERIFIED: all 23 checks passed`.

## The Flagsmith Cloud recording

`data/replay-cloud.jsonl` is the one-command demo run once on Flagsmith Cloud (free plan, an existing organisation) with `scripts/judge_demo.py --flagsmith https://api.flagsmith.com --org-key-env ... --organisation ...`, then written with `scripts/export_replay.py`. Real effect (experiment 1696): Flagsmith read Winning at look 2 (98.67% against the signed 99.95%), the agent's ship was refused, and the contract fired a win at look 3 (99.77% against 99.52%). A/A (experiment 1697): held for four looks and reached its horizon at look 5 (55.60%), keep control. Both actions ran after the owner's signed tap. Its bundles are `data/verify-cloud-cta.json` and `data/verify-cloud-aa.json`. The write-up, including what was not exercised on Cloud: [CLOUD.md](CLOUD.md).

    uv run python scripts/judge_demo.py --replay data/replay-cloud.jsonl --no-hold

```
10 of 10 recorded decisions recomputed identically by this checkout's engine; 0 event(s) did not reproduce
6 of 6 recorded refusals refused again for the recorded reason (1 on Flagsmith's 'Winning' reading, 95% or more, while the contract held)
```

`tests/test_replay.py` exports a run made over the test double of Flagsmith, replays it with no
difference, shows that an edited Flagsmith number or a signature by the wrong key does not reproduce, and
replays the shipped traces, including the signed-autonomy, language-model and Flagsmith Cloud ones.
