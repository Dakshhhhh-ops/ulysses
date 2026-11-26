"""Two Ships: the offline per-run export and the calibration hero drawn from it."""

from __future__ import annotations

import json
import math
import re
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ulysses.calibrate import offline
from ulysses.calibrate.flagsmith_source import PINNED_SHA256
from ulysses.service import calibration_view, two_ships
from ulysses.service.app import create_app

from .conftest import read_all

ROOT = Path(__file__).resolve().parents[1]


def _fake_engine():
    """A stand-in for Flagsmith's stats.py (normal approximation), so the export is tested without the download."""
    stats = types.SimpleNamespace()
    stats.VariantStats = lambda n, sum, sum_squares: (n, sum)  # noqa: A002 - Flagsmith's own argument names

    def compare_to_control(c, t):
        (n1, s1), (n2, s2) = c, t
        p1, p2 = s1 / n1, s2 / n2
        se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
        return types.SimpleNamespace(chance_to_win=0.5 * (1 + math.erf((p2 - p1) / se / math.sqrt(2))))

    stats.compare_to_control = compare_to_control
    consts = types.SimpleNamespace(RESULTS_MIN_IDENTITIES_PER_VARIANT=100, RESULTS_MIN_CONVERSIONS_PER_VARIANT=5)
    prov = {"stats.py": {"origin": "fake", "sha256": "f" * 64, "tag": "test"}}
    return stats, consts, prov


KW = dict(looks=12, per_arm_per_look=100, seed=4242, guardrail_rate=0.10)


def test_export_is_deterministic(tmp_path):
    a = offline.export_paths(tmp_path / "a", runs=30, seed=4242, looks=12, engine=_fake_engine())
    b = offline.export_paths(tmp_path / "b", runs=30, seed=4242, looks=12, engine=_fake_engine())
    assert a.read_bytes() == b.read_bytes()
    data = json.loads(a.read_text())
    assert len(data["runs"]) == 30 and all(len(r["chance_to_win"]) == 12 for r in data["runs"])
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}", a.read_text())  # no dates or clock times


def test_export_counts_match_the_summary_for_the_same_runs():
    sim = offline.simulate(runs=60, keep_paths=60, engine=_fake_engine(), **KW)
    ex = offline.paths_export(sim)
    s = sim["banner_every_look"]
    assert ex["counts"]["first_exit"] == {"winning": s["winning"], "losing": s["losing"]}
    for a, c in sim["contract"].items():
        assert ex["counts"]["contract"][a] == {"win": c["winning"], "loss": c["losing"]}
    assert s["winning"] > ex["counts"]["contract"]["0.1"]["win"]  # the fake engine still shows the peeking gap


def test_kept_runs_are_the_opening_runs_of_a_longer_simulation():
    short = offline.simulate(runs=20, keep_paths=20, engine=_fake_engine(), **KW)["_paths"]
    long = offline.simulate(runs=50, keep_paths=20, engine=_fake_engine(), **KW)["_paths"]
    assert short == long


def test_committed_sample_is_consistent():
    data = json.loads((ROOT / "data" / offline.PATHS_FILE).read_text(encoding="utf-8"))
    assert data["engine"]["stats.py"]["sha256"] == PINNED_SHA256["stats.py"]
    assert data["parameters"]["seed"] == offline.DEFAULT_SEEDS[0] and len(data["runs"]) == data["parameters"]["runs"]
    wins = sum(1 for r in data["runs"] if r["first_exit"] and r["first_exit"]["side"] == "winning")
    assert wins == data["counts"]["first_exit"]["winning"]
    for a, c in data["counts"]["contract"].items():
        assert c["win"] == sum(1 for r in data["runs"] if (r["contract"][a] or {}).get("outcome") == "win")


# ---- the page ------------------------------------------------------------------------------------

def _offline_json(engine_sha: str, seed, looks=12, runs=10000) -> dict:
    def s(w, l):
        return {"runs": runs, "winning": w, "losing": l, "any_call": w + l}
    return {"aa": {"label": "Offline engine calibration on simulated synthetic units (not user traffic)",
                   "engine": {"stats.py": {"sha256": engine_sha, "tag": "test"}},
                   "parameters": {"runs": runs, "looks": looks, "identities_per_arm_per_look": 100, "base_rate": 0.1,
                                  "relative_lift": 0.0, "seed": seed},
                   "banner_every_look": s(1337, 3100), "banner_final_look_only": s(390, 590),
                   "contract": {"0.05": s(134, 401), "0.1": s(300, 710)}}}


def _cal_dir(tmp_path: Path, with_export=True) -> tuple[Path, dict]:
    d = tmp_path / "cal"
    d.mkdir()
    (d / "offline.json").write_text(json.dumps(_offline_json("f" * 64, [4242, 1, 2, 3, 4])), encoding="utf-8")
    sim = offline.simulate(runs=100, keep_paths=100, engine=_fake_engine(), **KW)
    ex = offline.paths_export(sim)
    if with_export:
        (d / offline.PATHS_FILE).write_text(offline.dump_export(ex), encoding="utf-8")
    return d, ex


@pytest.fixture
def page(world, monkeypatch, tmp_path):
    svc, _, _ = world

    def get(d: Path) -> str:
        monkeypatch.setenv("ULYSSES_CALIBRATION_DIR", str(d))
        return read_all(TestClient(create_app(svc, agent_token="t" * 20)), svc).get("/calibration?tab=lab").text
    return get


def test_hero_renders_counts_from_the_export_and_rates_from_offline_json(page, tmp_path):
    d, ex = _cal_dir(tmp_path)
    html = page(d)
    hero = html.split('id="two-ships"', 1)[1].split("</section>", 1)[0]
    left, right = ex["counts"]["first_exit"]["winning"], ex["counts"]["contract"]["0.1"]["win"]
    nums = re.findall(r'data-ts-count data-final="(\d+)"[^>]*>(\d+)<', hero)
    assert nums == [(str(left), str(left)), (str(right), str(right))]   # final state in the HTML, no JS needed
    assert hero.count('class="ts-run ship"') == left + right
    assert hero.count('class="ts-wreck"') == left + right
    for rate in ("13.4%", "3.0%", "1.3%"):                               # from offline.json, never typed
        assert f"<b>{rate}</b>" in hero
    assert "Every variation here does nothing. Each red ship is a change that would have gone live anyway." in hero
    visible = re.sub(r"<details.*?</details>", "", hero, flags=re.S)
    visible = re.sub(r"<[^>]+>|<script.*?</script>", " ", re.sub(r"<script.*?</script>", "", visible, flags=re.S))
    assert not re.search(r"\balpha\b|\bz\b", visible, flags=re.I)        # plain language; the details say alpha
    assert "Per-run paths not exported" not in html
    assert html.count("<h1") == 1


def test_lab_fan_counts_equal_the_export(tmp_path):
    d, ex = _cal_dir(tmp_path)
    v = calibration_view.offline_view(d)
    assert len(v["paths"]) == 100
    assert v["counts"]["banner"] == sum(ex["counts"]["first_exit"].values())
    assert v["counts"]["contract"] == sum(ex["counts"]["contract"]["0.05"].values())


def test_no_export_and_no_matching_sample_keeps_the_old_note(page, tmp_path):
    d, _ = _cal_dir(tmp_path, with_export=False)
    html = page(d)
    assert 'id="two-ships"' not in html and "Per-run paths not exported" in html


def test_export_from_another_design_is_not_drawn(tmp_path):
    d, ex = _cal_dir(tmp_path)
    data = json.loads((d / "offline.json").read_text())
    data["aa"]["parameters"]["looks"] = 30
    (d / "offline.json").write_text(json.dumps(data))
    assert two_ships.build(d, data["aa"], sample=None) is None


def test_committed_sample_is_used_for_a_matching_run_without_its_own_export(tmp_path):
    d = tmp_path / "cal"
    d.mkdir()
    aa = _offline_json(PINNED_SHA256["stats.py"], list(offline.DEFAULT_SEEDS), looks=30)["aa"]
    hero = two_ships.build(d, aa)
    assert hero and not hero.get("error") and hero["origin"] == "committed"
    sample = json.loads(two_ships.SAMPLE.read_text(encoding="utf-8"))
    assert hero["left"]["ships"] == sample["counts"]["first_exit"]["winning"]


def test_empty_calibration_still_has_no_hero(page, tmp_path):
    html = page(tmp_path / "none")
    assert 'id="two-ships"' not in html and "Not run yet" in html
