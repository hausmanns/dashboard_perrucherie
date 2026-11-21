"""What the régie readers share.

Régie websites list their flats with the **net** rent; the household's budget
is charges included, like the portals show it. So a card that could match is
read once more on its detail page (charges, surface, floor, full text) by
`enrich()`, and stored. Later checks reuse what was stored and only read a
detail page again when the card's net rent changed. `could_match()` is the
cheap pre-check that keeps those detail reads to the few cards that matter.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from ..common import RunContext, detect_features, html_text, parse_chf, to_float
from ..http import Fetcher, SourceError
from ..places import ResolvedZone, canton_of_zip, geocode, in_zones, index, nearest_locality, normalize

DETAILS_PER_RUN = 40     # detail pages per source and check; the rest wait for the next check
CHARGES_MARGIN = 0.3     # charges can add up to ~30% to a net rent: keep a card while unsure
ROMANDIE = {"VD", "GE", "VS", "FR", "NE", "JU"}


def covers(zones: list[ResolvedZone], cantons: set, ctx: RunContext, label: str) -> bool:
    """Skip a régie (no request at all) when the search looks elsewhere."""
    if any(z.cantons & cantons for z in zones):
        return True
    ctx.warn(f"{label} ne gère pas de biens dans ces zones — ignoré")
    return False


def rooms_in(text: str) -> Optional[float]:
    """« 3.5 pièces », « 4,5 pces », « Studio » → 3.5, 4.5, 1."""
    t = (text or "").lower()
    m = re.search(r"(\d+(?:[.,]5)?)\s*(?:-\s*)?(?:pi[eè]ces?|pces?|p\.|pc\b|zimmer|zi\b|rooms?)", t)
    if m:
        return to_float(m.group(1))
    if re.search(r"\bstudio\b", t):
        return 1.0
    return None


NOT_HOMES = re.compile(r"\b(?:(?:parc|parking|garage|box|depot|cave|bureau|commerce|commercial|commerciale|"
                       r"arcade|atelier|local|restaurant|cafe|entrepot|cabinet|terrain|industriel|hotel)s?|"
                       r"bureaux|locaux)\b")


def first_number(text: str) -> Optional[int]:
    """« 178m2 », « 124 m² » → 178, 124 (the 2 of m² is not a digit of the surface)."""
    m = re.search(r"\d+", text or "")
    return int(m.group(0)) if m else None


def category_in(text: str, rooms: Optional[float] = None, kind: str = "") -> str:
    """apartment | house | furnished | room | other, from a type label or a title.
    An explicit type label (« Commerce ») decides alone; a title only counts as
    non-residential when it has no rooms (« 3.5 pièces avec place de parc »)."""
    if kind and NOT_HOMES.search(normalize(kind)):
        return "other"
    key = normalize(f"{kind} {text}")
    if NOT_HOMES.search(key) and not rooms:
        return "other"
    if re.search(r"\b(maison|villa|chalet|ferme)\b", key):
        return "house"
    if re.search(r"\b(meuble|meublee|temporaire)\b", key):
        return "furnished"
    if re.search(r"\b(chambre|colocation)\b", key) and not re.search(r"\bchambres?\s+a\s+coucher\b", key):
        return "room"
    return "apartment"


def money_after(text: str, *labels: str) -> Optional[int]:
    """The CHF amount right after one of the labels: « Charges : CHF 250.- » → 250."""
    for label in labels:
        m = re.search(rf"{label}\s*[:]?\s*(?:CHF\s*)?([\d'’  .,]+)(?:\s*CHF)?", text, re.I)
        if m:
            value = parse_chf(m.group(1))
            if value:
                return value
    return None


def zip_for(city: str, lat=None, lng=None) -> str:
    """Postcode from a position, else from a town name (its main postcode)."""
    if lat is not None and lng is not None:
        near = nearest_locality(lat, lng, city)
        return near.zip if near else ""
    key = normalize(re.sub(r"\b(VD|GE|VS|FR|NE|JU)\b", "", city or ""))
    locs = [l for l in index().localities if l.key == key] or \
           [l for l in index().localities if normalize(l.commune) == key]
    return max(locs, key=lambda l: l.share).zip if locs else ""


def could_match(item: dict, criteria: dict, zones: list[ResolvedZone]) -> bool:
    """Cheap check on a list card (net rent, maybe no surface) before reading its detail page."""
    cat = item.get("category")
    if cat == "other" or (cat and cat not in criteria["categories"]):
        return False
    rooms = item.get("rooms")
    if rooms is not None:
        if criteria.get("rooms_min") is not None and rooms < criteria["rooms_min"]:
            return False
        if criteria.get("rooms_max") is not None and rooms > criteria["rooms_max"]:
            return False
    net = item.get("price")
    if net is not None:
        if criteria.get("price_max") is not None and net > criteria["price_max"]:
            return False
        if criteria.get("price_min") is not None and net * (1 + CHARGES_MARGIN) < criteria["price_min"]:
            return False
    surface = item.get("surface")
    if surface is not None:
        if criteria.get("surface_min") is not None and surface < criteria["surface_min"]:
            return False
        if criteria.get("surface_max") is not None and surface > criteria["surface_max"]:
            return False
    return in_zones(item, zones)


def _stored_net(row: dict) -> Optional[int]:
    if row.get("price") is not None and row.get("charges") is not None:
        return row["price"] - row["charges"]
    return row.get("price")


def enrich(fetcher: Fetcher, cards: list[dict], ctx: RunContext, label: str,
           read_detail: Callable[[Fetcher, dict], dict]) -> list[dict]:
    """Cards → full listings. A card already read (stored with its text) is
    taken from the DB, unless its rent changed (net, or gross for the régies
    whose cards show it — Livit, Rosset, Publiaz); a new one gets its detail
    page read, at most DETAILS_PER_RUN per check — the others are returned as
    they are and read at the next check."""
    known = ctx.known_rows([c["source_id"] for c in cards])
    out, budget, deferred = [], DETAILS_PER_RUN, 0
    for card in cards:
        row = known.get(card["source_id"])
        if row and row.get("description") and (card.get("price") is None
                                               or card["price"] in (_stored_net(row), row.get("price"))):
            out.append(row)
            continue
        if budget <= 0:
            deferred += 1
            out.append(card)
            continue
        budget -= 1
        try:
            details = read_detail(fetcher, card)
        except SourceError:
            details = {}
        merged = {**card, **{k: v for k, v in details.items() if v not in (None, "", [], set())}}
        merged["features"] = set(card.get("features") or ()) | set(details.get("features") or ()) | \
            detect_features(f"{merged.get('title', '')}\n{merged.get('description', '')}")
        out.append(merged)
        ctx.progress(found=len(out), requests=fetcher.requests)
    if deferred:
        ctx.warn(f"{label} : {deferred} annonce(s) seront détaillées au prochain passage")
    return out


def net_to_gross(details: dict, net: Optional[int]) -> dict:
    """Detail values → price charges included. Net + charges wins over a stated
    « total », which some régies inflate with the parking places."""
    charges = details.get("charges")
    total = details.pop("total", None)
    if net and charges:
        details["price"] = net + charges
    elif total:
        details["price"] = total
        if net and total > net:
            details["charges"] = total - net
    return details


def locate_address(street: str, zip_code: str, city: str) -> dict:
    """Exact position from the federal geocoder (once per new listing)."""
    address = ", ".join(p for p in (street, f"{zip_code} {city}".strip()) if p)
    found = geocode(address) if address else None
    if not found:
        return {}
    out = {"lat": found["lat"], "lng": found["lng"], "geo_precision": found["precision"]}
    m = re.search(r"\b(\d{4})\b", found.get("label") or "")
    if m and not zip_code:
        out["zipcode"] = m.group(1)
    return out


def page_text(html: str) -> str:
    """Visible text of a detail page (body only, one line)."""
    body = re.search(r"<body[^>]*>(.*)</body>", html, re.S | re.I)
    return re.sub(r"\s+", " ", html_text(body.group(1) if body else html))


def canton(zip_code: str) -> str:
    return canton_of_zip(zip_code)
