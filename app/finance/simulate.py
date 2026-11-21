"""Year-by-year projection of the household, FI date solver and Monte Carlo.

The engine works on plain dataclasses (`Household`), so it is testable without
a database — `model.py` builds them from the stored data.

Conventions
-----------
- Real terms: every amount is in today's francs. Returns, salary growth and
  LPP interest are real. AVS pensions keep their real value (indexed); LPP
  pensions are fixed in nominal francs, so they lose `inflation_pct` a year.
- The first period is the rest of the current year (fraction `f`): flows are
  multiplied by `f`, returns compounded over `f`. Later periods are full years.
- Flows land at the end of the period: balance × (1 + r) + net flow.
- The deterministic projection uses the **median** return of each mix
  (exp(m) - 1 of the lognormal), so it is the middle Monte Carlo path, not an
  optimistic average.
- "Free" (FI) = everyone stops working in the same calendar year and the
  liquid money never falls below the floor until the horizon, while pensions
  (AVS, LPP, 3a) arrive at their legal ages. The plan also fails if liquid
  money goes negative while still working (e.g. a home purchase too early).

Monte Carlo: the liquid portfolio follows random yearly returns (correlated
lognormal stocks and bonds, fixed seed); pension accounts follow their expected
path. Taxes and AVS non-active contributions depend on the liquid wealth, so
for each year they are interpolated on a grid computed with the real tax
engine — exact on the deterministic path.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import date

from . import params as P
from . import pension as X
from . import tax as T

# --------------------------------------------------------------------------- inputs


@dataclass
class Adult:
    id: int
    name: str
    birth_year: int
    salary: float = 0.0                 # gross per year today, at the current work %
    work_pct: float = 100.0
    side_income: float = 0.0            # net self-employed income per year
    lpp_employee: float | None = None   # per year today (None = legal minimum)
    lpp_credits: float | None = None    # savings credits per year (employee + employer; None = legal)
    lpp_balance: float = 0.0
    lpp_conversion_pct: float = 6.0
    lpp_buyin_potential: float = 0.0
    vested_balance: float = 0.0
    p3a_balance: float = 0.0
    p3a_annual: float | None = None     # None = legal maximum while working
    liquid: float = 0.0                 # own share of the liquid accounts
    equity_share: float = 0.0           # of the liquid money (0-1)
    bond_share: float = 0.0
    avs_past_years: int = 0             # contribution years before this year
    avs_past_avg_income: float = 0.0
    retire_age_target: int | None = None
    commute: float = 0.0
    meals_out: bool = True
    canteen: bool = False
    aanp_rate: float = 0.01
    ijm_rate: float = 0.0
    insurance: float = 5_400.0          # yearly premiums paid (deduction)


@dataclass
class Kid:
    id: int
    name: str
    birth: date
    birth_parent_id: int | None = None


@dataclass
class Property:
    value: float
    tax_value: float
    mortgage: float
    rate_pct: float = 1.8
    owners: dict[int, float] = field(default_factory=dict)  # adult id → share
    maintenance_pct: float = 1.0
    imputed_rent_pct: float = 0.0       # valeur locative / value (0 = abolished)
    purchase_year: int | None = None    # None = already owned
    amortize_to_pct: float = 66.7
    amortize_years: int = 15


@dataclass
class Event:
    kind: str
    member_id: int | None = None
    start: date | None = None
    end: date | None = None
    params: dict = field(default_factory=dict)


@dataclass
class Household:
    adults: list[Adult]
    kids: list[Kid] = field(default_factory=list)
    spending: float = 60_000.0          # yearly living costs today (no taxes, no child costs)
    rent: float = 0.0                   # part of `spending` that is rent (stops with a home purchase)
    properties: list[Property] = field(default_factory=list)
    other_debts: float = 0.0
    events: list[Event] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    today: date = field(default_factory=date.today)
    single_parent: bool = False         # only for a one-adult household that lives alone with the kids

    @property
    def a(self) -> dict:
        return self.settings.get("assumptions", {})


# --------------------------------------------------------------------------- returns


def lognormal_params(mu: float, sigma: float) -> tuple[float, float]:
    """(m, s) of ln(1+R) for a return with arithmetic mean mu and volatility sigma."""
    s2 = math.log(1 + (sigma ** 2) / ((1 + mu) ** 2))
    return math.log(1 + mu) - s2 / 2, math.sqrt(s2)


def mix_stats(equity: float, bonds: float, a: dict) -> tuple[float, float]:
    """Arithmetic mean and volatility of a yearly rebalanced mix (rest = cash)."""
    cash = max(0.0, 1 - equity - bonds)
    me, se = a["equity_real_return_pct"] / 100, a["equity_vol_pct"] / 100
    mb, sb = a["bond_real_return_pct"] / 100, a["bond_vol_pct"] / 100
    mc = a["cash_real_return_pct"] / 100
    rho = a.get("equity_bond_corr", 0.0)
    mu = equity * me + bonds * mb + cash * mc
    var = (equity * se) ** 2 + (bonds * sb) ** 2 + 2 * equity * bonds * rho * se * sb
    return mu, math.sqrt(max(var, 0.0))


def median_return(equity: float, bonds: float, a: dict) -> float:
    mu, sigma = mix_stats(equity, bonds, a)
    if sigma <= 0:
        return mu
    m, _ = lognormal_params(mu, sigma)
    return math.exp(m) - 1


# --------------------------------------------------------------------------- helpers


def _active(ev: Event, year: int) -> bool:
    s = ev.start.year if ev.start else -10**9
    e = ev.end.year if ev.end else 10**9
    return s <= year <= e


def _kid_age(k: Kid, year: int) -> int | None:
    """Age reached during `year` (None = not born yet)."""
    if k.birth.year > year:
        return None
    return year - k.birth.year


def _child_monthly_cost(age: int, cc: dict) -> float:
    if age <= 3:
        return cc["0-3"]
    if age <= 12:
        return cc["4-12"]
    if age <= 17:
        return cc["13-17"]
    return cc["18-24"]


@dataclass
class Plan:
    """Decisions of one run."""
    stop_year: dict[int, int]           # adult id → first calendar year without work
    spending_factor: float = 1.0        # lean / fat
    coast: bool = False                 # keep working but stop adding savings
    extra_liquid: float = 0.0           # added today (bisection for "how much is needed")


@dataclass
class YearCtx:
    """What the tax step of one year needs, to recompute it for another liquid amount."""
    year: int
    f: float
    married: bool
    abroad: dict | None
    earners: list[T.Earner]
    adult_ids: list[int]
    children: list[list[T.Child]]
    parental: list[bool]
    other_wealth: list[float]           # property net etc. per taxpayer-adult
    liquid_shares: list[float]
    equity: float
    bonds: float
    nonactive: list[bool]               # pays AVS as non-active this year
    pension_income: list[float]


# --------------------------------------------------------------------------- engine


class Engine:
    def __init__(self, hh: Household):
        self.hh = hh
        self.a = {**_DEFAULT_ASSUMPTIONS, **hh.a}
        self.y0 = hh.today.year
        self.f0 = (date(self.y0 + 1, 1, 1) - hh.today).days / (date(self.y0 + 1, 1, 1) - date(self.y0, 1, 1)).days
        oldest_young = min((ad.birth_year for ad in hh.adults), default=self.y0 - 30)
        youngest = max((ad.birth_year for ad in hh.adults), default=self.y0 - 30)
        self.y_end = youngest + int(self.a["horizon_age"])
        self.oldest_birth = oldest_young
        self.communal = float(hh.settings.get("communal_coefficient", 78.5))
        self.cc = {**_DEFAULT_CHILD_COSTS, **hh.settings.get("child_costs", {})}

    # ------------------------------------------------------------------ public

    def default_plan(self) -> Plan:
        return Plan(stop_year={ad.id: ad.birth_year + (ad.retire_age_target or 65) for ad in self.hh.adults})

    def run(self, plan: Plan, keep_ctx: bool = False) -> dict:
        return self._run(plan, keep_ctx)

    # ------------------------------------------------------------------ core

    def _married(self, year: int) -> bool:
        s = self.hh.settings
        if s.get("civil_status") == "married":
            return True
        my = s.get("marriage_year")
        if my and year >= int(my):
            return True
        return any(ev.kind == "marriage" and ev.start and ev.start.year <= year for ev in self.hh.events)

    def _abroad(self, year: int) -> dict | None:
        for ev in self.hh.events:
            if ev.kind == "move_abroad" and ev.start and ev.start.year <= year:
                return {"cost_factor": 0.8, "tax_rate_pct": 10.0, "capital_tax_pct": 5.0, **ev.params}
        return None

    def _work_pct(self, ad: Adult, year: int) -> float:
        pct = ad.work_pct
        for ev in self.hh.events:
            if ev.member_id != ad.id or not _active(ev, year):
                continue
            if ev.kind == "part_time":
                pct = float(ev.params.get("work_pct", pct))
            elif ev.kind == "career_break":
                pct = 0.0
        return pct

    def _salary_today(self, ad: Adult, year: int) -> float:
        base = ad.salary / ad.work_pct * 100 if ad.work_pct > 0 else ad.salary
        for ev in sorted((e for e in self.hh.events if e.kind == "salary_change" and e.member_id == ad.id
                          and e.start and e.start.year <= year), key=lambda e: e.start):
            if "gross_annual" in ev.params:
                base = float(ev.params["gross_annual"]) * 100 / max(1.0, float(ev.params.get("work_pct", 100)))
            elif "pct_change" in ev.params:
                base *= 1 + float(ev.params["pct_change"]) / 100
        return base  # at 100 %

    def _growth(self, ad: Adult, year: int) -> float:
        g = self.a["salary_growth_real_pct"] / 100
        until = int(self.a["salary_growth_until_age"])
        age_now = self.y0 - ad.birth_year
        age = year - ad.birth_year
        years = max(0, min(age, until) - min(age_now, until))
        return (1 + g) ** years

    def _run(self, plan: Plan, keep_ctx: bool) -> dict:
        hh, a = self.hh, self.a
        infl = a["inflation_pct"]
        adults = hh.adults
        ids = [ad.id for ad in adults]
        n = len(adults)
        liquid = [ad.liquid for ad in adults]
        if plan.extra_liquid:
            tot = sum(liquid)
            shares = [l / tot for l in liquid] if tot > 0 else [1 / n] * n
            liquid = [l + plan.extra_liquid * s for l, s in zip(liquid, shares)]
        lpp = [ad.lpp_balance for ad in adults]
        vested = [ad.vested_balance for ad in adults]
        p3a = [ad.p3a_balance for ad in adults]
        p3a_left = [max(1, int(a["pillar3a_accounts"])) for _ in adults]
        p3a_started = [False] * n
        props = [copy.deepcopy(pr) for pr in hh.properties]
        for ev in hh.events:
            if ev.kind == "home_purchase" and ev.start:
                pr = ev.params
                props.append(Property(
                    value=float(pr.get("price", 0)), tax_value=float(pr.get("price", 0)) * float(pr.get("tax_value_pct", 70)) / 100,
                    mortgage=0.0, rate_pct=float(pr.get("mortgage_rate_pct", 1.8)),
                    owners={int(k): float(v) for k, v in (pr.get("owners") or {}).items()},
                    maintenance_pct=float(pr.get("maintenance_pct", 1.0)),
                    imputed_rent_pct=float(pr.get("imputed_rent_pct", 0.0)),
                    purchase_year=ev.start.year,
                ))
        other_debts = hh.other_debts
        eq = [ad.equity_share for ad in adults]
        bd = [ad.bond_share for ad in adults]
        tgt = a.get("target_equity_pct")
        if tgt is not None:
            eq = [float(tgt) / 100] * n
            bd = [min(b, 1 - e) for b, e in zip(bd, eq)]
        r_liq = [median_return(e, b, a) for e, b in zip(eq, bd)]
        r_3a = median_return(a["pillar3a_equity_pct"] / 100, 1 - a["pillar3a_equity_pct"] / 100, a)
        r_vested = median_return(a["vested_equity_pct"] / 100, 1 - a["vested_equity_pct"] / 100, a)
        avs_rec = [self._past_avs(ad) for ad in adults]
        avs_annual = [0.0] * n
        lpp_rente0: list[tuple[int, float] | None] = [None] * n   # (start year, nominal annual)
        withdrawn_lpp = [False] * n

        years_out = []
        ctxs: list[YearCtx] = []
        min_liquid = float("inf")
        failed_year = None
        fi_year = max(plan.stop_year.values()) if plan.stop_year else self.y0

        for year in range(self.y0, self.y_end + 1):
            f = self.f0 if year == self.y0 else 1.0
            p = P.get(year)
            married = self._married(year) and n == 2
            abroad = self._abroad(year)
            ages = [year - ad.birth_year for ad in adults]
            kids_ages = [(k, _kid_age(k, year)) for k in hh.kids]
            kids_here = [(k, ag) for k, ag in kids_ages if ag is not None and ag < int(self.cc["independent_at"])]

            # ---- work & salaries
            working = [year < plan.stop_year.get(ad.id, ad.birth_year + 65) and ages[i] < 70
                       and (self._salary_today(ad, year) > 0 or ad.side_income > 0)
                       for i, ad in enumerate(adults)]
            salary = [0.0] * n
            for i, ad in enumerate(adults):
                if working[i]:
                    pct = self._work_pct(ad, year)
                    salary[i] = self._salary_today(ad, year) * self._growth(ad, year) * pct / 100
            # maternity / paternity without employer top-up
            for k, ag in kids_ages:
                if ag == 0 and not a.get("employer_maternity_topup", True):
                    for i, ad in enumerate(adults):
                        if salary[i] <= 0:
                            continue
                        days = p["family"]["maternity_days"] if k.birth_parent_id == ad.id else p["family"]["paternity_days"]
                        daily = salary[i] / 365
                        apg = min(p["family"]["apg_rate"] * daily, p["family"]["apg_max_daily"])
                        salary[i] -= (daily - apg) * days
            side = [0.0] * n
            for ev in hh.events:
                if ev.kind == "side_income" and _active(ev, year):
                    for i, ad in enumerate(adults):
                        if ev.member_id in (ad.id, None):
                            side[i] += float(ev.params.get("annual", 0)) / (1 if ev.member_id else n)
            for i, ad in enumerate(adults):
                if working[i] and ad.side_income:
                    side[i] += ad.side_income

            # ---- contributions
            lpp_emp = [0.0] * n
            lpp_cred = [0.0] * n
            for i, ad in enumerate(adults):
                if salary[i] > 0 and not abroad:
                    base_salary = ad.salary if ad.salary > 0 else salary[i]
                    ratio = salary[i] / base_salary if base_salary else 1.0
                    legal = X.lpp_legal_credits(salary[i], ages[i], p)
                    lpp_cred[i] = ad.lpp_credits * ratio if ad.lpp_credits is not None else legal
                    lpp_emp[i] = ad.lpp_employee * ratio if ad.lpp_employee is not None else legal / 2
                    if salary[i] < p["lpp"]["entry_threshold"]:
                        lpp_cred[i] = lpp_emp[i] = 0.0
            social = [T.social_contributions(salary[i], p, aanp_rate=adults[i].aanp_rate,
                                             ijm_rate=adults[i].ijm_rate)["total"] - 0.0 for i in range(n)]
            self_avs = [side[i] * p["avs"]["self_employed_rate"] for i in range(n)]
            p3a_pay = [0.0] * n
            for i, ad in enumerate(adults):
                earned = salary[i] + side[i]
                if earned > 0 and not abroad and ages[i] < 65 and not plan.coast and not p3a_started[i]:
                    has_lpp = lpp_cred[i] > 0
                    cap = X.pillar3a_max(earned, has_lpp, p)
                    want = ad.p3a_annual if ad.p3a_annual is not None else cap
                    p3a_pay[i] = min(want, cap)

            # ---- children: costs, allowances, childcare
            all_work = n > 0 and all(salary[i] > 0 for i in range(n))
            kid_cost = 0.0
            childcare = 0.0
            allowances = 0.0
            for idx, (k, ag) in enumerate(sorted(kids_here, key=lambda t: t[0].birth)):
                kid_cost += _child_monthly_cost(ag, self.cc) * 12
                if all_work and ag <= 3:
                    childcare += self.cc["childcare_monthly"] * 12
                elif all_work and ag <= 12:
                    childcare += self.cc["afterschool_monthly"] * 12
                if any(working) and not abroad:
                    third = idx >= 2
                    if ag < 16:
                        allowances += 12 * (p["family"]["child_allowance_monthly_3rd"] if third else p["family"]["child_allowance_monthly"])
                    else:
                        allowances += 12 * (p["family"]["training_allowance_monthly_3rd"] if third else p["family"]["training_allowance_monthly"])
                    if ag == 0:
                        allowances += p["family"]["birth_allowance"]
            if n == 1:
                allowances *= float(hh.settings.get("_individual_kid_share", 1.0))
            receiver = max(range(n), key=lambda i: salary[i]) if n else 0

            # ---- pensions
            pension = [0.0] * n
            capital = [0.0] * n            # pension capital taken this year (taxed separately)
            for i, ad in enumerate(adults):
                age = ages[i]
                start_age = int(a["avs_start_age"])
                # AVS record: contributions of the year
                if age < 65 and year > self.y0 - 1:
                    self._avs_year(avs_rec[i], year, working[i], salary[i] + side[i], abroad)
                if age >= start_age and avs_annual[i] == 0:
                    avs_annual[i] = self._avs_pension(avs_rec, i, year, married, p, start_age)
                if age >= start_age:
                    pension[i] += avs_annual[i]
                # LPP: at stop (≥ 58) from the fund, otherwise vested benefits until 60
                stop = plan.stop_year.get(ad.id, ad.birth_year + 65)
                if not withdrawn_lpp[i]:
                    if not working[i] and year >= stop:
                        if stop - ad.birth_year >= p["lpp"]["earliest_age"] or age >= 65:
                            capital[i] += self._take_lpp(i, lpp, lpp_rente0, year, ad)
                            withdrawn_lpp[i] = True
                        elif lpp[i] > 0:
                            vested[i] += lpp[i]
                            lpp[i] = 0.0
                if vested[i] > 0 and age >= max(p["lpp"]["vested_earliest_age"], 60) and not working[i]:
                    capital[i] += vested[i]
                    vested[i] = 0.0
                if lpp_rente0[i]:
                    start_y, nominal = lpp_rente0[i]
                    pension[i] += nominal / (1 + infl / 100) ** (year - start_y)
                # 3a: one account a year from the chosen age (staggered = lower tax), all out by 64
                w_age = int(a["pillar3a_withdraw_from_age"])
                if p3a[i] > 0 and age >= w_age:
                    k = p3a_left[i]
                    take = p3a[i] if (k <= 1 or age >= 64) else p3a[i] / k
                    capital[i] += take
                    p3a[i] -= take
                    p3a_left[i] = max(1, k - 1)
                    p3a_started[i] = True

            # ---- property: purchase, mortgage costs, amortisation
            housing_cost = 0.0
            amort = [0.0] * n
            prop_wealth = [0.0] * n
            imputed = [0.0] * n
            prop_costs = [0.0] * n
            mort_interest = [0.0] * n
            owns_home = False
            lump = [0.0] * n
            for pr in props:
                if pr.purchase_year is not None and year < pr.purchase_year:
                    continue
                owners = pr.owners or {ad.id: 1 / n for ad in adults}
                if pr.purchase_year == year:
                    ev = next((e for e in hh.events if e.kind == "home_purchase" and e.start and e.start.year == year), None)
                    epar = ev.params if ev else {}
                    down = float(epar.get("down_payment", pr.value * 0.2))
                    use_3a = float(epar.get("use_3a", 0))
                    use_lpp = float(epar.get("use_lpp", 0))
                    for i, ad in enumerate(adults):
                        sh = owners.get(ad.id, 0.0)
                        take3 = min(p3a[i], use_3a * sh)
                        takel = min(lpp[i], use_lpp * sh)
                        p3a[i] -= take3
                        lpp[i] -= takel
                        capital[i] += take3 + takel
                        lump[i] -= down * sh + pr.value * 0.03 * sh   # equity + ~3 % purchase costs (notary, registry)
                    pr.mortgage = pr.value - down
                owns_home = True
                interest = pr.mortgage * pr.rate_pct / 100
                maint = pr.value * pr.maintenance_pct / 100
                target = pr.value * pr.amortize_to_pct / 100
                yearly_amort = 0.0
                if pr.mortgage > target:
                    yearly_amort = min(pr.mortgage - target, (pr.value * 0.8 - target) / max(1, pr.amortize_years))
                housing_cost += (interest + maint) * f
                for i, ad in enumerate(adults):
                    sh = owners.get(ad.id, 0.0)
                    amort[i] += yearly_amort * sh * f
                    prop_wealth[i] += (pr.tax_value - pr.mortgage) * sh
                    imputed[i] += pr.value * pr.imputed_rent_pct / 100 * sh
                    prop_costs[i] += maint * sh
                    mort_interest[i] += interest * sh
                pr.mortgage = max(target, pr.mortgage - yearly_amort * f) if yearly_amort else pr.mortgage
                pr.value *= (1 + a["property_growth_real_pct"] / 100) ** f
                pr.tax_value *= (1 + a["property_growth_real_pct"] / 100) ** f
            for ev in hh.events:
                if ev.kind == "lump_sum" and ev.start and ev.start.year == year:
                    amt = float(ev.params.get("amount", 0))
                    for i, ad in enumerate(adults):
                        if ev.member_id in (None, ad.id):
                            lump[i] += amt / (1 if ev.member_id else n)

            # ---- spending
            free = not any(working)
            spend = hh.spending
            if owns_home:
                spend -= hh.rent
            if free:
                spend *= 1 + a.get("fi_spending_change_pct", 0) / 100
            spend *= plan.spending_factor
            for ev in hh.events:
                if ev.kind == "expense_change" and _active(ev, year):
                    spend += float(ev.params.get("annual_delta", 0))
            if abroad:
                spend *= float(abroad["cost_factor"])
            spend = max(0.0, spend) + kid_cost + childcare
            total_spend = spend * f + housing_cost

            # ---- taxes (+ AVS as non-active, which depends on wealth too)
            children_split = self._children_split(kids_here, salary, n)
            ctx = YearCtx(
                year=year, f=f, married=married, abroad=abroad,
                earners=[], adult_ids=ids, children=children_split,
                parental=self._parental(salary, side, n, kids_here),
                other_wealth=prop_wealth, liquid_shares=[], equity=0.0, bonds=0.0,
                nonactive=[(salary[i] + side[i]) <= 0 and ages[i] < 65 and ages[i] >= 20 and not abroad for i in range(n)],
                pension_income=pension,
            )
            for i, ad in enumerate(adults):
                ctx.earners.append(T.Earner(
                    salary_gross=salary[i], self_employed=side[i], pension=pension[i],
                    other=allowances if i == receiver else 0.0,
                    social=social[i] + self_avs[i], lpp_employee=lpp_emp[i], pillar3a=p3a_pay[i],
                    has_pension_plan=lpp_cred[i] > 0 or p3a_pay[i] > 0,
                    commute=ad.commute if salary[i] > 0 else 0.0, meals_out=ad.meals_out and salary[i] > 0,
                    canteen=ad.canteen, childcare=childcare * self._care_share(i, n),
                    insurance=ad.insurance, rent_net=0.0 if owns_home else hh.rent / n,
                    debt_interest=mort_interest[i], imputed_rent=imputed[i], property_costs=prop_costs[i],
                ))
            tot_liq = sum(liquid)
            ctx.liquid_shares = [l / tot_liq for l in liquid] if tot_liq > 0 else [1 / n] * n
            ctx.equity = sum(e * l for e, l in zip(eq, liquid)) / tot_liq if tot_liq > 0 else (sum(eq) / n if n else 0)
            ctx.bonds = sum(b * l for b, l in zip(bd, liquid)) / tot_liq if tot_liq > 0 else (sum(bd) / n if n else 0)
            taxes, avs_na, tax_detail = self._taxes_for(ctx, [max(0.0, l) for l in liquid])
            for i in range(n):
                if ctx.nonactive[i] and avs_na[i] > 0:
                    avs_rec[i].add(year, X.credited_income_nonactive(avs_na[i], p))
            cap_tax = self._capital_taxes(capital, married, year, abroad)

            # ---- cash flow per adult, then returns
            shares = self._spend_shares(salary, side, pension, n)
            net = []
            for i in range(n):
                inflow = (salary[i] - social[i] - lpp_emp[i] + side[i] - self_avs[i]) * f + pension[i] * f \
                    + (allowances * f if i == receiver else 0.0) + capital[i] - cap_tax[i] + lump[i]
                outflow = taxes[i] * f + avs_na[i] * f + p3a_pay[i] * f + total_spend * shares[i] + amort[i]
                flow = inflow - outflow
                if plan.coast and any(working) and flow > 0:
                    flow = 0.0
                net.append(flow)
            for i in range(n):
                liquid[i] = liquid[i] * (1 + r_liq[i]) ** f + net[i]
            # the household pools its money: a negative balance is covered by the other adult
            if n == 2:
                for i, j in ((0, 1), (1, 0)):
                    if liquid[i] < 0 < liquid[j]:
                        move = min(-liquid[i], liquid[j])
                        liquid[i] += move
                        liquid[j] -= move
            for i in range(n):
                if lpp[i] > 0 or lpp_cred[i] > 0:
                    lpp[i] = lpp[i] * (1 + X.real_rate(a["lpp_interest_nominal_pct"], infl)) ** f + lpp_cred[i] * f
                p3a[i] = p3a[i] * (1 + r_3a) ** f + p3a_pay[i] * f
                vested[i] = vested[i] * (1 + r_vested) ** f
            if keep_ctx:
                ctxs.append(ctx)

            tot = sum(liquid)
            pension_assets = sum(lpp) + sum(vested) + sum(p3a)
            prop_equity = sum(pr.value - pr.mortgage for pr in props
                              if pr.purchase_year is None or pr.purchase_year <= year)
            floor = a.get("liquid_floor", 0) if free else 0.0
            if tot < floor - 1e-6 and failed_year is None:
                failed_year = year
            min_liquid = min(min_liquid, tot)
            years_out.append({
                "year": year, "ages": ages, "kids": [ag for _, ag in kids_here],
                "working": working, "married": married, "abroad": bool(abroad),
                "salary": sum(salary), "side": sum(side), "pension": sum(pension),
                "allowances": allowances, "capital": sum(capital), "capital_tax": sum(cap_tax),
                "taxes": sum(taxes), "avs_nonactive": sum(avs_na), "p3a": sum(p3a_pay),
                "spending": total_spend / f if f else total_spend,
                "net_flow": sum(net),
                "liquid": tot, "liquid_by_adult": list(liquid),
                "pension_assets": pension_assets, "p3a_balance": sum(p3a), "lpp_balance": sum(lpp),
                "vested_balance": sum(vested), "property_equity": prop_equity - other_debts,
                "net_worth": tot + pension_assets + prop_equity - other_debts,
                "tax_detail": tax_detail,
            })

        return {
            "years": years_out,
            "success": failed_year is None,
            "failed_year": failed_year,
            "min_liquid": min_liquid,
            "fi_year": fi_year,
            "avs_annual": avs_annual,
            "ctx": ctxs if keep_ctx else None,
            "returns": {"liquid": r_liq, "p3a": r_3a, "vested": r_vested},
        }

    # ------------------------------------------------------------------ pieces

    def _past_avs(self, ad: Adult) -> X.AvsRecord:
        rec = X.AvsRecord()
        start = self.y0 - ad.avs_past_years
        for y in range(start, self.y0):
            rec.add(y, ad.avs_past_avg_income)
        return rec

    def _avs_year(self, rec: X.AvsRecord, year: int, working: bool, earned: float, abroad) -> None:
        if abroad:
            return
        if working and earned > 0:
            rec.add(year, earned)
        elif year not in rec.incomes:
            rec.incomes[year] = 0.0  # counted; the non-active credit is added by _taxes_for

    def _avs_pension(self, recs: list[X.AvsRecord], i: int, year: int, married: bool, p: dict,
                     start_age: int) -> float:
        # education credits: years where a kid was under 16 (split between the parents)
        rec = copy.deepcopy(recs[i])
        n = len(self.hh.adults)
        for k in self.hh.kids:
            for y in range(k.birth.year + 1, k.birth.year + 17):
                if y in rec.incomes:
                    rec.education_years[y] = max(rec.education_years.get(y, 0.0), 1.0 / n)
        # married years: incomes split 50/50 between the spouses
        if n == 2 and married:
            other = recs[1 - i]
            for y in list(rec.incomes):
                if self._married(y) and y in other.incomes:
                    rec.incomes[y] = (recs[i].incomes[y] + other.incomes[y]) / 2
        r = X.avs_pension_from_record(rec, p, start_age=start_age)
        monthly = r["monthly"]
        if n == 2 and married:
            # couple cap (assume both draw — conservative for the second one)
            other_monthly = X.avs_pension_from_record(copy.deepcopy(recs[1 - i]), p, start_age=start_age)["monthly"]
            monthly, _ = X.couple_cap(monthly, other_monthly, p)
        return monthly * p["avs"]["payments_per_year"]

    def _take_lpp(self, i: int, lpp: list[float], rente0: list, year: int, ad: Adult) -> float:
        mode = self.a["lpp_withdrawal"]
        bal = lpp[i]
        lpp[i] = 0.0
        if mode == "capital" or bal <= 0:
            return bal
        part = bal if mode == "rente" else bal / 2
        nominal = part * ad.lpp_conversion_pct / 100
        # nominal francs of the start year are today's francs only in that year
        rente0[i] = (year, nominal)
        return bal - part

    def _children_split(self, kids_here, salary, n) -> list[list[T.Child]]:
        split = float(self.hh.settings.get("child_tax_split", 0.5))
        out = []
        solo = float(self.hh.settings.get("_individual_kid_share", 1.0))
        for i in range(n):
            share = solo if n == 1 else (split if i == 0 else 1 - split)
            out.append([T.Child(age=ag, share=share, in_childcare=ag < 14) for _, ag in kids_here])
        return out

    def _parental(self, salary, side, n, kids_here) -> list[bool]:
        if not kids_here:
            return [False] * n
        if n == 1:
            return [bool(self.hh.settings.get("_individual_parental", True))]
        hi = max(range(n), key=lambda i: salary[i] + side[i])
        return [i == hi for i in range(n)]

    def _care_share(self, i: int, n: int) -> float:
        if n == 1:
            return float(self.hh.settings.get("_individual_kid_share", 1.0))
        split = float(self.hh.settings.get("child_tax_split", 0.5))
        return split if i == 0 else 1 - split

    def _spend_shares(self, salary, side, pension, n) -> list[float]:
        if n == 1:
            return [1.0]
        if self.hh.settings.get("spending_split") == "equal":
            return [1 / n] * n
        inc = [salary[i] + side[i] + pension[i] for i in range(n)]
        tot = sum(inc)
        return [x / tot for x in inc] if tot > 0 else [1 / n] * n

    def _taxes_for(self, ctx: YearCtx, liquid: list[float]):
        """Income + wealth taxes and AVS non-active contributions for given liquid amounts."""
        a = self.a
        n = len(ctx.adult_ids)
        p = P.get(ctx.year)
        eq, bd = ctx.equity, ctx.bonds
        cash = max(0.0, 1 - eq - bd)
        cash_nominal = max(0.0, (1 + a["cash_real_return_pct"] / 100) * (1 + a["inflation_pct"] / 100) - 1)
        earners = []
        for i in range(n):
            e = copy.copy(ctx.earners[i])
            e.investment = liquid[i] * (eq * a["dividend_yield_pct"] + bd * a["bond_yield_pct"]) / 100
            e.savings_interest = liquid[i] * cash * cash_nominal
            earners.append(e)
        wealth = [liquid[i] + ctx.other_wealth[i] for i in range(n)]
        taxes = [0.0] * n
        detail = None
        if ctx.abroad:
            rate = float(ctx.abroad["tax_rate_pct"]) / 100
            for i in range(n):
                e = earners[i]
                taxes[i] = rate * (e.salary_gross + e.self_employed + e.pension + e.investment + e.other)
        elif ctx.married:
            tp = T.Taxpayer(status="married", earners=earners, children=[T.Child(age=c.age, share=1.0) for c in ctx.children[0]],
                            wealth=sum(wealth), communal_coefficient=self.communal, year=ctx.year)
            r = T.compute(tp)
            gi = [T._gross_income(e) for e in earners]
            tot = sum(gi)
            for i in range(n):
                taxes[i] = r["total"] * (gi[i] / tot if tot > 0 else 1 / n)
            detail = {"joint": _short(r)}
        else:
            detail = {}
            for i in range(n):
                tp = T.Taxpayer(status="single", earners=[earners[i]], children=ctx.children[i],
                                single_parent=self.hh.single_parent and n == 1,
                                ifd_parental=ctx.parental[i], wealth=wealth[i],
                                communal_coefficient=self.communal, year=ctx.year)
                r = T.compute(tp)
                taxes[i] = r["total"]
                detail[str(ctx.adult_ids[i])] = _short(r)
        # AVS as non-active person
        avs_na = [0.0] * n
        if any(ctx.nonactive):
            for i in range(n):
                if not ctx.nonactive[i]:
                    continue
                if ctx.married and n == 2:
                    j = 1 - i
                    spouse_contrib = ctx.earners[j].salary_gross * p["avs"]["employee_rate"] * 2
                    if spouse_contrib >= p["avs"]["spouse_cover_min"]:
                        continue
                    base = (sum(wealth) + 20 * sum(ctx.pension_income)) / 2
                else:
                    base = wealth[i] + 20 * ctx.pension_income[i]
                avs_na[i] = X.nonactive_contribution(base, p)
        return taxes, avs_na, detail

    def _capital_taxes(self, capital: list[float], married: bool, year: int, abroad) -> list[float]:
        n = len(capital)
        if not any(capital):
            return [0.0] * n
        if abroad:
            r = float(abroad["capital_tax_pct"]) / 100
            return [c * r for c in capital]
        if married and n == 2:
            t = T.capital_tax(sum(capital), married=True, year=year, communal_pct=self.communal)["total"]
            tot = sum(capital)
            return [t * c / tot for c in capital]
        return [T.capital_tax(c, married=False, year=year, communal_pct=self.communal)["total"] if c > 0 else 0.0
                for c in capital]


def _short(r: dict) -> dict:
    return {"ifd": r["ifd"]["tax"], "icc": r["vd"]["tax"], "wealth": r["wealth"]["total"],
            "taxable_ifd": r["ifd"]["taxable"], "taxable_vd": r["vd"]["taxable"], "total": r["total"]}


_DEFAULT_ASSUMPTIONS = {
    "inflation_pct": 1.0, "equity_real_return_pct": 4.5, "equity_vol_pct": 16.0,
    "bond_real_return_pct": 0.5, "bond_vol_pct": 5.0, "cash_real_return_pct": -0.5,
    "equity_bond_corr": 0.1, "dividend_yield_pct": 2.0, "bond_yield_pct": 1.0,
    "salary_growth_real_pct": 1.0, "salary_growth_until_age": 50,
    "lpp_interest_nominal_pct": 1.75, "pillar3a_equity_pct": 90, "vested_equity_pct": 60,
    "property_growth_real_pct": 0.5, "swr_pct": 3.5, "horizon_age": 95, "mc_runs": 1000,
    "mc_seed": 42, "lpp_withdrawal": "capital", "pillar3a_accounts": 3,
    "pillar3a_withdraw_from_age": 60, "avs_start_age": 65, "liquid_floor": 0,
    "employer_maternity_topup": True, "fi_spending_change_pct": 0, "target_equity_pct": None,
}
_DEFAULT_CHILD_COSTS = {"0-3": 900, "4-12": 1000, "13-17": 1200, "18-24": 1300,
                        "childcare_monthly": 1500, "afterschool_monthly": 400, "independent_at": 25}


# --------------------------------------------------------------------------- solver


def plan_for_stop_year(eng: Engine, year: int, **kw) -> Plan:
    stop = {}
    for ad in eng.hh.adults:
        if ad.retire_age_target:
            stop[ad.id] = ad.birth_year + ad.retire_age_target
        else:
            stop[ad.id] = max(year, eng.y0)
    return Plan(stop_year=stop, **kw)


def solve_fi_year(eng: Engine, *, spending_factor: float = 1.0) -> dict:
    """Earliest calendar year where everyone can stop working and the plan holds."""
    lo = eng.y0
    last = max(ad.birth_year + 70 for ad in eng.hh.adults) if eng.hh.adults else eng.y0
    cache: dict[int, dict] = {}

    def ok(y: int) -> bool:
        if y not in cache:
            cache[y] = eng.run(plan_for_stop_year(eng, y, spending_factor=spending_factor))
        return cache[y]["success"]

    if not ok(last):
        return {"fi_year": None, "run": cache[last]}
    hi = last
    if ok(lo):
        return {"fi_year": lo, "run": cache[lo]}
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return {"fi_year": hi, "run": cache[hi]}


def required_liquid_now(eng: Engine, plan: Plan, *, tol: float = 500.0) -> float:
    """Smallest liquid wealth today that makes `plan` work (bisection on an extra amount)."""
    current = sum(ad.liquid for ad in eng.hh.adults)
    if eng.run(plan).get("success"):
        lo, hi = -current, 0.0
        if eng.run(Plan(**{**plan.__dict__, "extra_liquid": lo}))["success"]:
            return 0.0
    else:
        lo, hi = 0.0, max(100_000.0, current)
        while not eng.run(Plan(**{**plan.__dict__, "extra_liquid": hi}))["success"]:
            lo, hi = hi, hi * 2
            if hi > 1e9:
                return float("inf")
    while hi - lo > tol:
        mid = (lo + hi) / 2
        if eng.run(Plan(**{**plan.__dict__, "extra_liquid": mid}))["success"]:
            hi = mid
        else:
            lo = mid
    return max(0.0, current + hi)


# --------------------------------------------------------------------------- Monte Carlo

_GRID = [0, 5e3, 1.5e4, 3e4, 6e4, 1e5, 1.5e5, 2.5e5, 3.5e5, 5e5, 7e5, 1e6, 1.4e6, 2e6, 3e6, 4.5e6, 7e6, 1.2e7, 2.5e7]


def monte_carlo(eng: Engine, plan: Plan, runs: int | None = None, seed: int | None = None) -> dict:
    import numpy as np

    a = eng.a
    runs = int(runs or a["mc_runs"])
    seed = int(a["mc_seed"] if seed is None else seed)
    det = eng.run(plan, keep_ctx=True)
    years = det["years"]
    ctxs = det["ctx"]
    T_ = len(years)
    # deterministic flows excluding the wealth-dependent part (taxes + AVS non-active)
    D = np.zeros(T_)
    grids_x, grids_y = [], []
    for t, (yr, ctx) in enumerate(zip(years, ctxs)):
        dep = (yr["taxes"] + yr["avs_nonactive"]) * ctx.f
        D[t] = yr["net_flow"] + dep
        liq_start = years[t - 1]["liquid"] if t else sum(ad.liquid for ad in eng.hh.adults) + plan.extra_liquid
        xs = sorted(set([max(0.0, liq_start)] + [g for g in _GRID]))
        ys = []
        for x in xs:
            split = [x * s for s in ctx.liquid_shares]
            taxes, avs_na, _ = eng._taxes_for(ctx, split)
            ys.append((sum(taxes) + sum(avs_na)) * ctx.f)
        grids_x.append(np.array(xs))
        grids_y.append(np.array(ys))
    # returns
    rng = np.random.default_rng(seed)
    me, se = a["equity_real_return_pct"] / 100, a["equity_vol_pct"] / 100
    mb, sb = a["bond_real_return_pct"] / 100, a["bond_vol_pct"] / 100
    mc = a["cash_real_return_pct"] / 100
    m_e, s_e = lognormal_params(me, se)
    m_b, s_b = lognormal_params(mb, sb)
    rho = a.get("equity_bond_corr", 0.0)
    z1 = rng.standard_normal((runs, T_))
    z2 = rho * z1 + math.sqrt(max(0.0, 1 - rho * rho)) * rng.standard_normal((runs, T_))
    r_e = np.exp(m_e + s_e * z1) - 1
    r_b = np.exp(m_b + s_b * z2) - 1
    L = np.full(runs, sum(ad.liquid for ad in eng.hh.adults) + plan.extra_liquid, dtype=float)
    out = np.zeros((runs, T_))
    alive = np.ones(runs, dtype=bool)
    fail_year = np.full(runs, -1)
    for t, ctx in enumerate(ctxs):
        eq, bd = ctx.equity, ctx.bonds
        cash = max(0.0, 1 - eq - bd)
        r = eq * r_e[:, t] + bd * r_b[:, t] + cash * mc
        tax = np.interp(np.maximum(L, 0.0), grids_x[t], grids_y[t])
        L = L * (1 + r) ** ctx.f + D[t] - tax
        newly = alive & (L < (a.get("liquid_floor", 0) if not any(years[t]["working"]) else 0.0) - 1e-6)
        fail_year[newly] = years[t]["year"]
        alive &= ~newly
        out[:, t] = L
    pct = {k: np.percentile(out, q, axis=0).tolist() for k, q in
           (("p10", 10), ("p25", 25), ("p50", 50), ("p75", 75), ("p90", 90))}
    return {
        "runs": runs, "seed": seed,
        "success_rate": float(alive.mean()),
        "years": [y["year"] for y in years],
        "liquid": pct,
        "deterministic": [y["liquid"] for y in years],
        "fail_years": {int(y): int((fail_year == y).sum()) for y in np.unique(fail_year[fail_year > 0])},
    }
