"""Canonical serialisation and hashing.

The hash covers the meaning of a contract, not its formatting: the TOML file is
parsed, turned into plain JSON values, and serialised with sorted keys, no
insignificant whitespace, UTF-8, and no NaN or infinity. Two files that differ
only in comments, key order or spacing hash the same; any change to a value
changes the hash.

Floats are written with Python's shortest round-trip representation. To keep
the canonical form portable, contracts only hold floats that are short decimals
(alpha, probabilities, fractions rounded to six places).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

HASH_PREFIX = "sha256:"


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(value: Any) -> str:
    """``sha256:<hex>`` of the canonical form of ``value``."""
    return HASH_PREFIX + sha256_hex(canonical_bytes(value))


def short_hash(h: str, n: int = 12) -> str:
    return h.removeprefix(HASH_PREFIX)[:n]
