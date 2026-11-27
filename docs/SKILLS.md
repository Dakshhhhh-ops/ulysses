# Agent skills

`skills/` holds two Agent Skills that take a coding agent through a whole Flagsmith experiment with
the Flagsmith CLI: plan, set up, start, look, decide, roll out, verify, leave notes.

- [`flagsmith-experiment-rollout`](../skills/flagsmith-experiment-rollout/SKILL.md) works on any
  Flagsmith with Experimentation. It needs only the CLI and Python's standard library.
- [`ulysses-stop-contract`](../skills/ulysses-stop-contract/SKILL.md) runs the same job under a
  signed Ulysses contract, for when "the agent promised to follow the rule" is not enough.

Install steps are in [`skills/README.md`](../skills/README.md).

## The gap they fill

Flagsmith's [set-up-with-a-coding-agent](https://docs.flagsmith.com/experimentation/set-up-with-a-coding-agent)
prompt ends once the experiment runs and the first results come back. Nothing tells the agent when
it may stop, what to do with a guardrail, or how to ship the winner. Left alone, an agent asked "ship
the winner" reads `chance_to_win`, sees it pass 95% and ships. Over 30 refreshes of an A/A test that
ships a do-nothing variation 13.4% of the time on Flagsmith's own `stats.py`.

## The stop rule, written into Flagsmith

`scripts/stop_rule.py plan` sizes the experiment from the baseline rate and the smallest lift worth
shipping, and prints a tag such as `[stop-rule v1 n=12786 early=0.9987 final=0.95 guard=0.95]`. The
agent puts it in the experiment's hypothesis when it creates the experiment, so the plan is visible in
Flagsmith's dashboard to everyone, before any data exists. `check` reads the rule back from Flagsmith
at every look and refuses to decide if the tag is missing or if the agent tries to pass a different
size.

The rule is Haybittle-Peto: before the planned size, ship only at a chance to win of 99.87% (z = 3);
at the planned size, one read at 95%. On the same 10,000 simulated A/A experiments and the same random
draws as `ulysses calibrate offline`:

| Rule | Ships a do-nothing variation | Ships a real +25% lift | Ships before the planned size (+25%) |
|---|---|---|---|
| First read over 95% | 13.4% | 91.0% | most runs |
| One read at the planned size | 3.9% | 91.0% | never |
| The skill's rule | 3.9% | 91.0% | 41.1% |

Simulated synthetic identities on Flagsmith `stats.py` v2.295.2, not user traffic. Reproduce with
`uv run python scripts/skill_rule_calibration.py --flagsmith-src <Flagsmith checkout>` (about a minute;
it calls the skill's own `check` at every read and stops if its first-95% count differs from
`data/offline.json`). It writes `data/skill-rule-offline.json`, which `tests/test_skills.py` holds the
skill's text to.

The rule keeps the false-ship rate of a single read and the same power, and still lets a clear
winner ship early. It does not stop early on a losing primary metric, because at low counts
Flagsmith's relative lift is skewed toward false losses; only a guardrail stops the run early.
Ulysses' contracts use a spending boundary with more looks and a signed alpha; this rule trades that
precision for something an agent can apply with no dependencies.

## Safety around production changes

- Every write (create, start, complete, flag change) waits for the person's yes, with the exact command shown.
- The decision comes from the recorded rule, never from the dashboard label; a HOLD while Flagsmith
  reads Winning is reported as such.
- Freshness is checked before any decision: a refresh still computing, or one that failed
  (`last_error_at`), is never read as current.
- A sample-ratio mismatch, which Flagsmith's own `srm_p_value` cannot report for experiments created
  through the API, stops the agent with nothing shipped.
- After `complete`, the agent checks what an identity is served, and knows the undo:
  `flagsmith flag update <flag> --weight <key>=0`, or `flagsmith flag disable <flag>`, for use only
  when the person asks to reverse the decision. Then the job ends; cleanup is a later code change. A
  live run showed why that line is needed: [SKILL-RUN.md](SKILL-RUN.md).
- `ulysses-stop-contract` removes the Flagsmith credential from the agent's shell and leaves signing
  and confirming to people; the contract service refuses everything else.

## Token use

Each look is three CLI calls and one line of output. The refresh call prints nothing on success, and
the experiment and results bodies go straight into `check`, which prints one JSON line (the tests hold
it under 700 characters). The raw bodies are several times that and grow with the experiment's age
(hourly timeseries); the line does not. Every other read in the skill uses `--jq` to return only the
ids and fields the next step needs. `SKILL.md` is about 9,000 characters (`wc -c`), loaded once; `references/REFERENCE.md` loads only when the
agent needs a field or an endpoint. The measured token count of a live run by
`gpt-5-mini`, call by call, is in [SKILL-RUN.md](SKILL-RUN.md).

## What the tests check

`uv run pytest -q tests/test_skills.py`:

- frontmatter: allowed keys, `name` format and folder match, description and compatibility lengths,
  body under 500 lines and about 5,000 tokens;
- every `flagsmith` command and flag used, against the CLI v2 command tree;
- every `ulysses` command in a code block, parsed by this repository's own parser, and the MCP tools named;
- every API path, against Flagsmith's routes, and against the source when `FLAGSMITH_SRC` (or the
  shared reference checkout) is present;
- the quoted figures against `data/skill-rule-offline.json`;
- the stop rule: interim Winning holds, overwhelming evidence ships with the variation id, the final
  read decides both ways, guardrail orientation for `up` and `down` metrics, freshness and lifecycle
  states, the recorded plan cannot be skipped or overridden, sample-ratio mismatch, several variations,
  sizing equal to `ulysses horizon` at one look, the chi-square tail against SciPy, and the script end
  to end on two concatenated CLI bodies.
