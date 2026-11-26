"""Where the numbers on a page came from: live traffic, synthetic identities or a lab.

Flagsmith counts identities; it cannot tell a real shopper from a simulated
one. The process that sends the traffic knows, so it records it here, next to
the service's database (``<run dir>/data-sources.json``, git-ignored, written
at run time). ``scripts/agent_session.py --with-traffic`` marks the demo
experiment as synthetic before its first look; a UI fixture marks its own
experiments as lab fixtures. The pages read it and never guess: an experiment
with no record says "Source not recorded".
"""

from __future__ import annotations

import json
from pathlib import Path

FILE = "data-sources.json"

KINDS = {
    "live": "Live traffic",
    "synthetic": "Synthetic identities",
    "lab": "Lab simulation",
    "fixture": "UI fixture",
}


def registry_path(db_path: str | Path | None) -> Path | None:
    if not db_path or str(db_path) == ":memory:":
        return None
    return Path(db_path).resolve().parent / FILE


def _read(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def mark(db_path: str | Path, environment: str, experiment_id: int, kind: str, detail: str) -> None:
    """Record the source of an experiment's traffic (called by the process that sends it)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    path = registry_path(db_path)
    if path is None:
        return
    data = _read(path)
    data[f"{environment}:{experiment_id}"] = {"kind": kind, "detail": detail}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def lookup(db_path: str | Path | None, environment: str, experiment_id: int, api_host: str = "") -> dict:
    """The source record for one experiment, shaped for the data chip and the hatched band."""
    rec = _read(registry_path(db_path)).get(f"{environment}:{experiment_id}")
    if not rec:
        return {"kind": "unrecorded", "simulated": True, "chip": "Source not recorded",
                "band": "SOURCE NOT RECORDED · this service has no record of whether these visitors were real or simulated",
                "detail": None}
    kind = rec.get("kind", "unrecorded")
    detail = rec.get("detail") or ""
    if kind == "live":
        return {"kind": "live", "simulated": False, "chip": f"Live · Flagsmith{(' at ' + api_host) if api_host else ''}",
                "band": None, "detail": detail}
    word = KINDS.get(kind, "Source not recorded")
    return {"kind": kind, "simulated": True, "chip": word,
            "band": f"{word.upper()} · {detail}" if detail else word.upper(), "detail": detail}
