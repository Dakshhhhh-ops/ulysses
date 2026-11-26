"""The live calibration is scored only under the design signed before the run (review finding 3)."""

import json
import re

import pytest

from ulysses.calibrate import live


def _trace(tmp_path, looks_signed=4, looks_run=4, header=True):
    rows = []
    if header:
        rows.append({"design": {"batch": "t", "experiments": 2, "looks": looks_signed, "per_arm_per_look": 100,
                                "horizon": 100 * looks_signed, "alpha": 0.05, "rate": 0.1}})
    chances = {1: [0.5, 0.97, 0.6, 0.5], 2: [0.5, 0.5, 0.5, 0.5]}
    for e, chs in chances.items():
        for k, c in enumerate(chs[:looks_run], start=1):
            rows.append({"batch": "t", "experiment": e, "look": k, "n_control": 100 * k, "n_treatment": 100 * k,
                         "chance_to_win": c, "as_of": "2000-01-01T00:00:00Z"})
    p = tmp_path / "live-t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def test_complete_run_is_scored_under_its_signed_design(tmp_path):
    s = live.summarise_trace(_trace(tmp_path))
    assert s["design"]["looks"] == 4 and not s["truncated"]
    assert s["banner_any_look"] == {"winning": 1, "losing": 0}
    assert s["contract"]["horizon"] == 2 and s["contract"]["holding"] == 0


def test_a_different_design_after_the_run_is_refused(tmp_path):
    with pytest.raises(ValueError, match="refusing to score"):
        live.summarise_trace(_trace(tmp_path), signed_looks=3)


def test_truncated_run_reports_holding_and_never_spends_the_final_look_early(tmp_path):
    s = live.summarise_trace(_trace(tmp_path, looks_signed=6, looks_run=4))
    assert s["truncated"] and s["looks_read"] == 4
    assert s["contract"]["horizon"] == 0 and s["contract"]["holding"] == 2
    assert s["banner_final_look"] is None
    assert "stopped after look 4 of 6" in s["markdown"]


def test_trace_without_header_needs_the_values_it_was_started_with(tmp_path):
    p = _trace(tmp_path, header=False)
    with pytest.raises(ValueError, match="no design header"):
        live.summarise_trace(p)
    assert live.summarise_trace(p, signed_looks=4, signed_per_arm=100)["design"]["looks"] == 4


def test_shareable_trace_has_no_timestamps(tmp_path):
    out = live.strip_trace(_trace(tmp_path), tmp_path / "shared.jsonl")
    assert not re.search(r"\d{4}-\d{2}-\d{2}", out.read_text())
    assert live.summarise_trace(out)["banner_any_look"]["winning"] == 1
