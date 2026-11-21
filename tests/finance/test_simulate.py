"""Projection engine: accounting identities, pensions, solver, Monte Carlo."""
import copy
import math
from datetime import date

import pytest

from app.finance import simulate as S
from app.finance.db import DEFAULT_SETTINGS

TODAY = date(2026, 1, 1)   # full first year: f = 1


def settings(**assumptions):
    s = copy.deepcopy(DEFAULT_SETTINGS)
    s["assumptions"].update(assumptions)
    return s


def flat_world(**extra):
    """No returns, no inflation, no growth: money only moves through flows."""
    return settings(inflation_pct=0.0, equity_real_return_pct=0.0, equity_vol_pct=0.0,
                    bond_real_return_pct=0.0, bond_vol_pct=0.0, cash_real_return_pct=0.0,
                    salary_growth_real_pct=0.0, lpp_interest_nominal_pct=0.0,
                    property_growth_real_pct=0.0, dividend_yield_pct=0.0, bond_yield_pct=0.0,
                    **extra)


def adult(id_=1, born=1990, salary=100_000.0, liquid=100_000.0, **kw):
    return S.Adult(id=id_, name=f"A{id_}", birth_year=born, salary=salary, liquid=liquid,
                   avs_past_years=14, avs_past_avg_income=salary, equity_share=0.8, **kw)


def hh(adults, **kw):
    kw.setdefault("settings", settings())
    kw.setdefault("today", TODAY)
    return S.Household(adults=adults, **kw)


def test_first_period_fraction():
    e = S.Engine(hh([adult()], today=date(2026, 7, 2)))
    assert e.f0 == pytest.approx((date(2027, 1, 1) - date(2026, 7, 2)).days / 365)


def test_lognormal_params_preserve_mean():
    m, s = S.lognormal_params(0.05, 0.15)
    assert math.exp(m + s * s / 2) - 1 == pytest.approx(0.05)
    assert S.median_return(0.0, 0.0, settings()["assumptions"]) == pytest.approx(-0.005)


def test_cash_flow_identity_in_a_flat_world():
    """With zero returns, the liquid balance changes by exactly the year's net flow."""
    h = hh([adult(salary=120_000, liquid=50_000)], settings=flat_world(), spending=50_000)
    eng = S.Engine(h)
    run = eng.run(eng.default_plan())
    prev = 50_000
    for y in run["years"][:40]:
        assert y["liquid"] == pytest.approx(prev + y["net_flow"], abs=1e-6)
        prev = y["liquid"]


def test_net_flow_while_working_matches_hand_computation():
    h = hh([adult(salary=100_000, liquid=0, lpp_employee=5_000, lpp_credits=10_000)],
           settings=flat_world(), spending=40_000)
    eng = S.Engine(h)
    y = eng.run(eng.default_plan())["years"][0]
    social = 100_000 * (0.053 + 0.011 + 0.01)
    expected = 100_000 - social - 5_000 - y["taxes"] - y["p3a"] - 40_000
    assert y["net_flow"] == pytest.approx(expected, abs=0.01)
    assert y["p3a"] == 7_258


def test_more_spending_means_later_freedom():
    base = hh([adult(salary=110_000, liquid=150_000)], spending=45_000)
    fi1 = S.solve_fi_year(S.Engine(base))["fi_year"]
    more = copy.deepcopy(base)
    more.spending = 65_000
    fi2 = S.solve_fi_year(S.Engine(more))["fi_year"]
    assert fi1 is not None and fi2 is not None
    assert fi2 > fi1


def test_rich_household_is_free_now():
    h = hh([adult(salary=80_000, liquid=8_000_000)], spending=60_000)
    eng = S.Engine(h)
    assert S.solve_fi_year(eng)["fi_year"] == 2026
    assert S.required_liquid_now(eng, S.plan_for_stop_year(eng, 2026)) < 8_000_000


def test_impossible_plan_has_no_fi_year():
    h = hh([adult(salary=30_000, liquid=0)], spending=80_000)
    assert S.solve_fi_year(S.Engine(h))["fi_year"] is None


def test_required_liquid_is_tight():
    h = hh([adult(salary=100_000, liquid=200_000, born=1975)], spending=50_000)
    eng = S.Engine(h)
    plan = S.plan_for_stop_year(eng, 2026)
    need = S.required_liquid_now(eng, plan, tol=100)
    extra = need - 200_000
    assert eng.run(S.Plan(**{**plan.__dict__, "extra_liquid": extra + 200}))["success"]
    assert not eng.run(S.Plan(**{**plan.__dict__, "extra_liquid": extra - 400}))["success"]


def test_avs_pension_full_career_max():
    # 44 years at a high income → maximum pension, 13 payments
    a = adult(born=1962, salary=150_000, liquid=2_000_000)
    a.avs_past_years = 44 - 1      # contributions from 21 to 63, this year (64) adds the last one
    h = hh([a], spending=60_000)
    eng = S.Engine(h)
    run = eng.run(eng.default_plan())
    y65 = next(y for y in run["years"] if y["ages"][0] == 65)
    assert y65["pension"] == pytest.approx(2520 * 13)


def test_avs_partial_career():
    a = adult(born=1962, salary=150_000, liquid=2_000_000)
    a.avs_past_years = 21          # + this year = 22 years → half pension
    h = hh([a], spending=60_000)
    eng = S.Engine(h)
    y65 = next(y for y in eng.run(eng.default_plan())["years"] if y["ages"][0] == 65)
    assert y65["pension"] == pytest.approx(1260 * 13)


def test_early_retiree_pays_avs_as_non_active():
    a = adult(born=1980, salary=100_000, liquid=1_500_000)
    eng = S.Engine(hh([a], spending=50_000))
    run = eng.run(S.plan_for_stop_year(eng, 2027))
    y = next(y for y in run["years"] if y["year"] == 2030)
    assert y["avs_nonactive"] >= 530
    y65 = next(y for y in run["years"] if y["ages"][0] == 66)
    assert y65["avs_nonactive"] == 0


def test_pillar3a_staggered_withdrawals():
    a = adult(born=1966, salary=100_000, liquid=500_000, p3a_balance=150_000)
    s = flat_world(pillar3a_accounts=3, pillar3a_withdraw_from_age=60)
    eng = S.Engine(hh([a], settings=s, spending=50_000))
    run = eng.run(S.plan_for_stop_year(eng, 2026))
    caps = {y["ages"][0]: y["capital"] for y in run["years"] if y["capital"] > 0}
    assert sorted(caps) == [60, 61, 62]
    assert sum(caps.values()) == pytest.approx(150_000)
    assert caps[60] == pytest.approx(50_000)


def test_lpp_capital_taken_at_stop_after_58_else_vested_until_60():
    a = adult(born=1970, salary=100_000, liquid=2_000_000, lpp_balance=300_000)
    s = flat_world()
    eng = S.Engine(hh([a], settings=s, spending=50_000))
    late = eng.run(S.plan_for_stop_year(eng, 2029))   # stops at 59
    assert next(y for y in late["years"] if y["year"] == 2029)["capital"] > 300_000
    early = eng.run(S.plan_for_stop_year(eng, 2026))  # stops at 56 → vested → withdrawn at 60
    assert next(y for y in early["years"] if y["year"] == 2026)["capital"] == 0
    assert next(y for y in early["years"] if y["year"] == 2030)["capital"] == pytest.approx(300_000)


def test_lpp_rente_loses_real_value():
    a = adult(born=1962, salary=100_000, liquid=1_000_000, lpp_balance=400_000, lpp_conversion_pct=6.0)
    s = settings(lpp_withdrawal="rente", inflation_pct=1.0)
    eng = S.Engine(hh([a], settings=s, spending=50_000))
    run = eng.run(eng.default_plan())
    ys = [y for y in run["years"] if y["ages"][0] >= 66][:3]
    lpp_part = [y["pension"] - ys[0]["pension"] for y in ys]
    assert lpp_part[1] < 0 and lpp_part[2] < lpp_part[1]


def test_children_cost_and_allowance():
    kid = S.Kid(id=9, name="Bébé", birth=date(2027, 3, 1), birth_parent_id=2)
    h = hh([adult(1), adult(2, salary=80_000)], kids=[kid], settings=flat_world(), spending=60_000)
    eng = S.Engine(h)
    ys = {y["year"]: y for y in eng.run(eng.default_plan())["years"]}
    assert ys[2026]["allowances"] == 0
    assert ys[2027]["allowances"] == pytest.approx(322 * 12 + 1_617)
    assert ys[2028]["allowances"] == pytest.approx(322 * 12)
    # child cost + crèche while both work
    assert ys[2028]["spending"] - ys[2026]["spending"] == pytest.approx((900 + 1500) * 12)


def test_marriage_changes_taxes():
    a1, a2 = adult(1, salary=120_000), adult(2, salary=60_000)
    base = hh([a1, a2], spending=60_000)
    married = hh([copy.deepcopy(a1), copy.deepcopy(a2)], spending=60_000,
                 settings={**settings(), "civil_status": "married"})
    y_u = S.Engine(base).run(S.Engine(base).default_plan())["years"][0]
    y_m = S.Engine(married).run(S.Engine(married).default_plan())["years"][0]
    assert y_u["taxes"] != pytest.approx(y_m["taxes"])
    assert y_m["married"] and not y_u["married"]


def test_home_purchase_moves_money_into_property():
    a = adult(salary=150_000, liquid=400_000)
    ev = S.Event(kind="home_purchase", start=date(2027, 6, 1),
                 params={"price": 1_000_000, "down_payment": 200_000, "mortgage_rate_pct": 2.0})
    h = hh([a], settings=flat_world(), spending=60_000, rent=30_000, events=[ev])
    eng = S.Engine(h)
    ys = {y["year"]: y for y in eng.run(eng.default_plan())["years"]}
    amort = (1_000_000 * 0.8 - 1_000_000 * 0.667) / 15          # 2nd tranche down to 2/3 in 15 years
    assert ys[2027]["property_equity"] == pytest.approx(200_000 + amort)
    # spending: rent gone, mortgage interest (2 % of 800'000) + maintenance (1 %) instead
    assert ys[2027]["spending"] == pytest.approx(60_000 - 30_000 + 16_000 + 10_000)
    # the down payment, purchase costs (3 %) and the amortisation left the liquid money
    without = hh([adult(salary=150_000, liquid=400_000)], settings=flat_world(), spending=60_000, rent=30_000)
    y_ref = S.Engine(without).run(S.Engine(without).default_plan())["years"][1]
    diff = y_ref["net_flow"] - ys[2027]["net_flow"]
    assert diff == pytest.approx(200_000 + 30_000 + amort - 30_000 + 26_000 + (ys[2027]["taxes"] - y_ref["taxes"]), abs=1)


def test_coast_mode_adds_no_savings():
    h = hh([adult(salary=120_000, liquid=100_000)], settings=flat_world(), spending=50_000)
    eng = S.Engine(h)
    run = eng.run(S.Plan(stop_year={1: 2055}, coast=True))
    assert run["years"][5]["liquid"] == pytest.approx(100_000)


def test_monte_carlo_without_volatility_equals_deterministic():
    s = settings(equity_vol_pct=0.0, bond_vol_pct=0.0)
    h = hh([adult(salary=100_000, liquid=300_000, born=1985)], settings=s, spending=55_000)
    eng = S.Engine(h)
    plan = S.plan_for_stop_year(eng, 2040)
    mc = S.monte_carlo(eng, plan, runs=20)
    det = mc["deterministic"]
    for a_, b_ in zip(mc["liquid"]["p50"][:50], det[:50]):
        assert a_ == pytest.approx(b_, rel=1e-6, abs=1.0)


def test_monte_carlo_is_reproducible_and_sane():
    h = hh([adult(salary=100_000, liquid=300_000, born=1985)], spending=55_000)
    eng = S.Engine(h)
    plan = S.plan_for_stop_year(eng, 2045)
    m1 = S.monte_carlo(eng, plan, runs=300, seed=7)
    m2 = S.monte_carlo(eng, plan, runs=300, seed=7)
    assert m1["success_rate"] == m2["success_rate"]
    assert 0.0 <= m1["success_rate"] <= 1.0
    for t in range(len(m1["years"])):
        assert m1["liquid"]["p10"][t] <= m1["liquid"]["p50"][t] <= m1["liquid"]["p90"][t]
    later = S.monte_carlo(eng, S.plan_for_stop_year(eng, 2055), runs=300, seed=7)
    assert later["success_rate"] >= m1["success_rate"]
