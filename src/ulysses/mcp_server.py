"""The Ulysses MCP extension: six tools for any MCP client.

The agent needs no Flagsmith key at all. On open-source Flagsmith any key that
can read experiments is an environment admin key that can also change flags,
so Ulysses does not wire Flagsmith's official MCP server to the agent; its
reads come through the contract service instead. The five action tools:

* ``sign_contract``: draft a stop contract from the experiment and return the
  signing card for the owner;
* ``start_under_contract``: start the experiment, refused without a signed hash;
* ``look``: one results refresh and read; returns hold or fire, with the card;
* ``execute``: ask the service to carry out the pre-signed action, which it
  does only after the owner's one-tap confirmation;
* ``request_amendment``: draft a change to a signed contract, which governs
  nothing until both the owner and the second signer sign it;

and one read-only tool, ``read_experiment``, which returns Flagsmith's view of
an experiment through the service.

This process holds only ``ULYSSES_URL`` and ``ULYSSES_AGENT_TOKEN``. It never
sees a Flagsmith key, and the service never returns one.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

INSTRUCTIONS = (
    "Ulysses governs Flagsmith experiments with a signed stop contract. Use sign_contract before an "
    "experiment starts and give the owner the sign_url. Use start_under_contract only after they sign. "
    "Call look on a schedule (it enforces Flagsmith's five-minute refresh floor) and report the card. "
    "A high chance to win, or a 'Winning variation' card, is not a decision: only a look whose outcome fires is. When a look fires, "
    "give the owner the confirm_url; call execute after they confirm. You cannot ship anything the "
    "contract does not authorise. To change a signed contract, use request_amendment; an amendment needs "
    "both the owner and the second signer."
)


class ServiceClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, transport=None):
        self.base_url = (base_url or os.environ.get("ULYSSES_URL") or "http://127.0.0.1:8200").rstrip("/")
        self.token = token or os.environ.get("ULYSSES_AGENT_TOKEN") or ""
        self.http = httpx.Client(base_url=self.base_url, timeout=300, transport=transport,
                                 headers={"Authorization": f"Bearer {self.token}"})

    def call(self, method: str, path: str, **kw) -> dict:
        try:
            r = self.http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"cannot reach the Ulysses service at {self.base_url}: {exc}",
                    "next": "Start it with: ulysses serve"}
        try:
            body: Any = r.json()
        except ValueError:
            body = {"detail": r.text[:500]}
        if r.status_code >= 400:
            out = {"ok": False, "status": r.status_code}
            out.update(body if isinstance(body, dict) else {"detail": body})
            if r.status_code == 403:
                out["refused_by"] = "contract service"
            return out
        return {"ok": True, **(body if isinstance(body, dict) else {"result": body})}


def _pct(x: float | None, places: int = 1) -> str:
    from .decision import _pct as pct

    return pct(x)


def card_text(look: dict) -> str:
    """A one-screen decision card in plain text."""
    if look.get("too_soon"):
        return f"Too soon: Flagsmith refreshes results every 5 minutes. Try again in {look['retry_after_seconds']}s."
    if look.get("already_decided"):
        p = (look.get("proposals") or [{}])[-1]
        return f"Already decided: {p.get('outcome')} -> {p.get('action')} ({p.get('state')}). Confirm at {p.get('confirm_url')}"
    d = look.get("decision") or {}
    word = {"hold": "HOLD", "watch": "HOLD", "not_ready": "HOLD", "win": "FIRE: WIN", "loss": "FIRE: LOSS",
            "horizon": "FIRE: NO DECISION", "guardrail_breach": "FIRE: GUARDRAIL"}.get(d.get("outcome"), "?")
    lines = [word, d.get("reason", "")]
    if d.get("chance_to_win") is not None:
        banner = " (over 95%: Winning by the docs' rule)" if d["chance_to_win"] > 0.95 else \
                 " (under 5%: Losing by the docs' rule)" if d["chance_to_win"] < 0.05 else ""
        lines.append(f"Flagsmith chance to win: {_pct(d['chance_to_win'], 2)}{banner}")
    if d.get("boundary_chance") is not None:
        lines.append(f"Contract needs: {_pct(d['boundary_chance'], 4)} at look {d.get('look_number')} of up to {d.get('looks_planned')}")
    if look.get("note"):
        lines.append(look["note"])
    if look.get("proposal"):
        p = look["proposal"]
        lines.append(f"Pre-signed action: {p['action'].replace('_', ' ')}. Owner confirms at {p['confirm_url']}")
    lines.append(f"Card: {look.get('card_url')}")
    return "\n".join(x for x in lines if x)


def build_server(client: ServiceClient | None = None) -> MCPServer:
    svc = client or ServiceClient()
    server = MCPServer(name="ulysses", title="Ulysses stop contracts", instructions=INSTRUCTIONS, version="0.1.0")

    @server.tool(
        description=(
            "Draft a stop contract for a Flagsmith experiment that has not started, and open the signing card "
            "for its owner. You choose the hypothesis, the primary metric, the baseline and the smallest relative "
            "lift that matters; the service sizes the horizon and sets the stop rule at its default rigour. "
            "Returns the contract hash, the sign_url to give the owner, the boundary summary and the horizon plan. "
            "owner and second_signer must be enrolled signers."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )
    def sign_contract(
        environment: str,
        experiment_id: int,
        owner: str,
        second_signer: str,
        min_relative_lift: float,
        baseline_rate: float | None = None,
        baseline_mean: float | None = None,
        baseline_sd: float | None = None,
        primary_metric: str | None = None,
        hypothesis: str | None = None,
        expected_rate_per_hour: float | None = None,
    ) -> dict:
        # The agent proposes what to test and the smallest lift that matters; the service sizes the horizon
        # and applies the default rigour (alpha 0.05, 80% power). Only a person can loosen it, by signing a
        # contract with a recorded override reason.
        body = {k: v for k, v in locals().items() if k not in ("svc",) and v is not None}
        out = svc.call("POST", "/api/contracts/draft", json=body)
        if out.get("ok"):
            out["next"] = f"Ask {owner} to read and sign the contract at {out['sign_url']}, then call start_under_contract."
        return out

    @server.tool(
        description=(
            "Start the Flagsmith experiment governed by a contract. The service refuses unless the contract's "
            "owner signature verifies, it has not expired and the experiment is still 'created'. Writes the "
            "contract hash into the experiment's hypothesis in Flagsmith."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    )
    def start_under_contract(contract_hash: str) -> dict:
        return svc.call("POST", f"/api/contracts/{contract_hash}/start")

    @server.tool(
        description=(
            "Run one look: refresh Flagsmith's results (at most once per five minutes), read Flagsmith's chance "
            "to win, and compare it with the signed boundary for this look. Returns HOLD or FIRE with a card. "
            "On FIRE the pre-signed action waits for the owner's one-tap confirmation."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    )
    def look(contract_hash: str) -> dict:
        out = svc.call("POST", f"/api/contracts/{contract_hash}/look")
        if out.get("ok"):
            out["card"] = card_text(out)
        return out

    @server.tool(
        description=(
            "Ask the contract service to carry out the pre-signed action for a contract that has fired: "
            "'ship_treatment' (complete and serve the treatment to everyone on the same flag) or 'keep_control'. "
            "Refused if no look has fired, if the action is not the one the contract assigns, or if the owner "
            "has not confirmed yet (you get the confirm_url)."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
    )
    def execute(contract_hash: str, action: str) -> dict:
        return svc.call("POST", f"/api/contracts/{contract_hash}/execute", json={"action": action})

    @server.tool(
        description=(
            "Read a Flagsmith experiment through the contract service: its status, flag, variations, metrics and "
            "rollout, and Flagsmith's latest computed results (identities per arm, chance to win and lift per "
            "metric). Read-only; you never need or receive a Flagsmith key."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    )
    def read_experiment(environment: str, experiment_id: int) -> dict:
        return svc.call("GET", f"/api/flagsmith/experiments/{environment}/{int(experiment_id)}")

    @server.tool(
        description=(
            "Ask to change a signed contract, for example to act now instead of waiting for the boundary "
            "(action_now: 'ship_treatment' or 'keep_control'), or to change fields such as the horizon. This only "
            "drafts an amendment: it governs nothing and authorises nothing until both the owner and the second "
            "signer have signed it. Returns the amendment's hash and both signing links."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )
    def request_amendment(contract_hash: str, reason: str, action_now: str | None = None,
                          changes: dict | None = None) -> dict:
        body: dict[str, Any] = {"reason": reason, "changes": changes or {}}
        if action_now:
            body["action_now"] = action_now
        out = svc.call("POST", f"/api/contracts/{contract_hash}/amend", json=body)
        if out.get("ok"):
            out["next"] = ("The amendment takes effect only after both the owner and the second signer sign it: "
                           f"{out.get('sign_urls')}")
        return out

    return server


def main() -> None:
    import logging

    # Keep the agent's terminal readable: no per-request log lines from the HTTP client.
    for name in ("httpx", "httpcore", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)
    build_server().run("stdio")


if __name__ == "__main__":
    main()
