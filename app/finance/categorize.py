"""Spending categories, rules and automatic categorisation.

Order of the decision, first match wins:
1. the household's own rules (learned from corrections: « always put COOP in
   Courses »),
2. built-in Swiss merchants and keywords (COOP, MIGROS, CFF, CSS, ACI…),
3. own-account transfers (counterparty IBAN of one of our accounts, 3a apps),
4. the LLM, in batches, for what is left (marked `llm`, easy to review).

A rule is a set of normalised words (accents/case/punctuation dropped) that
must all appear in the description + counterparty.
"""
from __future__ import annotations

import re
import unicodedata

CATEGORIES: dict[str, dict] = {
    # expenses
    "logement": {"label": "Logement", "emoji": "🏠", "type": "expense", "essential": True},
    "alimentation": {"label": "Courses", "emoji": "🛒", "type": "expense", "essential": True},
    "restaurants": {"label": "Restaurants & sorties", "emoji": "🍽️", "type": "expense", "essential": False},
    "transport": {"label": "Transports publics", "emoji": "🚆", "type": "expense", "essential": True},
    "voiture": {"label": "Voiture", "emoji": "🚗", "type": "expense", "essential": True},
    "sante": {"label": "Assurance maladie & santé", "emoji": "🩺", "type": "expense", "essential": True},
    "assurances": {"label": "Autres assurances", "emoji": "🛡️", "type": "expense", "essential": True},
    "telecom": {"label": "Téléphone & internet", "emoji": "📶", "type": "expense", "essential": True},
    "abonnements": {"label": "Abonnements", "emoji": "📺", "type": "expense", "essential": False},
    "enfants": {"label": "Enfants & garde", "emoji": "🍼", "type": "expense", "essential": True},
    "shopping": {"label": "Shopping & maison", "emoji": "🛍️", "type": "expense", "essential": False},
    "loisirs": {"label": "Loisirs & sport", "emoji": "🎾", "type": "expense", "essential": False},
    "voyages": {"label": "Voyages", "emoji": "🧳", "type": "expense", "essential": False},
    "cadeaux": {"label": "Cadeaux & dons", "emoji": "🎁", "type": "expense", "essential": False},
    "formation": {"label": "Formation", "emoji": "🎓", "type": "expense", "essential": False},
    "impots": {"label": "Impôts", "emoji": "🧾", "type": "expense", "essential": True},
    "frais": {"label": "Frais bancaires", "emoji": "🏦", "type": "expense", "essential": False},
    "divers": {"label": "Divers", "emoji": "❓", "type": "expense", "essential": False},
    # income
    "salaire": {"label": "Salaire", "emoji": "💼", "type": "income"},
    "revenus_annexes": {"label": "Revenus annexes", "emoji": "🧑‍💻", "type": "income"},
    "allocations": {"label": "Allocations & aides", "emoji": "👶", "type": "income"},
    "placements": {"label": "Dividendes & intérêts", "emoji": "📈", "type": "income"},
    "remboursements": {"label": "Remboursements", "emoji": "↩️", "type": "income"},
    # money that stays ours
    "virement": {"label": "Virement entre nos comptes", "emoji": "🔁", "type": "transfer"},
    "epargne": {"label": "Épargne & placements", "emoji": "🏔️", "type": "transfer"},
}

# Not part of the "living costs" that the plan projects: taxes are computed by
# the tax engine, children by the child-cost model.
EXCLUDED_FROM_LIVING = {"impots", "enfants"}

# Built-in keyword rules (normalised words → category). Longest patterns first.
BUILTIN: list[tuple[str, str]] = [
    # groceries
    ("coop pronto", "alimentation"), ("coop", "alimentation"), ("migros", "alimentation"),
    ("denner", "alimentation"), ("aldi", "alimentation"), ("lidl", "alimentation"),
    ("manor food", "alimentation"), ("volg", "alimentation"), ("spar", "alimentation"),
    ("aligro", "alimentation"), ("migrolino", "alimentation"), ("farmy", "alimentation"),
    ("too good to go", "alimentation"),
    # restaurants
    ("mcdonald", "restaurants"), ("burger king", "restaurants"), ("starbucks", "restaurants"),
    ("restaurant", "restaurants"), ("cafe", "restaurants"), ("pizzeria", "restaurants"),
    ("smood", "restaurants"), ("uber eats", "restaurants"), ("just eat", "restaurants"), ("eat ch", "restaurants"),
    # transport
    ("cff", "transport"), ("sbb", "transport"), ("tl lausanne", "transport"), ("transports publics", "transport"),
    ("mobilis", "transport"), ("publibike", "transport"), ("mobility", "transport"), ("uber", "transport"),
    ("easyride", "transport"),
    ("socar", "voiture"), ("migrol", "voiture"), ("tamoil", "voiture"), ("bp ", "voiture"), ("shell", "voiture"),
    ("avia", "voiture"), ("parking", "voiture"), ("parkingpay", "voiture"), ("vignette", "voiture"),
    ("service des automobiles", "voiture"),
    # health insurance & health
    ("assura", "sante"), ("helsana", "sante"), ("css", "sante"), ("groupe mutuel", "sante"), ("swica", "sante"),
    ("sanitas", "sante"), ("visana", "sante"), ("concordia", "sante"), ("kpt", "sante"), ("sympany", "sante"),
    ("atupri", "sante"), ("philos", "sante"), ("avenir assurance", "sante"), ("pharmacie", "sante"),
    ("amavita", "sante"), ("sun store", "sante"), ("pharmacieplus", "sante"), ("medecin", "sante"),
    ("dentiste", "sante"), ("chuv", "sante"),
    ("axa", "assurances"), ("zurich assurance", "assurances"), ("la mobiliere", "assurances"),
    ("mobiliere", "assurances"), ("baloise", "assurances"), ("generali", "assurances"), ("vaudoise", "assurances"),
    ("helvetia", "assurances"), ("allianz", "assurances"), ("smile direct", "assurances"),
    # telecom & subscriptions
    ("swisscom", "telecom"), ("sunrise", "telecom"), ("salt", "telecom"), ("wingo", "telecom"),
    ("yallo", "telecom"), ("galaxus mobile", "telecom"), ("init7", "telecom"), ("serafe", "telecom"),
    ("netflix", "abonnements"), ("spotify", "abonnements"), ("disney", "abonnements"), ("apple com bill", "abonnements"),
    ("google one", "abonnements"), ("youtube premium", "abonnements"), ("amazon prime", "abonnements"),
    ("canal", "abonnements"), ("le temps", "abonnements"), ("24 heures", "abonnements"),
    ("openai", "abonnements"), ("anthropic", "abonnements"), ("icloud", "abonnements"),
    # children
    ("creche", "enfants"), ("garderie", "enfants"), ("apems", "enfants"), ("parascolaire", "enfants"),
    ("bebe", "enfants"), ("pharmacie bebe", "enfants"), ("aubert", "enfants"),
    # shopping
    ("galaxus", "shopping"), ("digitec", "shopping"), ("ikea", "shopping"), ("manor", "shopping"),
    ("zalando", "shopping"), ("amazon", "shopping"), ("aliexpress", "shopping"), ("decathlon", "shopping"),
    ("interdiscount", "shopping"), ("fnac", "shopping"), ("jumbo", "shopping"), ("hornbach", "shopping"),
    ("brack", "shopping"), ("zara", "shopping"), ("ochsner", "shopping"),
    ("tutti", "shopping"), ("ricardo", "shopping"), ("pfister", "shopping"), ("conforama", "shopping"),
    # leisure & travel
    ("fitness", "loisirs"), ("basic fit", "loisirs"), ("cinema", "loisirs"), ("pathe", "loisirs"),
    ("ticketcorner", "loisirs"), ("piscine", "loisirs"), ("ski", "loisirs"), ("steam", "loisirs"),
    ("playstation", "loisirs"), ("nintendo", "loisirs"),
    ("easyjet", "voyages"), ("swiss international", "voyages"), ("airbnb", "voyages"), ("booking com", "voyages"),
    ("hotel", "voyages"), ("expedia", "voyages"), ("lufthansa", "voyages"), ("ryanair", "voyages"),
    ("trainline", "voyages"), ("sncf", "voyages"),
    # taxes
    ("administration cantonale des impots", "impots"), ("aci vaud", "impots"), ("office d impot", "impots"),
    ("impot", "impots"), ("steuerverwaltung", "impots"), ("tresorerie", "impots"),
    # housing
    ("loyer", "logement"), ("gerance", "logement"), ("regie", "logement"), ("naef", "logement"),
    ("bernard nicod", "logement"), ("romande energie", "logement"), ("services industriels", "logement"),
    ("sil lausanne", "logement"), ("groupe e", "logement"), ("electricite", "logement"),
    # bank fees
    ("frais de tenue", "frais"), ("frais bancaires", "frais"), ("kontofuhrung", "frais"), ("cotisation carte", "frais"),
    ("frais de carte", "frais"), ("account fee", "frais"),
    # income
    ("salaire", "salaire"), ("lohn", "salaire"), ("salary", "salaire"), ("payroll", "salaire"),
    ("allocations familiales", "allocations"), ("caisse d allocations", "allocations"), ("apg", "allocations"),
    ("dividend", "placements"), ("dividende", "placements"), ("interet crediteur", "placements"),
    ("interest", "placements"), ("zins", "placements"),
    ("remboursement", "remboursements"), ("refund", "remboursements"), ("ruckerstattung", "remboursements"),
    # savings & pensions (money that stays ours)
    ("viac", "epargne"), ("finpension", "epargne"), ("frankly", "epargne"), ("truewealth", "epargne"),
    ("selma", "epargne"), ("interactive brokers", "epargne"), ("swissquote", "epargne"), ("degiro", "epargne"),
    ("yuh invest", "epargne"), ("pilier 3a", "epargne"), ("saule 3a", "epargne"), ("3a", "epargne"),
    ("virement compte", "virement"), ("transfert", "virement"), ("transfer to", "virement"),
    ("transfer from", "virement"), ("top up", "virement"), ("topup", "virement"), ("recharge", "virement"),
]


def normalize(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _matches(pattern: str, haystack: str) -> bool:
    words = pattern.split()
    padded = f" {haystack} "
    if len(words) == 1:
        return f" {words[0]} " in padded or (len(words[0]) >= 5 and words[0] in haystack)
    return f" {pattern} " in padded or all(f" {w} " in padded for w in words)


def suggest_pattern(description: str, counterparty: str = "") -> str:
    """The words worth remembering for a rule: the merchant, not the reference numbers."""
    base = normalize(counterparty) or normalize(description)
    words = [w for w in base.split() if not w.isdigit() and len(w) > 1
             and w not in {"achat", "paiement", "payment", "carte", "card", "debit", "credit", "twint", "ch",
                           "sa", "ag", "gmbh", "sarl", "le", "la", "les", "de", "du", "des", "et", "chf", "eur",
                           "ordre", "lsv", "ebill", "e", "bill", "no", "nr", "ref", "date", "lausanne"}]
    return " ".join(words[:3])


def categorize(description: str, counterparty: str, amount: float, rules: list[dict],
               own_ibans: set[str] | None = None) -> tuple[str, str, bool]:
    """(category, source, is_transfer) for one transaction — '' when unknown."""
    hay = normalize(f"{description} {counterparty}")
    for r in rules:
        if r["pattern"] and _matches(r["pattern"], hay):
            return r["category"], "rule", bool(r["is_transfer"]) or CATEGORIES.get(r["category"], {}).get("type") == "transfer"
    if own_ibans:
        compact = re.sub(r"\s+", "", f"{description} {counterparty}").upper()
        if any(ib and ib in compact for ib in own_ibans):
            return "virement", "builtin", True
    for pat, cat in sorted(BUILTIN, key=lambda t: -len(t[0])):
        if _matches(pat, hay):
            ctype = CATEGORIES[cat]["type"]
            if ctype == "income" and amount < 0 and cat != "remboursements":
                continue
            if ctype == "expense" and amount > 0 and cat not in ("logement",):
                # a refund from a shop is still that category (negative spending)
                return cat, "builtin", False
            return cat, "builtin", ctype == "transfer"
    return "", "", False


def llm_prompt(items: list[dict]) -> str:
    cats = "\n".join(f"- {k}: {v['label']} ({v['type']})" for k, v in CATEGORIES.items())
    lines = "\n".join(f'{i}. {it["amount"]:+.2f} | {it["description"]} | {it.get("counterparty", "")}'
                      for i, it in enumerate(items))
    return (
        "Tu classes des opérations bancaires d'un ménage suisse (Lausanne).\n"
        "Catégories possibles (clé: libellé (type)):\n" + cats + "\n\n"
        "Montant négatif = dépense, positif = entrée. Un virement vers un 3e pilier, un courtier ou un autre de nos "
        "comptes est 'epargne' ou 'virement'. Si tu ne sais vraiment pas, utilise 'divers' (dépense) ou "
        "'remboursements' (entrée).\n\n"
        "Opérations (index. montant | libellé | contrepartie):\n" + lines + "\n\n"
        'Réponds en JSON strict : {"items": [{"i": 0, "category": "alimentation"}, ...]}'
    )


def parse_llm_answer(data: dict, n: int) -> dict[int, str]:
    out = {}
    for it in (data or {}).get("items", []):
        try:
            i = int(it.get("i"))
        except (TypeError, ValueError):
            continue
        cat = str(it.get("category", "")).strip()
        if 0 <= i < n and cat in CATEGORIES:
            out[i] = cat
    return out
