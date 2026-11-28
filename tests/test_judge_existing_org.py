"""judge_demo.py against an existing organisation (Flagsmith Cloud): no sign-up, no Docker, its own state.

No network: every call that would reach Flagsmith or start a process is replaced.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from ulysses import lab

ROOT = Path(__file__).resolve().parents[1]
CLOUD = "https://api.flagsmith.com"
KEY = "ser.not-a-real-admin-key-0123456789"


def _judge_demo():
    spec = importlib.util.spec_from_file_location("judge_demo_existing_org", ROOT / "scripts" / "judge_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def jd(tmp_path, monkeypatch):
    m = _judge_demo()
    monkeypatch.setattr(m, "RUN", tmp_path / ".run")
    monkeypatch.setattr(m, "JUDGE_ENV", tmp_path / ".run" / "judge.env")
    monkeypatch.setattr(m, "JUDGE_KEYS", tmp_path / "profile" / "judge" / "abc123")
    return m


def test_cloud_state_is_kept_apart_from_the_default_run(jd, tmp_path):
    assert jd.state_path("http://localhost:8280", None) == tmp_path / ".run" / "judge.env"
    assert jd.state_path(CLOUD, 42) == tmp_path / ".run" / "judge-cloud.env"
    assert jd.state_path("https://flags.example.org", 7) == tmp_path / ".run" / "judge-org-7.env"
    cloud_keys = jd.keys_dir(jd.state_path(CLOUD, 42))
    assert jd.keys_dir(jd.JUDGE_ENV) == jd.JUDGE_KEYS
    # The default run wipes JUDGE_KEYS when it bootstraps a new instance: Cloud's keys are beside it, not in it.
    assert cloud_keys != jd.JUDGE_KEYS and jd.JUDGE_KEYS not in cloud_keys.parents
    assert cloud_keys.name == "abc123-cloud"


def test_the_admin_key_is_read_by_name_from_the_environment_or_a_git_ignored_file(jd, tmp_path):
    local = tmp_path / ".env.local"
    local.write_text(f"OTHER=x\nFLAGSMITH_CLOUD_ADMIN_KEY={KEY}\n", encoding="utf-8")
    assert jd.resolve_org_key("FLAGSMITH_CLOUD_ADMIN_KEY", {}, [local]) == KEY
    assert jd.resolve_org_key("FLAGSMITH_CLOUD_ADMIN_KEY", {"FLAGSMITH_CLOUD_ADMIN_KEY": "from-env"}, [local]) == "from-env"
    assert jd.resolve_org_key("MISSING_KEY", {}, [local, tmp_path / "absent.env"]) is None


def test_state_values_name_the_key_variable_never_the_key(jd, tmp_path):
    state = jd.state_path(CLOUD, 42)
    vals, fresh = jd.existing_org_values(CLOUD, 42, None, "FLAGSMITH_CLOUD_ADMIN_KEY", 8287, None, state, {})
    assert fresh
    assert KEY not in "".join(vals.values()) and "FLAGSMITH_ADMIN_KEY" not in vals
    assert vals["FLAGSMITH_ADMIN_KEY_ENV"] == "FLAGSMITH_CLOUD_ADMIN_KEY"
    assert vals["FLAGSMITH_PROJECT_ID"] == ""  # set, so the self-hosted .env's project cannot leak in
    assert vals["ULYSSES_DB"] == ".run/judge-cloud.db" and vals["ULYSSES_WEBHOOK_URL"] == ""
    assert vals["ULYSSES_KEY_DIR"].startswith(str(jd.keys_dir(state)))
    # A rerun on the same organisation keeps the token, secret and store (its signers are registered there) ...
    again, fresh2 = jd.existing_org_values(CLOUD, 42, 5, "FLAGSMITH_CLOUD_ADMIN_KEY", 8290, None, state, vals)
    assert not fresh2 and again["ULYSSES_AGENT_TOKEN"] == vals["ULYSSES_AGENT_TOKEN"]
    assert again["ULYSSES_WEBHOOK_SECRET"] == vals["ULYSSES_WEBHOOK_SECRET"]
    assert again["ULYSSES_PUBLIC_URL"].endswith(":8290") and again["FLAGSMITH_PROJECT_ID"] == "5"
    # ... another organisation starts fresh.
    other, fresh3 = jd.existing_org_values(CLOUD, 43, None, "FLAGSMITH_CLOUD_ADMIN_KEY", 8287, None, state, vals)
    assert fresh3 and other["ULYSSES_AGENT_TOKEN"] != vals["ULYSSES_AGENT_TOKEN"]


def test_audit_webhook_is_skipped_without_a_public_url(jd, tmp_path):
    state = tmp_path / ".run" / "judge-cloud.env"
    assert jd.lab_setup_command("py", state, True, None)[-1] == "--no-webhook"
    assert "--no-webhook" not in jd.lab_setup_command("py", state, True, "https://tunnel.example/webhooks/flagsmith")
    assert "--no-webhook" not in jd.lab_setup_command("py", jd.JUDGE_ENV, False, None)  # default run unchanged


def test_cloud_events_go_to_the_cloud_events_host(monkeypatch):
    monkeypatch.delenv("FLAGSMITH_EVENTS_API_URL", raising=False)
    assert lab.is_flagsmith_cloud(CLOUD) and lab.is_flagsmith_cloud("https://edge.api.flagsmith.com/api/v1/")
    assert not lab.is_flagsmith_cloud("http://localhost:8000")
    assert not lab.is_flagsmith_cloud("https://api.flagsmith.com.example.org")
    assert lab.events_api_url(CLOUD) == "https://events.api.flagsmith.com/"
    assert lab.events_api_url("http://localhost:8000/") == "http://localhost:8000/api/"
    monkeypatch.setenv("FLAGSMITH_EVENTS_API_URL", "https://events.example.org")
    assert lab.events_api_url(CLOUD) == "https://events.example.org/"


def _run_main(jd, monkeypatch, argv, *, lab_rc=1):
    """Run main() with every outside effect recorded; ``ulysses lab setup`` fails so main stops there."""
    calls = []

    def fake_run(cmd, *args, **kw):
        calls.append({"cmd": [str(c) for c in cmd], "env": kw.get("env") or {}})
        return subprocess.CompletedProcess(cmd, lab_rc)

    def no_docker(name):
        raise AssertionError(f"looked for {name}: the existing-organisation path must not use Docker")

    def no_bootstrap(*a, **kw):
        raise AssertionError("signed up on an existing organisation's Flagsmith")

    monkeypatch.setattr(jd.subprocess, "run", fake_run)
    monkeypatch.setattr(jd.subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Popen")))
    monkeypatch.setattr(jd.shutil, "which", no_docker)
    monkeypatch.setattr(lab, "bootstrap_stock", no_bootstrap)
    monkeypatch.setattr(jd, "wait_healthy", lambda url, minutes: None)
    monkeypatch.setattr(jd, "port_free", lambda port: True)
    seen = []
    monkeypatch.setattr(jd, "org_key_works", lambda api, key, org: seen.append((api, key, org)) or True)
    monkeypatch.setattr(sys, "argv", ["judge_demo.py", *argv])
    with pytest.raises(SystemExit):
        jd.main()
    return calls, seen


def test_main_on_cloud_uses_the_existing_organisation_without_docker_or_signup(jd, tmp_path, monkeypatch):
    monkeypatch.setenv("ULYSSES_TEST_ORG_KEY", KEY)
    jd.RUN.mkdir(parents=True)
    jd.JUDGE_ENV.write_text("FLAGSMITH_API_URL=http://localhost:8280\nFLAGSMITH_ADMIN_KEY=self-hosted\n",
                            encoding="utf-8")
    before = jd.JUDGE_ENV.read_text(encoding="utf-8")
    stale = jd.RUN / "judge-cloud.db"
    stale.write_text("an earlier run's store", encoding="utf-8")
    calls, seen = _run_main(jd, monkeypatch, ["--flagsmith", CLOUD, "--org-key-env", "ULYSSES_TEST_ORG_KEY",
                                              "--organisation", "42592", "--port", "8287", "--shop-port", "8288"])
    assert seen == [(CLOUD, KEY, 42592)]
    assert jd.JUDGE_ENV.read_text(encoding="utf-8") == before  # the self-hosted state is untouched
    # A fresh state drops the store it names, and only that one: beside the state file, not in the checkout.
    assert not stale.exists()
    state = tmp_path / ".run" / "judge-cloud.env"
    text = state.read_text(encoding="utf-8")
    assert KEY not in text and "FLAGSMITH_ADMIN_KEY_ENV=ULYSSES_TEST_ORG_KEY" in text
    assert "FLAGSMITH_ORGANISATION_ID=42592" in text
    assert not any("docker" in c["cmd"][0] for c in calls)
    setup = next(c for c in calls if c["cmd"][2:5] == ["ulysses.cli", "lab", "setup"])
    assert setup["cmd"][-3:] == ["--env-file", str(state), "--no-webhook"]
    # The key goes to the setup process's environment, from where it was read; nothing else of it is kept.
    env = setup["env"]
    assert env["FLAGSMITH_ADMIN_KEY"] == KEY and env["FLAGSMITH_API_URL"] == CLOUD
    assert env["FLAGSMITH_ORGANISATION_ID"] == "42592" and env["FLAGSMITH_PROJECT_ID"] == ""
    assert "ULYSSES_TEST_ORG_KEY" not in env


def test_existing_org_flags_must_come_together(jd, monkeypatch, capsys):
    monkeypatch.setenv("ULYSSES_TEST_ORG_KEY", KEY)
    for argv in (["--org-key-env", "ULYSSES_TEST_ORG_KEY", "--organisation", "1"],  # no --flagsmith
                 ["--flagsmith", CLOUD, "--org-key-env", "ULYSSES_TEST_ORG_KEY"],  # no --organisation
                 ["--flagsmith", CLOUD, "--organisation", "1"]):  # no --org-key-env
        calls, seen = _run_main(jd, monkeypatch, argv)
        assert calls == [] and seen == []
    assert "--org-key-env needs --flagsmith" in capsys.readouterr().err


def test_a_key_that_cannot_see_the_organisation_stops_before_any_setup(jd, monkeypatch, capsys):
    monkeypatch.setenv("ULYSSES_TEST_ORG_KEY", KEY)
    monkeypatch.setattr(jd, "org_key_works", lambda api, key, org: False)
    monkeypatch.setattr(jd, "wait_healthy", lambda url, minutes: None)
    monkeypatch.setattr(jd, "port_free", lambda port: True)
    monkeypatch.setattr(jd.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("ran")))
    monkeypatch.setattr(sys, "argv", ["judge_demo.py", "--flagsmith", CLOUD, "--org-key-env", "ULYSSES_TEST_ORG_KEY",
                                      "--organisation", "9"])
    with pytest.raises(SystemExit):
        jd.main()
    err = capsys.readouterr().err
    assert "cannot read organisation 9" in err and KEY not in err
