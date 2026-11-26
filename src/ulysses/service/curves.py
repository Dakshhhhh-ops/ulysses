"""Chart geometry for the signing page's power and expected-sample-size curves.

The numbers come from ``ulysses.power`` (the engine); this module only maps them onto a
percentage plot box, picks clean ticks and formats labels. The template draws each panel as
an SVG polyline in a 0-100 box (non-scaling strokes) with HTML tick labels around it, so the
text stays at its real size at any width.
"""

from __future__ import annotations

import math

from .. import power
from . import fmt


def _nice_step(span: float, target: int) -> float:
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _ticks(lo: float, hi: float, target: int) -> list[float]:
    step = _nice_step(hi - lo, target)
    start = math.ceil(lo / step - 1e-9) * step
    out = []
    v = start
    while v <= hi + 1e-9:
        out.append(round(v, 10))
        v += step
    return out


def _count(n: float) -> str:
    return f"{n:,.0f}"


REF_GAP = 8.0  # percent of the y range


def build(c) -> dict | None:
    """Everything the template needs, or None when the curves cannot be computed."""
    try:
        cur = power.curves_for_contract(c)
    except Exception:  # noqa: BLE001 - a page must render even if the engine refuses a design
        return None
    pts = cur["points"]
    if len(pts) < 2:
        return None
    lift = cur["x_kind"] == "relative_lift"
    xs = [p["x"] for p in pts]
    x0, x1 = min(xs), max(xs)
    n = cur["horizon_identities_per_arm"]

    def px(x: float) -> float:
        return round((x - x0) / (x1 - x0) * 100, 3)

    def py(frac: float) -> float:  # 0..1 of the y range, 0 at the bottom
        return round(100 - frac * 100, 3)

    def xlab(x: float) -> str:
        return f"{x * 100:+.0f}%".replace("+0%", "0%") if lift else f"{x:g}"

    multi = cur["comparisons"] > 1
    series_a = [
        {"key": "win", "name": "Ships the treatment" if not multi else "Ships the treatment with the effect",
         "tone": "contract", "points": [(px(p["x"]), py(p["win"])) for p in pts], "dash": False},
        {"key": "loss", "name": "Keeps control on a loss" if not multi else "Keeps control: every arm lost",
         "tone": "ink", "points": [(px(p["x"]), py(p["loss"])) for p in pts], "dash": True},
    ]
    if multi:
        series_a.append({"key": "win_other", "name": "Ships a treatment with no effect", "tone": "amber",
                         "points": [(px(p["x"]), py(p["win_other"])) for p in pts], "dash": False})
    ess_max = n * 1.15  # headroom above the horizon line for its label
    series_b = [{"key": "ess", "name": "Expected identities per arm at the stop", "tone": "contract",
                 "points": [(px(p["x"]), py(p["expected_identities_per_arm"] / ess_max)) for p in pts],
                 "dash": False}]
    xt = [{"at": px(v), "text": xlab(v)} for v in _ticks(x0, x1, 6)]
    ya = [{"at": py(v), "text": f"{v:.0%}"} for v in (0, 0.25, 0.5, 0.75, 1.0)]
    yb = [{"at": py(v / ess_max), "text": _count(v)} for v in _ticks(0, n, 3) if v <= n]
    refs_b = [{"at": py(n / ess_max), "text": f"Horizon: at most {_count(n)} per arm", "side": "above"}]
    fixed = cur.get("fixed_sample_identities_per_arm")
    ref_note = None
    if fixed and fixed < ess_max:
        at = py(fixed / ess_max)
        # Both labels sit above their lines; when the lines are within 8% of the y range the lower
        # one's label goes under its line so the two never overlap.
        side = "below" if abs(at - refs_b[0]["at"]) < REF_GAP else "above"
        refs_b.append({"at": at, "side": side, "text": f"Single read: {_count(fixed)}"})
        ref_note = (f"Single read: the {_count(fixed)} identities per arm a test read once at the end needs for the "
                    "same power at the design lift, at the same alpha.")
    design = cur.get("design")
    mark_a = mark_b = None
    if design:
        mark_a = {"x": px(design["x"]), "y": py(design["win"]),
                  "text": f"{fmt.pct(design['win'], 1)} at {xlab(design['x'])}"}
        mark_b = {"x": px(design["x"]), "y": py(design["expected_identities_per_arm"] / ess_max),
                  "text": f"{_count(design['expected_identities_per_arm'])} at {xlab(design['x'])}"}
    zero = px(0.0) if x0 < 0 < x1 else None
    rows = [{"x": xlab(p["x"]) if not lift else f"{p['x'] * 100:+.1f}%", "z": f"{p['theta']:.2f}",
             "win": fmt.pct(p["win"], 1), "loss": fmt.pct(p["loss"], 1),
             "other": fmt.pct(p["win_other"], 2) if multi else None,
             "ess": _count(p["expected_identities_per_arm"])} for p in pts]
    hover = [{"x": px(p["x"]), "label": rows[i]["x"], "win": rows[i]["win"], "loss": rows[i]["loss"],
              "other": rows[i]["other"], "ess": rows[i]["ess"]} for i, p in enumerate(pts)]
    if cur["method"] == "analytic":
        method = ("Computed exactly for this boundary by the same numerical integration that builds it "
                  "(canonical sequential z model, looks on their planned counts).")
    else:
        sim = cur["simulation"]
        method = (f"Simulated: {sim['runs']:,} runs per point, fixed seed {sim['seed']}, {cur['comparisons']} "
                  "treatments sharing one control (their z values correlate), one treatment with the effect "
                  "and the others with none.")
    signed = c.design.min_relative_lift if c.design and c.design.min_relative_lift else None
    against = (signed is not None and signed < 0) or c.primary_metric.direction == "decrease"
    signed_note = None
    if lift and signed is not None and signed < 0:
        # power.py and horizon.py work with the size of the signed lift, so the axis is the improvement.
        signed_note = (f"You signed a lift of {signed * 100:+.0f}% (the metric should go down); the charts measure "
                       f"the improvement in that direction, so {xlab(abs(signed))} on the axis is that "
                       f"{signed * 100:+.0f}%.")
    return {
        "x_title": (("True relative improvement of the treatment" if against else "True relative lift of the treatment")
                    if lift else "True effect, as the expected z at the horizon"),
        "signed_note": signed_note,
        "x_ticks": xt, "zero": zero,
        "a": {"title": "Chance of each outcome", "y_ticks": ya, "series": series_a, "mark": mark_a},
        "b": {"title": "Expected identities per arm before it stops", "y_ticks": yb, "series": series_b,
              "mark": mark_b, "refs": refs_b, "ref_note": ref_note},
        "design": design, "design_text": None if not design else xlab(design["x"]),
        "method": method, "multi": multi, "rows": rows, "hover": hover,
        "baseline": None if not cur["baseline_rate"] else f"{cur['baseline_rate']:.1%}",
        "comparison_alpha": cur["comparison_alpha"], "comparisons": cur["comparisons"],
        "raw": cur,
    }
