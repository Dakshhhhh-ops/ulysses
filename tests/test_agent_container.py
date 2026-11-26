"""The agent/service split as a container boundary: the agent container cannot read the service's secrets.

The static tests read docker-compose.yml (through `docker compose config` when the Docker CLI is
installed, and as text either way) and docker/agent.Dockerfile. The live test builds the image and
runs `ls` / `cat` attempts and the boundary check inside the container; it needs Docker and runs
only with ULYSSES_LIVE_DOCKER=1 (ULYSSES_AGENT_CHECK_URL, ULYSSES_AGENT_CHECK_TOKEN and
ULYSSES_AGENT_CHECK_READ point it at a running service for the MCP part).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "docker" / "agent.Dockerfile"
ALLOWED_ENV = {"ULYSSES_URL", "ULYSSES_AGENT_TOKEN"}
# Everything on the service side of the split.
SECRET_PATHS = (".env", ".run", "keys", ".ulysses")


def _agent_block() -> str:
    """The agent service's lines of docker-compose.yml, by indentation."""
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = lines.index("  agent:")
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


def _config(tmp_path, *profiles: str) -> dict:
    empty = tmp_path / "empty.env"  # never interpolate from a real .env in a test
    empty.write_text("", encoding="utf-8")
    cmd = ["docker", "compose", "--env-file", str(empty), "-f", str(COMPOSE)]
    for p in profiles:
        cmd += ["--profile", p]
    r = subprocess.run(cmd + ["config", "--format", "json"], capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_agent_service_text_has_no_volume_env_file_or_secret():
    block = _agent_block()
    for word in ("volumes:", "env_file:", "secrets:", "configs:", "FLAGSMITH_", "ULYSSES_DB", "ULYSSES_KEY",
                 "WEBHOOK_SECRET", ".env", ".run", "ports:", "network_mode:", "privileged", "pid:"):
        assert word not in block, word
    env_keys = set(re.findall(r"^\s{6}([A-Z_]+):", block, re.M))
    assert env_keys == ALLOWED_ENV
    assert "read_only: true" in block and "profiles: ['agent']" in block


@pytest.mark.skipif(not shutil.which("docker"), reason="needs the Docker CLI")
def test_compose_config_gives_the_agent_only_the_url_and_token(tmp_path):
    cfg = _config(tmp_path, "agent")
    agent = cfg["services"]["agent"]
    assert set(agent["environment"]) == ALLOWED_ENV
    assert not agent.get("volumes") and not agent.get("env_file") and not agent.get("secrets")
    assert agent["read_only"] is True and agent["user"] == "10001:10001" and agent["cap_drop"] == ["ALL"]
    assert not agent.get("ports") and not agent.get("privileged")
    # Its network is its own: the Flagsmith stack (and its Postgres) is not on it.
    assert set(agent["networks"]) == {"agent-only"}
    for name, svc in cfg["services"].items():
        if name != "agent":
            assert "agent-only" not in (svc.get("networks") or {"default": None}), name
    assert agent["build"]["dockerfile"] == "docker/agent.Dockerfile"


@pytest.mark.skipif(not shutil.which("docker"), reason="needs the Docker CLI")
def test_a_plain_up_does_not_start_the_agent(tmp_path):
    assert "agent" not in _config(tmp_path)["services"]


def test_agent_image_copies_only_code():
    text = DOCKERFILE.read_text(encoding="utf-8")
    copies = [line.split()[1:-1] for line in text.splitlines()
              if line.startswith("COPY") and "--from=" not in line]
    sources = {s for c in copies for s in c}
    assert sources == {"pyproject.toml", "uv.lock", "README.md", "LICENSE", "src",
                       "scripts/agent_session.py", "scripts/agent_boundary_check.py"}
    assert not re.search(r"^\s*(VOLUME|ADD)\b", text, re.M)
    assert "USER 10001" in text
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for must in (".env", ".run/", "keys/", "**/*.key", "**/*.db"):
        assert must in ignored, must


def test_agent_session_reads_only_names_and_ids_from_the_demo_file():
    import importlib.util

    spec = importlib.util.spec_from_file_location("agent_session", ROOT / "scripts" / "agent_session.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    demo = {"environment": "env-key", "owner": "maya", "second_signer": "sam",
            "server_key": "ser.synthetic-not-a-real-key", "flag": "f",
            "experiments": {"aa": {"id": 1, "flag": "a"}, "cta": {"id": 2, "flag": "b"}}}
    view = mod.agent_view(demo)
    assert view == {"environment": "env-key", "owner": "maya", "second_signer": "sam",
                    "experiments": {"aa": {"id": 1}, "cta": {"id": 2}}}


def _load_script(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_judge_demo_agent_in_container_hands_the_agent_only_names_ids_url_and_token(tmp_path, monkeypatch, capsys):
    """run_agent_container, offline: the compose argv, the agent's view of the demo, the environment handed
    to `docker compose`, and the AGENT-RESULT handoff back to .run/agent-session-result.json."""
    import argparse

    jd = _load_script("judge_demo")
    agent_session = _load_script("agent_session")
    demo = {"environment": "env-key-synthetic", "owner": "maya", "second_signer": "sam",
            "server_key": "ser.synthetic-not-a-real-key", "flag": "f",
            "experiments": {"aa": {"id": 1, "flag": "a"}, "cta": {"id": 2, "flag": "b"}}}
    (tmp_path / "demo.json").write_text(json.dumps(demo), encoding="utf-8")
    monkeypatch.setattr(jd, "RUN", tmp_path)
    for k in ("ULYSSES_PUBLIC_URL", "ULYSSES_DB"):
        monkeypatch.setenv(k, "unset")  # run_agent_container updates these; monkeypatch puts them back
    monkeypatch.setenv("FLAGSMITH_API_KEY", "synthetic-admin-key-must-not-pass")
    monkeypatch.setenv("FLAGSMITH_ENVIRONMENT_KEY", "synthetic-env-key-must-not-pass")

    traffic_calls, synthetic_calls = [], []
    monkeypatch.setattr(agent_session, "start_traffic", lambda ns, procs: traffic_calls.append(ns))
    monkeypatch.setattr(agent_session, "mark_synthetic",
                        lambda view, which, db=None: synthetic_calls.append((view, which, db)))
    monkeypatch.setattr(jd, "_agent_session_module", lambda: agent_session)

    result = {"aa": {"outcome": "horizon", "action": "keep_control"},
              "cta": {"outcome": "win", "action": "ship_treatment"}}
    seen = {}

    class FakePopen:
        def __init__(self, cmd, cwd=None, env=None, stdout=None, text=None, encoding=None, errors=None):
            seen.update(cmd=cmd, env=env, encoding=encoding)
            self.stdout = iter(["[AGENT] look -> HOLD\n", "AGENT-RESULT " + json.dumps(result) + "\n"])

        def wait(self):
            return 0

    monkeypatch.setattr(jd.subprocess, "Popen", FakePopen)
    a = argparse.Namespace(shop_port=8241, port=8240)
    env_vals = {"ULYSSES_PUBLIC_URL": "http://127.0.0.1:8240", "ULYSSES_DB": ".run/judge.db",
                "ULYSSES_AGENT_TOKEN": "synthetic-agent-token"}
    assert jd.run_agent_container(a, env_vals, []) == 0

    cmd = seen["cmd"]
    assert cmd[:2] == ["docker", "compose"] and cmd[cmd.index("--profile") + 1] == "agent"
    i = cmd.index("run")
    assert cmd[i:i + 5] == ["run", "--rm", "--build", "--no-deps", "agent"]
    view = json.loads(cmd[cmd.index("--demo") + 1])
    assert view == agent_session.agent_view(demo)
    assert set(view) == {"environment", "owner", "second_signer", "experiments"}  # no key or secret fields
    assert all(set(e) == {"id"} for e in view["experiments"].values())
    assert "ser.synthetic" not in " ".join(cmd)
    env = seen["env"]
    assert not [k for k in env if k.startswith("FLAGSMITH_")]
    assert env["ULYSSES_AGENT_URL"] == "http://host.docker.internal:8240"
    assert env["ULYSSES_AGENT_TOKEN"] == "synthetic-agent-token"
    # The shop and visitors start on the service's side, and the experiments are labelled synthetic.
    assert len(traffic_calls) == 1 and traffic_calls[0].shop_port == 8241
    assert synthetic_calls == [(view, ["aa", "cta"], ".run/judge.db")]
    # The result line is parsed into the file the summary reads, and is not echoed; other lines are.
    assert json.loads((tmp_path / "agent-session-result.json").read_text()) == result
    printed = capsys.readouterr().out
    assert "[AGENT] look -> HOLD" in printed and "AGENT-RESULT" not in printed


def test_agent_session_output_survives_a_stream_that_cannot_encode_the_card_fences():
    """Regression: the card quotes names between ⟦ ⟧; with stdout piped or redirected on Windows
    (cp1252) the agent session died with UnicodeEncodeError at its first look."""
    import sys

    from ulysses.mcp_server import card_text

    look = {"decision": {"outcome": "hold", "reason": "Look 1 of 10. Hold. Guardrail refunds is inside its bound.",
                         "guardrails": [{"name": "refunds"}]}, "card_url": "http://127.0.0.1:8200/card/1"}
    card = card_text(look)
    with pytest.raises(UnicodeEncodeError):
        card.encode("cp1252")  # the card itself is not cp1252-safe, so the stream must tolerate it
    code = ("import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('agent_session', {str(ROOT / 'scripts' / 'agent_session.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "m.say('AGENT', 'look -> ' + sys.argv[1])\n")
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONPATH": str(ROOT / "src")}
    done = subprocess.run([sys.executable, "-c", code, card], capture_output=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-1500:]
    assert b"HOLD" in done.stdout and b"refunds" in done.stdout


# ---------------------------------------------------------------- live: needs Docker


def _compose(*args: str, env: dict | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", "-p", "ulysses", "-f", str(COMPOSE), "--profile", "agent", *args],
                          capture_output=True, text=True, timeout=timeout, cwd=ROOT,
                          env={**os.environ, **(env or {})})


@pytest.mark.skipif(os.environ.get("ULYSSES_LIVE_DOCKER") != "1", reason="set ULYSSES_LIVE_DOCKER=1 (needs Docker)")
def test_live_agent_container_cannot_read_the_services_secrets():
    built = _compose("build", "agent")
    assert built.returncode == 0, built.stderr[-2000:]
    # Shell attempts, the way an agent with a shell tool would try.
    attempts = " ; ".join([
        "cat /app/.env", "ls /app/.run", "cat /app/.run/ulysses.db", "cat /app/.run/judge.env",
        "cat /app/.run/demo.json", "ls /nonexistent/.ulysses/keys", "ls /root", "ls /run/secrets",
        "env | grep -c FLAGSMITH_", "touch /app/planted", "getent hosts postgres",
    ])
    script = "for c in \"$@\"; do sh -c \"$c\" >/dev/null 2>&1; echo \"$c => $?\"; done"
    r = _compose("run", "--rm", "--no-deps", "--entrypoint", "sh", "agent", "-c", script, "sh",
                 *[a.strip() for a in attempts.split(";")], timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    results = dict(line.rsplit(" => ", 1) for line in r.stdout.strip().splitlines())
    assert results, r.stdout
    for cmd, code in results.items():
        assert code != "0", f"{cmd!r} succeeded inside the agent container"
    # The boundary check: file reads, environment, names, and the MCP tools against the service.
    env = {}
    if os.environ.get("ULYSSES_AGENT_CHECK_URL"):
        env = {"ULYSSES_AGENT_URL": os.environ["ULYSSES_AGENT_CHECK_URL"],
               "ULYSSES_AGENT_TOKEN": os.environ.get("ULYSSES_AGENT_CHECK_TOKEN", "")}
    extra = ["--read", os.environ["ULYSSES_AGENT_CHECK_READ"]] if os.environ.get("ULYSSES_AGENT_CHECK_READ") else []
    chk = _compose("run", "--rm", "--no-deps", "--entrypoint", "python", "agent",
                   "scripts/agent_boundary_check.py", *extra, env=env, timeout=300)
    assert chk.returncode == 0, chk.stderr[-2000:]
    report = json.loads(chk.stdout[chk.stdout.index("{"):])
    assert not [f for f in report["files"] if f["readable"]], report["files"]
    assert report["flagsmith_env"] == [] and not report["resolves_postgres"] and not report["resolves_flagsmith"]
    assert not ({"ULYSSES_DB", "ULYSSES_WEBHOOK_SECRET", "ULYSSES_KEY_DIR"} & set(report["env_names"]))
    assert report["uid"] == 10001
    if env:
        mcp = report["mcp"]
        assert mcp.get("tools") == ["execute", "look", "read_experiment", "sign_contract", "start_under_contract"], mcp
        assert mcp["start_unknown_contract"]["ok"] is False
        if extra:
            assert mcp["read_experiment"]["ok"] is True and mcp["read_experiment"]["untrusted_fields_listed"]
