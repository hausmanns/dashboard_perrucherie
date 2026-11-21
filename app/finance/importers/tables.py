"""CSV / Excel exports: bank and neo-bank transactions, broker positions.

Swiss exports are messy: `;` separators, `1'234.50`, `dd.mm.yyyy`, a few
lines of account information before the header, UTF-8 with BOM, UTF-16 or
Windows-1252, debit and credit in two columns, amounts with a trailing minus.

Detection
---------
1. find the header row (the first row with ≥ 3 cells where a date-like and an
   amount-like column name appear);
2. give each column a role from its name, in FR / DE / EN / IT
   (date, amount, debit, credit, description, counterparty, currency, balance,
   state, quantity, symbol, isin, price, value…);
3. known profiles (Revolut, Wise, Neon, Interactive Brokers…) only adjust the
   details (which date, which rows to skip);
4. if no usable roles are found the table is returned as `kind: table` with its
   columns and first rows, and `documents.py` asks the LLM to map the columns.
"""
from __future__ import annotations

import csv
import io
import re
from decimal import Decimal

from ..categorize import normalize
from ..money import AmountError, parse_amount, parse_date

ROLE_WORDS: dict[str, list[str]] = {
    "date": ["booking date", "date de comptabilisation", "buchungsdatum", "completed date", "date",
             "datum", "data", "booked at", "trade date", "transaction date", "date d operation", "abschlussdatum"],
    "value_date": ["valuta", "value date", "date valeur", "date de valeur", "valuta date", "valeur", "started date"],
    "amount": ["amount", "montant", "betrag", "importo", "credit debit amount", "credit/debit amount",
               "individual amount", "transaction amount", "montant chf", "betrag chf", "betrag detail"],
    "debit": ["debit", "débit", "belastung", "lastschrift", "addebito", "sortie", "withdrawal", "paid out",
              "debit in chf", "débit en chf", "belastung chf"],
    "credit": ["credit", "crédit", "gutschrift", "accredito", "entree", "deposit", "paid in",
               "credit in chf", "crédit en chf", "gutschrift chf"],
    "description": ["description", "libelle", "libellé", "texte", "text", "buchungstext", "beschreibung",
                    "notification text", "texte de notification", "avisierungstext", "details", "descrizione",
                    "description1", "motif", "communication", "purpose", "verwendungszweck", "payment reference"],
    "counterparty": ["counterparty", "payee", "merchant", "beneficiaire", "bénéficiaire", "empfanger", "empfänger",
                     "auftraggeber", "contrepartie", "subject", "name", "sender", "recipient"],
    "currency": ["currency", "devise", "monnaie", "wahrung", "währung", "whg", "valuta currency", "ccy"],
    "balance": ["balance", "solde", "saldo", "running balance", "kontostand"],
    "state": ["state", "status", "statut"],
    "reference": ["reference", "référence", "referenz", "transaction no", "transaction id", "id", "transferwise id"],
    "fee": ["fee", "frais", "gebuhr", "gebühr"],
    # holdings
    "symbol": ["symbol", "ticker", "symbole", "valor", "valeur symbole"],
    "isin": ["isin", "symbol/isin", "symbole/isin"],
    "name": ["product", "produit", "instrument", "titre", "security", "wertschrift", "designation", "désignation",
             "description titre"],
    "quantity": ["quantity", "quantité", "quantite", "anzahl", "menge", "position", "nombre", "qty", "units", "parts"],
    "price": ["close price", "closing", "clôture", "cloture", "price", "cours", "kurs", "prix", "last price"],
    "value": ["value in chf", "valeur en chf", "market value", "value", "valeur", "wert", "montant local",
              "local value", "valeur en eur", "value in eur", "position value"],
    "asset_class": ["asset category", "asset class", "classe d actif", "type de titre", "kategorie"],
}

_DATE_ROLES = ("date", "value_date")


def _decode(data: bytes) -> str:
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:40])
    counts = {d: sample.count(d) for d in (";", ",", "\t", "|")}
    return max(counts, key=counts.get) if max(counts.values()) else ","


def _role(header: str) -> str | None:
    h = normalize(header)
    if not h:
        return None
    best, best_len = None, 0
    for role, words in ROLE_WORDS.items():
        for w in words:
            nw = normalize(w)
            if h == nw or (len(nw) >= 4 and (h.startswith(nw) or nw in h.split() or f" {nw} " in f" {h} ")):
                if len(nw) > best_len:
                    best, best_len = role, len(nw)
    return best


def _roles(header: list[str]) -> dict[str, int]:
    roles: dict[str, int] = {}
    for i, h in enumerate(header):
        r = _role(h)
        if r and r not in roles:
            roles[r] = i
    return roles


def _find_header(rows: list[list[str]]) -> int | None:
    for i, row in enumerate(rows[:40]):
        cells = [c for c in row if c.strip()]
        if len(cells) < 3:
            continue
        roles = _roles(row)
        if ("date" in roles or "value_date" in roles) and ({"amount", "debit", "credit"} & roles.keys()):
            return i
        if "quantity" in roles and ({"symbol", "isin", "name"} & roles.keys()):
            return i
    return None


def _decimal_sep(rows: list[list[str]], cols: list[int]) -> str | None:
    """Guess the file's decimal separator from the amount columns."""
    dot = comma = 0
    for r in rows[:200]:
        for c in cols:
            if c < len(r):
                v = r[c].strip()
                if re.search(r"\d,\d{2}$", v):
                    comma += 1
                elif re.search(r"\d\.\d{2}$", v):
                    dot += 1
    if comma > dot:
        return ","
    if dot:
        return "."
    return None


def _cell(row, idx):
    return row[idx].strip() if idx is not None and idx < len(row) else ""


def profile_of(header: list[str]) -> str:
    h = {normalize(x) for x in header}
    if {"type", "product", "started date", "completed date", "state"} <= h:
        return "revolut"
    if "transferwise id" in h or ({"running balance", "payment reference"} <= h):
        return "wise"
    if {"original amount", "original currency", "exchange rate", "subject"} <= h:
        return "neon"
    if "texte de notification" in h or "notification text" in h or "avisierungstext" in h:
        return "postfinance"
    if {"trade date", "booking date", "individual amount"} & h and "description1" in h:
        return "ubs"
    if "zkb referenz" in h:
        return "zkb"
    if "booked at" in h and "credit debit amount" in h:
        return "raiffeisen"
    return "generic"


def rows_to_parsed(rows: list[list[str]], engine_prefix: str = "csv") -> dict:
    ib = _ibkr(rows)
    if ib:
        return ib
    hi = _find_header(rows)
    if hi is None:
        return {"kind": "table", "engine": f"{engine_prefix}:unknown", "account": {},
                               "columns": rows[0] if rows else [], "rows": rows[1:30]}
    header = rows[hi]
    body = [r for r in rows[hi + 1:] if any(c.strip() for c in r)]
    roles = _roles(header)
    prof = profile_of(header)
    account = _preamble_account(rows[:hi])
    if "quantity" in roles and not ({"amount", "debit", "credit"} & roles.keys() and "date" in roles):
        return _holdings(header, body, roles, f"{engine_prefix}:{prof}", account)
    amount_cols = [roles[k] for k in ("amount", "debit", "credit", "balance") if k in roles]
    sep = _decimal_sep(body, amount_cols)
    txs = []
    skipped = 0
    for r in body:
        state = _cell(r, roles.get("state")).upper()
        if state and state not in ("COMPLETED", "TERMINÉ", "TERMINE", "ABGESCHLOSSEN", "BOOKED", "COMPTABILISÉ"):
            skipped += 1
            continue
        date_raw = _cell(r, roles.get("date")) or _cell(r, roles.get("value_date"))
        try:
            date = parse_date(date_raw)
        except ValueError:
            skipped += 1
            continue
        try:
            if "amount" in roles and _cell(r, roles["amount"]):
                amount = parse_amount(_cell(r, roles["amount"]), sep)
            else:
                deb = _cell(r, roles.get("debit"))
                cre = _cell(r, roles.get("credit"))
                amount = (parse_amount(cre, sep) if cre else Decimal(0)) - (abs(parse_amount(deb, sep)) if deb else Decimal(0))
        except AmountError:
            skipped += 1
            continue
        fee = _cell(r, roles.get("fee"))
        if fee and prof == "revolut":
            try:
                amount -= abs(parse_amount(fee, sep))
            except AmountError:
                pass
        if amount == 0:
            continue
        bal = _cell(r, roles.get("balance"))
        try:
            balance = parse_amount(bal, sep) if bal else None
        except AmountError:
            balance = None
        desc = _cell(r, roles.get("description"))
        if prof == "ubs":
            extra = [r[i].strip() for i, h in enumerate(header) if normalize(h) in ("description2", "description3")
                     and i < len(r) and r[i].strip()]
            desc = " ".join([desc] + extra)
        currency = (_cell(r, roles.get("currency")) or account.get("currency") or "CHF").upper()[:3]
        if prof == "neon":          # Neon: amounts are CHF, the currency column is the original one
            currency = "CHF"
        txs.append({
            "date": date, "amount": amount,
            "currency": currency,
            "description": desc, "counterparty": _cell(r, roles.get("counterparty")),
            "counterparty_iban": "", "reference": _cell(r, roles.get("reference")), "balance": balance,
        })
    balances = []
    with_bal = [t for t in txs if t.get("balance") is not None]
    if with_bal:
        last = max(with_bal, key=lambda t: t["date"])
        balances.append({"type": "closing", "date": last["date"], "amount": last["balance"],
                         "currency": last["currency"]})
    currencies = {t["currency"] for t in txs}
    if len(currencies) == 1 and not account.get("currency"):
        account["currency"] = next(iter(currencies))
    return {"kind": "transactions", "engine": f"{engine_prefix}:{prof}", "account": account,
            "transactions": txs, "balances": balances, "skipped": skipped,
            "columns": header, "roles": roles}


def _preamble_account(rows: list[list[str]]) -> dict:
    out = {}
    for r in rows:
        line = " ".join(r)
        m = re.search(r"\b(CH\d{2}[ ]?(?:[0-9A-Z]{4}[ ]?){4}[0-9A-Z]{1})\b", line.replace(" ", " "))
        if m and "iban" not in out:
            out["iban"] = m.group(1).replace(" ", "")
        m = re.search(r"\b(CHF|EUR|USD|GBP)\b", line)
        if m and "currency" not in out:
            out["currency"] = m.group(1)
    return out


def _holdings(header, body, roles, engine, account) -> dict:
    sep = _decimal_sep(body, [roles[k] for k in ("quantity", "price", "value") if k in roles])
    out = []
    for r in body:
        name = _cell(r, roles.get("name"))
        sym = _cell(r, roles.get("symbol"))
        isin = _cell(r, roles.get("isin"))
        if isin and not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", isin):
            if not sym:
                sym = isin
            isin = ""
        if not (name or sym or isin):
            continue
        try:
            qty = parse_amount(_cell(r, roles.get("quantity")), sep)
        except AmountError:
            continue
        def num(role):
            v = _cell(r, roles.get(role))
            try:
                return parse_amount(v, sep) if v else None
            except AmountError:
                return None
        cls = _cell(r, roles.get("asset_class")).lower()
        out.append({"symbol": sym, "isin": isin, "name": name or sym or isin, "quantity": qty,
                    "price": num("price"), "value": num("value"),
                    "currency": (_cell(r, roles.get("currency")) or "").upper()[:3],
                    "asset_class": "bonds" if "bond" in cls or "oblig" in cls else
                    "cash" if "cash" in cls else "crypto" if "crypto" in cls else "equity"})
    return {"kind": "holdings", "engine": engine, "account": account, "holdings": out}


def _ibkr(rows: list[list[str]]) -> dict | None:
    """Interactive Brokers activity statement: sections 'Open Positions', 'Net Asset Value'."""
    if not rows or not any(r and r[0] == "Statement" for r in rows[:5]):
        return None
    header = None
    out = []
    base_ccy = ""
    for r in rows:
        if len(r) > 3 and r[0] == "Account Information" and r[2] == "Base Currency":
            base_ccy = r[3]
        if r and r[0] == "Open Positions":
            if r[1] == "Header":
                header = r
            elif r[1] == "Data" and header and len(r) == len(header):
                rec = dict(zip(header, r))
                if rec.get("DataDiscriminator", "Summary") != "Summary":
                    continue
                try:
                    out.append({"symbol": rec.get("Symbol", ""), "isin": "", "name": rec.get("Symbol", ""),
                                "quantity": parse_amount(rec.get("Quantity", "0")),
                                "price": parse_amount(rec.get("Close Price", "0")),
                                "value": parse_amount(rec.get("Value", "0")),
                                "currency": rec.get("Currency", ""),
                                "asset_class": "bonds" if "Bond" in rec.get("Asset Category", "") else "equity"})
                except AmountError:
                    continue
    if not out:
        return None
    return {"kind": "holdings", "engine": "csv:ibkr", "account": {"currency": base_ccy, "institution": "Interactive Brokers"},
            "holdings": out}


def csv_rows(data: bytes) -> list[list[str]]:
    text = _decode(data)
    return list(csv.reader(io.StringIO(text), delimiter=_delimiter(text)))


def xlsx_rows(data: bytes) -> list[list[str]]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = []
    for row in ws.iter_rows(values_only=True):
        cells = []
        for v in row:
            if v is None:
                cells.append("")
            elif hasattr(v, "isoformat"):
                cells.append(v.isoformat()[:10])
            else:
                cells.append(str(v))
        rows.append(cells)
        if len(rows) > 20000:
            break
    return rows


def read_rows(filename: str, data: bytes) -> list[list[str]]:
    return xlsx_rows(data) if filename.lower().endswith((".xlsx", ".xlsm")) else csv_rows(data)


def parse_csv(data: bytes) -> dict:
    return rows_to_parsed(csv_rows(data), "csv")


def parse_xlsx(data: bytes) -> dict:
    return rows_to_parsed(xlsx_rows(data), "xlsx")


def apply_mapping(rows: list[list[str]], header_index: int, mapping: dict[str, int]) -> dict:
    """Parse a table with a column mapping chosen by the LLM (or the user)."""
    header = rows[header_index] if header_index < len(rows) else []
    fake = [""] * max(len(header), max(mapping.values(), default=0) + 1)
    names = {"date": "date", "amount": "amount", "debit": "debit", "credit": "credit",
             "description": "description", "counterparty": "counterparty", "currency": "currency",
             "balance": "balance"}
    for role, idx in mapping.items():
        if role in names and idx is not None and 0 <= int(idx) < len(fake):
            fake[int(idx)] = names[role]
    return rows_to_parsed([fake] + [r for r in rows[header_index + 1:]], "csv")
