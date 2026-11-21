"""Tax engine against the official 2026 tables.

IFD vectors: every row of AFC form 58c 2026 (« Tableau servant à calculer
l'impôt fédéral direct »), both columns — the table lists the unrounded
amounts; the tax due is then rounded down to 5 centimes.
Vaud vectors: the « barèmes sommaires de l'impôt cantonal de base 2026 » of the
ACI deductions table (income and wealth), and hand-checked full cases.
"""
import pytest

from app.finance import params as P
from app.finance import tax as T
from app.finance.money import floor_5ct

p = P.get(2026)

# (taxable income, single, married/parental) — AFC form 58c 2026
IFD_TABLE = [
    (18500, 25.41, None), (19000, 29.26, None), (20000, 36.96, None), (21000, 44.66, None),
    (22000, 52.36, None), (23000, 60.06, None), (24000, 67.76, None), (25000, 75.46, None),
    (26000, 83.16, None), (27000, 90.86, None), (28000, 98.56, None), (29000, 106.26, None),
    (30000, 113.96, None), (31000, 121.66, None), (32000, 129.36, None),
    (33000, 137.06, 33.00), (33100, 137.83, 34.00), (33200, 138.60, 35.00), (34000, 145.64, 43.00),
    (35000, 154.44, 53.00), (36000, 163.24, 63.00), (37000, 172.04, 73.00), (38000, 180.84, 83.00),
    (39000, 189.64, 93.00), (40000, 198.44, 103.00), (41000, 207.24, 113.00), (42000, 216.04, 123.00),
    (43400, 228.36, 137.00), (43500, 229.20, 138.00), (44000, 242.40, 143.00), (45000, 268.80, 153.00),
    (46000, 295.20, 163.00), (47000, 321.60, 173.00), (48000, 348.00, 183.00), (49000, 374.40, 193.00),
    (50000, 400.80, 203.00), (51000, 427.20, 213.00), (53300, 487.92, 236.00), (53400, 490.56, 237.00),
    (54000, 506.40, 249.00), (55000, 532.80, 269.00), (56000, 559.20, 289.00), (57000, 585.60, 309.00),
    (57900, 609.36, 327.00), (58000, 612.00, 329.00), (59000, 641.70, 349.00), (60000, 671.40, 369.00),
    (61200, 707.04, 393.00), (61300, 710.01, 395.00), (65000, 819.90, 506.00), (70000, 968.40, 656.00),
    (75000, 1116.90, 806.00), (76100, 1149.57, 839.00), (76200, 1152.50, 842.00), (77500, 1229.72, 881.00),
    (79000, 1318.82, 926.00), (79100, 1324.76, 929.00), (82000, 1497.02, 1045.00),
    (82100, 1502.95, 1049.00), (85000, 1694.35, 1165.00), (90000, 2024.35, 1365.00),
    (94800, 2341.15, 1557.00), (94900, 2347.75, 1561.00), (100000, 2684.35, 1816.00),
    (105000, 3014.35, 2066.00), (108600, 3251.95, 2246.00), (108700, 3258.55, 2251.00),
    (108800, 3265.15, 2257.00), (108900, 3271.75, 2263.00), (110000, 3368.55, 2329.00),
    (115000, 3808.55, 2629.00), (120500, 4292.55, 2959.00), (120600, 4301.35, 2965.00),
    (125000, 4688.55, 3273.00), (130000, 5128.55, 3623.00), (130400, 5163.75, 3651.00),
    (130500, 5172.55, 3658.00), (135000, 5568.55, 4018.00), (138300, 5858.95, 4282.00),
    (138400, 5867.75, 4290.00), (141400, 6131.75, 4560.00), (141500, 6140.55, 4569.00),
    (144200, 6437.55, 4812.00), (144300, 6448.55, 4821.00), (148200, 6877.55, 5211.00),
    (148300, 6888.55, 5221.00), (150300, 7108.55, 5441.00), (150400, 7119.55, 5452.00),
    (151000, 7185.55, 5524.00), (152300, 7328.55, 5680.00), (152400, 7339.55, 5692.00),
    (155000, 7625.55, 6030.00), (160000, 8175.55, 6680.00), (170000, 9275.55, 7980.00),
    (185000, 10925.55, 9930.00), (185100, 10936.55, 9943.00), (186000, 11055.35, 10060.00),
    (190000, 11583.35, 10580.00), (200000, 12903.35, 11880.00), (250000, 19503.35, 18380.00),
    (300000, 26103.35, 24880.00), (350000, 32703.35, 31380.00), (400000, 39303.35, 37880.00),
    (500000, 52503.35, 50880.00), (650000, 72303.35, 70380.00), (700000, 78903.35, 76880.00),
    (793800, 91284.95, 89074.00), (793900, 91298.15, 89087.00), (800000, 92000.00, 89880.00),
    (941200, 108238.00, 108236.00), (941300, 108249.50, 108249.00), (950000, 109250.00, 109250.00),
]


def _ifd_raw(scale, income):
    t = p["ifd"]
    return min(T.tariff(t[scale], income), t["flat_rate"] * income)


@pytest.mark.parametrize("income,single,married", IFD_TABLE)
def test_ifd_scale_matches_official_table(income, single, married):
    assert _ifd_raw("single", income) == pytest.approx(single, abs=0.006)
    if married is not None:
        assert _ifd_raw("married", income) == pytest.approx(married, abs=0.006)


@pytest.mark.parametrize("income,single,married", IFD_TABLE)
def test_ifd_tax_due_is_rounded_down_to_5_ct(income, single, married):
    assert T.ifd_income_tax(income, scale="single", children_for_reduction=0, p=p) == floor_5ct(single)


def test_ifd_below_first_bracket_and_minimum():
    assert T.ifd_income_tax(18400, scale="single", children_for_reduction=0, p=p) == 0
    # married: 32'100 → 24 fr. < 25 fr. minimum → not levied; 32'200 → 25 fr.
    assert T.ifd_income_tax(32100, scale="married", children_for_reduction=0, p=p) == 0
    assert T.ifd_income_tax(32200, scale="married", children_for_reduction=0, p=p) == 25


def test_ifd_child_reduction_and_floor_at_zero():
    # married 100'000 → 1'816; two children → -526
    assert T.ifd_income_tax(100_000, scale="married", children_for_reduction=2, p=p) == 1816 - 526
    assert T.ifd_income_tax(40_000, scale="married", children_for_reduction=1, p=p) == 0  # 103 - 263 < 0
    # half a child (unmarried parents splitting): -131.50
    assert T.ifd_income_tax(100_000, scale="married", children_for_reduction=0.5, p=p) == 1816 - 131.5


def test_ifd_fractions_below_100_dropped():
    assert T.ifd_income_tax(100_099, scale="single", children_for_reduction=0, p=p) == \
        T.ifd_income_tax(100_000, scale="single", children_for_reduction=0, p=p)


def test_ifd_is_monotonic():
    prev = -1
    for income in range(0, 1_200_000, 700):
        for scale in ("single", "married"):
            pass
        t = T.ifd_income_tax(income, scale="single", children_for_reduction=0, p=p)
        assert t >= prev
        prev = t


# ------------------------------------------------------------------ Vaud

# (taxable, base tax) at each bracket start — ACI barème sommaire 2026
VD_INCOME_TABLE = [
    (100, 1.00), (1600, 16.00), (3400, 52.00), (5100, 103.00), (8300, 231.00), (11900, 411.00),
    (15100, 603.00), (23600, 1198.00), (40500, 2550.00), (57200, 4053.00), (74400, 5773.00),
    (91200, 7621.00), (108100, 9649.00), (135000, 13011.50), (162000, 16521.50), (192500, 20639.00),
    (223000, 24909.00), (256000, 29694.00), (291700, 35049.00),
]


@pytest.mark.parametrize("income,base", VD_INCOME_TABLE)
def test_vd_income_scale(income, base):
    assert T.tariff(p["vd"]["income"], income) == pytest.approx(base, abs=0.001)


def test_vd_income_scale_is_continuous():
    """Each bracket's base equals the previous bracket carried to its start (law: art. 47 al. 2)."""
    br = p["vd"]["income"]
    for (s0, b0, r0), (s1, b1, _) in zip(br, br[1:]):
        assert b0 + (s1 - s0) / 100 * r0 == pytest.approx(b1, abs=0.01)


# (wealth, base) — ACI barème sommaire 2026 (impôt sur la fortune)
VD_WEALTH_TABLE = [(60_000, 32.65), (95_000, 66.60), (120_000, 108.85), (177_000, 205.20),
                   (355_000, 635.95), (711_000, 1757.35)]


@pytest.mark.parametrize("wealth,base", VD_WEALTH_TABLE)
def test_vd_wealth_scale(wealth, base):
    assert T.tariff(p["vd"]["wealth"], wealth, per=1000) == pytest.approx(base, abs=0.03)


def test_vd_wealth_tax_exemption_and_coefficients():
    assert T.vd_wealth_tax(59_999, married=False, communal_pct=78.5, p=p)["total"] == 0
    assert T.vd_wealth_tax(119_000, married=True, communal_pct=78.5, p=p)["total"] == 0
    w = T.vd_wealth_tax(500_400, married=False, communal_pct=78.5, p=p)
    assert w["taxable"] == 500_000
    base = 635.95 + 3.15 * 145
    assert w["base"] == pytest.approx(base)
    assert w["cantonal"] == pytest.approx(round(base * 1.55 * 20) / 20)
    assert w["communal"] == pytest.approx(round(base * 0.785 * 20) / 20)
    # from 2027 the cantonal part is 12 % lower
    w27 = T.vd_wealth_tax(500_000, married=False, communal_pct=78.5, p=P.get(2027))
    assert w27["cantonal"] == pytest.approx(round(base * 1.55 * 0.88 * 20) / 20)


def _single_tp(taxable_target_salary=0.0, **kw):
    return T.Taxpayer(status="single", earners=[T.Earner(**kw)], communal_coefficient=78.5, year=2026)


def test_vd_quotient_married():
    # 100'000 taxable, married, no child: rate income = 55'500 (55'555 → hundreds dropped)
    rate_income = T.vd_rate_income(100_000, 1.8, 0.0, 0, p)
    assert rate_income == 55_500
    base = T.vd_base_from_rate(100_000, rate_income, p)
    assert base == pytest.approx(100_000 * (2550 + 9 * 150) / 55_500)


def test_vd_quotient_children_cap():
    # small income: no cap
    det = T.vd_rate_income(100_000, 1.0, 0.5, 1, p)
    assert det == T.floor_to(100_000 / 1.5, 100)
    # very high income: reduction capped at the one obtained at 214'200
    det_hi = T.vd_rate_income(1_000_000, 1.0, 0.5, 1, p)
    cap = 214_200 / 1.0 - 214_200 / 1.5
    assert det_hi == T.floor_to(1_000_000 - cap, 100)


def test_capital_tax_100k_single_lausanne():
    c = T.capital_tax(100_000, married=False, year=2026, communal_pct=78.5)
    assert c["ifd"] == floor_5ct(2684.35 / 5)                     # 536.85
    vd_base = 100_000 * (7621 + 12 * 88) / 100_000 / 5           # rate of 100'000 / 5
    assert c["cantonal"] == pytest.approx(round(vd_base * 1.55 * 20) / 20)   # no yearly reduction
    assert c["communal"] == pytest.approx(round(vd_base * 0.785 * 20) / 20)
    assert 4.4 < c["rate_pct"] < 4.8


def test_capital_tax_married_uses_quotient_and_is_cheaper():
    single = T.capital_tax(300_000, married=False, year=2026, communal_pct=78.5)["total"]
    married = T.capital_tax(300_000, married=True, year=2026, communal_pct=78.5)["total"]
    assert married < single


def test_capital_tax_staggering_saves_tax():
    """Splitting 3a into three withdrawals (three tax years) costs less than one."""
    once = T.capital_tax(210_000, married=False, year=2026, communal_pct=78.5)["total"]
    split = 3 * T.capital_tax(70_000, married=False, year=2026, communal_pct=78.5)["total"]
    assert split < once


def test_social_contributions():
    s = T.social_contributions(100_000, p, aanp_rate=0.01, lpp_employee=4_000)
    assert s["avs"] == pytest.approx(5_300)
    assert s["ac"] == pytest.approx(1_100)
    assert s["aanp"] == pytest.approx(1_000)
    assert s["total"] == pytest.approx(11_400)
    hi = T.social_contributions(200_000, p, aanp_rate=0.01)
    assert hi["ac"] == pytest.approx(1_630.20)   # capped at 148'200
    assert hi["aanp"] == pytest.approx(1_482)


def test_full_single_salaried_case():
    """Single, 100'000 gross, Lausanne. Hand-computed from the 2026 rules."""
    gross = 100_000
    soc = T.social_contributions(gross, p, aanp_rate=0.01)
    lpp = 5_000
    e = T.Earner(salary_gross=gross, social=soc["total"], lpp_employee=lpp, pillar3a=7_258,
                 insurance=6_000, commute=1_000, meals_out=True)
    tp = T.Taxpayer(status="single", earners=[e], communal_coefficient=78.5, year=2026)
    r = T.compute(tp)
    net_salary = gross - soc["total"] - lpp                      # 87'600
    prof = 4_000 + 3_200 + 1_000                                 # forfait capped 4'000 (3 % = 2'628 → min 2'000? no: 2'628)
    prof = min(4_000, max(2_000, 0.03 * net_salary)) + 3_200 + 1_000
    base_net = gross - soc["total"] - lpp - 7_258 - prof
    ifd_taxable = T.floor_to(base_net - 1_800, 100)
    assert r["ifd"]["taxable"] == ifd_taxable
    assert r["ifd"]["tax"] == T.ifd_income_tax(ifd_taxable, scale="single", children_for_reduction=0, p=p)
    vd_taxable = T.floor_to(base_net - 5_000, 100)               # insurance cap 5'000, no modest deduction
    assert r["vd"]["taxable"] == vd_taxable
    base = T.tariff(p["vd"]["income"], vd_taxable)
    assert r["vd"]["base_tax"] == pytest.approx(base)
    assert r["vd"]["cantonal"] == pytest.approx(round(base * 1.55 * 0.95 * 20) / 20)
    assert r["vd"]["communal"] == pytest.approx(round(base * 0.785 * 20) / 20)
    assert 10 < r["effective_rate_pct"] < 16


def test_modest_income_deduction():
    # 20'000 net income single: deduction 17'000 reduced by 100 per started 200 above 17'099
    e = T.Earner(other=20_000 + 5_000, insurance=5_000)   # other income, no professional expenses
    tp = T.Taxpayer(status="single", earners=[e], year=2026)
    r = T.vd_taxable(tp, p)
    steps = -(-(20_000 - 17_099) // 200)       # ceil
    assert dict(r["deductions"])["Contribuable modeste"] == 17_000 - 100 * steps
    # well above: no deduction left
    e2 = T.Earner(other=80_000, insurance=5_000)
    r2 = T.vd_taxable(T.Taxpayer(status="single", earners=[e2], year=2026), p)
    assert "Contribuable modeste" not in dict(r2["deductions"])


def test_family_deduction_only_married_or_single_parent():
    child = [T.Child(age=2, share=1.0)]
    e = T.Earner(other=90_000, insurance=0)
    cohab = T.vd_taxable(T.Taxpayer(status="single", earners=[e], children=child), p)
    assert "Famille" not in dict(cohab["deductions"])
    alone = T.vd_taxable(T.Taxpayer(status="single", earners=[e], children=child, single_parent=True), p)
    assert dict(alone["deductions"])["Famille"] == 2_800 + 1_000
    married = T.vd_taxable(T.Taxpayer(status="married", earners=[e, T.Earner(insurance=0)], children=child), p)
    assert dict(married["deductions"])["Famille"] == 1_300 + 1_000


def test_family_deduction_phase_out():
    e = T.Earner(other=136_900, insurance=0)
    tp = T.Taxpayer(status="married", earners=[e, T.Earner(insurance=0)])
    r = T.vd_taxable(tp, p)
    # 10'500 above 126'400 → 5 full tranches of 2'100 → -500
    assert dict(r["deductions"])["Famille"] == 1_300 - 500


def test_unmarried_parents_split_child():
    """Each parent gets half the child: 0.25 part (Vaud), 3'400 deduction (IFD)."""
    child = T.Child(age=3, share=0.5)
    e = T.Earner(salary_gross=90_000, social=6_000, lpp_employee=4_000, insurance=5_000, meals_out=False)
    tp = T.Taxpayer(status="single", earners=[e], children=[child], ifd_parental=False)
    base, parts_child = T.vd_parts(tp, p)
    assert (base, parts_child) == (1.0, 0.25)
    ifd = T.ifd_taxable(tp, p)
    assert dict(ifd["deductions"])["Enfants"] == 3_400
    assert dict(ifd["deductions"])["Primes d'assurance"] == 1_800 + 350
    # the higher earner gets the parental scale: married scale + 131.50 off
    hi = T.compute(T.Taxpayer(status="single", earners=[e], children=[child], ifd_parental=True))
    lo = T.compute(tp)
    assert hi["ifd"]["scale"] == "married" and lo["ifd"]["scale"] == "single"
    assert hi["ifd"]["tax"] < lo["ifd"]["tax"]


def test_marriage_two_earner_deduction():
    a = T.Earner(salary_gross=100_000, social=7_400, lpp_employee=5_000, insurance=5_000)
    b = T.Earner(salary_gross=60_000, social=4_440, lpp_employee=2_500, insurance=5_000)
    r = T.ifd_taxable(T.Taxpayer(status="married", earners=[a, b]), p)
    d = dict(r["deductions"])
    low = 60_000 - 4_440 - 2_500 - (min(4000, max(2000, 0.03 * (60_000 - 4_440 - 2_500))) + 3_200)
    assert d["Double revenu"] == pytest.approx(min(14_100, max(8_600, low / 2)))
    assert d["Couple marié"] == 2_800
    assert d["Primes d'assurance"] == 3_700


def test_marginal_rate_reasonable():
    e = T.Earner(salary_gross=120_000, social=8_900, lpp_employee=6_000, insurance=6_000)
    m = T.marginal_rate(T.Taxpayer(status="single", earners=[e]))
    assert 0.25 < m < 0.40
