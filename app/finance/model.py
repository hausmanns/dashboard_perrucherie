"""From the database to the engine: builds `simulate.Household` and runs the plan.

Member `meta` (adults): avs_past_years, avs_past_avg_income, commute, meals_out,
canteen, aanp_rate, ijm_rate, insurance_annual, p3a_annual, lpp_conversion_pct.
Member `meta` (children): birth_parent_id.
Account `meta`: lpp → savings_credits_annual, employee_annual, conversion_pct,
buyin_potential, projected_65; property → tax_value, maintenance_pct,
imputed_rent_pct; mortgage → rate_pct, property_account_id; holdings → cash.

`projection()` is the one call the dashboard needs: FI date (normal / lean /
fat), how much liquid money it takes to stop today, progress, Coast FI,
rule-of-thumb comparison, year-by-year series and the milestones. Results are
cached on a hash of the inputs, so repeated page loads cost nothing.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date

from . import db, metrics
from . import simulate as S
from .money import from_cents

_CACHE: dict[str, dict] = {}


def _today() -> date:
    return date.today()


def _active(row: dict, today: date) -> bool:
    s, e = row.get("start_date"), row.get("end_date")
    return (not s or s <= today.isoformat()) and (not e or e >= today.isoformat())


def _events(conn, scenario_id: int | None) -> list[S.Event]:
    rows = db.rows(conn.execute("SELECT * FROM events WHERE enabled = 1 AND scenario_id IS NULL"))
    if scenario_id:
        sc = conn.execute("SELECT * FROM scenarios WHERE id = ?", (scenario_id,)).fetchone()
        if sc and sc["base_events"] == "drop":
            rows = []
        rows += db.rows(conn.execute("SELECT * FROM events WHERE enabled = 1 AND scenario_id = ?", (scenario_id,)))
    out = []
    for r in rows:
        out.append(S.Event(
            kind=r["kind"], member_id=r["member_id"],
            start=date.fromisoformat(r["start_date"]) if r["start_date"] else None,
            end=date.fromisoformat(r["end_date"]) if r["end_date"] else None,
            params=db.loads(r["params"], {}),
        ))
    return out


def scenario_settings(scenario_id: int | None) -> dict:
    s = db.get_settings()
    if scenario_id:
        with db.get_conn() as conn:
            sc = conn.execute("SELECT overrides FROM scenarios WHERE id = ?", (scenario_id,)).fetchone()
        if sc:
            s = db.merge_settings(s, db.loads(sc["overrides"], {}))
    return s


def build_household(scope: str | None = None, scenario_id: int | None = None, today: date | None = None) -> S.Household:
    today = today or _today()
    settings = scenario_settings(scenario_id)
    member = metrics.parse_scope(scope)
    accounts = metrics.list_accounts()
    spending = metrics.spending_profile()
    with db.get_conn() as conn:
        members = db.rows(conn.execute("SELECT * FROM members ORDER BY id"))
        incomes = db.rows(conn.execute("SELECT * FROM incomes"))
        events = _events(conn, scenario_id)
        holdings = db.rows(conn.execute("SELECT * FROM holdings"))
    adult_rows = [m for m in members if m["kind"] == "adult"]
    adult_ids = [m["id"] for m in adult_rows]

    # equity / bond share of each liquid account
    by_acc_hold: dict[int, list[dict]] = {}
    for h in holdings:
        by_acc_hold.setdefault(h["account_id"], []).append(h)

    def mix(acc: dict) -> tuple[float, float]:
        if acc["kind"] in ("current", "savings", "cash"):
            return 0.0, 0.0
        if acc["valuation"] == "holdings" and by_acc_hold.get(acc["id"]):
            hs = by_acc_hold[acc["id"]]
            vals = [(h, h["quantity"] * (h["manual_price"] or 1)) for h in hs]
            tot = sum(v for _, v in vals) or 1
            eq = sum(v for h, v in vals if h["asset_class"] in ("equity", "crypto", "real_estate")) / tot
            bd = sum(v for h, v in vals if h["asset_class"] == "bonds") / tot
            return eq, bd
        e = acc["equity_pct"]
        e = (100.0 if acc["kind"] in ("invest", "crypto") else 0.0) if e is None else float(e)
        return e / 100, (1 - e / 100) if acc["kind"] == "invest" else 0.0

    adults: list[S.Adult] = []
    for m in adult_rows:
        meta = db.loads(m["meta"], {})
        birth = date.fromisoformat(m["birth_date"]) if m["birth_date"] else date(today.year - 35, 1, 1)
        my_inc = [i for i in incomes if i["member_id"] == m["id"] and _active(i, today)]
        salaries = [i for i in my_inc if i["kind"] == "salary"]
        salary = sum(from_cents(i["gross_annual_cents"]) for i in salaries)
        main = max(salaries, key=lambda i: i["gross_annual_cents"], default=None)
        work_pct = float(main["work_pct"]) if main else 100.0
        side = sum(from_cents(i["gross_annual_cents"]) for i in my_inc if i["kind"] != "salary")
        lpp_emp = sum(from_cents(i["lpp_employee_annual_cents"]) for i in salaries
                      if i["lpp_employee_annual_cents"] is not None) if any(
            i["lpp_employee_annual_cents"] is not None for i in salaries) else None
        a = S.Adult(id=m["id"], name=m["name"], birth_year=birth.year, salary=salary, work_pct=work_pct,
                    side_income=side, lpp_employee=lpp_emp, retire_age_target=m["retire_age_target"])
        liquid = eqv = bdv = 0.0
        lpp_credits = None
        for acc in accounts:
            share = acc["shares"].get(m["id"], 0.0)
            if share <= 0 or not acc["include_in_fi"]:
                continue
            v = acc["value_chf"] * share
            ameta = acc["meta"]
            if acc["group"] == "liquid":
                e, b = mix(acc)
                liquid += v
                eqv += v * e
                bdv += v * b
            elif acc["kind"] == "pillar3a":
                a.p3a_balance += v
            elif acc["kind"] == "lpp":
                a.lpp_balance += v
                if ameta.get("savings_credits_annual") is not None:
                    lpp_credits = (lpp_credits or 0) + float(ameta["savings_credits_annual"]) * share
                if ameta.get("employee_annual") is not None and a.lpp_employee is None:
                    a.lpp_employee = float(ameta["employee_annual"]) * share
                if ameta.get("conversion_pct"):
                    a.lpp_conversion_pct = float(ameta["conversion_pct"])
                if ameta.get("buyin_potential"):
                    a.lpp_buyin_potential += float(ameta["buyin_potential"]) * share
            elif acc["kind"] == "vested":
                a.vested_balance += v
        a.lpp_credits = lpp_credits
        a.liquid = liquid
        a.equity_share = eqv / liquid if liquid > 0 else 0.0
        a.bond_share = bdv / liquid if liquid > 0 else 0.0
        age = today.year - birth.year
        a.avs_past_years = int(meta.get("avs_past_years", max(0, age - 21)))
        a.avs_past_avg_income = float(meta.get("avs_past_avg_income", salary * 0.75))
        a.commute = float(meta.get("commute", 0) or 0)
        a.meals_out = bool(meta.get("meals_out", True))
        a.canteen = bool(meta.get("canteen", False))
        a.aanp_rate = float(meta.get("aanp_rate", 0.01))
        a.ijm_rate = float(meta.get("ijm_rate", 0.0))
        a.insurance = float(meta.get("insurance_annual", 5_400))
        a.p3a_annual = float(meta["p3a_annual"]) if meta.get("p3a_annual") not in (None, "") else None
        if meta.get("lpp_conversion_pct"):
            a.lpp_conversion_pct = float(meta["lpp_conversion_pct"])
        adults.append(a)

    kids = []
    for m in members:
        if m["kind"] != "child" or not m["birth_date"]:
            continue
        meta = db.loads(m["meta"], {})
        kids.append(S.Kid(id=m["id"], name=m["name"], birth=date.fromisoformat(m["birth_date"]),
                          birth_parent_id=meta.get("birth_parent_id")))

    # property & debts
    props: list[S.Property] = []
    other_debts = 0.0
    prop_by_id: dict[int, S.Property] = {}
    for acc in accounts:
        if acc["kind"] == "property" and acc["include_in_fi"]:
            ameta = acc["meta"]
            owners = {int(k): float(v) for k, v in acc["shares"].items()}
            pr = S.Property(value=acc["value_chf"], tax_value=float(ameta.get("tax_value") or acc["value_chf"] * 0.7),
                            mortgage=0.0, owners=owners, maintenance_pct=float(ameta.get("maintenance_pct", 1.0)),
                            imputed_rent_pct=float(ameta.get("imputed_rent_pct", 0.0)))
            props.append(pr)
            prop_by_id[acc["id"]] = pr
    for acc in accounts:
        if acc["kind"] == "mortgage" and acc["include_in_fi"]:
            target = prop_by_id.get(int(acc["meta"].get("property_account_id") or 0)) or (props[0] if props else None)
            if target:
                target.mortgage += acc["value_chf"]
                target.rate_pct = float(acc["meta"].get("rate_pct", target.rate_pct))
            else:
                other_debts += acc["value_chf"]
        elif acc["kind"] == "loan" and acc["include_in_fi"]:
            other_debts += acc["value_chf"]

    hh_spending, rent = spending["annual"], spending["rent"]
    fire = settings.get("fire", {})
    if fire.get("spending_annual"):
        cur = hh_spending or 1
        settings["assumptions"]["fi_spending_change_pct"] = (float(fire["spending_annual"]) / cur - 1) * 100

    hh = S.Household(adults=adults, kids=kids, spending=hh_spending, rent=rent, properties=props,
                     other_debts=other_debts, events=events, settings=settings, today=today,
                     single_parent=len(adults) == 1 and bool(kids))
    if member is not None:
        hh = _individual(hh, member)
    return hh


def _individual(hh: S.Household, member_id: int) -> S.Household:
    """One adult alone with their share of the household costs (independent finances)."""
    me = next((a for a in hh.adults if a.id == member_id), None)
    if me is None:
        raise ValueError("unknown adult")
    others = [a for a in hh.adults if a.id != member_id]
    if hh.settings.get("spending_split") == "equal" or not others:
        share = 1 / len(hh.adults)
    else:
        tot = sum(a.salary + a.side_income for a in hh.adults)
        share = (me.salary + me.side_income) / tot if tot > 0 else 1 / len(hh.adults)
    s = json.loads(json.dumps(hh.settings))
    cc = s.setdefault("child_costs", {})
    for k in ("0-3", "4-12", "13-17", "18-24", "childcare_monthly", "afterschool_monthly"):
        if k in cc:
            cc[k] = float(cc[k]) * share
    idx = [a.id for a in hh.adults].index(member_id)
    split = float(hh.settings.get("child_tax_split", 0.5))
    s["child_tax_split"] = split if idx == 0 else 1 - split
    for pr in hh.properties:
        pr.owners = {member_id: pr.owners.get(member_id, 0.0)}
    return S.Household(adults=[me], kids=hh.kids, spending=hh.spending * share, rent=hh.rent * share,
                       properties=[pr for pr in hh.properties if pr.owners.get(member_id)],
                       other_debts=hh.other_debts * share,
                       events=[e for e in hh.events if e.member_id in (None, member_id)],
                       settings={**s, "civil_status": "unmarried", "marriage_year": None,
                                 "_individual_kid_share": s["child_tax_split"]},
                       today=hh.today, single_parent=False)


# --------------------------------------------------------------------------- projection

def _key(hh: S.Household, extra: str) -> str:
    raw = json.dumps({"hh": asdict(hh), "x": extra}, default=str, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()


def projection(scope: str | None = None, scenario_id: int | None = None) -> dict:
    hh = build_household(scope, scenario_id)
    if not hh.adults:
        return {"ready": False, "reason": "no_adults"}
    key = _key(hh, "projection")
    if key in _CACHE:
        return _CACHE[key]
    eng = S.Engine(hh)
    a = eng.a
    fire = hh.settings.get("fire", {})
    style = fire.get("style", "normal")
    factors = {"lean": float(fire.get("lean_ratio", 0.8)), "normal": 1.0, "fat": float(fire.get("fat_ratio", 1.4))}
    solved = {k: S.solve_fi_year(eng, spending_factor=f) for k, f in factors.items()}
    main = solved.get(style) or solved["normal"]
    fi_year = main["fi_year"]
    target_age = fire.get("target_age")
    if target_age:
        plan = S.plan_for_stop_year(eng, min(ad.birth_year for ad in hh.adults) + int(target_age),
                                    spending_factor=factors[style])
    elif fi_year:
        plan = S.plan_for_stop_year(eng, fi_year, spending_factor=factors[style])
    else:
        plan = eng.default_plan()
    run = eng.run(plan)

    liquid_now = sum(ad.liquid for ad in hh.adults)
    stop_now = S.plan_for_stop_year(eng, eng.y0, spending_factor=factors[style])
    need_now = S.required_liquid_now(eng, stop_now)
    progress = min(1.0, liquid_now / need_now) if need_now and need_now != float("inf") else (1.0 if need_now == 0 else 0.0)

    # Coast FI: stop saving today, keep working (covering expenses) until 65
    coast_plan = S.Plan(stop_year={ad.id: ad.birth_year + 65 for ad in hh.adults},
                        spending_factor=factors[style], coast=True)
    coast_ok = eng.run(coast_plan)["success"]
    coast_need = S.required_liquid_now(eng, coast_plan) if not coast_ok else None

    swr = a["swr_pct"] / 100
    first_free = next((y for y in run["years"] if not any(y["working"])), run["years"][-1])
    fi_spending = first_free["spending"]
    rule_number = fi_spending / swr if swr > 0 else None

    years = [{k: v for k, v in y.items() if k != "tax_detail"} for y in run["years"]]
    out = {
        "ready": True,
        "scope": scope or "household",
        "scenario_id": scenario_id,
        "today": hh.today.isoformat(),
        "style": style,
        "fi_year": fi_year,
        "fi_years": {k: v["fi_year"] for k, v in solved.items()},
        "fi_ages": {ad.name: (fi_year - ad.birth_year if fi_year else None) for ad in hh.adults},
        "plan_stop_year": max(plan.stop_year.values()),
        "plan_success": run["success"],
        "failed_year": run["failed_year"],
        "liquid_now": liquid_now,
        "required_liquid_now": need_now if need_now != float("inf") else None,
        "progress": progress,
        "coast": {"reached": coast_ok, "required_liquid_now": coast_need},
        "rule_of_thumb": {"swr_pct": a["swr_pct"], "spending": fi_spending, "number": rule_number},
        "spending_now": hh.spending,
        "adults": [{"id": ad.id, "name": ad.name, "birth_year": ad.birth_year} for ad in hh.adults],
        "kids": [{"id": k.id, "name": k.name, "birth": k.birth.isoformat()} for k in hh.kids],
        "years": years,
        "milestones": milestones(hh, run, fi_year),
        "returns": run["returns"],
        "assumptions": a,
    }
    if len(_CACHE) > 64:
        _CACHE.clear()
    _CACHE[key] = out
    return out


def montecarlo(scope: str | None = None, scenario_id: int | None = None, runs: int | None = None,
               target: float = 0.85) -> dict:
    """Success probability of the plan and of nearby stop years; the « safe » FI year."""
    hh = build_household(scope, scenario_id)
    if not hh.adults:
        return {"ready": False}
    key = _key(hh, f"mc:{runs}:{target}")
    if key in _CACHE:
        return _CACHE[key]
    eng = S.Engine(hh)
    proj = projection(scope, scenario_id)
    fi = proj["fi_year"] or proj["plan_stop_year"]
    style = proj["style"]
    fire = hh.settings.get("fire", {})
    factor = {"lean": float(fire.get("lean_ratio", 0.8)), "fat": float(fire.get("fat_ratio", 1.4))}.get(style, 1.0)
    main = S.monte_carlo(eng, S.plan_for_stop_year(eng, fi, spending_factor=factor), runs=runs)
    curve = []
    safe_year = None
    last_year = max(ad.birth_year + 65 for ad in hh.adults)
    for y in range(max(eng.y0, fi - 3), min(last_year, fi + 15) + 1):
        r = S.monte_carlo(eng, S.plan_for_stop_year(eng, y, spending_factor=factor), runs=min(400, runs or 400))
        curve.append({"year": y, "success_rate": r["success_rate"],
                      "ages": {ad.name: y - ad.birth_year for ad in hh.adults}})
        if safe_year is None and r["success_rate"] >= target:
            safe_year = y
            if len(curve) >= 6:
                break
    out = {"ready": True, "fi_year": fi, "plan": main, "curve": curve, "safe_year": safe_year, "target": target}
    _CACHE[key] = out
    return out


def milestones(hh: S.Household, run: dict, fi_year: int | None) -> list[dict]:
    out = []
    for k in hh.kids:
        out.append({"year": k.birth.year, "kind": "birth", "label": f"Naissance de {k.name}", "emoji": "👶"})
        out.append({"year": k.birth.year + 25, "kind": "independent", "label": f"{k.name} prend son envol",
                    "emoji": "🎓"})
    for ev in hh.events:
        if not ev.start:
            continue
        labels = {"home_purchase": ("Achat du logement", "🏡"), "marriage": ("Mariage", "💍"),
                  "move_abroad": ("Départ à l'étranger", "✈️"), "part_time": ("Temps partiel", "⏳"),
                  "career_break": ("Pause carrière", "🌴"), "lump_sum": ("Rentrée exceptionnelle", "💰"),
                  "side_income": ("Revenu annexe", "🧑‍💻"), "salary_change": ("Changement de salaire", "💼"),
                  "expense_change": ("Changement de dépenses", "🧾")}
        lab, emo = labels.get(ev.kind, (ev.kind, "📌"))
        name = next((a.name for a in hh.adults if a.id == ev.member_id), "")
        out.append({"year": ev.start.year, "kind": ev.kind, "label": f"{lab}{' · ' + name if name else ''}",
                    "emoji": emo})
    for ad in hh.adults:
        out.append({"year": ad.birth_year + int(hh.a.get("avs_start_age", 65)), "kind": "avs",
                    "label": f"Rente AVS · {ad.name}", "emoji": "🏛️"})
        out.append({"year": ad.birth_year + int(hh.a.get("pillar3a_withdraw_from_age", 60)), "kind": "p3a",
                    "label": f"Retraits 3a · {ad.name}", "emoji": "🏔️"})
    if fi_year:
        out.append({"year": fi_year, "kind": "fi", "label": "Liberté financière", "emoji": "🏁"})
    return sorted(out, key=lambda m: m["year"])
