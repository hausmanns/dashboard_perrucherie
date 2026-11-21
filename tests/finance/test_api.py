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


def test_photo_payslip_through_fake_llm(client, monkeypatch):
    from app import llm

    res = onboard(client)
    answer = {"doc_type": "payslip", "confidence": 0.9, "summary": "Fiche de salaire de Lea, novembre 2026",
              "fields": {"person_name": "Lea Dupont", "employer": "Hôpital", "period": "2026-11",
                         "gross_monthly": 7000, "net_monthly": 5900, "work_pct": 80, "salaries_per_year": 13,
                         "lpp_employee": 420, "aanp": 70}}
    monkeypatch.setattr(llm, "chat_json", lambda messages, settings=None: (answer, "local:test-model"))
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    d = client.post("/api/finance/documents", files=[("files", ("fiche.png", png, "image/png"))]).json()[0]
    d = client.get(f"/api/finance/documents/{d['id']}").json()
    assert d["status"] == "review" and d["doc_type"] == "payslip"
    assert d["extracted"]["member_id"] == res["members"]["l"]          # « Lea Dupont » → Lea
    assert "local:test-model" in d["engine"]
    client.post(f"/api/finance/documents/{d['id']}/apply", json={})
    inc = client.get(f"/api/finance/incomes?member_id={res['members']['l']}").json()
    assert inc[0]["gross_annual"] == pytest.approx(7000 * 13)
    assert inc[0]["lpp_employee_annual"] == pytest.approx(420 * 12)


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
