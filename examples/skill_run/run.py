"""One live run of the `flagsmith-experiment-rollout` Agent Skill, by a language model, through the Flagsmith CLI.

The model (OpenAI ``gpt-5-mini``, reasoning effort low, through ``examples/llm_agent/openai_model.py`` and its
spend guard) gets the skill's ``SKILL.md`` as its instructions and one tool, ``shell``. The shell
(``shell.py``) runs only ``flagsmith ...`` and ``python scripts/stop_rule.py ...`` with pipes between them; the
harness puts the Flagsmith URL and admin key in the CLI's environment and scrubs every output, so the model never
sees a key. The person's request gives the skill's planning answers; after that:

* **The owner.** Where the skill says to ask the person (before each write, before start, before complete), the
  model ends its turn with the question and the harness answers as a stand-in owner, logged ``[stand-in owner]``.
* **Traffic.** Once the model's ``start`` call succeeds, the harness starts ``traffic.py`` as a side process:
  synthetic visitors through the Flagsmith SDK, purchase 10% on control and 15% on the variation (a real effect).
  It stops when ``complete`` succeeds.
* **Wake-ups.** When the model ends a turn with nothing to ask (it is waiting for Flagsmith's next refresh, which
  is at most every five minutes), the harness sleeps ``--wake-every`` seconds (60 after a ``COMPUTING`` verdict)
  and sends one fixed message with no numbers in it. The model reads everything from Flagsmith itself.
* **Measurement.** Every model call's input, cached input and output tokens and its cost are recorded, with totals.

Every run writes the full log to ``.run/skill-run/`` (git-ignored). ``--export`` also writes the scrubbed copy
(no keys, no times or dates) to ``skills/flagsmith-experiment-rollout/examples/live-run.jsonl``.

    uv run python examples/skill_run/run.py --export
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from examples.skill_run.shell import Shell, scrub  # noqa: E402

for _stream in (sys.stdout, sys.stderr):  # a redirected Windows console is cp1252; the model writes arrows
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SKILL = ROOT / "skills" / "flagsmith-experiment-rollout"
RUN_DIR = ROOT / ".run" / "skill-run"
EXPORT = SKILL / "examples" / "live-run.jsonl"
FLAGSMITH_EXE = os.environ.get("SKILL_RUN_FLAGSMITH", str(ROOT.parents[1] / "tools" / "flagsmith-cli" / "flagsmith.exe"))
LOCK_CANDIDATES = (ROOT.parents[1] / "flagsmith-lab" / "LAB-LOCK", ROOT.parent / "_shared" / "flagsmith-lab" / "LAB-LOCK")
LOCK_LABEL = "ulysses-skill-run"

TOOL = {
    "name": "shell",
    "description": ("Run a command in the person's checkout. Only `flagsmith ...` (Flagsmith CLI v2, already "
                    "authenticated and bound to their project and environment) and `python scripts/stop_rule.py "
                    "...` run, with pipes, { a; b; }, ;, &&, NAME=value / NAME=$(...) and here-documents. "
                    "Variables persist between calls. Anything else is refused."),
    "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
}

PREFACE = """You are a coding agent working for the person in this conversation, with the Agent Skill \
`flagsmith-experiment-rollout` loaded. Its SKILL.md follows; follow it. Your one tool, `shell`, runs commands in \
the person's checkout, where `python scripts/stop_rule.py` is the skill's script. To ask the person something, end \
your turn with the question. Keep replies short.

"""

REQUEST = """Run an experiment on our checkout button in our Flagsmith environment and roll out the winner.

- What changes: a new string flag `{flag}`. Control value `{control}`; one variation, value `{variant}`, key \
`{variant}`, split 50/50 with control. Prefix anything you create with `{prefix}`.
- Success event: `purchase`. Guardrail event: `checkout_error` (must not go up).
- Control converts at about 10%. The smallest relative lift worth shipping is 40%.
- About {per_minute:g} visitors a minute reach checkout.
- The app code is already instrumented (getExperimentFlag on the checkout page, trackEvent for both events), so \
skip the code step.

Ask me before each write, as your skill says."""

OWNER_YES = "Yes, go ahead."
WAKE = "About {what} passed since your last reply. Continue the job. Keep the reply short."

_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?")
_ASKS = re.compile(r"\?|\b(approve|approval|confirm|ok to|okay to|may i|shall i|should i|go ahead|proceed|reply|say yes)\b", re.I)
_DECIDE = re.compile(r"\b(complete|roll ?out|rollout_decision|ship)\b", re.I)


def no_times(text: str) -> str:
    return _TIME.sub("<time>", text)


# ------------------------------------------------------------------------------------------------ credentials


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        k, sep, v = line.partition("=")
        k = k.strip().removeprefix("export ").strip()
        if sep and k and not k.startswith("#"):
            out[k] = v.strip().strip('"').strip("'")
    return out


def host_key_var(api_url: str) -> str:
    """The CLI's host-scoped key variable: ``http://localhost:8000`` -> ``FLAGSMITH_API_KEY_localhost_8000``."""
    u = urlparse(api_url)
    host = u.hostname or ""
    if u.port:
        host += f":{u.port}"
    return "FLAGSMITH_API_KEY_" + host.replace("-", "__").replace(".", "_").replace(":", "_")


def cli_env(api_url: str, admin_key: str, config_path: Path) -> dict[str, str]:
    """A minimal environment for the CLI child: no OpenAI key, nothing else from this process."""
    keep = ("PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA",
            "LOCALAPPDATA", "COMSPEC", "PATHEXT")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"FLAGSMITH_API_URL": api_url, host_key_var(api_url): admin_key, "FLAGSMITH_NO_INPUT": "1",
                "FLAGSMITH_CONFIG_PATH": str(config_path), "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def make_runner(env: dict[str, str], exe: str = FLAGSMITH_EXE, timeout: float = 120):
    script = SKILL / "scripts" / "stop_rule.py"

    def runner(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
        if argv[0] in ("python", "python3", "py"):
            real = [sys.executable, str(script), *argv[2:]]
        else:
            real = [exe, *argv[1:]]
        try:
            p = subprocess.run(real, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               env=env, cwd=str(SKILL), timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {timeout:.0f} s\n"
        return p.returncode, p.stdout, p.stderr

    return runner


# ------------------------------------------------------------------------------------------------ the lab lock


def lock_path() -> Path:
    if os.environ.get("SKILL_RUN_LAB_LOCK"):
        return Path(os.environ["SKILL_RUN_LAB_LOCK"])
    return next((p for p in LOCK_CANDIDATES if p.parent.is_dir()), LOCK_CANDIDATES[0])


def take_lock(path: Path, label: str = LOCK_LABEL) -> None:
    if path.exists():
        holder = path.read_text(encoding="utf-8", errors="replace").strip()
        if holder != label:
            raise SystemExit(f"the lab is locked by {holder!r} ({path}); wait for it to finish")
    path.write_text(label, encoding="utf-8")


def release_lock(path: Path, label: str = LOCK_LABEL) -> None:
    try:
        if path.read_text(encoding="utf-8", errors="replace").strip() == label:
            path.unlink()
    except OSError:
        pass


# ------------------------------------------------------------------------------------------------ accounting


@dataclass
class Tokens:
    calls: list[dict] = field(default_factory=list)

    def add(self, usage: dict | None) -> dict:
        u = usage or {}
        row = {"call": len(self.calls) + 1, "input_tokens": int(u.get("input_tokens") or 0),
               "cached_input_tokens": int(u.get("cached_input_tokens") or 0),
               "output_tokens": int(u.get("output_tokens") or 0),
               "reasoning_tokens": int(u.get("reasoning_tokens") or 0), "cost_usd": float(u.get("cost_usd") or 0.0)}
        self.calls.append(row)
        return row

    def totals(self) -> dict:
        t = {"calls": len(self.calls)}
        for k in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
            t[k] = sum(c[k] for c in self.calls)
        t["uncached_input_tokens"] = t["input_tokens"] - t["cached_input_tokens"]
        t["cost_usd"] = round(sum(c["cost_usd"] for c in self.calls), 6)
        return t


# ------------------------------------------------------------------------------------------------ the run


@dataclass
class State:
    experiment: int | None = None
    started: bool = False
    completed: bool = False
    verified: bool = False
    last_verdict: str | None = None
    verdicts: list[dict] = field(default_factory=list)


def observe(state: State, runs: list, output: str) -> list[str]:
    """Update the run state from one tool result, command by command (``runs``: (argv, returncode, stdout)).

    Each command's own exit status counts, not the chain's: ``complete && refresh`` where the refresh is
    throttled still completed. Returns the harness events triggered ('start', 'complete')."""
    events = []
    for argv, rc, out in runs:
        joined = " ".join(argv)
        m = re.search(r"experiments/(\d+)/", joined)
        if m:
            state.experiment = int(m.group(1))
        if re.search(r"experiments/\d+/start/", joined) and rc == 0 and '"running"' in out and not state.started:
            state.started = True
            events.append("start")
        if re.search(r"experiments/\d+/complete/", joined) and rc == 0 and '"completed"' in out \
                and not state.completed:
            state.completed = True
            events.append("complete")
        if state.completed and len(argv) > 1 and argv[1] in ("eval", "evaluate") and rc == 0:
            state.verified = True
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{") and '"verdict"' in line:
            try:
                v = json.loads(line)
            except ValueError:
                continue
            state.last_verdict = v.get("verdict")
            state.verdicts.append(v)
    return events


OWNER_LATER = "later"


def retry_seconds(output: str) -> int:
    """Flagsmith's 429 says "Expected available in N seconds"; 0 when the output has no such line."""
    m = re.search(r"Expected available in (\d+) seconds", output or "")
    return min(int(m.group(1)), 330) if m else 0


def owner_reply(state: State, text: str) -> str | None:
    """What the stand-in owner does when the model ends a turn.

    Before the start and after the completion every write needs the person, so any turn end gets a yes now. While
    the experiment runs, a question about completing or rolling out gets a yes now; any other question (for example
    "may I refresh the results?") gets a yes after the wake-up wait (``OWNER_LATER``), so the look lands after
    Flagsmith's five-minute window; no question means the model is waiting, and the wake-up message follows.
    """
    if not state.started or state.completed:
        return OWNER_YES
    if _ASKS.search(text or ""):
        return OWNER_YES if _DECIDE.search(text or "") else OWNER_LATER
    return None


class Log:
    def __init__(self, path: Path, secrets: list[str]):
        self.path = path
        self.secrets = secrets
        self.rows: list[dict] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = path.open("w", encoding="utf-8")

    def write(self, row: dict) -> None:
        row = json.loads(scrub(json.dumps(row, ensure_ascii=False), self.secrets))
        self.rows.append(row)
        self.fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.fh.flush()

    def export(self, path: Path, extra_secrets: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for row in self.rows:
                s = no_times(scrub(json.dumps(row, ensure_ascii=False), self.secrets + extra_secrets))
                fh.write(s + "\n")


def say(who: str, text: str) -> None:
    print(f"[{who}] {text}", flush=True)


def run(a) -> int:
    from examples.llm_agent.openai_model import OpenAIModel, SpendCapReached, load_key, secret_values, spend_summary

    env_vars = {**read_env_file(ROOT / ".env"), **{k: v for k, v in os.environ.items() if k.startswith("FLAGSMITH_")}}
    api_url = a.api_url or env_vars.get("FLAGSMITH_API_URL") or "http://localhost:8000"
    admin_key = env_vars.get("FLAGSMITH_ADMIN_KEY")
    if not admin_key:
        raise SystemExit("no FLAGSMITH_ADMIN_KEY in .env (git-ignored)")
    if not load_key():
        raise SystemExit("no OpenAI key (OPEN_AI_API_KEY in .env.local)")

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    config = RUN_DIR / "flagsmith.json"
    secrets = [admin_key, *secret_values()]
    env = cli_env(api_url, admin_key, config)
    runner = make_runner(env)

    # bind the checkout as the person's own flagsmith.json would (harness side; the model never runs init)
    config.write_text(json.dumps({"project": a.project, "apiUrl": api_url}), encoding="utf-8")
    rc, out, err = runner(["flagsmith", "env", "list", "--project", str(a.project),
                           "--jq", f'[.[] | select(.name == "{a.environment}")][0].api_key'], None)
    env_key = out.strip().strip('"')
    if rc != 0 or not env_key or env_key == "null":
        raise SystemExit(f"cannot find environment {a.environment!r} in project {a.project}: {scrub(err, secrets)}")
    config.write_text(json.dumps({"project": a.project, "environment": env_key, "apiUrl": api_url}, indent=2),
                      encoding="utf-8")
    rc, out, err = runner(["flagsmith", "environment", "key", "list", env_key,
                           "--jq", "[.[] | select(.active)][0].key"], None)
    server_key = out.strip().strip('"')
    if rc != 0 or not server_key.startswith("ser."):
        raise SystemExit("cannot read the environment's server-side key for the synthetic visitors")
    secrets.append(server_key)

    lock = lock_path()
    take_lock(lock)
    say("harness", f"took the lab lock ({lock.name}: {LOCK_LABEL})")
    log = Log(RUN_DIR / "live-run.full.jsonl", secrets)
    stop_file = RUN_DIR / "traffic.stop"
    stop_file.unlink(missing_ok=True)
    traffic: subprocess.Popen | None = None
    tokens = Tokens()
    state = State()
    shell = Shell(runner, secrets)
    model = OpenAIModel(model=a.openai_model, reasoning_effort="low")
    system = PREFACE + (SKILL / "SKILL.md").read_text(encoding="utf-8")
    request = REQUEST.format(prefix=a.prefix, flag=a.flag, control=a.control_value, variant=a.variant_value, per_minute=a.per_minute)
    messages: list[dict] = [{"role": "user", "content": request}]
    log.write({"type": "setup", "model": a.openai_model, "reasoning_effort": "low", "skill": "SKILL.md",
               "system_chars": len(system), "tool": TOOL["name"], "environment": a.environment,
               "traffic": {"synthetic": True, "per_minute": a.per_minute, "purchase_control": a.control_rate,
                           "purchase_variation": a.variant_rate, "checkout_error_both": a.error_rate}})
    log.write({"type": "user", "from": "person", "text": request})
    say("person", request)
    t_end = time.time() + a.max_minutes * 60
    outcome = "unfinished"
    asks_in_a_row = 0
    retry_after = 0  # seconds Flagsmith's last 429 said to wait; the owner's next yes waits for it
    try:
        while True:
            if len(tokens.calls) >= a.max_calls:
                outcome = f"stopped: {a.max_calls} model calls"
                break
            if time.time() > t_end:
                outcome = f"stopped: {a.max_minutes} minutes"
                break
            try:
                turn = model.next_turn(system=system, tools=[TOOL], messages=messages)
            except SpendCapReached as exc:
                outcome = f"stopped: {exc}"
                break
            row = tokens.add(turn.usage)
            calls = turn.tool_uses()
            log.write({"type": "assistant", "call": row["call"], "text": turn.text(),
                       "tool_calls": [c["input"].get("command") for c in calls], "stop_reason": turn.stop_reason,
                       "usage": {k: row[k] for k in ("input_tokens", "cached_input_tokens", "output_tokens",
                                                     "reasoning_tokens", "cost_usd")}})
            if turn.text():
                say("model", turn.text())
            messages.append({"role": "assistant", "content": turn.content})
            if tokens.totals()["cost_usd"] > a.budget:
                outcome = f"stopped: this run passed its ${a.budget:.2f} budget"
                break
            if calls:
                asks_in_a_row = 0
                results = []
                for c in calls:
                    cmd = str((c.get("input") or {}).get("command") or "")
                    say("model $", cmd)
                    res = shell.run(cmd)
                    say("tool", res.output[:1500])
                    log.write({"type": "tool", "command": cmd, "output": res.output, "exit": res.returncode,
                               **({"refused": res.refused} if res.refused else {})})
                    for ev in observe(state, res.runs, res.output):
                        if ev == "start" and traffic is None:
                            # traffic goes to the flag the experiment really uses, whatever the model named it
                            rc_f, out_f, _ = runner(["flagsmith", "api", f"api/v1/environments/{env_key}/experiments/"
                                                     f"{state.experiment}/", "--jq", ".feature.name"], None)
                            flag = out_f.strip().strip('"') if rc_f == 0 and out_f.strip() else a.flag
                            traffic = subprocess.Popen(
                                [sys.executable, "-m", "examples.skill_run.traffic", "--api-url", api_url,
                                 "--flag", flag, "--control-value", a.control_value, "--per-minute",
                                 str(a.per_minute), "--control-rate", str(a.control_rate), "--variant-rate",
                                 str(a.variant_rate), "--error-rate", str(a.error_rate), "--stop-file", str(stop_file),
                                 "--minutes", str(a.max_minutes)],
                                cwd=str(ROOT), env={**os.environ, "SKILL_RUN_SERVER_KEY": server_key},
                                stdout=(RUN_DIR / "traffic.log").open("w", encoding="utf-8"),
                                stderr=subprocess.STDOUT)
                            log.write({"type": "harness", "text": f"started the synthetic visitors on {flag} (side process)"})
                            say("harness", "started the synthetic visitors")
                        if ev == "complete" and traffic is not None:
                            stop_file.write_text("stop", encoding="utf-8")
                            log.write({"type": "harness", "text": "stopped the synthetic visitors"})
                            say("harness", "stopping the synthetic visitors")
                    retry_after = retry_seconds(res.output)
                    results.append({"type": "tool_result", "tool_use_id": c["id"], "content": res.output})
                messages.append({"role": "user", "content": results})
                continue
            # the model ended its turn
            if state.completed and state.verified:
                outcome = "done"
                break
            reply = owner_reply(state, turn.text())
            if reply:
                asks_in_a_row += 1
                if asks_in_a_row > a.max_asks:
                    outcome = (f"stopped: blocked, the model asked the stand-in owner {asks_in_a_row} times in a row "
                               "without running a command (a yes cannot answer what it is asking)")
                    break
            if reply and retry_after:
                say("harness", f"the stand-in owner waits {retry_after + 5} s, as Flagsmith's 429 asked")
                time.sleep(retry_after + 5)
                retry_after = 0
            if reply == OWNER_YES:
                log.write({"type": "user", "from": "stand-in owner", "text": reply})
                say("stand-in owner", reply)
                messages.append({"role": "user", "content": reply})
                continue
            wait = 60 if state.last_verdict in ("COMPUTING", "NO_RESULTS") else a.wake_every
            if wait <= 0:
                outcome = "ended at the model's reply (no wake-ups)"
                break
            say("harness", f"sleeping {wait} s until the next look")
            time.sleep(wait)
            msg = WAKE.format(what="a minute has" if wait < 120 else "five minutes have")
            if reply == OWNER_LATER:
                msg = f"{OWNER_YES} {msg}"
                say("stand-in owner", msg)
            log.write({"type": "user", "from": "stand-in owner" if reply == OWNER_LATER else "harness wake-up",
                       "text": msg})
            messages.append({"role": "user", "content": msg})
    finally:
        if traffic is not None:
            stop_file.write_text("stop", encoding="utf-8")
            try:
                traffic.wait(timeout=60)
            except subprocess.TimeoutExpired:
                traffic.kill()
        release_lock(lock)
        say("harness", "released the lab lock")
    totals = tokens.totals()
    log.write({"type": "totals", "outcome": outcome, "experiment": state.experiment, "tokens": totals,
               "per_call": tokens.calls, "verdicts": [v.get("verdict") for v in state.verdicts]})
    (RUN_DIR / "tokens.json").write_text(json.dumps({"totals": totals, "per_call": tokens.calls}, indent=2),
                                         encoding="utf-8")
    say("harness", f"outcome: {outcome}; experiment {state.experiment}; tokens {totals}")
    say("harness", spend_summary())
    if a.export:
        log.export(EXPORT, [env_key])
        say("harness", f"wrote the scrubbed transcript to {EXPORT.relative_to(ROOT)}")
    return 0 if outcome == "done" else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api-url")
    ap.add_argument("--project", type=int, default=3)
    ap.add_argument("--environment", default="ulysses-demo", help="environment name in the project")
    ap.add_argument("--flag", default="ulysses_skillrun_cta")
    ap.add_argument("--prefix", default="ulysses_skillrun_", help="the prefix the person asks for on new objects")
    ap.add_argument("--control-value", default="classic")
    ap.add_argument("--variant-value", default="bold")
    ap.add_argument("--per-minute", type=float, default=100)
    ap.add_argument("--control-rate", type=float, default=0.10)
    ap.add_argument("--variant-rate", type=float, default=0.15)
    ap.add_argument("--error-rate", type=float, default=0.02)
    ap.add_argument("--openai-model", default="gpt-5-mini")
    ap.add_argument("--wake-every", type=int, default=300)
    ap.add_argument("--max-calls", type=int, default=60)
    ap.add_argument("--max-asks", type=int, default=3, help="stop when the model asks this many times in a row")
    ap.add_argument("--max-minutes", type=float, default=80)
    ap.add_argument("--budget", type=float, default=0.30, help="stop once this run's model cost passes this (USD)")
    ap.add_argument("--export", action="store_true")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
