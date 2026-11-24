"""Money parsing, file readers (CAMT, CSV profiles, IBKR), dedup and categorisation."""
from decimal import Decimal

import pytest

from app.finance import categorize as C
from app.finance.importers import fingerprints, parse_file
from app.finance.importers import tables
from app.finance.money import AmountError, floor_5ct, floor_to, parse_amount, parse_date, round_5ct, to_cents


@pytest.mark.parametrize("raw,expected", [
    ("1'234.50", "1234.50"), ("1’234.50", "1234.50"), ("-1'234.50", "-1234.50"), ("1234.50-", "-1234.50"),
    ("(12.00)", "-12.00"), ("CHF 12.50", "12.50"), ("12.–", "12"), ("12.-", "12"), ("1.234,56", "1234.56"),
    ("1,234.56", "1234.56"), ("12,5", "12.5"), ("1 234.50", "1234.50"), ("−45.10", "-45.10"), ("+7", "7"),
    ("1,234", "1234"), ("1.234.567", "1234567"), ("0.05", "0.05"),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


def test_parse_amount_forced_separator_and_errors():
    assert parse_amount("1.234", ",") == Decimal("1234")
    with pytest.raises(AmountError):
        parse_amount("abc")
    with pytest.raises(AmountError):
        parse_amount("")


def test_cents_and_rounding():
    assert to_cents("10.005") == 1001          # half-up
    assert to_cents(0.1 + 0.2) == 30
    assert floor_5ct(2684.3499999999995) == 2684.35   # float noise never drops a step
    assert floor_5ct(29.26) == 29.25
    assert round_5ct(10.024) == 10.0 and round_5ct(10.025) == 10.05
    assert floor_to(18499.999999, 100) == 18500 - 100 or floor_to(18499.999999, 100) == 18500
    assert floor_to(100_099, 100) == 100_000


@pytest.mark.parametrize("raw,iso", [
    ("03.11.2026", "2026-11-03"), ("3.11.26", "2026-11-03"), ("2026-11-03", "2026-11-03"),
    ("2026-11-03 14:22:10", "2026-11-03"), ("03/11/2026", "2026-11-03"), ("3 nov. 2026", "2026-11-03"),
    ("Nov 3, 2026", "2026-11-03"), ("2026-11-03T08:00:00+01:00", "2026-11-03"),
])
def test_parse_date(raw, iso):
    assert parse_date(raw) == iso


CAMT = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.04">
 <BkToCstmrStmt><GrpHdr><MsgId>1</MsgId></GrpHdr>
  <Stmt><Id>S1</Id>
   <Acct><Id><IBAN>CH9300762011623852957</IBAN></Id><Ccy>CHF</Ccy><Ownr><Nm>Seb</Nm></Ownr>
     <Svcr><FinInstnId><Nm>Banque Cantonale Vaudoise</Nm></FinInstnId></Svcr></Acct>
   <Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="CHF">1000.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-10-31</Dt></Dt></Bal>
   <Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="CHF">6252.35</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-11-30</Dt></Dt></Bal>
   <Ntry><Amt Ccy="CHF">7500.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts>BOOK</Sts><BookgDt><Dt>2026-11-25</Dt></BookgDt>
     <AddtlNtryInf>Salaire novembre</AddtlNtryInf>
     <NtryDtls><TxDtls><RltdPties><Dbtr><Nm>EMPLOYEUR SA</Nm></Dbtr></RltdPties></TxDtls></NtryDtls></Ntry>
   <Ntry><Amt Ccy="CHF">2100.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts>BOOK</Sts><BookgDt><Dt>2026-11-01</Dt></BookgDt>
     <AddtlNtryInf>Ordre permanent</AddtlNtryInf>
     <NtryDtls><TxDtls><RltdPties><Cdtr><Nm>Gerance Robert SA</Nm></Cdtr></RltdPties>
       <RmtInf><Ustrd>Loyer novembre</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
   <Ntry><Amt Ccy="CHF">147.65</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts>BOOK</Sts><BookgDt><Dt>2026-11-05</Dt></BookgDt>
     <AddtlNtryInf>Achats carte</AddtlNtryInf>
     <NtryDtls>
      <TxDtls><AmtDtls><TxAmt><Amt Ccy="CHF">97.65</Amt></TxAmt></AmtDtls><CdtDbtInd>DBIT</CdtDbtInd>
        <RltdPties><Cdtr><Nm>COOP-1234 LAUSANNE</Nm></Cdtr></RltdPties></TxDtls>
      <TxDtls><AmtDtls><TxAmt><Amt Ccy="CHF">50.00</Amt></TxAmt></AmtDtls><CdtDbtInd>DBIT</CdtDbtInd>
        <RltdPties><Cdtr><Nm>CFF</Nm></Cdtr></RltdPties></TxDtls>
     </NtryDtls></Ntry>
   <Ntry><Amt Ccy="CHF">9.99</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts>PDNG</Sts><BookgDt><Dt>2026-11-30</Dt></BookgDt></Ntry>
  </Stmt></BkToCstmrStmt></Document>"""


def test_camt053():
    r = parse_file("releve.xml", CAMT)
    assert r["engine"] == "camt"
    assert r["account"]["iban"] == "CH9300762011623852957"
    assert r["account"]["institution"] == "Banque Cantonale Vaudoise"
    txs = r["transactions"]
    assert len(txs) == 4                                    # pending entry ignored, card batch split in two
    assert sum(t["amount"] for t in txs) == Decimal("7500") - Decimal("2100") - Decimal("147.65")
    rent = next(t for t in txs if t["amount"] == Decimal("-2100.00"))
    assert rent["counterparty"] == "Gerance Robert SA" and "Loyer" in rent["description"]
    assert {b["type"]: b["amount"] for b in r["balances"]} == {"opening": Decimal("1000.00"), "closing": Decimal("6252.35")}
    # opening + movements = closing
    assert Decimal("1000.00") + sum(t["amount"] for t in txs) == Decimal("6252.35")


REVOLUT = b"""Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
CARD_PAYMENT,Current,2026-11-02 12:01:10,2026-11-03 09:00:00,Migros,-23.40,0.00,CHF,COMPLETED,476.60
TOPUP,Current,2026-11-01 08:00:00,2026-11-01 08:00:01,Top-Up by *1234,500.00,0.00,CHF,COMPLETED,500.00
CARD_PAYMENT,Current,2026-11-04 10:00:00,,Netflix,-15.90,0.00,CHF,PENDING,
EXCHANGE,Current,2026-11-05 10:00:00,2026-11-05 10:00:00,Exchanged to EUR,-100.00,0.50,CHF,COMPLETED,376.10
"""


def test_revolut_csv():
    r = parse_file("account-statement.csv", REVOLUT)
    assert r["engine"] == "csv:revolut"
    txs = r["transactions"]
    assert len(txs) == 3                                    # pending skipped
    assert txs[0]["date"] == "2026-11-03"                    # completed date
    exch = next(t for t in txs if "Exchanged" in t["description"])
    assert exch["amount"] == Decimal("-100.50")             # fee included
    assert r["balances"][0]["amount"] == Decimal("376.10")


POSTFINANCE = """Date de début:;01.11.2026
Date de fin:;30.11.2026
Compte:;CH21 0900 0000 1234 5678 9
Monnaie:;CHF
Date;Texte de notification;Crédit en CHF;Débit en CHF;Valeur;Solde en CHF
30.11.2026;ACHAT/SERVICE CARTE COOP PRONTO;;12.50;30.11.2026;4'987.50
25.11.2026;CRÉDIT SALAIRE EMPLOYEUR;5'000.00;;25.11.2026;5'000.00

Disclaimer:
Ce n'est pas un document officiel.
""".encode("cp1252")


def test_postfinance_csv_with_preamble_and_cp1252():
    r = parse_file("export.csv", POSTFINANCE)
    assert r["engine"] == "csv:postfinance"
    assert r["account"]["iban"] == "CH2109000000123456789"
    amounts = sorted(t["amount"] for t in r["transactions"])
    assert amounts == [Decimal("-12.50"), Decimal("5000.00")]
    assert r["balances"][0]["amount"] == Decimal("4987.50")


GENERIC_DE = """Buchungsdatum;Valuta;Buchungstext;Belastung;Gutschrift;Saldo
01.11.2026;01.11.2026;Swisscom Rechnung;89.00;;1911.00
02.11.2026;02.11.2026;Lohn Oktober;;6200.00;8111.00
""".encode("utf-8-sig")


def test_generic_german_debit_credit_columns():
    r = parse_file("konto.csv", GENERIC_DE)
    txs = r["transactions"]
    assert [t["amount"] for t in txs] == [Decimal("-89.00"), Decimal("6200.00")]
    assert txs[0]["description"] == "Swisscom Rechnung"


IBKR = """Statement,Header,Field Name,Field Value
Statement,Data,Title,Activity Statement
Account Information,Data,Base Currency,CHF
Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code
Open Positions,Data,Summary,Stocks,USD,VT,120,1,95.2,11424,118.5,14220,2796,
Open Positions,Data,Summary,Stocks,CHF,CHSPI,30,1,150,4500,165.2,4956,456,
Open Positions,Total,,Stocks,CHF,,,,,,,19176,,
""".encode()


def test_ibkr_positions():
    r = parse_file("U123_activity.csv", IBKR)
    assert r["kind"] == "holdings" and r["engine"] == "csv:ibkr"
    assert [(h["symbol"], h["quantity"]) for h in r["holdings"]] == [("VT", Decimal("120")), ("CHSPI", Decimal("30"))]


DEGIRO = """Produit,Symbole/ISIN,Quantité,Clôture,Montant local,Valeur en EUR
VANGUARD FTSE AW,IE00BK5BQT80,50,"110,20","EUR 5510,00","5510,00"
CASH & CASH FUND,,,,"EUR 120,00","120,00"
""".encode()


def test_degiro_portfolio():
    r = parse_file("Portfolio.csv", DEGIRO)
    assert r["kind"] == "holdings"
    h = r["holdings"][0]
    assert h["isin"] == "IE00BK5BQT80" and h["quantity"] == Decimal("50") and h["price"] == Decimal("110.20")


def test_swiss_positions_with_a_valor_column():
    """A Valor (Swiss security number) is not a Yahoo ticker: it gets its own place."""
    r = parse_file("depot.csv", "Désignation;Valor;Quantité;Cours\nACME N;1'234'567;4;50.00\n".encode())
    assert r["kind"] == "holdings"
    h = r["holdings"][0]
    assert (h["symbol"], h["valor"], h["name"], h["quantity"]) == ("", "1234567", "ACME N", Decimal("4"))


def test_unknown_table_is_returned_for_llm_mapping():
    r = parse_file("weird.csv", b"a;b;c\n1;2;3\n4;5;6\n")
    assert r["kind"] == "table" and r["columns"] == ["a", "b", "c"]
    mapped = tables.apply_mapping([["Quand", "Quoi", "Combien"], ["01.11.2026", "Coop", "-12.30"]], 0,
                                  {"date": 0, "description": 1, "amount": 2})
    assert mapped["transactions"][0]["amount"] == Decimal("-12.30")


def test_fingerprints_keep_identical_lines_apart_but_stable():
    t = {"date": "2026-11-03", "amount": Decimal("-4.50"), "description": "Café", "reference": ""}
    a = fingerprints([t, dict(t)])
    assert a[0] != a[1]
    assert fingerprints([t, dict(t)]) == a                 # same file again → same keys → no duplicates


@pytest.mark.parametrize("desc,cp,amount,cat", [
    ("ACHAT CARTE COOP-1234 LAUSANNE", "", -45.2, "alimentation"),
    ("TWINT *Migros Sevelin", "", -12, "alimentation"),
    ("Abonnement", "Swisscom (Suisse) SA", -89, "telecom"),
    ("Prime LAMal", "Groupe Mutuel", -480, "sante"),
    ("Salaire octobre", "Employeur SA", 6500, "salaire"),
    ("Virement VIAC 3a", "", -605, "epargne"),
    ("ADMINISTRATION CANTONALE DES IMPOTS acompte", "", -1200, "impots"),
    ("Loyer novembre", "Gérance Robert", -2100, "logement"),
    ("Something unknown", "", -10, ""),
])
def test_builtin_categories(desc, cp, amount, cat):
    assert C.categorize(desc, cp, amount, [])[0] == cat


def test_user_rule_wins_and_transfer_detection():
    rules = [{"pattern": "coop", "category": "restaurants", "is_transfer": 0}]
    assert C.categorize("COOP Restaurant", "", -20, rules)[:2] == ("restaurants", "rule")
    cat, src, transfer = C.categorize("Virement", "CH93 0076 2011 6238 5295 7", -500, [],
                                      {"CH9300762011623852957"})
    assert (cat, transfer) == ("virement", True)


def test_suggest_pattern_drops_noise():
    assert C.suggest_pattern("ACHAT CARTE 03.11 COOP-1234 LAUSANNE", "") == "coop"
    assert C.suggest_pattern("x", "Groupe Mutuel Assurances") == "groupe mutuel assurances"


def test_llm_answer_parsing_ignores_invented_categories():
    assert C.parse_llm_answer({"items": [{"i": 0, "category": "alimentation"}, {"i": 1, "category": "luxe"},
                                         {"i": 9, "category": "voyages"}]}, 2) == {0: "alimentation"}
