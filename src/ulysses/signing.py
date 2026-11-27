"""Ed25519 signatures for contracts, confirmations and amendments.

A signature is over a short, domain-separated message so a signature made for
one purpose can never be replayed as another:

    ulysses/v1/<purpose>\\n<field>\\n<field>...

Public keys are written ``ed25519:<base64 of the 32 raw bytes>``. Private keys
live in files under the signer's own key directory (default ``~/.ulysses/keys``)
or, for the web signing surface, as non-extractable keys inside the signer's
browser. The service only ever sees public keys and signatures.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterable
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .canonical import content_hash

KEY_PREFIX = "ed25519:"

PURPOSE_SIGN = "sign-contract"
PURPOSE_CONFIRM = "confirm-action"
PURPOSE_ROTATE = "rotate-key"


class SignatureError(Exception):
    pass


def message(purpose: str, *fields: str) -> bytes:
    for f in fields:
        if "\n" in f:
            raise ValueError("signed fields may not contain newlines")
    return "\n".join([f"ulysses/v1/{purpose}", *fields]).encode("utf-8")


def contract_message(contract_hash: str, role: str) -> bytes:
    """What a signer signs to approve a contract in a given role."""
    return message(PURPOSE_SIGN, contract_hash, role)


def confirm_message(contract_hash: str, look_id: str, action: str, evidence: str | None = None) -> bytes:
    """What the owner signs to confirm one pre-signed action.

    For an action a look fired, ``evidence`` is ``evidence_hash`` over every look on the experiment up to and
    including that look, so the owner's tap also covers the numbers, the counted flags and the decisions it
    rests on: editing or dropping any of those looks afterwards breaks the signature. The service accepts no
    other form for a fired look. An amendment's immediate action has no look; its message names the proposal
    id in the look's place and carries no evidence line.
    """
    fields = [contract_hash, look_id, action]
    if evidence is not None:
        fields.append(f"evidence {evidence}")
    return message(PURPOSE_CONFIRM, *fields)


def rotation_message(signer: str, old_key: str, new_key: str, seq: int) -> bytes:
    """What a signer's current key signs to hand the name over to a new key.

    ``seq`` is how many rotations of this signer came before, so a rotation cannot be replayed after the
    signer has rotated away and back.
    """
    return message(PURPOSE_ROTATE, signer, old_key, new_key, f"seq {int(seq)}")


EVIDENCE_SEED = "ulysses/v2/look-evidence"

# The fields of a look's decision the evidence chain covers; ``ulysses export-bundle`` writes the same
# projection as each look's ``recorded`` object, so the service and a bundle give the same chain.
DECISION_FIELDS = ("outcome", "action", "counted", "look_number", "identities_per_arm", "fraction",
                   "chance_to_win", "boundary_z", "boundary_chance", "guardrail")
GUARDRAIL_FIELDS = ("name", "chance_worse", "boundary_z", "state")
EXPIRY_FIELDS = ("expires_after", "elapsed_seconds", "expired")


def decision_record(decision: dict | None) -> dict:
    d = decision or {}
    out = ({k: d.get(k) for k in DECISION_FIELDS}
           | {"guardrails": [{k: g.get(k) for k in GUARDRAIL_FIELDS} for g in d.get("guardrails") or []]})
    if d.get("expiry") is not None:
        # Only under a contract that signs an expiry, so records (and tap hashes) of other looks are unchanged.
        out["expiry"] = {k: d["expiry"].get(k) for k in EXPIRY_FIELDS}
    return out


def evidence_hash(looks: Iterable[tuple[int, str, str, dict | None, bool, dict | None]]) -> str:
    """A hash chain over looks given as ``(seq, look id, contract hash, Flagsmith snapshot, counted, decision)``.

    Each link is the SHA-256 of the canonical JSON of ``[previous link, seq, look id, contract hash, snapshot
    hash, counted, decision hash]``. The snapshot is Flagsmith's numbers for that look with its ``as_of`` time
    left out, and the decision is ``decision_record`` of what the service decided, so the service's store and a
    timestamp-free bundle give the same chain.
    """
    acc = EVIDENCE_SEED
    for seq, look_id, contract_hash, snapshot, counted, decision in looks:
        snap = None if snapshot is None else {k: v for k, v in snapshot.items() if k != "as_of"}
        acc = content_hash([acc, seq, look_id, contract_hash, content_hash(snap), bool(counted),
                            content_hash(decision_record(decision))])
    return acc


def encode_public(pub: Ed25519PublicKey) -> str:
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return KEY_PREFIX + base64.b64encode(raw).decode("ascii")


def decode_public(text: str) -> Ed25519PublicKey:
    if not text.startswith(KEY_PREFIX):
        raise SignatureError(f"public key must start with {KEY_PREFIX!r}")
    try:
        raw = base64.b64decode(text[len(KEY_PREFIX):], validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:  # noqa: BLE001 - normalise all decoding failures
        raise SignatureError(f"not a valid Ed25519 public key: {exc}") from exc


def encode_signature(sig: bytes) -> str:
    return base64.b64encode(sig).decode("ascii")


def verify(public_key: str, msg: bytes, signature_b64: str) -> None:
    """Raise ``SignatureError`` unless ``signature_b64`` is valid for ``msg``."""
    pub = decode_public(public_key)
    try:
        sig = base64.b64decode(signature_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SignatureError("signature is not valid base64") from exc
    if len(sig) != 64:
        raise SignatureError("an Ed25519 signature is 64 bytes")
    try:
        pub.verify(sig, msg)
    except InvalidSignature as exc:
        raise SignatureError("signature does not match") from exc


def is_valid(public_key: str, msg: bytes, signature_b64: str) -> bool:
    try:
        verify(public_key, msg, signature_b64)
    except SignatureError:
        return False
    return True


class Keypair:
    def __init__(self, private: Ed25519PrivateKey):
        self._private = private

    @classmethod
    def generate(cls) -> "Keypair":
        return cls(Ed25519PrivateKey.generate())

    @property
    def public(self) -> str:
        return encode_public(self._private.public_key())

    def sign(self, msg: bytes) -> str:
        return encode_signature(self._private.sign(msg))

    def to_pem(self, passphrase: str | None = None) -> bytes:
        enc = (serialization.BestAvailableEncryption(passphrase.encode()) if passphrase
               else serialization.NoEncryption())
        return self._private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)

    @classmethod
    def from_pem(cls, data: bytes, passphrase: str | None = None) -> "Keypair":
        try:
            key = serialization.load_pem_private_key(data, password=passphrase.encode() if passphrase else None)
        except TypeError as exc:
            raise SignatureError("this key file is encrypted; a passphrase is needed") from exc
        except ValueError as exc:
            raise SignatureError("wrong passphrase, or not a key file") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise SignatureError("key file does not hold an Ed25519 private key")
        return cls(key)


def default_key_dir() -> Path:
    """Signer keys live in the user's profile, never inside a project an agent works in."""
    return Path(os.environ.get("ULYSSES_KEY_DIR", Path.home() / ".ulysses" / "keys"))


def key_path(name: str, key_dir: Path | None = None) -> Path:
    safe = "".join(c for c in name if c.isalnum() or c in "-_.")
    if not safe:
        raise ValueError("signer name must contain letters or digits")
    return (key_dir or default_key_dir()) / f"{safe}.key"


def _restrict(path: Path) -> str | None:
    """Make the key readable by the current user only. Returns a warning if that is not possible."""
    if os.name == "nt":
        import getpass
        import subprocess

        try:
            user = subprocess.run(["whoami"], capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            user = ""
        user = user or os.environ.get("USERNAME") or getpass.getuser()
        if not user:
            return f"could not find the current Windows account to restrict {path}"
        try:
            r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"could not restrict {path} to {user}: {exc}"
        if r.returncode != 0:
            return f"could not restrict {path} to {user}: {r.stdout.strip() or r.stderr.strip()}"
        return None
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        return f"could not restrict {path}: {exc}"
    return None


def save_keypair(name: str, kp: Keypair, key_dir: Path | None = None, passphrase: str | None = None) -> Path:
    """Write a signer's key, encrypted with ``passphrase`` when one is given, readable only by this user."""
    path = key_path(name, key_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"{path} already exists; refusing to overwrite a signing key")
    path.write_bytes(kp.to_pem(passphrase))
    warn = _restrict(path)
    if warn:
        import warnings

        warnings.warn(warn, stacklevel=2)
    return path


def passphrase_for(name: str) -> str | None:
    """A key's passphrase from ULYSSES_KEY_PASSPHRASE_<NAME>, if the caller set one (scripted runs only)."""
    env = "ULYSSES_KEY_PASSPHRASE_" + "".join(c if c.isalnum() else "_" for c in name).upper()
    return os.environ.get(env)


def load_keypair(name: str, key_dir: Path | None = None, passphrase: str | None = None,
                 prompt: bool = False) -> Keypair:
    path = key_path(name, key_dir)
    if not path.exists():
        raise FileNotFoundError(
            f"no signing key for {name!r} at {path}. Create one with: ulysses keys new {name}"
        )
    data = path.read_bytes()
    pw = passphrase or passphrase_for(name)
    if pw is None and b"ENCRYPTED" in data and prompt:
        import getpass

        pw = getpass.getpass(f"Passphrase for {name}'s signing key: ")
    return Keypair.from_pem(data, pw)
