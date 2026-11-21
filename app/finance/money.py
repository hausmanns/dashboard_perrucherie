"""Amounts and dates the Swiss way.

- Amounts travel as `Decimal` while parsing and are stored as integer centimes.
- Swiss formats: `1'234.50`, `1’234.50`, `1 234.50`, `-1234.50`, `1234.50-`
  (trailing minus, some bank exports), `(12.00)`, `CHF 12.50`, `12.–` / `12.-`.
  German/EU formats too: `1.234,50`.
- Tax bills round to 5 centimes (`floor_5ct`), cash to 5 centimes (`round_5ct`).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

_MINUS = "-−‒–—"  # hyphen, minus sign, dashes
_SPACES = "    "
_APOS = "'’ʼ`´"


class AmountError(ValueError):
    pass


def parse_amount(value, decimal_sep: str | None = None) -> Decimal:
    """Parse a human-written amount into a Decimal.

    `decimal_sep` ('.' or ',') forces the decimal separator when the caller
    knows the file's convention; otherwise it is guessed per value.
    """
    if value is None:
        raise AmountError("empty amount")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    s = str(value).strip()
    if not s:
        raise AmountError("empty amount")

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    # currency codes / symbols anywhere
    s = re.sub(r"(?i)\b(chf|eur|usd|gbp|fr\.?|sfr\.?)\b", "", s)
    s = s.replace("€", "").replace("$", "").replace("£", "").strip()
    # "12.–" / "12.-" = 12.00
    s = re.sub(r"[.,][" + _MINUS + r"]$", "", s)
    if s and s[-1] in _MINUS:
        negative, s = not negative, s[:-1].strip()
    if s and s[0] in _MINUS:
        negative, s = not negative, s[1:].strip()
    elif s.startswith("+"):
        s = s[1:].strip()
    for ch in _SPACES + _APOS:
        s = s.replace(ch, "")
    if not s:
        raise AmountError(f"no digits in {value!r}")

    has_dot, has_comma = "." in s, "," in s
    sep = decimal_sep
    if sep is None:
        if has_dot and has_comma:
            sep = "." if s.rfind(".") > s.rfind(",") else ","
        elif has_comma:
            # "12,50" decimal; "1,234" / "1,234,567" thousands
            sep = "," if not re.fullmatch(r"\d{1,3}(,\d{3})+", s) else None
        elif has_dot:
            sep = "." if not re.fullmatch(r"\d{1,3}(\.\d{3}){2,}", s) else None
    if sep == ",":
        s = s.replace(".", "").replace(",", ".")
    elif sep == ".":
        s = s.replace(",", "")
    else:
        s = s.replace(",", "").replace(".", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise AmountError(f"not an amount: {value!r}")
    return -d if negative else d


def to_cents(value) -> int:
    """Francs (Decimal/float/str) → integer centimes, half-up."""
    if value is None:
        raise AmountError("empty amount")
    d = value if isinstance(value, Decimal) else parse_amount(value)
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def opt_cents(value) -> int | None:
    return None if value is None or value == "" else to_cents(value)


def from_cents(cents: int | None) -> float | None:
    if cents is None:
        return None
    return float(Decimal(int(cents)) / 100)


def _cents_exact(x: float) -> Decimal:
    """Float → Decimal rounded to the centime first, so that binary noise
    (2684.3499999999995) never pushes a floor one step down."""
    return Decimal(repr(float(x))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def floor_5ct(x: float) -> float:
    """Round down to 5 centimes (federal tax: « ramené aux 5 ct. inférieurs »)."""
    d = _cents_exact(x) * 20
    return float(d.to_integral_value(rounding=ROUND_FLOOR) / 20)


def round_5ct(x: float) -> float:
    d = _cents_exact(x) * 20
    return float(d.to_integral_value(rounding=ROUND_HALF_UP) / 20)


def floor_to(x: float, step: int) -> float:
    """Drop the fractions below `step` francs (tax: « fractions inférieures à 100 fr. abandonnées »)."""
    if x <= 0:
        return 0.0
    return float((_cents_exact(x) // step) * step)


def round_chf(x: float) -> float:
    return float(_cents_exact(x))


# ---------------------------------------------------------------- dates

_DATE_FORMATS = (
    "%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y",
    "%Y/%m/%d", "%Y%m%d", "%d %m %Y",
)
_MONTHS_FR = {
    "janv": 1, "janvier": 1, "fevr": 2, "février": 2, "fevrier": 2, "févr": 2, "mars": 3, "avr": 4,
    "avril": 4, "mai": 5, "juin": 6, "juil": 7, "juillet": 7, "aout": 8, "août": 8, "sept": 9,
    "septembre": 9, "oct": 10, "octobre": 10, "nov": 11, "novembre": 11, "dec": 12, "déc": 12,
    "décembre": 12, "decembre": 12,
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "juni": 6, "juli": 7, "dezember": 12, "okt": 10, "dez": 12,
}


def parse_date(value) -> str:
    """Any common date notation → ISO `YYYY-MM-DD`. Day first (Swiss usage)."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value or "").strip()
    if not s:
        raise ValueError("empty date")
    # ISO datetime / with time part
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ]", s)
    if m:
        s = m.group(1)
    m = re.match(r"^(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+\d{1,2}:\d{2}", s)
    if m:
        s = m.group(1)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    # "3 nov. 2026", "Nov 3, 2026"
    m = re.match(r"^(\d{1,2})\.?\s+([A-Za-zÀ-ÿ]+)\.?\s+(\d{4})$", s)
    if m and m.group(2).lower() in _MONTHS_FR:
        return date(int(m.group(3)), _MONTHS_FR[m.group(2).lower()], int(m.group(1))).isoformat()
    m = re.match(r"^([A-Za-zÀ-ÿ]+)\.?\s+(\d{1,2}),?\s+(\d{4})$", s)
    if m and m.group(1).lower() in _MONTHS_FR:
        return date(int(m.group(3)), _MONTHS_FR[m.group(1).lower()], int(m.group(2))).isoformat()
    raise ValueError(f"unknown date format: {value!r}")


def year_fraction_left(today: date) -> float:
    """Share of the calendar year still to come after `today` (0 < f ≤ 1)."""
    start = date(today.year, 1, 1)
    end = date(today.year + 1, 1, 1)
    return (end - today).days / (end - start).days
