"""Finances / FIRE — /api/finance/*

Agent-first like the rest of the dashboard: every screen of the « Finances »
tab is one of these calls. Amounts are francs (float) in and out; the database
keeps centimes. Data lives in data/finance/ (gitignored) — see app/finance/.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .. import llm
from ..finance import backup, db, documents, gamify, ledger, market, metrics, model, params
from ..finance import pension as X
from ..finance import tax as T
from ..finance.categorize import CATEGORIES
from ..finance.money import from_cents, opt_cents, to_cents

router = APIRouter(prefix="/api/finance", tags=["finance"])

EVENT_KINDS = {
    "part_time": "Temps partiel (params: work_pct)",
    "career_break": "Pause carrière / congé sans solde",
    "salary_change": "Changement de salaire (params: gross_annual ou pct_change)",
    "side_income": "Revenu annexe (params: annual)",
    "home_purchase": "Achat immobilier (params: price, down_payment, use_3a, use_lpp, mortgage_rate_pct, owners)",
    "marriage": "Mariage",
    "move_abroad": "Départ à l'étranger (params: cost_factor, tax_rate_pct, capital_tax_pct)",
    "lump_sum": "Rentrée / dépense exceptionnelle (params: amount, négatif = dépense)",
    "expense_change": "Changement durable des dépenses (params: annual_delta)",
}


def _json(v) -> str:
    return json.dumps(v if v is not None else {})


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- status & settings


@router.get("/status")
def status():
    """What exists, what is configured, and the reference lists (categories, kinds…)."""
    s = db.get_settings()
    with db.get_conn() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                  for t in ("members", "accounts", "transactions", "documents", "incomes")}
    return {
        "onboarding_done": bool(s.get("onboarding_done")),
        "counts": counts,
        "settings": s,
        "categories": CATEGORIES,
        "account_kinds": metrics.ACCOUNT_KINDS,
        "doc_types": documents.DOC_TYPES,
        "event_kinds": EVENT_KINDS,
        "offline": market.offline(),
        "sources": params.SOURCES,
        "year": date.today().year,
        "p3a_max": params.get(date.today().year)["p3a"]["max_with_lpp"],
    }


@router.get("/llm/status")
def llm_status():
    """Is the local model server up (Ollama…), is the cloud configured, is the OCR (Tesseract) there?"""
    from ..finance.importers import ocr

    return {**llm.status(db.get_settings()), "ocr": ocr.status()}


@router.get("/settings")
def get_settings():
    return db.get_settings()


@router.put("/settings")
def put_settings(patch: dict):
    """Deep-merge a patch into the settings (e.g. {"assumptions": {"swr_pct": 3.25}})."""
    return db.save_settings(patch)


# --------------------------------------------------------------------------- members


class MemberIn(BaseModel):
    name: str
    emoji: str = "🙂"
    kind: str = Field("adult", pattern="^(adult|child)$")
    birth_date: Optional[str] = None
    expected: bool = False
    color: str = ""
    retire_age_target: Optional[int] = Field(None, ge=30, le=70)
    meta: dict = {}


def _member(row) -> dict:
    d = dict(row)
    d["meta"] = db.loads(d["meta"], {})
    d["expected"] = bool(d["expected"]) or bool(d["birth_date"] and d["birth_date"] > date.today().isoformat())
    return d


@router.get("/members")
def list_members():
    with db.get_conn() as conn:
        return [_member(r) for r in conn.execute("SELECT * FROM members ORDER BY kind, id")]


@router.post("/members", status_code=201)
def create_member(m: MemberIn):
    with db.get_conn() as conn:
        cur = conn.execute("INSERT INTO members (name, emoji, kind, birth_date, expected, color, retire_age_target, meta) "
                           "VALUES (?,?,?,?,?,?,?,?)", (m.name.strip(), m.emoji, m.kind, m.birth_date, int(m.expected),
                                                        m.color, m.retire_age_target, _json(m.meta)))
        return _member(conn.execute("SELECT * FROM members WHERE id = ?", (cur.lastrowid,)).fetchone())


@router.put("/members/{member_id}")
def update_member(member_id: int, m: MemberIn):
    with db.get_conn() as conn:
        old = conn.execute("SELECT meta FROM members WHERE id = ?", (member_id,)).fetchone()
        if not old:
            raise HTTPException(404, "Personne introuvable")
        meta = {**db.loads(old["meta"], {}), **(m.meta or {})}
        conn.execute("UPDATE members SET name=?, emoji=?, kind=?, birth_date=?, expected=?, color=?, retire_age_target=?, "
                     "meta=? WHERE id=?", (m.name.strip(), m.emoji, m.kind, m.birth_date, int(m.expected), m.color,
                                           m.retire_age_target, _json(meta), member_id))
        return _member(conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone())


@router.delete("/members/{member_id}", status_code=204)
def delete_member(member_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM members WHERE id = ?", (member_id,))


# --------------------------------------------------------------------------- incomes


class IncomeIn(BaseModel):
    member_id: int
    kind: str = Field("salary", pattern="^(salary|side|rental|other)$")
    label: str = ""
    employer: str = ""
    gross_annual: float = Field(0, ge=0)
    net_annual: Optional[float] = None
    work_pct: float = Field(100, ge=0, le=100)
    lpp_employee_annual: Optional[float] = None
    lpp_employer_annual: Optional[float] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    notes: str = ""


def _income(row) -> dict:
    d = dict(row)
    for k in ("gross_annual", "net_annual", "lpp_employee_annual", "lpp_employer_annual"):
        d[k] = from_cents(d.pop(f"{k}_cents"))
    return d


@router.get("/incomes")
def list_incomes(member_id: Optional[int] = None):
    q, args = "SELECT * FROM incomes", ()
    if member_id:
        q, args = q + " WHERE member_id = ?", (member_id,)
    with db.get_conn() as conn:
        return [_income(r) for r in conn.execute(q + " ORDER BY member_id, id", args)]


def _income_vals(i: IncomeIn):
    return (i.member_id, i.kind, i.label, i.employer, to_cents(i.gross_annual), opt_cents(i.net_annual), i.work_pct,
            opt_cents(i.lpp_employee_annual), opt_cents(i.lpp_employer_annual), i.start_date, i.end_date, i.notes)


@router.post("/incomes", status_code=201)
def create_income(i: IncomeIn):
    with db.get_conn() as conn:
        cur = conn.execute("INSERT INTO incomes (member_id, kind, label, employer, gross_annual_cents, net_annual_cents, "
                           "work_pct, lpp_employee_annual_cents, lpp_employer_annual_cents, start_date, end_date, notes) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", _income_vals(i))
        return _income(conn.execute("SELECT * FROM incomes WHERE id = ?", (cur.lastrowid,)).fetchone())


@router.put("/incomes/{income_id}")
def update_income(income_id: int, i: IncomeIn):
    with db.get_conn() as conn:
        n = conn.execute("UPDATE incomes SET member_id=?, kind=?, label=?, employer=?, gross_annual_cents=?, "
                         "net_annual_cents=?, work_pct=?, lpp_employee_annual_cents=?, lpp_employer_annual_cents=?, "
                         "start_date=?, end_date=?, notes=?, updated_at=datetime('now') WHERE id=?",
                         (*_income_vals(i), income_id)).rowcount
        if not n:
            raise HTTPException(404, "Revenu introuvable")
        return _income(conn.execute("SELECT * FROM incomes WHERE id = ?", (income_id,)).fetchone())


@router.delete("/incomes/{income_id}", status_code=204)
def delete_income(income_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM incomes WHERE id = ?", (income_id,))


# --------------------------------------------------------------------------- accounts


class AccountIn(BaseModel):
    name: str
    institution: str = ""
    kind: str
    currency: str = "CHF"
    owners: dict = {}                 # {member_id: share}; {} = shared equally
    iban: str = ""
    valuation: str = Field("manual", pattern="^(manual|holdings)$")
    equity_pct: Optional[float] = Field(None, ge=0, le=100)
    include_in_fi: bool = True
    archived: bool = False
    meta: dict = {}
    balance: Optional[float] = None   # creates today's snapshot
    balance_date: Optional[str] = None


def _check_kind(kind: str):
    if kind not in metrics.ACCOUNT_KINDS:
        raise HTTPException(422, f"type de compte inconnu : {kind}")


@router.get("/accounts")
def list_accounts(include_archived: bool = False):
    return metrics.list_accounts(include_archived)


@router.post("/accounts", status_code=201)
def create_account(a: AccountIn):
    _check_kind(a.kind)
    with db.get_conn() as conn:
        cur = conn.execute("INSERT INTO accounts (name, institution, kind, currency, owners, iban, valuation, equity_pct, "
                           "include_in_fi, archived, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (a.name.strip(), a.institution, a.kind, a.currency.upper()[:3], _json(a.owners),
                            a.iban.replace(" ", "").upper(), a.valuation, a.equity_pct, int(a.include_in_fi),
                            int(a.archived), _json(a.meta)))
        acc_id = cur.lastrowid
        if a.balance is not None:
            conn.execute("INSERT INTO snapshots (account_id, date, balance_cents) VALUES (?, ?, ?)",
                         (acc_id, a.balance_date or date.today().isoformat(), to_cents(a.balance)))
    return get_account(acc_id)


@router.get("/accounts/{account_id}")
def get_account(account_id: int):
    acc = next((a for a in metrics.list_accounts(True) if a["id"] == account_id), None)
    if not acc:
        raise HTTPException(404, "Compte introuvable")
    with db.get_conn() as conn:
        acc["snapshots"] = [{"id": r["id"], "date": r["date"], "balance": from_cents(r["balance_cents"]),
                             "source": r["source"], "note": r["note"]} for r in
                            conn.execute("SELECT * FROM snapshots WHERE account_id = ? ORDER BY date DESC LIMIT 60",
                                         (account_id,))]
        if acc["valuation"] == "holdings":
            _, _, acc["holdings"] = metrics.holdings_value(conn, account_id)
    return acc


@router.put("/accounts/{account_id}")
def update_account(account_id: int, a: AccountIn):
    _check_kind(a.kind)
    with db.get_conn() as conn:
        old = conn.execute("SELECT meta FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not old:
            raise HTTPException(404, "Compte introuvable")
        meta = {**db.loads(old["meta"], {}), **(a.meta or {})}
        conn.execute("UPDATE accounts SET name=?, institution=?, kind=?, currency=?, owners=?, iban=?, valuation=?, "
                     "equity_pct=?, include_in_fi=?, archived=?, meta=?, updated_at=datetime('now') WHERE id=?",
                     (a.name.strip(), a.institution, a.kind, a.currency.upper()[:3], _json(a.owners),
                      a.iban.replace(" ", "").upper(), a.valuation, a.equity_pct, int(a.include_in_fi),
                      int(a.archived), _json(meta), account_id))
        if a.balance is not None:
            _upsert_snapshot(conn, account_id, a.balance_date or date.today().isoformat(), a.balance, "manual")
    return get_account(account_id)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(account_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))


class SnapshotIn(BaseModel):
    balance: float
    date: Optional[str] = None
    note: str = ""


def _upsert_snapshot(conn, account_id: int, day: str, balance: float, source: str, note: str = ""):
    conn.execute("INSERT INTO snapshots (account_id, date, balance_cents, source, note) VALUES (?, ?, ?, ?, ?) "
                 "ON CONFLICT(account_id, date) DO UPDATE SET balance_cents = excluded.balance_cents, "
                 "source = excluded.source, note = excluded.note", (account_id, day, to_cents(balance), source, note))


@router.post("/accounts/{account_id}/snapshots", status_code=201)
def add_snapshot(account_id: int, s: SnapshotIn):
    with db.get_conn() as conn:
        if not conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone():
            raise HTTPException(404, "Compte introuvable")
        _upsert_snapshot(conn, account_id, s.date or date.today().isoformat(), s.balance, "manual", s.note)
    return get_account(account_id)


@router.delete("/snapshots/{snapshot_id}", status_code=204)
def delete_snapshot(snapshot_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))


class HoldingIn(BaseModel):
    symbol: str = ""
    isin: str = ""
    valor: str = ""  # Swiss security number
    name: str
    quantity: float
    currency: str = "CHF"
    asset_class: str = Field("equity", pattern="^(equity|bonds|cash|real_estate|crypto|commodity)$")
    cost_basis: Optional[float] = None
    manual_price: Optional[float] = None


@router.post("/accounts/{account_id}/holdings", status_code=201)
def add_holding(account_id: int, h: HoldingIn):
    with db.get_conn() as conn:
        conn.execute("UPDATE accounts SET valuation = 'holdings' WHERE id = ?", (account_id,))
        conn.execute("INSERT INTO holdings (account_id, symbol, isin, valor, name, quantity, currency, asset_class, "
                     "cost_basis_cents, manual_price) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (account_id, h.symbol.strip().upper(), h.isin.strip().upper(), h.valor.strip(), h.name,
                      h.quantity, h.currency.upper()[:3], h.asset_class, opt_cents(h.cost_basis), h.manual_price))
    if h.symbol:
        market.refresh_prices([h.symbol.strip().upper()])
    metrics.write_auto_snapshots()
    return get_account(account_id)


@router.put("/holdings/{holding_id}")
def update_holding(holding_id: int, h: HoldingIn):
    with db.get_conn() as conn:
        row = conn.execute("SELECT account_id FROM holdings WHERE id = ?", (holding_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Position introuvable")
        conn.execute("UPDATE holdings SET symbol=?, isin=?, valor=?, name=?, quantity=?, currency=?, asset_class=?, "
                     "cost_basis_cents=?, manual_price=?, updated_at=datetime('now') WHERE id=?",
                     (h.symbol.strip().upper(), h.isin.strip().upper(), h.valor.strip(), h.name, h.quantity,
                      h.currency.upper()[:3], h.asset_class, opt_cents(h.cost_basis), h.manual_price, holding_id))
    if h.symbol.strip():  # a ticker added by hand: its live price right away
        market.refresh_prices([h.symbol.strip().upper()])
    metrics.write_auto_snapshots()
    return get_account(row["account_id"])


@router.delete("/holdings/{holding_id}", status_code=204)
def delete_holding(holding_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM holdings WHERE id = ?", (holding_id,))


# --------------------------------------------------------------------------- monthly check-in


@router.get("/checkin")
def checkin_form():
    """The accounts to update this month, prefilled with their last value."""
    accounts = [a for a in metrics.list_accounts() if a["valuation"] == "manual"]
    auto = [a for a in metrics.list_accounts() if a["valuation"] == "holdings"]
    return {"month": date.today().strftime("%Y-%m"), "streak": gamify.streak(),
            "accounts": accounts, "auto_accounts": auto}


class CheckinBalance(BaseModel):
    account_id: int
    balance: float


class CheckinIn(BaseModel):
    date: Optional[str] = None
    balances: list[CheckinBalance] = []
    note: str = ""


@router.post("/checkin")
def checkin(c: CheckinIn):
    """Monthly ritual: balances in, then the climb of the month (net worth, freedom days, XP, badges)."""
    day = c.date or date.today().isoformat()
    before = metrics.net_worth()
    prev_proj = _safe_projection()
    with db.get_conn() as conn:
        for b in c.balances:
            _upsert_snapshot(conn, b.account_id, day, b.balance, "manual")
    after = metrics.net_worth()
    month = day[:7]
    with db.get_conn() as conn:
        last = conn.execute("SELECT * FROM checkins WHERE month < ? ORDER BY month DESC LIMIT 1", (month,)).fetchone()
        conn.execute("INSERT INTO checkins (month, net_worth_cents, liquid_cents, note) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(month) DO UPDATE SET net_worth_cents = excluded.net_worth_cents, "
                     "liquid_cents = excluded.liquid_cents, note = excluded.note, done_at = datetime('now')",
                     (month, to_cents(round(after["total"], 2)), to_cents(round(after["liquid"], 2)), c.note))
    gamify.award("checkin", month, 100, f"Bilan {month}")
    proj = _safe_projection()
    new_badges = gamify.check_badges(proj)
    spend = metrics.spending_profile()["annual"] or 1
    ref_liquid = from_cents(last["liquid_cents"]) if last else before["liquid"]
    ref_total = from_cents(last["net_worth_cents"]) if last else before["total"]
    return {
        "month": month,
        "net_worth": after["total"], "liquid": after["liquid"],
        "change": after["total"] - ref_total, "liquid_change": after["liquid"] - ref_liquid,
        "freedom_days_gained": (after["liquid"] - ref_liquid) / (spend / 365),
        "progress_before": (prev_proj or {}).get("progress"), "progress": (proj or {}).get("progress"),
        "fi_year_before": (prev_proj or {}).get("fi_year"), "fi_year": (proj or {}).get("fi_year"),
        "new_badges": [{"code": b, **gamify.BADGES[b]} for b in new_badges],
        "streak": gamify.streak(), "level": gamify.level_for(gamify.total_xp()),
    }


def _safe_projection(scope=None):
    try:
        p = model.projection(scope)
        return p if p.get("ready") else None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- transactions


class TxIn(BaseModel):
    account_id: int
    date: str
    amount: float
    description: str = ""
    counterparty: str = ""
    category: str = ""
    currency: Optional[str] = None
    note: str = ""
    spread_months: int = Field(1, ge=1, le=metrics.MAX_SPREAD)


class TxUpdate(BaseModel):
    category: Optional[str] = None
    learn: bool = True
    is_transfer: Optional[bool] = None
    member_id: Optional[int] = None
    note: Optional[str] = None
    # count it over N months from its month (yearly premium → 12); 1 = only its own month
    spread_months: Optional[int] = Field(None, ge=1, le=metrics.MAX_SPREAD)


def _tx(row) -> dict:
    d = dict(row)
    d["amount"] = from_cents(d.pop("amount_cents"))
    d["is_transfer"] = bool(d["is_transfer"])
    return d


@router.get("/transactions")
def list_transactions(account_id: Optional[int] = None, category: Optional[str] = None, q: Optional[str] = None,
                      month: Optional[str] = None, uncategorized: bool = False,
                      limit: int = Query(200, le=2000), offset: int = 0):
    where, args = [], []
    if account_id:
        where.append("t.account_id = ?"), args.append(account_id)
    if category:
        where.append("t.category = ?"), args.append(category)
    if uncategorized:
        where.append("t.category = ''")
    if month:
        where.append("substr(t.date, 1, 7) = ?"), args.append(month)
    if q:
        where.append("(t.description LIKE ? OR t.counterparty LIKE ?)"), args.extend([f"%{q}%", f"%{q}%"])
    sql = ("SELECT t.*, a.name AS account_name FROM transactions t JOIN accounts a ON a.id = t.account_id"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY t.date DESC, t.id DESC LIMIT ? OFFSET ?")
    with db.get_conn() as conn:
        items = [_tx(r) for r in conn.execute(sql, (*args, limit, offset))]
        total = conn.execute("SELECT COUNT(*) c FROM transactions t" + (" WHERE " + " AND ".join(where) if where else ""),
                             args).fetchone()["c"]
    return {"items": items, "total": total}


@router.post("/transactions", status_code=201)
def create_transaction(t: TxIn):
    from decimal import Decimal
    res = ledger.import_transactions(t.account_id, [{"date": t.date, "amount": Decimal(str(t.amount)),
                                                     "description": t.description, "counterparty": t.counterparty,
                                                     "currency": t.currency, "reference": f"manual-{_now()}"}])
    if t.category or t.spread_months > 1:
        with db.get_conn() as conn:
            row = conn.execute("SELECT id FROM transactions WHERE import_id = ?", (res["import_id"],)).fetchone()
            if row and t.spread_months > 1:
                conn.execute("UPDATE transactions SET spread_months = ? WHERE id = ?", (t.spread_months, row["id"]))
        if row and t.category:
            ledger.set_category(row["id"], t.category, learn=False)
    return res


@router.put("/transactions/{tx_id}")
def update_transaction(tx_id: int, u: TxUpdate):
    out = {}
    if u.category is not None:
        try:
            out = ledger.set_category(tx_id, u.category, learn=u.learn, is_transfer=u.is_transfer)
        except KeyError:
            raise HTTPException(404, "Opération introuvable")
        except ValueError as e:
            raise HTTPException(422, str(e))
        gamify.award("categorize", str(tx_id), 2, "Opération classée")
    with db.get_conn() as conn:
        if u.member_id is not None:
            conn.execute("UPDATE transactions SET member_id = ? WHERE id = ?", (u.member_id or None, tx_id))
        if u.note is not None:
            conn.execute("UPDATE transactions SET note = ? WHERE id = ?", (u.note, tx_id))
        if u.spread_months is not None:
            conn.execute("UPDATE transactions SET spread_months = ? WHERE id = ?", (u.spread_months, tx_id))
        if u.is_transfer is not None and u.category is None:
            conn.execute("UPDATE transactions SET is_transfer = ? WHERE id = ?", (int(u.is_transfer), tx_id))
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Opération introuvable")
    return {**_tx(row), **{k: v for k, v in out.items() if k in ("rule", "reapplied")}}


@router.delete("/transactions/{tx_id}", status_code=204)
def delete_transaction(tx_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))


@router.post("/transactions/categorize-ai")
def categorize_ai(limit: int = 200):
    """Let the LLM propose categories for what rules didn't recognise (descriptions only)."""
    return ledger.llm_categorize(db.get_settings(), limit)


@router.delete("/imports/{import_id}")
def undo_import(import_id: int):
    """Undo an import: removes the transactions it added."""
    return {"deleted": ledger.undo_import(import_id)}


class RuleIn(BaseModel):
    pattern: str
    category: str
    is_transfer: bool = False


@router.get("/rules")
def list_rules():
    with db.get_conn() as conn:
        return db.rows(conn.execute("SELECT * FROM rules ORDER BY origin, pattern"))


@router.post("/rules", status_code=201)
def create_rule(r: RuleIn):
    if r.category not in CATEGORIES:
        raise HTTPException(422, "catégorie inconnue")
    pattern = ledger.normalize(r.pattern)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO rules (pattern, category, is_transfer) VALUES (?, ?, ?) ON CONFLICT(pattern) DO "
                     "UPDATE SET category = excluded.category, is_transfer = excluded.is_transfer",
                     (pattern, r.category, int(r.is_transfer)))
    return {"pattern": pattern, "reapplied": ledger.recategorize()}


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))


# --------------------------------------------------------------------------- budget


@router.get("/budget")
def get_budget(scope: str = "household"):
    """Planned monthly amount per category next to the 12-month actual average.

    Each row has `common` (the shared part), `personal` {member_id: amount} and `planned`,
    what the scope spends: for the household, common + every personal line; for one adult
    (`member:<id>`), their own line + `common_share` (their `share` of the common part).
    `actual` follows the scope too (see metrics._tx_rows for who an operation counts for).
    """
    member = metrics.parse_scope(scope)
    with db.get_conn() as conn:
        common, personal = metrics.budget_lines(conn)
        shares = metrics.common_shares(conn)
        adult_ids = [a["id"] for a in metrics.adults(conn)]
    if member is not None and member not in adult_ids:
        raise HTTPException(404, "adulte inconnu")
    share = shares.get(member, 0.0) if member is not None else None
    cf = metrics.cashflow(scope, 12)
    n = max(1, cf["months_with_data"])
    actual = {k: v / n for k, v in cf["by_category"].items()}
    rows = []
    for k, c in CATEGORIES.items():
        if c["type"] != "expense":
            continue
        com = common.get(k, 0.0)
        pers = {mid: personal.get(mid, {}).get(k, 0.0) for mid in adult_ids}
        if member is None:
            planned, com_share = com + sum(pers.values()), None
        else:
            com_share = com * share
            planned = pers[member] + com_share
        rows.append({"category": k, **c, "common": com, "personal": pers, "common_share": com_share,
                     "planned": planned, "actual": actual.get(k)})
    return {"scope": scope, "member_id": member, "share": share, "shares": shares,
            "split": db.get_settings().get("spending_split", "income"),
            "rows": rows, "planned_total": sum(r["planned"] for r in rows),
            "actual_total": (cf["spending"] / n) if cf["months_with_data"] else None,
            "months_with_data": cf["months_with_data"], "profile": metrics.spending_profile()}


@router.put("/budget")
def put_budget(values: dict[str, float], member_id: Optional[int] = None):
    """Set planned amounts per month, {category: CHF}. Without `member_id`: the shared part
    (« Commun »); with it: that adult's personal part. 0 empties a line."""
    with db.get_conn() as conn:
        if member_id is not None and member_id not in [a["id"] for a in metrics.adults(conn)]:
            raise HTTPException(404, "adulte inconnu")
        for k, v in values.items():
            if k not in CATEGORIES:
                continue
            cents = to_cents(max(0.0, float(v or 0)))
            if member_id is None:
                conn.execute("INSERT INTO budget (category, monthly_cents) VALUES (?, ?) ON CONFLICT(category) "
                             "DO UPDATE SET monthly_cents = excluded.monthly_cents", (k, cents))
            elif cents:
                conn.execute("INSERT INTO budget_personal (category, member_id, monthly_cents) VALUES (?, ?, ?) "
                             "ON CONFLICT(category, member_id) DO UPDATE SET monthly_cents = excluded.monthly_cents",
                             (k, member_id, cents))
            else:
                conn.execute("DELETE FROM budget_personal WHERE category = ? AND member_id = ?", (k, member_id))
    return get_budget("household" if member_id is None else f"member:{member_id}")


# --------------------------------------------------------------------------- goals


class GoalIn(BaseModel):
    name: str
    kind: str = "custom"
    emoji: str = "🎯"
    target: float = Field(0, ge=0)
    target_date: Optional[str] = None
    account_ids: list[int] = []
    scope: str = "household"
    notes: str = ""


def _goal(row, accounts) -> dict:
    d = dict(row)
    d["target"] = from_cents(d.pop("target_cents"))
    d["account_ids"] = db.loads(d["account_ids"], [])
    member = metrics.parse_scope(d["scope"])
    if d["account_ids"]:
        pool = [a for a in accounts if a["id"] in d["account_ids"]]
    else:
        pool = [a for a in accounts if a["group"] == "liquid"]
    cur = sum(a["signed_chf"] * (1.0 if member is None else a["shares"].get(member, 0.0)) for a in pool)
    d["current"] = cur
    d["progress"] = min(1.0, cur / d["target"]) if d["target"] else 0.0
    if d["target_date"] and d["target"] > cur:
        months = max(1, (date.fromisoformat(d["target_date"]) - date.today()).days / 30.44)
        d["monthly_needed"] = (d["target"] - cur) / months
    return d


@router.get("/goals")
def list_goals():
    accounts = metrics.list_accounts()
    with db.get_conn() as conn:
        rows = list(conn.execute("SELECT * FROM goals ORDER BY achieved_at IS NOT NULL, target_date"))
    goals = [_goal(r, accounts) for r in rows]
    for g in goals:
        if g["progress"] >= 1 and not g["achieved_at"]:
            with db.get_conn() as conn:
                conn.execute("UPDATE goals SET achieved_at = ? WHERE id = ?", (_now(), g["id"]))
            gamify.award("goal", str(g["id"]), 300, g["name"])
            g["achieved_at"] = _now()
            g["just_achieved"] = True
    return goals


@router.post("/goals", status_code=201)
def create_goal(g: GoalIn):
    with db.get_conn() as conn:
        cur = conn.execute("INSERT INTO goals (name, kind, emoji, target_cents, target_date, account_ids, scope, notes) "
                           "VALUES (?,?,?,?,?,?,?,?)", (g.name, g.kind, g.emoji, to_cents(g.target), g.target_date,
                                                        json.dumps(g.account_ids), g.scope, g.notes))
        gid = cur.lastrowid
    return next(x for x in list_goals() if x["id"] == gid)


@router.put("/goals/{goal_id}")
def update_goal(goal_id: int, g: GoalIn):
    with db.get_conn() as conn:
        n = conn.execute("UPDATE goals SET name=?, kind=?, emoji=?, target_cents=?, target_date=?, account_ids=?, "
                         "scope=?, notes=? WHERE id=?", (g.name, g.kind, g.emoji, to_cents(g.target), g.target_date,
                                                         json.dumps(g.account_ids), g.scope, g.notes, goal_id)).rowcount
    if not n:
        raise HTTPException(404, "Objectif introuvable")
    return next(x for x in list_goals() if x["id"] == goal_id)


@router.delete("/goals/{goal_id}", status_code=204)
def delete_goal(goal_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))


# --------------------------------------------------------------------------- events & scenarios


class EventIn(BaseModel):
    kind: str
    label: str = ""
    member_id: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    params: dict = {}
    scenario_id: Optional[int] = None
    enabled: bool = True


def _event(row) -> dict:
    d = dict(row)
    d["params"] = db.loads(d["params"], {})
    d["enabled"] = bool(d["enabled"])
    return d


@router.get("/events")
def list_events(scenario_id: Optional[int] = None):
    with db.get_conn() as conn:
        if scenario_id:
            rows = conn.execute("SELECT * FROM events WHERE scenario_id = ? ORDER BY start_date", (scenario_id,))
        else:
            rows = conn.execute("SELECT * FROM events ORDER BY start_date")
        return [_event(r) for r in rows]


@router.post("/events", status_code=201)
def create_event(e: EventIn):
    if e.kind not in EVENT_KINDS:
        raise HTTPException(422, "type d'événement inconnu")
    with db.get_conn() as conn:
        cur = conn.execute("INSERT INTO events (kind, label, member_id, start_date, end_date, params, scenario_id, enabled) "
                           "VALUES (?,?,?,?,?,?,?,?)", (e.kind, e.label, e.member_id, e.start_date, e.end_date,
                                                        _json(e.params), e.scenario_id, int(e.enabled)))
        return _event(conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone())


@router.put("/events/{event_id}")
def update_event(event_id: int, e: EventIn):
    with db.get_conn() as conn:
        n = conn.execute("UPDATE events SET kind=?, label=?, member_id=?, start_date=?, end_date=?, params=?, "
                         "scenario_id=?, enabled=? WHERE id=?", (e.kind, e.label, e.member_id, e.start_date, e.end_date,
                                                                 _json(e.params), e.scenario_id, int(e.enabled),
                                                                 event_id)).rowcount
        if not n:
            raise HTTPException(404, "Événement introuvable")
        return _event(conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())


@router.delete("/events/{event_id}", status_code=204)
def delete_event(event_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))


class ScenarioIn(BaseModel):
    name: str
    description: str = ""
    overrides: dict = {}
    base_events: str = Field("keep", pattern="^(keep|drop)$")


@router.get("/scenarios")
def list_scenarios():
    with db.get_conn() as conn:
        out = []
        for r in conn.execute("SELECT * FROM scenarios ORDER BY id"):
            d = dict(r)
            d["overrides"] = db.loads(d["overrides"], {})
            out.append(d)
        return out


@router.post("/scenarios", status_code=201)
def create_scenario(s: ScenarioIn):
    with db.get_conn() as conn:
        cur = conn.execute("INSERT INTO scenarios (name, description, overrides, base_events) VALUES (?,?,?,?)",
                           (s.name, s.description, _json(s.overrides), s.base_events))
        sid = cur.lastrowid
    gamify.check_badges()
    return next(x for x in list_scenarios() if x["id"] == sid)


@router.put("/scenarios/{scenario_id}")
def update_scenario(scenario_id: int, s: ScenarioIn):
    with db.get_conn() as conn:
        conn.execute("UPDATE scenarios SET name=?, description=?, overrides=?, base_events=? WHERE id=?",
                     (s.name, s.description, _json(s.overrides), s.base_events, scenario_id))
    return next((x for x in list_scenarios() if x["id"] == scenario_id), None)


@router.delete("/scenarios/{scenario_id}", status_code=204)
def delete_scenario(scenario_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM scenarios WHERE id = ?", (scenario_id,))


@router.get("/scenarios/compare")
def compare_scenarios(ids: str = "", scope: str = "household"):
    """Base plan + the given scenarios side by side (FI year, taxes, net worth at 65…)."""
    wanted = [None] + [int(x) for x in ids.split(",") if x.strip().isdigit()]
    names = {s["id"]: s["name"] for s in list_scenarios()}
    out = []
    for sid in wanted:
        p = model.projection(scope, sid)
        if not p.get("ready"):
            continue
        yrs = p["years"]
        first_adult = p["adults"][0]
        at65 = next((y for y in yrs if y["year"] == first_adult["birth_year"] + 65), yrs[-1])
        out.append({
            "scenario_id": sid, "name": names.get(sid, "Plan actuel"),
            "fi_year": p["fi_year"], "fi_years": p["fi_years"], "fi_ages": p["fi_ages"],
            "required_liquid_now": p["required_liquid_now"], "progress": p["progress"],
            "taxes_10y": sum(y["taxes"] for y in yrs[:10]),
            "taxes_lifetime": sum(y["taxes"] + y["capital_tax"] for y in yrs),
            "net_worth_65": at65["net_worth"], "liquid_series": [[y["year"], y["liquid"]] for y in yrs],
        })
    return out


# --------------------------------------------------------------------------- documents


@router.post("/documents", status_code=201)
async def upload_documents(files: list[UploadFile] = File(...), member_id: Optional[int] = Form(None),
                           account_id: Optional[int] = Form(None), doc_type: Optional[str] = Form(None)):
    """Drop one or many files (CAMT.053 XML, CSV, XLSX, PDF, photos). Read in the background.
    `doc_type` (optional): the type chosen by the household — the model then only extracts its fields."""
    try:
        hint = documents.check_type(doc_type)
    except ValueError as e:
        raise HTTPException(422, str(e))
    out = []
    for f in files:
        data = await f.read()
        try:
            d = documents.store(f.filename or "document", data, f.content_type or "", member_id, account_id, hint)
        except ValueError as e:
            raise HTTPException(413, str(e))
        rechosen = d.get("duplicate") and hint and hint != d["hint_type"] and d["status"] != "applied"
        if rechosen:  # same file again, now with a type: read it again as that type
            documents.set_hint(d["id"], hint)
        if not d.get("duplicate") or d["status"] in ("error", "pending") or rechosen:
            documents.parse_async(d["id"])
        out.append(d)
    return out


@router.get("/documents")
def list_documents(limit: int = 100):
    return documents.list_docs(limit)


@router.get("/documents/{doc_id}")
def get_document(doc_id: int):
    try:
        return documents.get(doc_id)
    except KeyError:
        raise HTTPException(404, "Document introuvable")


@router.get("/documents/{doc_id}/file")
def get_document_file(doc_id: int):
    d = get_document(doc_id)
    path = db.docs_dir() / d["stored_name"]
    if not path.exists():
        raise HTTPException(404, "Fichier absent")
    return FileResponse(path, filename=d["filename"], media_type=d["mime"] or None)


class ReparseIn(BaseModel):
    doc_type: Optional[str] = None  # None: keep the current choice · "": let the model guess · else that type


@router.post("/documents/{doc_id}/reparse")
def reparse_document(doc_id: int, body: Optional[ReparseIn] = None):
    get_document(doc_id)
    if body and body.doc_type is not None:
        try:
            documents.set_hint(doc_id, body.doc_type)
        except ValueError as e:
            raise HTTPException(422, str(e))
    documents.parse_async(doc_id)
    return {"status": "parsing"}


class MaskEdits(BaseModel):
    off: list[str] = []      # tags (« [PERSONNE_2] ») the household lets go out as written
    extra: list[str] = []    # more text to hide
    remember: bool = False   # on send: add `extra` to settings.privacy.always_mask


@router.post("/documents/{doc_id}/mask")
def mask_document(doc_id: int, body: Optional[MaskEdits] = None):
    """Mode private_cloud: the masked text with these changes, and the final check. Nothing saved or sent."""
    get_document(doc_id)
    try:
        return documents.mask_preview(doc_id, body.model_dump() if body else {})
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/documents/{doc_id}/send")
def send_document(doc_id: int, body: Optional[MaskEdits] = None):
    """Mode private_cloud: send the checked, masked text to the cloud model (read in the background,
    then status `review`). 409 when the final check still finds a private detail: nothing leaves."""
    get_document(doc_id)
    try:
        return documents.send(doc_id, body.model_dump() if body else {})
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/documents/{doc_id}/apply")
def apply_document(doc_id: int, body: dict):
    """Write a reviewed document (body may set account_id, member_id, fields, create_account, doc_type)."""
    try:
        res = documents.apply(doc_id, body or {})
    except KeyError:
        raise HTTPException(404, "Document introuvable")
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"result": res, "document": documents.get(doc_id)}


@router.post("/documents/{doc_id}/ignore")
def ignore_document(doc_id: int):
    documents._set(doc_id, status="ignored")
    return documents.get(doc_id)


@router.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: int):
    try:
        documents.delete(doc_id)
    except KeyError:
        raise HTTPException(404, "Document introuvable")


# --------------------------------------------------------------------------- overview & plan


@router.get("/overview")
def overview(scope: str = "household"):
    """Everything the « Sommet » screen needs in one call."""
    nw = metrics.net_worth(scope)
    cf = metrics.cashflow(scope, 12)
    prof = metrics.spending_profile()
    proj = _safe_projection(scope)
    short = None
    if proj:
        short = {k: proj[k] for k in ("fi_year", "fi_years", "fi_ages", "progress", "required_liquid_now",
                                      "liquid_now", "coast", "rule_of_thumb", "plan_success", "milestones",
                                      "spending_now", "adults", "kids", "style")}
    spend = ((proj or {}).get("spending_now") or prof["annual"]) or 1
    game = gamify.state(proj)
    with db.get_conn() as conn:
        last = conn.execute("SELECT * FROM checkins ORDER BY month DESC LIMIT 2").fetchall()
    return {
        "scope": scope,
        "net_worth": nw, "cashflow": {k: cf[k] for k in ("income", "spending", "savings", "savings_rate",
                                                       "months_with_data", "uncategorized", "by_category")},
        "spending_profile": prof,
        "freedom_days": nw["liquid"] / (spend / 365) if spend else None,
        "projection": short,
        "game": game,
        "goals": list_goals(),
        "last_checkins": [dict(r) for r in last],
    }


@router.get("/projection")
def projection(scope: str = "household", scenario_id: Optional[int] = None):
    """Year-by-year plan, FI year (lean/normal/fat), money needed to stop now, Coast FI, milestones."""
    try:
        return model.projection(scope, scenario_id)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/montecarlo")
def montecarlo(scope: str = "household", scenario_id: Optional[int] = None, runs: int = Query(1000, ge=50, le=5000),
               target: float = Query(0.85, ge=0.5, le=0.99)):
    """Success probability of the plan (random returns), percentile bands, and the « safe » FI year."""
    return model.montecarlo(scope, scenario_id, runs, target)


@router.get("/history")
def history(scope: str = "household", months: int = Query(36, ge=1, le=240)):
    return metrics.history(scope, months)


@router.get("/cashflow")
def cashflow(scope: str = "household", months: int = Query(12, ge=1, le=120)):
    return metrics.cashflow(scope, months)


# --------------------------------------------------------------------------- calculators (agents)


class EarnerIn(BaseModel):
    salary_gross: float = 0
    self_employed: float = 0
    pension: float = 0
    investment: float = 0
    savings_interest: float = 0
    other: float = 0
    lpp_employee: Optional[float] = None
    lpp_buyin: float = 0
    pillar3a: float = 0
    age: int = 40
    aanp_rate: float = 0.01
    commute: float = 0
    meals_out: bool = True
    childcare: float = 0
    insurance: float = 5400
    rent_net: float = 0


class TaxIn(BaseModel):
    status: str = Field("single", pattern="^(single|married)$")
    earners: list[EarnerIn]
    children_ages: list[int] = []
    child_share: float = 1.0
    single_parent: bool = False
    ifd_parental: Optional[bool] = None
    wealth: float = 0
    year: int = 2026
    communal_coefficient: Optional[float] = None


@router.post("/calc/tax")
def calc_tax(t: TaxIn):
    """Federal + Vaud income and wealth tax for any situation (social contributions estimated from gross)."""
    p = params.get(t.year)
    earners = []
    for e in t.earners:
        lpp = e.lpp_employee if e.lpp_employee is not None else T.legal_lpp_employee(e.salary_gross, e.age, p)
        soc = T.social_contributions(e.salary_gross, p, aanp_rate=e.aanp_rate)["total"]
        earners.append(T.Earner(salary_gross=e.salary_gross, self_employed=e.self_employed, pension=e.pension,
                                investment=e.investment, savings_interest=e.savings_interest, other=e.other,
                                social=soc + e.self_employed * p["avs"]["self_employed_rate"], lpp_employee=lpp,
                                lpp_buyin=e.lpp_buyin, pillar3a=e.pillar3a, commute=e.commute, meals_out=e.meals_out,
                                childcare=e.childcare, insurance=e.insurance, rent_net=e.rent_net,
                                has_pension_plan=lpp > 0 or e.pillar3a > 0))
    kids = [T.Child(age=a, share=t.child_share) for a in t.children_ages]
    parental = t.ifd_parental if t.ifd_parental is not None else (bool(kids) and t.status == "single")
    tp = T.Taxpayer(status=t.status, earners=earners, children=kids, single_parent=t.single_parent,
                    ifd_parental=parental, wealth=t.wealth, year=t.year,
                    communal_coefficient=t.communal_coefficient or float(db.get_settings()["communal_coefficient"]))
    r = T.compute(tp)
    r["marginal_rate_pct"] = T.marginal_rate(tp) * 100
    return r


@router.get("/calc/capital-tax")
def calc_capital_tax(amount: float, married: bool = False, year: int = 2026):
    """Tax on a pension capital withdrawal (2nd pillar / 3a) in Lausanne (or the configured commune)."""
    return T.capital_tax(amount, married=married, year=year,
                         communal_pct=float(db.get_settings()["communal_coefficient"]))


@router.get("/calc/avs")
def calc_avs(average_income: float, years: int = 44, start_age: int = 65, year: int = 2026):
    """AVS old-age pension from an average yearly income (RAMD) and contribution years."""
    p = params.get(year)
    monthly = X.avs_monthly(average_income, years, p) * X.early_late_factor(start_age, p)
    return {"monthly": monthly, "annual": monthly * p["avs"]["payments_per_year"],
            "full_scale_monthly": X.avs_full_monthly(average_income, p)}


@router.get("/calibration")
def calibration():
    """The engine against your real tax bills: same taxable income → same tax?"""
    s = db.get_settings()
    out = []
    with db.get_conn() as conn:
        bills = db.rows(conn.execute("SELECT * FROM tax_bills ORDER BY year DESC"))
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM members")}
    for b in bills:
        p = params.get(b["year"])
        married = b["member_id"] is None and s.get("civil_status") == "married"
        taxable = from_cents(b["taxable_income_cents"])
        rate_income = from_cents(b["rate_income_cents"]) or taxable
        icc_bill = from_cents(b["icc_cents"])
        row = {"id": b["id"], "year": b["year"], "member": names.get(b["member_id"], "Ménage"),
               "taxable_income": taxable, "rate_income": rate_income, "icc_bill": icc_bill,
               "ifd_bill": from_cents(b["ifd_cents"])}
        if taxable:
            base = T.vd_base_from_rate(taxable, T.floor_to(rate_income, 100), p)
            c, m = T.vd_coefficients(p, float(s["communal_coefficient"]), reduction=p["vd"]["income_reduction"])
            icc = round(base * c * 20) / 20 + round(base * m * 20) / 20
            wealth = T.vd_wealth_tax(from_cents(b["taxable_wealth_cents"]) or 0, married=married,
                                     communal_pct=float(s["communal_coefficient"]), p=p)["total"]
            row["icc_engine"] = icc + wealth
            if icc_bill:
                row["icc_gap_pct"] = (row["icc_engine"] - icc_bill) / icc_bill * 100
        out.append(row)
    return out


# --------------------------------------------------------------------------- gamification


@router.get("/game")
def game():
    return gamify.state(_safe_projection())


@router.post("/quests/{quest_id:path}/done")
def quest_done(quest_id: str):
    return gamify.complete_quest(quest_id)


# --------------------------------------------------------------------------- market data


@router.post("/market/refresh")
def market_refresh():
    """Fetch prices of all holdings, FX rates and the Swiss CPI now (normally daily)."""
    return market.refresh_all()


@router.get("/market/cpi")
def market_cpi():
    return market.latest_cpi() or {}


# --------------------------------------------------------------------------- backup


class BackupIn(BaseModel):
    passphrase: str


@router.post("/backup/export")
def backup_export(b: BackupIn):
    """Encrypted copy of the finance data, to move it to another computer."""
    try:
        name, blob = backup.export(b.passphrase)
    except backup.BackupError as e:
        raise HTTPException(422, str(e))
    return Response(blob, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/backup/import")
async def backup_import(file: UploadFile = File(...), passphrase: str = Form(...)):
    try:
        res = backup.restore(await file.read(), passphrase)
    except backup.BackupError as e:
        raise HTTPException(422, str(e))
    model._CACHE.clear()
    return res


# --------------------------------------------------------------------------- onboarding


class OnboardingIn(BaseModel):
    household: dict = {}
    members: list[dict] = []
    accounts: list[dict] = []
    budget: dict[str, float] = {}
    fire: dict = {}
    assumptions: dict = {}
    goals: list[dict] = []
    events: list[dict] = []
    child_costs: dict = {}


@router.post("/onboarding")
def onboarding(o: OnboardingIn):
    """Create the whole household in one go (the wizard's last step). `key`s link members to the rest."""
    keys: dict[str, int] = {}
    with db.get_conn() as conn:
        for m in o.members:
            if not m.get("name"):
                continue
            cur = conn.execute(
                "INSERT INTO members (name, emoji, kind, birth_date, expected, retire_age_target, meta) VALUES (?,?,?,?,?,?,?)",
                (m["name"].strip(), m.get("emoji") or "🙂", m.get("kind", "adult"), m.get("birth_date"),
                 int(bool(m.get("expected"))), m.get("retire_age_target"), _json(m.get("meta") or {})))
            keys[str(m.get("key") or m["name"])] = cur.lastrowid
        for m in o.members:
            mid = keys.get(str(m.get("key") or m.get("name")))
            if not mid:
                continue
            if m.get("kind") == "child" and m.get("birth_parent"):
                meta = {**(m.get("meta") or {}), "birth_parent_id": keys.get(str(m["birth_parent"]))}
                conn.execute("UPDATE members SET meta = ? WHERE id = ?", (_json(meta), mid))
            sal = m.get("salary") or {}
            if sal.get("gross_annual"):
                conn.execute("INSERT INTO incomes (member_id, kind, employer, gross_annual_cents, work_pct, "
                             "lpp_employee_annual_cents) VALUES (?, 'salary', ?, ?, ?, ?)",
                             (mid, sal.get("employer", ""), to_cents(sal["gross_annual"]), float(sal.get("work_pct") or 100),
                              opt_cents(sal.get("lpp_employee_annual"))))
            if m.get("side_income"):
                conn.execute("INSERT INTO incomes (member_id, kind, label, gross_annual_cents) VALUES (?, 'side', ?, ?)",
                             (mid, "Revenu annexe", to_cents(m["side_income"])))
        for a in o.accounts:
            if a.get("kind") not in metrics.ACCOUNT_KINDS or not a.get("name"):
                continue
            owners = {str(keys[k]): v for k, v in (a.get("owners") or {}).items() if k in keys}
            cur = conn.execute("INSERT INTO accounts (name, institution, kind, currency, owners, equity_pct, meta) "
                               "VALUES (?,?,?,?,?,?,?)", (a["name"], a.get("institution", ""), a["kind"],
                                                          (a.get("currency") or "CHF").upper(), _json(owners),
                                                          a.get("equity_pct"), _json(a.get("meta") or {})))
            if a.get("balance") not in (None, ""):
                conn.execute("INSERT INTO snapshots (account_id, date, balance_cents, source) VALUES (?, ?, ?, 'manual')",
                             (cur.lastrowid, date.today().isoformat(), to_cents(float(a["balance"]))))
        for cat, v in (o.budget or {}).items():
            if cat in CATEGORIES and v:
                conn.execute("INSERT INTO budget (category, monthly_cents) VALUES (?, ?) ON CONFLICT(category) DO UPDATE "
                             "SET monthly_cents = excluded.monthly_cents", (cat, to_cents(float(v))))
        for g in o.goals:
            if g.get("name") and g.get("target"):
                conn.execute("INSERT INTO goals (name, kind, emoji, target_cents, target_date) VALUES (?,?,?,?,?)",
                             (g["name"], g.get("kind", "custom"), g.get("emoji", "🎯"), to_cents(float(g["target"])),
                              g.get("target_date")))
        for e in o.events:
            if e.get("kind") in EVENT_KINDS:
                conn.execute("INSERT INTO events (kind, label, member_id, start_date, end_date, params) VALUES (?,?,?,?,?,?)",
                             (e["kind"], e.get("label", ""), keys.get(str(e.get("member"))) if e.get("member") else None,
                              e.get("start_date"), e.get("end_date"), _json(e.get("params") or {})))
    patch = {**(o.household or {}), "onboarding_done": True}
    if o.fire:
        patch["fire"] = o.fire
    if o.assumptions:
        patch["assumptions"] = o.assumptions
    if o.child_costs:
        patch["child_costs"] = o.child_costs
    db.save_settings(patch)
    model._CACHE.clear()
    proj = _safe_projection()
    new = gamify.check_badges(proj)
    return {"members": keys, "projection": {k: proj[k] for k in ("fi_year", "fi_ages", "progress")} if proj else None,
            "new_badges": new, "level": gamify.level_for(gamify.total_xp())}


@router.post("/reset")
def reset(confirm: str = ""):
    """Erase every finance record (asks `confirm=EFFACER`). The documents folder is emptied too."""
    if confirm != "EFFACER":
        raise HTTPException(400, "ajoutez ?confirm=EFFACER")
    with db.get_conn() as conn:
        for t in ("transactions", "snapshots", "holdings", "imports", "incomes", "events", "scenarios", "goals",
                  "budget", "budget_personal", "rules", "documents", "accounts", "members", "xp_events", "badges", "checkins",
                  "tax_bills", "settings"):
            conn.execute(f"DELETE FROM {t}")
    for f in db.docs_dir().glob("*"):
        if f.is_file():
            f.unlink()
    model._CACHE.clear()
    return {"reset": True}


def summary() -> dict:
    """Compact block for /api/summary and the home card."""
    s = db.get_settings()
    if not s.get("onboarding_done"):
        return {"onboarding_done": False}
    proj = _safe_projection()
    g = gamify.state(proj)
    return {
        "onboarding_done": True,
        "fi_year": (proj or {}).get("fi_year"),
        "fi_ages": (proj or {}).get("fi_ages"),
        "progress": (proj or {}).get("progress"),
        "altitude": g["altitude"]["meters"], "peak": g["altitude"]["last_peak"]["name"],
        "level": g["level"], "streak": g["streak"],
        "quests": [{"title": q["title"], "emoji": q["emoji"]} for q in g["quests"][:3]],
        "quest_count": len(g["quests"]),  # the tab badge: all open quests, not just the 3 shown
    }
