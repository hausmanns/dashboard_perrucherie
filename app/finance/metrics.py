"""What the household has and how money moves: net worth, history, cash flow.

Scopes: `household` (everything) or `member:<id>` (each account counted for
that adult's ownership share; a joint account with `{}` owners is split
equally between the adults).

Account value
-------------
- `manual` accounts: the latest snapshot.
- `holdings` accounts: Σ quantity × latest price (online, else the manual
  price) converted to CHF, + the cash part (`meta.cash`). `write_auto_snapshots`
  stores that total once a day, so the history fills itself.
Debts (mortgage, loan) are stored as positive amounts owed and subtracted.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from . import db, market
from .categorize import CATEGORIES, EXCLUDED_FROM_LIVING
from .money import from_cents, to_cents

ACCOUNT_KINDS: dict[str, dict] = {
    "current": {"label": "Compte courant", "group": "liquid", "asset": "cash", "emoji": "💳"},
    "savings": {"label": "Compte épargne", "group": "liquid", "asset": "cash", "emoji": "🐷"},
    "invest": {"label": "Titres / courtage", "group": "liquid", "asset": "equity", "emoji": "📈"},
    "crypto": {"label": "Crypto", "group": "liquid", "asset": "equity", "emoji": "🪙"},
    "cash": {"label": "Espèces", "group": "liquid", "asset": "cash", "emoji": "💵"},
    "pillar3a": {"label": "3e pilier A", "group": "pension", "asset": "equity", "emoji": "🏔️"},
    "lpp": {"label": "2e pilier (LPP)", "group": "pension", "asset": "cash", "emoji": "🏢"},
    "vested": {"label": "Libre passage", "group": "pension", "asset": "equity", "emoji": "🧳"},
    "property": {"label": "Immobilier", "group": "property", "asset": "real_estate", "emoji": "🏠"},
    "mortgage": {"label": "Hypothèque", "group": "debt", "asset": "debt", "emoji": "🏦"},
    "loan": {"label": "Crédit / dette", "group": "debt", "asset": "debt", "emoji": "🧾"},
    "other": {"label": "Autre bien", "group": "other", "asset": "other", "emoji": "📦"},
}
GROUPS = ("liquid", "pension", "property", "other", "debt")


# --------------------------------------------------------------------------- scope

def adults(conn) -> list[dict]:
    return db.rows(conn.execute("SELECT * FROM members WHERE kind = 'adult' ORDER BY id"))


def parse_scope(scope: str | None) -> int | None:
    """None for the household, else the member id."""
    if not scope or scope == "household":
        return None
    if scope.startswith("member:"):
        return int(scope.split(":", 1)[1])
    return int(scope)


def owner_shares(acc: dict, adult_ids: list[int]) -> dict[int, float]:
    owners = db.loads(acc.get("owners"), {})
    shares = {int(k): float(v) for k, v in owners.items() if float(v) > 0}
    if not shares:
        return {i: 1 / len(adult_ids) for i in adult_ids} if adult_ids else {}
    tot = sum(shares.values())
    return {k: v / tot for k, v in shares.items()}


def scope_share(acc: dict, member_id: int | None, adult_ids: list[int]) -> float:
    if member_id is None:
        return 1.0
    return owner_shares(acc, adult_ids).get(member_id, 0.0)


# --------------------------------------------------------------------------- values

def holdings_value(conn, account_id: int) -> tuple[float, bool, list[dict]]:
    """(CHF value, approximate?, positions)."""
    total, approx, out = 0.0, False, []
    for h in db.rows(conn.execute("SELECT * FROM holdings WHERE account_id = ?", (account_id,))):
        price, pdate, pcur = None, None, h["currency"]
        if h["symbol"]:
            lp = market.last_price(h["symbol"], conn)
            if lp:
                price, pdate, pcur = lp[0], lp[1], (lp[2] or h["currency"])
        if price is None:
            price = h["manual_price"]
        if price is None:
            approx = True
            price = 0.0
        fx, fx_approx = market.fx_to_chf(pcur or h["currency"], None, conn)
        approx |= fx_approx
        value = h["quantity"] * price
        out.append({**h, "price": price, "price_date": pdate, "value": value, "value_chf": value * fx,
                    "cost_basis": from_cents(h["cost_basis_cents"])})
        total += value * fx
    return total, approx, out


def account_value(conn, acc: dict, on: str | None = None) -> dict:
    """Value of one account (account currency + CHF) at date `on` (default: latest)."""
    meta = db.loads(acc.get("meta"), {})
    if acc["valuation"] == "holdings" and on is None:
        val_chf, approx, _ = holdings_value(conn, acc["id"])
        cash = float(meta.get("cash", 0) or 0)
        fx, fx_approx = market.fx_to_chf(acc["currency"], None, conn)
        return {"value": None, "value_chf": val_chf + cash * fx, "date": date.today().isoformat(),
                "source": "holdings", "approx": approx or fx_approx}
    row = conn.execute(
        "SELECT date, balance_cents, source FROM snapshots WHERE account_id = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 1", (acc["id"], on or "9999-12-31")).fetchone()
    if not row:
        return {"value": 0.0, "value_chf": 0.0, "date": None, "source": None, "approx": False}
    value = from_cents(row["balance_cents"])
    # holdings accounts snapshot their CHF total (write_auto_snapshots)
    fx, approx = (1.0, False) if acc["valuation"] == "holdings" else market.fx_to_chf(acc["currency"], row["date"], conn)
    return {"value": value, "value_chf": value * fx, "date": row["date"], "source": row["source"], "approx": approx}


def signed(kind: str, value_chf: float) -> float:
    return -value_chf if ACCOUNT_KINDS.get(kind, {}).get("group") == "debt" else value_chf


def list_accounts(include_archived: bool = False) -> list[dict]:
    with db.get_conn() as conn:
        ads = [a["id"] for a in adults(conn)]
        q = "SELECT * FROM accounts" + ("" if include_archived else " WHERE archived = 0") + " ORDER BY kind, name"
        out = []
        for acc in db.rows(conn.execute(q)):
            v = account_value(conn, acc)
            kind = ACCOUNT_KINDS.get(acc["kind"], ACCOUNT_KINDS["other"])
            last = v["date"]
            stale_days = (date.today() - date.fromisoformat(last)).days if last else None
            out.append({
                **acc, "owners": db.loads(acc["owners"], {}), "meta": db.loads(acc["meta"], {}),
                "shares": owner_shares(acc, ads), "group": kind["group"], "kind_label": kind["label"],
                "emoji": kind["emoji"], "value": v["value"], "value_chf": v["value_chf"],
                "signed_chf": signed(acc["kind"], v["value_chf"]), "last_date": last,
                "stale_days": stale_days, "value_source": v["source"], "approx": v["approx"],
            })
    return out


def net_worth(scope: str | None = None) -> dict:
    member = parse_scope(scope)
    with db.get_conn() as conn:
        ads = [a["id"] for a in adults(conn)]
    groups = {g: 0.0 for g in GROUPS}
    approx = False
    by_kind: dict[str, float] = defaultdict(float)
    accounts = []
    for acc in list_accounts():
        share = 1.0 if member is None else acc["shares"].get(member, 0.0)
        if share <= 0:
            continue
        v = acc["signed_chf"] * share
        groups[acc["group"]] += v
        by_kind[acc["kind"]] += v
        approx |= acc["approx"]
        accounts.append({"id": acc["id"], "name": acc["name"], "kind": acc["kind"], "group": acc["group"],
                         "value_chf": v, "stale_days": acc["stale_days"]})
    total = sum(groups.values())
    return {"scope": scope or "household", "total": total, "groups": groups, "by_kind": dict(by_kind),
            "liquid": groups["liquid"], "pension": groups["pension"],
            "property_net": groups["property"] + groups["debt"] + groups["other"],
            "approx": approx, "accounts": accounts}


def _month_ends(months: int) -> list[str]:
    today = date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(months):
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        end = min(today, nxt - timedelta(days=1))
        out.append((f"{y:04d}-{m:02d}", end.isoformat()))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return list(reversed(out))


def history(scope: str | None = None, months: int = 36) -> list[dict]:
    """Month-end values by group (last snapshot on or before each month end, carried forward)."""
    member = parse_scope(scope)
    with db.get_conn() as conn:
        ads = [a["id"] for a in adults(conn)]
        accs = db.rows(conn.execute("SELECT * FROM accounts WHERE archived = 0"))
        first = conn.execute("SELECT MIN(date) d FROM snapshots").fetchone()["d"]
        out = []
        for month, end in _month_ends(months):
            if first and end < first[:10] and out == []:
                continue
            g = {k: 0.0 for k in GROUPS}
            for acc in accs:
                share = scope_share(acc, member, ads)
                if share <= 0:
                    continue
                val = account_value(conn, acc, on=end)["value_chf"]
                kind = ACCOUNT_KINDS.get(acc["kind"], ACCOUNT_KINDS["other"])
                g[kind["group"]] += signed(acc["kind"], val) * share
            out.append({"month": month, **g, "total": sum(g.values())})
    return out


def write_auto_snapshots() -> int:
    """Store today's value of every holdings account (CHF) — keeps the history alive."""
    today = date.today().isoformat()
    n = 0
    with db.get_conn() as conn:
        for acc in db.rows(conn.execute("SELECT * FROM accounts WHERE valuation = 'holdings' AND archived = 0")):
            v = account_value(conn, acc)
            conn.execute(
                "INSERT INTO snapshots (account_id, date, balance_cents, source) VALUES (?, ?, ?, 'auto') "
                "ON CONFLICT(account_id, date) DO UPDATE SET balance_cents = excluded.balance_cents "
                "WHERE snapshots.source IN ('auto')",
                (acc["id"], today, to_cents(round(v["value_chf"], 2))))
            n += 1
    return n


# --------------------------------------------------------------------------- budget & shared costs

def split_by(incomes: dict[int, float], mode: str | None) -> dict[int, float]:
    """Each adult's part of the shared costs: by income (default) or equal parts (`spending_split`)."""
    if not incomes:
        return {}
    tot = sum(max(0.0, v) for v in incomes.values())
    if mode == "equal" or len(incomes) == 1 or tot <= 0:
        return {i: 1 / len(incomes) for i in incomes}
    return {i: max(0.0, v) / tot for i, v in incomes.items()}


def common_shares(conn) -> dict[int, float]:
    """Today's split of the shared costs between the adults — the rule of the individual plan
    (`model._individual`): gross income of the income lines active today, or equal parts."""
    today = date.today().isoformat()
    inc = {a["id"]: 0.0 for a in adults(conn)}
    for r in conn.execute("SELECT member_id, gross_annual_cents, start_date, end_date FROM incomes"):
        if r["member_id"] in inc and (not r["start_date"] or r["start_date"] <= today) \
                and (not r["end_date"] or r["end_date"] >= today):
            inc[r["member_id"]] += from_cents(r["gross_annual_cents"])
    return split_by(inc, db.get_settings().get("spending_split"))


def budget_lines(conn) -> tuple[dict[str, float], dict[int, dict[str, float]]]:
    """The planned budget per month: (shared part {category: CHF}, personal parts {member_id: {category: CHF}})."""
    common = {r["category"]: from_cents(r["monthly_cents"]) for r in conn.execute("SELECT * FROM budget")}
    personal: dict[int, dict[str, float]] = {}
    for r in conn.execute("SELECT * FROM budget_personal"):
        personal.setdefault(r["member_id"], {})[r["category"]] = from_cents(r["monthly_cents"])
    return common, personal


def shared_categories(common: dict[str, float], personal: dict[int, dict[str, float]]) -> set[str]:
    """Living-cost categories budgeted only as shared. Their real spending counts as shared too,
    whichever account paid it: rent paid by one adult is still split, and the other one paying
    them back (a transfer between own accounts) changes nothing."""
    mine = {c for lines in personal.values() for c, v in lines.items() if v}
    return {c for c, v in common.items() if v and c not in mine and c not in EXCLUDED_FROM_LIVING}


# --------------------------------------------------------------------------- cash flow

def _tx_rows(conn, since: str, member: int | None, ads: list[int]) -> list[dict]:
    """Transactions since a date, each with `chf` = its amount for the scope.

    For one adult: an explicit `member_id` wins; then a category budgeted only as shared
    counts for that adult's share of the shared costs; else the account's ownership share."""
    accs = {a["id"]: a for a in db.rows(conn.execute("SELECT * FROM accounts"))}
    shared, my_part = set(), 0.0
    if member is not None:
        shared = shared_categories(*budget_lines(conn))
        my_part = common_shares(conn).get(member, 0.0)
    out = []
    for t in db.rows(conn.execute("SELECT * FROM transactions WHERE date >= ? ORDER BY date", (since,))):
        acc = accs.get(t["account_id"])
        if not acc:
            continue
        if member is None:
            share = 1.0
        elif t["member_id"]:
            share = 1.0 if t["member_id"] == member else 0.0
        elif t["category"] in shared and not t["is_transfer"]:
            share = my_part
        else:
            share = scope_share(acc, member, ads)
        if share <= 0:
            continue
        fx, _ = market.fx_to_chf(t["currency"] or acc["currency"], t["date"], conn)
        t["chf"] = from_cents(t["amount_cents"]) * fx * share
        out.append(t)
    return out


MAX_SPREAD = 24  # months an operation can be spread over (PUT /transactions/{id} spread_months)


def _add_months(ym: str, k: int) -> str:
    """'2026-11' + 2 → '2027-01'."""
    n = int(ym[:4]) * 12 + int(ym[5:7]) - 1 + k
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


def cashflow(scope: str | None = None, months: int = 12) -> dict:
    """Income, spending and savings per month from the imported transactions.

    An operation with `spread_months` = N counts 1/N in its month and in each of the N-1
    months after it (a yearly health premium paid in January → 1/12 each month). A part only
    lands in a month that has statements; parts in months without data are left out.
    """
    member = parse_scope(scope)
    window = [m for m, _end in _month_ends(months)]
    start = window[0] + "-01"
    with db.get_conn() as conn:
        ads = [a["id"] for a in adults(conn)]
        # look back far enough to catch an older payment that is spread into the window
        txs = _tx_rows(conn, _add_months(window[0], 1 - MAX_SPREAD) + "-01", member, ads)
    in_window = set(window)
    covered = {t["date"][:7] for t in txs}  # months with at least one statement line
    by_month: dict[str, dict] = {}
    cats: dict[str, float] = defaultdict(float)
    uncategorized = 0
    for t in txs:
        cat = t["category"]
        ctype = CATEGORIES.get(cat, {}).get("type")
        if t["is_transfer"] or ctype == "transfer":
            continue
        if not cat and t["date"] >= start:
            uncategorized += 1
        is_income = ctype == "income" or (not cat and t["chf"] > 0)
        n = min(MAX_SPREAD, max(1, int(t.get("spread_months") or 1)))
        for i in range(n):
            m = _add_months(t["date"][:7], i)
            if m not in in_window or m not in covered:
                continue
            part = t["chf"] / n
            rec = by_month.setdefault(m, {"month": m, "income": 0.0, "spending": 0.0, "by_category": defaultdict(float)})
            if is_income:
                rec["income"] += part
            else:
                rec["spending"] += -part
                rec["by_category"][cat or "divers"] += -part
                cats[cat or "divers"] += -part
    rows = []
    for m in window:
        r = by_month.get(m, {"month": m, "income": 0.0, "spending": 0.0, "by_category": {}})
        inc, sp = r["income"], r["spending"]
        rows.append({"month": m, "income": inc, "spending": sp, "savings": inc - sp,
                     "savings_rate": (inc - sp) / inc if inc > 0 else None,
                     "by_category": dict(r["by_category"])})
    with_data = [r for r in rows if r["income"] or r["spending"]]
    inc = sum(r["income"] for r in with_data)
    sp = sum(r["spending"] for r in with_data)
    return {
        "scope": scope or "household", "months": rows, "months_with_data": len(with_data),
        "income": inc, "spending": sp, "savings": inc - sp,
        "savings_rate": (inc - sp) / inc if inc > 0 else None,
        "by_category": dict(sorted(cats.items(), key=lambda kv: -kv[1])),
        "uncategorized": uncategorized,
    }


def spending_profile() -> dict:
    """Yearly living costs for the plan: budget if set, else imported transactions, else a default.

    Taxes and children are excluded: the engine computes them itself. Amounts are for the
    household; `personal` / `personal_rent` ({member_id: CHF per year}) are the parts of
    `annual` / `rent` that one adult spends alone (empty unless the budget has personal lines).
    """
    with db.get_conn() as conn:
        common, personal = budget_lines(conn)

    def living(lines: dict[str, float]) -> dict[str, float]:
        return {k: v for k, v in lines.items() if k not in EXCLUDED_FROM_LIVING and v}

    mine = {m: living(lines) for m, lines in personal.items()}
    mine = {m: lines for m, lines in mine.items() if lines}
    personal_annual: dict[int, float] = {m: sum(lines.values()) * 12 for m, lines in mine.items()}
    personal_rent: dict[int, float] = {m: lines.get("logement", 0.0) * 12 for m, lines in mine.items()}
    cf = cashflow(None, 12)
    if living(common) or mine:
        by_cat: dict[str, float] = defaultdict(float)
        for lines in (living(common), *mine.values()):
            for k, v in lines.items():
                by_cat[k] += v * 12
        by_cat = dict(by_cat)
        annual = sum(by_cat.values())
        rent = by_cat.get("logement", 0.0)
        source = "budget"
    elif cf["months_with_data"] >= 2:
        k = 12 / cf["months_with_data"]
        by_cat = {c: v * k for c, v in cf["by_category"].items() if c not in EXCLUDED_FROM_LIVING}
        annual = sum(by_cat.values())
        rent = by_cat.get("logement", 0.0)
        source = "transactions"
    else:
        annual, rent, source, by_cat = 60_000.0, 24_000.0, "default", {}
    essential = sum(v for c, v in by_cat.items() if CATEGORIES.get(c, {}).get("essential"))
    return {"annual": annual, "rent": rent, "source": source, "by_category": by_cat,
            "personal": personal_annual, "personal_rent": personal_rent,
            "essential": essential if by_cat else annual * 0.7,
            "actual_12m": cf["spending"] * 12 / cf["months_with_data"] if cf["months_with_data"] else None}
