"""Writing transactions: import with de-duplication, categorisation, learning rules."""
from __future__ import annotations

import logging
from decimal import Decimal

from . import db
from .categorize import CATEGORIES, categorize, llm_prompt, normalize, parse_llm_answer, suggest_pattern
from .importers import fingerprints
from .money import to_cents

log = logging.getLogger(__name__)


def _rules(conn) -> list[dict]:
    return db.rows(conn.execute("SELECT * FROM rules ORDER BY length(pattern) DESC"))


def own_ibans(conn) -> set[str]:
    return {r["iban"].replace(" ", "").upper() for r in conn.execute("SELECT iban FROM accounts WHERE iban != ''")}


def import_transactions(account_id: int, txs: list[dict], document_id: int | None = None) -> dict:
    """Insert new transactions (existing fingerprints are skipped), categorised on the way."""
    fps = fingerprints(txs)
    new = dup = 0
    with db.get_conn() as conn:
        acc = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not acc:
            raise ValueError("compte inconnu")
        rules = _rules(conn)
        ibans = own_ibans(conn) - {acc["iban"].replace(" ", "").upper()}
        cur = conn.execute("INSERT INTO imports (document_id, account_id, rows_total) VALUES (?, ?, ?)",
                           (document_id, account_id, len(txs)))
        import_id = cur.lastrowid
        for t, fp in zip(txs, fps):
            amount = t["amount"] if isinstance(t["amount"], Decimal) else Decimal(str(t["amount"]))
            cat, src, transfer = categorize(t.get("description", ""), t.get("counterparty", ""), float(amount),
                                            rules, ibans)
            if t.get("counterparty_iban") and t["counterparty_iban"].replace(" ", "").upper() in ibans:
                cat, src, transfer = "virement", "builtin", True
            try:
                conn.execute(
                    "INSERT INTO transactions (account_id, date, amount_cents, currency, description, counterparty, "
                    "category, category_source, is_transfer, import_id, fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (account_id, t["date"], to_cents(amount), (t.get("currency") or acc["currency"]).upper(),
                     (t.get("description") or "")[:500], (t.get("counterparty") or "")[:200], cat, src,
                     int(transfer), import_id, fp))
                new += 1
            except Exception as e:  # sqlite3.IntegrityError → duplicate
                if "UNIQUE" in str(e):
                    dup += 1
                else:
                    raise
        conn.execute("UPDATE imports SET rows_new = ?, rows_duplicate = ? WHERE id = ?", (new, dup, import_id))
    return {"import_id": import_id, "total": len(txs), "new": new, "duplicate": dup}


def recategorize() -> int:
    """Re-run rules + built-ins on every transaction not categorised by hand (after a new rule).
    Priority: the household's rules > built-in merchants > the LLM's guess > nothing."""
    n = 0
    with db.get_conn() as conn:
        rules = _rules(conn)
        ibans = own_ibans(conn)
        for t in db.rows(conn.execute("SELECT * FROM transactions WHERE category_source != 'manual'")):
            cat, src, transfer = categorize(t["description"], t["counterparty"], t["amount_cents"] / 100, rules, ibans)
            if not cat or (cat == t["category"] and src == t["category_source"]):
                continue
            conn.execute("UPDATE transactions SET category = ?, category_source = ?, is_transfer = ? WHERE id = ?",
                         (cat, src, int(transfer), t["id"]))
            n += 1
    return n


def set_category(tx_id: int, category: str, *, learn: bool = True, is_transfer: bool | None = None) -> dict:
    """Manual category; optionally remember the merchant as a rule and apply it to similar lines."""
    if category and category not in CATEGORIES:
        raise ValueError("catégorie inconnue")
    with db.get_conn() as conn:
        t = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if not t:
            raise KeyError(tx_id)
        transfer = is_transfer if is_transfer is not None else CATEGORIES.get(category, {}).get("type") == "transfer"
        conn.execute("UPDATE transactions SET category = ?, category_source = 'manual', is_transfer = ? WHERE id = ?",
                     (category, int(transfer), tx_id))
        pattern = suggest_pattern(t["description"], t["counterparty"]) if learn else ""
        applied = 0
        if pattern and category:
            conn.execute("INSERT INTO rules (pattern, category, is_transfer, origin) VALUES (?, ?, ?, 'learned') "
                         "ON CONFLICT(pattern) DO UPDATE SET category = excluded.category, "
                         "is_transfer = excluded.is_transfer", (pattern, category, int(transfer)))
    if pattern and category:
        applied = recategorize()
    return {"id": tx_id, "category": category, "rule": pattern or None, "reapplied": applied}


def llm_categorize(settings: dict, limit: int = 200) -> dict:
    """Ask the LLM for the categories still unknown (descriptions only — no balances)."""
    from .. import llm

    with db.get_conn() as conn:
        todo = db.rows(conn.execute(
            "SELECT id, description, counterparty, amount_cents FROM transactions "
            "WHERE category = '' ORDER BY date DESC LIMIT ?", (limit,)))
    if not todo:
        return {"done": 0, "left": 0}
    done = 0
    engine = ""
    for start in range(0, len(todo), 40):
        batch = todo[start:start + 40]
        items = [{"amount": b["amount_cents"] / 100, "description": b["description"],
                  "counterparty": b["counterparty"]} for b in batch]
        try:
            data, engine = llm.chat_json(llm.text_message(llm_prompt(items)), settings)
        except llm.LLMError as e:
            return {"done": done, "error": str(e), "left": len(todo) - done}
        answer = parse_llm_answer(data, len(batch))
        with db.get_conn() as conn:
            for i, cat in answer.items():
                transfer = CATEGORIES[cat]["type"] == "transfer"
                conn.execute("UPDATE transactions SET category = ?, category_source = 'llm', is_transfer = ? "
                             "WHERE id = ? AND category = ''", (cat, int(transfer), batch[i]["id"]))
                done += 1
    return {"done": done, "engine": engine, "left": len(todo) - done}


def undo_import(import_id: int) -> int:
    with db.get_conn() as conn:
        n = conn.execute("DELETE FROM transactions WHERE import_id = ?", (import_id,)).rowcount
        conn.execute("DELETE FROM imports WHERE id = ?", (import_id,))
    return n


__all__ = ["import_transactions", "recategorize", "set_category", "llm_categorize", "undo_import", "normalize"]
