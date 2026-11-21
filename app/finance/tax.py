"""Swiss taxes for a Vaud household: federal (IFD) + canton/commune (ICC).

What is computed
----------------
- Employee social contributions (AVS/AI/APG, AC, AANP, IJM, LPP).
- Taxable income for each jurisdiction, with the deductions that matter for a
  salaried household: professional expenses, pension contributions, insurance
  premiums, childcare, two-earner, child/spouse deductions (IFD), family,
  modest-income and housing deductions (Vaud).
- IFD income tax (single / married-parental scale, 263 fr. per child, 11.5 %
  cap, 25 fr. minimum, rounded down to 5 ct.).
- Vaud income tax with the quotient familial (and its cap for children),
  the cantonal coefficient, the yearly cantonal reduction and the communal
  coefficient.
- Vaud wealth tax (pension assets are exempt by law — callers pass taxable
  wealth only).
- Taxes on pension capital (2nd pillar, 3a): separately, IFD at 1/5 of the
  scale, Vaud at 1/5 of the rates.

Who is a taxpayer
-----------------
Married couples are one taxpayer (incomes added). An unmarried couple is two
taxpayers; a common child is split between them (`child_share`, 0.5 each by
default: Vaud splits the 0.5 part, IFD splits the child deduction), and the
federal parental scale goes to the parent with the higher income (AFC
circular 30, joint parental authority, no maintenance paid).

Numbers are floats in francs; every function is pure and documented with the
article it implements. See tests/finance/test_tax.py for the official test
vectors.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from . import params as P
from .money import floor_5ct, floor_to, round_5ct


# --------------------------------------------------------------------------- helpers

def tariff(brackets: list[tuple], amount: float, per: int = 100) -> float:
    """Tax from a bracket table: base of the last bracket started + rate per `per` above it."""
    tax = 0.0
    for start, base, rate in brackets:
        if amount >= start:
            tax = base + (amount - start) / per * rate
        else:
            break
    return max(0.0, tax)


def legal_lpp_employee(gross: float, age: int, p: dict) -> float:
    """Employee half of the legal minimum savings credits (LPP art. 16). Risk premiums ignored."""
    lpp = p["lpp"]
    if gross < lpp["entry_threshold"] or age < 25 or age >= p["avs"]["ref_age"]:
        return 0.0
    coordinated = max(lpp["min_coordinated"], min(gross, lpp["max_salary"]) - lpp["coordination"])
    rate = next((r for lo, hi, r in lpp["age_credits"] if lo <= age <= hi), 0.0)
    return coordinated * rate / 2


def social_contributions(gross: float, p: dict, *, aanp_rate: float = 0.01, ijm_rate: float = 0.0,
                         lpp_employee: float = 0.0) -> dict:
    """What the employer withholds from a gross salary (employee shares)."""
    gross = max(0.0, gross)
    avs = gross * p["avs"]["employee_rate"]
    ac = min(gross, p["ac"]["ceiling"]) * p["ac"]["rate"]
    aanp = min(gross, p["uvg_ceiling"]) * aanp_rate
    ijm = gross * ijm_rate
    total = avs + ac + aanp + ijm + lpp_employee
    return {"avs": avs, "ac": ac, "aanp": aanp, "ijm": ijm, "lpp": lpp_employee, "total": total,
            "net": gross - total}


# --------------------------------------------------------------------------- inputs

@dataclass
class Earner:
    """One person's taxable year. All amounts per year, in francs."""
    salary_gross: float = 0.0          # employment income, AVS-liable (13th + bonus included)
    self_employed: float = 0.0         # net side income as self-employed
    pension: float = 0.0               # AVS + LPP pensions (taxed 100 %)
    investment: float = 0.0            # dividends + bond coupons
    savings_interest: float = 0.0      # interest on savings accounts
    other: float = 0.0                 # family allowances received, alimony…
    social: float = 0.0                # AVS/AC/AANP/IJM employee contributions (+ self-employed AVS)
    lpp_employee: float = 0.0          # ordinary LPP contributions (employee share)
    lpp_buyin: float = 0.0             # voluntary buy-in (rachat)
    pillar3a: float = 0.0
    has_pension_plan: bool = True      # affiliated to LPP or pays 3a (IFD insurance deduction)
    commute: float = 0.0               # actual commuting costs
    meals_out: bool = True
    canteen: bool = False
    childcare: float = 0.0             # documented third-party childcare paid by this person
    insurance: float = 5_400.0         # premiums paid (LAMal + supplementary + life)
    rent_net: float = 0.0              # share of the net rent (Vaud housing deduction)
    debt_interest: float = 0.0
    imputed_rent: float = 0.0          # valeur locative (owner-occupier)
    property_costs: float = 0.0        # maintenance, deductible


@dataclass
class Child:
    age: int = 0
    share: float = 1.0                 # this taxpayer's share of the child (0.5 = split)
    in_childcare: bool = True          # < 14 and cared for by a third party


@dataclass
class Taxpayer:
    status: str = "single"             # single | married
    earners: list[Earner] = field(default_factory=list)   # 1 (single) or 2 (married)
    children: list[Child] = field(default_factory=list)
    single_parent: bool = False        # lives ALONE with the child (not in concubinage) → Vaud 1.3
    ifd_parental: bool = False         # federal parental scale + 263/child (single parent or higher-income cohabiting parent)
    wealth: float = 0.0                # taxable net wealth (pension assets excluded)
    communal_coefficient: float = 78.5 # %
    year: int = 2026

    @property
    def married(self) -> bool:
        return self.status == "married"


# --------------------------------------------------------------------------- deductions

def _professional(e: Earner, d: dict) -> float:
    """LIFD art. 26 / LI art. 30: forfait 3 % of net salary (min/max), meals, commuting."""
    if e.salary_gross <= 0:
        return 0.0
    net_salary = max(0.0, e.salary_gross - e.social - e.lpp_employee)
    forfait = min(d["prof_max"], max(d["prof_min"], d["prof_pct"] * net_salary))
    meals = (d["meals_canteen"] if e.canteen else d["meals"]) if e.meals_out else 0.0
    transport = e.commute if d["transport_max"] is None else min(e.commute, d["transport_max"])
    return min(net_salary, forfait + meals + transport)


def _earned_net(e: Earner, d: dict) -> float:
    """Salary + self-employed income after contributions and professional expenses
    (the base of the two-earner deduction)."""
    return max(0.0, e.salary_gross + e.self_employed - e.social - e.lpp_employee - e.lpp_buyin
               - e.pillar3a - _professional(e, d))


def _gross_income(e: Earner) -> float:
    return (e.salary_gross + e.self_employed + e.pension + e.investment + e.savings_interest
            + e.other + e.imputed_rent)


def _base_net(e: Earner, d: dict) -> float:
    """Income minus the "organic" deductions (contributions, professional, property, debt)."""
    return (_gross_income(e) - e.social - e.lpp_employee - e.lpp_buyin - e.pillar3a
            - _professional(e, d) - e.property_costs - e.debt_interest)


def ifd_taxable(tp: Taxpayer, p: dict) -> dict:
    d = p["ifd"]["deductions"]
    lines: list[tuple[str, float]] = []
    net = sum(_base_net(e, d) for e in tp.earners)
    children = sum(c.share for c in tp.children)
    pension_plan = any(e.has_pension_plan for e in tp.earners)
    # insurance premiums + savings interest (LIFD art. 33 al. 1 let. g, 1bis)
    if tp.married:
        cap = d["insurance_married"] if pension_plan else d["insurance_married_no_pension"]
    else:
        cap = d["insurance_single"] if pension_plan else d["insurance_single_no_pension"]
    cap += d["insurance_child"] * children
    paid = sum(e.insurance + e.savings_interest for e in tp.earners)
    lines.append(("Primes d'assurance", min(paid, cap)))
    # childcare (art. 33 al. 3)
    care_cap = d["childcare_max"] * sum(c.share for c in tp.children if c.in_childcare and c.age < 14)
    lines.append(("Frais de garde", min(sum(e.childcare for e in tp.earners), care_cap)))
    # two-earner deduction (art. 33 al. 2)
    if tp.married and len(tp.earners) == 2:
        earned = sorted(_earned_net(e, d) for e in tp.earners)
        low = earned[0]
        if low > 0:
            ded = min(d["two_earner_max"], max(d["two_earner_min"], d["two_earner_pct"] * low))
            lines.append(("Double revenu", min(ded, low)))
    # social deductions (art. 35)
    lines.append(("Enfants", d["child"] * children))
    if tp.married:
        lines.append(("Couple marié", d["spouse"]))
    deductions = sum(v for _, v in lines)
    taxable = floor_to(max(0.0, net - deductions), 100)
    return {"net_income": net, "deductions": [(k, v) for k, v in lines if v], "taxable": taxable}


def vd_taxable(tp: Taxpayer, p: dict) -> dict:
    """Vaud taxable income, in the order of the tax return (codes 650 → 800)."""
    d = p["vd"]["deductions"]
    lines: list[tuple[str, float]] = []
    gross_net = sum(_base_net(e, d) for e in tp.earners)
    children = sum(c.share for c in tp.children)
    # general deductions that come before the "revenu net" (code 650)
    # insurance premiums and savings interest (LI art. 37 al. 1 let. g): two separate caps
    ins_cap = (d["insurance_married"] if tp.married else d["insurance_single"]) + d["insurance_child"] * children
    int_cap = ((d["savings_interest_married"] if tp.married else d["savings_interest_single"])
               + d["savings_interest_child"] * children)
    lines.append(("Primes d'assurance", min(sum(e.insurance for e in tp.earners), ins_cap)))
    lines.append(("Intérêts d'épargne", min(sum(e.savings_interest for e in tp.earners), int_cap)))
    if tp.married and len(tp.earners) == 2:               # code 235, double activité
        low = min(_earned_net(e, d) for e in tp.earners)
        if low > 0:
            lines.append(("Double activité", min(d["two_earner"], low)))
    net = gross_net - sum(v for _, v in lines)              # code 650, revenu net
    # housing (art. 39, code 660): net rent above 20 % of the net income, rent capped
    housing = 0.0
    rent = sum(e.rent_net for e in tp.earners)
    if rent > 0:
        family = tp.married or tp.single_parent
        rent_cap = (d["housing_rent_cap_family"] if family else d["housing_rent_cap_single"]) \
            + d["housing_rent_cap_child"] * children
        housing = min(d["housing_max"], max(0.0, min(rent, rent_cap) - d["housing_income_pct"] * max(0.0, net)))
        lines.append(("Logement", housing))
    # childcare (art. 37 al. 1 let. k, code 670)
    care_cap = d["childcare_max"] * sum(c.share for c in tp.children if c.in_childcare and c.age < 14)
    childcare = min(sum(e.childcare for e in tp.earners), care_cap)
    lines.append(("Frais de garde", childcare))
    # modest taxpayer (art. 42, code 695): on the net income minus housing and childcare
    base_for_modest = max(0.0, net - housing - childcare)
    amount = d["modest_base"]
    if tp.married:
        amount += d["modest_spouse"]
    elif tp.single_parent:
        amount += d["modest_single_parent"]
    amount += d["modest_child"] * children
    limit = amount + 99                                     # 17'000 → limit 17'099 (law: 16'000 / 16'099)
    if base_for_modest > limit:                             # -100 per started 200 francs above the limit
        steps = int((base_for_modest - limit + d["modest_step"] - 1e-9) // d["modest_step"])
        amount = max(0.0, amount - 100 * steps)
    lines.append(("Contribuable modeste", min(amount, base_for_modest)))
    # family (art. 42a, code 725): married couples and single parents only, phased out with income
    if tp.married or tp.single_parent:
        fam = d["family_married"] if tp.married else d["family_single_parent"]
        fam += d["family_child"] * children
        if net > d["family_threshold2"]:
            fam -= 100 * ((d["family_threshold2"] - d["family_threshold"]) // d["family_step"])
            fam -= 100 * int((net - d["family_threshold2"]) // d["family_step2"])
        elif net > d["family_threshold"]:
            fam -= 100 * int((net - d["family_threshold"]) // d["family_step"])
        lines.append(("Famille", max(0.0, fam)))
    deductions = sum(v for _, v in lines)
    taxable = floor_to(max(0.0, gross_net - deductions), 100)
    return {"net_income": net, "deductions": [(k, v) for k, v in lines if v], "taxable": taxable}


# --------------------------------------------------------------------------- taxes

def ifd_income_tax(taxable: float, *, scale: str, children_for_reduction: float, p: dict) -> float:
    """LIFD art. 36: scale (single or married/parental), 11.5 % cap, -263/child, ≥ 25 fr., 5 ct. down."""
    t = p["ifd"]
    income = floor_to(taxable, 100)
    tax = min(tariff(t[scale], income), t["flat_rate"] * income)
    tax = floor_5ct(tax)
    if scale == "married":
        tax = max(0.0, tax - t["child_reduction"] * children_for_reduction)
    return 0.0 if tax < t["min_tax"] else tax


def vd_parts(tp: Taxpayer, p: dict) -> tuple[float, float]:
    """(base parts, child parts) of the Vaud quotient familial (LI art. 43)."""
    pr = p["vd"]["parts"]
    base = pr["married"] if tp.married else (pr["single_parent"] if tp.single_parent else pr["single"])
    child = pr["child"] * sum(c.share for c in tp.children if c.age < 25)
    return base, child


def vd_rate_income(taxable: float, base_parts: float, child_parts: float, n_children: int, p: dict) -> float:
    """Revenu déterminant pour le taux (art. 43 + cap of the children's reduction, art. 43 al. 3)."""
    det0 = taxable / base_parts
    det = taxable / (base_parts + child_parts)
    if child_parts > 0 and n_children > 0:
        v = p["vd"]
        cap_income = v["child_cap_income"] + v["child_cap_extra"] * (n_children - 1)
        cap = cap_income / base_parts - cap_income / (base_parts + child_parts)
        if det0 - det > cap:
            det = det0 - cap
    return floor_to(det, 100)


def vd_base_from_rate(taxable: float, rate_income: float, p: dict, divisor: float = 1.0) -> float:
    """Base tax (100 %) = taxable × rate of the rate-determining income (art. 47)."""
    if taxable <= 0 or rate_income <= 0:
        return 0.0
    rate = tariff(p["vd"]["income"], rate_income) / rate_income / divisor
    return taxable * rate


def vd_coefficients(p: dict, communal_pct: float, *, reduction: float) -> tuple[float, float]:
    cantonal = p["vd"]["cantonal_coefficient"] * (1 - reduction)
    return cantonal, communal_pct / 100.0


def vd_wealth_tax(wealth: float, *, married: bool, communal_pct: float, p: dict) -> dict:
    v = p["vd"]
    exempt = v["wealth_exempt_married"] if married else v["wealth_exempt_single"]
    taxable = floor_to(max(0.0, wealth), 1000)
    if taxable < exempt:
        return {"taxable": taxable, "base": 0.0, "cantonal": 0.0, "communal": 0.0, "total": 0.0}
    base = tariff(v["wealth"], taxable, per=1000)
    c, m = vd_coefficients(p, communal_pct, reduction=v["wealth_reduction"])
    cantonal, communal = round_5ct(base * c), round_5ct(base * m)
    return {"taxable": taxable, "base": base, "cantonal": cantonal, "communal": communal,
            "total": cantonal + communal}


def compute(tp: Taxpayer) -> dict:
    """All yearly taxes of one taxpayer. Returns a detailed breakdown."""
    p = P.get(tp.year)
    ifd = ifd_taxable(tp, p)
    vd = vd_taxable(tp, p)
    n_children_red = sum(c.share for c in tp.children) if (tp.married or tp.ifd_parental) else 0.0
    scale = "married" if (tp.married or tp.ifd_parental) else "single"
    ifd_tax = ifd_income_tax(ifd["taxable"], scale=scale, children_for_reduction=n_children_red, p=p)

    base_parts, child_parts = vd_parts(tp, p)
    n_children = sum(1 for c in tp.children if c.share > 0 and c.age < 25)
    rate_income = vd_rate_income(vd["taxable"], base_parts, child_parts, n_children, p)
    base = vd_base_from_rate(vd["taxable"], rate_income, p)
    c, m = vd_coefficients(p, tp.communal_coefficient, reduction=p["vd"]["income_reduction"])
    icc_cantonal, icc_communal = round_5ct(base * c), round_5ct(base * m)
    wealth = vd_wealth_tax(tp.wealth, married=tp.married, communal_pct=tp.communal_coefficient, p=p)

    income_total = sum(_gross_income(e) for e in tp.earners)
    total = ifd_tax + icc_cantonal + icc_communal + wealth["total"]
    return {
        "year": tp.year,
        "status": tp.status,
        "gross_income": income_total,
        "ifd": {"net_income": ifd["net_income"], "deductions": ifd["deductions"],
                "taxable": ifd["taxable"], "scale": scale, "tax": ifd_tax},
        "vd": {"net_income": vd["net_income"], "deductions": vd["deductions"],
               "taxable": vd["taxable"], "parts": base_parts + child_parts,
               "rate_income": rate_income, "base_tax": base,
               "rate_pct": (base / vd["taxable"] * 100) if vd["taxable"] else 0.0,
               "cantonal": icc_cantonal, "communal": icc_communal,
               "tax": icc_cantonal + icc_communal},
        "wealth": wealth,
        "total": total,
        "effective_rate_pct": (total / income_total * 100) if income_total > 0 else 0.0,
    }


def marginal_rate(tp: Taxpayer, delta: float = 1000.0) -> float:
    """Tax saved per franc of extra deduction — the value of a 3a payment or an LPP buy-in."""
    base = compute(tp)["total"]
    tp2 = copy.deepcopy(tp)
    if tp2.earners:
        tp2.earners[0].pillar3a += delta
    return max(0.0, (base - compute(tp2)["total"]) / delta)


def capital_tax(amount: float, *, married: bool, year: int, communal_pct: float,
                single_parent: bool = False) -> dict:
    """Tax on a pension capital (2nd pillar, 3a) taken in `year`.

    IFD art. 38: one fifth of the art. 36 scale (married/parental scale for
    couples and single parents, no 263 reduction).
    Vaud art. 49: one fifth of the art. 47 rates; married couples add their
    capitals of the year and use the 1.8 quotient (no child parts). The yearly
    cantonal reduction does not apply (LRIPP art. 4).
    Callers sum the capitals of one taxpayer (or married couple) and year first.
    """
    p = P.get(year)
    amount = floor_to(max(0.0, amount), 100)
    if amount <= 0:
        return {"amount": 0.0, "ifd": 0.0, "cantonal": 0.0, "communal": 0.0, "total": 0.0, "rate_pct": 0.0}
    scale = "married" if (married or single_parent) else "single"
    t = p["ifd"]
    ifd = floor_5ct(min(tariff(t[scale], amount), t["flat_rate"] * amount) / t["capital_divisor"])
    if ifd < t["min_tax"]:
        ifd = 0.0
    q = p["vd"]["parts"]["married"] if married else p["vd"]["parts"]["single"]
    rate_income = floor_to(amount / q, 100)
    base = vd_base_from_rate(amount, rate_income, p, divisor=p["vd"]["capital_divisor"])
    c, m = vd_coefficients(p, communal_pct, reduction=0.0)
    cantonal, communal = round_5ct(base * c), round_5ct(base * m)
    total = ifd + cantonal + communal
    return {"amount": amount, "ifd": ifd, "cantonal": cantonal, "communal": communal,
            "total": total, "rate_pct": total / amount * 100}
