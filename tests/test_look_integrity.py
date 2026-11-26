"""Look timing, the alpha ledger, amendments, overruns and concurrent counting.

Each test reproduces one way the guarantee "a do-nothing variation ships at most alpha/2 of the time"
could leak, through the service's real code path:

* an agent that can read z between looks and calls ``look`` only when z is high picks its own analysis
  times from the data;
* a horizon amended below an already counted look froze the contract;
* a final read past the horizon was analysed as if it had landed on the horizon;
* after an amendment the boundaries were re-derived as if the new horizon had always applied;
* two service processes on one database could both count the same planned look.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from statistics import NormalDist

import numpy as np
import pytest

from ulysses import boundary as bd
from ulysses import core as core_mod
from ulysses.core import Invalid, Settings, Ulysses
from ulysses.decision import Observation, evaluate
from ulysses.flagsmith.fake import FakeFlagsmith
from ulysses.store import Store

from .conftest import make_contract, sign_as

PHI = NormalDist().cdf


def started(svc, keys, **kw):
    h = svc.submit(make_contract(keys, **kw))["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    return h


def amend_and_sign(svc, keys, h, changes, reason="traffic lower than planned"):
    out = svc.amend(h, changes, reason=reason)
    sign_as(svc, keys, out["hash"], "maya", "owner")
    sign_as(svc, keys, out["hash"], "sam", "second_signer")
    return out["hash"]


def applied_boundary(decision: dict) -> float | None:
    """The boundary a counted look applied: the ledger's full-precision value when present."""
    led = decision.get("ledger") or {}
    if "boundary_applied" in led:
        return led["boundary_applied"]
    return decision.get("boundary_z")


def peek_runs() -> int:
    return int(os.environ.get("ULYSSES_PEEK_RUNS", "250"))


# ---- finding 1: the agent must not choose when a look counts ------------------------------------------


def test_strategic_peeking_agent_cannot_raise_false_wins(keys, clock):
    """An agent polls ``read_experiment`` at every refresh, and calls ``look`` only when the z it saw would
    cross the boundary it would get by looking right then. With no effect, this agent must end with exactly
    the outcome an honest agent gets by looking at the planned counts, on every path.

    Runs through the service (store, contract, fake Flagsmith); only the z paths come from numpy.
    Fixed seed; 250 paths by default (ULYSSES_PEEK_RUNS overrides it for the figures in STATISTICS.md)."""
    n_horizon, looks, grid, runs = 1000, 5, 25, peek_runs()
    fs = FakeFlagsmith(clock=clock)
    store = Store(":memory:")
    svc = Ulysses(store, fs, Settings(public_url="http://test", refresh_wait_seconds=0, poll_seconds=0),
                  clock=clock, sleep=lambda s: clock.advance(s))
    svc.register_signer("maya", keys["maya"].public)
    svc.register_signer("sam", keys["sam"].public)

    ts = np.arange(1, grid + 1) / grid
    rng = np.random.default_rng(20_311)
    z = np.cumsum(rng.standard_normal((runs, grid)) * math.sqrt(1 / grid), axis=1) / np.sqrt(ts)
    planned = [grid * k // looks - 1 for k in range(1, looks + 1)]
    signed = bd.spending_boundaries([k / looks for k in range(1, looks + 1)], 0.05)

    def honest(path: np.ndarray) -> bool:
        for j, b in zip(planned, signed):
            if b is None:
                continue
            if path[j] >= b:
                return True
            if path[j] <= -b:
                return False
        return False

    agent_wins = honest_wins = differ = 0
    for i in range(runs):
        exp = 1000 + i
        fs.add_experiment(exp)
        h = started(svc, keys, n=n_horizon, looks=looks, experiment=exp)
        # Reads before the first planned count are skipped: no path, old or new, can analyse there.
        for g in range(grid // looks - 1, grid):
            n = n_horizon * (g + 1) // grid
            fs.set_results(exp, n, n, PHI(float(z[i, g])))
            clock.advance(300)
            seen = svc.read_experiment("env-key", exp)
            if store.contract_row(h)["state"] != "active":
                break
            chance = ((seen.get("results") or {}).get("metrics") or {}).get("purchase", {}).get("chance_to_win")
            if chance is None:
                continue
            prior = [lk["identities_per_arm"] / n_horizon
                     for lk in svc.experiment_view("env-key", exp)["looks"] if lk["counted"]]
            if (prior and prior[-1] >= n / n_horizon) or n < n_horizon * (len(prior) + 1) / looks:
                continue  # not a planned count the agent could analyse at
            b = bd.spending_boundaries([*prior, n / n_horizon], 0.05, final_spends_all=n >= n_horizon)[-1]
            if b is not None and bd.chance_to_z(chance) >= b:
                svc.look(h)
                break
        won = any(p["outcome"] == "win" for p in store.proposals_for(h))
        ref = honest(z[i])
        agent_wins += won
        honest_wins += ref
        differ += won != ref
    print(f"\nstrategic peeking agent, {runs} null paths, seed 20311, {grid} reads, {looks} looks: "
          f"false win {agent_wins}/{runs} = {agent_wins / runs:.2%}; honest schedule {honest_wins}/{runs} = "
          f"{honest_wins / runs:.2%}; paths that differ {differ}")
    assert differ == 0
    assert agent_wins / runs <= 0.025 + 4 * math.sqrt(0.025 * 0.975 / runs)


def test_read_experiment_hides_the_number_between_looks(world, keys):
    svc, fs, _ = world
    started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 50, 51, 0.9731, guardrails={"refunds": 0.4213})
    out = svc.read_experiment("env-key", 1)
    res = out["results"]
    assert res["identities"] == {"control": 50, "big_cta": 51}
    assert res["redacted"] is True and res["next_look_at_identities"] == 100
    text = json.dumps(out)
    assert "0.9731" not in text and "0.4213" not in text
    assert all(m.get("chance_to_win") is None and m.get("lift") is None for m in res["metrics"].values())


def test_watch_reply_and_its_card_hide_the_number(world, keys):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 50, 50, 0.9731, guardrails={"refunds": 0.4213})
    out = svc.look(h)
    d = out["decision"]
    assert d["outcome"] == "watch" and d["z"] is None and d["chance_to_win"] is None
    assert all(g["chance_worse"] is None and g["z"] is None for g in d["guardrails"])
    for blob in (out, svc.look_view(out["look_id"]), svc.experiment_view("env-key", 1)):
        text = json.dumps(blob)
        assert "0.9731" not in text and "97.31" not in text and "0.4213" not in text, blob


def test_a_read_that_reaches_the_planned_count_is_the_look(world, keys, clock):
    """The first read at or past the next planned count is the analysis, whichever path made it."""
    svc, fs, store = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 104, 101, 0.6)
    out = svc.read_experiment("env-key", 1)
    counted = [lk for lk in store.looks("env-key", 1) if lk["counted"]]
    assert len(counted) == 1 and counted[0]["decision"]["look_number"] == 1
    assert out["look"]["decision"]["outcome"] == "hold" and out["look"]["look_id"] == counted[0]["id"]
    # The number of a counted look is spent, so it is shown.
    assert out["results"]["metrics"]["purchase"]["chance_to_win"] == pytest.approx(0.6)
    # A later look at the same count is between looks: the planned look was already taken.
    clock.advance(300)
    assert svc.look(h)["decision"]["outcome"] == "watch"
    assert len([lk for lk in store.looks("env-key", 1) if lk["counted"]]) == 1


def test_numbers_are_shown_again_once_the_contract_has_decided(world, keys):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 1000, 1000, PHI(-3.3))
    assert svc.look(h)["decision"]["outcome"] == "loss"
    fs.set_results(1, 1100, 1100, 0.9731)
    out = svc.read_experiment("env-key", 1)
    assert out["results"]["metrics"]["purchase"]["chance_to_win"] == pytest.approx(0.9731)


def test_counted_looks_carry_an_alpha_ledger(world, keys, clock):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=5)
    bs, ts = [], []
    for k, n in enumerate((200, 400, 600), start=1):
        fs.set_results(1, n, n, 0.5)
        out = svc.look(h)
        led = out["decision"]["ledger"]
        bs.append(led["boundary_applied"])
        ts.append(n / 1000)
        u, lo = bd.crossing_probabilities(ts, bs)
        assert led["alpha_spent"] == pytest.approx(sum(u) + sum(lo), abs=1e-9)
        assert led["alpha_signed"] == 0.05 and led["look_number"] == k
        clock.advance(300)
    card = svc.look_view(out["look_id"])
    assert card["alpha"]["spent"] == pytest.approx(led["alpha_spent"])
    assert card["alpha"]["signed"] == 0.05
    assert "of the signed 0.05" in card["alpha"]["text"]


# ---- finding 2: an amendment must not freeze the contract ---------------------------------------------


def test_amendment_cannot_set_the_horizon_at_or_below_a_counted_look(world, keys, clock):
    svc, fs, _ = world
    h = started(svc, keys, n=1000, looks=10)
    for n in (300, 600):
        fs.set_results(1, n, n, 0.5)
        assert svc.look(h)["decision"]["counted"]
        clock.advance(300)
    for bad in (500, 600):
        with pytest.raises(Invalid, match="already counted a look at 600"):
            svc.amend(h, {"horizon": {"identities_per_arm": bad}}, reason="smaller")
    assert svc.amend(h, {"horizon": {"identities_per_arm": 601}}, reason="smaller")["hash"]


def test_engine_is_not_frozen_by_a_counted_look_past_an_amended_horizon(keys):
    """Robustness for contracts amended before the rule existed: looks at 300 and 600, horizon now 500."""
    c = make_contract(keys, n=500, looks=10)
    for n in (700, 2000):
        d = evaluate(c, [0.6, 1.2], Observation(n, n, 0.9999999, {"refunds": 0.5}))
        assert d.counted and d.fires, (n, d.outcome)


# ---- finding 3: a final read past the horizon -----------------------------------------------------------


@pytest.mark.parametrize("over", [1.5, 10.0])
def test_final_read_past_the_horizon_spends_exactly_the_alpha_left(keys, over):
    c = make_contract(keys, n=1000, looks=5)
    prior, bs = [], []
    for n in (200, 400, 600, 800):
        d = evaluate(c, prior, Observation(n, n, 0.5, {"refunds": 0.5}))
        bs.append(applied_boundary(d.to_dict()))
        prior.append(d.fraction)
    n_final = int(1000 * over)
    d = evaluate(c, prior, Observation(n_final, n_final, 0.5, {"refunds": 0.5}))
    assert d.outcome == "horizon"
    landed = [n / n_final for n in (200, 400, 600, 800)] + [1.0]
    u, lo = bd.crossing_probabilities(landed, [*bs, applied_boundary(d.to_dict())])
    print(f"\noverrun {over}x: false win {sum(u):.5f}, false loss {sum(lo):.5f}")
    assert sum(u) == pytest.approx(0.025, abs=2e-5)
    assert sum(lo) == pytest.approx(0.025, abs=2e-5)


# ---- finding 4: an amendment spends only the alpha that is left ------------------------------------------


def test_amended_horizon_spends_only_the_alpha_left(world, keys, clock):
    svc, fs, store = world
    h = started(svc, keys, n=1000, looks=5)
    for n in (200, 400, 600):
        fs.set_results(1, n, n, 0.5)
        assert svc.look(h)["decision"]["counted"]
        clock.advance(300)
    h2 = amend_and_sign(svc, keys, h, {"horizon": {"identities_per_arm": 2000}})
    for n in (800, 1200, 1600, 2000):
        fs.set_results(1, n, n, 0.5)
        assert svc.look(h2)["decision"]["counted"], n
        clock.advance(300)
    counted = [lk for lk in store.looks("env-key", 1) if lk["counted"]]
    assert [lk["identities_per_arm"] for lk in counted] == [200, 400, 600, 800, 1200, 1600, 2000]
    fr = [lk["identities_per_arm"] / 2000 for lk in counted]
    u, lo = bd.crossing_probabilities(fr, [applied_boundary(lk["decision"]) for lk in counted])
    print(f"\nhorizon doubled after 3 of 5 looks: false win {sum(u):.5f}, false loss {sum(lo):.5f}")
    assert sum(u) == pytest.approx(0.025, abs=2e-5)
    assert sum(lo) == pytest.approx(0.025, abs=2e-5)


# ---- finding 5: the store ------------------------------------------------------------------------------


def test_store_uses_wal_a_busy_timeout_and_unique_look_numbers(tmp_path):
    st = Store(tmp_path / "u.db")
    assert st._one("PRAGMA journal_mode")["journal_mode"] == "wal"
    assert st._one("PRAGMA busy_timeout")["timeout"] >= 1000
    st.add_look(contract_hash="h", environment="e", experiment_id=1, counted=True, identities_per_arm=1,
                outcome="hold", decision={}, snapshot=None, refreshed=True, now=1.0)
    with pytest.raises(sqlite3.IntegrityError):
        st._exec("INSERT INTO looks(id, contract_hash, environment, experiment_id, seq, counted, identities_per_arm, "
                 "outcome, decision, snapshot, refreshed, at) VALUES ('x','h','e',1,1,0,1,'watch','{}',NULL,0,1)")


def test_store_opens_an_older_database_with_duplicate_look_numbers(tmp_path):
    p = tmp_path / "old.db"
    db = sqlite3.connect(p)
    db.executescript(
        "CREATE TABLE looks (id TEXT PRIMARY KEY, contract_hash TEXT NOT NULL, environment TEXT NOT NULL, "
        "experiment_id INTEGER NOT NULL, seq INTEGER NOT NULL, counted INTEGER NOT NULL, identities_per_arm "
        "INTEGER NOT NULL, outcome TEXT NOT NULL, decision TEXT NOT NULL, snapshot TEXT, refreshed INTEGER NOT NULL, "
        "at REAL NOT NULL);"
        "INSERT INTO looks VALUES ('a','h','e',1,1,1,1,'hold','{}',NULL,1,1);"
        "INSERT INTO looks VALUES ('b','h','e',1,1,1,1,'hold','{}',NULL,1,2);")
    db.commit()
    db.close()
    st = Store(p)
    assert len(st.looks("e", 1)) == 2
    st.add_look(contract_hash="h", environment="e", experiment_id=1, counted=False, identities_per_arm=1,
                outcome="watch", decision={}, snapshot=None, refreshed=True, now=3.0)
    assert [lk["seq"] for lk in st.looks("e", 1)] == [1, 1, 2]


def test_two_service_processes_count_a_planned_look_once(tmp_path, keys, clock, monkeypatch):
    """Two services on one database (as two processes would be) both refresh at the planned count."""
    fs = FakeFlagsmith(clock=clock)
    fs.add_experiment(1)
    path = tmp_path / "shared.db"
    settings = Settings(public_url="http://test", refresh_wait_seconds=0, poll_seconds=0)
    one = Ulysses(Store(path), fs, settings, clock=clock, sleep=lambda s: None)
    two = Ulysses(Store(path), fs, settings, clock=clock, sleep=lambda s: None)
    for svc in (one, two):
        svc.register_signer("maya", keys["maya"].public)
        svc.register_signer("sam", keys["sam"].public)
    h = started(one, keys, n=1000, looks=10)
    fs.set_results(1, 100, 100, 0.6)

    real = core_mod.evaluate
    other: dict = {}

    def racing(*a, **kw):
        # The first evaluation (service one) lets service two run a whole look before it records its own.
        if not other:
            t = threading.Thread(target=lambda: other.setdefault("out", two.look(h)))
            other["thread"] = t
            t.start()
            t.join(timeout=1.0)
        return real(*a, **kw)

    monkeypatch.setattr(core_mod, "evaluate", racing)
    first = one.look(h)
    other["thread"].join(timeout=30)
    monkeypatch.setattr(core_mod, "evaluate", real)
    counted = [lk for lk in Store(path).looks("env-key", 1) if lk["counted"]]
    outcomes = sorted([first["decision"]["outcome"], other["out"]["decision"]["outcome"]])
    assert len(counted) == 1, [(lk["seq"], lk["decision"]["look_number"]) for lk in counted]
    assert outcomes == ["hold", "watch"]


def test_the_service_poller_takes_the_look_on_schedule(world, keys, clock):
    """``ulysses serve --poll``: the service looks by itself; the refresh floor still holds."""
    svc, fs, store = world
    h = started(svc, keys, n=1000, looks=10)
    fs.set_results(1, 100, 100, 0.6)
    assert [r["decision"]["outcome"] for r in svc.poll_once()] == ["hold"]
    assert svc.poll_once()[0].get("too_soon")
    clock.advance(300)
    fs.set_results(1, 150, 150, 0.6)
    assert svc.poll_once()[0]["decision"]["outcome"] == "watch"
    assert [lk["counted"] for lk in store.looks("env-key", 1)] == [1, 0]
    assert svc.contract_status(h)["state"] == "active"
