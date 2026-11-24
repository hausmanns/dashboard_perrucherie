"""End-to-end through the HTTP API (no startup events: no scheduler, no Telegram)."""
import base64
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from tests.finance.test_import import CAMT


@pytest.fixture()
def client(fresh_db, monkeypatch):
    from app import main
    from app.finance import documents, model

    model._CACHE.clear()
    monkeypatch.setattr(documents, "parse_async", lambda doc_id: documents.parse(doc_id))
    return TestClient(main.app)


ONBOARD = {
    "household": {"civil_status": "unmarried", "commune": "Lausanne", "communal_coefficient": 78.5},
    "members": [
        {"key": "s", "name": "Seb", "emoji": "🦊", "kind": "adult", "birth_date": "1993-04-02",
         "salary": {"gross_annual": 110_000, "work_pct": 100}},
        {"key": "l", "name": "Lea", "emoji": "🦉", "kind": "adult", "birth_date": "1994-08-15",
         "salary": {"gross_annual": 90_000, "work_pct": 80}},
        {"key": "b", "name": "Bébé", "emoji": "👶", "kind": "child", "birth_date": "2027-03-01", "expected": True,
         "birth_parent": "l"},
    ],
    "accounts": [
        {"name": "BCV courant", "kind": "current", "owners": {"s": 0.5, "l": 0.5}, "balance": 10_000},
        {"name": "Épargne Seb", "kind": "savings", "owners": {"s": 1}, "balance": 40_000},
        {"name": "IBKR", "kind": "invest", "owners": {"s": 1}, "balance": 80_000, "equity_pct": 100},
        {"name": "3a Lea", "kind": "pillar3a", "owners": {"l": 1}, "balance": 30_000},
        {"name": "LPP Seb", "kind": "lpp", "owners": {"s": 1}, "balance": 70_000},
    ],
    "budget": {"logement": 2400, "alimentation": 900, "sante": 900, "loisirs": 400, "transport": 200,
               "impots": 1500},
    "fire": {"style": "normal"},
    "events": [{"kind": "part_time", "member": "l", "start_date": "2027-06-01", "end_date": "2030-12-31",
                "params": {"work_pct": 60}}],
}


def onboard(client):
    r = client.post("/api/finance/onboarding", json=ONBOARD)
    assert r.status_code == 200, r.text
    return r.json()


def test_onboarding_overview_projection(client):
    res = onboard(client)
    assert set(res["members"]) == {"s", "l", "b"}
    assert "onboarding" in res["new_badges"]
    ov = client.get("/api/finance/overview").json()
    nw = ov["net_worth"]
    assert nw["total"] == pytest.approx(10_000 + 40_000 + 80_000 + 30_000 + 70_000)
    assert nw["liquid"] == pytest.approx(130_000)
    assert nw["pension"] == pytest.approx(100_000)
    # budget excludes taxes from the living costs (the engine computes them)
    assert ov["spending_profile"]["annual"] == pytest.approx((2400 + 900 + 900 + 400 + 200) * 12)
    assert ov["spending_profile"]["rent"] == pytest.approx(2400 * 12)
    assert ov["freedom_days"] == pytest.approx(130_000 / ((2400 + 900 + 900 + 400 + 200) * 12 / 365))
    proj = client.get("/api/finance/projection").json()
    assert proj["ready"] and proj["fi_year"] and proj["fi_year"] >= date.today().year
    assert proj["years"][0]["year"] == date.today().year
    assert any(m["kind"] == "birth" for m in proj["milestones"])
    # individual view: Seb alone with his share
    seb_id = res["members"]["s"]
    ind = client.get(f"/api/finance/overview?scope=member:{seb_id}").json()
    assert ind["net_worth"]["liquid"] == pytest.approx(5_000 + 40_000 + 80_000)
    assert client.get(f"/api/finance/projection?scope=member:{seb_id}").json()["ready"]
    mc = client.get("/api/finance/montecarlo?runs=100").json()
    assert 0 <= mc["plan"]["success_rate"] <= 1 and mc["curve"]


def test_checkin_awards_xp_and_streak(client):
    onboard(client)
    accts = client.get("/api/finance/checkin").json()["accounts"]
    cur = next(a for a in accts if a["name"] == "BCV courant")
    r = client.post("/api/finance/checkin", json={"balances": [{"account_id": cur["id"], "balance": 13_000}]}).json()
    assert r["change"] == pytest.approx(3_000)
    assert r["streak"]["this_month_done"] and r["streak"]["months"] == 1
    assert any(b["code"] == "first_checkin" for b in r["new_badges"])
    game = client.get("/api/finance/game").json()
    assert game["level"]["xp"] >= 300
    assert not any(q["kind"] == "checkin" for q in game["quests"])


def test_camt_upload_apply_categorize_learn(client):
    onboard(client)
    acc = client.post("/api/finance/accounts", json={"name": "BCV Seb", "kind": "current",
                                                     "iban": "CH93 0076 2011 6238 5295 7"}).json()
    docs = client.post("/api/finance/documents", files=[("files", ("releve.xml", CAMT, "application/xml"))]).json()
    d = client.get(f"/api/finance/documents/{docs[0]['id']}").json()
    assert d["status"] == "review" and d["doc_type"] == "bank_statement"
    assert d["extracted"]["account_id"] == acc["id"]              # matched by IBAN
    applied = client.post(f"/api/finance/documents/{d['id']}/apply", json={}).json()
    assert applied["result"]["import"]["new"] == 4
    acc2 = client.get(f"/api/finance/accounts/{acc['id']}").json()
    assert acc2["value"] == pytest.approx(6252.35)                 # closing balance snapshot
    # same file again → recognised, nothing duplicated
    again = client.post("/api/finance/documents", files=[("files", ("copie.xml", CAMT, "application/xml"))]).json()
    assert again[0]["duplicate"]
    txs = client.get(f"/api/finance/transactions?account_id={acc['id']}").json()["items"]
    cats = {t["counterparty"]: t["category"] for t in txs}
    assert cats["COOP-1234 LAUSANNE"] == "alimentation" and cats["CFF"] == "transport"
    assert cats["EMPLOYEUR SA"] == "salaire"
    # a correction becomes a rule
    rent = next(t for t in txs if t["amount"] == -2100)
    r = client.put(f"/api/finance/transactions/{rent['id']}", json={"category": "logement", "learn": True}).json()
    assert r["category"] == "logement" and r["rule"]
    assert client.get("/api/finance/rules").json()
    cf = client.get("/api/finance/cashflow?months=3").json()
    assert cf["income"] >= 7500 and cf["spending"] >= 2100


def test_import_is_idempotent(client):
    from decimal import Decimal
    from app.finance import ledger

    onboard(client)
    acc = client.get("/api/finance/accounts").json()[0]
    txs = [{"date": "2026-11-03", "amount": Decimal("-4.50"), "description": "Café"},
           {"date": "2026-11-03", "amount": Decimal("-4.50"), "description": "Café"}]
    assert ledger.import_transactions(acc["id"], txs)["new"] == 2
    second = ledger.import_transactions(acc["id"], txs)
    assert second["new"] == 0 and second["duplicate"] == 2


def test_budget_goals(client):
    onboard(client)
    b = client.put("/api/finance/budget", json={"restaurants": 300}).json()
    row = next(r for r in b["rows"] if r["category"] == "restaurants")
    assert row["planned"] == 300
    g = client.post("/api/finance/goals", json={"name": "Coussin", "kind": "emergency", "target": 50_000}).json()
    assert g["current"] == pytest.approx(130_000) and g["progress"] == 1.0
    goals = client.get("/api/finance/goals").json()
    assert goals[0]["achieved_at"]


def _adult_ids(client):
    ms = client.get("/api/finance/members").json()
    return next(m["id"] for m in ms if m["name"] == "Seb"), next(m["id"] for m in ms if m["name"] == "Lea")


def test_personal_budget_lines_and_individual_plans(client):
    onboard(client)  # shared budget: 2400 + 900 + 900 + 400 + 200 of living costs (+ taxes, excluded)
    seb, lea = _adult_ids(client)
    r = client.put(f"/api/finance/budget?member_id={seb}", json={"loisirs": 150, "telecom": 60})
    assert r.status_code == 200
    assert client.put("/api/finance/budget?member_id=9999", json={"loisirs": 1}).status_code == 404

    hh = client.get("/api/finance/budget").json()
    loisirs = next(x for x in hh["rows"] if x["category"] == "loisirs")
    assert loisirs["common"] == 400 and loisirs["personal"][str(seb)] == 150 and loisirs["personal"][str(lea)] == 0
    assert loisirs["planned"] == 550

    me = client.get(f"/api/finance/budget?scope=member:{seb}").json()
    s = me["share"]
    assert s == pytest.approx(110 / 200)  # by income (default)
    row = next(x for x in me["rows"] if x["category"] == "loisirs")
    assert row["common_share"] == pytest.approx(400 * s) and row["planned"] == pytest.approx(150 + 400 * s)
    logement = next(x for x in me["rows"] if x["category"] == "logement")
    assert logement["planned"] == pytest.approx(2400 * s)

    # the household plan uses everything; each adult their own lines + their share of the shared part
    common = (2400 + 900 + 900 + 400 + 200) * 12
    mine = (150 + 60) * 12
    assert client.get("/api/finance/projection").json()["spending_now"] == pytest.approx(common + mine)
    p_seb = client.get(f"/api/finance/projection?scope=member:{seb}").json()
    p_lea = client.get(f"/api/finance/projection?scope=member:{lea}").json()
    assert p_seb["spending_now"] == pytest.approx(mine + common * s)
    assert p_lea["spending_now"] == pytest.approx(common * (1 - s))

    # 0 removes a personal line; equal split from the settings
    client.put(f"/api/finance/budget?member_id={seb}", json={"telecom": 0})
    client.put("/api/finance/settings", json={"spending_split": "equal"})
    me = client.get(f"/api/finance/budget?scope=member:{seb}").json()
    assert me["share"] == 0.5
    assert next(x for x in me["rows"] if x["category"] == "telecom")["planned"] == 0


def test_shared_categories_count_as_shared_whoever_paid(client):
    onboard(client)
    seb, lea = _adult_ids(client)
    client.put(f"/api/finance/budget?member_id={seb}", json={"loisirs": 150})  # loisirs: no longer shared-only
    acc = next(a for a in client.get("/api/finance/accounts").json() if a["name"] == "Épargne Seb")
    today = date.today().isoformat()
    for desc, amount, cat in (("Loyer", -2400, "logement"), ("Fitness", -100, "loisirs"), ("ACI Vaud", -800, "impots")):
        r = client.post("/api/finance/transactions", json={"account_id": acc["id"], "date": today, "amount": amount,
                                                            "description": desc, "category": cat})
        assert r.status_code == 201, r.text
    s = client.get(f"/api/finance/budget?scope=member:{seb}").json()["share"]

    cf_seb = client.get(f"/api/finance/cashflow?scope=member:{seb}").json()["by_category"]
    cf_lea = client.get(f"/api/finance/cashflow?scope=member:{lea}").json()["by_category"]
    # rent is budgeted as shared only: split, although Seb's account paid it
    assert cf_seb["logement"] == pytest.approx(2400 * s) and cf_lea["logement"] == pytest.approx(2400 * (1 - s))
    # sport has a personal line, taxes are never shared: they follow the account
    assert cf_seb["loisirs"] == pytest.approx(100) and "loisirs" not in cf_lea
    assert cf_seb["impots"] == pytest.approx(800) and "impots" not in cf_lea
    # the household sees everything once
    cf = client.get("/api/finance/cashflow").json()["by_category"]
    assert cf["logement"] == pytest.approx(2400)
    lea_row = next(x for x in client.get(f"/api/finance/budget?scope=member:{lea}").json()["rows"]
                   if x["category"] == "logement")
    assert lea_row["actual"] == pytest.approx(2400 * (1 - s))


def _month_first(k: int) -> str:
    """The 1st of the month, k months before this one."""
    t = date.today()
    n = t.year * 12 + t.month - 1 - k
    return date(n // 12, n % 12 + 1, 1).isoformat()


def test_spread_a_yearly_payment_over_months(client):
    onboard(client)
    acc = next(a for a in client.get("/api/finance/accounts").json() if a["name"] == "Épargne Seb")

    def add(k, desc, amount, cat, **extra):
        r = client.post("/api/finance/transactions", json={"account_id": acc["id"], "date": _month_first(k),
                                                            "amount": amount, "description": desc, "category": cat, **extra})
        assert r.status_code == 201, r.text

    # three months of statements; the yearly health premium was paid two months ago
    add(2, "Assura prime annuelle", -6000, "sante")
    add(1, "Migros", -100, "alimentation")
    add(0, "Migros", -100, "alimentation")
    tx = client.get("/api/finance/transactions?q=Assura").json()["items"][0]
    assert tx["spread_months"] == 1
    cf = client.get("/api/finance/cashflow").json()
    assert cf["by_category"]["sante"] == pytest.approx(6000)          # all of it in one month

    r = client.put(f"/api/finance/transactions/{tx['id']}", json={"spread_months": 12})
    assert r.status_code == 200 and r.json()["spread_months"] == 12
    cf = client.get("/api/finance/cashflow").json()
    months = {m["month"]: m for m in cf["months"]}
    for k in (2, 1, 0):
        assert months[_month_first(k)[:7]]["by_category"]["sante"] == pytest.approx(500)
    assert cf["by_category"]["sante"] == pytest.approx(1500)          # months without statements get nothing
    assert cf["months_with_data"] == 3
    row = next(x for x in client.get("/api/finance/budget").json()["rows"] if x["category"] == "sante")
    assert row["actual"] == pytest.approx(500)                         # the "réel" is the monthly cost

    # last year's premium, paid before the 12-month window, still reaches into it
    add(13, "Assura 2025", -4800, "sante", spread_months=12)
    months = {m["month"]: m for m in client.get("/api/finance/cashflow").json()["months"]}
    assert months[_month_first(2)[:7]]["by_category"]["sante"] == pytest.approx(500 + 400)
    assert months[_month_first(1)[:7]]["by_category"]["sante"] == pytest.approx(500)

    for bad in (0, 25):
        assert client.put(f"/api/finance/transactions/{tx['id']}", json={"spread_months": bad}).status_code == 422
    client.put(f"/api/finance/transactions/{tx['id']}", json={"spread_months": 1})
    months = {m["month"]: m for m in client.get("/api/finance/cashflow").json()["months"]}
    assert months[_month_first(2)[:7]]["by_category"]["sante"] == pytest.approx(6000 + 400)


def test_old_database_gets_new_columns(tmp_path, monkeypatch):
    import sqlite3

    from app.finance import db

    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    old = sqlite3.connect(tmp_path / "finance.db")
    old.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, date TEXT NOT NULL, "
                "amount_cents INTEGER NOT NULL, fingerprint TEXT NOT NULL)")
    old.execute("INSERT INTO transactions (account_id, date, amount_cents, fingerprint) VALUES (1, '2026-01-05', -500000, 'x')")
    old.commit()
    old.close()
    db.init_db()
    with db.get_conn() as conn:
        assert conn.execute("SELECT spread_months FROM transactions").fetchone()["spread_months"] == 1
    db.init_db()                                                       # running it again is harmless


def test_photo_payslip_through_fake_llm(client, monkeypatch):
    from app import llm

    res = onboard(client)
    answer = {"doc_type": "payslip", "confidence": 0.9, "summary": "Fiche de salaire de Lea, novembre 2026",
              "fields": {"person_name": "Lea Dupont", "employer": "Hôpital", "period": "2026-11",
                         "gross_monthly": 7000, "net_monthly": 5900, "work_pct": 80, "salaries_per_year": 13,
                         "lpp_employee": 420, "aanp": 70}}
    def fake_chat(messages, settings=None, trace=None):
        trace.update(prompt="Tu lis…", images=1, attempts=[{"engine": "local:test-model", "seconds": 1.2,
                                                            "reasoning": "Le brut est 7000.", "raw": "{}"}])
        return answer, "local:test-model"

    monkeypatch.setattr(llm, "chat_json", fake_chat)
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    d = client.post("/api/finance/documents", files=[("files", ("fiche.png", png, "image/png"))]).json()[0]
    d = client.get(f"/api/finance/documents/{d['id']}").json()
    assert d["status"] == "review" and d["doc_type"] == "payslip"
    assert d["extracted"]["member_id"] == res["members"]["l"]          # « Lea Dupont » → Lea
    assert "local:test-model" in d["engine"]
    assert d["debug"]["attempts"][0]["reasoning"] == "Le brut est 7000."  # 🔍 « Sous le capot »
    listed = client.get("/api/finance/documents").json()[0]
    assert listed["has_debug"] is True and "debug" not in listed       # the list stays light
    client.post(f"/api/finance/documents/{d['id']}/apply", json={})
    inc = client.get(f"/api/finance/incomes?member_id={res['members']['l']}").json()
    assert inc[0]["gross_annual"] == pytest.approx(7000 * 13)
    assert inc[0]["lpp_employee_annual"] == pytest.approx(420 * 12)


def test_payslip_deductions_copied_with_their_minus_sign(client, monkeypatch):
    """Nemotron 3.5 Bolt writes « lpp_employee: -412.6 » as printed on the slip: still a positive contribution."""
    from app import llm
    from app.finance import db

    res = onboard(client)
    answer = {"doc_type": "payslip", "fields": {"person_name": "Lea Dupont", "gross_monthly": 7000,
                                                "net_monthly": 5900, "salaries_per_year": 12,
                                                "lpp_employee": -412.6, "aanp": "-70.00"}}
    monkeypatch.setattr(llm, "chat_json", lambda messages, settings=None, trace=None: (answer, "local:nemotron"))
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    d = client.post("/api/finance/documents", files=[("files", ("fiche.png", png, "image/png"))]).json()[0]
    client.post(f"/api/finance/documents/{d['id']}/apply", json={})
    inc = client.get(f"/api/finance/incomes?member_id={res['members']['l']}").json()
    assert inc[0]["lpp_employee_annual"] == pytest.approx(412.6 * 12)
    with db.get_conn() as conn:
        meta = json.loads(conn.execute("SELECT meta FROM members WHERE id = ?", (res["members"]["l"],)).fetchone()["meta"])
    assert meta["aanp_rate"] == pytest.approx(0.01)


def test_broker_statement_without_tickers(client, monkeypatch):
    """A tax statement prints names, not tickers: Nemotron copied the name into `symbol`,
    the share price into `value` and a coin's code into `currency`."""
    from app import llm
    from app.finance import db

    res = onboard(client)
    answer = {"doc_type": "broker_statement", "fields": {"person_name": "Lea Dupont", "broker": "Banque Test",
                                                         "currency": "CHF", "holdings": [
        {"name": "ACME HOLDING N", "symbol": "ACME HOLDING N", "isin": "1'234'567", "quantity": 4, "price": 50,
         "value": 50, "currency": "CHF"},
        {"name": "Big Corp", "symbol": "BIGC", "isin": "US0000000001", "valor": 765432.0, "quantity": 2.5,
         "price": 100, "value": 250, "currency": "usd"},
        {"name": "Coincoin", "symbol": "CCN", "isin": "n/a", "quantity": 1000, "value": 20, "currency": "CCN"}]}}
    monkeypatch.setattr(llm, "chat_json", lambda messages, settings=None, trace=None: (answer, "local:nemotron"))
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    d = client.post("/api/finance/documents", files=[("files", ("releve.png", png, "image/png"))]).json()[0]
    ex = client.get(f"/api/finance/documents/{d['id']}").json()["extracted"]
    hs = ex["fields"]["holdings"]
    assert ex["member_id"] == res["members"]["l"]                          # the holder is found
    assert [h["symbol"] for h in hs] == ["", "BIGC", "CCN"]                 # a name is not a ticker
    assert [(h["isin"], h["valor"]) for h in hs] == [("", "1234567"), ("US0000000001", "765432"), ("", "")]
    assert [h["value"] for h in hs] == [200, 250, 20]                      # 4 × 50, not the share price
    assert [h["currency"] for h in hs] == ["CHF", "USD", "CHF"]
    assert hs[2]["asset_class"] == "crypto"
    # a new account for Lea alone (not shared by default)
    r = client.post(f"/api/finance/documents/{d['id']}/apply",
                    json={"create_account": {"name": "Banque Test", "kind": "invest",
                                             "owners": {str(res["members"]["l"]): 1}}}).json()
    acc_id = r["result"]["account_created"]
    acc = client.get(f"/api/finance/accounts/{acc_id}").json()
    assert acc["owners"] == {str(res["members"]["l"]): 1}
    rows = sorted(acc["holdings"], key=lambda h: h["name"])
    assert [(h["symbol"], h["valor"], h["manual_price"], h["currency"]) for h in rows] == [
        ("", "1234567", 50, "CHF"), ("BIGC", "765432", 100, "USD"), ("CCN", "", 0.02, "CHF")]
    assert rows[2]["asset_class"] == "crypto"

    # edit a position by hand (add its ticker): the next statement keeps the ticker
    acme = rows[0]
    put = client.put(f"/api/finance/holdings/{acme['id']}", json={**{k: acme[k] for k in (
        "isin", "valor", "name", "quantity", "currency", "asset_class", "manual_price")}, "symbol": "acme.sw"})
    assert put.status_code == 200
    with db.get_conn() as conn:
        assert conn.execute("SELECT symbol FROM holdings WHERE id = ?", (acme["id"],)).fetchone()["symbol"] == "ACME.SW"
        conn.execute("UPDATE documents SET status = 'review' WHERE id = ?", (d["id"],))
    client.post(f"/api/finance/documents/{d['id']}/apply", json={"account_id": acc_id})
    with db.get_conn() as conn:
        again = db.rows(conn.execute("SELECT symbol, valor FROM holdings WHERE account_id = ? ORDER BY name", (acc_id,)))
    assert again[0] == {"symbol": "ACME.SW", "valor": "1234567"} and len(again) == 3


def test_new_account_from_a_document_can_be_shared(client, monkeypatch):
    from app import llm

    res = onboard(client)
    answer = {"doc_type": "broker_statement", "fields": {"person_name": "Lea Dupont", "holdings": []}}
    monkeypatch.setattr(llm, "chat_json", lambda messages, settings=None, trace=None: (answer, "local:nemotron"))
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    d = client.post("/api/finance/documents", files=[("files", ("titres.png", png, "image/png"))]).json()[0]
    assert client.get(f"/api/finance/documents/{d['id']}").json()["extracted"]["member_id"] == res["members"]["l"]
    r = client.post(f"/api/finance/documents/{d['id']}/apply",
                    json={"create_account": {"name": "Commun", "kind": "invest", "owners": {}}}).json()
    # Lea was detected, but « Commun » was chosen: the choice wins
    assert client.get(f"/api/finance/accounts/{r['result']['account_created']}").json()["owners"] == {}


def test_chosen_type_guides_the_model(client, monkeypatch):
    """A bank's tax statement was guessed as a tax assessment: the household picks the type,
    the model gets a short prompt with only that type's fields, and its own guess is overruled."""
    from app import llm

    onboard(client)
    sent = []
    answer = {"doc_type": "tax_assessment", "fields": {"holdings": [{"name": "Big Corp", "quantity": 2, "price": 10}]}}
    def fake_chat(messages, settings=None, trace=None):
        sent.append(json.dumps(messages, ensure_ascii=False))
        return answer, "local:nemotron"
    monkeypatch.setattr(llm, "chat_json", fake_chat)
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

    d = client.post("/api/finance/documents", files=[("files", ("releve.png", png, "image/png"))]).json()[0]
    assert client.get(f"/api/finance/documents/{d['id']}").json()["doc_type"] == "tax_assessment"  # guessed wrong
    assert "Identifie son type" in sent[-1]

    r = client.post(f"/api/finance/documents/{d['id']}/reparse", json={"doc_type": "broker_statement"})
    assert r.status_code == 200
    d = client.get(f"/api/finance/documents/{d['id']}").json()
    assert d["doc_type"] == "broker_statement" and d["hint_type"] == "broker_statement"
    assert d["extracted"]["fields"]["holdings"][0]["value"] is None
    assert "holdings" in sent[-1] and "gross_monthly" not in sent[-1] and "Identifie son type" not in sent[-1]

    client.post(f"/api/finance/documents/{d['id']}/reparse")                      # « Relire » keeps the choice
    assert client.get(f"/api/finance/documents/{d['id']}").json()["doc_type"] == "broker_statement"
    client.post(f"/api/finance/documents/{d['id']}/reparse", json={"doc_type": ""})  # back to guessing
    assert client.get(f"/api/finance/documents/{d['id']}").json()["doc_type"] == "tax_assessment"
    assert client.post(f"/api/finance/documents/{d['id']}/reparse", json={"doc_type": "nope"}).status_code == 422

    # chosen at upload: the same file again, with a type, is read again as that type
    client.post("/api/finance/documents", files=[("files", ("releve.png", png, "image/png"))],
                data={"doc_type": "broker_statement"})
    assert client.get(f"/api/finance/documents/{d['id']}").json()["doc_type"] == "broker_statement"


def test_backup_roundtrip(client):
    onboard(client)
    r = client.post("/api/finance/backup/export", json={"passphrase": "correct horse battery"})
    assert r.status_code == 200
    blob = r.content
    assert blob.startswith(b"PERRUFIN1") and b"Seb" not in blob        # encrypted
    client.post("/api/finance/reset?confirm=EFFACER")
    assert client.get("/api/finance/members").json() == []
    bad = client.post("/api/finance/backup/import", files={"file": ("b.perrufin", blob)},
                      data={"passphrase": "wrong passphrase"})
    assert bad.status_code == 422
    ok = client.post("/api/finance/backup/import", files={"file": ("b.perrufin", blob)},
                     data={"passphrase": "correct horse battery"})
    assert ok.status_code == 200, ok.text
    assert {m["name"] for m in client.get("/api/finance/members").json()} == {"Seb", "Lea", "Bébé"}


def test_calc_endpoints_and_calibration(client, fresh_db):
    from app.finance import tax as T

    r = client.post("/api/finance/calc/tax", json={"status": "single", "earners": [{"salary_gross": 100_000}]}).json()
    assert r["total"] > 0 and 0 < r["marginal_rate_pct"] < 50
    cap = client.get("/api/finance/calc/capital-tax?amount=100000").json()
    assert cap["total"] == pytest.approx(T.capital_tax(100_000, married=False, year=2026, communal_pct=78.5)["total"])
    avs = client.get("/api/finance/calc/avs?average_income=90720&years=44").json()
    assert avs["monthly"] == 2520 and avs["annual"] == 2520 * 13
    # a bill computed with the same rules → 0 % gap
    p = T.P.get(2026)
    base = T.vd_base_from_rate(80_000, 80_000, p)
    icc = round(base * 1.55 * 0.95 * 20) / 20 + round(base * 0.785 * 20) / 20
    with fresh_db.get_conn() as conn:
        conn.execute("INSERT INTO tax_bills (year, taxable_income_cents, rate_income_cents, icc_cents) VALUES (2026, ?, ?, ?)",
                     (8_000_000, 8_000_000, round(icc * 100)))
    cal = client.get("/api/finance/calibration").json()
    assert abs(cal[0]["icc_gap_pct"]) < 0.01


def test_scenarios_compare_marriage(client):
    onboard(client)
    s = client.post("/api/finance/scenarios", json={"name": "Mariés en 2028",
                                                    "overrides": {"marriage_year": 2028}}).json()
    rows = client.get(f"/api/finance/scenarios/compare?ids={s['id']}").json()
    assert [r["name"] for r in rows] == ["Plan actuel", "Mariés en 2028"]
    assert rows[0]["taxes_10y"] != pytest.approx(rows[1]["taxes_10y"])


def test_summary_block(client):
    onboard(client)
    from app.routers import finance
    s = finance.summary()
    assert s["onboarding_done"] and s["altitude"] >= 372 and "level" in s
