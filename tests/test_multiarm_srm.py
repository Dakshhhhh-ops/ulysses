"""Several treatments against one control, the sample-ratio check, and the signer's curves."""

from statistics import NormalDist

import pytest

from ulysses import boundary as bd
from ulysses import contract as ct
from ulysses import power, srm
from ulysses.core import Invalid
from ulysses.decision import Observation, evaluate, observation_from_snapshot
from ulysses.flagsmith.gateway import MetricResult, ResultsSnapshot
from ulysses.horizon import plan

from .conftest import make_contract, read_all, sign_as, tap

PHI = NormalDist().cdf
ARMS = ["big_cta", "wide_cta", "green_cta"]


def three_arm(keys, *, n=1200, looks=6, experiment=1, **kw) -> ct.Contract:
    return ct.draft(
        experiment=ct.ExperimentRef(project=1, environment="env-key", experiment=experiment, flag="checkout_button",
                                    treatment=ARMS[0], more_treatments=ARMS[1:]),
        hypothesis="One of three buttons lifts purchases",
        primary_metric=ct.Metric(name="purchase", direction="increase"),
        guardrails=[ct.Guardrail(name="refunds", direction="not_increase")],
        horizon=ct.Horizon(identities_per_arm=n, looks=looks),
        owner=ct.Signer(name="maya", public_key=keys["maya"].public),
        second_signer=ct.Signer(name="sam", public_key=keys["sam"].public),
        **kw,
    )


def obs3(n, chances, guard=None, counts=None):
    ids = counts or {"control": n, **{a: n for a in ARMS}}
    return Observation(ids["control"], ids[ARMS[0]], chances[ARMS[0]], identities=ids, arm_chance=chances,
                       arm_guardrail_worse=guard or {a: {"refunds": 0.5} for a in ARMS})


# ---- contract ------------------------------------------------------------------------


def test_multi_arm_contract_splits_alpha_by_bonferroni(keys):
    c = three_arm(keys)
    assert c.boundary.multiplicity == "bonferroni"
    assert c.comparisons == 3 and c.comparison_alpha == pytest.approx(0.05 / 3)
    want = bd.spending_boundaries([k / 6 for k in range(1, 7)], 0.05 / 3)
    assert [p.z for p in c.boundary.planned] == pytest.approx([round(z, 4) for z in want], abs=1e-4)
    # The final per-comparison boundary is stricter than the two-arm one at the same alpha.
    two = make_contract(keys, n=1200, looks=6)
    assert c.boundary.planned[-1].z > two.boundary.planned[-1].z
    # Hand-dropping the correction does not validate.
    data = c.canonical()
    del data["boundary"]["multiplicity"]
    with pytest.raises(ValueError, match="multiplicity"):
        ct.from_canonical(data)
    # A table built at the unsplit alpha does not validate either.
    data = c.canonical()
    data["boundary"]["planned"] = [p.model_dump(mode="json", exclude_none=True)
                                   for p in ct.planned_table(c.horizon, 0.05)]
    with pytest.raises(ValueError, match="does not match"):
        ct.from_canonical(data)


def test_two_arm_contract_hash_has_no_new_fields_but_the_split(keys):
    c = make_contract(keys)
    data = c.canonical()
    assert "more_treatments" not in data["experiment"] and "multiplicity" not in data["boundary"]
    assert data["sample_ratio"]["split"] == {"control": 50.0, "big_cta": 50.0}
    assert data["sample_ratio"]["p_threshold"] == 0.001


def test_equal_split_sums_to_100():
    s = ct.equal_split(["control", "a", "b"])
    assert sum(s.values()) == pytest.approx(100.0) and len(s) == 3
    with pytest.raises(ValueError):
        ct.SampleRatio(split={"control": 60.0, "a": 30.0})


def test_horizon_for_several_treatments_is_larger():
    one = plan(min_relative_lift=0.25, baseline_rate=0.10, looks=10)
    three = plan(min_relative_lift=0.25, baseline_rate=0.10, looks=10, comparisons=3)
    assert three.identities_per_arm > one.identities_per_arm
    assert three.comparison_alpha == pytest.approx(0.05 / 3)


# ---- decision ------------------------------------------------------------------------


def test_multi_arm_hold_win_and_winner(keys):
    c = three_arm(keys)
    b = bd.spending_boundaries([1 / 6, 2 / 6, 3 / 6], 0.05 / 3, final_spends_all=False)[-1]
    two_arm_b = bd.spending_boundaries([1 / 6, 2 / 6, 3 / 6], 0.05, final_spends_all=False)[-1]
    assert b > two_arm_b
    # Over the two-arm boundary but under the Bonferroni one: hold.
    mid = PHI((b + two_arm_b) / 2)
    d = evaluate(c, [1 / 6, 2 / 6], obs3(600, {ARMS[0]: 0.6, ARMS[1]: mid, ARMS[2]: 0.4}))
    assert d.outcome == "hold" and d.winner is None
    assert [r["state"] for r in d.arms] == ["within", "within", "within"]
    # Two arms cross: the larger z wins and is the one named.
    d = evaluate(c, [1 / 6, 2 / 6], obs3(600, {ARMS[0]: PHI(b + 0.1), ARMS[1]: PHI(b + 0.5), ARMS[2]: 0.4}))
    assert d.outcome == "win" and d.winner == ARMS[1] and d.action == "ship_treatment"
    assert d.z == pytest.approx(b + 0.5, abs=1e-3)
    assert "wide_cta" in d.reason and "Bonferroni" in d.reason


def test_multi_arm_loss_needs_every_arm_below(keys):
    c = three_arm(keys)
    b = bd.spending_boundaries([1 / 6, 2 / 6, 3 / 6], 0.05 / 3, final_spends_all=False)[-1]
    low = PHI(-b - 0.2)
    d = evaluate(c, [1 / 6, 2 / 6], obs3(600, {ARMS[0]: low, ARMS[1]: low, ARMS[2]: 0.5}))
    assert d.outcome == "hold"
    d = evaluate(c, [1 / 6, 2 / 6], obs3(600, {a: low for a in ARMS}))
    assert d.outcome == "loss" and d.action == "keep_control"


def test_multi_arm_waits_for_every_arm_and_uses_the_smallest(keys):
    c = three_arm(keys)
    counts = {"control": 400, ARMS[0]: 400, ARMS[1]: 190, ARMS[2]: 400}
    d = evaluate(c, [], obs3(0, {a: 0.5 for a in ARMS}, counts=counts))
    assert d.identities_per_arm == 190 and d.outcome == "watch"
    d = evaluate(c, [], obs3(200, {ARMS[0]: 0.9, ARMS[1]: None, ARMS[2]: 0.5}))
    assert d.outcome == "not_ready" and "green_cta" not in d.reason and "wide_cta" in d.reason


def test_multi_arm_guardrail_per_arm_at_split_alpha(keys):
    c = three_arm(keys)
    t = [1 / 6, 2 / 6, 3 / 6]
    split = bd.one_sided_boundaries(t, ct.GUARDRAIL_ALPHA / 3, final_spends_all=False)[-1]
    whole = bd.one_sided_boundaries(t, ct.GUARDRAIL_ALPHA, final_spends_all=False)[-1]
    between = PHI((split + whole) / 2)
    guard = {a: {"refunds": 0.5} for a in ARMS}
    guard[ARMS[2]] = {"refunds": between}
    d = evaluate(c, t[:2], obs3(600, {a: 0.6 for a in ARMS}, guard=guard))
    assert d.outcome == "hold"
    guard[ARMS[2]] = {"refunds": PHI(split + 0.05)}
    d = evaluate(c, t[:2], obs3(600, {a: 0.6 for a in ARMS}, guard=guard))
    assert d.outcome == "guardrail_breach" and "green_cta" in d.reason and d.action == "keep_control"


def test_snapshot_maps_each_treatment(keys):
    c = three_arm(keys)
    snap = ResultsSnapshot(
        identities={"control": 300, ARMS[0]: 301, ARMS[1]: 299, ARMS[2]: 302},
        metrics={
            "purchase": MetricResult("purchase", "up", 0.11),  # first treatment's copy: must be ignored here
            **{f"purchase@{a}": MetricResult("purchase", "up", ch) for a, ch in zip(ARMS, (0.7, 0.8, 0.2))},
            **{f"refunds@{a}": MetricResult("refunds", "down", 0.4) for a in ARMS},
        },
    )
    o = observation_from_snapshot(c, snap)
    assert o.arm_chance == {ARMS[0]: 0.7, ARMS[1]: 0.8, ARMS[2]: 0.2}
    assert o.arm_guardrail_worse[ARMS[1]]["refunds"] == pytest.approx(0.6)
    assert o.identities[ARMS[2]] == 302


# ---- sample ratio --------------------------------------------------------------------


def test_chi_square_matches_textbook_values():
    # 1 degree of freedom: statistic 3.841459 is the 95th percentile.
    stat, p = srm.chi_square([1000 + 30.99, 1000 - 30.99], [0.5, 0.5])
    assert stat == pytest.approx(30.99 ** 2 * 2 / 1000, rel=1e-9)
    s, p2 = srm.chi_square([600, 400], [0.6, 0.4])
    assert s == 0 and p2 == 1.0
    _, p3 = srm.chi_square([1000, 1000, 1000, 880], [0.25] * 4)
    assert 0 < p3 < 0.05


def test_chi_square_matches_flagsmith_srm_p_value_when_source_is_available():
    import os

    src = os.environ.get("FLAGSMITH_SRC")
    if not src:
        pytest.skip("set FLAGSMITH_SRC to a Flagsmith checkout to compare with its srm_p_value")
    from ulysses.calibrate import flagsmith_source

    stats, _, _ = flagsmith_source.load(src)
    for obs, shares in (([512, 488], [0.5, 0.5]), ([1000, 900], [0.5, 0.5]), ([300, 280, 330, 250], [0.25] * 4),
                        ([700, 320], [0.7, 0.3])):
        assert srm.chi_square(obs, shares)[1] == pytest.approx(stats.srm_p_value(obs, shares), rel=1e-9, abs=1e-15)


def test_planted_broken_split_holds_a_win(keys):
    c = make_contract(keys, n=1000, looks=10)
    # Control got 1000 identities, treatment 800, against a signed 50/50: a broken split.
    d = evaluate(c, [k / 10 for k in range(1, 8)], Observation(1000, 800, PHI(4.5), {"refunds": 0.4}))
    assert d.outcome == "hold" and d.action is None
    assert d.sample_ratio["state"] == "mismatch" and d.sample_ratio["p_value"] < 0.001
    assert "traffic split is broken" in d.reason and "chi-square" in d.reason
    # The same win with a healthy split ships.
    ok = evaluate(c, [k / 10 for k in range(1, 8)], Observation(800, 812, PHI(4.5), {"refunds": 0.4}))
    assert ok.outcome == "win" and ok.sample_ratio["state"] == "ok"


def test_broken_split_at_the_horizon_is_no_decision(keys):
    c = make_contract(keys, n=1000, looks=10)
    d = evaluate(c, [k / 10 for k in range(1, 10)], Observation(1300, 1000, PHI(4.5), {"refunds": 0.4}))
    assert d.outcome == "horizon" and d.action == "keep_control" and "broken" in d.reason


def test_broken_split_never_blocks_a_loss_or_a_guardrail(keys):
    c = make_contract(keys, n=1000, looks=10)
    d = evaluate(c, [k / 10 for k in range(1, 8)], Observation(1000, 800, PHI(-4.5), {"refunds": 0.4}))
    assert d.outcome == "loss"
    # A guardrail breach with a broken split still stops for the guardrail (keep control), not a hold.
    g = evaluate(c, [k / 10 for k in range(1, 8)], Observation(1000, 800, PHI(0), {"refunds": PHI(4.0)}))
    assert g.outcome == "guardrail_breach" and g.action == "keep_control"
    assert g.sample_ratio["state"] == "mismatch"


def test_srm_threshold_is_signed_and_used(keys):
    loose = make_contract(keys, n=1000, looks=10,
                          sample_ratio=ct.SampleRatio(split={"control": 50.0, "big_cta": 50.0}, p_threshold=0.05))
    # 900 against 800 on a signed 50/50: p about 0.0153, a hold under the signed 0.05, a win under the default 0.001.
    obs = Observation(900, 800, PHI(4.5), {"refunds": 0.4})
    assert srm.chi_square([900, 800], [0.5, 0.5])[1] == pytest.approx(0.01529, abs=1e-5)
    d = evaluate(loose, [k / 10 for k in range(1, 8)], obs)
    assert d.outcome == "hold" and d.action is None
    assert d.sample_ratio["threshold"] == 0.05 and d.sample_ratio["state"] == "mismatch"
    strict = evaluate(make_contract(keys, n=1000, looks=10), [k / 10 for k in range(1, 8)], obs)
    assert strict.outcome == "win" and strict.sample_ratio["state"] == "ok"


def test_no_signed_split_means_no_check(keys):
    c = make_contract(keys, n=1000, looks=10, sample_ratio=None)
    d = evaluate(c, [k / 10 for k in range(1, 8)], Observation(1000, 800, PHI(4.5), {"refunds": 0.4}))
    assert d.outcome == "win" and d.sample_ratio is None


def test_too_few_identities_are_not_tested(keys):
    c = make_contract(keys, n=200, looks=4)
    d = evaluate(c, [], Observation(45, 40, 0.6))
    assert d.sample_ratio["state"] == "too_few"


# ---- the service ---------------------------------------------------------------------


def test_signed_split_must_match_flagsmith_rollout(world, keys):
    svc, fs, _ = world
    bad = make_contract(keys, sample_ratio=ct.SampleRatio(split={"control": 60.0, "big_cta": 40.0}))
    with pytest.raises(Invalid, match="Flagsmith's rollout gives it 50.0%"):
        svc.submit(bad)
    with pytest.raises(ValueError, match="exactly the arms"):
        svc.submit(make_contract(keys, sample_ratio=ct.SampleRatio(split={"control": 50.0, "other": 50.0})))


def test_contract_model_rejects_a_split_naming_other_arms(keys):
    data = three_arm(keys).canonical()
    data["sample_ratio"]["split"] = {"control": 50.0, "zzz": 50.0}
    with pytest.raises(ValueError, match="exactly the arms"):
        ct.from_canonical(data)
    data = three_arm(keys).canonical()
    data["sample_ratio"]["split"] = {"control": 50.0, ARMS[0]: 50.0}  # drops two treatments
    with pytest.raises(ValueError, match="exactly the arms"):
        ct.from_canonical(data)


def test_regenerating_the_table_sets_the_multiplicity(keys):
    data = three_arm(keys).canonical()
    del data["boundary"]["multiplicity"]
    fixed = ct.with_regenerated_table(data)
    assert fixed.boundary.multiplicity == "bonferroni"
    assert [p.z for p in fixed.boundary.planned] == [p.z for p in three_arm(keys).boundary.planned]
    # A two-arm draft that wrongly carries the key gets it dropped.
    two = make_contract(keys).canonical()
    two["boundary"]["multiplicity"] = "bonferroni"
    assert ct.with_regenerated_table(two).boundary.multiplicity is None


def test_two_arm_contract_cannot_cover_a_three_arm_flag(world, keys):
    svc, fs, _ = world
    fs.add_experiment(2, more_treatments=("wide_cta", "green_cta"))
    with pytest.raises(Invalid, match="must name every treatment"):
        svc.submit(make_contract(keys, experiment=2))


def test_multi_arm_flow_ships_the_winner(world, keys, clock):
    svc, fs, store = world
    fs.add_experiment(2, treatment=ARMS[0], more_treatments=tuple(ARMS[1:]))
    out = svc.draft_for_experiment(environment="env-key", experiment_id=2, owner="maya", second_signer="sam",
                                   min_relative_lift=0.5, baseline_rate=0.2, looks=4)
    h = out["hash"]
    c = svc.contract(h)
    assert c.experiment.treatments == ARMS and c.boundary.multiplicity == "bonferroni"
    assert c.sample_ratio.split == pytest.approx({"control": 25.0, **{a: 25.0 for a in ARMS}})
    assert out["horizon_plan"]["comparisons"] == 3
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    n = c.horizon.identities_per_arm
    half = n // 2 + 1
    clock.advance(301)
    fs.set_arm_results(2, {"control": half, **{a: half for a in ARMS}},
                       {ARMS[0]: 0.6, ARMS[1]: PHI(8.0), ARMS[2]: 0.3})
    look = svc.look(h)
    assert look["decision"]["outcome"] == "win" and look["decision"]["winner"] == ARMS[1]
    p = look["proposal"]
    done = svc.confirm(p["id"], "maya", tap(svc, keys, p))
    assert done["result"]["serving"] == ARMS[1]
    assert fs.served["checkout_button"] == ARMS[1]
    assert fs.experiments[2].raw["rollout_variant"] == fs.experiments[2].variation(ARMS[1]).option_id


def test_multi_arm_amendment_cannot_ship_without_a_winner(world, keys, clock):
    svc, fs, _ = world
    fs.add_experiment(2, treatment=ARMS[0], more_treatments=tuple(ARMS[1:]))
    h = svc.submit(three_arm(keys, experiment=2))["hash"]
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    with pytest.raises(Invalid, match="cannot say which one to ship"):
        svc.amend(h, {}, "ship early", action_now="ship_treatment")
    out = svc.amend(h, {}, "stop now", action_now="keep_control")
    assert out["hash"] != h


# ---- curves --------------------------------------------------------------------------


def test_curves_two_arm_are_analytic_and_hit_the_design_power(keys):
    hp = plan(min_relative_lift=0.25, baseline_rate=0.10, looks=10)
    c = make_contract(keys, n=hp.identities_per_arm, looks=10,
                      design=ct.Design(baseline_rate=0.10, min_relative_lift=0.25, power=0.8))
    cur = power.curves_for_contract(c)
    assert cur["method"] == "analytic" and cur["x_kind"] == "relative_lift"
    assert cur["design"]["win"] == pytest.approx(0.8, abs=0.005)
    zero = next(p for p in cur["points"] if p["x"] == 0)
    assert zero["win"] == pytest.approx(0.025, abs=1e-5) and zero["loss"] == pytest.approx(0.025, abs=1e-5)
    wins = [p["win"] for p in cur["points"]]
    assert all(a <= b + 1e-12 for a, b in zip(wins, wins[1:]))  # power rises with the lift
    assert all(p["expected_identities_per_arm"] <= hp.identities_per_arm + 1e-6 for p in cur["points"])
    assert cur["design"]["expected_identities_per_arm"] < cur["fixed_sample_identities_per_arm"]


def test_curves_without_baseline_use_drift(keys):
    cur = power.curves_for_contract(make_contract(keys, n=1000, looks=5))
    assert cur["x_kind"] == "drift" and cur["design"] is None


def test_curves_multi_arm_are_simulated_and_keep_false_ships_under_half_alpha(keys):
    c = three_arm(keys, n=4000, looks=6, design=ct.Design(baseline_rate=0.10, min_relative_lift=0.25, power=0.8))
    cur = power.curves_for_contract(c)
    assert cur["method"] == "simulation" and cur["simulation"]["seed"] == power.SIM_SEED
    zero = next(p for p in cur["points"] if p["x"] == 0)
    # Global null: shipping any treatment stays under alpha/2 (Bonferroni), within simulation noise.
    assert zero["win"] + zero["win_other"] < 0.025 + 3 * (0.025 * 0.975 / power.SIM_RUNS) ** 0.5


def test_sign_page_draws_the_curves(world, keys):
    from fastapi.testclient import TestClient

    from ulysses.service.app import create_app

    svc, fs, _ = world
    hp = plan(min_relative_lift=0.25, baseline_rate=0.10, looks=10)
    h = svc.submit(make_contract(keys, n=hp.identities_per_arm, looks=10,
                                 design=ct.Design(baseline_rate=0.10, min_relative_lift=0.25, power=0.8)))["hash"]
    client = read_all(TestClient(create_app(svc, agent_token="test-agent-token-0123456789")), svc)
    html = client.get(f"/sign/{h}").text
    assert 'id="curves"' in html and "Chance of each outcome" in html and "oc-line" in html
    assert "Traffic split" in html and "0.001" in html
    data = client.get(f"/human/contract/{h}/curves").json()
    assert data["method"] == "analytic" and len(data["points"]) == power.POINTS


def test_three_arm_card_names_the_leading_arm_not_the_first_treatment(world, keys, clock):
    import re

    from fastapi.testclient import TestClient

    from ulysses.service.app import create_app

    svc, fs, _ = world
    fs.add_experiment(2, treatment=ARMS[0], more_treatments=tuple(ARMS[1:]))
    h = svc.draft_for_experiment(environment="env-key", experiment_id=2, owner="maya", second_signer="sam",
                                 min_relative_lift=0.5, baseline_rate=0.2, looks=4)["hash"]
    c = svc.contract(h)
    sign_as(svc, keys, h)
    svc.start_under_contract(h)
    half = c.horizon.identities_per_arm // 2 + 1
    clock.advance(301)
    # wide_cta leads, but big_cta is the first treatment (and the unsuffixed metric Flagsmith's parser keeps).
    fs.set_arm_results(2, {"control": half, **{a: half for a in ARMS}},
                       {ARMS[0]: 0.55, ARMS[1]: 0.97, ARMS[2]: 0.3},
                       guardrails={ARMS[2]: {"refunds": 0.9}},
                       lifts={ARMS[0]: 0.012, ARMS[1]: 0.09, ARMS[2]: -0.02})
    look = svc.look(h)
    assert look["decision"]["outcome"] == "hold" and look["decision"]["winner"] is None
    assert look["flagsmith"]["metrics"]["purchase@wide_cta"]["lift"] == 0.09
    client = read_all(TestClient(create_app(svc, agent_token="test-agent-token-0123456789")), svc)
    html = client.get(f"/card/{look['look_id']}").text
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    assert "control vs wide_cta" in txt and "control vs big_cta" not in txt
    assert re.search(r"lift \+9\.0+% wide_cta over control", txt), txt[txt.find("lift"):][:80]
    assert "wide_cta ahead" in txt and "big_cta ahead" not in txt
    for a in ARMS:
        assert f"refunds ({a}) within rule" in txt
        assert f"<code>refunds ({a})</code>" in html


def _curve_contract(keys, lift=0.25, direction="increase"):
    hp = plan(min_relative_lift=lift, baseline_rate=0.10, looks=10)
    c = make_contract(keys, n=hp.identities_per_arm, looks=10,
                      design=ct.Design(baseline_rate=0.10, min_relative_lift=lift, power=0.8))
    return c.model_copy(update={"primary_metric": ct.Metric(name="purchase", direction=direction)})


def test_curve_reference_labels_are_short_and_never_overlap(keys, monkeypatch):
    from ulysses.service import curves as oc

    c = _curve_contract(keys)
    cv = oc.build(c)
    fixed = cv["raw"]["fixed_sample_identities_per_arm"]
    horizon_ref, single = cv["b"]["refs"]
    assert single["text"] == f"Single read: {fixed:,}" and len(single["text"]) < 22
    assert f"{fixed:,}" in cv["b"]["ref_note"]
    far = abs(single["at"] - horizon_ref["at"]) >= oc.REF_GAP
    assert single["side"] == ("above" if far else "below")
    # Two reference lines closer than 8% of the y range: the lower label moves under its line.
    near = dict(cv["raw"], fixed_sample_identities_per_arm=int(c.horizon.identities_per_arm * 0.97))
    monkeypatch.setattr(oc.power, "curves_for_contract", lambda _c: near)
    close = oc.build(c)["b"]["refs"]
    assert abs(close[1]["at"] - close[0]["at"]) < oc.REF_GAP
    assert close[0]["side"] == "above" and close[1]["side"] == "below"


def test_curves_for_a_negative_signed_lift_say_so(keys):
    from ulysses.service import curves as oc

    up = oc.build(_curve_contract(keys))
    assert up["x_title"] == "True relative lift of the treatment" and up["signed_note"] is None
    down = oc.build(_curve_contract(keys, lift=-0.25, direction="decrease"))
    assert down["x_title"] == "True relative improvement of the treatment"
    assert "-25%" in down["signed_note"] and "+25%" in down["signed_note"]
