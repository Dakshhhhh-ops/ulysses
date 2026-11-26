"""The "drawing slowly" fallback keeps the flat SVG drawing on screen.

When the instrument's first 30 animated frames average more than 24 ms, ``checkSpeed`` hides the
canvas and shows the SVG. The render loop must stop there; if it ran one more frame it would set
``data-ready="gl"`` again and the CSS would hide the SVG, leaving the panel blank.

This drives a real page in headless Chromium (SwiftShader WebGL) with ``requestAnimationFrame``
slowed to about 40 ms a frame, so the speed check fires on its own. It is skipped when the
Playwright package or a Chromium build is not installed. Set ``ULYSSES_CHROMIUM`` to a chrome
executable to use a specific build.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from ulysses.service.app import create_app

from .conftest import make_contract, sign_as

sync_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

SLOW_RAF = """(() => {
  window.requestAnimationFrame = (cb) => setTimeout(() => cb(performance.now()), 40);
})();"""

STATE = """() => {
  const st = document.querySelector('.plate-stage');
  const svg = st.querySelector('svg.elevation');
  const cs = getComputedStyle(svg);
  const cv = st.querySelector('canvas');
  return {ready: st.dataset.ready, flat: document.querySelector('figure.plate').dataset.flat,
          note: st.querySelector('.plate-note')?.textContent || '', visibility: cs.visibility, display: cs.display,
          shapes: svg.children.length, canvas: cv ? getComputedStyle(cv).display : null};
}"""


def _held_card(svc, fs, keys, clock) -> str:
    h = svc.submit(make_contract(keys, looks=10, n=4000))["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    out = None
    for n, ch in ((400, 0.66), (800, 0.88), (1200, 0.96)):
        clock.advance(301)
        fs.set_results(1, n, n, ch, {"refunds": 0.5})
        out = svc.look(h)
    return out["look_id"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(world, keys, clock, monkeypatch, tmp_path):
    svc, fs, _ = world
    monkeypatch.setenv("ULYSSES_CALIBRATION_DIR", str(tmp_path / "none"))
    look_id = _held_card(svc, fs, keys, clock)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(svc, agent_token="t" * 20), host="127.0.0.1", port=port,
                                           log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/card/{look_id}"
    finally:
        server.should_exit = True
        t.join(timeout=10)


def test_slow_device_fallback_leaves_the_svg_visible(served):
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=os.environ.get("ULYSSES_CHROMIUM") or None,
                                        args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        except Exception as exc:  # no Chromium build installed for this Playwright version
            pytest.skip(f"no Chromium for Playwright: {str(exc).splitlines()[0]}")
        try:
            ctx = browser.new_context(viewport={"width": 1280, "height": 1100}, reduced_motion="no-preference")
            ctx.add_init_script(SLOW_RAF)
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(served, wait_until="networkidle")
            page.wait_for_function("() => document.querySelector('figure.plate')?.dataset.flat === 'true'", timeout=30000)
            page.wait_for_timeout(1500)   # frames that would run after the check, if the loop had kept going
            s = page.evaluate(STATE)
            ctx.close()
        finally:
            browser.close()
    assert not errors, errors
    assert "drawing slowly" in s["note"], s
    assert s["ready"] == "svg", s
    assert s["visibility"] == "visible" and s["display"] != "none", s
    assert s["shapes"] > 0 and s["canvas"] == "none", s
