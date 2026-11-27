# Agent skills for Flagsmith experiments

Two skills in the [Agent Skills](https://agentskills.io/specification) format. Each folder is
self-contained: a `SKILL.md` and the files it names.

| Skill | What the agent does | Needs |
|---|---|---|
| [`flagsmith-experiment-rollout`](flagsmith-experiment-rollout/SKILL.md) | Plans a Flagsmith A/B experiment, records its stop rule in the experiment's hypothesis, sets it up and starts it with the Flagsmith CLI, reads results without peeking, checks guardrails, then completes it and rolls the winner (or control) out to everyone | Flagsmith CLI v2, Python 3.8+ |
| [`ulysses-stop-contract`](ulysses-stop-contract/SKILL.md) | The same job with the stop rule enforced: the owner signs it before the start and a contract service refuses any early or different ship | the above, plus a running Ulysses service |

The first one stands alone: it does not need Ulysses.

## Install

Copy a skill folder to where your agent loads skills.

Claude Code, for every project:

```sh
mkdir -p ~/.claude/skills
cp -r skills/flagsmith-experiment-rollout ~/.claude/skills/
```

Claude Code, for one repository: copy it to `.claude/skills/` in that repository. Other agents that
read Agent Skills: copy it to their skills folder (for example `.agents/skills/`). Restart the agent
so it reads the new skill; it loads `SKILL.md` when a request matches its description, and
`references/` and `scripts/` only when a step calls for them.

Install the Flagsmith CLI if `flagsmith --version` is missing or older than v2:
`curl -fsSL https://get.flagsmith.com | sh` or `brew install Flagsmith/tap/flagsmith`
(Windows: `irm https://raw.githubusercontent.com/Flagsmith/flagsmith-cli/main/install.ps1 | iex`).

## Check

```sh
uv run pytest -q tests/test_skills.py
```

It checks the frontmatter against the specification, that every `flagsmith` command and flag the
skills use exists in CLI v2, that every `ulysses` command parses, that every API path is a Flagsmith
route (and, with `FLAGSMITH_SRC` pointing at a Flagsmith checkout, that it is in the source), that the
numbers quoted match `data/skill-rule-offline.json`, and the stop rule's verdicts.
