"""SQLite store for contracts, signatures, looks, proposals and the action log.

The database lives in a git-ignored run directory (default ``.run/ulysses.db``)
because it holds run-time timestamps.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS signers (
    name TEXT PRIMARY KEY,
    public_key TEXT NOT NULL UNIQUE,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS enrol_tokens (
    token TEXT PRIMARY KEY,
    signer TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS contracts (
    hash TEXT PRIMARY KEY,
    canonical TEXT NOT NULL,
    environment TEXT NOT NULL,
    experiment_id INTEGER NOT NULL,
    supersedes TEXT,
    state TEXT NOT NULL,           -- draft | signed | active | decided | executed | superseded | void
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS signatures (
    contract_hash TEXT NOT NULL,
    role TEXT NOT NULL,            -- owner | second_signer
    signer TEXT NOT NULL,
    signature TEXT NOT NULL,
    received_at REAL NOT NULL,
    PRIMARY KEY (contract_hash, role)
);
CREATE TABLE IF NOT EXISTS looks (
    id TEXT PRIMARY KEY,
    contract_hash TEXT NOT NULL,
    environment TEXT NOT NULL,
    experiment_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    counted INTEGER NOT NULL,
    identities_per_arm INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    decision TEXT NOT NULL,
    snapshot TEXT,
    refreshed INTEGER NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    contract_hash TEXT NOT NULL,
    look_id TEXT,
    outcome TEXT NOT NULL,
    action TEXT NOT NULL,
    state TEXT NOT NULL,           -- pending | confirmed | executed | failed
    confirm_signer TEXT,
    confirm_signature TEXT,
    result TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    environment TEXT NOT NULL,
    experiment_id INTEGER NOT NULL,
    kind TEXT NOT NULL,            -- update_hypothesis | start | complete
    contract_hash TEXT,
    proposal_id TEXT,
    detail TEXT,
    ok INTEGER NOT NULL DEFAULT 1,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refusals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    what TEXT NOT NULL,
    reason TEXT NOT NULL,
    contract_hash TEXT,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deviations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    environment TEXT,
    experiment_id INTEGER,
    contract_hash TEXT,
    audit_id TEXT,
    log TEXT NOT NULL,
    author TEXT,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_entries (
    audit_id TEXT PRIMARY KEY,
    entry TEXT NOT NULL,
    received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_seen (
    audit_id TEXT PRIMARY KEY,
    verdict TEXT NOT NULL,
    action_id INTEGER
);
CREATE TABLE IF NOT EXISTS expected_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    environment TEXT NOT NULL,
    experiment_id INTEGER NOT NULL,
    kind TEXT NOT NULL,            -- start | complete | update_hypothesis
    contract_hash TEXT,
    proposal_id TEXT,
    expect TEXT NOT NULL,          -- what Flagsmith must show afterwards (JSON)
    also TEXT NOT NULL,            -- side-effect audit kinds Flagsmith writes for this call (JSON list)
    created_at REAL NOT NULL,
    valid_until REAL NOT NULL,
    matched_audit_id TEXT,
    landed INTEGER                 -- NULL unknown, 1 read back as applied, 0 read back as not applied
);
CREATE TABLE IF NOT EXISTS governed (
    environment TEXT NOT NULL,
    experiment_id INTEGER NOT NULL,
    contract_hash TEXT NOT NULL,
    feature_id INTEGER,
    flag TEXT NOT NULL,
    option_ids TEXT,
    rollout TEXT,                  -- the experiment's rollout as Flagsmith reported it at start (JSON)
    PRIMARY KEY (environment, experiment_id)
);
"""

MIGRATIONS = [
    ("deviations", "note_state", "ALTER TABLE deviations ADD COLUMN note_state TEXT"),
    ("deviations", "note", "ALTER TABLE deviations ADD COLUMN note TEXT"),
    ("deviations", "kind", "ALTER TABLE deviations ADD COLUMN kind TEXT"),
    ("deviations", "source", "ALTER TABLE deviations ADD COLUMN source TEXT"),
    ("audit_seen", "expected_id", "ALTER TABLE audit_seen ADD COLUMN expected_id INTEGER"),
    ("audit_seen", "detail", "ALTER TABLE audit_seen ADD COLUMN detail TEXT"),
]


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            for table, column, sql in MIGRATIONS:
                cols = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in cols:
                    self._db.execute(sql)

    # ---- helpers -------------------------------------------------------------

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, tuple(args))

    def _one(self, sql: str, args: Iterable[Any] = ()) -> dict | None:
        row = self._exec(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self._exec(sql, args).fetchall()]

    def transaction(self):
        return _Tx(self)

    # ---- signers ---------------------------------------------------------------

    def add_signer(self, name: str, public_key: str, now: float) -> None:
        self._exec(
            "INSERT INTO signers(name, public_key, added_at) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET public_key=excluded.public_key, added_at=excluded.added_at",
            (name, public_key, now),
        )

    def signer(self, name: str) -> dict | None:
        return self._one("SELECT * FROM signers WHERE name=?", (name,))

    def signers(self) -> list[dict]:
        return self._all("SELECT name, public_key FROM signers ORDER BY name")

    def new_enrol_token(self, signer: str, now: float) -> str:
        token = uuid.uuid4().hex
        self._exec("INSERT INTO enrol_tokens(token, signer, created_at) VALUES (?,?,?)", (token, signer, now))
        return token

    def signer_by_key(self, public_key: str) -> dict | None:
        return self._one("SELECT * FROM signers WHERE public_key=?", (public_key,))

    def use_enrol_token(self, token: str) -> str | None:
        with self._lock:
            row = self._one("SELECT * FROM enrol_tokens WHERE token=? AND used=0", (token,))
            if not row:
                return None
            self._exec("UPDATE enrol_tokens SET used=1 WHERE token=?", (token,))
            return row["signer"]

    def peek_enrol_token(self, token: str) -> str | None:
        row = self._one("SELECT signer FROM enrol_tokens WHERE token=? AND used=0", (token,))
        return row["signer"] if row else None

    # ---- contracts -------------------------------------------------------------

    def put_contract(self, h: str, canonical: dict, environment: str, experiment_id: int,
                     supersedes: str | None, now: float) -> bool:
        """Insert a draft. Returns False if the same contract already exists."""
        with self._lock:
            if self._one("SELECT hash FROM contracts WHERE hash=?", (h,)):
                return False
            self._exec(
                "INSERT INTO contracts(hash, canonical, environment, experiment_id, supersedes, state, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (h, json.dumps(canonical, sort_keys=True), environment, experiment_id, supersedes, "draft", now),
            )
            return True

    def contract_row(self, h: str) -> dict | None:
        row = self._one("SELECT * FROM contracts WHERE hash=?", (h,))
        if row:
            row["canonical"] = json.loads(row["canonical"])
        return row

    def set_contract_state(self, h: str, state: str) -> None:
        self._exec("UPDATE contracts SET state=? WHERE hash=?", (state, h))

    def cas_contract_state(self, h: str, from_states: Iterable[str], to: str) -> bool:
        """Change state only if it is currently one of ``from_states``; True if it changed."""
        fs = tuple(from_states)
        q = ",".join("?" * len(fs))
        cur = self._exec(f"UPDATE contracts SET state=? WHERE hash=? AND state IN ({q})", (to, h, *fs))
        return cur.rowcount == 1

    def governing_contract(self, environment: str, experiment_id: int) -> dict | None:
        row = self._one(
            "SELECT * FROM contracts WHERE environment=? AND experiment_id=? "
            "AND state IN ('starting','active','decided','executing','executed') ORDER BY created_at DESC LIMIT 1",
            (environment, experiment_id),
        )
        if row:
            row["canonical"] = json.loads(row["canonical"])
        return row

    def contracts_for(self, environment: str, experiment_id: int) -> list[dict]:
        rows = self._all(
            "SELECT * FROM contracts WHERE environment=? AND experiment_id=? ORDER BY created_at",
            (environment, experiment_id),
        )
        for r in rows:
            r["canonical"] = json.loads(r["canonical"])
        return rows

    def all_contracts(self) -> list[dict]:
        return self._all(
            "SELECT hash, environment, experiment_id, supersedes, state, created_at FROM contracts ORDER BY created_at DESC"
        )

    def governed_experiments(self) -> list[tuple[str, int]]:
        return [
            (r["environment"], r["experiment_id"])
            for r in self._all(
                "SELECT DISTINCT environment, experiment_id FROM contracts "
                "WHERE state IN ('starting','active','decided','executing','executed','superseded','deviated')"
            )
        ]

    # ---- signatures --------------------------------------------------------------

    def add_signature(self, h: str, role: str, signer: str, signature: str, now: float) -> bool:
        """Record a signature. The first one for a role is kept: a replay never moves its time."""
        cur = self._exec(
            "INSERT OR IGNORE INTO signatures(contract_hash, role, signer, signature, received_at) VALUES (?,?,?,?,?)",
            (h, role, signer, signature, now),
        )
        return cur.rowcount == 1

    def signatures(self, h: str) -> dict[str, dict]:
        return {r["role"]: r for r in self._all("SELECT * FROM signatures WHERE contract_hash=?", (h,))}

    # ---- looks ---------------------------------------------------------------------

    def add_look(self, *, contract_hash: str, environment: str, experiment_id: int, counted: bool,
                 identities_per_arm: int, outcome: str, decision: dict, snapshot: dict | None,
                 refreshed: bool, now: float) -> str:
        with self._lock:
            seq = (self._one("SELECT COUNT(*) AS c FROM looks WHERE environment=? AND experiment_id=?",
                             (environment, experiment_id)) or {"c": 0})["c"] + 1
            look_id = f"L{seq:04d}-{uuid.uuid4().hex[:8]}"
            self._exec(
                "INSERT INTO looks(id, contract_hash, environment, experiment_id, seq, counted, identities_per_arm, "
                "outcome, decision, snapshot, refreshed, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (look_id, contract_hash, environment, experiment_id, seq, int(counted), identities_per_arm,
                 outcome, json.dumps(decision), json.dumps(snapshot) if snapshot is not None else None,
                 int(refreshed), now),
            )
            return look_id

    def looks(self, environment: str, experiment_id: int) -> list[dict]:
        rows = self._all("SELECT * FROM looks WHERE environment=? AND experiment_id=? ORDER BY seq",
                         (environment, experiment_id))
        for r in rows:
            r["decision"] = json.loads(r["decision"])
            r["snapshot"] = json.loads(r["snapshot"]) if r["snapshot"] else None
        return rows

    def look(self, look_id: str) -> dict | None:
        r = self._one("SELECT * FROM looks WHERE id=?", (look_id,))
        if r:
            r["decision"] = json.loads(r["decision"])
            r["snapshot"] = json.loads(r["snapshot"]) if r["snapshot"] else None
        return r

    def last_refreshed_look_at(self, environment: str, experiment_id: int) -> float | None:
        r = self._one("SELECT MAX(at) AS t FROM looks WHERE environment=? AND experiment_id=? AND refreshed=1",
                      (environment, experiment_id))
        return r["t"] if r else None

    # ---- proposals -------------------------------------------------------------------

    def add_proposal(self, *, contract_hash: str, look_id: str | None, outcome: str, action: str,
                     state: str, now: float) -> str:
        pid = "P-" + uuid.uuid4().hex[:12]
        self._exec(
            "INSERT INTO proposals(id, contract_hash, look_id, outcome, action, state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (pid, contract_hash, look_id, outcome, action, state, now, now),
        )
        return pid

    def proposal(self, pid: str) -> dict | None:
        return self._one("SELECT * FROM proposals WHERE id=?", (pid,))

    def proposals_for(self, contract_hash: str) -> list[dict]:
        return self._all("SELECT * FROM proposals WHERE contract_hash=? ORDER BY created_at", (contract_hash,))

    def update_proposal(self, pid: str, now: float, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE proposals SET {sets}, updated_at=? WHERE id=?", (*fields.values(), now, pid))

    # ---- action log, refusals, deviations ----------------------------------------------

    def log_action(self, *, environment: str, experiment_id: int, kind: str, contract_hash: str | None,
                   proposal_id: str | None, detail: Any, now: float) -> int:
        cur = self._exec(
            "INSERT INTO actions(environment, experiment_id, kind, contract_hash, proposal_id, detail, at) "
            "VALUES (?,?,?,?,?,?,?)",
            (environment, experiment_id, kind, contract_hash, proposal_id, json.dumps(detail), now),
        )
        return int(cur.lastrowid)

    def mark_action_ok(self, action_id: int) -> None:
        self._exec("UPDATE actions SET ok=1 WHERE id=?", (action_id,))

    def mark_action_failed(self, action_id: int) -> None:
        self._exec("UPDATE actions SET ok=0 WHERE id=?", (action_id,))

    def actions(self, environment: str | None = None, experiment_id: int | None = None) -> list[dict]:
        if environment is None:
            rows = self._all("SELECT * FROM actions ORDER BY id")
        else:
            rows = self._all("SELECT * FROM actions WHERE environment=? AND experiment_id=? ORDER BY id",
                             (environment, experiment_id))
        for r in rows:
            r["detail"] = json.loads(r["detail"]) if r["detail"] else None
        return rows

    def log_refusal(self, what: str, reason: str, contract_hash: str | None, now: float) -> None:
        self._exec("INSERT INTO refusals(what, reason, contract_hash, at) VALUES (?,?,?,?)",
                   (what, reason, contract_hash, now))

    def refusals(self) -> list[dict]:
        return self._all("SELECT * FROM refusals ORDER BY id")

    def add_deviation(self, *, environment: str | None, experiment_id: int | None, contract_hash: str | None,
                      audit_id: Any, log: str, author: str | None, now: float, kind: str | None = None,
                      source: str = "audit webhook", note: str | None = None) -> int:
        cur = self._exec(
            "INSERT INTO deviations(environment, experiment_id, contract_hash, audit_id, log, author, at, kind, "
            "source, note, note_state) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (environment, experiment_id, contract_hash, None if audit_id is None else str(audit_id), log, author,
             now, kind, source, note, "pending" if note else "n/a"),
        )
        return int(cur.lastrowid)

    def pending_notes(self) -> list[dict]:
        return self._all("SELECT * FROM deviations WHERE note_state='pending' ORDER BY id")

    def set_note_state(self, deviation_id: int, state: str) -> None:
        self._exec("UPDATE deviations SET note_state=? WHERE id=?", (state, deviation_id))

    # ---- expected writes: what the service is about to do to Flagsmith ---------------

    def add_expected_write(self, *, environment: str, experiment_id: int, kind: str, contract_hash: str | None,
                           proposal_id: str | None, expect: dict, also: list[str], now: float,
                           window: float) -> int:
        cur = self._exec(
            "INSERT INTO expected_writes(environment, experiment_id, kind, contract_hash, proposal_id, expect, also, "
            "created_at, valid_until) VALUES (?,?,?,?,?,?,?,?,?)",
            (environment, experiment_id, kind, contract_hash, proposal_id, json.dumps(expect), json.dumps(also),
             now, now + window),
        )
        return int(cur.lastrowid)

    def expected_writes(self, environment: str, experiment_id: int) -> list[dict]:
        rows = self._all("SELECT * FROM expected_writes WHERE environment=? AND experiment_id=? ORDER BY id",
                         (environment, experiment_id))
        for r in rows:
            r["expect"] = json.loads(r["expect"])
            r["also"] = json.loads(r["also"])
        return rows

    def expected_write(self, ew_id: int) -> dict | None:
        r = self._one("SELECT * FROM expected_writes WHERE id=?", (ew_id,))
        if r:
            r["expect"] = json.loads(r["expect"])
            r["also"] = json.loads(r["also"])
        return r

    def claim_expected_write(self, ew_id: int, audit_id: Any) -> bool:
        """One-time match of an audit entry to an expected write."""
        cur = self._exec("UPDATE expected_writes SET matched_audit_id=? WHERE id=? AND matched_audit_id IS NULL",
                         (str(audit_id), ew_id))
        return cur.rowcount == 1

    def set_expected_landed(self, ew_id: int, landed: bool) -> None:
        self._exec("UPDATE expected_writes SET landed=? WHERE id=?", (1 if landed else 0, ew_id))

    # ---- governed flags ------------------------------------------------------------------

    def set_governed(self, *, environment: str, experiment_id: int, contract_hash: str, feature_id: int | None,
                     flag: str, option_ids: list, rollout: Any) -> None:
        self._exec(
            "INSERT OR REPLACE INTO governed(environment, experiment_id, contract_hash, feature_id, flag, option_ids, "
            "rollout) VALUES (?,?,?,?,?,?,?)",
            (environment, experiment_id, contract_hash, feature_id, flag, json.dumps(option_ids),
             json.dumps(rollout, sort_keys=True, default=str)),
        )

    def governed_row(self, environment: str, experiment_id: int) -> dict | None:
        r = self._one("SELECT * FROM governed WHERE environment=? AND experiment_id=?", (environment, experiment_id))
        if r:
            r["option_ids"] = json.loads(r["option_ids"] or "[]")
            r["rollout"] = json.loads(r["rollout"]) if r["rollout"] else None
        return r

    def governed_rows(self) -> list[dict]:
        rows = self._all("SELECT * FROM governed")
        for r in rows:
            r["option_ids"] = json.loads(r["option_ids"] or "[]")
            r["rollout"] = json.loads(r["rollout"]) if r["rollout"] else None
        return rows

    def deviations(self, environment: str | None = None, experiment_id: int | None = None) -> list[dict]:
        if environment is None:
            return self._all("SELECT * FROM deviations ORDER BY id")
        return self._all("SELECT * FROM deviations WHERE environment=? AND experiment_id=? ORDER BY id",
                         (environment, experiment_id))

    def keep_audit_entry(self, audit_id: Any, entry: dict, now: float) -> None:
        self._exec("INSERT OR IGNORE INTO audit_entries(audit_id, entry, received_at) VALUES (?,?,?)",
                   (str(audit_id), json.dumps(entry, default=str), now))

    def kept_audit_entries(self) -> list[dict]:
        return [json.loads(r["entry"]) for r in self._all("SELECT entry FROM audit_entries")]

    def audit_verdict(self, audit_id: Any) -> dict | None:
        return self._one("SELECT * FROM audit_seen WHERE audit_id=?", (str(audit_id),))

    def set_audit_verdict(self, audit_id: Any, verdict: str, action_id: int | None,
                          expected_id: int | None = None, detail: dict | None = None) -> None:
        self._exec("INSERT OR REPLACE INTO audit_seen(audit_id, verdict, action_id, expected_id, detail) "
                   "VALUES (?,?,?,?,?)",
                   (str(audit_id), verdict, action_id, expected_id, json.dumps(detail) if detail else None))

    def audit_verdicts(self) -> dict[str, dict]:
        out = {}
        for r in self._all("SELECT * FROM audit_seen"):
            r["detail"] = json.loads(r["detail"]) if r.get("detail") else None
            out[r["audit_id"]] = r
        return out

    def matched_action_ids(self) -> set[int]:
        return {r["action_id"] for r in self._all("SELECT action_id FROM audit_seen WHERE action_id IS NOT NULL")}


class _Tx:
    """BEGIN/COMMIT around a group of writes. Re-entrant: an inner block joins the outer one."""

    def __init__(self, store: Store):
        self.store = store

    def __enter__(self):
        st = self.store
        st._lock.acquire()
        depth = getattr(st, "_tx_depth", 0)
        if depth == 0:
            st._db.execute("BEGIN IMMEDIATE")
        st._tx_depth = depth + 1
        return st

    def __exit__(self, exc_type, exc, tb):
        st = self.store
        try:
            st._tx_depth -= 1
            if st._tx_depth == 0:
                st._db.execute("COMMIT" if exc_type is None else "ROLLBACK")
        finally:
            st._lock.release()
        return False
