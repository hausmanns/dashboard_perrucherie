"""Retraites Populaires (retraitespopulaires.ch) — the Vaud pension fund, one of
the canton's biggest landlords (often moderate rents).

Server-rendered list, 12 cards per page, newest first:
`/immobilier/louer/louer-un-appartement?type=0&page=N` (type 0 = flats; the
seniors-only flats, type 2, are left out). A card gives the net rent, surface,
availability, rooms (in its title) and street + town, but no position: a card
that could match gets its detail page read once (charges, total rent, floor,
text) and its address placed by the federal geocoder.
"""
from __future__ import annotations

import html as html_lib
import re

from ..common import RunContext, listing, parse_chf, to_int
from ..http import Fetcher, SourceError
from ..places import ResolvedZone
from .common import (canton, category_in, could_match, covers, enrich, locate_address, net_to_gross,
                     page_text, rooms_in, zip_for)

KEY = "retraites_populaires"
LABEL = "Retraites Populaires"
COLOR = "#2f9e44"
NOTE = "Vaud · caisse de pension, loyers souvent modérés"
FULL_TEXT = False
GROUP = "regie"
CANTONS = {"VD"}

BASE = "https://www.retraitespopulaires.ch"
LIST = f"{BASE}/immobilier/louer/louer-un-appartement"
PAGE_SIZE = 12


def parse_cards(page: str) -> list[dict]:
    out = []
    for m in re.finditer(r'<a href="(/detail-biens-immobilier\?uniqueId=[^"]+)"[^>]*>\s*<article[^>]*data-offer-id="(\d+)"(.*?)</article>',
                         page, re.S):
        link, oid, block = m.group(1), m.group(2), m.group(3)
        title = re.search(r"<h3>(.*?)</h3>", block, re.S)
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip() if title else "Appartement"
        surface = re.search(r'class="specs">\s*≈?\s*(\d+)\s*m', block)
        price = re.search(r'class="value">([^<]+)<', block)
        address = re.search(r'class="address">(.*?)</span>', block, re.S)
        street, city = "", ""
        if address:
            parts = [html_lib.unescape(p).strip() for p in re.split(r"<br\s*/?>", address.group(1))]
            street, city = (parts + [""])[:2] if len(parts) > 1 else ("", parts[0])
        image = re.search(r'<img src="([^"]+)"', block)
        available = re.search(r'class="availability">\s*([^<]+?)\s*<', block)
        rooms = rooms_in(title)
        out.append(listing(
            source=KEY, source_id=oid, url=BASE + html_lib.unescape(link),
            title=title, category=category_in(title, rooms), price=parse_chf(price.group(1)) if price else None,
            rooms=rooms, surface=to_int(surface.group(1)) if surface else None,
            street=street, city=city, zipcode=zip_for(city), geo_precision="zip",
            available_from=_available(available.group(1) if available else ""),
            image_url=html_lib.unescape(image.group(1)) if image else "",
            agency="Retraites Populaires",
        ))
    return out


MONTHS = {m: i for i, m in enumerate(("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
                                      "août", "septembre", "octobre", "novembre", "décembre"), 1)}


def _available(text: str):
    t = (text or "").lower()
    if "de suite" in t or "immédiat" in t:
        return "immediately"
    m = re.search(r"(\d{1,2})(?:er)?\s+([a-zéû]+)\s+(\d{4})", t)
    if m and m.group(2) in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    return None


def read_detail(fetcher: Fetcher, card: dict) -> dict:
    resp = fetcher.get(card["url"])
    if resp.status_code != 200:
        return {}
    text = page_text(resp.text)
    amount = lambda label: parse_chf((re.search(rf"([\d'’]+)\.-\s*{label}", text) or [None, None])[1])
    floor = re.search(r"(Rez|\d+)\s*(?:ème|er|e)?\s*Étage", text)
    desc = re.search(r"Description\s*(.+?)\s*(?:Environnement|Transports|Contact|Postuler|$)", text)
    details = net_to_gross({
        "charges": amount("Charges"),
        "total": amount(r"Loyer Total"),
        "floor": (0 if floor.group(1).lower() == "rez" else to_int(floor.group(1))) if floor else None,
        "description": desc.group(1)[:3000] if desc else "",
    }, amount(r"Loyer net") or card.get("price"))
    details.update(locate_address(card.get("street", ""), "", card.get("city", "")))
    if details.get("zipcode") or card.get("zipcode"):
        details["canton"] = canton(details.get("zipcode") or card.get("zipcode"))
    return details


def search(criteria: dict, zones: list[ResolvedZone], ctx: RunContext) -> list[dict]:
    if not covers(zones, CANTONS, ctx, LABEL):
        return []
    if not set(criteria.get("categories") or []) & {"apartment", "furnished"}:
        return []
    fetcher = Fetcher()
    try:
        candidates: dict[str, dict] = {}
        for page in range(1, ctx.max_pages + 1):
            resp = fetcher.get(LIST, params={"type": 0, **({"page": page} if page > 1 else {})})
            if resp.status_code != 200:
                raise SourceError(f"retraitespopulaires.ch a répondu {resp.status_code}")
            cards = parse_cards(resp.text)
            ctx.note_ids([c["source_id"] for c in cards])
            for c in cards:
                if could_match(c, criteria, zones):
                    candidates.setdefault(c["source_id"], c)
            ctx.progress(found=len(candidates), pages=page, requests=fetcher.requests)
            if len(cards) < PAGE_SIZE or ctx.caught_up([c["source_id"] for c in cards]):
                break
        return enrich(fetcher, list(candidates.values()), ctx, LABEL, read_detail)
    finally:
        ctx.progress(requests=fetcher.requests)
        fetcher.close()
