"""The data behind the instrument: one dict per page, shared by the SVG and WebGL drawings.

``build_*`` turn what a route already has (``look_view()``, ``/human/contract``)
into a ``scene`` dict. It is written into the page as a JSON island and drawn
twice: by ``templates/_elevation.html`` as a flat SVG (the fallback, the
pre-load placeholder and the small table glyphs) and by the three.js bundle.
``elevation()`` is the front view both drawings agree on; the JS bundle has a
twin of it and ``tests/test_ui_scene.py`` plus ``web/src/parity.test.js`` hold
them to the same fixture.

No statistics happen here. Every z comes from the decision core; the only
arithmetic is the drawing scale (x from the information fraction, radius from z).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from . import fmt
from .voyage import is_siren

CONST: dict[str, float] = json.loads((Path(__file__).parent / "elevation_constants.json").read_text(encoding="utf-8"))
K = CONST["K"]
Z_MAX = CONST["Z_MAX"]
X0, X1 = CONST["X0"], CONST["X1"]
BANNER_Z = CONST["BANNER_Z"]
LATE = CONST["LATE_TOLERANCE"]

# ring states (shared with the shader)
FUTURE, PAST, CURRENT, WIN, LOSS, HORIZON, NOSTOP, GHOST = range(8)

WORDS = {
    "hold": "HOLD",
    "watch": "HOLD · BETWEEN LOOKS",
    "not_ready": "COLLECTING DATA",
    "win": "WIN: SHIP VARIATION",
    "loss": "LOSS: KEEP CONTROL",
    "horizon": "NO DECISION: KEEP CONTROL",
    "guardrail_breach": "GUARDRAIL: STOP, KEEP CONTROL",
}
SHORT = {"hold": "Hold", "watch": "Between looks", "not_ready": "Collecting data", "win": "Win", "loss": "Loss",
         "horizon": "No decision", "guardrail_breach": "Guardrail stop"}


def x_of(t: float) -> float:
    return X0 + (X1 - X0) * max(0.0, min(1.0, t))


def r_of(z: float | None) -> float:
    if z is None:
        return K * Z_MAX
    return K * max(-Z_MAX, min(Z_MAX, z))


def _r6(v: float) -> float:
    return round(v, 6)


# ---- monotone cubic (Fritsch-Carlson), the shell's profile ---------------------------

def monotone(xs: list[float], ys: list[float], samples: int = 96) -> list[list[float]]:
    n = len(xs)
    if n == 0:
        return []
    if n == 1:
        return [[_r6(xs[0]), _r6(ys[0])]]
    d = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) for i in range(n - 1)]
    m = [0.0] * n
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, n - 1):
        m[i] = 0.0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = m[i + 1] = 0.0
            continue
        a, b = m[i] / d[i], m[i + 1] / d[i]
        s = a * a + b * b
        if s > 9:
            tau = 3 / math.sqrt(s)
            m[i], m[i + 1] = tau * a * d[i], tau * b * d[i]
    out = []
    lo, hi = xs[0], xs[-1]
    j = 0
    for s in range(samples):
        x = lo + (hi - lo) * s / (samples - 1)
        while j < n - 2 and x > xs[j + 1]:
            j += 1
        h = xs[j + 1] - xs[j]
        u = (x - xs[j]) / h
        h00 = (1 + 2 * u) * (1 - u) ** 2
        h10 = u * (1 - u) ** 2
        h01 = u * u * (3 - 2 * u)
        h11 = u * u * (u - 1)
        y = h00 * ys[j] + h10 * h * m[j] + h01 * ys[j + 1] + h11 * h * m[j + 1]
        out.append([_r6(x), _r6(y)])
    return out


# ---- shared pieces ---------------------------------------------------------------------

def planned_rows(planned: list[dict]) -> list[dict]:
    rows = []
    for p in planned:
        z = p.get("z")
        rows.append({
            "look": p["look"], "t": p["fraction"], "n": p["identities_per_arm"], "z": z,
            "needs": fmt.boundary_chance(p.get("chance_to_win"), z) if z is not None else "no stop possible",
            "z_text": fmt.zval(z),
        })
    return rows


def _source(src: dict | None) -> dict:
    return src or {"kind": "unrecorded", "simulated": True, "chip": "Source not recorded",
                   "band": "SOURCE NOT RECORDED", "detail": None}


def _contract_block(h: str, summary: dict, horizon: dict | None, alpha: float | None) -> dict:
    return {"hash": h, "short": h.split(":")[-1][:12], "experiment": summary.get("experiment"),
            "flag": summary.get("flag"), "arms": summary.get("arms"),
            "horizon": (horizon or {}).get("identities_per_arm"), "looks": (horizon or {}).get("looks"),
            "alpha": alpha}


def lead_arm(arms: list[str] | None, dec: dict | None) -> str | None:
    """The treatment a card is about: the only one, else the winner, else the highest chance to win."""
    if not arms or len(arms) < 2:
        return None
    if len(arms) == 2:
        return arms[1]
    dec = dec or {}
    if dec.get("winner"):
        return dec["winner"]
    rows = [r for r in dec.get("arms") or [] if r.get("chance_to_win") is not None]
    if rows:
        return max(rows, key=lambda r: r["chance_to_win"])["arm"]
    return arms[1]


# ---- card ------------------------------------------------------------------------------

def build_card(d: dict, *, source: dict | None, horizon: dict | None, alpha: float | None,
               fractions: dict[int, dict] | None = None) -> dict:
    """``d`` is ``look_view()``; ``fractions`` maps look seq to {fraction, look_number} from the stored decisions."""
    dec = d["decision"]
    cur_seq = d["look"]["seq"]
    planned = planned_rows(d["contract"]["planned"])
    hz_n = (horizon or {}).get("identities_per_arm")
    fractions = fractions or {}
    hist = []
    for x in d["history"]:
        if x["seq"] > cur_seq:
            continue
        fr = fractions.get(x["seq"], {})
        t = fr.get("fraction")
        if t is None and hz_n:
            t = min(1.0, x["identities_per_arm"] / hz_n)
        hist.append({
            "seq": x["seq"], "t": t if t is not None else 0.0, "n": x["identities_per_arm"],
            "counted": bool(x["counted"]), "outcome": x["outcome"], "look_number": fr.get("look_number"),
            "z": x["z"] if x["chance_to_win"] is not None else None,
            "boundary_z": x["boundary_z"],
            "ctw": fmt.ctw(x["chance_to_win"]) if x["chance_to_win"] is not None else None,
            "needs": (fmt.boundary_chance(x["boundary_chance"], x["boundary_z"])
                      if x["boundary_z"] is not None else ("no stop possible" if x["counted"] else None)),
            "z_text": fmt.zval(x["z"]) if x["chance_to_win"] is not None else None,
            "bz_text": fmt.zval(x["boundary_z"]),
            "word": SHORT.get(x["outcome"], x["outcome"]),
            "siren": is_siren(x["counted"], x["outcome"], x["chance_to_win"]),
        })
    p = d.get("proposal")
    outcome = dec["outcome"]
    z, bz = dec.get("z"), dec.get("boundary_z")
    money = None
    if outcome in ("hold", "watch") and z is not None and abs(z) > BANNER_Z and (bz is None or abs(z) < bz):
        money = "Winning" if z > 0 else "Losing"
    planned_t = None
    k = dec.get("look_number")
    if k and 1 <= k <= len(planned):
        planned_t = planned[k - 1]["t"]
    snap = d.get("flagsmith") or None
    return {
        "kind": "card",
        "source": _source(source),
        "contract": {**_contract_block(d["contract"]["hash"], d["contract"]["summary"], horizon, alpha),
                     "lead": lead_arm(d["contract"]["summary"].get("arms"), dec)},
        "planned": planned,
        "signed": True,
        "history": hist,
        "current_seq": cur_seq,
        "decision": {
            "outcome": outcome, "word": WORDS.get(outcome, outcome.upper()),
            "z": z, "boundary_z": bz, "t": dec.get("fraction"), "look_number": k,
            "looks_planned": dec.get("looks_planned"), "planned_t": planned_t,
            "ctw": fmt.ctw(dec.get("chance_to_win")) if dec.get("chance_to_win") is not None else None,
            "needs": fmt.boundary_chance(dec.get("boundary_chance"), bz) if bz is not None else None,
            "z_text": fmt.zval(z), "bz_text": fmt.zval(bz),
            "n": dec.get("identities_per_arm"),
        },
        "proposal": None if not p else {
            "id": p["id"], "state": p["state"], "action": p["action"],
            "serving": (p.get("result") or {}).get("serving"),
        },
        "money": money,
        "flagsmith_missing": snap is None,
        "end_cap": outcome == "horizon",
        "dim_after": outcome == "guardrail_breach",
    }


# ---- sign / amendment -------------------------------------------------------------------

def build_sign(c: dict, *, source: dict | None, horizon: dict | None, alpha: float | None,
               superseded: list[dict] | None = None) -> dict:
    s = c["summary"]
    signed = not c.get("needs") and c["state"] not in ("draft",)
    am = s.get("amendment") or None
    own = planned_rows(c["planned"])
    original = planned_rows(superseded) if superseded else None
    return {
        "kind": "sign",
        "source": _source(source),
        "contract": _contract_block(c["hash"], s, horizon, alpha),
        "planned": original or own,          # what the bell is turned from: the signed original on an amendment
        "rows": own,                         # this contract's own boundary table
        "signed": bool(signed or original),
        # Before signing the bell is a pencil drawing (rings, edge, banner), never an empty shell.
        "wire": not bool(signed or original),
        "expired": bool(c.get("expired")),
        "history": [], "current_seq": None, "decision": None, "proposal": None, "money": None,
        "proposed": own if original else None,
        "action_now": (am or {}).get("action_now"),
        "first_look": (s.get("first_reachable_look") or {}).get("look"),
        "final_look": (s.get("final_look") or {}).get("look"),
    }


def build_example(planned: list[dict], *, alpha: float, looks: int) -> dict:
    """The empty home page: a template boundary, no experiment, no data."""
    return {
        "kind": "example",
        "source": {"kind": "example", "simulated": True, "chip": "No data",
                   "band": "EXAMPLE SHAPE · template boundary, no experiment, no data", "detail": None},
        "contract": {"hash": None, "short": None, "experiment": None, "flag": None, "arms": None,
                     "horizon": None, "looks": looks, "alpha": alpha},
        "planned": planned_rows(planned),
        "signed": False, "wire": True,
        "history": [], "current_seq": None, "decision": None, "proposal": None, "money": None,
    }


# ---- the front view (shared with the SVG and the JS twin) ----------------------------------

def rings(scene: dict) -> list[dict]:
    """Every ring to draw: planned ones plus the ring actually applied at a late look."""
    planned = scene.get("planned") or []
    dec = scene.get("decision") or {}
    prop = scene.get("proposal") or {}
    out = []
    applied: dict[int, dict] = {}
    for hrow in scene.get("history") or []:
        if hrow.get("counted") and hrow.get("look_number") and hrow.get("boundary_z") is not None:
            applied[hrow["look_number"]] = hrow
    cur_k = dec.get("look_number")
    outcome = dec.get("outcome")
    fired = {"win": WIN, "loss": LOSS, "horizon": HORIZON}.get(outcome or "")
    executed = prop.get("state") == "executed"
    for p in planned:
        k = p["look"]
        base = NOSTOP if p["z"] is None else FUTURE
        state = base
        if cur_k is not None and k < cur_k and p["z"] is not None:
            state = PAST
        if cur_k is not None and k == cur_k:
            state = fired if fired is not None else CURRENT
        if outcome == "guardrail_breach" and cur_k is None:
            pass
        ring = {"k": k, "x": _r6(x_of(p["t"])), "r": _r6(r_of(p["z"])), "state": state, "applied": False,
                "open": 0.0, "side": 0}
        a = applied.get(k)
        if a is not None and abs(a["t"] - p["t"]) > LATE:
            ring_applied = dict(ring, x=_r6(x_of(a["t"])), r=_r6(r_of(a["boundary_z"])), applied=True)
            ring["state"] = GHOST
            out.append(ring)
            ring = ring_applied
        if state in (WIN, LOSS) and k == cur_k:
            ring["side"] = 1 if state == WIN else -1
            ring["open"] = 1.0 if executed else 0.0
        out.append(ring)
    return out


def profile(scene: dict, samples: int = 96) -> list[list[float]]:
    pts = [(x_of(p["t"]), r_of(p["z"])) for p in scene.get("planned") or [] if p["z"] is not None]
    if not pts:
        return []
    return monotone([a for a, _ in pts], [b for _, b in pts], samples)


def thread(scene: dict) -> list[list[list[float]]]:
    """Polylines of Flagsmith's z: one per unbroken run of refreshes that had a chance to win."""
    runs: list[list[list[float]]] = []
    cur: list[list[float]] = []
    for h in scene.get("history") or []:
        if h.get("z") is None:
            if cur:
                runs.append(cur)
            cur = []
            continue
        cur.append([_r6(x_of(h["t"])), _r6(r_of(h["z"]))])
    if cur:
        runs.append(cur)
    return runs


def beads(scene: dict) -> list[dict]:
    out = []
    for h in scene.get("history") or []:
        if h.get("z") is None:
            out.append({"seq": h["seq"], "x": _r6(x_of(h["t"])), "y": 0.0, "kind": "tick"})
            continue
        z = h["z"]
        kind = "bead" if h.get("counted") else "hollow"
        if h["seq"] == scene.get("current_seq"):
            kind = "current" if h.get("counted") else "current-hollow"
        out.append({"seq": h["seq"], "x": _r6(x_of(h["t"])), "y": _r6(r_of(z)), "kind": kind,
                    "clamped": abs(z) > Z_MAX, "siren": bool(h.get("siren"))})
    return out


def elevation(scene: dict) -> dict:
    """The exact front view, in world units (y up). The SVG flips y; WebGL draws the same numbers."""
    w, h, m = CONST["BOX_W"], CONST["BOX_H"], CONST["MARGIN"]
    return {
        "box": [_r6(-w * (1 + m) / 2), _r6(-h * (1 + m) / 2), _r6(w * (1 + m)), _r6(h * (1 + m))],
        "mast": [X0 - 0.2, X1 + 0.2],
        "banner": _r6(K * BANNER_Z),
        "zmax": _r6(K * Z_MAX),
        "rings": rings(scene),
        "profile": profile(scene),
        "thread": thread(scene),
        "beads": beads(scene),
        "proposed": [{"x": _r6(x_of(p["t"])), "r": _r6(r_of(p["z"])), "dashed": p["z"] is None}
                     for p in (scene.get("proposed") or [])],
    }


def svg_path(points: list[list[float]], sign: float = 1.0) -> str:
    return " ".join(("M" if i == 0 else "L") + f"{x:.4f} {-sign * y:.4f}" for i, (x, y) in enumerate(points))


def envelope_path(prof: list[list[float]]) -> str:
    if not prof:
        return ""
    top = svg_path(prof, 1.0)
    bottom = " ".join(f"L{x:.4f} {y:.4f}" for x, y in reversed(prof))
    return f"{top} {bottom} Z"


def aria_label(scene: dict) -> str:
    dec = scene.get("decision")
    if not dec:
        c = scene.get("contract") or {}
        n = len(scene.get("planned") or [])
        if scene.get("kind") == "example":
            return f"Example boundary shape for a {n}-look contract. No experiment and no data."
        return f"The signed boundary for experiment {c.get('experiment')}: {n} looks, drawn as rings around a mast."
    parts = []
    if dec.get("look_number"):
        parts.append(f"Look {dec['look_number']} of {dec['looks_planned']}.")
    if dec.get("ctw"):
        parts.append(f"Flagsmith chance to win {dec['ctw']}.")
    if dec.get("needs"):
        parts.append(f"The contract needs {dec['needs']}.")
    parts.append(SHORT.get(dec["outcome"], dec["outcome"]) + ".")
    return " ".join(parts)


def to_json(scene: dict) -> str:
    """JSON for a <script type=application/json> island (safe against </script>)."""
    return json.dumps(scene, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def latest_looks(store: Any, rows: list[dict]) -> dict[str, dict]:
    """For the home table: the newest look of each contract, read from the store."""
    out: dict[str, dict] = {}
    seen: set[tuple[str, int]] = set()
    for r in rows:
        key = (r["environment"], r["experiment_id"])
        if key in seen:
            continue
        seen.add(key)
        for lk in store.looks(*key):
            out[lk["contract_hash"]] = lk
    return out


def fan_flat(scene: dict, limit: int = 400) -> dict:
    """The calibration fan folded flat (the Read view), for the SVG fallback: at most ``limit`` paths."""
    paths = scene.get("paths") or []
    looks = scene.get("looks") or 1
    shown = paths[:limit]
    lines, dots = [], []
    for run in shown:
        pts = [p for p in run["points"] if p[1] is not None]
        cat = "contract" if run.get("contract") else ("banner" if run.get("banner") else "quiet")
        if len(pts) > 1:
            lines.append({"cat": cat, "d": " ".join(f"{x_of(lk / looks):.4f},{-r_of(z):.4f}" for lk, z in pts)})
    for run in paths:
        for kind in ("banner", "contract"):
            e = run.get(kind)
            if e and e.get("z") is not None:
                dots.append({"cat": kind, "x": _r6(x_of(e["look"] / looks)), "y": _r6(-r_of(e["z"]))})
    return {"lines": lines, "dots": dots, "shown": len(shown), "total": len(paths)}
