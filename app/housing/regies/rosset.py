"""Rosset (rosset.ch) — régie of Geneva, Lausanne and Fribourg.

A Next.js site: its rental page carries the listings as data in the page
(the `self.__next_f.push(...)` chunks hold a `"properties":[…]` list). With
`types=appartement,maison` and a large `page` (the list grows by 24 per
page), one request returns every flat and house: title, address, town,
canton, position, rooms, surface, availability and « CHF 1'300/mois (+ CHF
225 charges) ». The detail page of a listing that could match gives the ad's
text.
"""
from __future__ import annotations

import html as html_lib
import json
import re

from ..common import RunContext, listing, parse_chf, to_float
from ..http import Fetcher, SourceError
from ..places import ResolvedZone
from .common import canton, category_in, could_match, covers, enrich, first_number, page_text, zip_for

KEY = "rosset"
LABEL = "Rosset"
COLOR = "#495057"
NOTE = "Genève, Vaud, Fribourg · tout le catalogue en une page"
FULL_TEXT = False
GROUP = "regie"
CANTONS = {"GE", "VD", "FR"}

BASE = "https://www.rosset.ch"
URL = f"{BASE}/louer"
PAGES = 20               # 24 listings per page: up to 480 in one request
TYPES = {"appartement": "apartment", "maison": "house"}


def _chunks(page: str) -> str:
    """The React Server Components payload, decoded."""
    parts = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', page, re.S)
    return "".join(json.loads(f'"{p}"') for p in parts)


def parse_properties(page: str) -> tuple[list[dict], int | None]:
    data = _chunks(page)
    start = data.find('"properties":[')
    if start < 0:
        raise SourceError("rosset.ch : la liste des biens a changé de forme")
    props, _ = json.JSONDecoder().raw_decode(data[start + len('"properties":'):])
    total = re.search(r'"totalCount":(\d+)', data)
    return [p for p in props if isinstance(p, dict)], int(total.group(1)) if total else None


def to_listing(p: dict) -> dict | None:
    category = TYPES.get(p.get("propertyType") or "")
    if not category:
        return None  # parkings, ateliers, bureaux, dépôts
    title = html_lib.unescape(p.get("location") or "")
    rooms = to_float(p.get("roomsRaw")) or to_float(re.sub(r"[^\d.,]", "", p.get("rooms") or "")) or None
    if category == "apartment":
        category = category_in(title, rooms)
    price_text = p.get("price") or ""
    net = parse_chf(price_text.split("(")[0]) or p.get("priceRaw")
    charges = parse_chf((re.search(r"\+\s*CHF\s*([\d'’  .,]+)\s*charges", price_text) or [None, None])[1])
    address = [a.strip() for a in (p.get("address") or "").split(",")]
    zip_code = next((a for a in address if re.fullmatch(r"\d{4}", a)), "") or \
        zip_for(p.get("city") or "", p.get("lat"), p.get("lng"))
    available = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", p.get("availabilityDate") or "")
    lat, lng = p.get("lat"), p.get("lng")
    images = [u for u in p.get("images") or [] if isinstance(u, str)][:12]
    return listing(
        source=KEY, source_id=str(p.get("id")), url=f"{BASE}/{p.get('uri') or ''}", title=title, category=category,
        price=(net + charges) if net and charges else net, charges=charges, rooms=rooms,
        surface=first_number(p.get("surface") or ""), street=address[0] if len(address) > 2 else "",
        zipcode=zip_code, city=p.get("city") or "", canton=canton(zip_code),
        lat=lat, lng=lng, geo_precision="exact" if lat is not None else "zip",
        available_from=f"{available.group(3)}-{available.group(2)}-{available.group(1)}" if available else None,
        image_url=images[0] if images else "", images=images, agency="Rosset",
    )


def read_detail(fetcher: Fetcher, card: dict) -> dict:
    """The ad's text: after the « Town, Street » line, up to the viewing contact."""
    resp = fetcher.get(card["url"])
    if resp.status_code != 200:
        return {}
    text = page_text(resp.text)
    place = f"{card.get('city')}, {card.get('street')}" if card.get("street") else card.get("city") or ""
    start = text.find(place) if place else -1
    if start < 0:
        return {}
    body = text[start + len(place):]
    end = re.search(r"\s(?:Contact visite|Remarques visite|Situation géographique|Biens similaires)", body)
    return {"description": (body[: end.start()] if end else body).strip()[:3000]}


def search(criteria: dict, zones: list[ResolvedZone], ctx: RunContext) -> list[dict]:
    if not covers(zones, CANTONS, ctx, LABEL):
        return []
    fetcher = Fetcher()
    try:
        resp = fetcher.get(URL, params={"types": ",".join(TYPES), "page": PAGES})
        if resp.status_code != 200:
            raise SourceError(f"rosset.ch a répondu {resp.status_code}")
        props, total = parse_properties(resp.text)
        if total and total > len(props):
            ctx.warn(f"{LABEL} : {total - len(props)} annonce(s) au-delà de la page lue")
        candidates = [c for c in (to_listing(p) for p in props) if c and could_match(c, criteria, zones)]
        ctx.progress(found=len(candidates), pages=1, requests=fetcher.requests)
        return enrich(fetcher, candidates, ctx, LABEL, read_detail)
    finally:
        ctx.progress(requests=fetcher.requests)
        fetcher.close()
