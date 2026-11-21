"""Gérance Borgeaud (gerance-borgeaud.ch) — small Lausanne régie.

The WordPress page `/a-louer/` lists every object as a card: rooms,
bathrooms, surface, parking, title, rent, postcode + town and a link. The
detail page of a card that could match gives the net rent, charges, floor,
availability and the text (with lift, balcony…). No position is published,
so the listing is placed on its postcode.
"""
from __future__ import annotations

import html as html_lib
import re

from ..common import RunContext, listing, parse_chf, to_float, to_int
from ..http import Fetcher, SourceError
from ..places import ResolvedZone
from .common import (canton, category_in, could_match, covers, enrich, first_number, money_after, net_to_gross,
                     page_text, rooms_in, zip_for)

KEY = "borgeaud"
LABEL = "Gérance Borgeaud"
COLOR = "#a61e4d"
NOTE = "Lausanne et environs · quelques objets"
FULL_TEXT = False
GROUP = "regie"
CANTONS = {"VD"}

URL = "https://gerance-borgeaud.ch/a-louer/"


def parse_cards(page: str) -> list[dict]:
    out = []
    for block in re.split(r'<div class="property-card2">', page)[1:]:
        link = re.search(r'<h4 class="property-card-title"><a href="([^"]+)">(.*?)</a>', block, re.S)
        if not link:
            continue
        url, title = link.group(1), html_lib.unescape(re.sub(r"<[^>]+>", "", link.group(2))).strip()
        meta = {icon: html_lib.unescape(v).strip() for icon, v in
                re.findall(r'<i class="fa-light fa-([a-z-]+)[^"]*"></i>([^<]*)</span>', block)}
        price = re.search(r'property-card-price">([^<]*)<', block)
        place = re.search(r'property-card-location">([^<]*)<', block)
        place = html_lib.unescape(place.group(1)).strip() if place else ""
        zip_m = re.match(r"(\d{4})\s+(.*)", place)
        image = re.search(r'<img[^>]+src="([^"]+)"', block)
        rooms = to_float(meta.get("door-open")) or rooms_in(title)
        city = zip_m.group(2) if zip_m else place
        zip_code = zip_m.group(1) if zip_m else zip_for(city)
        out.append(listing(
            source=KEY, source_id=url.rstrip("/").rsplit("/", 1)[-1], url=url, title=title,
            category=category_in(title, rooms), price=parse_chf(html_lib.unescape(price.group(1))) if price else None,
            rooms=rooms, surface=first_number(meta.get("arrow-up-right-and-arrow-down-left-from-center", "")),
            zipcode=zip_code, city=city, canton=canton(zip_code), geo_precision="zip",
            features={"parking"} if meta.get("square-parking", "").lower() == "oui" else set(),
            image_url=image.group(1) if image else "", agency="Gérance Borgeaud",
        ))
    return out


def read_detail(fetcher: Fetcher, card: dict) -> dict:
    resp = fetcher.get(card["url"])
    if resp.status_code != 200:
        return {}
    text = page_text(resp.text)
    about = re.search(r"A propos de ce bien\s*(.+?)\s*(?:Type A louer|En images|Emplacement)", text)
    floor = re.search(r"Étage\s*(Rez|\d+)", text, re.I)
    features = {f for f, word in (("elevator", "Ascenseur"), ("parking", "Parking")) if re.search(rf"{word}\s*Oui", text)}
    details = {
        "charges": money_after(text, r"Charges"),
        "total": money_after(text, r"Loyer total"),
        "floor": (0 if floor.group(1).lower() == "rez" else to_int(floor.group(1))) if floor else None,
        "description": about.group(1)[:3000] if about else "",
        "features": features,
    }
    return net_to_gross(details, money_after(text, r"Loyer net") or card.get("price"))


def search(criteria: dict, zones: list[ResolvedZone], ctx: RunContext) -> list[dict]:
    if not covers(zones, CANTONS, ctx, LABEL):
        return []
    fetcher = Fetcher()
    try:
        resp = fetcher.get(URL)
        if resp.status_code != 200:
            raise SourceError(f"gerance-borgeaud.ch a répondu {resp.status_code}")
        candidates = [c for c in parse_cards(resp.text) if could_match(c, criteria, zones)]
        ctx.progress(found=len(candidates), pages=1, requests=fetcher.requests)
        return enrich(fetcher, candidates, ctx, LABEL, read_detail)
    finally:
        ctx.progress(requests=fetcher.requests)
        fetcher.close()
