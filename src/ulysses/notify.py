"""Ask the owner: one outbound message when an action waits for the owner's tap, one FYI when an action ran
on its own under the signed ``[autonomy]`` clause.

The message only tells a person where to look. It authorises nothing: the action still runs only after the
owner's signed tap on the card, made with the owner's key, or (``keep_control`` only) because the signed
contract's autonomy clause lists it. Nothing in the message, and nothing sent back to the URL, changes that.

One URL, ``ULYSSES_NOTIFY_URL``, in one of two shapes:

* ``json`` (the default): a POST with a JSON body whose ``text`` field is what Slack incoming webhooks show
  and whose ``content`` field is what Discord webhooks show; the same facts sit under ``ulysses`` for any
  other receiver. ``allowed_mentions`` is empty so Discord pings nobody.
* ``ntfy``: a plain-text POST with ``Title``, ``Click`` (the card URL), ``Priority`` and ``Tags`` headers.
  Chosen when the host is ``ntfy.sh`` (or a subdomain of it) or starts with ``ntfy.``, or when
  ``ULYSSES_NOTIFY_FORMAT=ntfy``.

Text that the agent, the contract's author or Flagsmith wrote (the hypothesis, an amendment reason, metric
names in the decision's reason) goes only into plainly labelled, length-limited fields, with every character
outside a small plain set replaced, so it cannot carry links, mentions or markup into a chat.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

ASK = "ask"  # an action waits for the owner's signed tap
FYI = "fyi"  # an action ran without a tap under the signed autonomy clause
TEST = "test"  # `ulysses notify test`

MAX_ATTEMPTS = 5  # per message, across restarts; then the service stops trying
ATTEMPTS_PER_TRY = 2  # in one delivery round, before waiting for the next round
TIMEOUT_SECONDS = 8.0
UNTRUSTED_LIMIT = 160  # characters of agent- or author-written text in a message
REASON_LIMIT = 300
TEXT_LIMIT = 1800  # under Discord's 2000-character content limit

GRANTS_NOTHING = ("This message grants nothing. Only the owner's signed tap on the card, made with the owner's "
                  "key, runs an action that waits for a tap.")

# Everything else in untrusted text becomes "?": no <links|text>, @mentions, markdown, control characters.
_UNSAFE = re.compile(r"[^A-Za-z0-9 .,:;!?'\"()/%+=_\-]")
_SPACE = re.compile(r"\s+")


class NotifyError(Exception):
    """Delivery failed. The text never contains the URL (a Slack or Discord URL is itself a secret)."""


def plain(text: str | None, limit: int) -> str:
    """One line of plain characters, at most ``limit`` long."""
    s = _UNSAFE.sub("?", _SPACE.sub(" ", str(text or "")).strip())
    return s if len(s) <= limit else s[: max(limit - 3, 0)].rstrip() + "..."


def shape_for(url: str, fmt: str | None = None) -> str:
    """``ntfy`` or ``json`` for this URL (``fmt`` = ``auto``, ``json`` or ``ntfy`` overrides the guess)."""
    fmt = (fmt or "auto").strip().lower()
    if fmt in ("json", "ntfy"):
        return fmt
    host = (urlparse(url).hostname or "").lower()
    return "ntfy" if host == "ntfy.sh" or host.endswith(".ntfy.sh") or host.startswith("ntfy.") else "json"


@dataclass
class Message:
    kind: str
    title: str
    lines: list[str]
    card_url: str
    facts: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        t = "\n".join([self.title, *self.lines])
        return t if len(t) <= TEXT_LIMIT else t[: TEXT_LIMIT - 3] + "..."


def _pct(x: float | None) -> str | None:
    return None if x is None else f"{x * 100:.2f}%"


def reading_line(decision: dict | None) -> str | None:
    """Flagsmith's reading the decision rests on, in one sentence (None without a look)."""
    if not decision:
        return None
    where = (f"at look {decision['look_number']} of {decision.get('looks_planned')}"
             if decision.get("look_number") else "at this read")
    parts = []
    if decision.get("chance_to_win") is not None:
        parts.append(f"{_pct(decision['chance_to_win'])} chance to win")
    if decision.get("boundary_chance") is not None:
        parts.append(f"the contract needed {_pct(decision['boundary_chance'])} to ship")
    if decision.get("identities_per_arm") is not None:
        parts.append(f"{int(decision['identities_per_arm']):,} identities in the smaller arm")
    return f"Flagsmith's reading {where}: " + (", ".join(parts) if parts else "no result yet") + "."


def build(kind: str, *, experiment: int, flag: str, owner: str, outcome: str, action: str, serving: str | None,
          decision: dict | None, card_url: str, agent_text: str | None = None,
          agent_label: str = "Hypothesis") -> Message:
    """The message for one proposal. ``agent_text`` is the only free text and is labelled as such."""
    flag_p = plain(flag, 80)
    what = plain(outcome.replace("_", " "), 40).upper()
    act = plain(action.replace("_", " "), 40)
    head = f"experiment {int(experiment)} on flag {flag_p}"
    lines: list[str] = []
    if outcome == "amendment":
        lines.append(f"The contract decided: a signed amendment authorises '{act}' now.")
    else:
        lines.append(f"The contract decided: {what}.")
        if decision and decision.get("reason"):
            lines.append("Why (the service's own words, shortened): " + plain(decision["reason"], REASON_LIMIT))
    if r := reading_line(decision):
        lines.append(r)
    if kind == FYI:
        title = f"Ulysses acted on its own under {plain(owner, 40)}'s signed clause: {head}"
        lines.append(f"The agent did '{act}' by itself: flag {flag_p} now serves {plain(serving or 'control', 60)} "
                     "to everyone. The signed [autonomy] clause lets this action run without a tap.")
        lines.append(f"Card: {card_url}")
        lines.append("Nothing is waiting for you. " + GRANTS_NOTHING)
    elif kind == TEST:
        title = "Ulysses test message: notifications reach this channel"
        lines = ["When a contract fires and an action waits for the owner's tap, a message like this one names "
                 "the experiment, the decision, Flagsmith's reading and the card to open.",
                 f"Card: {card_url}", GRANTS_NOTHING]
    else:
        title = f"Ulysses needs {plain(owner, 40)}'s tap: {head}"
        target = (f"ship {plain(serving, 60)} to everyone on {flag_p}" if action == "ship_treatment" and serving
                  else f"{act} on {flag_p}")
        lines.append(f"Waiting for you: {target}. Nothing runs until you confirm.")
        lines.append(f"Open the signed card and confirm with your key: {card_url}")
        lines.append(GRANTS_NOTHING)
    if agent_text:
        lines.append(f"{agent_label} (free text from the contract, not checked, at most {UNTRUSTED_LIMIT} "
                     f"characters): \"{plain(agent_text, UNTRUSTED_LIMIT)}\"")
    facts = {"kind": {ASK: "ask_owner", FYI: "autonomy_fyi", TEST: "test"}[kind], "experiment": int(experiment),
             "flag": flag_p, "outcome": outcome, "action": action, "serving": serving,
             "decision": plain((decision or {}).get("reason"), REASON_LIMIT) or None,
             "reading": None if not decision else {k: decision.get(k) for k in (
                 "look_number", "looks_planned", "chance_to_win", "boundary_chance", "identities_per_arm")},
             "card_url": card_url, "grants": "nothing"}
    return Message(kind=kind, title=title, lines=lines, card_url=card_url, facts=facts)


def render(msg: Message, shape: str) -> tuple[bytes, dict[str, str]]:
    """The request body and headers for one shape."""
    if shape == "ntfy":
        ascii_title = msg.title.encode("ascii", "replace").decode("ascii")[:200]
        headers = {"Content-Type": "text/plain; charset=utf-8", "Title": ascii_title,
                   "Click": msg.card_url.encode("ascii", "replace").decode("ascii"),
                   "Priority": "high" if msg.kind == ASK else "default",
                   "Tags": "raised_hand" if msg.kind == ASK else "information_source"}
        return "\n".join(msg.lines).encode("utf-8"), headers
    import json

    body = {"text": msg.text, "content": msg.text, "allowed_mentions": {"parse": []}, "ulysses": msg.facts}
    return json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}


Transport = Callable[[str, bytes, dict], int]


def http_post(url: str, body: bytes, headers: dict) -> int:
    import httpx

    try:
        r = httpx.post(url, content=body, headers=headers, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:  # its text can carry the URL; keep only the kind of failure
        raise NotifyError(type(exc).__name__) from None
    return r.status_code


class Notifier:
    """Sends one message. Holds the URL; never returned by any route or written to the store."""

    def __init__(self, url: str, *, fmt: str | None = None, transport: Transport | None = None,
                 secrets: list[str] | None = None, sleep: Callable[[float], None] = time.sleep,
                 background: bool = False, attempts_per_try: int = ATTEMPTS_PER_TRY,
                 max_attempts: int = MAX_ATTEMPTS):
        self.url = url
        self.shape = shape_for(url, fmt)
        self._transport = transport or http_post
        self._secrets = [s for s in (secrets or []) if s and len(s) >= 8]
        self.sleep = sleep
        self.background = background
        self.attempts_per_try = attempts_per_try
        self.max_attempts = max_attempts

    @property
    def channel(self) -> str:
        """Where messages go, without the secret part of the URL."""
        return "ntfy" if self.shape == "ntfy" else "webhook"

    def send(self, msg: Message) -> None:
        body, headers = render(msg, self.shape)
        blob = body + "\n".join(headers.values()).encode("utf-8", "replace")
        if any(s.encode("utf-8") in blob for s in self._secrets):
            raise NotifyError("withheld: the message would have carried a configured secret")
        try:
            status = self._transport(self.url, body, headers)
        except NotifyError:
            raise
        except Exception as exc:  # noqa: BLE001 - a transport failure is a failed delivery, nothing more
            raise NotifyError(type(exc).__name__) from None
        if not 200 <= int(status) < 300:
            raise NotifyError(f"HTTP {status}")


def from_env(env: dict, *, secrets: list[str] | None = None, background: bool = True) -> Notifier | None:
    url = (env.get("ULYSSES_NOTIFY_URL") or "").strip()
    if not url:
        return None
    if urlparse(url).scheme not in ("http", "https"):
        raise SystemExit("ULYSSES_NOTIFY_URL must be an http(s) URL")
    return Notifier(url, fmt=env.get("ULYSSES_NOTIFY_FORMAT"), secrets=secrets, background=background)
