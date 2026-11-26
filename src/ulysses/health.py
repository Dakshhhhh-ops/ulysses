"""Flagsmith Feature Health, both ways.

Out: when a look records a guardrail breach or a sample-ratio mismatch, or the service records a
deviation, the service posts ``unhealthy`` for the experiment's flag to its own Feature Health
"Webhook" provider in Flagsmith (created by ``ulysses lab setup``). The flag then carries Flagsmith's
Unhealthy tag and shows the reason and a link to the decision card. When the condition clears (a later
look finds the split back within the signed test) or the experiment is completed under its contract,
the service posts ``healthy``, but only over an ``unhealthy`` it sent itself: Webhook events carry no
external id, so every caller of a project's Webhook provider shares one latest-event slot per flag
and environment, and a ``healthy`` sent on its own could hide another caller's alert.

A report that cannot reach Flagsmith never breaks the look or the execution it came from: it is tried a
few times with backoff, then kept as pending and sent, in order, before the next report.

In: a contract that signs ``[health] block_ship_when_unhealthy = true`` refuses to ship the treatment
while Flagsmith's ``GET /projects/<p>/feature-health/events/`` holds a latest UNHEALTHY event for the
flag in the experiment's environment (or in all environments) from any provider. Keeping control is
never blocked: it is the rollback. If the events cannot be read, a ship is refused too, since the
signed rule could not be checked.

The endpoints and payload shapes are in ``flagsmith/health.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

import httpx

from .canonical import short_hash
from .flagsmith import health as fh
from .flagsmith.gateway import ExperimentInfo, FlagsmithError

log = logging.getLogger("ulysses.health")

SCHEMA = """
CREATE TABLE IF NOT EXISTS health_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    environment TEXT NOT NULL,
    experiment_id INTEGER NOT NULL,
    contract_hash TEXT,
    flag TEXT NOT NULL,
    status TEXT NOT NULL,          -- unhealthy | healthy
    causes TEXT NOT NULL,          -- JSON list: guardrail_breach, sample_ratio, deviation, completed, cleared
    payload TEXT NOT NULL,         -- the body for Flagsmith's webhook, minus the environment name
    state TEXT NOT NULL,           -- pending | sent | rejected | gave_up
    attempts INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    last_error TEXT,
    at REAL NOT NULL
);
"""

MAX_TOTAL_ATTEMPTS = 20  # a pending report is retried before later reports until this many tries


def _http_post(url: str, body: dict, timeout: float = 5.0) -> int:
    # No Authorization header: the signed path is the credential, and the admin key never goes to a URL
    # that was handed back by an API response.
    r = httpx.post(url, json=body, timeout=timeout)
    return r.status_code


class FeatureHealth:
    def __init__(self, store, gateway, *, webhook_url: str | None, public_url: str,
                 clock: Callable[[], float], sleep: Callable[[float], None],
                 post: Callable[[str, dict], int] | None = None, attempts: int = 3, backoff: float = 0.5):
        self.store = store
        self._fs = gateway
        self.webhook_url = webhook_url or None
        self.public_url = public_url.rstrip("/")
        self.clock = clock
        self.sleep = sleep
        self.post = post or _http_post
        self.attempts = max(1, attempts)
        self.backoff = backoff
        self._env_cache: dict[str, fh.EnvironmentInfo] = {}
        with store._lock:
            store._db.executescript(SCHEMA)

    # ------------------------------------------------------------------ reading

    def _environment(self, env_key: str) -> fh.EnvironmentInfo:
        if env_key not in self._env_cache:
            getter = getattr(self._fs, "environment_info", None)
            if getter is None:
                raise FlagsmithError("this Flagsmith client cannot read environments")
            self._env_cache[env_key] = getter(env_key)
        return self._env_cache[env_key]

    def state(self, env_key: str, exp: ExperimentInfo) -> dict:
        """Flagsmith's Feature Health for the experiment's flag in its environment. Raises FlagsmithError."""
        reader = getattr(self._fs, "health_events", None)
        if reader is None:
            raise FlagsmithError("this Flagsmith client cannot read Feature Health events")
        env = self._environment(env_key)
        events = reader(env.project)
        bad = fh.unhealthy_for(events, exp.feature_id, env.id)
        mine = [e for e in events if e.feature == exp.feature_id]
        return {
            "state": "unhealthy" if bad else "healthy",
            "environment": env.name,
            "feature_id": exp.feature_id,
            "events_for_flag": len(mine),
            "unhealthy": [{"provider": e.provider_name, "scope": "all environments" if e.environment is None
                           else "this environment"} for e in bad],
            "_titles": [t for e in bad for t in e.titles()],
        }

    def check_ship(self, contract, exp: ExperimentInfo, action: str) -> dict | None:
        """The signed Feature Health rule at execution. None when the contract does not sign it.

        Returns the record to keep with the executed outcome; ``blocked`` with ``detail`` means refuse.
        """
        rule = getattr(contract, "health", None)
        if rule is None or not rule.block_ship_when_unhealthy:
            return None
        ref = contract.experiment
        rec: dict[str, Any] = {"rule": "block_ship_when_unhealthy", "action": action}
        try:
            st = self.state(ref.environment, exp)
        except FlagsmithError as exc:
            rec.update(state="unreadable", error=str(exc)[:200])
            if action == "ship_treatment":
                rec.update(blocked=True, detail=(
                    "the contract signs block_ship_when_unhealthy, and Flagsmith's Feature Health could not be "
                    f"read ({exc}), so the rule cannot be checked and nothing was shipped. Try again shortly."))
            return rec
        titles = st.pop("_titles")
        rec.update(st)
        if st["state"] == "unhealthy" and action == "ship_treatment":
            who = ", ".join(sorted({u["provider"] or "unknown provider" for u in st["unhealthy"]}))
            rec.update(blocked=True, detail=(
                f"Flagsmith's Feature Health reports flag {ref.flag!r} unhealthy in {st['environment']!r} "
                f"(provider: {who}" + (f"; {'; '.join(titles[:3])}" if titles else "") + "). The contract "
                "signs block_ship_when_unhealthy, so the treatment is not shipped while that stands. Clear or "
                "dismiss the alert in Flagsmith (or let its provider report healthy), then execute again. "
                "Keeping control is not blocked."))
        return rec

    # ------------------------------------------------------------------ reporting

    def _last(self, env_key: str, experiment_id: int) -> dict | None:
        return self.store._one(
            "SELECT * FROM health_reports WHERE environment=? AND experiment_id=? AND state != 'rejected' "
            "ORDER BY id DESC LIMIT 1", (env_key, experiment_id))

    def reports(self, env_key: str | None = None, experiment_id: int | None = None) -> list[dict]:
        if env_key is None:
            rows = self.store._all("SELECT * FROM health_reports ORDER BY id")
        else:
            rows = self.store._all("SELECT * FROM health_reports WHERE environment=? AND experiment_id=? ORDER BY id",
                                   (env_key, experiment_id))
        for r in rows:
            r["payload"] = json.loads(r["payload"])
            r["causes"] = json.loads(r["causes"])
        return rows

    def _queue(self, contract, h: str, status: str, causes: list[str], blocks: list[dict], url: str,
               url_title: str) -> dict | None:
        if not self.webhook_url:
            return None
        ref = contract.experiment
        payload = {"feature": ref.flag, "status": status,
                   "reason": {"text_blocks": blocks, "url_blocks": [{"url": url, "title": url_title}]}}
        cur = self.store._exec(
            "INSERT INTO health_reports (environment, experiment_id, contract_hash, flag, status, causes, payload, "
            "state, at) VALUES (?,?,?,?,?,?,?,?,?)",
            (ref.environment, ref.experiment, h, ref.flag, status, json.dumps(causes), json.dumps(payload),
             "pending", self.clock()))
        rid = cur.lastrowid
        self.drain()
        row = self.store._one("SELECT * FROM health_reports WHERE id=?", (rid,))
        out = {"reported": status, "causes": causes, "delivered": row["state"] == "sent", "state": row["state"],
               "attempts": row["attempts"], "flagsmith_status": row["http_status"]}
        if row["last_error"] and row["state"] != "sent":
            out["detail"] = row["last_error"]
        return out

    def drain(self) -> int:
        """Send pending reports in order; stop at the first that still cannot be delivered."""
        if not self.webhook_url:
            return 0
        sent = 0
        for row in self.store._all("SELECT * FROM health_reports WHERE state='pending' ORDER BY id"):
            if not self._deliver(row):
                break
            sent += 1
        return sent

    def _deliver(self, row: dict) -> bool:
        """True when the row is settled (sent or rejected); False when it stays pending."""
        body = json.loads(row["payload"])
        tries = row["attempts"]
        error, status = None, None
        for i in range(self.attempts):
            if i:
                self.sleep(self.backoff * (2 ** (i - 1)))
            tries += 1
            try:
                env = self._environment(row["environment"])
                status = int(self.post(self.webhook_url, {**body, "environment": env.name}))
            except (FlagsmithError, httpx.HTTPError, OSError) as exc:
                text = str(exc).replace(self.webhook_url, fh.redact_webhook(self.webhook_url) or "")
                error = f"cannot reach Flagsmith's Feature Health webhook: {type(exc).__name__}: {text}"[:300]
                status = None
                continue
            if 200 <= status < 300:
                self._settle(row["id"], "sent", tries, status, None)
                return True
            if status in (400, 404):
                # Not worth retrying: the payload names no feature in the provider's project, or the signed
                # URL is not a provider (rotated or deleted). Kept so the operator can see it.
                self._settle(row["id"], "rejected", tries, status,
                             f"Flagsmith answered {status} to the Feature Health webhook "
                             + ("(no such provider: run `ulysses lab setup` again)" if status == 404
                                else "(flag not found in the provider's project, or payload refused)"))
                log.warning("feature health report %s refused by Flagsmith (%s)", row["id"], status)
                return True
            error = f"Flagsmith answered {status} to the Feature Health webhook"
        state = "gave_up" if tries >= MAX_TOTAL_ATTEMPTS else "pending"
        self._settle(row["id"], state, tries, status, error)
        log.warning("feature health report %s not delivered after %s tries: %s", row["id"], tries, error)
        return state == "gave_up"

    def _settle(self, rid: int, state: str, attempts: int, status: int | None, error: str | None) -> None:
        self.store._exec("UPDATE health_reports SET state=?, attempts=?, http_status=?, last_error=? WHERE id=?",
                         (state, attempts, status, error, rid))

    def _safely(self, fn, *a) -> dict | None:
        try:
            return fn(*a)
        except Exception as exc:  # noqa: BLE001 - a health report never breaks a look or an execution
            log.warning("feature health report failed: %s", exc)
            return {"reported": None, "delivered": False, "detail": f"{type(exc).__name__}: {exc}"[:300]}

    # ---- hooks the contract service calls --------------------------------------------

    def after_look(self, contract, h: str, look_id: str, decision: dict) -> dict | None:
        return self._safely(self._after_look, contract, h, look_id, decision)

    def _after_look(self, contract, h: str, look_id: str, decision: dict) -> dict | None:
        if not self.webhook_url:
            return None
        self.drain()
        ref = contract.experiment
        causes, blocks = [], []
        if decision.get("outcome") == "guardrail_breach":
            causes.append("guardrail_breach")
            blocks.append({"title": f"Ulysses: guardrail {decision.get('guardrail')!r} breached",
                           "text": f"{decision.get('reason')} Experiment {ref.experiment}, contract {short_hash(h)}."})
        sr = decision.get("sample_ratio") or {}
        if sr.get("state") == "mismatch":
            causes.append("sample_ratio")
            obs = ", ".join(f"{k} {v}" for k, v in (sr.get("observed") or {}).items())
            want = ", ".join(f"{k} {v:.1%}" for k, v in (sr.get("expected_share") or {}).items())
            blocks.append({"title": "Ulysses: sample-ratio mismatch",
                           "text": (f"Flagsmith counted {obs} identities against the signed split {want}; the "
                                    f"chi-square p-value {sr.get('p_value'):.2g} is below the signed threshold "
                                    f"{sr.get('threshold'):g}. A win is held while it stands. Experiment "
                                    f"{ref.experiment}, contract {short_hash(h)}.")})
        card = f"{self.public_url}/card/{look_id}"
        last = self._last(ref.environment, ref.experiment)
        if causes:
            if last and last["status"] == "unhealthy" and json.loads(last["causes"]) == causes \
                    and "guardrail_breach" not in causes:
                return {"reported": None, "detail": "already reported unhealthy for the same cause"}
            return self._queue(contract, h, "unhealthy", causes, blocks, card, "Ulysses decision card")
        if last and last["status"] == "unhealthy" and set(json.loads(last["causes"])) <= {"sample_ratio"}:
            return self._queue(contract, h, "healthy", ["cleared"], [{
                "title": "Ulysses: sample ratio back within the signed split",
                "text": f"Look on experiment {ref.experiment} found the split consistent with contract {short_hash(h)}."}],
                card, "Ulysses decision card")
        return None

    def after_deviation(self, contract, h: str, kind: str, text: str) -> dict | None:
        return self._safely(self._after_deviation, contract, h, kind, text)

    def _after_deviation(self, contract, h: str, kind: str, text: str) -> dict | None:
        if not self.webhook_url:
            return None
        ref = contract.experiment
        return self._queue(contract, h, "unhealthy", ["deviation"], [{
            "title": f"Ulysses: {kind.replace('_', ' ')} outside the signed contract",
            "text": (f"{text} Experiment {ref.experiment} changed outside contract {short_hash(h)}; recorded as a "
                     f"deviation. Check it: ulysses verify {ref.experiment}")}],
            f"{self.public_url}/sign/{h}", "Ulysses contract")

    def after_execute(self, contract, h: str, look_id: str | None, action: str, serving: str) -> dict | None:
        return self._safely(self._after_execute, contract, h, look_id, action, serving)

    def _after_execute(self, contract, h: str, look_id: str | None, action: str, serving: str) -> dict | None:
        if not self.webhook_url:
            return None
        self.drain()
        ref = contract.experiment
        last = self._last(ref.environment, ref.experiment)
        if not last or last["status"] != "unhealthy" or "deviation" in json.loads(last["causes"]):
            return None
        url = f"{self.public_url}/card/{look_id}" if look_id else f"{self.public_url}/sign/{h}"
        return self._queue(contract, h, "healthy", ["completed"], [{
            "title": "Ulysses: experiment completed under its contract",
            "text": (f"Executed {action.replace('_', ' ')} after the owner's signed confirmation; flag {ref.flag} "
                     f"now serves {serving} to everyone, so the arm behind the earlier alert is no longer served. "
                     f"Contract {short_hash(h)}. Check it: ulysses verify {ref.experiment}")}],
            url, "Ulysses decision card")
