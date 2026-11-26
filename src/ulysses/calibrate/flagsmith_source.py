"""Load Flagsmith's own statistics module, unmodified, for the offline calibration.

The calibration must run Flagsmith's code, not a re-implementation of it. This
loads ``api/experimentation/stats.py`` and ``constants.py`` from either a local
Flagsmith checkout (``--flagsmith-src`` or ``FLAGSMITH_SRC``) or, failing that,
downloads them from GitHub at a pinned release tag. Either way the file's
SHA-256 (line endings normalised) must match the pinned value, so a reader
knows exactly which engine produced the table.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import types
from pathlib import Path

import httpx

FLAGSMITH_TAG = "v2.295.2"
PINNED_SHA256 = {
    "stats.py": "97c5ebfca7c81a52ee234af24db5a831ec5f57bc9b461d23b50e5388d83bb880",
    "constants.py": "d77d20ab300a198ca3ebacbace2c5ac22fc61529bff64ad166e9c55f10d4564d",
}
RAW = "https://raw.githubusercontent.com/Flagsmith/flagsmith/{tag}/api/experimentation/{name}"


class SourceError(Exception):
    pass


def _normalised_sha(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _locate(name: str, src: str | Path | None, cache: Path) -> tuple[bytes, str]:
    candidates = []
    for root in (src, os.environ.get("FLAGSMITH_SRC")):
        if root:
            candidates.append(Path(root) / "api" / "experimentation" / name)
    for p in candidates:
        if p.exists():
            return p.read_bytes(), str(p)
    cached = cache / FLAGSMITH_TAG / name
    if cached.exists():
        return cached.read_bytes(), str(cached)
    url = RAW.format(tag=FLAGSMITH_TAG, name=name)
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise SourceError(
            f"cannot find Flagsmith's {name}. Pass --flagsmith-src <path to a Flagsmith checkout> "
            f"or allow the download from {url} ({exc})"
        ) from exc
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(r.content)
    return r.content, url


def load(src: str | Path | None = None, cache: Path = Path(".run/flagsmith-src")) -> tuple[types.ModuleType, types.ModuleType, dict]:
    """Return (stats module, constants module, provenance)."""
    provenance: dict[str, dict] = {}
    modules = {}
    for name in ("constants.py", "stats.py"):
        data, origin = _locate(name, src, cache)
        sha = _normalised_sha(data)
        if sha != PINNED_SHA256[name]:
            raise SourceError(
                f"{origin} has SHA-256 {sha}, expected {PINNED_SHA256[name]} (Flagsmith {FLAGSMITH_TAG}). "
                "Refusing to calibrate against a different engine."
            )
        mod_name = f"_flagsmith_experimentation_{name[:-3]}"
        spec = importlib.util.spec_from_loader(mod_name, loader=None)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules[mod_name] = mod
        exec(compile(data.decode("utf-8"), origin, "exec"), mod.__dict__)  # noqa: S102 - pinned by hash above
        modules[name] = mod
        provenance[name] = {"origin": origin, "sha256": sha, "tag": FLAGSMITH_TAG}
    return modules["stats.py"], modules["constants.py"], provenance
