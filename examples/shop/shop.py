"""A one-page checkout that runs one Flagsmith experiment.

The call-to-action style comes from the flag ``ulysses_checkout_cta``:
``classic`` (control) or ``bold``. The page asks Flagsmith with
``get_experiment_flag``, which records the exposure; a purchase or a refund
request is sent with ``track_event``. Flagsmith computes every number from
those events. The shop never decides the winner and never holds an admin key:
it uses the environment's server-side key for local evaluation only.

Run:  uv run python examples/shop/shop.py      (serves http://127.0.0.1:8210)
Needs .run/lab.json from `ulysses lab setup`.
"""

from __future__ import annotations

import html
import json
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
from flagsmith import Flagsmith
from flagsmith.analytics import EventProcessorConfig

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".run" / "lab.json"
PORT = int(os.environ.get("SHOP_PORT", "8210"))

if not STATE.exists():
    sys.exit("No .run/lab.json. Run `uv run ulysses lab setup` first.")
demo = json.loads(STATE.read_text())["demo"]
FLAG = demo["flag"]
# The A/A flag: both of its variations carry the same headline, so any "winner" on it is noise.
AA_FLAG = (demo.get("aa") or {}).get("flag")

flagsmith = Flagsmith(
    environment_key=demo["server_key"],
    api_url=demo["api_url"] + "/api/v1/",
    enable_local_evaluation=True,
    environment_refresh_interval_seconds=10,
    enable_events=True,
    # Self-hosted Flagsmith takes events under /api/; Flagsmith Cloud on its events host (``ulysses lab setup``
    # writes which one into .run/lab.json).
    event_processor_config=EventProcessorConfig(events_api_url=demo.get("events_api_url") or demo["api_url"] + "/api/",
                                                flush_interval_seconds=2.0),
)

app = FastAPI(title="Ulysses demo shop")

STYLES = {
    "classic": "background:#e9e7e1;color:#222;border:1px solid #bbb;font-weight:500",
    "bold": "background:#1f3fd1;color:#fff;border:0;font-weight:800;font-size:20px;padding:16px 28px",
}


def cta_for(visitor: str) -> tuple[str, str]:
    flag = flagsmith.get_experiment_flag(FLAG, identifier=visitor)
    value = str(flag.value) if flag.enabled and flag.value else "classic"
    variant = getattr(flag, "variant", None) or ("bold" if value == "bold" else "control")
    return value, variant


def headline_for(visitor: str) -> str:
    """The A/A experiment's exposure: same text in both arms."""
    if not AA_FLAG:
        return "Your basket"
    flag = flagsmith.get_experiment_flag(AA_FLAG, identifier=visitor)
    return str(flag.value) if flag.enabled and flag.value else "Your basket"


SERVICE = os.environ.get("ULYSSES_PUBLIC_URL", "http://127.0.0.1:8200").rstrip("/")
FONTS = ROOT / "src" / "ulysses" / "service" / "static" / "fonts"
if FONTS.exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/fonts", StaticFiles(directory=str(FONTS)), name="fonts")

# The page chrome only. STYLES and the button are the experiment and stay as they are.
CHROME = """
@font-face{font-family:"Plex Cond";font-weight:500;font-display:swap;src:url(/fonts/ibm-plex-sans-condensed-latin-500-normal.woff2) format("woff2")}
@font-face{font-family:"Plex Cond";font-weight:600;font-display:swap;src:url(/fonts/ibm-plex-sans-condensed-latin-600-normal.woff2) format("woff2")}
@font-face{font-family:"Plex Mono";font-weight:400;font-display:swap;src:url(/fonts/ibm-plex-mono-latin-400-normal.woff2) format("woff2")}
:root{--paper:#F4F2EC;--sheet:#FBFAF7;--ink:#121518;--graphite:#4B525A;--rule:#8C8574;--contract:#1F4E8C}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:500 16px/24px "Plex Cond","Arial Narrow",sans-serif;padding-bottom:48px}
.shop{max-width:560px;margin:0 auto;padding:40px 16px 56px}
.shop h1{font:600 28px/34px "Plex Cond",sans-serif;margin:0 0 20px}
.basket{background:var(--sheet);border:1px solid var(--rule);display:grid;grid-template-columns:128px 1fr;gap:20px;padding:20px;align-items:center;margin-bottom:24px}
.basket svg{width:128px;height:128px}
.line{display:flex;justify-content:space-between;gap:12px;font-size:18px}
.price{font-family:"Plex Mono",monospace;font-variant-numeric:tabular-nums}
.muted{color:var(--graphite);font-size:14px}
.strip{position:fixed;left:0;right:0;bottom:0;min-height:32px;display:flex;align-items:center;padding:4px 12px;
 background:var(--paper) repeating-linear-gradient(45deg,rgb(18 21 24/.14) 0 1px,transparent 1px 6px);border-top:1px solid var(--rule)}
.strip p{margin:0;background:var(--sheet);border:1px solid var(--rule);border-radius:2px;padding:2px 8px;font:400 12px/18px "Plex Mono",monospace;
 display:flex;flex-wrap:wrap;align-items:center;gap:0 6px}
.strip a{color:var(--contract)}
.strip svg{width:16px;height:16px;vertical-align:-3px}
a:focus-visible,button:focus-visible{outline:2px solid var(--contract);outline-offset:2px}
@media (max-width:639px){body{padding-bottom:64px}}
"""

STILL_LIFE = """<svg viewBox="0 0 128 128" aria-hidden="true" fill="none" stroke="#121518" stroke-width="1.5" stroke-linejoin="round">
<path d="M34 30h60l-14 34H48z"/><path d="M30 30h68"/><path d="M58 64v6h12v-6"/><path d="M98 34c8 0 10 10 2 14"/>
<path d="M44 74h40c6 10 8 24 4 38-1 4-4 6-8 6H48c-4 0-7-2-8-6-4-14-2-28 4-38z"/><path d="M84 82c8 0 10 14 2 18"/>
<path d="M42 96h44" stroke-dasharray="3 3"/><path d="M20 122h88"/></svg>"""


def _research() -> dict | None:
    """Experiment and contract for the strip, read from the demo's run files and the service's store."""
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from ulysses.service import scene as sc
        from ulysses.store import Store

        d = json.loads((ROOT / ".run" / "demo.json").read_text())
        db = ROOT / os.environ.get("ULYSSES_DB", ".run/ulysses.db")
        if not db.exists():
            return None
        st = Store(db)
        rows = st.contracts_for(d["environment"], d["experiment_id"])
        if not rows:
            return None
        h = rows[-1]["hash"]
        looks = st.looks(d["environment"], d["experiment_id"])
        planned = rows[-1]["canonical"]["boundary"]["planned"]
        e = sc.elevation({"planned": sc.planned_rows(planned)})
        prof = e["profile"]
        glyph = ""
        if prof:
            top = " ".join(f"{x:.2f},{-y:.2f}" for x, y in prof)
            bot = " ".join(f"{x:.2f},{y:.2f}" for x, y in prof)
            glyph = (f'<svg viewBox="-4.7 -2.9 9.4 5.8" aria-hidden="true"><path d="M-4.4 0H4.4" stroke="#121518" stroke-width=".3"/>'
                     f'<polyline points="{top}" fill="none" stroke="#1F4E8C" stroke-width=".45"/>'
                     f'<polyline points="{bot}" fill="none" stroke="#1F4E8C" stroke-width=".45"/></svg>')
        return {"experiment": d["experiment_id"], "hash": h, "short": h.split(":")[-1][:12],
                "card": looks[-1]["id"] if looks else None, "glyph": glyph}
    except Exception:  # noqa: BLE001 - the strip shows nothing when it cannot find them
        return None


@app.get("/", response_class=HTMLResponse)
def page(visitor: str = "guest"):
    value, variant = cta_for(visitor)
    headline = html.escape(headline_for(visitor))
    v = html.escape(visitor)
    r = _research()
    exp = ""
    if r:
        card = f'<a href="{SERVICE}/card/{r["card"]}">Experiment {r["experiment"]}</a>' if r["card"] else f'Experiment {r["experiment"]}'
        exp = f' · {card} under contract {r["glyph"]}<a href="{SERVICE}/sign/{html.escape(r["hash"])}">{r["short"]}</a>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Checkout</title><style>{CHROME}</style></head>
<body><main class="shop">
<h1>{headline}</h1>
<div class="basket">{STILL_LIFE}<div><div class="line"><span>1 &times; Ceramic pour-over set</span><span class="price">34.00</span></div>
<p class="muted">Dripper, carafe and two filters. Ships in two days.</p></div></div>
<form method="post" action="/buy"><input type="hidden" name="visitor" value="{v}">
<button data-cta="{html.escape(value)}" data-variant="{html.escape(variant)}" style="{STYLES.get(value, STYLES['classic'])};border-radius:8px;padding:12px 22px;cursor:pointer">
Pay now</button></form>
</main>
<footer class="strip"><p>Demo shop for Ulysses · Flagsmith assigned you: <b>{html.escape(value)}</b> · Most traffic here is simulated by agent_session.py{exp}</p></footer>
</body></html>"""


@app.post("/buy")
def buy(visitor: str = Form(...)):
    flagsmith.track_event("purchase", identifier=visitor)
    return JSONResponse({"ok": True})


@app.post("/refund")
def refund(visitor: str = Form(...)):
    flagsmith.track_event("refund_requested", identifier=visitor)
    return JSONResponse({"ok": True})


@app.get("/cta")
def cta(visitor: str):
    """What this visitor is served right now, without recording an exposure."""
    flag = flagsmith.get_identity_flags(visitor).get_flag(FLAG)
    return {"visitor": visitor, "value": flag.value, "enabled": flag.enabled,
            "in_experiment": bool(getattr(flag, "experiment", None) and flag.experiment.in_experiment)}


@app.get("/healthz")
def healthz(deep: bool = False):
    """``?deep=1`` also checks that Flagsmith's environment document has loaded (the page needs it)."""
    if deep:
        try:
            flagsmith.get_identity_flags("healthcheck-probe").get_flag(FLAG)
        except Exception as exc:  # noqa: BLE001 - any failure means the page cannot serve the experiment
            return JSONResponse({"ok": False, "detail": f"Flagsmith not ready: {exc}"}, status_code=503)
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
