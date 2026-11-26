"""``ulysses`` command line."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

from . import DISTRIBUTION, __version__
from . import boundary as bd
from . import contract as ct
from . import signing

console = Console()
err = Console(stderr=True)


def fail(msg: str, hint: str | None = None, code: int = 1) -> None:
    err.print(f"[bold red]error[/] {msg}")
    if hint:
        err.print(f"[dim]next:[/] {hint}")
    sys.exit(code)


def _env() -> None:
    from .service.runtime import load_dotenv

    load_dotenv()


def _service() -> tuple[str, str]:
    _env()
    url = os.environ.get("ULYSSES_URL") or os.environ.get("ULYSSES_PUBLIC_URL") or "http://127.0.0.1:8200"
    return url.rstrip("/"), os.environ.get("ULYSSES_AGENT_TOKEN", "")


def _api(method: str, path: str, *, agent: bool = True, **kw) -> dict:
    url, token = _service()
    headers = {"Authorization": f"Bearer {token}"} if agent else {}
    try:
        r = httpx.request(method, url + path, headers=headers, timeout=300, **kw)
    except httpx.HTTPError as exc:
        fail(f"cannot reach the contract service at {url} ({exc})", "start it with: ulysses serve")
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text}
    if r.status_code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else body
        if isinstance(detail, list):
            detail = "; ".join(f"{e.get('loc')}: {e.get('msg')}" for e in detail)
        hint = None
        if isinstance(body, dict):
            hint = body.get("sign_url") or body.get("confirm_url") or body.get("next")
        fail(f"{r.status_code} {body.get('error', '') if isinstance(body, dict) else ''}: {detail}", hint)
    return body


def _store():
    _env()
    from .store import Store

    return Store(os.environ.get("ULYSSES_DB", ".run/ulysses.db"))


# ---- commands ------------------------------------------------------------------------------


def _env_lines(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and not line.lstrip().startswith("#"):
                out[k.strip()] = v.strip()
    return out


def cmd_init(a) -> None:
    root = Path(a.dir)
    root.mkdir(parents=True, exist_ok=True)
    env = root / ".env"
    port = a.port
    public = f"http://127.0.0.1:{port}"
    have = _env_lines(env)
    wanted = {
        "FLAGSMITH_API_URL": a.flagsmith_url,
        "FLAGSMITH_ADMIN_KEY": "",
        "FLAGSMITH_ORGANISATION_ID": "",
        "ULYSSES_AGENT_TOKEN": secrets.token_urlsafe(24),
        "ULYSSES_PUBLIC_URL": public,
        # Flagsmith in Docker reaches the host through host.docker.internal; its SSRF_ALLOWED_HOSTS must list it.
        # Flagsmith Cloud needs a public URL instead (for example a tunnel).
        "ULYSSES_WEBHOOK_URL": f"http://host.docker.internal:{port}/webhooks/flagsmith",
        "ULYSSES_WEBHOOK_SECRET": secrets.token_urlsafe(32),
        "ULYSSES_SLACK_WEBHOOK": "",
    }
    added = []
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    for k, v in wanted.items():
        if k not in have:
            lines.append(f"{k}={v}")
            added.append(k)
        elif not have[k] and k in ("ULYSSES_AGENT_TOKEN", "ULYSSES_WEBHOOK_SECRET"):
            lines = [f"{k}={v}" if ln.startswith(f"{k}=") else ln for ln in lines]
            added.append(k)
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green]{'wrote' if added else 'kept'}[/] {env}"
                  + (f" (added {', '.join(added)})" if added else "") + "; keep it out of git")
    token = _env_lines(env)["ULYSSES_AGENT_TOKEN"]
    tpl = root / "ulysses.contract.example.toml"
    tpl.write_text(TEMPLATE, encoding="utf-8")
    console.print(f"[green]wrote[/] {tpl}")
    servers: dict = {
        "ulysses": {
            "command": "uvx" if a.uvx else "uv",
            "args": (["--from", DISTRIBUTION, "ulysses", "mcp"] if a.uvx
                     else ["run", "--directory", str(root.resolve()), "ulysses", "mcp"]),
            "env": {"ULYSSES_URL": public, "ULYSSES_AGENT_TOKEN": token},
        }
    }
    if a.with_flagsmith_mcp:
        servers["flagsmith"] = {
            "command": "uvx",
            "args": ["--from", "git+https://github.com/Flagsmith/flagsmith.git@main#subdirectory=mcp", "flagsmith-mcp"],
            "env": {"TRANSPORT": "stdio", "FLAGSMITH_API_URL": a.flagsmith_url,
                    "FLAGSMITH_API_TOKEN": "ENVIRONMENT ADMIN KEY: gives the agent write access to flags"},
        }
        err.print("[yellow]warning[/] --with-flagsmith-mcp: on open-source Flagsmith the only key that can read "
                  "experiments is an environment admin key, which can also change flags. That gives the agent a "
                  "path around the contract. Ulysses' read_experiment tool needs no key.")
    p = root / "ulysses.mcp.json"
    p.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")
    console.print(f"[green]wrote[/] {p} (holds the agent token: git-ignored; paste into your MCP client)")
    gi = root / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    add = [x for x in (".env", ".run/", "ulysses.mcp.json") if x not in lines]
    if add:
        gi.write_text("\n".join(lines + add) + "\n")
    console.print(
        "\nNext:\n"
        "  1. put your organisation admin API key and organisation id in .env\n"
        "     (Flagsmith: Organisation settings > API Keys > create an admin key; the id is in that page's URL)\n"
        "  2. uv run ulysses lab setup            (demo flag, metrics, audit webhook with the secret above)\n"
        "  3. uv run python scripts/demo_setup.py (a fresh experiment and two signers)\n"
        f"  4. uv run ulysses serve               (contract service on {public})\n"
        "  For your own experiments instead of the demo: ulysses keys new <name>, ulysses signer add, ulysses serve.\n"
        "  Keep .env, the service database and signer keys where the agent cannot read them."
    )


TEMPLATE = """\
# Ulysses stop contract template. The agent drafts a filled-in copy with
# sign_contract; you can also draft one with `ulysses contract draft`.
# boundary.planned is computed for you; editing it by hand is rejected.

ulysses_contract = 1
hypothesis = "The new checkout button raises purchases without raising refunds"

[experiment]
project = 1
environment = "<environment key>"
experiment = 0
flag = "<flag name>"
control = "control"
treatment = "<variation key>"

[primary_metric]
name = "<goal metric name in Flagsmith>"
direction = "increase"

[[guardrails]]
name = "<guardrail metric name in Flagsmith>"
direction = "not_increase"
alpha = 0.05  # one-sided false-stop budget, spent at the counted looks

[horizon]
identities_per_arm = 2000
looks = 12
min_minutes_between_looks = 5

[boundary]
spending = "lan_demets_obrien_fleming"
alpha = 0.05
sides = "two_sided"

[actions]
win = "ship_treatment"
loss = "keep_control"
horizon = "keep_control"
guardrail_breach = "keep_control"

[owner]
name = "<your name>"
public_key = "ed25519:<from ulysses keys new>"

[second_signer]
name = "<a colleague>"
public_key = "ed25519:<their public key>"

[validity]
start_within_days = 30

[design]
baseline_rate = 0.05
min_relative_lift = 0.1
power = 0.8
"""


def cmd_serve(a) -> None:
    from .service.runtime import default_port, serve

    _env()
    serve(a.host, a.port or default_port(), poll_seconds=a.poll)


def cmd_mcp(a) -> None:
    # The agent side reads only its two settings, never the Flagsmith key, even when
    # it is started from a directory whose .env holds that key for the service.
    env = Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() in ("ULYSSES_URL", "ULYSSES_AGENT_TOKEN", "ULYSSES_PUBLIC_URL"):
                os.environ.setdefault(k.strip(), v.strip())
    if "ULYSSES_URL" not in os.environ and "ULYSSES_PUBLIC_URL" in os.environ:
        os.environ["ULYSSES_URL"] = os.environ["ULYSSES_PUBLIC_URL"]
    for k in [k for k in os.environ if k.startswith("FLAGSMITH_")]:
        del os.environ[k]
    from .mcp_server import main

    main()


def cmd_keys_new(a) -> None:
    import getpass

    kp = signing.Keypair.generate()
    if a.no_passphrase:
        pw = None
        err.print("[yellow]warning[/] key stored without a passphrase; anything that can read the file can sign")
    else:
        pw = signing.passphrase_for(a.name) or getpass.getpass(f"Passphrase for {a.name}'s new signing key: ")
        if len(pw) < 8:
            fail("use a passphrase of at least 8 characters, or --no-passphrase for a throwaway test key")
    path = signing.save_keypair(a.name, kp, Path(a.key_dir) if a.key_dir else None, passphrase=pw)
    console.print(f"[green]created[/] {path}")
    console.print(kp.public)


def cmd_keys_show(a) -> None:
    console.print(signing.load_keypair(a.name, Path(a.key_dir) if a.key_dir else None, prompt=True).public)


def cmd_signer_add(a) -> None:
    from .core import Ulysses
    from .flagsmith.fake import FakeFlagsmith

    svc = Ulysses(_store(), FakeFlagsmith())
    svc.register_signer(a.name, a.public_key)
    console.print(f"[green]registered[/] {a.name}")


def cmd_signer_enrol(a) -> None:
    store = _store()
    tok = store.new_enrol_token(a.name, time.time())
    url, _ = _service()
    console.print(f"Send this one-time link to {a.name}; it creates a key in their browser:\n{url}/enrol/{tok}")


def cmd_signer_list(a) -> None:
    t = Table("signer", "public key")
    for s in _store().signers():
        t.add_row(s["name"], s["public_key"])
    console.print(t)


def cmd_contract_verify(a) -> None:
    try:
        c = ct.load(a.file)
    except Exception as exc:  # noqa: BLE001
        fail(f"{a.file} is not a valid contract: {exc}")
    console.print(f"[green]valid[/] {c.hash}")
    _print_boundary(c)


def cmd_contract_fix(a) -> None:
    import tomllib

    data = tomllib.loads(Path(a.file).read_text(encoding="utf-8"))
    c = ct.with_regenerated_table(data)
    ct.save(c, a.file)
    console.print(f"[green]rewrote[/] {a.file} -> {c.hash}")


def cmd_contract_draft(a) -> None:
    body = {"environment": a.environment, "experiment_id": a.experiment, "owner": a.owner,
            "second_signer": a.second_signer, "min_relative_lift": a.min_lift, "baseline_rate": a.baseline_rate,
            "alpha": a.alpha, "power": a.power, "looks": a.looks, "identities_per_arm": a.identities_per_arm,
            "expected_rate_per_hour": a.rate_per_hour, "hypothesis": a.hypothesis,
            "override_reason": a.override_reason}
    out = _api("POST", "/api/contracts/draft", json={k: v for k, v in body.items() if v is not None})
    console.print(f"[green]drafted[/] {out['hash']}\nsign at: {out['sign_url']}")
    if a.out:
        toml = httpx.get(_service()[0] + f"/api/contracts/{out['hash']}/toml",
                         headers={"Authorization": f"Bearer {_service()[1]}"}, timeout=60).text
        Path(a.out).write_text(toml, encoding="utf-8")
        console.print(f"[green]wrote[/] {a.out}")


def cmd_contract_submit(a) -> None:
    out = _api("POST", "/api/contracts", json={"toml": Path(a.file).read_text(encoding="utf-8")})
    console.print(f"[green]submitted[/] {out['hash']}  state={out['state']}\nsign at: {out['sign_url']}")


def cmd_contract_show(a) -> None:
    out = _api("GET", f"/api/contracts/{a.hash}")
    console.print_json(data=out)


def cmd_sign(a) -> None:
    kp = signing.load_keypair(a.as_, Path(a.key_dir) if a.key_dir else None, prompt=True)
    sig = kp.sign(signing.contract_message(a.hash, a.role))
    out = _api("POST", "/human/sign", agent=False,
               json={"hash": a.hash, "role": a.role, "signer": a.as_, "signature": sig})
    console.print(f"[green]signed[/] as {a.as_} ({a.role}); contract is {out['state']}")


def cmd_confirm(a) -> None:
    url, _ = _service()
    card = httpx.get(f"{url}/human/card/{a.ident}", timeout=60).json()
    p = card.get("proposal")
    if not p:
        fail("nothing to confirm on this card")
    msg = card["confirm_message"]
    kp = signing.load_keypair(a.as_, Path(a.key_dir) if a.key_dir else None, prompt=True)
    console.print(f"Confirming: [bold]{p['action'].replace('_', ' ')}[/] (outcome {p['outcome']})")
    out = _api("POST", "/human/confirm", agent=False,
               json={"proposal_id": p["id"], "signer": a.as_, "signature": kp.sign(msg.encode())})
    console.print(f"[green]{out['state']}[/] {out.get('result') or ''}")


def cmd_start(a) -> None:
    out = _api("POST", f"/api/contracts/{a.hash}/start")
    console.print(f"[green]started[/] experiment {out['experiment']['id']} under {a.hash}")


def _print_card(out: dict) -> None:
    from .mcp_server import card_text

    text = card_text(out)
    first, _, rest = text.partition("\n")
    style = "bold green" if "WIN" in first else "bold red" if ("LOSS" in first or "GUARDRAIL" in first) else "bold yellow"
    console.print(f"[{style}]{first}[/]")
    if rest:
        console.print(rest)


def cmd_look(a) -> None:
    _print_card(_api("POST", f"/api/contracts/{a.hash}/look"))


TRANSIENT = {429, 502, 503, 504}


def _look_once(path: str) -> tuple[dict | None, str | None]:
    """(response, transient problem). Permanent errors exit with one line."""
    url, token = _service()
    try:
        r = httpx.post(url + path, headers={"Authorization": f"Bearer {token}"}, timeout=300)
    except httpx.HTTPError as exc:
        return None, f"contract service did not answer ({type(exc).__name__})"
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text[:200]}
    if r.status_code in TRANSIENT:
        return None, f"{r.status_code}: {body.get('detail') if isinstance(body, dict) else body}"
    if r.status_code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else body
        fail(f"{r.status_code}: {detail}", (body or {}).get("confirm_url") if isinstance(body, dict) else None)
    if body.get("look_not_taken"):
        return None, body.get("detail", "Flagsmith did not answer")
    return body, None


def cmd_watch(a) -> None:
    backoff = 60
    misses = 0
    while True:
        out, problem = _look_once(f"/api/contracts/{a.hash}/look")
        if problem:
            misses += 1
            if misses > a.max_retries:
                fail(f"gave up after {misses} tries: {problem}", f"run `ulysses watch {a.hash}` again later")
            console.print(f"[dim]{problem}; retrying in {backoff}s ({misses}/{a.max_retries})[/]")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue
        misses, backoff = 0, 60
        if out.get("too_soon"):
            time.sleep(min(out["retry_after_seconds"] + 2, 330))
            continue
        _print_card(out)
        if out.get("already_decided") or (out.get("decision") or {}).get("fires") or out.get("state") == "deviated":
            return
        time.sleep(a.every)


def cmd_execute(a) -> None:
    out = _api("POST", f"/api/contracts/{a.hash}/execute", json={"action": a.action})
    console.print(f"[green]{out['state']}[/] {out.get('result') or ''}")


def cmd_amend(a) -> None:
    changes = json.loads(a.changes) if a.changes else {}
    out = _api("POST", f"/api/contracts/{a.hash}/amend",
               json={"changes": changes, "reason": a.reason, "action_now": a.action_now})
    console.print(f"[green]amendment drafted[/] {out['hash']}")
    for role, url in out["sign_urls"].items():
        console.print(f"  {role}: {url}")


def _print_boundary(c: ct.Contract) -> None:
    t = Table("look", "identities per arm", "win needs chance to win", "z")
    for p in c.boundary.planned:
        t.add_row(str(p.look), str(p.identities_per_arm),
                  "no stop possible" if p.chance_to_win is None else f"{p.chance_to_win * 100:.4f}%",
                  "-" if p.z is None else f"{p.z:.3f}")
    console.print(t)


def cmd_boundary(a) -> None:
    fr = [k / a.looks for k in range(1, a.looks + 1)]
    zs = bd.spending_boundaries(fr, a.alpha)
    t = Table("look", "fraction", "z", "chance to win needed", "cumulative alpha")
    for k, (f, z) in enumerate(zip(fr, zs), start=1):
        t.add_row(str(k), f"{f:.3f}", "-" if z is None else f"{z:.4f}",
                  "no stop possible" if z is None else f"{bd.z_to_chance(z) * 100:.4f}%",
                  f"{bd.obf_spending(f, a.alpha):.6f}")
    console.print(t)
    console.print(f"[dim]Lan-DeMets O'Brien-Fleming spending, alpha {a.alpha} two-sided[/]")


def cmd_horizon(a) -> None:
    from . import horizon

    try:
        p = horizon.plan(min_relative_lift=a.min_lift, baseline_rate=a.baseline_rate, baseline_mean=a.baseline_mean,
                         baseline_sd=a.baseline_sd, alpha=a.alpha, power=a.power, looks=a.looks,
                         exposure_rate_per_hour=a.rate_per_hour)
    except ValueError as exc:
        fail(str(exc))
    console.print(f"[bold]{p.identities_per_arm}[/] identities per arm "
                  f"(one-look design: {p.fixed_sample_identities_per_arm}; sequential inflation {p.inflation}), "
                  f"{p.looks} looks, alpha {p.alpha}, power {p.power:.0%} for a {p.min_relative_lift:.0%} relative lift")
    if p.hours_to_horizon is not None:
        console.print(f"At {p.exposure_rate_per_hour:.1f} new identities per arm per hour: horizon in "
                      f"{p.hours_to_horizon:.1f} h, a look every {p.minutes_between_looks:.0f} min")
    for n in p.notes:
        console.print(f"[yellow]note[/] {n}")


def cmd_audit_replay(a) -> None:
    out = _api("GET", f"/api/audit/replay?project={a.project}")
    t = Table("audit id", "change", "by", "verdict", "contract", "confirmed by")
    for r in out["rows"]:
        colour = {"signed": "green", "deviation": "red", "signature invalid": "red"}.get(r["verdict"], "dim")
        t.add_row(str(r["audit_id"]), r["log"][:60], r["author"] or "API key", f"[{colour}]{r['verdict']}[/]",
                  (r.get("contract") or "")[:19], r.get("confirmed_by") or "")
    console.print(t)
    console.print(f"{out['governed_lifecycle_changes']} lifecycle changes on governed experiments: "
                  f"[green]{out['mapped_to_signed_contract']} map to a signed contract[/], "
                  f"[red]{out['deviations']} deviations[/], {out['invalid']} with invalid signatures. "
                  f"({out['not_governed']} changes on experiments not under a contract.)")


def cmd_audit_sync(a) -> None:
    out = _api("POST", f"/api/audit/sync?project={a.project}")
    devs = [x for x in out if x.get("verdict") == "deviation" and not x.get("duplicate")]
    console.print(f"processed {len(out)} audit entries; {len(devs)} new deviations")


def cmd_calibrate_offline(a) -> None:
    from .calibrate import offline

    console.print("Running Flagsmith's own stats.py on simulated A/A tests (synthetic units) ...")
    r = offline.run(runs=a.runs, flagsmith_src=a.flagsmith_src, looks=a.looks, per_arm_per_look=a.per_arm)
    console.print(r["markdown"])
    console.print(f"[dim]written: {', '.join(r['paths'])}[/]")


def cmd_calibrate_live(a) -> None:
    from .calibrate import live

    _env()
    s = live.run(experiments=a.experiments, looks=a.looks, per_arm_per_look=a.per_arm, alpha=a.alpha)
    console.print(s["markdown"])


def cmd_calibrate_summarise(a) -> None:
    from .calibrate import live

    try:
        s = live.summarise_trace(a.trace, signed_looks=a.signed_looks, signed_per_arm=a.signed_per_arm)
    except ValueError as exc:
        fail(str(exc))
    out = Path(a.trace).with_name("live.md")
    out.write_text(s["markdown"], encoding="utf-8")
    console.print(s["markdown"])
    console.print(f"[dim]written: {out}[/]")


def cmd_lab_setup(a) -> None:
    from . import lab

    _env()
    cfg = lab.LabConfig.from_env()
    d = lab.setup_demo(cfg)
    console.print({k: v for k, v in d.items() if "key" not in k})
    if a.no_webhook:
        console.print("[yellow]audit webhook skipped[/] (--no-webhook): deviation detection by webhook is off; "
                      "the look-time state check still runs")
        return
    secret = os.environ.get("ULYSSES_WEBHOOK_SECRET")
    if not secret:
        fail("ULYSSES_WEBHOOK_SECRET is not set; the audit webhook must be signed",
             "run `ulysses init` to generate one, or pass --no-webhook")
    hook_url = os.environ.get("ULYSSES_WEBHOOK_URL", "http://host.docker.internal:8200/webhooks/flagsmith")
    try:
        h = lab.ensure_audit_webhook(cfg, hook_url, secret)
    except RuntimeError as exc:
        if "private network" in str(exc) or "internal" in str(exc):
            err.print("[yellow]audit webhook skipped[/]: Flagsmith refused the URL. On self-hosted Flagsmith add "
                      "host.docker.internal to SSRF_ALLOWED_HOSTS and run `ulysses lab setup` again "
                      "(deviation detection by webhook is off until then).")
            return
        fail(f"could not register the audit webhook: {exc}")
    console.print(f"audit-log webhook -> {h['url']} (enabled: {h['enabled']}, signed)")


def cmd_lab_experiment(a) -> None:
    from . import lab

    _env()
    out = lab.new_demo_experiment(lab.LabConfig.from_env(), a.name, a.hypothesis)
    console.print(out)


# ---- verification -------------------------------------------------------------------------


def _verify_sources(a):
    """The store and the experiment's environment, for export and live verify."""
    from .service import sources
    from .store import Store

    _env()
    db = a.db or os.environ.get("ULYSSES_DB", ".run/ulysses.db")
    if not Path(db).exists():
        fail(f"no Ulysses store at {db}", "pass --db <path to the service's database>")
    store = Store(db)
    env = a.environment
    if not env:
        envs = sorted({r["environment"] for r in store.all_contracts() if r["experiment_id"] == a.experiment})
        if not envs:
            fail(f"the store at {db} holds no contract for experiment {a.experiment}")
        if len(envs) > 1:
            fail(f"experiment {a.experiment} appears in {len(envs)} environments", "pass --environment <key>")
        env = envs[0]
    rec = sources._read(sources.registry_path(db)).get(f"{env}:{a.experiment}")
    return store, env, rec


def _flagsmith_gateway():
    from .flagsmith.rest import RestFlagsmith

    key = os.environ.get("FLAGSMITH_ADMIN_KEY", "")
    if not key:
        fail("FLAGSMITH_ADMIN_KEY is not set; reading the experiment from Flagsmith needs a key",
             "set it in the environment or .env, or use --bundle")
    return RestFlagsmith(os.environ.get("FLAGSMITH_API_URL", "http://localhost:8000"), key)


def _build_bundle(a) -> dict:
    from . import verify as vf
    from .flagsmith.gateway import FlagsmithError

    store, env, traffic = _verify_sources(a)
    state, missing = None, getattr(a, "no_flagsmith", None)
    if not missing:
        try:
            state = vf.flagsmith_state(_flagsmith_gateway(), env, a.experiment)
        except FlagsmithError as exc:
            fail(f"could not read experiment {a.experiment} from Flagsmith: {exc}",
                 "check FLAGSMITH_API_URL, or export with --no-flagsmith '<reason>'")
    return vf.export_bundle(store, env, a.experiment, flagsmith=state, flagsmith_unavailable=missing,
                            traffic=traffic)


def cmd_export_bundle(a) -> None:
    from . import verify as vf

    bundle = _build_bundle(a)
    stamps = vf.find_timestamps(bundle)
    if stamps:
        fail(f"the bundle would carry {len(stamps)} timestamp(s), first at {stamps[0]}; not written")
    text = vf.dumps(bundle)
    leaks = vf.find_secrets(text, [os.environ.get(k, "") for k in
                                   ("FLAGSMITH_ADMIN_KEY", "ULYSSES_AGENT_TOKEN", "ULYSSES_WEBHOOK_SECRET")])
    if leaks:
        fail(f"the bundle would carry {leaks[0]}; not written")
    out = Path(a.out or f"ulysses-bundle-{a.experiment}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(text.encode("utf-8"))
    console.print(f"[green]wrote[/] {out}: {len(bundle['contracts'])} contract(s), {len(bundle['looks'])} look(s), "
                  f"{len(bundle['outcomes'])} outcome(s)"
                  + ("" if bundle["flagsmith_final"] else "; no Flagsmith state (" + bundle["flagsmith_final_missing"] + ")"))
    console.print(f"check it anywhere with: ulysses verify --bundle {out}")


def cmd_verify(a) -> None:
    from . import verify as vf
    from .flagsmith.gateway import FlagsmithError

    trusted = {}
    for item in a.trust_key or []:
        name, sep, key = item.partition("=")
        if not sep:
            fail(f"--trust-key takes NAME=ed25519:..., got {item!r}")
        trusted[name.strip()] = key.strip()
    if a.expect_hash and not vf.HASH_RE.fullmatch(a.expect_hash):
        fail(f"--expect-hash takes sha256:<64 hex>, got {a.expect_hash!r}")
    pre: list = []
    if a.bundle:
        try:
            bundle = vf.load_bundle(a.bundle)
        except (OSError, ValueError) as exc:
            fail(f"cannot read the bundle {a.bundle}: {exc}")
        ref = bundle.get("experiment") if isinstance(bundle, dict) else None
        bundle_id = ref.get("experiment_id") if isinstance(ref, dict) else None
        if a.experiment is not None and a.experiment != bundle_id:
            fail(f"you asked to verify experiment {a.experiment}, but the bundle {a.bundle} is for experiment "
                 f"{bundle_id!r}", "drop the id, or pass the bundle for that experiment")
        live = bool(a.check_flagsmith)
        if live:
            env = ref.get("environment") if isinstance(ref, dict) else None
            if not isinstance(env, str) or not isinstance(bundle_id, int):
                fail("the bundle names no experiment to read from Flagsmith")
            _env()
            try:
                state = vf.flagsmith_state(_flagsmith_gateway(), env, bundle_id)
            except FlagsmithError as exc:
                fail(f"could not read experiment {bundle_id} from Flagsmith: {exc}", "check FLAGSMITH_API_URL")
            pre.append(vf.compare_flagsmith(bundle.get("flagsmith_final"), state))
            bundle = dict(bundle, flagsmith_final=state)
            bundle.pop("flagsmith_final_missing", None)
            print(f"Verifying bundle {a.bundle}: hypothesis and final state read from Flagsmith now, everything "
                  "else from the bundle")
        else:
            print(f"Verifying bundle {a.bundle} (offline: no service, no Flagsmith; every source is the exporter's)")
    else:
        if a.experiment is None:
            fail("give an experiment id, or --bundle <path>")
        if a.check_flagsmith:
            fail("--check-flagsmith goes with --bundle; `ulysses verify <id>` always reads Flagsmith")
        bundle = _build_bundle(a)
        live = True
        print(f"Verifying experiment {a.experiment}: hypothesis and final state read from Flagsmith now, "
              "contract text, signatures and looks from the service's store; every one is re-checked here")
    traffic = bundle.get("traffic") if isinstance(bundle, dict) else None
    if isinstance(traffic, dict):
        print(f"Traffic: {traffic.get('kind')} ({traffic.get('detail')})")
    checks = pre + vf.verify_bundle(bundle, trusted_keys=trusted, expect_hash=a.expect_hash, flagsmith_live=live)
    for c in checks:
        print(c.line())
    text, code = vf.summary(checks)
    print(text)
    sys.exit(code)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ulysses", description="Signed stop contracts for Flagsmith experiments.")
    p.add_argument("--version", action="version", version=f"ulysses {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="add the contract template, .env and MCP client config to a project")
    s.add_argument("--dir", default=".")
    s.add_argument("--flagsmith-url", default="https://api.flagsmith.com")
    s.add_argument("--port", type=int, default=8200, help="port for the service; written to every URL")
    s.add_argument("--with-flagsmith-mcp", action="store_true",
                   help="also add Flagsmith's official MCP server (needs an admin key on open source: not recommended)")
    s.add_argument("--uvx", action="store_true", help=f"write the MCP config for the published package (uvx --from {DISTRIBUTION})")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("serve", help="run the contract service (holds the Flagsmith write key)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=None, help="default: the port in ULYSSES_PUBLIC_URL, else 8200")
    s.add_argument("--poll", type=float, nargs="?", const=60.0, default=None, metavar="SECONDS",
                   help="take looks on the service's own schedule, trying every SECONDS (default 60); each "
                        "contract's refresh floor still applies")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("mcp", help="run the MCP extension over stdio")
    s.set_defaults(fn=cmd_mcp)

    keys = sub.add_parser("keys", help="your local signing keys").add_subparsers(dest="sub", required=True)
    s = keys.add_parser("new")
    s.add_argument("name")
    s.add_argument("--key-dir")
    s.add_argument("--no-passphrase", action="store_true", help="store unencrypted (tests only)")
    s.set_defaults(fn=cmd_keys_new)
    s = keys.add_parser("show")
    s.add_argument("name")
    s.add_argument("--key-dir")
    s.set_defaults(fn=cmd_keys_show)

    sg = sub.add_parser("signer", help="who may sign (operator)").add_subparsers(dest="sub", required=True)
    s = sg.add_parser("add")
    s.add_argument("name")
    s.add_argument("public_key")
    s.set_defaults(fn=cmd_signer_add)
    s = sg.add_parser("enrol", help="print a one-time link that creates a key in the signer's browser")
    s.add_argument("name")
    s.set_defaults(fn=cmd_signer_enrol)
    s = sg.add_parser("list")
    s.set_defaults(fn=cmd_signer_list)

    c = sub.add_parser("contract", help="draft, check and submit contracts").add_subparsers(dest="sub", required=True)
    s = c.add_parser("verify", help="validate a contract file and print its hash and boundary")
    s.add_argument("file")
    s.set_defaults(fn=cmd_contract_verify)
    s = c.add_parser("fix", help="recompute boundary.planned in a hand-edited draft")
    s.add_argument("file")
    s.set_defaults(fn=cmd_contract_fix)
    s = c.add_parser("draft", help="draft from a Flagsmith experiment (through the service)")
    s.add_argument("--environment", required=True)
    s.add_argument("--experiment", type=int, required=True)
    s.add_argument("--owner", required=True)
    s.add_argument("--second-signer", required=True)
    s.add_argument("--min-lift", type=float, required=True, help="smallest relative lift you care about, e.g. 0.1")
    s.add_argument("--baseline-rate", type=float)
    s.add_argument("--alpha", type=float, default=0.05)
    s.add_argument("--power", type=float, default=0.8)
    s.add_argument("--looks", type=int)
    s.add_argument("--identities-per-arm", type=int)
    s.add_argument("--rate-per-hour", type=float)
    s.add_argument("--hypothesis")
    s.add_argument("--override-reason", help="why the signers accept a design outside the service policy")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_contract_draft)
    s = c.add_parser("submit")
    s.add_argument("file")
    s.set_defaults(fn=cmd_contract_submit)
    s = c.add_parser("show")
    s.add_argument("hash")
    s.set_defaults(fn=cmd_contract_show)

    s = sub.add_parser("sign", help="sign a contract with your key file")
    s.add_argument("hash")
    s.add_argument("--as", dest="as_", required=True)
    s.add_argument("--role", default="owner", choices=["owner", "second_signer"])
    s.add_argument("--key-dir")
    s.set_defaults(fn=cmd_sign)

    s = sub.add_parser("confirm", help="one-tap confirm, from the command line")
    s.add_argument("ident", help="look id or proposal id")
    s.add_argument("--as", dest="as_", required=True)
    s.add_argument("--key-dir")
    s.set_defaults(fn=cmd_confirm)

    for name, fn, help_ in (("start", cmd_start, "start the experiment under a signed contract"),
                            ("look", cmd_look, "one look: refresh, read, compare")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("hash")
        s.set_defaults(fn=fn)
    s = sub.add_parser("watch", help="look on schedule until the contract fires")
    s.add_argument("hash")
    s.add_argument("--every", type=int, default=310, help="seconds between looks (Flagsmith floor is 300)")
    s.add_argument("--max-retries", type=int, default=10, help="transient failures in a row before giving up")
    s.set_defaults(fn=cmd_watch)
    s = sub.add_parser("execute", help="ask the service to run the pre-signed action")
    s.add_argument("hash")
    s.add_argument("--action", required=True, choices=["ship_treatment", "keep_control"])
    s.set_defaults(fn=cmd_execute)
    s = sub.add_parser("amend", help="draft an amendment (needs owner and second signer)")
    s.add_argument("hash")
    s.add_argument("--reason", required=True)
    s.add_argument("--changes", help='JSON, e.g. \'{"horizon": {"identities_per_arm": 3000}}\'')
    s.add_argument("--action-now", choices=["ship_treatment", "keep_control"])
    s.set_defaults(fn=cmd_amend)

    s = sub.add_parser("verify", help="audit a decision: hashes, signatures, every look against the signed rule")
    s.add_argument("experiment", type=int, nargs="?", help="Flagsmith experiment id (live: store + Flagsmith)")
    s.add_argument("--bundle", help="verify an exported bundle instead; needs no service and no Flagsmith")
    s.add_argument("--environment", help="Flagsmith environment key (default: the one the store holds)")
    s.add_argument("--db", help="the service's store (default ULYSSES_DB or .run/ulysses.db)")
    s.add_argument("--trust-key", action="append", metavar="NAME=KEY",
                   help="a signer's public key you know out of band; the contract must name the same key")
    s.add_argument("--expect-hash", metavar="sha256:...",
                   help="the contract hash you read on the experiment yourself; the bundle's chain must hold it")
    s.add_argument("--check-flagsmith", action="store_true",
                   help="with --bundle: read the hypothesis and final state from Flagsmith now and compare")
    s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("export-bundle", help="write a self-contained, timestamp-free bundle for `verify --bundle`")
    s.add_argument("experiment", type=int)
    s.add_argument("--environment")
    s.add_argument("--db")
    s.add_argument("--out", help="default ulysses-bundle-<experiment>.json")
    s.add_argument("--no-flagsmith", metavar="REASON",
                   help="export without Flagsmith's final state (for example the instance is gone); the reason is "
                        "written into the bundle and `verify` marks those checks SKIP")
    s.set_defaults(fn=cmd_export_bundle)

    s = sub.add_parser("boundary", help="print the O'Brien-Fleming boundary table")
    s.add_argument("--looks", type=int, default=12)
    s.add_argument("--alpha", type=float, default=0.05)
    s.set_defaults(fn=cmd_boundary)

    s = sub.add_parser("horizon", help="identities per arm and look schedule")
    s.add_argument("--min-lift", type=float, required=True)
    s.add_argument("--baseline-rate", type=float)
    s.add_argument("--baseline-mean", type=float)
    s.add_argument("--baseline-sd", type=float)
    s.add_argument("--alpha", type=float, default=0.05)
    s.add_argument("--power", type=float, default=0.8)
    s.add_argument("--looks", type=int)
    s.add_argument("--rate-per-hour", type=float, help="new identities per arm per hour (from Flagsmith exposures)")
    s.set_defaults(fn=cmd_horizon)

    au = sub.add_parser("audit", help="deviations and replay").add_subparsers(dest="sub", required=True)
    s = au.add_parser("replay", help="show that every lifecycle change maps to a signed contract")
    s.add_argument("--project", type=int, required=True)
    s.set_defaults(fn=cmd_audit_replay)
    s = au.add_parser("sync", help="pull Flagsmith's audit log and record deviations")
    s.add_argument("--project", type=int, required=True)
    s.set_defaults(fn=cmd_audit_sync)

    cal = sub.add_parser("calibrate", help="engine calibration on A/A tests").add_subparsers(dest="sub", required=True)
    s = cal.add_parser("offline", help="Flagsmith's own stats.py on simulated A/A tests")
    s.add_argument("--runs", type=int, default=2000)
    s.add_argument("--looks", type=int, default=30)
    s.add_argument("--per-arm", type=int, default=100)
    s.add_argument("--flagsmith-src", help="path to a Flagsmith checkout (else downloaded at the pinned tag)")
    s.set_defaults(fn=cmd_calibrate_offline)
    s = cal.add_parser("summarise", help="recompute the live table from a captured trace")
    s.add_argument("trace", help=".run/calibration/live-<batch>.jsonl")
    s.add_argument("--signed-looks", type=int, help="only for a trace without a design header: the looks it was started with")
    s.add_argument("--signed-per-arm", type=int, help="only for a trace without a design header")
    s.set_defaults(fn=cmd_calibrate_summarise)
    s = cal.add_parser("live", help="A/A experiments on a running self-hosted Flagsmith")
    s.add_argument("--experiments", type=int, default=100)
    s.add_argument("--looks", type=int, default=12)
    s.add_argument("--per-arm", type=int, default=100)
    s.add_argument("--alpha", type=float, default=0.05)
    s.set_defaults(fn=cmd_calibrate_live)

    lb = sub.add_parser("lab", help="create the demo objects in Flagsmith").add_subparsers(dest="sub", required=True)
    s = lb.add_parser("setup")
    s.add_argument("--no-webhook", action="store_true", help="do not register the audit-log webhook")
    s.set_defaults(fn=cmd_lab_setup)
    s = lb.add_parser("experiment", help="create a fresh demo experiment on the demo flag")
    s.add_argument("--name", default="ulysses checkout cta")
    s.add_argument("--hypothesis", default="The bold call to action raises purchases without raising refund requests.")
    s.set_defaults(fn=cmd_lab_experiment)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - one readable line, not a traceback
        if os.environ.get("ULYSSES_DEBUG"):
            raise
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        if isinstance(exc, httpx.HTTPError):
            hint = "check that Flagsmith and the contract service are running and reachable"
        elif isinstance(exc, (FileNotFoundError, PermissionError)):
            hint = "check the path, or run `ulysses init` in this folder first"
        else:
            hint = None
        fail(f"{type(exc).__name__}: {detail}"[:400], (hint + "; " if hint else "") + "set ULYSSES_DEBUG=1 for the traceback")


if __name__ == "__main__":
    main()
