"""The plain-language explainer page (``/explain``), drawn only from committed data.

Nothing here is typed in. The false-winner rates come from ``data/offline.json`` and the Two Ships
counts from ``data/paths-offline.json`` (through ``calibration_view.offline_view``, the same code the
calibration page uses). The tempted-agent story is read from ``data/replay-tempted.jsonl``, the
recorded run the README's "The agent tried to ship. Twice." table comes from. No Flagsmith and no
contract store are needed, so the page renders on a fresh clone.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import calibration_view, fmt, two_ships

DATA = two_ships.SAMPLE.parent
TEMPTED = DATA / "replay-tempted.jsonl"
WIN = 0.95


def _rates(offline: dict | None) -> dict | None:
    """The headline rates, read from offline.json's own counts."""
    p = DATA / "offline.json"
    if not offline or offline.get("error") or not p.exists():
        return None
    try:
        aa = json.loads(p.read_text(encoding="utf-8"))["aa"]
        n = aa["banner_every_look"]["runs"]
        con = aa["contract"]
        return {
            "n": fmt.count(n),
            "looks": aa["parameters"]["looks"],
            "peek": fmt.rate(aa["banner_every_look"]["winning"], n),
            "once": fmt.rate(aa["banner_final_look_only"]["winning"], n),
            "same": fmt.rate(con["0.1"]["winning"], n) if "0.1" in con else None,
            "default": fmt.rate(con["0.05"]["winning"], n) if "0.05" in con else None,
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None


def tempted(path: Path | None = None) -> dict | None:
    """The real-effect experiment of the recorded tempted run: each look, each refused ship, the end."""
    path = path or TEMPTED
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return None
    exp = next((r for r in rows if r.get("event") == "experiment" and r.get("kind") != "A/A"), None)
    if not exp:
        return None
    contracts = [r for r in rows if r.get("event") == "contract"]
    exps = [r for r in rows if r.get("event") == "experiment"]
    try:
        h = contracts[exps.index(exp)]["hash"]  # contracts are recorded right after their experiment
    except (IndexError, KeyError, ValueError):
        return None
    looks, attempts, steps = [], 0, []
    last_look = None
    for r in rows:
        if r.get("hash") != h:
            continue
        if r.get("event") == "look":
            d = r.get("decision") or {}
            if d.get("look_number") is None or d.get("chance_to_win") is None:
                continue
            last_look = {"n": d["look_number"], "ctw": fmt.ctw(d["chance_to_win"]),
                         "needs": fmt.boundary_chance(d.get("boundary_chance"), d.get("boundary_z")),
                         "winning": d["chance_to_win"] >= WIN, "outcome": d.get("outcome"),
                         "action": d.get("action"), "refused": False}
            looks.append(last_look)
        elif (r.get("event") == "attempt" and r.get("call") == "execute" and r.get("action") == "ship_treatment"
              and r.get("expect") == "refused" and last_look and last_look["outcome"] == "hold"):
            last_look["refused"] = True
            attempts += 1
    if not looks:
        return None
    sirens = [lk for lk in looks if lk["winning"] and lk["refused"]]
    end = looks[-1]
    confirm = next((r for r in rows if r.get("event") == "confirm" and r.get("hash") == h), None)
    result = (confirm or {}).get("recorded_result") or {}
    return {
        "flag": exp.get("flag"), "treatment": exp.get("treatment"), "control": exp.get("control"),
        "looks": looks, "sirens": sirens, "refused": attempts, "end": end,
        "kept_control": end.get("action") == "keep_control",
        "tapped_by": (confirm or {}).get("signer"), "serving": result.get("serving"),
        "file": f"data/{path.name}",
    }


def page() -> dict:
    offline = calibration_view.offline_view(DATA)
    ts = (offline or {}).get("two_ships") if offline and not offline.get("error") else None
    return {"rates": _rates(offline), "ts": ts, "story": tempted()}
