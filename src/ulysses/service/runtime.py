"""Build the contract service from environment variables and serve it."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import httpx

from ..core import Settings, Ulysses
from ..flagsmith.rest import RestFlagsmith
from ..store import Store
from .app import create_app

DEFAULT_PORT = 8200
log = logging.getLogger("ulysses.service")


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env reader (KEY=VALUE lines); real environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def slack_notifier(url: str, public_url: str):
    def notify(event: str, payload: dict) -> None:
        if event == "fired":
            text = (f"*Ulysses: contract fired ({payload['outcome'].replace('_', ' ')})*\n{payload['reason']}\n"
                    f"Confirm the pre-signed action: {payload['card_url']}")
        elif event == "executed":
            text = f"*Ulysses executed* {payload['action'].replace('_', ' ')}: flag now serves `{payload['serving']}`."
        elif event == "deviation":
            text = f"*Ulysses deviation:* {payload['log']} by {payload.get('author') or 'an API key'} (outside contract)."
        elif event == "started":
            text = f"*Ulysses:* experiment {payload['experiment']} started under contract {payload['hash'][:19]}."
        else:
            text = f"Ulysses {event}: {payload}"
        httpx.post(url, json={"text": text}, timeout=10)

    return notify


def link_settings() -> dict:
    """How long read links, enrolment links and signer sessions last (seconds), from the environment."""
    out = {}
    for env, field in (("ULYSSES_LINK_TTL_SECONDS", "link_ttl_seconds"),
                       ("ULYSSES_ENROL_TTL_SECONDS", "enrol_ttl_seconds"),
                       ("ULYSSES_SESSION_TTL_SECONDS", "session_ttl_seconds")):
        if os.environ.get(env):
            out[field] = float(os.environ[env])
    return out


def local_service(public_url: str | None = None, db_path: str | Path | None = None) -> Ulysses:
    """The service's rules over its own database, without Flagsmith: for minting links and enrolment tokens
    on the service's machine (``ulysses link``, ``ulysses signer enrol``, the demo scripts)."""
    from ..flagsmith.fake import FakeFlagsmith

    url = (public_url or os.environ.get("ULYSSES_PUBLIC_URL") or f"http://127.0.0.1:{DEFAULT_PORT}").rstrip("/")
    store = Store(db_path or os.environ.get("ULYSSES_DB", ".run/ulysses.db"))
    return Ulysses(store, FakeFlagsmith(), Settings(public_url=url, **link_settings()))


def build(db_path: str | Path | None = None) -> tuple[Ulysses, dict]:
    url = os.environ.get("FLAGSMITH_API_URL", "http://localhost:8000")
    key = os.environ.get("FLAGSMITH_ADMIN_KEY", "")
    if not key:
        raise SystemExit("FLAGSMITH_ADMIN_KEY is not set. The contract service needs Flagsmith's organisation "
                         "admin API key (only the service ever holds it). See .env.example.")
    token = os.environ.get("ULYSSES_AGENT_TOKEN", "")
    if len(token) < 16:
        raise SystemExit("ULYSSES_AGENT_TOKEN must be set (16+ characters). Run `ulysses init` to create one.")
    public_url = os.environ.get("ULYSSES_PUBLIC_URL", f"http://127.0.0.1:{DEFAULT_PORT}").rstrip("/")
    slack = os.environ.get("ULYSSES_SLACK_WEBHOOK") or None
    health_url = os.environ.get("ULYSSES_HEALTH_WEBHOOK_URL") or None
    settings = Settings(public_url=public_url, slack_webhook=slack, health_webhook_url=health_url,
                        # Kept well under the 300 s client timeout so a slow look does not invite a retry.
                        refresh_wait_seconds=float(os.environ.get("ULYSSES_REFRESH_WAIT", "150")),
                        **link_settings())
    store = Store(db_path or os.environ.get("ULYSSES_DB", ".run/ulysses.db"))
    svc = Ulysses(store, RestFlagsmith(url, key), settings,
                  notify=slack_notifier(slack, public_url) if slack else None)
    return svc, {"agent_token": token, "flagsmith_key": key, "webhook_secret": os.environ.get("ULYSSES_WEBHOOK_SECRET") or None,
                 "public_url": public_url, "health_webhook_url": health_url}


def start_poller(svc: Ulysses, every_seconds: float) -> threading.Thread:
    """Take the looks on the service's own schedule, so they do not wait for an agent to ask."""

    def run() -> None:
        while True:
            try:
                svc.poll_once()
            except Exception as exc:  # noqa: BLE001 - the poller must outlive one bad round
                log.warning("scheduled looks failed: %s", exc)
            time.sleep(every_seconds)

    t = threading.Thread(target=run, name="ulysses-poller", daemon=True)
    t.start()
    return t


def make_asgi(poll_seconds: float | None = None):
    load_dotenv()
    svc, cfg = build()
    if poll_seconds:
        log.info("taking looks on schedule every %.0f s (subject to each contract's refresh floor)", poll_seconds)
        start_poller(svc, poll_seconds)
    insecure = os.environ.get("ULYSSES_WEBHOOK_INSECURE") == "1"
    if not cfg["webhook_secret"]:
        log.warning("ULYSSES_WEBHOOK_SECRET is not set: %s", "accepting UNSIGNED audit webhooks (dev only)"
                    if insecure else "the audit webhook is disabled until it is (deviation detection by webhook off)")
    # The operator's way in: the contract list needs a read capability like every other human page.
    print(f"Ulysses contract list (read link, expires in {svc.settings.link_ttl_seconds / 3600:g} h): "
          f"{svc.overview_link()}", flush=True)
    return create_app(svc, agent_token=cfg["agent_token"],
                      protected_secrets=[cfg["flagsmith_key"], cfg["webhook_secret"] or "",
                                         cfg["health_webhook_url"] or ""],
                      webhook_secret=cfg["webhook_secret"], webhook_insecure=insecure)


def default_port() -> int:
    """The port in ULYSSES_PUBLIC_URL, so the links the service hands out match where it listens."""
    from urllib.parse import urlparse

    u = urlparse(os.environ.get("ULYSSES_PUBLIC_URL", ""))
    return u.port or DEFAULT_PORT


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT, poll_seconds: float | None = None) -> None:
    import uvicorn

    if not (8200 <= port <= 8299):
        log.warning("port %s is outside Ulysses' usual range 8200-8299", port)
    uvicorn.run(make_asgi(poll_seconds), host=host, port=port, log_level="info")
