"""SQLite store for contracts, signatures, looks, proposals and the action log.

The database lives in a git-ignored run directory (default ``.run/ulysses.db``)
because it holds run-time timestamps.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
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
    token TEXT PRIMARY KEY,        -- SHA-256 of the token; the token itself is only in the link
    signer TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS signer_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signer TEXT NOT NULL,
    kind TEXT NOT NULL,            -- enrolled | rotated
    old_key TEXT,
    new_key TEXT NOT NULL,
    seq INTEGER NOT NULL,          -- rotations of this signer before this one
    signature TEXT,                -- for a rotation: the old key's signature over signing.rotation_message
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS service_faults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    environment TEXT,
    experiment_id INTEGER,
    contract_hash TEXT,
    what TEXT NOT NULL,            -- e.g. status_reverted, rollout_changed_by_note
    detail TEXT NOT NULL,
    repaired INTEGER NOT NULL DEFAULT 0,
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

log = logging.getLogger("ulysses")

BUSY_TIMEOUT_MS = 10_000

MIGRATIONS = [
    ("deviations", "note_state", "ALTER TABLE deviations ADD COLUMN note_state TEXT"),
    ("deviations", "note", "ALTER TABLE deviations ADD COLUMN note TEXT"),
    ("deviations", "kind", "ALTER TABLE deviations ADD COLUMN kind TEXT"),
    ("deviations", "source", "ALTER TABLE deviations ADD COLUMN source TEXT"),
    ("audit_seen", "expected_id", "ALTER TABLE audit_seen ADD COLUMN expected_id INTEGER"),
    ("audit_seen", "detail", "ALTER TABLE audit_seen ADD COLUMN detail TEXT"),
    # A token row without an expiry (made before expiries existed) never validates.
    ("enrol_tokens", "expires_at", "ALTER TABLE enrol_tokens ADD COLUMN expires_at REAL"),
]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None,
                                   timeout=BUSY_TIMEOUT_MS / 1000)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            # Several service processes may share one file: readers do not block the writer under WAL,
            # and a writer waits for the other's transaction instead of failing at once.
            self._db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            for table, column, sql in MIGRATIONS:
                cols = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in cols:
                    self._db.execute(sql)
            self._index_looks()

    def _index_looks(self) -> None:
        """One look per sequence number per experiment, so two processes cannot record the same look twice.

        A database written before the constraint may already hold a repeated number; renumbering would
        break the owner's signed evidence chain over the looks, so such a file keeps a plain index and
        relies on the counting transaction alone.
        """
        try:
            self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS looks_experiment_seq "
                             "ON looks(environment, experiment_id, seq)")
        except sqlite3.IntegrityError:
            log.warning("looks table has repeated sequence numbers; keeping a non-unique index")
            self._db.execute("CREATE INDEX IF NOT EXISTS looks_experiment_seq_plain "
                             "ON looks(environment, experiment_id, seq)")

    # ---- helpers -------------------------------------------------------------

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, tuple(args))

    # Rows are fetched while the lock is held: the connection is shared between threads, and a
    # fetch interleaved with another thread's statement can come back empty.
    def _one(self, sql: str, args: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            row = self._db.execute(sql, tuple(args)).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self._db.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def transaction(self):
        return _Tx(self)

    # ---- signers ---------------------------------------------------------------

    def add_signer(self, name: str, public_key: str, now: float) -> None:
        """Register a new signer. Never replaces a registered key: that takes ``rotate_signer``."""
        with self._lock:
            self._exec("INSERT OR IGNORE INTO signers(name, public_key, added_at) VALUES (?,?,?)",
                       (name, public_key, now))
            row = self.signer(name)
            if not row or row["public_key"] != public_key:
                raise ValueError(f"signer {name!r} is already registered with a different key; "
                                 "a new key needs a rotation signed by the old one")

    def signer(self, name: str) -> dict | None:
        return self._one("SELECT * FROM signers WHERE name=?", (name,))

    def signers(self) -> list[dict]:
        return self._all("SELECT name, public_key FROM signers ORDER BY name")

    def rotate_signer(self, name: str, old_key: str, new_key: str, seq: int, signature: str, now: float) -> bool:
        """Swap ``name``'s key from ``old_key`` to ``new_key`` if it is still ``old_key`` and ``seq`` rotations
        came before; record the event. False if either moved on meanwhile."""
        with self.transaction():
            if self.rotation_count(name) != seq:
                return False
            cur = self._exec("UPDATE signers SET public_key=?, added_at=? WHERE name=? AND public_key=?",
                             (new_key, now, name, old_key))
            if cur.rowcount != 1:
                return False
            self._exec("INSERT INTO signer_events(signer, kind, old_key, new_key, seq, signature, at) "
                       "VALUES (?,?,?,?,?,?,?)", (name, "rotated", old_key, new_key, seq, signature, now))
            return True

    def rotation_count(self, name: str) -> int:
        r = self._one("SELECT COUNT(*) AS c FROM signer_events WHERE signer=? AND kind='rotated'", (name,))
        return int((r or {"c": 0})["c"])

    def log_signer_event(self, name: str, kind: str, new_key: str, now: float) -> None:
        self._exec("INSERT INTO signer_events(signer, kind, old_key, new_key, seq, signature, at) "
                   "VALUES (?,?,?,?,?,?,?)", (name, kind, None, new_key, self.rotation_count(name), None, now))

    def signer_events(self, names: Iterable[str] | None = None) -> list[dict]:
        rows = self._all("SELECT signer, kind, old_key, new_key, seq, signature, at FROM signer_events ORDER BY id")
        if names is not None:
            wanted = set(names)
            rows = [r for r in rows if r["signer"] in wanted]
        return rows

    def new_enrol_token(self, signer: str, now: float, ttl: float) -> str:
        token = secrets.token_urlsafe(32)
        self._exec("INSERT INTO enrol_tokens(token, signer, created_at, expires_at) VALUES (?,?,?,?)",
                   (_token_hash(token), signer, now, now + ttl))
        return token

    def signer_by_key(self, public_key: str) -> dict | None:
        return self._one("SELECT * FROM signers WHERE public_key=?", (public_key,))

    def use_enrol_token(self, token: str, now: float) -> str | None:
        with self._lock:
            name = self.peek_enrol_token(token, now)
            if not name:
                return None
            cur = self._exec("UPDATE enrol_tokens SET used=1 WHERE token=? AND used=0", (_token_hash(token),))
            return name if cur.rowcount == 1 else None

    def peek_enrol_token(self, token: str, now: float) -> str | None:
        row = self._one("SELECT signer FROM enrol_tokens WHERE token=? AND used=0 AND expires_at IS NOT NULL "
                        "AND expires_at > ?", (_token_hash(token), now))
        return row["signer"] if row else None

    # ---- the service's own secret ----------------------------------------------

    def service_secret(self) -> bytes:
        """A random secret made on first use and kept here; it signs the human pages' read links."""
        with self._lock:
            self._exec("INSERT OR IGNORE INTO meta(key, value) VALUES ('read_secret', ?)", (secrets.token_hex(32),))
            return bytes.fromhex(self._one("SELECT value FROM meta WHERE key='read_secret'")["value"])

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

    def contract_state(self, h: str) -> str | None:
        row = self._one("SELECT state FROM contracts WHERE hash=?", (h,))
        return row["state"] if row else None

    def contract_states(self, environment: str, experiment_id: int) -> list[dict]:
        """Hash and state of every contract on an experiment, oldest first (no contract text parsed)."""
        return self._all("SELECT hash, state FROM contracts WHERE environment=? AND experiment_id=? "
                         "ORDER BY created_at", (environment, experiment_id))

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
            seq = (self._one("SELECT COALESCE(MAX(seq), 0) AS c FROM looks WHERE environment=? AND experiment_id=?",
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

    def cas_proposal(self, pid: str, from_states: Iterable[str], now: float, **fields: Any) -> bool:
        """Update a proposal only while its state is one of ``from_states``; True if it changed."""
        fs = tuple(from_states)
        sets = ", ".join(f"{k}=?" for k in fields)
        q = ",".join("?" * len(fs))
        cur = self._exec(f"UPDATE proposals SET {sets}, updated_at=? WHERE id=? AND state IN ({q})",
                         (*fields.values(), now, pid, *fs))
        return cur.rowcount == 1

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

    def add_service_fault(self, *, environment: str, experiment_id: int, contract_hash: str | None, what: str,
                          detail: str, repaired: bool, now: float) -> int:
        """The service's own write damaged Flagsmith state. Kept apart from deviations (not someone else's change)."""
        cur = self._exec(
            "INSERT INTO service_faults(environment, experiment_id, contract_hash, what, detail, repaired, at) "
            "VALUES (?,?,?,?,?,?,?)",
            (environment, experiment_id, contract_hash, what, detail, 1 if repaired else 0, now))
        return int(cur.lastrowid)

    def service_faults(self) -> list[dict]:
        return self._all("SELECT * FROM service_faults ORDER BY id")

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
