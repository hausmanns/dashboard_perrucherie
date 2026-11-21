"""Readers for the files the household drops: deterministic first, LLM last.

`parse_file(filename, data)` returns a `Parsed` dict:

    kind         transactions | holdings | table | pdf | image | unknown
    engine       camt | csv:<profile> | xlsx:<profile> | pdf-text | …
    account      {iban, currency, institution, name} hints to pick the account
    transactions [{date, amount: Decimal, currency, description, counterparty,
                   counterparty_iban, reference, balance}]
    balances     [{date, amount: Decimal, type}]   (opening / closing)
    holdings     [{symbol, isin, name, quantity, price, currency, value, asset_class}]
    columns / rows  for an unknown table (the LLM maps the columns)
    text / pages    for a PDF (text per page; scanned pages rendered to PNG)

Nothing is written here; `documents.py` decides what to apply.
"""
from __future__ import annotations

import hashlib
from collections import Counter

from ..categorize import normalize
from . import camt, pdf, tables


def parse_file(filename: str, data: bytes) -> dict:
    name = (filename or "").lower()
    head = data[:2048].lstrip()
    if name.endswith((".xml", ".camt", ".053", ".054")) or (head.startswith(b"<?xml") and b"camt.05" in data[:4096]):
        return camt.parse(data)
    if name.endswith(".pdf") or head.startswith(b"%PDF"):
        return pdf.parse(data)
    if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".heic")):
        mime = "image/png" if name.endswith(".png") else "image/webp" if name.endswith(".webp") else "image/jpeg"
        return {"kind": "image", "engine": "image", "images": [(data, mime)], "account": {}}
    if name.endswith((".xlsx", ".xlsm")):
        return tables.parse_xlsx(data)
    if name.endswith((".csv", ".txt", ".tsv")) or b";" in head or b"," in head:
        return tables.parse_csv(data)
    return {"kind": "unknown", "engine": "", "account": {}}


def fingerprints(transactions: list[dict]) -> list[str]:
    """Stable dedup keys. Two identical lines on the same day (two coffees) get #0 and #1,
    so re-importing an overlapping statement adds nothing and loses nothing."""
    seen: Counter = Counter()
    out = []
    for t in transactions:
        cents = int((t["amount"] * 100).to_integral_value())
        base = f'{t["date"]}|{cents}|{normalize(t.get("description", ""))[:80]}|{t.get("reference") or ""}'
        k = seen[base]
        seen[base] += 1
        out.append(hashlib.sha1(f"{base}#{k}".encode()).hexdigest()[:24])
    return out
