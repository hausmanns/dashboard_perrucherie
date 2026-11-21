"""The three pillars: AVS (1st), LPP (2nd), 3a (3rd).

AVS pension (LAVS art. 29bis-38)
--------------------------------
monthly full pension (scale 44) from the average yearly income (RAMD):
    RAMD ≤ 36 × min : 74/100 × min + 13/600 × RAMD
    RAMD > 36 × min : 104/100 × min + 8/600 × RAMD
bounded by [min, max] (1'260 … 2'520 fr. in 2026, reached at 15'120 and
90'720 fr.). Missing contribution years scale it down (years / 44). Education
credits (3 × the minimum yearly pension per year with a child under 16) are
added to the incomes. Married couples: both pensions together are capped at
150 % of the maximum. 13 payments a year from 2026.

People who stop working before 65 keep paying AVS as non-active persons
(wealth + 20 × pension income → table); those contributions are credited back
as income for the pension (contribution × AVS share / 8.7 %).

The engine works in today's francs, so past and future incomes are treated as
already revalued — which is what the AVS revaluation factor does.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import params as P


# --------------------------------------------------------------------------- AVS

def avs_full_monthly(ramd: float, p: dict) -> float:
    """Full (scale 44) monthly old-age pension for an average yearly income `ramd`."""
    a = p["avs"]
    mn, mx = a["min_monthly"], a["max_monthly"]
    if ramd <= 36 * mn:
        r = 0.74 * mn + 13 / 600 * ramd
    else:
        r = 1.04 * mn + 8 / 600 * ramd
    return max(mn, min(mx, r))


def avs_monthly(ramd: float, years: float, p: dict) -> float:
    """Monthly pension with a contribution record of `years` (partial pension below 44)."""
    a = p["avs"]
    if years <= 0:
        return 0.0
    full = avs_full_monthly(ramd, p)
    return round(full * min(years, a["full_years"]) / a["full_years"])


def nonactive_contribution(wealth_plus_pension20: float, p: dict) -> float:
    """Yearly AVS/AI/APG contribution of a non-active person (table, mémento 2.03)."""
    a = p["avs"]
    x = max(0.0, wealth_plus_pension20)
    if x >= a["nonactive_cap_from"]:
        return float(a["nonactive_max"])
    contrib = float(a["nonactive_min"])
    for start, at_start, step in a["nonactive_steps"]:
        if x >= start:
            contrib = at_start + step * int((x - start) // 50_000)
    return min(float(a["nonactive_max"]), contrib)


def credited_income_nonactive(contribution: float, p: dict) -> float:
    """Income written on the AVS account for a non-active contribution (530 fr. → 5'000 fr.)."""
    a = p["avs"]
    return contribution * a["nonactive_avs_share"] / a["avs_rate_for_credit"]


def education_credit(p: dict) -> float:
    a = p["avs"]
    return a["education_credit_factor"] * a["min_monthly"] * 12


def early_late_factor(start_age: int, p: dict) -> float:
    """Multiplier for a pension drawn before/after the reference age (63-70)."""
    a = p["avs"]
    diff = start_age - a["ref_age"]
    if diff < 0:
        years = min(-diff, a["early_max_years"])
        return 1 - a["early_reduction_per_year"] * years
    if diff > 0:
        return 1 + a["deferral_increase"][min(diff, 5)]
    return 1.0


@dataclass
class AvsRecord:
    """A person's AVS account as the engine builds it, year by year."""
    incomes: dict[int, float] = field(default_factory=dict)   # year → income credited (only years WITH contributions)
    education_years: dict[int, float] = field(default_factory=dict)  # year → share of the credit (0.5 = split)

    def add(self, year: int, income: float) -> None:
        self.incomes[year] = self.incomes.get(year, 0.0) + max(0.0, income)


def avs_pension_from_record(rec: AvsRecord, p: dict, *, start_age: int = 65) -> dict:
    """Pension from a contribution record: RAMD over the years with contributions,
    scale = those years / 44, then the early/late factor."""
    years = len(rec.incomes)
    edu = education_credit(p)
    total_income = sum(rec.incomes.values()) + sum(edu * s for s in rec.education_years.values())
    ramd = total_income / years if years else 0.0
    monthly = avs_monthly(ramd, years, p) * early_late_factor(start_age, p)
    return {"ramd": ramd, "years": years, "monthly": monthly,
            "annual": monthly * p["avs"]["payments_per_year"]}


def couple_cap(monthly_a: float, monthly_b: float, p: dict) -> tuple[float, float]:
    """Married couple: the two pensions together ≤ 150 % of the maximum (reduced proportionally)."""
    cap = p["avs"]["couple_cap_monthly"]
    total = monthly_a + monthly_b
    if total <= cap or total <= 0:
        return monthly_a, monthly_b
    f = cap / total
    return monthly_a * f, monthly_b * f


# --------------------------------------------------------------------------- LPP

def lpp_coordinated_salary(gross: float, p: dict) -> float:
    lpp = p["lpp"]
    if gross < lpp["entry_threshold"]:
        return 0.0
    return max(lpp["min_coordinated"], min(gross, lpp["max_salary"]) - lpp["coordination"])


def lpp_age_rate(age: int, p: dict) -> float:
    return next((r for lo, hi, r in p["lpp"]["age_credits"] if lo <= age <= hi), 0.0)


def lpp_legal_credits(gross: float, age: int, p: dict) -> float:
    """Legal minimum yearly savings credits (employee + employer)."""
    if age >= p["avs"]["ref_age"]:
        return 0.0
    return lpp_coordinated_salary(gross, p) * lpp_age_rate(age, p)


def pillar3a_max(gross_earned: float, has_lpp: bool, p: dict) -> float:
    """Maximum deductible 3a payment for the year."""
    t = p["p3a"]
    if gross_earned <= 0:
        return 0.0
    if has_lpp:
        return float(t["max_with_lpp"])
    return min(float(t["max_without_lpp"]), t["max_without_lpp_pct"] * gross_earned)


def real_rate(nominal_pct: float, inflation_pct: float) -> float:
    """(1 + nominal) / (1 + inflation) - 1, as a fraction."""
    return (1 + nominal_pct / 100) / (1 + inflation_pct / 100) - 1


def project_lpp(balance: float, credits_per_year: float, years: int, interest_real: float) -> float:
    """Balance after `years` of credits (paid at year end) and interest."""
    b = balance
    for _ in range(max(0, years)):
        b = b * (1 + interest_real) + credits_per_year
    return b


__all__ = [
    "AvsRecord", "avs_full_monthly", "avs_monthly", "avs_pension_from_record", "couple_cap",
    "credited_income_nonactive", "early_late_factor", "education_credit", "lpp_age_rate",
    "lpp_coordinated_salary", "lpp_legal_credits", "nonactive_contribution", "pillar3a_max",
    "project_lpp", "real_rate", "P",
]
