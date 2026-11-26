"""Read capabilities for the human pages, and the signer's browser session.

The pages under ``/``, ``/sign``, ``/card`` and ``/human/...`` show contracts, Flagsmith's numbers and
signer names. Nobody reads them by reaching the port alone. A read needs one of:

* a capability: ``?cap=<expiry>.<mac>`` on a link the service minted (the ``sign_url``, ``card_url``
  and ``confirm_url`` it hands the agent, or the overview link the operator prints). The MAC is
  HMAC-SHA256 with the service's own secret over the purpose, the scope and the expiry, so a link
  cannot be widened to another experiment or kept past its expiry. A scope is one experiment
  (every contract in its chain and every card on it) or ``*`` (the overview, for the operator);
* a signer session: a cookie set when a signer enrols a key, signed the same way, naming the signer
  and the key it was set for. It reads the experiments whose contracts name that signer, and stops
  working when the key is rotated.

The secret is random, made on first use and kept in the service's database, so links survive a restart
and are worthless to anyone without that database. Nothing here is a timestamp in a committed file:
expiries are computed at run time from the service clock.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

ALL = "*"
PURPOSE_READ = "ulysses/v1/read-capability"
PURPOSE_SESSION = "ulysses/v1/signer-session"


def experiment_scope(environment: str, experiment_id: int) -> str:
    return f"exp:{environment}:{int(experiment_id)}"


def _mac(secret: bytes, *fields: str) -> str:
    raw = hmac.new(secret, "\n".join(fields).encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def mint(secret: bytes, scope: str, expires: float) -> str:
    exp = str(int(expires))
    return f"{exp}.{_mac(secret, PURPOSE_READ, scope, exp)}"


def valid(secret: bytes, scope: str, token: str | None, now: float) -> bool:
    """True if ``token`` grants ``scope`` (or everything) and has not expired."""
    if not token or "." not in token:
        return False
    exp, _, mac = token.partition(".")
    if not exp.isdigit() or int(exp) <= now:
        return False
    for s in (scope, ALL) if scope != ALL else (ALL,):
        if hmac.compare_digest(mac, _mac(secret, PURPOSE_READ, s, exp)):
            return True
    return False


def _key_id(public_key: str) -> str:
    return hashlib.sha256(public_key.encode("utf-8")).hexdigest()[:16]


def session(secret: bytes, signer: str, public_key: str, expires: float) -> str:
    exp = str(int(expires))
    name = base64.urlsafe_b64encode(signer.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{name}.{exp}.{_mac(secret, PURPOSE_SESSION, signer, _key_id(public_key), exp)}"


def session_signer(secret: bytes, token: str | None, now: float, key_of) -> str | None:
    """The signer a session cookie names, if it is genuine, unexpired and for their current key."""
    if not token or token.count(".") != 2:
        return None
    name_b64, exp, mac = token.split(".")
    if not exp.isdigit() or int(exp) <= now:
        return None
    try:
        signer = base64.urlsafe_b64decode(name_b64 + "=" * (-len(name_b64) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    key = key_of(signer)
    if not key:
        return None
    if not hmac.compare_digest(mac, _mac(secret, PURPOSE_SESSION, signer, _key_id(key), exp)):
        return None
    return signer
