"""Official Swiss figures used by the engine, per tax year.

Every number below comes from an official 2026 publication (sources at the end
of each block). Values marked `# estimated` are not published in a readable
form and are derived (indexation) — they only matter in edge cases, and the
calibration screen compares the engine with the real tax bill anyway.

`get(year)` returns the figures for a year: 2026 is the base; later years add
the changes already voted (Vaud -12 % from 2027, LPP minimum interest 2 % in
2027). Projections are in real terms, so keeping 2026 brackets for later years
is exactly what the yearly cold-progression compensation does. Years before
2026 reuse 2026 (good enough to check an older bill).
"""
from __future__ import annotations

import copy
from functools import lru_cache

# Brackets: (start of bracket, tax at start, rate per 100 francs above start).
# Income below the first start is untaxed.

BASE_2026: dict = {
    "year": 2026,
    # ----------------------------------------------------------------- AVS / AI / APG
    # Source: OFAS « Montants valables à partir du 1er janvier 2026 » (06.11.2025);
    # mémento 2.03 (table des cotisations des personnes sans activité lucrative, 2026).
    "avs": {
        "min_monthly": 1260,
        "max_monthly": 2520,
        "couple_cap_monthly": 3780,          # married: sum of both pensions capped at 150 %
        "payments_per_year": 13,             # 13th AVS pension from 2026 (1/12 of the year's pensions, December)
        "full_years": 44,                    # complete contribution record (scale 44)
        "ref_age": 65,
        "employee_rate": 0.053,              # AVS/AI/APG, employee share (employer pays the same)
        "self_employed_rate": 0.10,          # approx. (degressive below 60'500)
        "nonactive_min": 530,                # AVS 435 + AI 70 + APG 25
        "nonactive_avs_share": 435 / 530,    # part of a non-active contribution that is AVS
        "nonactive_max": 26500,
        # wealth + 20 × pension income: < 350'000 → min; then steps of 50'000
        "nonactive_steps": [                 # (from, contribution at that step, increment per further 50'000)
            (350_000, 636, 106),
            (1_750_000, 3604, 159),
        ],
        "nonactive_cap_from": 8_950_000,
        "spouse_cover_min": 1060,            # a working spouse paying ≥ 2 × min covers the other one
        "avs_rate_for_credit": 0.087,        # AVS part of the 10.6 % (non-active contribution → credited income)
        "education_credit_factor": 3,        # × the minimum yearly pension, per year with a child < 16
        "early_max_years": 2,
        "early_reduction_per_year": 0.068,
        "deferral_increase": {1: 0.052, 2: 0.108, 3: 0.171, 4: 0.240, 5: 0.315},
    },
    "ac": {"rate": 0.011, "ceiling": 148_200},     # chômage, employee share, up to the UVG ceiling
    "uvg_ceiling": 148_200,                         # AANP (non-professional accident) ceiling
    # ----------------------------------------------------------------- LPP (2nd pillar)
    # Source: OFAS 2026 amounts; Conseil fédéral (taux minimal 1.25 % 2026, 2 % 2027).
    "lpp": {
        "entry_threshold": 22_680,
        "coordination": 26_460,
        "max_salary": 90_720,
        "min_coordinated": 3_780,
        "min_interest_pct": 1.25,
        "conversion_rate_pct": 6.8,           # legal minimum on the mandatory part
        "age_credits": [(25, 34, 0.07), (35, 44, 0.10), (45, 54, 0.15), (55, 65, 0.18)],
        "earliest_age": 58,                   # early retirement allowed by most fund rules
        "vested_earliest_age": 60,            # vested benefits: 5 years before the reference age
        "vested_latest_age": 70,
    },
    # ----------------------------------------------------------------- 3a
    "p3a": {"max_with_lpp": 7_258, "max_without_lpp": 36_288, "max_without_lpp_pct": 0.20,
            "earliest_age": 60, "latest_age": 65, "latest_age_working": 70},
    # ----------------------------------------------------------------- Federal direct tax (IFD)
    # Source: AFC form 58c 2026 « Tableau servant à calculer l'impôt fédéral direct »;
    # AFC « Aperçu des bases de calcul … impôt à la source 2026 » for the deductions.
    "ifd": {
        "single": [
            (18_500, 25.41, 0.77), (33_200, 138.60, 0.88), (43_500, 229.20, 2.64),
            (58_000, 612.00, 2.97), (76_200, 1_152.50, 5.94), (82_100, 1_502.95, 6.60),
            (108_900, 3_271.75, 8.80), (141_500, 6_140.55, 11.00), (185_100, 10_936.55, 13.20),
        ],
        "married": [  # also single parents living with their child (« barème parental »)
            (29_700, 0.00, 1.00), (53_400, 237.00, 2.00), (61_300, 395.00, 3.00),
            (79_100, 929.00, 4.00), (94_900, 1_561.00, 5.00), (108_700, 2_251.00, 6.00),
            (120_600, 2_965.00, 7.00), (130_500, 3_658.00, 8.00), (138_400, 4_290.00, 9.00),
            (144_300, 4_821.00, 10.00), (148_300, 5_221.00, 11.00), (150_400, 5_452.00, 12.00),
            (152_400, 5_692.00, 13.00),
        ],
        "flat_rate": 0.115,           # higher incomes: 11.5 % of the whole income
        "child_reduction": 263,       # off the tax, per child (parental scale only)
        "min_tax": 25,                # amounts below 25 francs are not levied
        "capital_divisor": 5,         # pension capital: one fifth of the ordinary scale
        "deductions": {
            "prof_pct": 0.03, "prof_min": 2_000, "prof_max": 4_000,
            "meals": 3_200, "meals_canteen": 1_600,
            "transport_max": 3_300,
            "insurance_single": 1_800, "insurance_single_no_pension": 2_700,
            "insurance_married": 3_700, "insurance_married_no_pension": 5_550,
            "insurance_child": 700,
            "child": 6_800, "spouse": 2_800, "childcare_max": 25_800,
            "two_earner_pct": 0.50, "two_earner_min": 8_600, "two_earner_max": 14_100,
        },
    },
    # ----------------------------------------------------------------- Vaud (ICC)
    # Source: ACI « Tableau des principales déductions vaudoises 2026 » (barèmes
    # sommaires 2026, quotient familial, déductions); LI 642.11 arts 37-60;
    # Loi sur l'impôt 2026 (coefficient 155 %); LRIPP (réduction 5 % en 2026).
    "vd": {
        "income": [
            (0, 0.00, 1.00), (1_600, 16.00, 2.00), (3_400, 52.00, 3.00), (5_100, 103.00, 4.00),
            (8_300, 231.00, 5.00), (11_900, 411.00, 6.00), (15_100, 603.00, 7.00),
            (23_600, 1_198.00, 8.00), (40_500, 2_550.00, 9.00), (57_200, 4_053.00, 10.00),
            (74_400, 5_773.00, 11.00), (91_200, 7_621.00, 12.00), (108_100, 9_649.00, 12.50),
            (135_000, 13_011.50, 13.00), (162_000, 16_521.50, 13.50), (192_500, 20_639.00, 14.00),
            (223_000, 24_909.00, 14.50), (256_000, 29_694.00, 15.00), (291_700, 35_049.00, 15.50),
        ],
        # per 1'000 francs of wealth (‰). First bracket derived from the law (0.24 ‰) + the 2026 table.
        "wealth": [
            (0, 0.00, 0.24), (35_000, 8.40, 0.97), (95_000, 66.60, 1.69), (177_000, 205.20, 2.42),
            (355_000, 635.95, 3.15), (711_000, 1_757.35, 3.39),
        ],
        "wealth_exempt_single": 60_000,
        "wealth_exempt_married": 120_000,
        "cantonal_coefficient": 1.55,
        "income_reduction": 0.05,         # cantonal part only (LRIPP), not on pension capital (art. 49)
        "wealth_reduction": 0.0,
        "capital_divisor": 5,             # art. 49: one fifth of the art. 47 rates
        "parts": {"single": 1.0, "married": 1.8, "single_parent": 1.3, "child": 0.5},
        "child_cap_income": 214_200,      # art. 43 al. 3 (180'000 indexed) # estimated
        "child_cap_extra": 23_800,        # + per further child (20'000 indexed) # estimated
        "deductions": {
            "prof_pct": 0.03, "prof_min": 2_000, "prof_max": 4_000,
            "meals": 3_200, "meals_canteen": 1_600,
            "transport_max": None,
            "insurance_single": 5_000, "insurance_married": 9_900, "insurance_child": 1_300,
            "savings_interest_single": 1_600, "savings_interest_married": 3_300, "savings_interest_child": 300,
            "childcare_max": 15_200,
            "two_earner": 1_700,
            "family_married": 1_300, "family_single_parent": 2_800, "family_child": 1_000,
            "family_threshold": 126_400, "family_step": 2_100,
            "family_threshold2": 164_200, "family_step2": 1_000,
            "modest_base": 17_000, "modest_spouse": 5_700, "modest_single_parent": 3_200,
            "modest_child": 3_500, "modest_step": 200,
            "housing_max": 6_800, "housing_income_pct": 0.20,
            "housing_rent_cap_single": 11_200,   # estimated (9'360 indexed)
            "housing_rent_cap_family": 13_700,   # estimated (11'520 indexed)
            "housing_rent_cap_child": 3_700,     # estimated (3'120 indexed)
        },
    },
    # ----------------------------------------------------------------- Family (Vaud)
    # Source: Caisse AVS Vaud « Prestations familiales 2026 »; APG 2026.
    "family": {
        "child_allowance_monthly": 322, "child_allowance_monthly_3rd": 365,
        "training_allowance_monthly": 425, "training_allowance_monthly_3rd": 468,
        "birth_allowance": 1_617,
        "nonactive_income_limit": 60_480,
        "apg_rate": 0.80, "apg_max_daily": 220,
        "maternity_days": 98, "paternity_days": 14,
    },
}

# Changes already decided for later years (merged over the base).
CHANGES: dict[int, dict] = {
    # Initiative « Baisse d'impôts pour tous » accepted on 27.09.2026: -12 % on the
    # cantonal tax on income and wealth from 2027 (communal part unchanged).
    # Federal Council 04.11.2026: LPP minimum interest 2 % from 2027.
    2027: {"vd": {"income_reduction": 0.12, "wealth_reduction": 0.12}, "lpp": {"min_interest_pct": 2.0}},
}


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@lru_cache(maxsize=None)
def _for_year(year: int) -> dict:
    p = BASE_2026
    for y in sorted(CHANGES):
        if y <= year:
            p = _merge(p, CHANGES[y])
    p = copy.deepcopy(p)
    p["year"] = year
    return p


def get(year: int) -> dict:
    """Figures applicable for `year` (do not mutate the result)."""
    return _for_year(int(year))


SOURCES = [
    "AFC — Formulaire 58c 2026, barèmes de l'impôt fédéral direct",
    "AFC — Aperçu des bases de calcul pour les barèmes fédéraux de l'impôt à la source 2026",
    "ACI Vaud — Tableau des principales déductions vaudoises 2026 (barèmes sommaires, quotient familial)",
    "Loi vaudoise sur les impôts directs cantonaux (LI, BLV 642.11), état 2026",
    "Loi vaudoise sur l'impôt 2026 (coefficient 155 %) et LRIPP (réduction 5 % 2026)",
    "État de Vaud — acceptation de l'initiative « Baisse d'impôts pour tous » (12 % dès 2027)",
    "OFAS — Montants valables à partir du 1er janvier 2026 (AVS, LPP, 3a)",
    "Centre d'information AVS/AI — Mémento 2.03, cotisations des personnes sans activité lucrative",
    "Caisse AVS Vaud — Prestations familiales 2026",
]
