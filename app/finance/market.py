"""Online market data, cached in the finance DB: prices, exchange rates, Swiss CPI.

Only tickers and currency codes ever leave the house — never an amount.
Every fetch is best-effort: when offline (or `FINANCE_OFFLINE=1`, used by the
tests) the last cached value is used, then a conservative built-in default
(flagged `approx`), so the dashboard never breaks for lack of internet.

- Prices: Yahoo Finance chart API (symbols like `VT`, `CHSPI.SW`, `VWRL.SW`,
  `BTC-USD`), Stooq as a fallback.
- FX: ECB reference rates through frankfurter.app (CHF per unit of currency).
- CPI: SNB data portal, Swiss consumer prices (year-on-year %).
"""
from __future__ import annotations

import csv
import io
import logging
import os
from datetime import date, datetime, timedelta

import httpx

from . import db

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (household-dashboard)"}
FALLBACK_FX = {"CHF": 1.0, "EUR": 0.93, "USD": 0.80, "GBP": 1.06, "JPY": 0.0053, "CAD": 0.58, "SEK": 0.083}


def offline() -> bool:
    return os.environ.get("FINANCE_OFFLINE") == "1"


# --------------------------------------------------------------------------- FX

def fx_to_chf(currency: str, on: str | None = None, conn=None) -> tuple[float, bool]:
    """(CHF per unit, approximate?) for `currency` at date `on` (latest known on or before)."""
    cur = (currency or "CHF").upper()
    if cur == "CHF":
        return 1.0, False
    own = conn is None
    if own:
        ctx = db.get_conn()
        conn = ctx.__enter__()
    try:
        row = conn.execute(
            "SELECT chf_per_unit FROM fx_rates WHERE currency = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (cur, on or "9999-12-31"),
        ).fetchone()
        if not row:
            row = conn.execute("SELECT chf_per_unit FROM fx_rates WHERE currency = ? ORDER BY date LIMIT 1",
                               (cur,)).fetchone()
    finally:
        if own:
            ctx.__exit__(None, None, None)
    if row:
        return float(row["chf_per_unit"]), False
    return FALLBACK_FX.get(cur, 1.0), True


def refresh_fx(currencies: list[str]) -> dict:
    curs = sorted({c.upper() for c in currencies if c and c.upper() != "CHF"})
    if not curs or offline():
        return {"updated": 0, "skipped": "offline" if offline() else "nothing"}
    try:
        r = httpx.get("https://api.frankfurter.app/latest", params={"from": "CHF", "to": ",".join(curs)},
                      timeout=15, headers=UA)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001 — best effort
        log.warning("FX refresh failed: %s", e)
        return {"updated": 0, "error": str(e)}
    day = data.get("date") or date.today().isoformat()
    n = 0
    with db.get_conn() as conn:
        for cur, per_chf in (data.get("rates") or {}).items():
            if per_chf:
                conn.execute("INSERT OR REPLACE INTO fx_rates (currency, date, chf_per_unit) VALUES (?, ?, ?)",
                             (cur, day, 1.0 / float(per_chf)))
                n += 1
    return {"updated": n, "date": day}


# --------------------------------------------------------------------------- prices

def _yahoo(symbol: str) -> tuple[str, float, str] | None:
    r = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                  params={"range": "5d", "interval": "1d"}, headers=UA, timeout=15)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    meta = res["meta"]
    price = meta.get("regularMarketPrice")
    ts = meta.get("regularMarketTime")
    if price is None:
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        if not closes:
            return None
        price = closes[-1]
    day = datetime.fromtimestamp(ts).date().isoformat() if ts else date.today().isoformat()
    cur = (meta.get("currency") or "").upper()
    if cur == "GBP" and meta.get("currency") == "GBp":   # pence
        price /= 100
    return day, float(price), cur


def _stooq(symbol: str) -> tuple[str, float, str] | None:
    s = symbol.lower()
    if "." not in s:
        s += ".us"
    r = httpx.get("https://stooq.com/q/l/", params={"s": s, "f": "sd2t2ohlcv", "h": "", "e": "csv"},
                  headers=UA, timeout=15)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    if not rows or rows[0].get("Close") in (None, "", "N/D"):
        return None
    return rows[0]["Date"], float(rows[0]["Close"]), ""


def refresh_prices(symbols: list[str]) -> dict:
    syms = sorted({s.strip() for s in symbols if s and s.strip()})
    if not syms or offline():
        return {"updated": 0, "skipped": "offline" if offline() else "nothing", "errors": {}}
    done, errors = 0, {}
    for sym in syms:
        got = None
        for fn in (_yahoo, _stooq):
            try:
                got = fn(sym)
                if got:
                    break
            except Exception as e:  # noqa: BLE001
                errors[sym] = f"{fn.__name__}: {e}"
        if not got:
            continue
        day, price, cur = got
        with db.get_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO prices (symbol, date, close, currency) VALUES (?, ?, ?, ?)",
                         (sym, day, price, cur))
        errors.pop(sym, None)
        done += 1
    return {"updated": done, "errors": errors}


def last_price(symbol: str, conn) -> tuple[float, str, str] | None:
    row = conn.execute("SELECT close, date, currency FROM prices WHERE symbol = ? ORDER BY date DESC LIMIT 1",
                       (symbol,)).fetchone()
    return (float(row["close"]), row["date"], row["currency"]) if row else None


# --------------------------------------------------------------------------- CPI

def refresh_cpi() -> dict:
    """Swiss CPI year-on-year inflation from the SNB data portal (cube plkopr)."""
    if offline():
        return {"updated": 0, "skipped": "offline"}
    try:
        r = httpx.get("https://data.snb.ch/api/cube/plkopr/data/csv/en", headers=UA, timeout=20)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return {"updated": 0, "error": str(e)}
    n = 0
    lines = [l for l in r.text.splitlines() if l and not l.startswith('"CubeId') and ";" in l]
    reader = csv.reader(lines, delimiter=";")
    header = None
    with db.get_conn() as conn:
        for row in reader:
            row = [c.strip('"') for c in row]
            if header is None:
                header = row
                continue
            rec = dict(zip(header, row))
            month = rec.get("Date", "")
            dim = rec.get("D0", "")
            val = rec.get("Value", "")
            # D0 = VVP: change from previous year's month (in %) — the headline figure
            if dim not in ("VVP", "LIK_VVP") or not val or len(month) != 7:
                continue
            try:
                conn.execute("INSERT OR REPLACE INTO cpi (month, yoy_pct) VALUES (?, ?)", (month, float(val)))
                n += 1
            except ValueError:
                continue
    return {"updated": n}


def latest_cpi() -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT month, yoy_pct FROM cpi ORDER BY month DESC LIMIT 1").fetchone()
        avg = conn.execute(
            "SELECT AVG(yoy_pct) a, COUNT(*) n FROM (SELECT yoy_pct FROM cpi ORDER BY month DESC LIMIT 120)"
        ).fetchone()
    if not row:
        return None
    return {"month": row["month"], "yoy_pct": row["yoy_pct"], "avg_10y_pct": avg["a"], "months": avg["n"]}


def refresh_all() -> dict:
    """Daily job: prices of every holding, FX of every foreign currency, CPI."""
    settings = db.get_settings()
    online = settings.get("online", {})
    with db.get_conn() as conn:
        syms = [r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM holdings WHERE symbol != ''")]
        curs = [r["c"] for r in conn.execute(
            "SELECT DISTINCT currency c FROM accounts UNION SELECT DISTINCT currency FROM holdings")]
    out = {}
    if online.get("prices", True):
        out["prices"] = refresh_prices(syms)
    if online.get("fx", True):
        out["fx"] = refresh_fx(curs + ["EUR", "USD"])
    if online.get("cpi", True):
        out["cpi"] = refresh_cpi()
    from . import metrics  # noqa: WPS433 — avoid an import cycle
    metrics.write_auto_snapshots()
    return out


def stale(conn, days: int = 1) -> bool:
    row = conn.execute("SELECT MAX(date) d FROM prices").fetchone()
    if not row or not row["d"]:
        return True
    return row["d"] < (date.today() - timedelta(days=days)).isoformat()
