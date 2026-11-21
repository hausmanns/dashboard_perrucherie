"""ISO 20022 bank statements: camt.053 (statement), camt.052 (report), camt.054 (notification).

Every Swiss bank exports camt.053 (« relevé électronique », « e-statement
XML »). It is the most reliable import: exact amounts, IBAN, opening and
closing balances. Namespaces vary by version, so tags are matched by local name.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from decimal import Decimal

from ..money import parse_date


def _ln(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, *path):
    cur = el
    for name in path:
        if cur is None:
            return None
        cur = next((c for c in cur if _ln(c.tag) == name), None)
    return cur


def _children(el, name):
    return [c for c in (el if el is not None else []) if _ln(c.tag) == name]


def _text(el, *path) -> str:
    node = _child(el, *path)
    return (node.text or "").strip() if node is not None and node.text else ""


def _date(el) -> str:
    if el is None:
        return ""
    for name in ("Dt", "DtTm"):
        t = _text(el, name)
        if t:
            return parse_date(t[:10])
    return ""


def _signed(amount: str, indicator: str) -> Decimal:
    d = Decimal(amount)
    return -d if indicator == "DBIT" else d


def parse(data: bytes) -> dict:
    root = ET.fromstring(data)
    stmts = [el for el in root.iter() if _ln(el.tag) in ("Stmt", "Rpt", "Ntfctn")]
    out = {"kind": "transactions", "engine": "camt", "account": {}, "transactions": [], "balances": []}
    for st in stmts:
        acct = _child(st, "Acct")
        iban = _text(acct, "Id", "IBAN") or _text(acct, "Id", "Othr", "Id")
        ccy = _text(acct, "Ccy")
        owner = _text(acct, "Ownr", "Nm")
        svcr = _text(acct, "Svcr", "FinInstnId", "Nm") or _text(acct, "Svcr", "FinInstnId", "BICFI") or \
            _text(acct, "Svcr", "FinInstnId", "BIC")
        if iban and not out["account"]:
            out["account"] = {"iban": iban.replace(" ", ""), "currency": ccy or "CHF", "owner": owner,
                              "institution": svcr}
        for bal in _children(st, "Bal"):
            code = _text(bal, "Tp", "CdOrPrtry", "Cd")
            amt = _child(bal, "Amt")
            if amt is None or not amt.text:
                continue
            out["balances"].append({
                "type": {"OPBD": "opening", "PRCD": "opening", "CLBD": "closing", "CLAV": "available"}.get(code, code),
                "date": _date(_child(bal, "Dt")),
                "amount": _signed(amt.text.strip(), _text(bal, "CdtDbtInd")),
                "currency": amt.get("Ccy", ccy),
            })
        for ntry in _children(st, "Ntry"):
            if _text(ntry, "Sts") in ("PDNG", "INFO") or _text(ntry, "Sts", "Cd") in ("PDNG", "INFO"):
                continue
            amt_el = _child(ntry, "Amt")
            ind = _text(ntry, "CdtDbtInd")
            date = _date(_child(ntry, "BookgDt")) or _date(_child(ntry, "ValDt"))
            info = _text(ntry, "AddtlNtryInf")
            ref = _text(ntry, "AcctSvcrRef") or _text(ntry, "NtryRef")
            details = [tx for nd in _children(ntry, "NtryDtls") for tx in _children(nd, "TxDtls")]
            lines = []
            for tx in details:
                tx_amt = _child(tx, "AmtDtls", "TxAmt", "Amt")
                if tx_amt is None:
                    tx_amt = _child(tx, "Amt")
                tx_ind = _text(tx, "CdtDbtInd") or ind
                party = "Cdtr" if tx_ind == "DBIT" else "Dbtr"
                name = _text(tx, "RltdPties", party, "Nm") or _text(tx, "RltdPties", party, "Pty", "Nm")
                piban = _text(tx, "RltdPties", party + "Acct", "Id", "IBAN")
                rmt = _child(tx, "RmtInf")
                ustrd = " ".join(t.text.strip() for t in (rmt if rmt is not None else [])
                                 if _ln(t.tag) == "Ustrd" and t.text)
                addtl = _text(tx, "AddtlTxInf")
                lines.append({"amount_el": tx_amt, "ind": tx_ind, "name": name, "iban": piban,
                              "text": " ".join(x for x in (addtl, ustrd) if x),
                              "ref": _text(tx, "Refs", "AcctSvcrRef") or _text(tx, "Refs", "EndToEndId")})
            split = len(lines) > 1 and all(l["amount_el"] is not None for l in lines)
            if split:
                total = sum(_signed(l["amount_el"].text.strip(), l["ind"]) for l in lines)
                split = total == _signed(amt_el.text.strip(), ind)
            if split:
                for l in lines:
                    out["transactions"].append({
                        "date": date, "amount": _signed(l["amount_el"].text.strip(), l["ind"]),
                        "currency": l["amount_el"].get("Ccy", ccy or "CHF"),
                        "description": l["text"] or info, "counterparty": l["name"],
                        "counterparty_iban": l["iban"], "reference": l["ref"] or ref,
                    })
            else:
                first = lines[0] if lines else {}
                out["transactions"].append({
                    "date": date, "amount": _signed(amt_el.text.strip(), ind),
                    "currency": amt_el.get("Ccy", ccy or "CHF"),
                    "description": " ".join(x for x in (info, first.get("text", "")) if x).strip(),
                    "counterparty": first.get("name", ""), "counterparty_iban": first.get("iban", ""),
                    "reference": ref,
                })
    return out
