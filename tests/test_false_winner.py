"""Counting a live false-winner trace (synthetic rows, no Flagsmith)."""

import json

import pytest

from ulysses.calibrate import false_winner

DESIGN = {"batch": "abc123", "experiments": 3, "looks": 3, "per_arm_per_look": 100, "horizon": 300,
          "alpha": 0.05, "rate": 0.1, "min_relative_lift": 0.5, "baseline_rate": 0.1, "top_up_looks": 0}


def _row(exp, look, chance, outcome, *, counted=True, stale=False, fired=None, attempt=1, look_number="same"):
    r = {"batch": "abc123", "experiment": exp, "look": look, "attempt": attempt, "n_control": 100 * look,
         "n_treatment": 100 * look, "chance_to_win": chance, "stale": stale, "look_id": f"L{look:04d}-x{exp}",
         "contract": {"outcome": outcome, "counted": counted,
                      "look_number": look if look_number == "same" else look_number, "boundary_chance": 0.9999}}
    if fired:
        r["fired"] = fired
    return r


def _write(tmp_path, rows, design=DESIGN):
    p = tmp_path / "trace.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in [{"design": design}, *rows]) + "\n", encoding="utf-8")
    return p


def test_counts_winning_reads_the_contract_held_and_what_it_shipped(tmp_path):
    keep = {"outcome": "horizon", "action": "keep_control", "state": "executed", "serving": "same"}
    rows = [
        _row(1, 1, 0.97, "hold"), _row(1, 2, 0.96, "hold"), _row(1, 3, 0.70, "horizon", fired=keep),
        _row(2, 1, 0.50, "hold"), _row(2, 2, 0.03, "hold"), _row(2, 3, 0.40, "horizon", fired=keep),
        # A stale re-read repeats the previous numbers and is not a new reading.
        _row(3, 1, 0.60, "hold"), _row(3, 2, 0.98, "watch", counted=False, stale=True),
        _row(3, 2, 0.62, "hold", attempt=2), _row(3, 3, 0.55, "hold"),
    ]
    s = false_winner.count(_write(tmp_path, rows))
    assert s["experiments"] == 3
    assert s["over_95_while_holding"]["experiments"] == 1
    assert s["over_95_while_holding"]["looks"] == {"1": [{"contract_look": 1, "scheduled_round": 1},
                                                         {"contract_look": 2, "scheduled_round": 2}]}
    assert s["over_95_while_holding"]["first_look_histogram"] == [
        {"contract_look": 1, "scheduled_round": 1, "experiments": 1}]
    assert s["under_5_while_holding"]["experiments"] == 1
    assert s["either_while_holding"] == 2
    assert s["contract_outcomes"] == {"horizon": 2, "still holding": 1}
    assert s["pre_signed_actions"] == {"keep_control (executed)": 2}
    assert s["contract_shipped_treatment"] == 0
    assert s["hold_cards_over_95"][0]["look_id"] == "L0001-x1"
    assert "1 of 3 experiments" in s["text"]


def test_a_win_that_fires_is_not_a_held_winning_read_and_counts_as_shipped(tmp_path):
    ship = {"outcome": "win", "action": "ship_treatment", "state": "executed", "serving": "same"}
    rows = [_row(1, 1, 0.9999, "win", fired=ship), {"batch": "abc123", "experiment": 2,
                                                    "operator": "completed after the run, kept control"}]
    s = false_winner.count(_write(tmp_path, rows))
    assert s["over_95_while_holding"]["experiments"] == 0
    assert s["contract_shipped_treatment"] == 1
    assert s["contract_outcomes"] == {"win": 1}
    assert s["ended_by_operator_after_the_run"] == 1


def test_a_not_ready_first_round_shifts_the_contract_look_and_is_reported_by_the_contracts_number(tmp_path):
    # Round 1 comes back not_ready twice (no reading, not counted); every later round N is contract look N-1.
    loss = {"outcome": "loss", "action": "keep_control", "state": "executed", "serving": "same"}
    rows = [
        _row(1, 1, None, "not_ready", counted=False, look_number=None),
        _row(1, 1, None, "not_ready", counted=False, look_number=None, attempt=2),
        _row(1, 2, 0.97, "hold", look_number=1),
        _row(1, 3, 0.02, "hold", look_number=2),
        _row(1, 4, 0.0001, "loss", look_number=3, fired=loss),
    ]
    s = false_winner.count(_write(tmp_path, rows))
    assert s["looks_recorded"] == 5
    assert s["looks_not_ready"] == 2
    assert s["over_95_while_holding"]["looks"] == {"1": [{"contract_look": 1, "scheduled_round": 2}]}
    assert s["over_95_while_holding"]["first_look_histogram"] == [
        {"contract_look": 1, "scheduled_round": 2, "experiments": 1}]
    assert s["under_5_while_holding"]["looks"] == {"1": [{"contract_look": 2, "scheduled_round": 3}]}
    card = s["hold_cards_over_95"][0]
    assert (card["contract_look"], card["scheduled_round"]) == (1, 2)
    assert "contract look 1 (scheduled round 2): 1" in s["text"]
    assert "2 of them not ready" in s["text"]
    assert s["contract_outcomes"] == {"loss": 1}


def test_a_trace_without_its_design_is_refused(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(_row(1, 1, 0.5, "hold")) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        false_winner.count(p)
