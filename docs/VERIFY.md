# Verifying a decision

`ulysses verify` lets someone who does not trust the contract service check a decision for themselves: that the contract on the experiment is the one that was signed, that the right people signed it, and that the rule fired at the look where the variation was shipped, using the numbers Flagsmith returned.

```sh
uv run ulysses verify <experiment id> [--db .run/ulysses.db] [--environment <key>]   # live
uv run ulysses export-bundle <experiment id> --out bundle.json                       # write a bundle
uv run ulysses verify --bundle bundle.json --expect-hash sha256:...                  # offline, pinned by hash
uv run ulysses verify --bundle bundle.json --trust-key maya=ed25519:... --trust-key sam=ed25519:...
uv run ulysses verify --bundle bundle.json --check-flagsmith                         # re-read Flagsmith
```

The live form reads the experiment's hypothesis and final state from Flagsmith at the moment you run it (it needs a Flagsmith key that can read the experiment, `FLAGSMITH_API_URL` and `FLAGSMITH_ADMIN_KEY`). The contract text, signatures and looks come from the service's store. It does not trust any of them: every hash, signature and decision is worked out again by the verifier, and the hash Flagsmith's hypothesis names is what ties the rest to the real experiment.

`--bundle` runs the same checks on a file and needs no service and no Flagsmith. In that mode every source is the exporter's, including the lines about Flagsmith and the key registry. Those Flagsmith lines are named `... (as exported, not read from Flagsmith)`, and the key line says "the bundle's registry", not "the service registered". Someone can build a bundle end to end with fresh keys that agrees with itself on every line. So a bundle comes out `VERIFIED` only when something from outside it pins it:

- `--expect-hash sha256:...`: the contract hash you read on the experiment in Flagsmith yourself. The bundle's verified chain must hold it, and the signers' keys then come from that contract's text;
- `--trust-key NAME=KEY` for every signer the contracts name, with keys you got some other way;
- `--check-flagsmith`: the verifier reads the hypothesis and final state from Flagsmith now, compares them with the bundle's copy (`exported Flagsmith state`) and uses what it read.

Without one of these the result is `CONSISTENT (unpinned)` and the exit status is 2.

## What it checks

Each check prints one line, `PASS`, `FAIL` or `SKIP`, with its reason.

| Check | How |
|---|---|
| `bundle format` | The version, and the shape of every section (types of contracts, looks, seq, recorded decisions, outcomes and Flagsmith's state; no two looks with one seq). A malformed bundle fails on this line instead of crashing the verifier. |
| `no timestamps` | No value looks like a date (Y-M-D, Y/M/D, D/M/Y, a month name), a clock time or a Unix time (a number in the seconds or milliseconds range, or a decimal one in a string), and no key is named as a time (`as_of`, `time`, `timestamp`, `date`, `..._at`). |
| `contract N hash` | The contract's TOML text is parsed and validated (including the signed boundary table) and hashed again (SHA-256 of the canonical JSON, `canonical.py`). The result must equal the hash the bundle claims. |
| `contract <hash> owner signature`, `... second signer signature` | Ed25519 over `ulysses/v1/sign-contract\n<hash>\n<role>`, checked against the public key the contract itself names for that role. An amendment needs both. |
| `contract <hash> chain` | Each later contract is an amendment whose `supersedes` is the contract before it, with the same two signers. |
| `public key <name>` | The keys named across the chain agree, the key in the registry (the service's in live mode, the bundle's in bundle mode) is the same, and, with `--trust-key`, the key you supplied is the same. |
| `hypothesis in Flagsmith` | Flagsmith's hypothesis names exactly one governing contract (`Governed by stop contract sha256:...`), every amendment after it is recorded, and every contract hash it names matches contract text in the bundle. |
| `trust anchor` | What ties the result to the real experiment: the hypothesis read from Flagsmith just now, the hash you pinned, or a trusted key for every signer. `SKIP` when there is none. |
| `deviations recorded`, `deviations in Flagsmith` | Any change outside the contract that the service recorded, or that is written on the experiment, fails the audit. |
| `look N (<id>)` | The decision is worked out again from Flagsmith's numbers for that look (identities per arm, chance to win, each guardrail's chance it got worse) with the signed rule: `decision.evaluate`, which recomputes the boundary from the contract's horizon, looks and alpha, using the earlier looks that the recomputation itself counted. Outcome, counted or not, look number, boundary, action and each guardrail's state must match what was recorded. A counted look after one that fired under the same contract fails: the service takes no look once a contract has fired. |
| `look N (<id>) outcome` | The first look that fires under a contract must have its outcome in the bundle, in any state (a voided one too). Dropping the outcome to present the experiment as still open fails here. |
| `outcome <id> fired` | The executed outcome comes from a look whose recomputed outcome is that outcome, the first one to fire under that contract; or from a doubly signed amendment that authorises an action now. |
| `outcome <id> action` | The action is the one the contract assigns to that outcome. |
| `outcome <id> confirmation` | The owner's tap, checked against the owner's key in the contract. For a look's outcome the card asks the owner to sign `ulysses/v1/confirm-action\n<hash>\n<look id>\n<action>\nevidence <chain>`. `<chain>` is a hash chain over every look on the experiment up to and including the one that fired: each link is the SHA-256 of `[previous link, seq, look id, contract hash, SHA-256 of Flagsmith's numbers for that look without as_of]` (`signing.evidence_hash`). An amendment's immediate action is signed without the evidence line. |
| `outcome <id> evidence` | `PASS` when the tap covers the looks exactly as the bundle holds them. Editing any of those looks' numbers, or dropping one, after the owner signed breaks the signature and fails the confirmation line. `SKIP` when the tap was signed in the earlier form, without the evidence line, which the service still accepts. |
| `outcome <id> shipped` | The variation the outcome records serving is the one the action ships (the treatment, or control). |
| `final state in Flagsmith` | Flagsmith shows the experiment completed with a rollout decision that serves that same variation, and shows no completion if nothing was executed. |
| `outcome line in Flagsmith` | The outcome line the service wrote on the experiment matches the verified outcome. |

Exit status: 0 when every check passed (`VERIFIED`), 1 when any failed (`FAILED`), 2 when none failed but the bundle is not pinned (`CONSISTENT (unpinned)`) or some check could not run (`INCOMPLETE`, a `SKIP`, for example a bundle exported without Flagsmith's state). Only a 0 means verified. Anyone can drop a section, so the verifier never reads a missing section as a pass. `ulysses verify <id> --bundle <path>` refuses an id that is not the bundle's experiment.

## What it cannot prove

- From the numbers alone, that a look's numbers were not rewritten. An edit is caught when it makes a recorded decision disagree with the bundle's own numbers, or when it points an outcome at a look that did not fire. A consistent rewrite of a look's numbers (a hold rewritten to a win included), or a deleted look, does not show on any look line. It is caught only through the owner's tap. A tap in the current form covers every look up to the one that fired, so any such edit fails the confirmation line. A tap in the earlier form does not cover them, and its `evidence` line is `SKIP`.
- That the numbers are the ones Flagsmith returned at the time. Flagsmith keeps only its latest results, so no one can fetch the old ones afterwards. The evidence chain proves only that they are the numbers the owner signed over, which are the numbers the service showed on the card.
- In bundle mode, anything about Flagsmith. The `... (as exported, not read from Flagsmith)` lines check the exporter's copy of Flagsmith's hypothesis and final state against the rest of the bundle, not against Flagsmith. The key registry is the exporter's too. A bundle forged end to end with fresh keys, a new contract hash and matching Flagsmith lines passes every one of those checks. It ends `CONSISTENT (unpinned)`, exit 2, and fails as soon as it is pinned: its hash is not the one on the real experiment, and its keys are not the real signers'. `--check-flagsmith` replaces the exporter's copy with what Flagsmith returns now.
- That a public key belongs to the person named. The contract binds a name to a key; pass `--trust-key name=key` with a key you got some other way (from the signer, or from an earlier contract you trust) to bind it.
- Anything about who held the Flagsmith key. A dashboard admin can still change an experiment; that shows up as a deviation or as a final state that does not match, not as something the verifier can prevent.

## The bundle

`export-bundle` writes one JSON file:

- `contracts`: each contract in the experiment's chain as its TOML text, the hash it claims, and its signatures (role, signer, base64 signature);
- `public_keys`: the public keys the service registered for the signers those contracts name;
- `looks`: each look's id, sequence, contract, Flagsmith's numbers (identities per variation; per metric its chance to win, lift and direction; the SRM p-value) and the decision the service recorded;
- `outcomes`: each proposal with its outcome, action, state, the owner's confirmation signature and the recorded result (Flagsmith's `complete` request and response status, and what the flag serves);
- `deviations`, `traffic` (where the visitors came from, if the run recorded it, for example "synthetic"), and `flagsmith_final`: status, hypothesis, rollout decision and variant, and the flag's variations, read from Flagsmith at export.

It holds no timestamps: Flagsmith's `as_of` is dropped and the store's times are never copied. `export-bundle` refuses to write a bundle in which the `no timestamps` check finds anything, or that holds a PEM private key or the value of `FLAGSMITH_ADMIN_KEY`, `ULYSSES_AGENT_TOKEN` or `ULYSSES_WEBHOOK_SECRET`. The only keys in it are the signers' public keys. The contract does name the Flagsmith environment it governs by its environment key, which is Flagsmith's client-side key, not an API key. It is part of the signed text and cannot be taken out without breaking the hash.

If Flagsmith cannot be read at export, `--no-flagsmith "<reason>"` writes the bundle without `flagsmith_final` and records the reason. Those checks then come out as `SKIP`.

## Example: the one-command demo's real-effect experiment

`data/verify-judge-demo-cta.json` was exported from the store of a clean run of `uv run python scripts/judge_demo.py` (the run described in the README's "The demo run"). The visitors were synthetic, from `examples/shop/visitors.py`. That run's Flagsmith was removed afterwards with `docker compose -p ulysses down -v`, so the bundle carries no Flagsmith state and the two checks that need it are skipped. The owner's tap in that run was signed in the earlier form, so its evidence line is skipped too. Nothing outside the file pins it, so it ends `CONSISTENT (unpinned)`:

```
uv run ulysses export-bundle 1 --db <run>/.run/judge.db --out data/verify-judge-demo-cta.json \
    --no-flagsmith "the Flagsmith instance the one-command demo started was removed after the run (docker compose -p ulysses down -v)"
uv run ulysses verify --bundle data/verify-judge-demo-cta.json
```

```
Verifying bundle data/verify-judge-demo-cta.json (offline: no service, no Flagsmith; every source is the exporter's)
Traffic: synthetic (visitors simulated by examples/shop/visitors.py; Flagsmith assigns the variations)
PASS  bundle format: version 1, experiment 1
PASS  contract 1 hash: the contract text hashes to sha256:5b93c4f74a0e5d1be591846051080751d2ca00e271f29582bf91c991780bfafd
PASS  contract 5b93c4f74a0e owner signature: maya's signature verifies against ed25519:wb/cYzPi5X4UDC/p3j5m+S0pFsnC7srJxixJzb0IxfE=, the key in the contract
PASS  public key maya: the contract names ed25519:wb/cYzPi5X4UDC/p3j5m+S0pFsnC7srJxixJzb0IxfE= and the bundle's registry lists the same key
PASS  public key sam: the contract names ed25519:oEaTwZ44SVMY49DOMALF9r2OLI5aGuBVzUZYlARRC8g= and the bundle's registry lists the same key
SKIP  hypothesis in Flagsmith (as exported, not read from Flagsmith): the bundle carries no Flagsmith state (the Flagsmith instance the one-command demo started was removed after the run (docker compose -p ulysses down -v))
SKIP  trust anchor: nothing outside the bundle pins it: its contract text, keys, registry and Flagsmith lines all come from the exporter, who could build a self-consistent bundle with fresh keys. Pin it with --expect-hash <the hash on the experiment in Flagsmith>, --trust-key NAME=KEY for every signer, or --check-flagsmith
PASS  deviations recorded: none
PASS  look 1 (L0001-f4f24e3c): between looks at 0 per arm (next analysis at 37); nothing could fire
PASS  look 2 (L0002-f6461d71): look 1 of 30 at 224 per arm: Flagsmith chance to win 95.21% against the signed boundary 99.9999% (z 4.8029) -> hold; guardrail ulysses_refund_request 75.45% chance worse against 99.70%: pass
PASS  look 3 (L0003-02091991): look 2 of 30 at 450 per arm: Flagsmith chance to win 95.91% against the signed boundary 99.95% (z 3.2954) -> hold; guardrail ulysses_refund_request 28.95% chance worse against 99.70%: pass
PASS  look 4 (L0004-0ca5a6a5): look 3 of 30 at 674 per arm: Flagsmith chance to win 97.48% against the signed boundary 99.58% (z 2.6322) -> hold; guardrail ulysses_refund_request 56.79% chance worse against 99.70%: pass
PASS  look 5 (L0005-03c7ac3c): look 4 of 30 at 913 per arm: Flagsmith chance to win 99.65% against the signed boundary 98.70% (z 2.2253) -> win; guardrail ulysses_refund_request 57.01% chance worse against 99.69%: pass
PASS  look 5 (L0005-03c7ac3c) outcome: fired win; outcome P-d037c520ed05 (executed) records it
PASS  outcome P-d037c520ed05 fired: look L0005-03c7ac3c fired win under the signed rule
PASS  outcome P-d037c520ed05 action: ship_treatment is the action the contract assigns to win
PASS  outcome P-d037c520ed05 confirmation: maya's signed tap verifies for ship_treatment
SKIP  outcome P-d037c520ed05 evidence: the tap was signed in the earlier form, which names the look and the action but not the looks' numbers; a consistent rewrite of those numbers, or a dropped look, would not show here
PASS  outcome P-d037c520ed05 shipped: ship_treatment ships 'bold' on flag ulysses_checkout_cta
SKIP  final state in Flagsmith (as exported, not read from Flagsmith): the bundle carries no Flagsmith state
CONSISTENT (unpinned): 16 checks passed, 3 more could not run; nothing outside the bundle ties it to the real experiment (see the trust anchor line)
```

The command exits 2. Three looks held although Flagsmith's chance to win was over 95% at each of them, and the fourth fired. The verifier gets the same result from the contract text and Flagsmith's numbers alone. Pinned with `--expect-hash sha256:5b93c4f74a0e5d1be591846051080751d2ca00e271f29582bf91c991780bfafd`, the trust anchor passes and the result is `INCOMPLETE: 17 checks passed, 3 could not run on this bundle`, still exit 2, for the two Flagsmith lines and the evidence line. Pinning only means something when the hash comes from outside the file. Here it was copied from the bundle's own `contract 1 hash` line, because that Flagsmith instance is gone, so this run shows how the flag works, not an audit.

Try a tampered copy: in look `L0005-03c7ac3c`, change both copies of `"chance_to_win": 0.9964997314012384` (Flagsmith's number and the recorded decision) to `0.98`, and run the verifier again. That look now fails ("outcome recorded 'win', the signed rule gives 'hold'"), so does the outcome line ("under the signed rule look L0005-03c7ac3c gives 'hold'"), and the command exits 1 with `FAILED: 2 of 19 checks failed`. Changing only one of the two copies fails that look on the mismatch instead. `tests/test_verify.py` covers each kind of tampering: an edited contract, a contract edited with its hash rewritten, a swapped owner or confirmation signature, an edited look number, a consistent rewrite of a held look and a dropped look (both caught by the owner's tap), an outcome pointing at a look that held, a fired look whose outcome was dropped, a counted look after the fire, a shipped variation the rule did not fire (in the outcome and in Flagsmith's final state), an action the contract does not assign, a hypothesis hash that matches no contract, a missing second signature on an amendment, a mismatched trusted key, a self-consistent forgery with fresh keys, a deviation, malformed bundles, and timestamps in several forms.

## Live, against Flagsmith

`scripts/verify_live_run.py` runs one short governed experiment on a self-hosted Flagsmith (synthetic identities with a planted effect, a two-look design far below the service's policy, signed with an override reason that says so). The owner signs the text the card shows, evidence line included. It then runs `ulysses export-bundle`, `ulysses verify <id>` against Flagsmith and `ulysses verify --bundle` on the result.

One run on the shared self-hosted lab (Flagsmith 2.295.2, `uv run python scripts/verify_live_run.py`), made with the verifier as it was before the trust anchor, evidence and outcome-presence checks were added: experiment 323. The SDK sent 71 identities to control and 69 to bold. At look 1 of 2 Flagsmith's chance to win was 99.82% against a signed boundary of 99.30% (z 2.4589), so the contract fired a win, and the owner's tap shipped bold. Both `ulysses verify 323` (live) and `ulysses verify --bundle` ended `VERIFIED: all 14 checks passed` and exited 0 under that earlier verifier. These included the hypothesis Flagsmith holds naming the contract's hash, and Flagsmith's final state (`completed`, `rollout_decision 'variant'`, serving `bold`). The identities and the effect were synthetic. That run has not been repeated with the current verifier, whose check count and bundle result (unpinned unless `--expect-hash` is given) differ. That bundle is not shipped, because its contract names the environment key of a lab instance that is still running.
