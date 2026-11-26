"""Number formatting for every page, the JSON islands and the card text.

One rule matters here: a boundary is never shown as 100%. A contract that needs
z 6.99 at an early look needs a chance to win of 1 - 1.4e-12; printing that with
a fixed number of places rounds it to "100.0000%", which reads as "impossible"
or "certain". The formatter adds places until the figure is strictly inside
(0, 100), up to eight, and above that says "more than 99.999999%". The z value
always travels with it.
"""

from __future__ import annotations

import math

from ..boundary import z_to_chance

MAX_PLACES = 8
CEILING = "more than 99.999999%"
FLOOR = "less than 0.000001%"


def _inside(x: float, places: int) -> str | None:
    text = f"{x * 100:.{places}f}"
    v = float(text)
    return text if 0.0 < v < 100.0 else None


def pct(x: float | None, places: int = 2) -> str:
    """A chance as a percentage with ``places`` decimals, widened only if it would round to 0 or 100."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    if x <= 0.0:
        return "0%"
    if x >= 1.0:
        return "100%"
    for p in range(places, MAX_PLACES + 1):
        t = _inside(x, p)
        if t is not None:
            return t + "%"
    return CEILING if x > 0.5 else FLOOR


def ctw(x: float | None) -> str:
    """Flagsmith's chance to win: two decimals ("96.00%")."""
    return pct(x, 2)


def boundary_chance(chance: float | None, z: float | None = None) -> str:
    """A boundary as a chance to win, never 100%.

    When ``z`` is given it is the source of truth (stored chances are rounded to
    six places, so a z of 6.99 is stored as exactly 1.0).
    """
    if z is not None and math.isfinite(z):
        chance = z_to_chance(z)
    if chance is None:
        return "no stop possible"
    if chance >= 1.0:
        return CEILING
    if chance <= 0.0:
        return FLOOR
    # Shortest form (two to eight places) that stays strictly inside (0, 100).
    for p in range(2, MAX_PLACES + 1):
        t = _inside(chance, p)
        if t is not None:
            return t + "%"
    return CEILING if chance > 0.5 else FLOOR


def loss_boundary(chance: float | None, z: float | None = None) -> str:
    """The mirror image: a loss is called below 100% minus the win boundary."""
    if z is not None and math.isfinite(z):
        chance = z_to_chance(z)
    if chance is None:
        return "no stop possible"
    return boundary_chance(1.0 - chance) if chance < 1.0 else FLOOR


def zval(z: float | None, places: int = 2) -> str:
    if z is None or not math.isfinite(z):
        return "–"
    return f"{z:.{places}f}".replace("-", "−")


def boundary_with_z(chance: float | None, z: float | None) -> str:
    """"99.9957% (z 3.93)", or "no stop possible"."""
    if z is None and chance is None:
        return "no stop possible"
    return f"{boundary_chance(chance, z)} (z {zval(z)})"


def count(n: int | float | None) -> str:
    if n is None:
        return "–"
    return f"{int(n):,}"


def lift(x: float | None) -> str | None:
    if x is None or not math.isfinite(x):
        return None
    sign = "+" if x >= 0 else "−"
    return f"{sign}{abs(x) * 100:.1f}%"


def rate(k: int, n: int) -> str:
    return "n/a" if not n else f"{100 * k / n:.1f}%"


def age(value, now: float | None = None) -> str | None:
    """A timestamp as a relative age ("4 minutes ago"); pages never show a calendar date or time."""
    import datetime as _dt
    import time as _time

    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
    else:
        try:
            ts = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    secs = max(0, int((now if now is not None else _time.time()) - ts))
    if secs < 60:
        return "under a minute ago"
    if secs < 3600:
        m = secs // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if secs < 86400:
        h = secs // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = secs // 86400
    return f"{d} day{'s' if d != 1 else ''} ago"


def _ts(value) -> float | None:
    import datetime as _dt

    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def duration(secs: float | None) -> str | None:
    """A length of time in words ("42 seconds", "3 minutes", "2 days"); never a date."""
    if secs is None:
        return None
    secs = int(round(abs(secs)))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = secs // size
            return f"{n} {unit}{'s' if n != 1 else ''}"
    return f"{secs} second{'s' if secs != 1 else ''}"


def results_age(as_of, look_at) -> str | None:
    """How old Flagsmith's results were when the look read them, as a sentence fragment."""
    a, b = _ts(as_of), _ts(look_at)
    if a is None or b is None:
        return None
    gap = b - a
    if gap < 0:
        return "recomputed just after this look was taken"
    return f"{duration(gap)} old when this look read them"


FILTERS = {
    "age": age,
    "pct": pct,
    "ctw": ctw,
    "bchance": boundary_chance,
    "bloss": loss_boundary,
    "zval": zval,
    "bz": boundary_with_z,
    "count": count,
    "lift": lift,
    "duration": duration,
}
