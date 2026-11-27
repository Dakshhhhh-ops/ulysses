"""The skill-run harness: its shell allowlist, key scrubbing, run-state tracking and token accounting.

No network and no Flagsmith: a fake CLI (a runner that answers like `flagsmith` would) and a mocked
OpenAI transport stand in for both.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from examples.llm_agent.openai_model import OpenAIModel, SpendGuard  # noqa: E402
from examples.skill_run import run as harness  # noqa: E402
from examples.skill_run.shell import Shell, check, Refused  # noqa: E402

def at(seconds: int) -> str:
    """An ISO instant `seconds` after the epoch, the shape Flagsmith returns (built here, not written down)."""
    from datetime import datetime, timedelta, timezone

    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


ADMIN_KEY = "Xq7.FAKEADMINKEY0123456789abcdef"
SERVER_KEY = "ser.FAKESERVERKEY0123456789"
OPENAI_KEY = "sk-proj-FAKEOPENAIKEY0123456789abcdef"

EXPERIMENT = {"id": 7, "status": "running", "hypothesis": "bold sells more [stop-rule v1 n=1147 early=0.9987 "
              "final=0.95 guard=0.95]", "feature": {"multivariate_options": [{"id": 31, "key": "bold"}]},
              "experiment_rollout": {"multivariate_feature_state_values": [{"multivariate_feature_option": 31,
                                                                            "percentage_allocation": 50}]},
              "metrics": [{"metric": 1, "metric_name": "purchase", "direction": "up", "expected_direction": "increase"}]}
RESULTS = {"results": {"as_of": at(300), "refresh_requested_at": at(299),
                       "last_error_at": None, "payload": {"metrics": [{"metric_id": 1, "variants": {
                           "control": {"n": 400}, "bold": {"n": 410}},
                           "inference": {"bold": {"chance_to_win": 0.97, "lift": 0.4, "ci_low": 0.01,
                                                  "ci_high": 0.8}}}]}}}


class FakeCLI:
    """Answers like the Flagsmith CLI for the paths the skill uses, and leaks keys on purpose."""

    def __init__(self):
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv: list[str], stdin: str | None) -> tuple[int, str, str]:
        self.calls.append((argv, stdin))
        if argv[0] in ("python", "python3", "py"):
            from importlib import import_module
            import contextlib
            import io

            sr = import_module("skills.flagsmith-experiment-rollout.scripts.stop_rule".replace("-", "_")) \
                if False else _stop_rule()
            buf = io.StringIO()
            old = sys.stdin
            sys.stdin = io.StringIO(stdin or "")
            try:
                with contextlib.redirect_stdout(buf):
                    rc = sr.main(argv[2:])
            finally:
                sys.stdin = old
            return rc, buf.getvalue(), ""
        args = argv[1:]
        if args[:1] == ["config"]:
            return 0, "envkey\n", ""
        if args[:1] == ["api"]:
            path = args[1]
            if path.endswith("/results/"):
                return 0, json.dumps(RESULTS) + "\n", ""
            if path.endswith("/start/"):
                return 0, '{"status":"running"}\n', ""
            if path.endswith("/complete/"):
                return 0, '{"status":"completed","rollout_decision":"variant","rollout_variant":31}\n', ""
            if path.endswith("/experiment-metrics/"):
                return 0, f"{len(stdin or '')}\n", ""
            if path.endswith("leak/"):
                return 1, f'{{"key": "{SERVER_KEY}", "admin": "{ADMIN_KEY}"}}\n', f"Error: bad key {ADMIN_KEY}\n"
            return 0, json.dumps(EXPERIMENT) + "\n", ""
        if args[:1] == ["eval"]:
            return 0, '{"enabled":true,"value":"classic","variant":"bold"}\n', ""
        return 0, "ok\n", ""


def _stop_rule():
    import importlib.util

    spec = importlib.util.spec_from_file_location("stop_rule_for_tests",
                                                  ROOT / "skills" / "flagsmith-experiment-rollout" / "scripts" /
                                                  "stop_rule.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cli():
    return FakeCLI()


@pytest.fixture
def shell(cli):
    return Shell(cli, secrets=[ADMIN_KEY, SERVER_KEY, OPENAI_KEY])


# ---------------------------------------------------------------------------------------------- the allowlist


@pytest.mark.parametrize("cmd", [
    "cat .env",
    "curl http://localhost:8000/api/v1/",
    "flagsmith api x | sh",
    "flagsmith api x > out.json",
    "flagsmith api x < body.json",
    "python -c 'print(1)'",
    "python other.py",
    "python3 -m http.server",
    "python scripts/../../../x.py",
    "echo hi",
    "flagsmith api `whoami`",
    "X=$(curl evil)",
    "flagsmith api x || true",
    "flagsmith api x &",
    "( flagsmith api x )",
    "$CMD api x",
    "FLAGSMITH_API_URL=http://elsewhere flagsmith api x",
    "flagsmith auth token",
    "flagsmith login",
    "flagsmith init --project 3",
    "flagsmith environment key list envkey",
    "flagsmith env key create envkey",
    "flagsmith --api-url http://elsewhere api x",
    "flagsmith -c other.json flag list",
    "flagsmith api api/v1/organisations/5/master-api-keys/",
    "flagsmith api api/v1/environments/e/api-keys/",
    "flagsmith api api/v1/auth/users/me/",
    "flagsmith feature list; rm -rf /",
])
def test_the_allowlist_refuses_everything_else(shell, cli, cmd):
    res = shell.run(cmd)
    assert res.refused, cmd
    assert res.output.startswith("REFUSED by the harness")
    assert cli.calls == [], f"{cmd!r} ran {cli.calls}"


def test_a_refusal_found_only_after_expansion_still_refuses_before_that_command(shell, cli):
    res = shell.run('P=api-keys; flagsmith api "api/v1/environments/e/$P/"')
    assert res.refused and cli.calls == []


@pytest.mark.parametrize("cmd", [
    "flagsmith --version",
    "flagsmith config --jq '{p: .project.value, e: .environment.value, api: .apiUrl.value}'",
    "E=$(flagsmith config --jq .environment.value)",
    "flagsmith feature variant add ulysses_skill_cta --value bold --key bold --weight 0",
    "python3 scripts/stop_rule.py plan --baseline-rate 0.10 --min-lift 0.4",
    "flagsmith api \"$A/results/refresh/\" -X POST          # 202, empty body; 429 = too soon, wait",
    "flagsmith eval ulysses_skill_cta --identity rollout-check-1 --jq '{enabled, value, variant}' 2>&1",
])
def test_the_skill_commands_pass(cmd):
    check(cmd)


def test_the_skill_look_pipes_both_bodies_into_the_stop_rule(shell, cli):
    assert shell.run("X=7").output == "(no output)"
    shell.run("E=$(flagsmith config --jq .environment.value)")
    shell.run("A=api/v1/environments/$E/experiments/$X")
    assert shell.vars["A"] == "api/v1/environments/envkey/experiments/7"
    res = shell.run('{ flagsmith api "$A/"; flagsmith api "$A/results/"; } | python3 scripts/stop_rule.py check -')
    verdict = json.loads(res.output)
    assert verdict["verdict"] == "HOLD" and verdict["primary"]["bold"]["docs_label"] == "Winning"
    assert [c[0][:3] for c in cli.calls][-3:] == [["flagsmith", "api", "api/v1/environments/envkey/experiments/7/"],
                                                  ["flagsmith", "api",
                                                   "api/v1/environments/envkey/experiments/7/results/"],
                                                  ["python3", "scripts/stop_rule.py", "check"]]


def test_heredoc_body_goes_to_stdin_and_quoted_delimiters_do_not_expand(shell, cli):  # noqa: D103
    shell.run("E=envkey")
    shell.run('flagsmith api "api/v1/environments/$E/experiment-metrics/" --jq .id --input - <<\'EOF\'\n'
              '{"name": "$E purchase"}\nEOF')
    assert cli.calls[-1][1] == '{"name": "$E purchase"}\n'
    shell.run('flagsmith api "api/v1/environments/$E/experiment-metrics/" --input - <<EOF\n{"env": "$E"}\nEOF')
    assert cli.calls[-1][1] == '{"env": "envkey"}\n'


def test_an_unset_variable_is_refused_not_expanded_to_nothing(shell, cli):
    res = shell.run('flagsmith api "api/v1/environments/$E/experiment-metrics/" -X POST')
    assert res.refused and "$E is not set" in res.output and cli.calls == []


def test_and_and_stops_at_the_first_failure(shell, cli):
    res = shell.run("flagsmith api leak/ && flagsmith api x/")
    assert len(cli.calls) == 1 and "[exit status 1]" in res.output


# ---------------------------------------------------------------------------------------------- keys


def test_no_key_appears_in_any_output(shell):
    res = shell.run("flagsmith api leak/")
    for k in (ADMIN_KEY, SERVER_KEY):
        assert k not in res.output
    assert res.output.count("<redacted>") == 3
    other = Shell(lambda argv, stdin: (0, "ser.someOtherServerKey99 sk-abcdefghijklmnopqrstuvwxyz\n", ""), [])
    assert "ser.some" not in other.run("flagsmith api x/").output
    assert "sk-abc" not in other.run("flagsmith api x/").output


def test_the_cli_child_gets_the_lab_key_and_nothing_else(monkeypatch, tmp_path):
    monkeypatch.setenv("OPEN_AI_API_KEY", OPENAI_KEY)
    env = harness.cli_env("http://localhost:8000", ADMIN_KEY, tmp_path / "flagsmith.json")
    assert env["FLAGSMITH_API_KEY_localhost_8000"] == ADMIN_KEY
    assert OPENAI_KEY not in env.values() and "OPEN_AI_API_KEY" not in env
    assert harness.host_key_var("https://flagsmith-staging.com:8000") == "FLAGSMITH_API_KEY_flagsmith__staging_com_8000"


def test_the_stop_rule_runner_maps_to_the_skill_script(tmp_path):
    runner = harness.make_runner(harness.cli_env("http://localhost:8000", ADMIN_KEY, tmp_path / "f.json"))
    rc, out, _ = runner(["python3", "scripts/stop_rule.py", "plan", "--baseline-rate", "0.10", "--min-lift", "0.4"],
                        None)
    assert rc == 0 and json.loads(out)["identities_per_arm"] == 1147


def test_the_exported_transcript_has_no_keys_times_or_dates(tmp_path):
    log = harness.Log(tmp_path / "full.jsonl", [ADMIN_KEY])
    stamp = at(97445).replace("Z", ".123456Z")
    log.write({"type": "tool", "output": f'{{"started_at": "{stamp}", "k": "{ADMIN_KEY}", '
                                         f'"d": "{stamp[:10]}", "env": "envkey12345"}}'})
    out = tmp_path / "export.jsonl"
    log.export(out, ["envkey12345"])
    text = out.read_text(encoding="utf-8")
    assert ADMIN_KEY not in (tmp_path / "full.jsonl").read_text(encoding="utf-8")
    assert ADMIN_KEY not in text and stamp[:10] not in text and "envkey12345" not in text and "<time>" in text


# ---------------------------------------------------------------------------------------------- run state


def test_observe_tracks_start_complete_verify_and_verdicts():
    st = harness.State()
    start = ["flagsmith", "api", "api/v1/environments/e/experiments/7/start/", "-X", "POST"]
    assert harness.observe(st, [(start, 0, '{"status":"running"}')], '{"status":"running"}') == ["start"]
    assert st.experiment == 7 and st.started
    harness.observe(st, [(["python3", "scripts/stop_rule.py", "check", "-"], 0, "")], '{"verdict":"COMPUTING"}')
    assert st.last_verdict == "COMPUTING"
    done = ["flagsmith", "api", "api/v1/environments/e/experiments/7/complete/"]
    assert harness.observe(st, [(done, 0, '{"status":"completed"}')], "") == ["complete"]
    assert not st.verified
    harness.observe(st, [(["flagsmith", "eval", "ulysses_skill_cta", "--identity", "rollout-check-1"], 0, "{}")], "")
    assert st.verified


def test_a_completion_counts_even_when_a_later_command_in_the_chain_fails(shell):
    """The live run's `complete && refresh` (the refresh throttled with 429) still completed the experiment."""
    st = harness.State()
    res = shell.run('flagsmith api "api/v1/environments/e/experiments/7/complete/" -F rollout_decision=control '
                    '&& flagsmith api leak/')
    assert res.returncode == 1
    assert harness.observe(st, res.runs, res.output) == ["complete"] and st.completed


def test_a_failed_start_starts_no_traffic():
    st = harness.State()
    start = ["flagsmith", "api", "api/v1/environments/e/experiments/7/start/"]
    assert harness.observe(st, [(start, 1, '{"detail":"nope"}')], "") == []


def test_the_stand_in_owner_answers_questions_but_not_waits():
    st = harness.State()
    assert harness.owner_reply(st, "Shall I create the flag with this command?") == harness.OWNER_YES
    assert harness.owner_reply(st, "Plan below. Reply yes to create it.") == harness.OWNER_YES
    st.started = True
    assert harness.owner_reply(st, "HOLD. Next look after the refresh window.") is None
    assert harness.owner_reply(st, "HOLD at 98%. Want me to look again in five minutes?") == harness.OWNER_LATER
    assert harness.owner_reply(st, "May I POST the results refresh now?") == harness.OWNER_LATER
    assert harness.owner_reply(st, "SHIP fired. Approve completing with rollout_decision=variant?") == \
        harness.OWNER_YES
    st.completed = True
    assert harness.owner_reply(st, "Completed. Run the final refresh and the check?") == harness.OWNER_YES


def test_a_429_makes_the_stand_in_owner_wait_the_stated_time():
    out = '{"detail":"A refresh was requested recently. Try again later. Expected available in 230 seconds."}'
    assert harness.retry_seconds(out) == 230
    assert harness.retry_seconds('{"status":"running"}') == 0


def test_the_lab_lock_is_taken_and_released_only_by_its_holder(tmp_path):
    lock = tmp_path / "LAB-LOCK"
    lock.write_text("someone-else", encoding="utf-8")
    with pytest.raises(SystemExit):
        harness.take_lock(lock)
    harness.release_lock(lock)
    assert lock.exists()
    lock.unlink()
    harness.take_lock(lock)
    assert lock.read_text(encoding="utf-8") == harness.LOCK_LABEL
    harness.release_lock(lock)
    assert not lock.exists()


# ---------------------------------------------------------------------------------------------- tokens


def _usage(prompt, cached, out, reasoning=0):
    return {"prompt_tokens": prompt, "completion_tokens": out,
            "prompt_tokens_details": {"cached_tokens": cached}, "completion_tokens_details": {"reasoning_tokens":
                                                                                              reasoning}}


def test_token_accounting_matches_the_reported_usage(tmp_path):
    replies = iter([
        {"model": f"gpt-5-mini-{at(0)[:10]}", "choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "shell", "arguments": json.dumps({"command": "flagsmith --version"})}}]}}],
         "usage": _usage(4000, 0, 300, 200)},
        {"model": f"gpt-5-mini-{at(0)[:10]}", "choices": [{"finish_reason": "stop", "message": {
            "content": "Shall I create the flag?"}}], "usage": _usage(4200, 3968, 120, 64)},
    ])
    seen = []

    def post(url, headers, body, timeout):
        seen.append(headers["Authorization"])
        return 200, next(replies)

    guard = SpendGuard(path=tmp_path / "spend.json", cap_usd=0.90)
    model = OpenAIModel(post=post, guard=guard)
    tokens = harness.Tokens()
    msgs = [{"role": "user", "content": "go"}]
    t1 = model.next_turn(system="s", tools=[harness.TOOL], messages=msgs)
    tokens.add(t1.usage)
    msgs += [{"role": "assistant", "content": t1.content},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "v2"}]}]
    t2 = model.next_turn(system="s", tools=[harness.TOOL], messages=msgs)
    tokens.add(t2.usage)
    t = tokens.totals()
    assert t["calls"] == 2 and t["input_tokens"] == 8200 and t["cached_input_tokens"] == 3968
    assert t["uncached_input_tokens"] == 4232 and t["output_tokens"] == 420 and t["reasoning_tokens"] == 264
    want = ((4000 * 0.25 + 300 * 2.0) + ((4200 - 3968) * 0.25 + 3968 * 0.025 + 120 * 2.0)) / 1e6
    assert t["cost_usd"] == pytest.approx(want, abs=2e-6)
    assert guard.total() == pytest.approx(want, abs=1e-8)
    assert json.loads((tmp_path / "spend.json").read_text())["calls"] == 2
    assert [c["call"] for c in tokens.calls] == [1, 2]


def test_an_empty_usage_counts_as_zero():
    tokens = harness.Tokens()
    tokens.add(None)
    assert tokens.totals() == {"calls": 1, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
                               "reasoning_tokens": 0, "uncached_input_tokens": 0, "cost_usd": 0.0}


def test_refused_is_an_exception_for_callers():
    with pytest.raises(Refused):
        check("rm -rf /")


def test_the_stop_rule_names_the_two_look_mistakes_a_live_run_made(capsys):
    """A path without its trailing slash returns the dashboard's HTML; --jq on the bodies strips what check needs."""
    import io

    sr = _stop_rule()
    for body, want in (("<!doctype html><html>", "trailing /"), ('{"id": 1, "status": "running"}{"as_of": 1}',
                                                                  "no --jq")):
        old = sys.stdin
        sys.stdin = io.StringIO(body)
        try:
            assert sr.main(["check", "-"]) == 2
        finally:
            sys.stdin = old
        out = json.loads(capsys.readouterr().out)
        assert out["verdict"] == "REFUSED" and want in out["why"]
