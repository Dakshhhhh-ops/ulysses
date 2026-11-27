"""The contract service over HTTP.

Two surfaces with different authentication:

* ``/api/...`` is the agent surface. It needs ``Authorization: Bearer
  <ULYSSES_AGENT_TOKEN>`` and can read, draft, propose and ask; every write to
  Flagsmith behind it passes the checks in ``core.py``. Nothing it returns ever
  contains the Flagsmith key (a response guard refuses to send a body that
  does).
* ``/sign``, ``/card``, ``/enrol`` and ``/human/...`` are the signing surface.
  A write there (sign, confirm, enrol, rotate) is authorised by an Ed25519
  signature from a registered signer, made in the signer's browser (WebCrypto,
  non-extractable key) or by the ``ulysses sign`` command, or by a one-time
  enrolment link. A read there (the contract list, contract pages, curves and
  cards) needs a read capability on the link the service minted, the cookie
  that link leaves, or the signer session set at enrolment
  (``ulysses.capability``); the agent token is not a read grant there.

``/webhooks/flagsmith`` receives Flagsmith's organisation audit-log webhook.
It refuses every post unless a shared secret is configured and the body's
``X-Flagsmith-Signature`` HMAC matches (an explicit insecure flag exists for a
throwaway dev stack only), and it records lifecycle and flag changes made
outside a contract as deviations, after checking Flagsmith really shows them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .. import capability
from .. import contract as ct
from .. import horizon as hz
from ..canonical import short_hash
from ..core import Invalid, NotFound, Ulysses, UlyssesError
from ..flagsmith.gateway import AuditEntry, FlagsmithError
from ..flagsmith.rest import parse_audit
from . import ui

log = logging.getLogger("ulysses.service")
HERE = Path(__file__).parent
READ_REFUSED = ("this page needs the link the contract service printed (it carries a read capability), or the "
                "browser that enrolled a signer named in the contract")


class SecretGuard:
    """ASGI middleware: never let a response body carry a configured secret.

    Defence in depth only: it matches the secrets' exact bytes, so an encoded or split-up copy passes. The
    real defence is that no route returns the key at all. A whole response (one body message, as every JSON
    route sends) is checked before anything is sent and withheld with a 500 if it matches. A streamed
    response is passed on as it comes, holding back only the last few bytes of each chunk (one secret's
    length) so a secret split across chunks is still seen; if one appears the stream is cut there.
    """

    def __init__(self, app, secrets_: list[str]):
        self.app = app
        self.secrets = [s.encode() for s in secrets_ if s and len(s) >= 8]
        self.hold = max((len(s) for s in self.secrets), default=1) - 1

    def _leaks(self, data: bytes) -> bool:
        return any(s in data for s in self.secrets)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.secrets:
            return await self.app(scope, receive, send)
        start: dict[str, Any] = {}
        state = {"streaming": False, "tail": b"", "cut": False}

        async def capture(message):
            if message["type"] == "http.response.start":
                start.update(message)
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            if state["cut"]:
                return
            body, more = message.get("body", b""), bool(message.get("more_body"))
            if not state["streaming"] and not more:
                # The whole response in one message: check it before a byte goes out.
                if self._leaks(body):
                    log.error("blocked a response that contained a secret: %s", scope.get("path"))
                    body = b'{"error":"Redacted","detail":"response withheld: it contained a secret"}'
                    headers = [(k, v) for k, v in start.get("headers", [])
                               if k.lower() not in (b"content-length", b"content-type")]
                    headers += [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
                    await send({"type": "http.response.start", "status": 500, "headers": headers})
                else:
                    await send(start)
                await send({"type": "http.response.body", "body": body})
                return
            if not state["streaming"]:
                state["streaming"] = True
                await send(start)
            data = state["tail"] + body
            if self._leaks(data):
                log.error("cut a streamed response that contained a secret: %s", scope.get("path"))
                state["cut"] = True
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            if more:
                keep = min(self.hold, len(data))
                out, state["tail"] = data[:len(data) - keep], data[len(data) - keep:]
            else:
                out, state["tail"] = data, b""
            await send({"type": "http.response.body", "body": out, "more_body": more})

        return await self.app(scope, receive, capture)


class ReadHeaders:
    """ASGI middleware for the human pages: no Referer and no caching, and the cookies a route asked for.

    A capability link must not leak to another site through the Referer header or sit in a shared cache. A
    route that accepted a capability (or enrolled a key) leaves the cookies to set in ``scope["state"]``; they
    are added to the response headers here, without touching the body (so a streamed body still streams).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        human = path == "/" or path.startswith(("/sign", "/card", "/human/", "/enrol"))

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                state = scope.get("state") or {}
                cookies = {n: (v, a) for n, v, a in [*state.get("read_cookies", []), *state.get("session_cookies", [])]}
                for name, (value, max_age) in cookies.items():
                    headers.append((b"set-cookie", (f"{name}={value}; Max-Age={int(max_age)}; Path=/; HttpOnly; "
                                                    "SameSite=Lax").encode()))
                if human:
                    headers += [(b"referrer-policy", b"no-referrer"), (b"cache-control", b"no-store")]
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, wrapped)


# ---- request bodies --------------------------------------------------------------------


class DraftBody(BaseModel):
    environment: str
    experiment_id: int
    owner: str
    second_signer: str
    min_relative_lift: float
    baseline_rate: float | None = None
    baseline_mean: float | None = None
    baseline_sd: float | None = None
    primary_metric: str | None = None
    hypothesis: str | None = None
    alpha: float = 0.05
    power: float = 0.8
    looks: int | None = None
    identities_per_arm: int | None = None
    expected_rate_per_hour: float | None = None
    start_within_days: int = 30
    # How long the contract governs from the experiment's start, e.g. "30d" or "36h". None: no expiry.
    expires_after: str | None = None
    guardrail_alpha: float = 0.01
    override_reason: str | None = None
    # Actions the agent may run without the owner's tap (only keep_control); the owner signs this clause.
    without_confirmation: list[str] | None = None


class SubmitBody(BaseModel):
    toml: str | None = None
    contract: dict | None = None


class AmendBody(BaseModel):
    changes: dict = {}
    reason: str
    action_now: str | None = None


class ExecuteBody(BaseModel):
    action: str | None = None


class SignBody(BaseModel):
    hash: str
    role: str
    signer: str
    signature: str


class ConfirmBody(BaseModel):
    proposal_id: str
    signer: str
    signature: str


class EnrolBody(BaseModel):
    token: str
    public_key: str


class RotateBody(BaseModel):
    signer: str
    public_key: str
    signature: str


SESSION_COOKIE = "ulysses_session"
READ_COOKIE = "ulysses_read_"


def create_app(
    svc: Ulysses,
    *,
    agent_token: str,
    protected_secrets: list[str] | None = None,
    webhook_secret: str | None = None,
    webhook_insecure: bool = False,
) -> Any:
    """``webhook_insecure`` accepts unsigned audit webhooks; only for a throwaway local dev stack."""
    if not agent_token or len(agent_token) < 16:
        raise ValueError("ULYSSES_AGENT_TOKEN must be set to at least 16 characters")
    app = FastAPI(title="Ulysses contract service", version="0.1.0", docs_url="/api/docs", redoc_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["short"] = short_hash
    ui.setup(templates)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    ui.register(app, svc, templates)

    def agent(authorization: str = Header(default="")) -> None:
        scheme, _, tok = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(tok.encode(), agent_token.encode()):
            raise HTTPException(status_code=401, detail="agent token required (Authorization: Bearer <token>)")

    # ---- read authorisation for the human pages ------------------------------------------

    def _cookie_for(scope: str) -> str:
        return READ_COOKIE + ("all" if scope == capability.ALL else hashlib.sha256(scope.encode()).hexdigest()[:16])

    def _signer_reads(signer: str, scope: str) -> bool:
        _, env, eid = scope.split(":", 2)
        for row in svc.store.contracts_for(env, int(eid)):
            names = {(row["canonical"].get(r) or {}).get("name") for r in ("owner", "second_signer")}
            if signer in names:
                return True
        return False

    def _granted(request: Request, scope: str) -> bool:
        cap = request.query_params.get("cap")
        if cap and svc.read_allowed(scope, cap):
            exp = int(cap.split(".", 1)[0])
            name = _cookie_for(capability.ALL if svc.read_allowed(capability.ALL, cap) else scope)
            request.state.read_cookies = [*getattr(request.state, "read_cookies", []),
                                          (name, cap, max(int(exp - svc.clock()), 1))]
            return True
        for name in (_cookie_for(scope), _cookie_for(capability.ALL)):
            if svc.read_allowed(scope, request.cookies.get(name)):
                return True
        signer = svc.session_signer(request.cookies.get(SESSION_COOKIE))
        return bool(signer and scope != capability.ALL and _signer_reads(signer, scope))

    def _scope_of_contract(h: str) -> str | None:
        row = svc.store.contract_row(h)
        return svc.read_scope(row["environment"], row["experiment_id"]) if row else None

    def _scope_of_card(ident: str) -> str | None:
        rec = svc.store.proposal(ident) if ident.startswith("P-") else svc.store.look(ident)
        return _scope_of_contract(rec["contract_hash"]) if rec else None

    def _require(request: Request, scope: str | None) -> None:
        """Raise 401 unless this request may read ``scope``; an unknown id is a 404 only to an overview grant."""
        if scope is None:
            if _granted(request, capability.ALL):
                raise NotFound("nothing with that id on this service")
            raise HTTPException(status_code=401, detail=READ_REFUSED)
        if not _granted(request, scope):
            raise HTTPException(status_code=401, detail=READ_REFUSED)

    def _locked(request: Request):
        return templates.TemplateResponse(request, "locked.html", {}, status_code=401)

    @app.exception_handler(UlyssesError)
    async def _ulysses_error(_: Request, exc: UlyssesError):
        return JSONResponse(exc.to_dict(), status_code=exc.status)

    @app.exception_handler(FlagsmithError)
    async def _flagsmith_error(_: Request, exc: FlagsmithError):
        return JSONResponse({"error": "FlagsmithError", "detail": str(exc), "flagsmith_status": exc.status},
                            status_code=502)

    # ---- agent surface ------------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/api/contracts/draft", dependencies=[Depends(agent)])
    def draft(body: DraftBody):
        return svc.draft_for_experiment(**body.model_dump())

    @app.post("/api/contracts", dependencies=[Depends(agent)])
    def submit(body: SubmitBody):
        if body.toml:
            return svc.submit(body.toml)
        if body.contract:
            return svc.submit(body.contract)
        raise Invalid("send either toml or contract")

    @app.get("/api/contracts", dependencies=[Depends(agent)])
    def list_contracts():
        return svc.store.all_contracts()

    @app.get("/api/contracts/{h}", dependencies=[Depends(agent)])
    def status(h: str):
        return svc.contract_status(h)

    @app.get("/api/contracts/{h}/toml", dependencies=[Depends(agent)], response_class=PlainTextResponse)
    def contract_toml(h: str):
        return ct.to_toml(svc.contract(h))

    @app.post("/api/contracts/{h}/start", dependencies=[Depends(agent)])
    def start(h: str):
        return svc.start_under_contract(h)

    @app.post("/api/contracts/{h}/look", dependencies=[Depends(agent)])
    def look(h: str):
        return svc.look(h)

    @app.post("/api/contracts/{h}/amend", dependencies=[Depends(agent)])
    def amend(h: str, body: AmendBody):
        return svc.amend(h, body.changes, body.reason, body.action_now)

    @app.post("/api/contracts/{h}/execute", dependencies=[Depends(agent)])
    def execute_contract(h: str, body: ExecuteBody):
        if not body.action:
            raise Invalid("say which action you are asking for")
        return svc.execute_for_contract(h, body.action)

    @app.get("/api/proposals/{pid}", dependencies=[Depends(agent)])
    def proposal(pid: str):
        return svc.proposal(pid)

    @app.post("/api/proposals/{pid}/execute", dependencies=[Depends(agent)])
    def execute(pid: str, body: ExecuteBody | None = None):
        return svc.execute(pid, requested_action=body.action if body else None)

    @app.get("/api/looks/{look_id}", dependencies=[Depends(agent)])
    def look_detail(look_id: str):
        return svc.look_view(look_id)

    @app.get("/api/experiments/{environment}/{experiment_id}", dependencies=[Depends(agent)])
    def experiment(environment: str, experiment_id: int):
        return svc.experiment_view(environment, experiment_id)

    @app.get("/api/flagsmith/experiments/{environment}/{experiment_id}", dependencies=[Depends(agent)])
    def flagsmith_read(environment: str, experiment_id: int):
        """Read-only proxy: Flagsmith's experiment, results and exposure rate. GET only, no key returned."""
        return svc.read_experiment(environment, experiment_id)

    @app.get("/api/horizon", dependencies=[Depends(agent)])
    def horizon(min_relative_lift: float, baseline_rate: float | None = None, baseline_mean: float | None = None,
                baseline_sd: float | None = None, alpha: float = 0.05, power: float = 0.8,
                looks: int | None = None, exposure_rate_per_hour: float | None = None):
        try:
            return hz.plan(min_relative_lift=min_relative_lift, baseline_rate=baseline_rate,
                           baseline_mean=baseline_mean, baseline_sd=baseline_sd, alpha=alpha, power=power,
                           looks=looks, exposure_rate_per_hour=exposure_rate_per_hour).to_dict()
        except ValueError as exc:
            raise Invalid(str(exc)) from exc

    @app.get("/api/audit/replay", dependencies=[Depends(agent)])
    def replay(project: int):
        return svc.audit_replay(project)

    @app.post("/api/audit/sync", dependencies=[Depends(agent)])
    def sync(project: int):
        return svc.sync_audit(project)

    @app.get("/api/deviations", dependencies=[Depends(agent)])
    def deviations():
        return svc.store.deviations()

    @app.get("/api/service-faults", dependencies=[Depends(agent)])
    def service_faults():
        """Damage the service's own writes did to Flagsmith state (kept apart from deviations)."""
        return svc.store.service_faults()

    @app.get("/api/refusals", dependencies=[Depends(agent)])
    def refusals():
        return svc.store.refusals()

    @app.get("/api/signers/events", dependencies=[Depends(agent)])
    def signer_events():
        """Every registration, enrolment and key rotation, with the old key's signature on each rotation."""
        return svc.store.signer_events()

    # ---- Flagsmith audit-log webhook ---------------------------------------------------

    @app.post("/webhooks/flagsmith")
    async def flagsmith_webhook(request: Request):
        # Fail closed: without a shared secret nobody can tell Flagsmith's posts from anyone else's.
        if not webhook_secret and not webhook_insecure:
            raise HTTPException(status_code=503, detail="webhook secret not configured: set ULYSSES_WEBHOOK_SECRET "
                                "(and the same secret on Flagsmith's organisation webhook)")
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HTTPException(status_code=415, detail="content-type must be application/json")
        raw = await request.body()
        if webhook_secret:
            sig = request.headers.get("X-Flagsmith-Signature", "")
            want = hmac.new(webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, want):
                raise HTTPException(status_code=401, detail="bad X-Flagsmith-Signature")
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="body is not JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        if body.get("event_type") != "AUDIT_LOG_CREATED":
            return {"ignored": body.get("event_type")}
        data = body.get("data")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="data must be a JSON object")
        entry: AuditEntry = parse_audit(data)
        return svc.handle_audit(entry)

    # ---- signing surface: writes authorised by signatures, reads by capabilities -----------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        rows = svc.store.all_contracts()
        if not _granted(request, capability.ALL):
            scopes = {r["hash"]: svc.read_scope(r["environment"], r["experiment_id"]) for r in rows}
            rows = [r for r in rows if _granted(request, scopes[r["hash"]])]
            has_any = (any(k.startswith(READ_COOKIE) for k in request.cookies) or request.query_params.get("cap")
                       or svc.session_signer(request.cookies.get(SESSION_COOKIE)))
            if not rows and not has_any:
                return _locked(request)
        return templates.TemplateResponse(request, "index.html", {"contracts": rows, **ui.index_context(svc, rows)})

    def _contract_data(h: str) -> dict:
        st = svc.contract_status(h)
        c = svc.contract(h)
        from ..signing import contract_message

        st["messages"] = {role: contract_message(h, role).decode() for role in ("owner", "second_signer")}
        st["planned"] = [p.model_dump() for p in c.boundary.planned]
        st["toml"] = ct.to_toml(c)
        return st

    @app.get("/human/contract/{h}")
    def human_contract(request: Request, h: str):
        _require(request, _scope_of_contract(h))
        return _contract_data(h)

    @app.get("/human/contract/{h}/curves")
    def human_curves(request: Request, h: str):
        """Power and expected-sample-size curves of a contract's boundary (ulysses.power)."""
        from .. import power

        _require(request, _scope_of_contract(h))
        return power.curves_for_contract(svc.contract(h))

    @app.get("/sign/{h}", response_class=HTMLResponse)
    def sign_page(request: Request, h: str, role: str = "owner"):
        scope = _scope_of_contract(h)
        if scope is None or not _granted(request, scope):
            if scope is None and _granted(request, capability.ALL):
                return templates.TemplateResponse(request, "missing.html", {"what": "contract", "id": h},
                                                  status_code=404)
            return _locked(request)
        try:
            data = _contract_data(h)
        except NotFound:
            return templates.TemplateResponse(request, "missing.html", {"what": "contract", "id": h}, status_code=404)
        return templates.TemplateResponse(request, "sign.html", {"c": data, "role": role, "h": h,
                                                                 **ui.sign_context(svc, data)})

    @app.post("/human/sign")
    def human_sign(body: SignBody):
        return svc.sign(body.hash, body.role, body.signer, body.signature)

    def _card_data(ident: str) -> dict:
        if ident.startswith("P-"):
            return {"kind": "proposal", **svc.proposal_card(ident)}
        return {"kind": "look", **svc.look_view(ident)}

    @app.get("/human/card/{ident}")
    def human_card(request: Request, ident: str):
        _require(request, _scope_of_card(ident))
        return _card_data(ident)

    @app.get("/card/{ident}", response_class=HTMLResponse)
    def card_page(request: Request, ident: str):
        scope = _scope_of_card(ident)
        if scope is None or not _granted(request, scope):
            if scope is None and _granted(request, capability.ALL):
                return templates.TemplateResponse(request, "missing.html", {"what": "card", "id": ident},
                                                  status_code=404)
            return _locked(request)
        try:
            data = _card_data(ident)
        except NotFound:
            return templates.TemplateResponse(request, "missing.html", {"what": "card", "id": ident}, status_code=404)
        return templates.TemplateResponse(request, "card.html", {"d": data, "ident": ident,
                                                                 **ui.card_context(svc, data)})

    @app.post("/human/confirm")
    def human_confirm(body: ConfirmBody):
        return svc.confirm(body.proposal_id, body.signer, body.signature)

    @app.get("/enrol/{token}", response_class=HTMLResponse)
    def enrol_page(request: Request, token: str):
        # Showing the page spends nothing (link previews GET it); only the POST with the key does.
        name = svc.store.peek_enrol_token(token, svc.clock())
        if name and svc.store.signer(name):
            name = None  # a registered name keeps its key; the POST would be refused, so offer nothing here
        return templates.TemplateResponse(request, "enrol.html", {"token": token, "name": name},
                                          status_code=200 if name else 404)

    @app.post("/human/enrol")
    def human_enrol(request: Request, body: EnrolBody):
        out = svc.enrol_with_token(body.token, body.public_key)
        # This browser now holds the signer's key: let it read the experiments whose contracts name them.
        request.state.session_cookies = [(SESSION_COOKIE, svc.signer_session(out["name"]),
                                          int(svc.settings.session_ttl_seconds))]
        return out

    @app.post("/human/rotate")
    def human_rotate(body: RotateBody):
        """Hand a signer's name to a new key; the request must be signed by the current key."""
        return svc.rotate_signer_key(body.signer, body.public_key, body.signature)

    @app.get("/sign", include_in_schema=False)
    def sign_root():
        return RedirectResponse("/")

    return SecretGuard(ReadHeaders(app), protected_secrets or [])
