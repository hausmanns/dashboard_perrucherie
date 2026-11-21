"""AVS / LPP / 3a against the 2026 official figures."""
import pytest

from app.finance import params as P
from app.finance import pension as X

p = P.get(2026)


@pytest.mark.parametrize("ramd,expected", [
    (0, 1260), (15_120, 1260), (45_360, 1915.2), (60_000, 1310.4 + 8 / 600 * 60_000),
    (90_720, 2520), (200_000, 2520),
])
def test_avs_full_scale_formula(ramd, expected):
    assert X.avs_full_monthly(ramd, p) == pytest.approx(expected)


def test_avs_formula_is_continuous_at_36_times_minimum():
    lo = 0.74 * 1260 + 13 / 600 * 45_360
    hi = 1.04 * 1260 + 8 / 600 * 45_360
    assert lo == pytest.approx(hi)


def test_avs_partial_pension():
    assert X.avs_monthly(90_720, 44, p) == 2520
    assert X.avs_monthly(90_720, 22, p) == 1260
    assert X.avs_monthly(90_720, 50, p) == 2520   # capped at 44 years
    assert X.avs_monthly(90_720, 0, p) == 0


@pytest.mark.parametrize("base,contrib", [
    (0, 530), (349_999, 530), (350_000, 636), (400_000, 742), (1_000_000, 2_014), (1_049_999, 2_014),
    (1_050_000, 2_120), (1_700_000, 3_498), (1_750_000, 3_604), (1_800_000, 3_763), (1_850_000, 3_922),
    (8_900_000, 26_341), (8_950_000, 26_500), (50_000_000, 26_500),
])
def test_nonactive_contribution_table(base, contrib):
    """Mémento 2.03 (2026), chapter 8."""
    assert X.nonactive_contribution(base, p) == contrib


def test_nonactive_memento_example_married():
    # Example 19: wealth 500'000 + pension 75'000 × 20 = 2'000'000 → half each = 1'000'000 → 2'014
    assert X.nonactive_contribution((500_000 + 75_000 * 20) / 2, p) == 2_014


def test_credited_income():
    assert X.credited_income_nonactive(530, p) == pytest.approx(5_000)


def test_record_with_education_credits():
    rec = X.AvsRecord()
    for y in range(2000, 2044):
        rec.add(y, 60_000)
    for y in range(2026, 2042):        # 16 years with a child, credit split 50/50
        rec.education_years[y] = 0.5
    r = X.avs_pension_from_record(rec, p)
    edu = 3 * 1260 * 12
    assert r["ramd"] == pytest.approx(60_000 + 16 * 0.5 * edu / 44)
    assert r["annual"] == pytest.approx(r["monthly"] * 13)


def test_early_and_late():
    assert X.early_late_factor(65, p) == 1
    assert X.early_late_factor(63, p) == pytest.approx(1 - 2 * 0.068)
    assert X.early_late_factor(60, p) == pytest.approx(1 - 2 * 0.068)  # max 2 years early
    assert X.early_late_factor(70, p) == pytest.approx(1.315)


def test_couple_cap():
    a, b = X.couple_cap(2520, 2520, p)
    assert a + b == pytest.approx(3780)
    assert a == pytest.approx(b)
    assert X.couple_cap(1500, 1500, p) == (1500, 1500)


def test_lpp_legal_credits():
    # 100'000 gross: coordinated 90'720 - 26'460 = 64'260; age 40 → 10 %
    assert X.lpp_coordinated_salary(100_000, p) == 64_260
    assert X.lpp_legal_credits(100_000, 40, p) == pytest.approx(6_426)
    assert X.lpp_coordinated_salary(22_679, p) == 0
    assert X.lpp_coordinated_salary(25_000, p) == 3_780       # minimum coordinated salary
    assert X.lpp_legal_credits(100_000, 24, p) == 0            # savings start at 25


def test_pillar3a_max():
    assert X.pillar3a_max(80_000, True, p) == 7_258
    assert X.pillar3a_max(80_000, False, p) == 16_000
    assert X.pillar3a_max(500_000, False, p) == 36_288
    assert X.pillar3a_max(0, True, p) == 0


def test_real_rate_and_projection():
    assert X.real_rate(2.0, 1.0) == pytest.approx(1.02 / 1.01 - 1)
    assert X.project_lpp(100_000, 10_000, 2, 0.0) == 120_000
    assert X.project_lpp(100_000, 0, 1, 0.01) == pytest.approx(101_000)
