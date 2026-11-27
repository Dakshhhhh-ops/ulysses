"""The plain-language explainer page: public, drawn from committed data only, no dates."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ulysses.service import explain, fmt
from ulysses.service.app import create_app

ROOT = Path(__file__).resolve().parents[1]
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{1,2}:\d{2}(:\d{2})?\b|"
                  r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\b")


@pytest.fixture
def client(world, monkeypatch, tmp_path):
    svc, _, _ = world
    monkeypatch.setenv("ULYSSES_CALIBRATION_DIR", str(tmp_path / "none"))  # no local run: committed data only
    return TestClient(create_app(svc, agent_token="t" * 20))


def _peek_rate() -> str:
    aa = json.loads((ROOT / "data" / "offline.json").read_text(encoding="utf-8"))["aa"]
    return fmt.rate(aa["banner_every_look"]["winning"], aa["banner_every_look"]["runs"])


def _visible(html: str) -> str:
    html = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def test_page_is_public_and_renders(client):
    r = client.get("/explain")           # no read capability, no cookie: the page is for anyone
    assert r.status_code == 200
    assert "New here?" in r.text and "Tied to the mast" in r.text
    assert r.text.count("<h1") == 1      # the Two Ships partial drops to an h2 here


def test_false_winner_rate_is_computed_from_committed_data(client):
    rate = _peek_rate()
    assert rate == "13.4%"               # the README's claim, and what data/offline.json says
    html = client.get("/explain").text
    assert f'<p class="xp-big">{rate}</p>' in html
    assert 'id="two-ships"' in html      # the Two Ships hero, from data/paths-offline.json


def test_tempted_run_is_read_from_the_recording():
    st = explain.tempted()
    assert st and st["treatment"] == "bold"
    assert [s["ctw"] for s in st["sirens"]] == ["97.39%", "96.47%"]
    assert [s["needs"] for s in st["sirens"]] == ["99.57%", "98.75%"]
    assert st["refused"] == 2 and st["end"]["ctw"] == "91.91%" and st["kept_control"]


def test_tempted_numbers_reach_the_page(client):
    text = _visible(client.get("/explain").text)
    for v in ("97.39%", "96.47%", "91.91%"):
        assert v in text


def test_nav_and_index_link_to_the_page(client, world):
    from .conftest import read_all

    svc, _, _ = world
    html = client.get("/explain").text
    assert re.search(r'<a href="/explain" class="nav-new" aria-current="page">New here\?</a>', html)
    cal = client.get("/calibration").text
    assert 'href="/explain"' in cal
    assert 'href="/explain"' in client.get("/").text                       # the locked page too
    assert "New here? Start here" in read_all(client, svc).get("/").text   # and the contract list


def test_no_dates_or_clock_times(client):
    text = _visible(client.get("/explain").text)
    assert not DATE.search(text), DATE.search(text)
    src = (ROOT / "src" / "ulysses" / "service" / "templates" / "explain.html").read_text(encoding="utf-8")
    assert not DATE.search(src)


def test_missing_recording_degrades(client, monkeypatch, tmp_path):
    monkeypatch.setattr(explain, "TEMPTED", tmp_path / "missing.jsonl")
    r = client.get("/explain")
    assert r.status_code == 200 and "could not be read" in r.text
