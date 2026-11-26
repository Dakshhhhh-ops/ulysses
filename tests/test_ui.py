"""The web UI's honesty rules: boundaries never print as 100%, the SVG and WebGL drawings
share one projection, calibration counts match the scene, and the built bundle is current."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ulysses import contract as ct
from ulysses.service import calibration_view, fmt, scene as sc
from ulysses.service.app import create_app

from .conftest import make_contract, sign_as

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "web" / "src" / "parity-fixture.json"


# ---- the formatter --------------------------------------------------------------------------

def test_no_boundary_formats_as_100_percent():
    for looks in (10, 30):
        for p in ct.planned_table(ct.Horizon(identities_per_arm=3000, looks=looks), 0.05):
            if p.z is None:
                continue
            text = fmt.boundary_chance(p.chance_to_win, p.z)
            assert not re.fullmatch(r"100(\.0+)?%", text), (looks, p.look, text)
            assert "100." not in text and text != "100%"
            assert fmt.loss_boundary(p.chance_to_win, p.z) not in ("0%", "0.00%")


def test_30_look_early_boundaries():
    planned = ct.planned_table(ct.Horizon(identities_per_arm=3000, looks=30), 0.05)
    texts = [fmt.boundary_with_z(p.chance_to_win, p.z) for p in planned[2:5]]
    assert texts[0] == "more than 99.999999% (z 6.99)"
    assert texts[1].startswith("99.99999") and texts[1].endswith("(z 6.03)")
    assert texts[2].endswith("(z 5.37)") and "100" not in texts[2]
    assert fmt.boundary_chance(None, None) == "no stop possible"


def test_flagsmith_chance_is_two_decimals_but_never_100():
    assert fmt.ctw(0.96) == "96.00%"
    assert fmt.ctw(0.999996) == "99.9996%"
    assert fmt.ctw(None) == "n/a"


# ---- the shared projection ----------------------------------------------------------------------

def _fixture_scene() -> dict:
    planned = [p.model_dump() for p in ct.planned_table(ct.Horizon(identities_per_arm=3000, looks=30), 0.05)]
    hist = [
        {"seq": 1, "t": 0.0133, "z": None, "counted": False, "look_number": None, "boundary_z": None},
        {"seq": 2, "t": 0.1, "z": 0.2, "counted": True, "look_number": 3, "boundary_z": 6.9913},
        {"seq": 3, "t": 0.1467, "z": 1.7, "counted": False, "look_number": None, "boundary_z": None},
        {"seq": 4, "t": 0.1667, "z": 1.91, "counted": True, "look_number": 5, "boundary_z": 5.3679},
        {"seq": 5, "t": 0.21, "z": 9.2, "counted": True, "look_number": 6, "boundary_z": 4.7},
    ]
    return {"planned": sc.planned_rows(planned), "history": hist, "current_seq": 5,
            "decision": {"look_number": 6, "outcome": "win"}, "proposal": {"state": "executed"},
            "proposed": sc.planned_rows(planned[:4])}


def test_parity_fixture_matches_python_elevation():
    """web/src/parity.test.js holds the JS twin to the same file."""
    scene = _fixture_scene()
    got = {"scene": scene, "elevation": sc.elevation(scene)}
    if not FIXTURE.exists():
        FIXTURE.write_text(json.dumps(got, indent=1), encoding="utf-8")
    want = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert want["scene"] == json.loads(json.dumps(scene))
    assert want["elevation"] == json.loads(json.dumps(got["elevation"]))


def test_rings_radius_is_k_times_z_and_null_looks_are_dashed_at_zmax():
    scene = _fixture_scene()
    e = sc.elevation(scene)
    by_k = {r["k"]: r for r in e["rings"] if r["state"] != sc.GHOST}
    assert by_k[1]["state"] == sc.NOSTOP and by_k[1]["r"] == pytest.approx(sc.K * sc.Z_MAX)
    assert by_k[10]["r"] == pytest.approx(sc.K * scene["planned"][9]["z"], abs=1e-6)
    assert by_k[6]["state"] == sc.WIN and by_k[6]["open"] == 1.0 and by_k[6]["side"] == 1
    # a look after its planned count: the planned ring becomes a ghost, the applied one is drawn
    assert any(r["state"] == sc.GHOST and r["k"] == 6 for r in e["rings"])
    # the thread breaks where Flagsmith had no chance to win; that refresh draws a tick on the mast
    assert len(e["thread"]) == 1 and e["beads"][0]["kind"] == "tick"
    assert e["beads"][2]["kind"] == "hollow"
    assert e["beads"][4]["clamped"] is True and e["beads"][4]["y"] == pytest.approx(sc.K * sc.Z_MAX)


# ---- calibration --------------------------------------------------------------------------------

def _trace(tmp_path: Path) -> Path:
    rows = []
    chances = {  # experiment -> chance per look (8 looks)
        1: [0.5, 0.6, 0.97, 0.7, 0.6, 0.5, 0.55, 0.52],      # banner call at look 3, no contract call
        2: [0.4, 0.2, 0.03, 0.001, 1e-5, 1e-6, 1e-7, 1e-8],  # banner at 3, contract loss later
        3: [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
        4: [0.5, 0.6],                                         # did not finish: left out
    }
    for e, chs in chances.items():
        for k, ch in enumerate(chs, start=1):
            rows.append({"batch": "t", "experiment": e, "look": k, "n_control": 100 * k, "n_treatment": 100 * k,
                         "chance_to_win": ch})
    header = {"design": {"batch": "t", "experiments": 4, "looks": 8, "per_arm_per_look": 100, "horizon": 800,
                         "alpha": 0.05, "rate": 0.1}}           # traces now open with their signed design
    p = tmp_path / "live-t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [header, *rows]), encoding="utf-8")
    return p


def test_calibration_exit_points_equal_the_table_counts(tmp_path):
    view = calibration_view.live_view(_trace(tmp_path).parent)
    banner_points = sum(1 for r in view["paths"] if r["banner"])
    contract_points = sum(1 for r in view["paths"] if r["contract"])
    assert banner_points == view["rows"][0]["k"] == view["counts"]["banner"] == 2
    row05 = next(r for r in view["rows"] if r.get("level") == "0.05")
    assert contract_points == row05["k"] == view["counts"]["contract"] == 1
    assert len(view["paths"]) == 4                 # every experiment is kept, as summarise_trace does


def test_calibration_without_files_shows_no_figures(tmp_path):
    assert calibration_view.calibration_page(tmp_path) == {"offline": None, "live": None, "run_dir": str(tmp_path)}


# ---- pages --------------------------------------------------------------------------------------

@pytest.fixture
def client(world, monkeypatch, tmp_path):
    svc, fs, store = world
    monkeypatch.setenv("ULYSSES_CALIBRATION_DIR", str(tmp_path / "none"))
    return TestClient(create_app(svc, agent_token="t" * 20)), svc, fs


def test_pages_keep_their_ids_and_label_the_source(client, keys, clock):
    c, svc, fs = client
    h = svc.submit(make_contract(keys, looks=4, n=400))["hash"]
    page = c.get(f"/sign/{h}").text
    for i in ('id="sign-btn"', 'id="sign-panel"', 'data-hash=', 'id="role"', 'id="msg"', 'id="key-note"', "data-message-for="):
        assert i in page
    assert "Flagsmith docs" in page and "Flagsmith dashboard" not in page
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    out = None
    for n, ch in ((100, 0.7), (200, 0.9), (300, 0.9999)):
        clock.advance(301)
        fs.set_results(1, n, n, ch, {"refunds": 0.5})
        out = svc.look(h)
    card = c.get(f"/card/{out['look_id']}").text
    for i in ('id="confirm-btn"', 'id="confirm-panel"', "data-proposal=", 'id="confirm-text"', 'id="key-note"', 'id="msg"'):
        assert i in card
    assert "Source not recorded" in card            # nothing recorded for this experiment: the chip says so
    assert "not computed by Flagsmith" in card       # srm_p_value is None
    assert not re.search(r">\s*100(\.0+)?%", card)


def test_calibration_page_with_no_files_has_no_numbers(client):
    c, _, _ = client
    page = c.get("/calibration?tab=lab").text
    assert "Not run yet" in page and "calibrate offline" in page
    body = page.split('<main id="main">', 1)[1]
    assert not re.search(r"\d+\.\d%", body)


def test_built_bundle_is_current():
    dist = ROOT / "src" / "ulysses" / "service" / "static" / "dist"
    assert (dist / "instrument.js").exists(), "run `npm run build` in web/"
    recorded = (dist / "BUILD").read_text().split()[-1]
    h = hashlib.sha256()
    src = ROOT / "web" / "src"
    # same walk as web/build.mjs: directory entries sorted by name, .js/.mjs/.json, tests excluded
    def walk(d: Path):
        for e in sorted(d.iterdir(), key=lambda p: p.name):
            if e.is_dir():
                yield from walk(e)
            elif re.search(r"\.(js|mjs|json)$", e.name) and not e.name.endswith(".test.js"):
                yield e
    for p in walk(src):
        h.update(p.relative_to(ROOT / "web").as_posix().encode())
        h.update(p.read_text(encoding="utf-8").replace("\r\n", "\n").encode())
    h.update(b"elevation_constants.json")
    h.update((ROOT / "src" / "ulysses" / "service" / "elevation_constants.json").read_text(encoding="utf-8").replace("\r\n", "\n").encode())
    assert h.hexdigest() == recorded, "web/ sources changed since the bundle was built: run `npm run build` in web/"


# ---- no calendar date or clock time on any page -----------------------------------------------------

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}(:\d{2})?\b|\b(January|February|March|April|May|June|July|August|"
                   r"September|October|November|December)\b|\b(Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.? \d")


def test_no_page_renders_a_date_or_time(world, keys, clock, tmp_path, monkeypatch):
    """Flagsmith's results carry an ISO ``as_of`` and live traces carry them per read; none may reach a page."""
    from dataclasses import replace

    svc, fs, store = world
    cal = tmp_path / "cal"
    cal.mkdir()
    header = {"design": {"batch": "z", "experiments": 2, "looks": 2, "per_arm_per_look": 100, "horizon": 200,
                         "alpha": 0.05, "rate": 0.1}}
    reads = [{"batch": "z", "experiment": e, "look": k, "n_control": 100 * k, "n_treatment": 100 * k,
              "chance_to_win": ch, "as_of": "2001-02-03T04:05:06.000000Z"}
             for e, chs in ((1, (0.5, 0.97)), (2, (0.4, 0.3))) for k, ch in enumerate(chs, start=1)]
    (cal / "live-z.jsonl").write_text("\n".join(json.dumps(r) for r in [header, *reads]), encoding="utf-8")
    monkeypatch.setenv("ULYSSES_CALIBRATION_DIR", str(cal))
    client = TestClient(create_app(svc, agent_token="t" * 20))

    unsigned = svc.submit(make_contract(keys, looks=4, n=400, experiment=1))["hash"]
    pages = ["/", f"/sign/{unsigned}"]
    sign_as(svc, keys, unsigned)
    pages.append(f"/sign/{unsigned}")
    svc.start_under_contract(unsigned)
    out = None
    for n, ch in ((100, 0.7), (200, 0.97), (300, 0.99999)):
        clock.advance(301)
        fs.set_results(1, n, n, ch, {"refunds": 0.5})
        fs.results[1] = replace(fs.results[1], as_of="2001-02-03T04:05:06.000000Z")
        out = svc.look(unsigned)
        pages.append(f"/card/{out['look_id']}")
    if out.get("proposal"):
        pages.append(f"/card/{out['proposal']['id']}")
    pages += ["/", "/calibration?tab=live", "/calibration?tab=lab", f"/enrol/{svc.enrol_token('maya')}",
              "/card/L9999-nothing"]
    for page in pages:
        html = client.get(page).text
        body = html.split("<body>", 1)[-1]
        hit = _DATE.search(body)
        assert hit is None, f"{page} shows {hit.group(0)!r}"
