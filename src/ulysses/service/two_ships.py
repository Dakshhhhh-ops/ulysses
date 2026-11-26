"""Two Ships: the calibration hero, the same 100 A/A runs sailed by two rules.

Read from ``paths-offline.json`` (written by ``ulysses calibrate offline`` next to ``offline.json``) or,
when the run directory has an ``offline.json`` from the same design and engine but no export, from the
committed copy ``data/paths-offline.json`` made by the same command. Nothing is typed in: the 100-run
counts are counted from the runs, and the 10,000-run rates come from ``offline.json``.

Left: an agent that stops at the first look over 95% (ships) or under 5% (keeps control). Right: the
Ulysses contract at alpha 0.10, the same two-sided level as reading 95% / 5% once. The page draws the
final state as SVG; a small inline script replays it once.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..boundary import chance_to_z
from ..calibrate.live import _contract
from ..calibrate.offline import PATHS_FILE, WIN
from . import fmt

SAMPLE = Path(__file__).resolve().parents[3] / "data" / PATHS_FILE
SAME_LEVEL = "0.1"     # contract level with the same two-sided strictness as a 95% / 5% reading
DEFAULT_LEVEL = "0.05"

# Drawing box (SVG user units). Vertical axis is Flagsmith's chance to win on a normal scale.
W, H = 600, 330
L, R, T, B = 52, 14, 34, 30
Z_TOP, Z_BOT = 3.9, -3.3
TICKS = ((0.999, "99.9%"), (WIN, "95%"), (0.5, "50%"), (0.05, "5%"), (0.001, "0.1%"))
DURATION = 6.0         # seconds for the replay of all looks


def _x(look: int, looks: int) -> float:
    return round(L + (W - L - R) * (look - 1) / max(1, looks - 1), 1)


def _y(z: float) -> float:
    z = max(Z_BOT, min(Z_TOP, z))
    return round(T + (H - T - B) * (Z_TOP - z) / (Z_TOP - Z_BOT), 1)


def _matches(export: dict, aa: dict) -> bool:
    """The export belongs to this offline.json: same engine, design and opening seed."""
    try:
        p, q = export["parameters"], aa["parameters"]
        seed = q["seed"][0] if isinstance(q["seed"], list) else q["seed"]
        return (export["engine"]["stats.py"]["sha256"] == aa["engine"]["stats.py"]["sha256"]
                and p["seed"] == seed and p["looks"] == q["looks"]
                and p["identities_per_arm_per_look"] == q["identities_per_arm_per_look"]
                and p["base_rate"] == q["base_rate"] and p.get("relative_lift", 0) == q.get("relative_lift", 0)
                and p["runs"] <= q["runs"] // max(1, len(q["seed"]) if isinstance(q["seed"], list) else 1))
    except (KeyError, TypeError, IndexError):
        return False


def load(run_dir: Path, aa: dict, sample: Path | None = SAMPLE) -> tuple[dict, str] | None:
    """(export, where it came from) or None. The run's own export first, then the committed copy."""
    for path, origin in ((run_dir / PATHS_FILE, "run"), (sample, "committed")):
        if path is None or not path.exists():
            continue
        try:
            export = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if export.get("runs") and _matches(export, aa):
            return export, origin
    return None


def _panel(runs: list[dict], looks: int, stop_of, title: str, rule: str) -> dict:
    voyages, ships, kept = [], 0, 0
    for r in runs:
        stop = stop_of(r)  # (look, "ship" | "kept") or None
        end = stop[0] if stop else looks
        pts = [(k, chance_to_z(c)) for k, c in enumerate(r["chance_to_win"][:end], start=1) if c is not None]
        if not pts:
            continue
        cat = stop[1] if stop else "sail"
        ships += cat == "ship"
        kept += cat == "kept"
        x_end, y_end = _x(pts[-1][0], looks), _y(pts[-1][1])
        voyages.append({
            "cat": cat, "run": r["run"],
            "d": "M" + " L".join(f"{_x(k, looks)},{_y(z)}" for k, z in pts),
            "end": {"x": x_end, "y": y_end, "look": pts[-1][0]},
            "e": round(pts[-1][0] / looks, 4),  # share of the replay at which this voyage ends
        })
    voyages.sort(key=lambda v: {"sail": 0, "kept": 1, "ship": 2}[v["cat"]])  # red drawn on top
    return {"title": title, "rule": rule, "voyages": voyages, "ships": ships, "kept": kept,
            "sailed": len(runs) - ships - kept, "n": len(runs),
            "ship_times": sorted(round(v["e"] * DURATION, 2) for v in voyages if v["cat"] == "ship")}


def _first_exit(r: dict):
    e = r.get("first_exit")
    return (e["look"], "ship" if e["side"] == "winning" else "kept") if e else None


def _contract_stop(level: str):
    def stop(r: dict):
        c = (r.get("contract") or {}).get(level)
        return (c["look"], "ship" if c["outcome"] == "win" else "kept") if c else None
    return stop


def _boundary(level: str, looks: int, per_arm: int) -> dict:
    planned = _contract(float(level), looks, looks * per_arm).model_dump()["boundary"]["planned"]
    pts = [(p["look"], p["z"]) for p in planned if p.get("z") is not None]
    def path(sign: int, limit: float) -> str:
        seg = [(k, z) for k, z in pts if z <= limit]  # the boundary only where it is inside the drawing
        return ("M" + " L".join(f"{_x(k, looks)},{_y(sign * z)}" for k, z in seg)) if len(seg) > 1 else ""
    final = pts[-1][1] if pts else None
    return {"upper": path(1, Z_TOP), "lower": path(-1, -Z_BOT),
            "final_label_y": _y(final) if final is not None else None}


def view(export: dict, aa: dict, origin: str, file_label: str) -> dict:
    p = export["parameters"]
    looks, runs = int(p["looks"]), export["runs"]
    level = SAME_LEVEL if SAME_LEVEL in runs[0]["contract"] else next(iter(runs[0]["contract"]))
    left = _panel(runs, looks, _first_exit, "Unbound agent: ships at the first “Winning”",
                  "Stops the first time Flagsmith shows over 95% (ships it) or under 5% (keeps control).")
    right = _panel(runs, looks, _contract_stop(level), "Agent under a signed Ulysses contract",
                   "Acts on the same Flagsmith number, only where its signed boundary allows.")
    # Recount guard: the drawing and the export's own counts must agree, or nothing is shown.
    c = export["counts"]
    if left["ships"] != c["first_exit"]["winning"] or right["ships"] != c["contract"][level]["win"]:
        return {"error": "The per-run export's counts do not match its runs; rerun the offline calibration."}
    n = aa["banner_every_look"]["runs"]
    con = aa["contract"]
    rates = {"n": n, "n_text": fmt.count(n),
             "unbound": fmt.rate(aa["banner_every_look"]["winning"], n),
             "same": fmt.rate(con[level]["winning"], n) if level in con else None,
             "default": fmt.rate(con[DEFAULT_LEVEL]["winning"], n) if DEFAULT_LEVEL in con else None}
    left["rate_line"] = f"{rates['unbound']} of {rates['n_text']}"
    right["rate_line"] = f"{rates['same']} of {rates['n_text']}" if rates["same"] else None
    stats = export["engine"].get("stats.py", {})
    ticks = [{"y": _y(chance_to_z(c)), "text": t, "key": t in ("95%", "5%")} for c, t in TICKS]
    return {
        "left": left, "right": right, "rates": rates, "level": level, "looks": looks, "runs": len(runs),
        "w": W, "h": H, "plot": {"l": L, "r": W - R, "t": T, "b": H - B},
        "ticks": ticks, "x_ticks": [{"x": _x(k, looks), "text": str(k)} for k in (1, 10, 20, looks)],
        "x_title": round((_x(10, looks) + _x(20, looks)) / 2, 1),
        "line95": _y(chance_to_z(WIN)), "line5": _y(chance_to_z(0.05)),
        "boundary": _boundary(level, looks, int(p["identities_per_arm_per_look"])),
        "duration": DURATION, "origin": origin, "file": file_label,
        "seed": p["seed"], "per_arm": p["identities_per_arm_per_look"], "base_rate": p["base_rate"],
        "engine": f"Flagsmith stats.py {stats.get('tag', '?')} sha256 {str(stats.get('sha256', ''))[:12]}",
        "command": export.get("command", ""),
    }


def build(run_dir: Path, aa: dict, sample: Path | None = SAMPLE) -> dict | None:
    found = load(run_dir, aa, sample)
    if not found:
        return None
    export, origin = found
    label = f"{PATHS_FILE} next to offline.json" if origin == "run" else f"data/{PATHS_FILE} (committed copy)"
    try:
        return view(export, aa, origin, label)
    except (KeyError, TypeError, ValueError, IndexError, StopIteration):
        return {"error": f"{PATHS_FILE} could not be read; rerun the offline calibration."}
