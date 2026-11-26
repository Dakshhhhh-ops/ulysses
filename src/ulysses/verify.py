"""Audit a Ulysses decision without trusting the contract service.

``export_bundle`` gathers everything a decision rests on into one JSON file:
the contract text of every contract in the experiment's chain, the signatures,
the public keys the service registered, every look with the numbers Flagsmith
returned, the executed outcome and Flagsmith's own final state of the
experiment. It holds no timestamps and no keys beyond the signers' public keys.

``verify_bundle`` re-derives the decision from that file alone:

* each contract's hash is recomputed from its TOML text;
* the experiment's hypothesis in Flagsmith names the governing contract and
  every amendment, and every contract hash it names is in the bundle;
* every signature (owner, second signer on an amendment, the owner's one-tap
  confirmation) is checked against the public key named in the contract; the
  owner's tap on a fired look also covers a hash chain over every look up to
  that one (``signing.evidence_hash``), so editing or dropping one of those
  looks after the tap breaks it;
* every look is re-evaluated from Flagsmith's numbers with the signed rule,
  recomputing the boundary from the contract; the recorded decision must match;
* a look that fires must have its outcome in the bundle, and no counted look
  may follow it under the same contract;
* each executed outcome must be one a look actually fired (or a doubly signed
  amendment authorised), its action the one the contract assigns, and the
  variation Flagsmith ended up serving the one that action ships.

What the numbers alone cannot prove. An edit is caught when it makes a recorded
decision disagree with the bundle's own numbers, or points an outcome at a look
that did not fire. A consistent rewrite of a look's numbers (hold to win
included), or a deleted look, is caught only through the owner's tap: a tap in
the current form covers every look up to the one that fired; a tap in the
earlier form does not (its evidence line is SKIP). Neither proves the numbers
are the ones Flagsmith returned, only that they are the ones the owner signed.

In ``--bundle`` mode every source is the exporter's: the contract text, the key
registry and the Flagsmith lines (named "as exported"). A bundle built end to
end with fresh keys is self-consistent, so it is VERIFIED only when something
from outside pins it (``expect_hash``, a trusted key for every signer, or
Flagsmith read live); otherwise the result is CONSISTENT (unpinned), exit 2.

The checks print one line each, PASS, FAIL or SKIP (a check that needs data the
bundle does not carry, such as Flagsmith's final state).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from . import contract as ct
from . import signing
from .canonical import short_hash
from .decision import FIRES, CountedLook, _pct, counted_look, evaluate, observation_from_snapshot
from .flagsmith.gateway import CONTROL_KEY, FlagsmithGateway, MetricResult, ResultsSnapshot

BUNDLE_VERSION = 1
MARK = "[Ulysses]"
GOVERNING_STATES = ("starting", "active", "decided", "executing", "executed", "superseded", "deviated")
HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")
GOVERNED_RE = re.compile(r"Governed by stop contract (sha256:[0-9a-f]{64})")
AMENDMENT_RE = re.compile(r"Amendment (sha256:[0-9a-f]{64}) supersedes ([0-9a-f]{12})")
FIRED_RE = re.compile(r"Contract ([0-9a-f]{12}) fired: ([a-z ]+)\. Executed '([a-z ]+)'.*now serves (\S+) to everyone")
BUNDLE_NOTE = (
    "Ulysses verification bundle. Check it with `ulysses verify --bundle <this file>`: no service and no "
    "Flagsmith needed. It holds no timestamps and no keys other than the signers' public keys."
)

# A date (Y-M-D, Y/M/D, D/M/Y or M/D/Y, or with a month name), a clock time, an ISO timestamp, or a decimal Unix
# time inside a string. Base64 signatures and keys have no '-', ':', '.' or spaces, so these do not fire on them.
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_TS_PATTERNS = [
    re.compile(r"\b(19|20)\d{2}-\d{2}-\d{2}\b"),
    re.compile(r"\b(19|20)\d{2}/\d{1,2}/\d{1,2}\b"),
    re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-](19|20)\d{2}\b"),
    re.compile(r"\b" + _MONTH + r"\s+\d{1,2}(st|nd|rd|th)?,?\s+(19|20)\d{2}\b", re.IGNORECASE),
    re.compile(r"\b\d{1,2}(st|nd|rd|th)?\s+" + _MONTH + r",?\s+(19|20)\d{2}\b", re.IGNORECASE),
    re.compile(r"\b\d{2}:\d{2}:\d{2}\b"),
    re.compile(r"T\d{2}:\d{2}"),
    re.compile(r"\b1[5-9]\d{8}\.\d+\b"),
]
# Keys that name a time, whatever their value holds.
_TS_KEYS = re.compile(r"^(as_of|time|timestamp|date|datetime)$|_at$", re.IGNORECASE)


def _epoch_like(v: float) -> bool:
    """A number in the range of Unix seconds or milliseconds (2001 to 2603 in seconds)."""
    return 1e9 <= v < 2e10 or 1e12 <= v < 2e13


@dataclass(frozen=True)
class Check:
    status: str  # PASS | FAIL | SKIP
    name: str
    reason: str

    def line(self) -> str:
        return f"{self.status}  {self.name}: {self.reason}"


class BundleError(Exception):
    pass


# ============================================================================ export


def flagsmith_state(gateway: FlagsmithGateway, environment: str, experiment_id: int) -> dict:
    """The parts of Flagsmith's experiment a verifier needs, read straight from Flagsmith."""
    exp = gateway.get_experiment(environment, experiment_id)
    raw = exp.raw or {}
    return {
        "source": "read from Flagsmith's experiment endpoint at export",
        "status": exp.status,
        "hypothesis": exp.hypothesis,
        "flag": exp.flag,
        "rollout_decision": raw.get("rollout_decision"),
        "rollout_variant": raw.get("rollout_variant"),
        "variations": [{"key": v.key, "option_id": v.option_id} for v in exp.variations],
    }


def _strip_snapshot(snap: dict | None) -> dict | None:
    if snap is None:
        return None
    return {k: v for k, v in snap.items() if k != "as_of"}


def _chain_hashes(store, environment: str, experiment_id: int, hypothesis: str | None) -> list[str]:
    rows = store.contracts_for(environment, experiment_id)  # oldest first
    keep = [r["hash"] for r in rows if r["state"] in GOVERNING_STATES]
    named = HASH_RE.findall(hypothesis or "")
    known = {r["hash"] for r in rows}
    for h in named:  # a hash Flagsmith names is part of the audit even if the service no longer marks it governing
        if h in known and h not in keep:
            keep.append(h)
    # Order by the supersedes links, roots first, falling back to creation order.
    by_hash = {r["hash"]: r for r in rows}
    order = {h: i for i, h in enumerate(r["hash"] for r in rows)}
    out: list[str] = []
    pending = sorted(keep, key=lambda h: order[h])
    while pending:
        progressed = False
        for h in list(pending):
            sup = by_hash[h]["supersedes"]
            if sup is None or sup in out or sup not in pending:
                out.append(h)
                pending.remove(h)
                progressed = True
        if not progressed:
            out.extend(pending)
            break
    return out


def export_bundle(store, environment: str, experiment_id: int, *, flagsmith: dict | None = None,
                  flagsmith_unavailable: str | None = None, traffic: dict | None = None) -> dict:
    """Build the bundle from the service's store and (optionally) Flagsmith's state."""
    hyp = (flagsmith or {}).get("hypothesis")
    chain = _chain_hashes(store, environment, experiment_id, hyp)
    if not chain:
        raise BundleError(f"the store holds no started contract for experiment {experiment_id} in {environment}")
    contracts = []
    names: set[str] = set()
    for h in chain:
        row = store.contract_row(h)
        c = ct.from_canonical(row["canonical"])
        names.update({c.owner.name, c.second_signer.name})
        sigs = store.signatures(h)
        contracts.append({
            "hash": h,
            "toml": ct.to_toml(c),
            "signatures": [{"role": role, "signer": s["signer"], "signature": s["signature"]}
                           for role, s in sorted(sigs.items())],
        })
    looks = []
    for lk in store.looks(environment, experiment_id):
        looks.append({
            "id": lk["id"],
            "seq": lk["seq"],
            "contract_hash": lk["contract_hash"],
            "flagsmith": _strip_snapshot(lk["snapshot"]),
            # The same projection the owner's tap covers (signing.evidence_hash).
            "recorded": signing.decision_record(lk["decision"]),
        })
    outcomes = []
    for h in chain:
        for p in store.proposals_for(h):
            outcomes.append({
                "id": p["id"],
                "contract_hash": p["contract_hash"],
                "look_id": p["look_id"],
                "outcome": p["outcome"],
                "action": p["action"],
                "state": p["state"],
                "confirm_signer": p["confirm_signer"],
                "confirm_signature": p["confirm_signature"],
                "result": json.loads(p["result"]) if p["result"] else None,
            })
    deviations = [{"kind": d.get("kind"), "source": d.get("source"), "log": d["log"], "author": d.get("author"),
                   "audit_id": d.get("audit_id"), "contract_hash": d.get("contract_hash")}
                  for d in store.deviations(environment, experiment_id)]
    bundle: dict[str, Any] = {
        "ulysses_bundle": BUNDLE_VERSION,
        "note": BUNDLE_NOTE,
        "exported_by": f"ulysses {__version__} export-bundle",
        "experiment": {"environment": environment, "experiment_id": experiment_id},
        "traffic": traffic,
        "contracts": contracts,
        "public_keys": {s["name"]: s["public_key"] for s in store.signers() if s["name"] in names},
        # Each key rotation of those signers, signed by the key it replaced (no times).
        "key_rotations": [{k: e[k] for k in ("signer", "old_key", "new_key", "seq", "signature")}
                          for e in store.signer_events(names) if e["kind"] == "rotated"],
        "looks": looks,
        "outcomes": outcomes,
        "deviations": deviations,
        "flagsmith_final": flagsmith,
    }
    if flagsmith is None:
        bundle["flagsmith_final_missing"] = flagsmith_unavailable or "not read at export"
    return bundle


def find_timestamps(bundle: dict) -> list[str]:
    """Every value or key in the bundle that looks like a date, a clock time or a Unix time.

    Strings are matched against date and time patterns; integers and floats in the Unix seconds or milliseconds
    range are flagged; a key named as a time (``as_of``, ``time``, ``timestamp``, ``date``, ``..._at``) is flagged
    whatever it holds.
    """
    hits: list[str] = []

    def walk(v: Any, path: str) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if isinstance(k, str) and _TS_KEYS.search(k):
                    hits.append(f"{path}.{k}: a key named as a time")
                walk(x, f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            if math.isfinite(v) and _epoch_like(abs(v)):
                hits.append(f"{path}: {v!r}")
        elif isinstance(v, str):
            s = v
            for pat in _TS_PATTERNS:
                m = pat.search(s)
                if m:
                    hits.append(f"{path}: {m.group(0)}")
                    break

    walk(bundle, "$")
    return hits


def find_secrets(text: str, secrets: list[str]) -> list[str]:
    out = []
    if "PRIVATE KEY" in text:
        out.append("a PEM private key")
    for s in secrets:
        if s and len(s) >= 8 and s in text:
            out.append("a configured secret value")
    return out


def dumps(bundle: dict) -> str:
    return json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def load_bundle(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ============================================================================ verify


def _snapshot(d: dict | None) -> ResultsSnapshot | None:
    if d is None:
        return None
    metrics = {name: MetricResult(name=name, direction=m.get("direction") or "up",
                                  chance_to_win=m.get("chance_to_win"), lift=m.get("lift"))
               for name, m in (d.get("metrics") or {}).items()}
    return ResultsSnapshot(identities={k: int(v) for k, v in (d.get("identities") or {}).items()},
                           metrics=metrics, srm_p_value=d.get("srm_p_value"))


def _close(a: float | None, b: float | None, tol: float = 1e-4) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(a, b, abs_tol=tol)


def _same_chance(a: float | None, b: float | None) -> bool:
    return _close(a, b, 1e-12)


def _authorised(c: ct.Contract, outcome: str) -> str | None:
    if outcome == "amendment":
        return c.amendment.action_now if c.amendment else None
    if outcome in FIRES:
        return getattr(c.actions, outcome)
    return None


def _shape_problem(bundle: Any) -> str | None:
    """The first way the bundle's structure is not what the verifier reads, or None."""

    def is_int(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool)

    def opt(v: Any, t: type | tuple) -> bool:
        return v is None or isinstance(v, t)

    if not isinstance(bundle, dict):
        return f"the bundle is a JSON {type(bundle).__name__}, not an object"
    ref = bundle.get("experiment")
    if not isinstance(ref, dict) or not isinstance(ref.get("environment"), str) or not is_int(ref.get("experiment_id")):
        return "experiment must be an object with a string environment and an integer experiment_id"
    for key in ("contracts", "looks", "outcomes", "deviations"):
        if not opt(bundle.get(key), list) or not all(isinstance(x, dict) for x in bundle.get(key) or []):
            return f"{key} must be a list of objects"
    for i, c in enumerate(bundle.get("contracts") or []):
        if not isinstance(c.get("hash"), str) or not isinstance(c.get("toml"), str):
            return f"contracts[{i}] needs a string hash and a string toml"
        sigs = c.get("signatures")
        if not opt(sigs, list) or not all(isinstance(x, dict) and opt(x.get("role"), str) and opt(x.get("signer"), str)
                                          and opt(x.get("signature"), str) for x in sigs or []):
            return f"contracts[{i}].signatures must be a list of objects with string role, signer and signature"
    keys = bundle.get("public_keys")
    if not opt(keys, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in (keys or {}).items()):
        return "public_keys must map signer names to key strings"
    rots = bundle.get("key_rotations")
    if not opt(rots, list) or not all(isinstance(r, dict) and all(isinstance(r.get(k), str) for k in
                                      ("signer", "old_key", "new_key", "signature")) and is_int(r.get("seq"))
                                      for r in rots or []):
        return "key_rotations must be a list of objects with string signer, old_key, new_key, signature and integer seq"
    seqs: set[int] = set()
    for i, lk in enumerate(bundle.get("looks") or []):
        if not isinstance(lk.get("id"), str) or not is_int(lk.get("seq")) or not isinstance(lk.get("contract_hash"), str):
            return f"looks[{i}] needs a string id, an integer seq and a string contract_hash"
        if lk["seq"] in seqs:
            return f"looks[{i}] repeats seq {lk['seq']}"
        seqs.add(lk["seq"])
        if not isinstance(lk.get("recorded"), dict):
            return f"looks[{i}].recorded must be an object"
        rg = lk["recorded"].get("guardrails")
        if not opt(rg, list) or not all(isinstance(g, dict) for g in rg or []):
            return f"looks[{i}].recorded.guardrails must be a list of objects"
        snap = lk.get("flagsmith")
        if snap is not None:
            if not isinstance(snap, dict):
                return f"looks[{i}].flagsmith must be an object or null"
            ids, metrics = snap.get("identities"), snap.get("metrics")
            if not opt(ids, dict) or not all(is_int(v) for v in (ids or {}).values()):
                return f"looks[{i}].flagsmith.identities must map variations to integers"
            if not opt(metrics, dict) or not all(isinstance(m, dict) for m in (metrics or {}).values()):
                return f"looks[{i}].flagsmith.metrics must map metric names to objects"
    for i, p in enumerate(bundle.get("outcomes") or []):
        if not isinstance(p.get("id"), str) or not opt(p.get("look_id"), str) or not opt(p.get("contract_hash"), str):
            return f"outcomes[{i}] needs a string id, and look_id and contract_hash must be strings or null"
        for k in ("outcome", "action", "state", "confirm_signer", "confirm_signature"):
            if not opt(p.get(k), str):
                return f"outcomes[{i}].{k} must be a string or null"
        if not opt(p.get("result"), dict):
            return f"outcomes[{i}].result must be an object or null"
    fs = bundle.get("flagsmith_final")
    if fs is not None:
        if not isinstance(fs, dict):
            return "flagsmith_final must be an object or null"
        if not opt(fs.get("hypothesis"), str) or not opt(fs.get("status"), str):
            return "flagsmith_final.hypothesis and status must be strings or null"
        if not opt(fs.get("variations"), list) or not all(isinstance(v, dict) for v in fs.get("variations") or []):
            return "flagsmith_final.variations must be a list of objects"
    if not opt(bundle.get("traffic"), dict):
        return "traffic must be an object or null"
    return None


def verify_bundle(bundle: dict, trusted_keys: dict[str, str] | None = None, *, expect_hash: str | None = None,
                  flagsmith_live: bool = False) -> list[Check]:
    """Every check on ``bundle``, one ``Check`` each.

    ``flagsmith_live`` says the bundle's ``flagsmith_final`` was read from Flagsmith by this verifier just now
    (``ulysses verify <id>``, or ``--bundle --check-flagsmith``), not taken from the exporter. ``expect_hash`` is a
    contract hash the auditor pins from outside the bundle (the one Flagsmith's experiment names).
    """
    checks: list[Check] = []

    def add(status: str, name: str, reason: str) -> None:
        checks.append(Check(status, name, reason))

    if not isinstance(bundle, dict) or bundle.get("ulysses_bundle") != BUNDLE_VERSION:
        found = bundle.get("ulysses_bundle") if isinstance(bundle, dict) else type(bundle).__name__
        add("FAIL", "bundle format", f"expected ulysses_bundle {BUNDLE_VERSION}, found {found!r}")
        return checks
    problem = _shape_problem(bundle)
    if problem:
        add("FAIL", "bundle format", problem)
        return checks
    try:
        _run_checks(bundle, add, trusted_keys or {}, expect_hash, flagsmith_live)
    except Exception as exc:  # noqa: BLE001 - a malformed bundle is a failed audit, reported on its own line
        add("FAIL", "bundle format", f"the verifier could not read this bundle: {type(exc).__name__}: {exc}")
    return checks


def _run_checks(bundle: dict, add, trusted_keys: dict[str, str], expect_hash: str | None, flagsmith_live: bool) -> None:
    ref = bundle["experiment"]
    env, eid = ref["environment"], ref["experiment_id"]
    add("PASS", "bundle format", f"version {BUNDLE_VERSION}, experiment {eid}")
    ts = find_timestamps(bundle)
    if ts:
        add("FAIL", "no timestamps", f"{len(ts)} found, first {ts[0]}")
    # Lines about Flagsmith's state say where that state came from: read now, or the exporter's copy.
    src = "" if flagsmith_live else " (as exported, not read from Flagsmith)"
    registry_said = "the service registered" if flagsmith_live else "the bundle's registry lists"

    # ---- contracts: text -> hash, signatures, chain -----------------------------------
    contracts: dict[str, ct.Contract] = {}
    chain: list[str] = []
    for i, entry in enumerate(bundle.get("contracts") or []):
        claimed = entry.get("hash", "")
        label = f"contract {i + 1} hash"
        try:
            c = ct.from_toml(entry.get("toml", ""))
        except Exception as exc:  # noqa: BLE001 - any parse or validation error is the finding
            add("FAIL", label, f"the contract text does not parse as a valid contract: {str(exc).splitlines()[0]}")
            continue
        h = c.hash
        if h != claimed:
            add("FAIL", label, f"the contract text hashes to {h}, the bundle claims {claimed}")
            continue
        if h in contracts:
            add("FAIL", label, f"{h} appears twice in the bundle")
            continue
        add("PASS", label, f"the contract text hashes to {h}")
        contracts[h] = c
        chain.append(h)
        if c.experiment.environment != env or c.experiment.experiment != eid:
            add("FAIL", f"contract {short_hash(h)} experiment",
                f"the contract governs experiment {c.experiment.experiment}, the bundle is for {eid}")
        sigs = {s.get("role"): s for s in entry.get("signatures") or []}
        roles = ("owner", "second_signer") if c.amendment else ("owner",)
        for role in ("owner", "second_signer"):
            named = c.owner if role == "owner" else c.second_signer
            s = sigs.get(role)
            name = f"contract {short_hash(h)} {role.replace('_', ' ')} signature"
            if s is None:
                if role in roles:
                    add("FAIL", name, f"no {role.replace('_', ' ')} signature; this contract needs one")
                continue
            if s.get("signer") != named.name:
                add("FAIL", name, f"signed by {s.get('signer')!r}, the contract names {named.name!r}")
                continue
            try:
                signing.verify(named.public_key, signing.contract_message(h, role), s.get("signature") or "")
            except signing.SignatureError as exc:
                add("FAIL", name, f"{exc} (key {named.public_key} named in the contract for {named.name})")
                continue
            add("PASS", name, f"{named.name}'s signature verifies against {named.public_key}, the key in the contract")
    for prev, h in zip(chain, chain[1:]):
        c = contracts[h]
        if not c.amendment:
            add("FAIL", f"contract {short_hash(h)} chain", "a later contract in the chain is not an amendment")
        elif c.amendment.supersedes != prev:
            add("FAIL", f"contract {short_hash(h)} chain",
                f"supersedes {c.amendment.supersedes}, but the contract before it is {prev}")
        else:
            add("PASS", f"contract {short_hash(h)} chain", f"amendment supersedes {short_hash(prev)}: {c.amendment.reason}")
            old = contracts[prev]
            if (old.owner, old.second_signer) != (c.owner, c.second_signer):
                add("FAIL", f"contract {short_hash(h)} signers", "the amendment changes who signs")
    if chain and contracts[chain[0]].amendment and contracts[chain[0]].amendment.supersedes not in contracts:
        add("PASS", f"contract {short_hash(chain[0])} chain",
            "the first contract here is an amendment of a contract that never started")
    if not chain:
        add("FAIL", "contracts", "no contract in the bundle could be checked")
        return

    # ---- key rotations ---------------------------------------------------------------------
    # A rotation hands a signer's name to a new key; it counts only if the key it replaced signed it.
    rotated_to: dict[tuple[str, str], str] = {}
    for r in bundle.get("key_rotations") or []:
        label = f"key rotation {r['signer']} #{r['seq']}"
        msg = signing.rotation_message(r["signer"], r["old_key"], r["new_key"], r["seq"])
        if signing.is_valid(r["old_key"], msg, r["signature"]):
            rotated_to[(r["signer"], r["old_key"])] = r["new_key"]
            add("PASS", label, f"{r['old_key']} handed {r['signer']} to {r['new_key']}, signed by the old key")
        else:
            add("FAIL", label, f"the rotation to {r['new_key']} is not signed by the key it replaced, {r['old_key']}")

    def _rotated(name: str, frm: str, to: str) -> bool:
        seen = {frm}
        while (name, frm) in rotated_to:
            frm = rotated_to[(name, frm)]
            if frm == to:
                return True
            if frm in seen:
                return False
            seen.add(frm)
        return False

    # ---- public keys -----------------------------------------------------------------------
    registry = bundle.get("public_keys") or {}
    named_keys: dict[str, str] = {}
    for c in contracts.values():
        for s in (c.owner, c.second_signer):
            named_keys.setdefault(s.name, s.public_key)
            if named_keys[s.name] != s.public_key:
                add("FAIL", f"public key {s.name}", "contracts in the chain name different keys for the same signer")
    pinned_keys = 0
    for name, key in sorted(named_keys.items()):
        reg = registry.get(name)
        trusted = trusted_keys.get(name)
        if trusted is not None and trusted != key:
            add("FAIL", f"public key {name}", f"the contract names {key}; you trust {trusted}")
        elif reg is not None and reg != key and not _rotated(name, key, reg):
            add("FAIL", f"public key {name}", f"{registry_said} {reg}, the contract names {key}")
        elif reg is not None and reg != key:
            add("PASS", f"public key {name}", f"the contract names {key}; {name} later rotated to {reg} by "
                                             "rotations each signed by the key it replaced")
        elif trusted is not None:
            pinned_keys += 1
            add("PASS", f"public key {name}", f"matches the key you supplied, {key}")
        else:
            add("PASS", f"public key {name}",
                f"the contract names {key}" + (f" and {registry_said} the same key" if reg else ""))
    for name, key in sorted(trusted_keys.items()):
        if name not in named_keys:
            add("FAIL", f"public key {name}", "you supplied a key for a signer no contract names")

    # ---- Flagsmith's hypothesis ----------------------------------------------------------
    fs = bundle.get("flagsmith_final")
    governing_start: str | None = None
    if fs is None:
        add("SKIP", f"hypothesis in Flagsmith{src}",
            f"the bundle carries no Flagsmith state ({bundle.get('flagsmith_final_missing', 'not read')})")
    else:
        hyp = fs.get("hypothesis") or ""
        started = GOVERNED_RE.findall(hyp)
        unknown = sorted({h for h in HASH_RE.findall(hyp) if h not in contracts})
        if unknown:
            add("FAIL", f"hypothesis in Flagsmith{src}",
                f"names {unknown[0]}, which matches no contract text in this bundle")
        elif not started:
            add("FAIL", f"hypothesis in Flagsmith{src}", "names no governing contract")
        elif len(set(started)) > 1:
            add("FAIL", f"hypothesis in Flagsmith{src}", f"names {len(set(started))} different governing contracts")
        else:
            governing_start = started[0]
            later = chain[chain.index(governing_start) + 1:]
            amended = {a for a, _ in AMENDMENT_RE.findall(hyp)}
            missing = [h for h in later if h not in amended]
            if missing:
                add("FAIL", f"hypothesis in Flagsmith{src}", f"amendment {missing[0]} is not recorded on the experiment")
            else:
                add("PASS", f"hypothesis in Flagsmith{src}",
                    f"names {governing_start} as the governing contract"
                    + (f" and {len(later)} amendment(s), all in this bundle" if later else
                       ", whose text is in this bundle"))
        devs = [ln for ln in hyp.splitlines() if ln.startswith(MARK) and "Deviation" in ln]
        if devs:
            add("FAIL", f"deviations in Flagsmith{src}", f"{len(devs)} deviation line(s): {devs[0][:160]}")

    # ---- what ties this bundle to the real experiment --------------------------------------
    if flagsmith_live and governing_start:
        add("PASS", "trust anchor", f"Flagsmith's hypothesis, read just now, names {governing_start}; the contract "
                                    "text, its signers' keys and every signature hang off that hash")
    elif expect_hash:
        if expect_hash in contracts:
            add("PASS", "trust anchor", f"the contract hash you pinned, {expect_hash}, is in this bundle's verified "
                                        "chain, so the signers' keys come from the contract you named")
        else:
            add("FAIL", "trust anchor", f"you pinned {expect_hash}; no contract in this bundle has that hash")
    elif named_keys and pinned_keys == len(named_keys):
        add("PASS", "trust anchor", "every signer's key is one you supplied with --trust-key")
    else:
        add("SKIP", "trust anchor",
            "nothing outside the bundle pins it: its contract text, keys, registry and Flagsmith lines all come from "
            "the exporter, who could build a self-consistent bundle with fresh keys. Pin it with --expect-hash <the "
            "hash on the experiment in Flagsmith>, --trust-key NAME=KEY for every signer, or --check-flagsmith")

    devs = bundle.get("deviations") or []
    if devs:
        add("FAIL", "deviations recorded", f"{len(devs)} change(s) outside the contract, first: {devs[0].get('log')}")
    else:
        add("PASS", "deviations recorded", "none")

    # ---- looks -----------------------------------------------------------------------------
    looks = sorted(bundle.get("looks") or [], key=lambda lk: lk["seq"])
    recomputed: dict[str, dict] = {}
    # The alpha ledger as recomputed here: identities and the boundaries each counted look applied, under
    # whichever contract counted it (an amendment integrates forward from them, as the service does).
    counted: list[CountedLook] = []
    first_fire: dict[str, dict] = {}  # contract hash -> the first look that fired under it
    for lk in looks:
        lid = lk["id"]
        name = f"look {lk['seq']} ({lid})"
        h = lk["contract_hash"]
        c = contracts.get(h)
        if c is None:
            add("FAIL", name, f"recorded under {h}, which is not a verified contract in this bundle")
            continue
        obs = observation_from_snapshot(c, _snapshot(lk.get("flagsmith")))
        d = evaluate(c, list(counted), obs)
        recomputed[lid] = {"outcome": d.outcome, "action": d.action, "contract": h, "seq": lk["seq"]}
        if d.counted:
            counted.append(counted_look(d.to_dict()))
        rec = lk["recorded"]
        diffs = []
        if h in first_fire and d.counted:
            diffs.append(f"counted after look {first_fire[h]['id']} fired under the same contract; the service "
                         "takes no look once a contract has fired")
        if rec.get("outcome") != d.outcome:
            diffs.append(f"outcome recorded {rec.get('outcome')!r}, the signed rule gives {d.outcome!r}")
        if bool(rec.get("counted")) != d.counted:
            diffs.append(f"counted recorded {rec.get('counted')}, recomputed {d.counted}")
        if rec.get("look_number") != d.look_number:
            diffs.append(f"look number recorded {rec.get('look_number')}, recomputed {d.look_number}")
        if not _same_chance(rec.get("chance_to_win"), d.chance_to_win):
            diffs.append(f"chance to win recorded {_pct(rec.get('chance_to_win'))}, Flagsmith's number in the "
                         f"bundle is {_pct(d.chance_to_win)}")
        if not _close(rec.get("boundary_z"), d.boundary_z):
            diffs.append(f"boundary recorded z {rec.get('boundary_z')}, the contract gives z {d.boundary_z}")
        if rec.get("action") != d.action:
            diffs.append(f"action recorded {rec.get('action')!r}, the contract gives {d.action!r}")
        rg = {g.get("name"): g for g in rec.get("guardrails") or []}
        for g in d.guardrails:
            r = rg.get(g["name"])
            if r is None or r.get("state") != g["state"] or not _same_chance(r.get("chance_worse"), g["chance_worse"]):
                diffs.append(f"guardrail {g['name']} recorded {None if r is None else r.get('state')!r}, "
                             f"recomputed {g['state']!r}")
        if d.outcome in FIRES and h not in first_fire:
            first_fire[h] = lk
        if diffs:
            add("FAIL", name, "; ".join(diffs))
            continue
        if d.outcome == "watch":
            why = (f"between looks at {d.identities_per_arm} per arm (next analysis at "
                   f"{d.next_look_at_identities}); nothing could fire")
        elif d.outcome == "not_ready":
            why = "Flagsmith had no result for the primary metric yet; not counted"
        elif d.boundary_z is None:
            why = (f"look {d.look_number} of {d.looks_planned} at {d.identities_per_arm} per arm: "
                   f"Flagsmith {_pct(d.chance_to_win)} -> {d.outcome}")
        else:
            why = (f"look {d.look_number} of {d.looks_planned} at {d.identities_per_arm} per arm: Flagsmith chance "
                   f"to win {_pct(d.chance_to_win)} against the signed boundary {_pct(d.boundary_chance)} "
                   f"(z {d.boundary_z:.4f}) -> {d.outcome}")
        if d.guardrails and d.counted:
            why += "; " + ", ".join(
                f"guardrail {g['name']} {_pct(g['chance_worse'])} chance worse against {_pct(g['boundary_chance'])}: "
                f"{g['state']}" for g in d.guardrails)
        add("PASS", name, why)

    outcomes = bundle.get("outcomes") or []
    # A look that fired under the signed rule must have its outcome in the bundle (any state, a voided one too):
    # an exporter cannot drop the decision and present the experiment as still open.
    for h, lk in first_fire.items():
        r = recomputed[lk["id"]]
        name = f"look {lk['seq']} ({lk['id']}) outcome"
        mine = [p for p in outcomes if p.get("look_id") == lk["id"] and p.get("contract_hash") == h]
        if not mine:
            add("FAIL", name, f"the signed rule fires {r['outcome']} at this look, but the bundle holds no outcome "
                              "for it")
        else:
            add("PASS", name, f"fired {r['outcome']}; outcome {mine[0].get('id')} ({mine[0].get('state')}) records it")

    # ---- outcomes --------------------------------------------------------------------------
    executed = [p for p in outcomes if p.get("state") == "executed"]
    by_seq = [(lk["seq"], lk["id"], lk["contract_hash"], lk.get("flagsmith"),
               bool((lk.get("recorded") or {}).get("counted")), lk.get("recorded")) for lk in looks]
    for p in outcomes:
        pid = p.get("id", "?")
        name = f"outcome {pid}"
        h = p.get("contract_hash")
        c = contracts.get(h)
        if p.get("state") == "void":
            add("PASS", name, f"{p.get('outcome')} on {short_hash(h or '')} was voided by an amendment; "
                              "it authorised nothing")
            continue
        if c is None:
            add("FAIL", name, f"belongs to {h}, which is not a verified contract in this bundle")
            continue
        # 1. The outcome fired under the signed rule.
        r = None
        if p.get("outcome") == "amendment":
            if p.get("look_id"):
                add("FAIL", f"{name} fired", "an amendment outcome cannot come from a look")
            elif not (c.amendment and c.amendment.action_now):
                add("FAIL", f"{name} fired", "the contract authorises no immediate action")
            else:
                add("PASS", f"{name} fired", f"the doubly signed amendment {short_hash(h)} authorises "
                                             f"{c.amendment.action_now} now")
        else:
            r = recomputed.get(p.get("look_id") or "")
            if r is None:
                add("FAIL", f"{name} fired", f"look {p.get('look_id')} is not in the bundle or did not verify")
            elif r["contract"] != h:
                add("FAIL", f"{name} fired", f"look {p.get('look_id')} was under another contract")
            elif r["outcome"] != p.get("outcome") or r["outcome"] not in FIRES:
                add("FAIL", f"{name} fired", f"the outcome is {p.get('outcome')!r}, but under the signed rule "
                                             f"look {p.get('look_id')} gives {r['outcome']!r}")
            else:
                earlier = [lid for lid, x in recomputed.items() if x["contract"] == h and x["outcome"] in FIRES
                           and x["seq"] < r["seq"]]
                if earlier:
                    add("FAIL", f"{name} fired", f"look {earlier[0]} fired first under this contract")
                else:
                    add("PASS", f"{name} fired", f"look {p.get('look_id')} fired {r['outcome']} under the signed rule")
        # 2. The action is the signed one.
        want = _authorised(c, p.get("outcome") or "")
        if want != p.get("action"):
            add("FAIL", f"{name} action", f"the contract assigns {want!r} to {p.get('outcome')!r}, "
                                          f"the outcome records {p.get('action')!r}")
        else:
            add("PASS", f"{name} action", f"{want} is the action the contract assigns to {p.get('outcome')}")
        # 3. The owner's tap, and the looks it covers.
        if p.get("state") in ("executed", "confirmed") or p.get("confirm_signature"):
            sig = p.get("confirm_signature") or ""
            legacy = signing.confirm_message(h, p.get("look_id") or pid, p.get("action") or "")
            bound = None
            if p.get("look_id") and r is not None:
                covered = [x for x in by_seq if x[0] <= r["seq"]]
                ev = signing.evidence_hash(covered)
                bound = (signing.confirm_message(h, p["look_id"], p.get("action") or "", ev), len(covered), ev)
            if p.get("confirm_signer") != c.owner.name:
                add("FAIL", f"{name} confirmation", f"confirmed by {p.get('confirm_signer')!r}, the owner is "
                                                    f"{c.owner.name!r}")
            elif bound and signing.is_valid(c.owner.public_key, bound[0], sig):
                add("PASS", f"{name} confirmation", f"{c.owner.name}'s signed tap verifies for {p.get('action')}")
                add("PASS", f"{name} evidence", f"the tap also covers the {bound[1]} look(s) up to "
                                                f"{p['look_id']} exactly as this bundle holds them ({bound[2]})")
            elif signing.is_valid(c.owner.public_key, legacy, sig):
                add("PASS", f"{name} confirmation", f"{c.owner.name}'s signed tap verifies for {p.get('action')}")
                if p.get("look_id"):
                    add("SKIP", f"{name} evidence",
                        "the tap was signed in the earlier form, which names the look and the action but not the "
                        "looks' numbers; a consistent rewrite of those numbers, or a dropped look, would not show here")
            else:
                add("FAIL", f"{name} confirmation",
                    f"the confirmation signature does not verify against {c.owner.public_key} for this outcome and "
                    "action" + (" with the looks this bundle holds up to that look (a look edited or dropped "
                                "after the owner signed breaks it)" if bound else ""))
        # 4. What was shipped.
        if p.get("state") == "executed":
            ship = c.experiment.treatment if p.get("action") == "ship_treatment" else CONTROL_KEY
            serving = (p.get("result") or {}).get("serving")
            if serving != ship:
                add("FAIL", f"{name} shipped", f"the outcome records serving {serving!r}; the action "
                                               f"{p.get('action')} ships {ship!r}")
            else:
                add("PASS", f"{name} shipped", f"{p.get('action')} ships {ship!r} on flag {c.experiment.flag}")
            _health_check(add, name, c, p)
    if len(executed) > 1:
        add("FAIL", "one executed outcome", f"{len(executed)} outcomes were executed on one experiment")
    if executed and executed[-1].get("contract_hash") != chain[-1]:
        add("FAIL", "executed by the governing contract",
            f"the executed outcome belongs to {executed[-1].get('contract_hash')}, the last contract is {chain[-1]}")

    # ---- Flagsmith's final state ---------------------------------------------------------
    if fs is None:
        add("SKIP", f"final state in Flagsmith{src}", "the bundle carries no Flagsmith state")
    else:
        status = fs.get("status")
        if not executed:
            if status == "completed":
                add("FAIL", f"final state in Flagsmith{src}", "Flagsmith shows the experiment completed, but no "
                                                             "outcome was executed under the contract")
            else:
                add("PASS", f"final state in Flagsmith{src}", f"{status}; nothing executed yet")
        else:
            p = executed[-1]
            c = contracts.get(p.get("contract_hash"))
            ship = None if c is None else (c.experiment.treatment if p.get("action") == "ship_treatment" else CONTROL_KEY)
            options = {v.get("option_id"): v.get("key") for v in fs.get("variations") or []}
            decision = fs.get("rollout_decision")
            served = (options.get(fs.get("rollout_variant"), f"option {fs.get('rollout_variant')}")
                      if decision == "variant" else CONTROL_KEY if decision == "control" else None)
            if status != "completed":
                add("FAIL", f"final state in Flagsmith{src}", f"Flagsmith shows {status!r}, but the outcome was executed")
            elif served != ship:
                add("FAIL", f"final state in Flagsmith{src}", f"Flagsmith serves {served!r} (rollout_decision "
                                                             f"{decision!r}); the rule that fired ships {ship!r}")
            else:
                add("PASS", f"final state in Flagsmith{src}", f"completed, rollout_decision {decision!r}, serving "
                                                             f"{served!r}, as the fired rule ships")
        hyp = fs.get("hypothesis") or ""
        fired_lines = FIRED_RE.findall(hyp)
        by_short = {short_hash(h): h for h in contracts}
        for short, outcome, action, serving in fired_lines:
            match = [p for p in executed if short_hash(p.get("contract_hash") or "") == short
                     and (p.get("outcome") or "").replace("_", " ") == outcome
                     and (p.get("action") or "").replace("_", " ") == action
                     and (p.get("result") or {}).get("serving") == serving]
            if short not in by_short or not match:
                add("FAIL", f"outcome line in Flagsmith{src}",
                    f"the experiment says contract {short} fired {outcome} and executed {action} serving {serving}; "
                    "no verified outcome in the bundle matches")
            else:
                add("PASS", f"outcome line in Flagsmith{src}",
                    f"the experiment records contract {short} fired {outcome}, executed {action}, serving {serving}")
        if executed and not fired_lines:
            add("SKIP", f"outcome line in Flagsmith{src}", "the experiment carries no outcome line (the service "
                                                          "writes it after completing; the final state above is "
                                                          "the evidence)")


def _health_check(add, name: str, c: ct.Contract, p: dict) -> None:
    """The Feature Health state the service relied on when it executed, for a contract that signs the rule.

    This is the service's own record of what Flagsmith's feature-health events said at that moment; the
    events themselves are not in the bundle, so it shows what was relied on rather than proving it.
    """
    if c.health is None or not c.health.block_ship_when_unhealthy:
        return
    fh = (p.get("result") or {}).get("feature_health")
    if p.get("action") != "ship_treatment":
        state = (fh or {}).get("state", "not read")
        add("PASS", f"{name} feature health", f"{p.get('action')} is never blocked by Feature Health "
                                               f"(recorded state: {state})")
        return
    if not isinstance(fh, dict):
        add("FAIL", f"{name} feature health", "the contract signs block_ship_when_unhealthy, but the executed "
                                               "ship records no Feature Health check")
    elif fh.get("state") != "healthy":
        add("FAIL", f"{name} feature health", f"the treatment shipped while the recorded Feature Health state "
                                               f"was {fh.get('state')!r}")
    else:
        add("PASS", f"{name} feature health",
            f"the service recorded Flagsmith's Feature Health for flag {c.experiment.flag} in "
            f"{fh.get('environment')!r} as healthy before shipping ({fh.get('events_for_flag', 0)} latest "
            "event(s) for the flag, none unhealthy); recorded by the service, not re-read here")


def compare_flagsmith(exported: dict | None, live: dict) -> Check:
    """Compare the Flagsmith state an exporter wrote into a bundle with the state read from Flagsmith now."""
    name = "exported Flagsmith state"
    if exported is None:
        return Check("PASS", name, "the bundle carried none; the Flagsmith lines below use the state read just now")
    fields = ("status", "hypothesis", "flag", "rollout_decision", "rollout_variant", "variations")
    differ = [f for f in fields if (exported or {}).get(f) != live.get(f)]
    if differ:
        return Check("FAIL", name, f"Flagsmith now differs from the bundle's copy in {', '.join(differ)}; the lines "
                                   "below use the state read just now")
    return Check("PASS", name, "matches what Flagsmith returns now (status, hypothesis, rollout, variations)")


def summary(checks: list[Check]) -> tuple[str, int]:
    n = {s: sum(1 for c in checks if c.status == s) for s in ("PASS", "FAIL", "SKIP")}
    if n["FAIL"]:
        return f"FAILED: {n['FAIL']} of {len(checks)} checks failed", 1
    unpinned = any(c.name == "trust anchor" and c.status == "SKIP" for c in checks)
    if unpinned:
        others = n["SKIP"] - 1
        return (f"CONSISTENT (unpinned): {n['PASS']} checks passed"
                + (f", {others} more could not run" if others else "")
                + "; nothing outside the bundle ties it to the real experiment (see the trust anchor line)", 2)
    if n["SKIP"]:
        return (f"INCOMPLETE: {n['PASS']} checks passed, {n['SKIP']} could not run on this bundle "
                "(see SKIP lines)", 2)
    return f"VERIFIED: all {n['PASS']} checks passed", 0
