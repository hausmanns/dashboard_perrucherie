"""The climb: altitude, XP, levels, badges, quests, streaks.

- **Altitude**: progress towards freedom drawn as a climb from the lake
  (Léman, 372 m) to the Pointe Dufour (4'634 m, the highest Swiss summit).
  Progress = liquid money ÷ liquid money needed to stop today (bridge model:
  AVS, LPP and 3a arrive later). Famous summits are the checkpoints.
- **XP** for the useful habits: monthly check-in, documents, categorising,
  goals, quests. Every award has a unique (kind, ref), so nothing counts twice.
- **Badges**: milestones computed from the data (first 100k, 6 months of
  safety net, savings rate 30 %, 3a maxed, Coast FI…).
- **Quests**: what is worth doing now, with the money at stake when it can be
  computed (e.g. the 3a deadline and the tax it saves).
"""
from __future__ import annotations

import json
import math
from datetime import date

from . import db

LAKE, SUMMIT = 372, 4634

PEAKS = [  # (altitude, name, flavour text, « where you are » phrase)
    (372, "Lac Léman", "Le camp de base, au bord de l'eau.", "au bord du Léman"),
    (528, "Cathédrale de Lausanne", "Premiers escaliers gravis.", "au-dessus de la cathédrale de Lausanne"),
    (1080, "Mont Pèlerin", "La vue sur Lavaux se dégage.", "au-dessus du Mont Pèlerin"),
    (1607, "Chasseron", "Le Jura sous vos pieds.", "au-dessus du Chasseron"),
    (2042, "Rochers-de-Naye", "Les marmottes vous saluent.", "au-dessus des Rochers-de-Naye"),
    (2502, "Säntis", "Mi-chemin de la liberté.", "au-dessus du Säntis"),
    (3238, "Titlis", "Les glaciers commencent.", "au-dessus du Titlis"),
    (3454, "Jungfraujoch", "Le toit de l'Europe en vue.", "au-dessus du Jungfraujoch"),
    (3883, "Petit Cervin", "L'air se raréfie, la liberté approche.", "au-dessus du Petit Cervin"),
    (4158, "Jungfrau", "Presque au sommet.", "au-dessus de la Jungfrau"),
    (4478, "Cervin", "Le dernier ressaut.", "au-dessus du Cervin"),
    (4634, "Pointe Dufour", "Liberté financière atteinte !", "au sommet de la Pointe Dufour"),
]

LEVEL_TITLES = [
    (1, "Marmotte curieuse"), (3, "Randonneur·se du dimanche"), (5, "Marcheur·se aguerri·e"),
    (8, "Bouquetin"), (12, "Guide de montagne"), (16, "Alpiniste"), (20, "Sherpa de la liberté"),
    (25, "Légende des 4000"),
]

BADGES: dict[str, dict] = {
    "onboarding": {"label": "Premier de cordée", "emoji": "🧗", "desc": "Le foyer est décrit, l'ascension commence.", "xp": 200},
    "first_checkin": {"label": "Le point du mois", "emoji": "📒", "desc": "Premier bilan mensuel.", "xp": 50},
    "streak_3": {"label": "Régularité suisse", "emoji": "⏱️", "desc": "3 mois de bilan d'affilée.", "xp": 100},
    "streak_6": {"label": "Horloger", "emoji": "⌚", "desc": "6 mois d'affilée.", "xp": 200},
    "streak_12": {"label": "Métronome", "emoji": "🎼", "desc": "Un an de bilans sans en manquer un.", "xp": 500},
    "nw_10k": {"label": "Premiers 10'000", "emoji": "🌱", "desc": "Patrimoine net de 10'000 fr.", "xp": 50},
    "nw_50k": {"label": "50'000", "emoji": "🌿", "desc": "Patrimoine net de 50'000 fr.", "xp": 100},
    "nw_100k": {"label": "Le premier 100k", "emoji": "💯", "desc": "Le plus dur est fait : 100'000 fr.", "xp": 250},
    "nw_250k": {"label": "Quart de million", "emoji": "🪨", "desc": "250'000 fr. de patrimoine net.", "xp": 300},
    "nw_500k": {"label": "Demi-million", "emoji": "⛰️", "desc": "500'000 fr.", "xp": 400},
    "nw_1m": {"label": "Millionnaires", "emoji": "🏔️", "desc": "Un million de patrimoine net.", "xp": 1000},
    "emergency_3": {"label": "Coussin de sécurité", "emoji": "🛟", "desc": "3 mois de dépenses en liquide.", "xp": 100},
    "emergency_6": {"label": "Bunker", "emoji": "🏰", "desc": "6 mois de dépenses en liquide.", "xp": 150},
    "savings_20": {"label": "Fourmi", "emoji": "🐜", "desc": "Taux d'épargne ≥ 20 % sur 12 mois.", "xp": 100},
    "savings_30": {"label": "Écureuil", "emoji": "🐿️", "desc": "Taux d'épargne ≥ 30 %.", "xp": 200},
    "savings_50": {"label": "Moine épargnant", "emoji": "🧘", "desc": "Taux d'épargne ≥ 50 %.", "xp": 400},
    "p3a_max": {"label": "3a au max", "emoji": "🏔️", "desc": "Le 3e pilier versé au maximum cette année.", "xp": 150},
    "fi_25": {"label": "Un quart du chemin", "emoji": "🥾", "desc": "25 % de la liberté.", "xp": 200},
    "fi_50": {"label": "À mi-pente", "emoji": "⛺", "desc": "50 % de la liberté.", "xp": 400},
    "fi_75": {"label": "Dernier refuge", "emoji": "🛖", "desc": "75 % de la liberté.", "xp": 600},
    "coast": {"label": "Le téléphérique", "emoji": "🚡", "desc": "Coast FI : sans épargner un franc de plus, la retraite à 65 est assurée.", "xp": 500},
    "lean_fi": {"label": "Liberté frugale", "emoji": "🎒", "desc": "Libres avec un budget serré (Lean FI).", "xp": 800},
    "fi": {"label": "Pointe Dufour", "emoji": "🏁", "desc": "Liberté financière !", "xp": 2000},
    "debt_free": {"label": "Sans dette", "emoji": "🕊️", "desc": "Aucun crédit en cours (hors hypothèque).", "xp": 100},
    "docs_10": {"label": "Archiviste", "emoji": "🗂️", "desc": "10 documents lus et appliqués.", "xp": 150},
    "calibrated": {"label": "Contrôle fiscal", "emoji": "🔍", "desc": "Le moteur comparé à une vraie décision de taxation.", "xp": 150},
    "explorer": {"label": "Explorateur·rice", "emoji": "🧭", "desc": "2 scénarios comparés.", "xp": 100},
    "goal_done": {"label": "Objectif atteint", "emoji": "🎯", "desc": "Un objectif rempli.", "xp": 150},
}


# --------------------------------------------------------------------------- XP


def award(kind: str, ref: str, xp: int, note: str = "") -> bool:
    """Give XP once per (kind, ref). Returns True when it was new."""
    with db.get_conn() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO xp_events (kind, ref, xp, note) VALUES (?, ?, ?, ?)",
                           (kind, ref, int(xp), note))
        return cur.rowcount > 0


def total_xp() -> int:
    with db.get_conn() as conn:
        return int(conn.execute("SELECT COALESCE(SUM(xp), 0) s FROM xp_events").fetchone()["s"])


def level_for(xp: int) -> dict:
    """Level n starts at 150 × (n-1)^1.7 XP (the first badges are generous; later levels take months)."""
    n = int(math.floor((max(0, xp) / 150) ** (1 / 1.7))) + 1
    start = int(150 * (n - 1) ** 1.7)
    nxt = int(150 * n ** 1.7)
    title = [t for lv, t in LEVEL_TITLES if lv <= n][-1]
    return {"level": n, "title": title, "xp": xp, "level_start": start, "next_level": nxt,
            "progress": (xp - start) / (nxt - start) if nxt > start else 1.0}


def altitude(progress: float) -> dict:
    p = max(0.0, min(1.0, progress or 0.0))
    alt = LAKE + p * (SUMMIT - LAKE)
    passed = [pk for pk in PEAKS if pk[0] <= alt + 0.5]
    nxt = next((pk for pk in PEAKS if pk[0] > alt + 0.5), None)
    return {
        "meters": round(alt), "progress": p,
        "last_peak": {"meters": passed[-1][0], "name": passed[-1][1], "text": passed[-1][2], "above": passed[-1][3]},
        "next_peak": ({"meters": nxt[0], "name": nxt[1], "text": nxt[2],
                       "progress_needed": (nxt[0] - LAKE) / (SUMMIT - LAKE)} if nxt else None),
        "peaks": [{"meters": m, "name": n, "progress": (m - LAKE) / (SUMMIT - LAKE)} for m, n, _, _ in PEAKS],
    }


# --------------------------------------------------------------------------- streaks


def _prev_month(m: str) -> str:
    y, mo = int(m[:4]), int(m[5:7])
    return f"{y - 1:04d}-12" if mo == 1 else f"{y:04d}-{mo - 1:02d}"


def streak() -> dict:
    with db.get_conn() as conn:
        months = {r["month"] for r in conn.execute("SELECT month FROM checkins")}
    cur = date.today().strftime("%Y-%m")
    m = cur if cur in months else _prev_month(cur)
    n = 0
    while m in months:
        n += 1
        m = _prev_month(m)
    return {"months": n, "this_month_done": cur in months, "total": len(months)}


# --------------------------------------------------------------------------- badges


def earned() -> dict[str, str]:
    with db.get_conn() as conn:
        return {r["code"]: r["earned_at"] for r in conn.execute("SELECT code, earned_at FROM badges")}


def _grant(code: str, detail: str = "") -> bool:
    with db.get_conn() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO badges (code, detail) VALUES (?, ?)", (code, detail))
        new = cur.rowcount > 0
    if new:
        award("badge", code, BADGES[code]["xp"], BADGES[code]["label"])
    return new


def check_badges(projection: dict | None = None) -> list[str]:
    """Grant every badge whose condition is now true. Returns the new ones."""
    from . import metrics, model

    new: list[str] = []
    settings = db.get_settings()

    def give(code, cond, detail=""):
        if cond and _grant(code, detail):
            new.append(code)

    give("onboarding", settings.get("onboarding_done"))
    st = streak()
    give("first_checkin", st["total"] >= 1)
    give("streak_3", st["months"] >= 3)
    give("streak_6", st["months"] >= 6)
    give("streak_12", st["months"] >= 12)
    nw = metrics.net_worth()
    for code, amount in (("nw_10k", 1e4), ("nw_50k", 5e4), ("nw_100k", 1e5), ("nw_250k", 2.5e5),
                         ("nw_500k", 5e5), ("nw_1m", 1e6)):
        give(code, nw["total"] >= amount)
    prof = metrics.spending_profile()
    monthly = (prof["annual"] or 1) / 12
    give("emergency_3", nw["liquid"] >= 3 * monthly)
    give("emergency_6", nw["liquid"] >= 6 * monthly)
    cf = metrics.cashflow(None, 12)
    sr = cf["savings_rate"] if cf["months_with_data"] >= 3 else None
    if sr is not None:
        give("savings_20", sr >= 0.20)
        give("savings_30", sr >= 0.30)
        give("savings_50", sr >= 0.50)
    with db.get_conn() as conn:
        loans = conn.execute("SELECT COUNT(*) c FROM accounts WHERE kind = 'loan' AND archived = 0").fetchone()["c"]
        has_accounts = conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
        docs = conn.execute("SELECT COUNT(*) c FROM documents WHERE status = 'applied'").fetchone()["c"]
        bills = conn.execute("SELECT COUNT(*) c FROM tax_bills").fetchone()["c"]
        scenarios = conn.execute("SELECT COUNT(*) c FROM scenarios").fetchone()["c"]
        goals_done = conn.execute("SELECT COUNT(*) c FROM goals WHERE achieved_at IS NOT NULL").fetchone()["c"]
    give("debt_free", has_accounts and loans == 0 and nw["total"] > 0)
    give("docs_10", docs >= 10)
    give("calibrated", bills >= 1)
    give("explorer", scenarios >= 2)
    give("goal_done", goals_done >= 1)
    give("p3a_max", _p3a_maxed_this_year())
    if projection is None and settings.get("onboarding_done"):
        try:
            projection = model.projection()
        except Exception:  # noqa: BLE001 — badges must never break a request
            projection = None
    if projection and projection.get("ready"):
        p = projection.get("progress") or 0
        give("fi_25", p >= 0.25)
        give("fi_50", p >= 0.50)
        give("fi_75", p >= 0.75)
        give("fi", p >= 0.999)
        give("coast", (projection.get("coast") or {}).get("reached"))
        lean = (projection.get("fi_years") or {}).get("lean")
        give("lean_fi", lean is not None and lean <= date.today().year)
    return new


def _p3a_maxed_this_year() -> bool:
    """Every working adult's 3a grew by at least the yearly maximum since 1 January."""
    from . import params as P

    year = date.today().year
    cap = P.get(year)["p3a"]["max_with_lpp"]
    with db.get_conn() as conn:
        accs = db.rows(conn.execute("SELECT * FROM accounts WHERE kind = 'pillar3a' AND archived = 0"))
        adults_ = db.rows(conn.execute("SELECT id FROM members WHERE kind = 'adult'"))
        if not accs or not adults_:
            return False
        paid: dict[int, float] = {}
        for acc in accs:
            owners = db.loads(acc["owners"], {}) or {str(a["id"]): 1 / len(adults_) for a in adults_}
            start = conn.execute("SELECT balance_cents FROM snapshots WHERE account_id = ? AND date < ? "
                                 "ORDER BY date DESC LIMIT 1", (acc["id"], f"{year}-01-01")).fetchone()
            end = conn.execute("SELECT balance_cents FROM snapshots WHERE account_id = ? AND date >= ? "
                               "ORDER BY date DESC LIMIT 1", (acc["id"], f"{year}-01-01")).fetchone()
            if not start or not end:
                continue
            growth = (end["balance_cents"] - start["balance_cents"]) / 100
            for k, v in owners.items():
                paid[int(k)] = paid.get(int(k), 0.0) + growth * float(v)
    return bool(paid) and all(v >= cap * 0.98 for v in paid.values())


# --------------------------------------------------------------------------- quests


def quests(projection: dict | None = None) -> list[dict]:
    from . import metrics, model, params as P
    from . import tax as T

    out: list[dict] = []
    today = date.today()
    st = streak()
    if not st["this_month_done"]:
        out.append({"id": f"checkin:{today:%Y-%m}", "kind": "checkin", "emoji": "📒", "xp": 100,
                    "title": f"Faire le point de {_month_fr(today.month)}",
                    "text": "Mettez à jour les soldes (2 minutes) et voyez de combien vous avez grimpé.",
                    "action": "checkin"})
    accounts = metrics.list_accounts()
    stale = [a for a in accounts if a["valuation"] == "manual" and (a["stale_days"] is None or a["stale_days"] > 45)]
    if stale and st["this_month_done"]:
        out.append({"id": f"stale:{today:%Y-%m}", "kind": "stale", "emoji": "🕰️", "xp": 30,
                    "title": f"{len(stale)} compte(s) pas à jour",
                    "text": ", ".join(a["name"] for a in stale[:4]) + " — un relevé glissé ici suffit.",
                    "action": "accounts"})
    with db.get_conn() as conn:
        uncategorized = conn.execute("SELECT COUNT(*) c FROM transactions WHERE category = ''").fetchone()["c"]
        adults_ = db.rows(conn.execute("SELECT * FROM members WHERE kind = 'adult'"))
        incomes = db.rows(conn.execute("SELECT * FROM incomes WHERE kind = 'salary'"))
        bills = conn.execute("SELECT COUNT(*) c FROM tax_bills").fetchone()["c"]
        budget = conn.execute("SELECT (SELECT COUNT(*) FROM budget WHERE monthly_cents > 0) "
                              "+ (SELECT COUNT(*) FROM budget_personal WHERE monthly_cents > 0) c").fetchone()["c"]
        done = {r["ref"] for r in conn.execute("SELECT ref FROM xp_events WHERE kind = 'quest'")}
    if uncategorized:
        out.append({"id": "categorize", "kind": "categorize", "emoji": "🏷️", "xp": min(100, uncategorized),
                    "title": f"Classer {uncategorized} opération(s)",
                    "text": "Chaque correction apprend une règle : la prochaine fois, c'est automatique.",
                    "action": "transactions"})
    lpp_accounts = [a for a in accounts if a["kind"] == "lpp"]
    for ad in adults_:
        if not any(i["member_id"] == ad["id"] for i in incomes):
            out.append({"id": f"salary:{ad['id']}", "kind": "data", "emoji": "💼", "xp": 50,
                        "title": f"Salaire de {ad['name']}",
                        "text": "Glissez une fiche ou le certificat de salaire : tout est rempli tout seul.",
                        "action": "documents"})
        mine = [a for a in lpp_accounts if a["shares"].get(ad["id"], 0) > 0]
        if not mine or not any(a["meta"].get("savings_credits_annual") for a in mine):
            out.append({"id": f"lpp:{ad['id']}", "kind": "data", "emoji": "🏢", "xp": 50,
                        "title": f"Certificat LPP de {ad['name']}",
                        "text": "Le certificat de prévoyance (reçu chaque année) rend la projection bien plus juste.",
                        "action": "documents"})
    if not bills:
        out.append({"id": "taxbill", "kind": "data", "emoji": "🔍", "xp": 150,
                    "title": "Vérifier le moteur fiscal",
                    "text": "Ajoutez votre dernière décision de taxation : on compare au calcul, au franc près.",
                    "action": "documents"})
    if not budget:
        out.append({"id": "budget", "kind": "data", "emoji": "🧮", "xp": 50, "title": "Estimer le budget",
                    "text": "Cinq curseurs suffisent — ou importez trois mois de relevés.", "action": "budget"})
    # 3a before 31 December — the tax saved at the marginal rate
    p = P.get(today.year)
    if today.month >= 9:
        try:
            hh = model.build_household()
        except Exception:  # noqa: BLE001
            hh = None
        for ad in (hh.adults if hh else []):
            if ad.salary <= 0:
                continue
            ref = f"p3a:{today.year}:{ad.id}"
            if ref in done:
                continue
            amount = p["p3a"]["max_with_lpp"]
            soc = T.social_contributions(ad.salary, p, aanp_rate=ad.aanp_rate)["total"]
            e = T.Earner(salary_gross=ad.salary, social=soc, lpp_employee=ad.lpp_employee or 0,
                         insurance=ad.insurance, commute=ad.commute)
            tp = T.Taxpayer(status="single", earners=[e], year=today.year,
                            communal_coefficient=float(db.get_settings().get("communal_coefficient", 78.5)))
            saving = T.marginal_rate(tp, amount) * amount
            out.append({"id": ref, "kind": "p3a", "emoji": "🏔️", "xp": 150, "deadline": f"{today.year}-12-31",
                        "title": f"3e pilier de {ad.name} avant le 31.12",
                        "text": f"Verser {amount:,.0f} fr. économise environ {saving:,.0f} fr. d'impôts cette année."
                                .replace(",", "'"),
                        "value": saving, "action": "done"})
        for ad in (hh.adults if hh else []):
            if ad.lpp_buyin_potential > 1000:
                out.append({"id": f"buyin:{today.year}:{ad.id}", "kind": "lpp_buyin", "emoji": "🏢", "xp": 50,
                            "title": f"Rachat LPP possible pour {ad.name}",
                            "text": f"Jusqu'à {ad.lpp_buyin_potential:,.0f} fr. déductibles. À étaler sur plusieurs "
                                    "années, et pas dans les 3 ans avant un retrait en capital.".replace(",", "'"),
                            "action": "info"})
    nw = metrics.net_worth()
    prof = metrics.spending_profile()
    if nw["liquid"] < 3 * prof["annual"] / 12 and accounts:
        out.append({"id": "emergency", "kind": "goal", "emoji": "🛟", "xp": 100, "title": "Coussin de sécurité",
                    "text": f"Visez {3 * prof['annual'] / 12:,.0f} fr. disponibles (3 mois de dépenses).".replace(",", "'"),
                    "action": "goals"})
    return [q for q in out if q["id"] not in done]


def complete_quest(quest_id: str) -> dict:
    q = next((q for q in quests() if q["id"] == quest_id), None)
    xp = q["xp"] if q else 50
    new = award("quest", quest_id, xp, q["title"] if q else quest_id)
    badges = check_badges()
    return {"awarded": new, "xp": xp if new else 0, "badges": badges}


def _month_fr(m: int) -> str:
    return ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
            "novembre", "décembre"][m - 1]


def state(projection: dict | None = None) -> dict:
    """Everything the « Sommet » view shows."""
    xp = total_xp()
    have = earned()
    with db.get_conn() as conn:
        recent = db.rows(conn.execute("SELECT kind, ref, xp, note, at FROM xp_events ORDER BY at DESC, id DESC LIMIT 12"))
    return {
        "level": level_for(xp),
        "altitude": altitude((projection or {}).get("progress", 0.0)),
        "streak": streak(),
        "badges": [{"code": c, **b, "earned_at": have.get(c)} for c, b in BADGES.items()],
        "earned_count": len(have),
        "quests": quests(projection),
        "recent_xp": recent,
    }


__all__ = ["award", "altitude", "check_badges", "complete_quest", "level_for", "quests", "state", "streak", "json"]
