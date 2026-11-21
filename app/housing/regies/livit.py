"""Livit (livit.ch) — one of Switzerland's largest régies, with a Lausanne
branch.

The Drupal search page `/fr/search/residential` is server-rendered, 12 cards
per page (`page=N`, from 0), and filters on a circle — `geo[value]` =
« lat,lng » and `geo[distance][from]` = one of DISTANCES km — plus rooms,
surface and rent. The zones of a search become one circle (or one per zone
when together they reach too far). A card gives title, street + postcode +
town, rooms, floor, availability, surface and the rent **charges included**;
the detail page of a card that could match adds net rent and charges, the
equipment list and the full text, and its address is placed by the federal
geocoder.
"""
from __future__ import annotations

import html as html_lib
import math
import re

from ..common import RunContext, listing, parse_chf, to_float, to_int
from ..http import Fetcher, SourceError
from ..places import CANTON_NAMES, ResolvedZone, haversine_km
from .common import (canton, category_in, could_match, covers, enrich, first_number, locate_address,
                     money_after, net_to_gross, page_text)

KEY = "livit"
LABEL = "Livit"
COLOR = "#862e9c"
NOTE = "Toute la Suisse · lu autour des zones de la recherche"
FULL_TEXT = False
GROUP = "regie"
CANTONS = set(CANTON_NAMES)

BASE = "https://www.livit.ch"
LIST = f"{BASE}/fr/search/residential"
PAGE_SIZE = 12
DISTANCES = (1, 2, 5, 10, 15, 20, 30, 40)   # the radii the site accepts, km
CATEGORY_CODES = {"apartment": "APPT", "furnished": "APPT", "room": "APPT", "house": "HOUSE"}
FEATURE_WORDS = {"Ascenseur": "elevator", "Balcon": "balcony", "Terrasse": "balcony", "Animaux autorisés": "pets",
                 "Jardin": "garden", "Lave-linge": "washing_machine", "Place de parc": "parking",
                 "Garage": "parking", "Accès sans obstacles": "wheelchair", "Minergie": "new_build"}


def _radius(lat: float, lon: float, bbox: tuple) -> float:
    s, w, n, e = bbox
    return max(haversine_km(lat, lon, y, x) for y in (s, n) for x in (w, e))


def circles(zones: list[ResolvedZone]) -> tuple[list[tuple[float, float, int]], list[str]]:
    """Zones → (lat, lng, km) circles covering them: one around them all when
    it stays within the site's largest radius, else one per zone. Returns the
    circles and the zones too large to cover entirely."""
    s, w = min(z.bbox[0] for z in zones), min(z.bbox[1] for z in zones)
    n, e = max(z.bbox[2] for z in zones), max(z.bbox[3] for z in zones)
    lat, lon = (s + n) / 2, (w + e) / 2
    reach = _radius(lat, lon, (s, w, n, e))
    parts = [(lat, lon, reach, "")] if reach <= DISTANCES[-1] else \
        [(z.lat, z.lon, _radius(z.lat, z.lon, z.bbox), z.label) for z in zones]
    out, too_wide = [], []
    for lat, lon, km, label in parts:
        if km > DISTANCES[-1] and label:
            too_wide.append(label)
        out.append((round(lat, 5), round(lon, 5), next((d for d in DISTANCES if d >= math.ceil(km)), DISTANCES[-1])))
    return out, too_wide


def _specs(block: str) -> dict:
    """Icon name → value: « rooms » → « 3.5 », « bars » → « 1. Etage », « labels » → « CHF 2'175.- »."""
    pairs = re.findall(r'spritemap\.svg[^"]*#([\w-]+)"\s*>\s*</use>\s*</svg>\s*</div>\s*<div[^>]*>(.*?)</div>',
                       block, re.S)
    return {icon: html_lib.unescape(re.sub(r"<[^>]+>", "", v)).strip() for icon, v in pairs}


def _floor(text: str):
    if not text:
        return None
    if text.lower().startswith("rez"):
        return 0
    return to_int((re.match(r"(-?\d+)", text) or [None, None])[1])


def _available(block: str, text: str):
    when = re.search(r'<time datetime="(\d{4}-\d{2}-\d{2})', block)
    if when:
        return when.group(1)
    return "immediately" if re.search(r"tout de suite|imm[ée]diat", text or "", re.I) else None


def parse_cards(page: str) -> list[dict]:
    # The title link (title, then street / postcode / town spans); the specs link that follows has no spans.
    heads = list(re.finditer(r'<a[^>]*href="/fr/locataire/objet/([\w.]+)"\s*@click[^>]*>\s*<div[^>]*>\s*'
                             r'<div[^>]*>([^<]*)</div>\s*<div>\s*(<span.*?)</div>', page, re.S))
    out = []
    for i, m in enumerate(heads):
        oid = m.group(1)
        block = page[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(page)]
        title = html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2)))).strip()
        spans = [html_lib.unescape(s).strip().rstrip(",") for s in re.findall(r"<span[^>]*>(.*?)</span>", m.group(3), re.S)]
        street, zip_code, city = (spans + ["", "", ""])[:3] if len(spans) >= 3 else ("", "", " ".join(spans))
        specs = _specs(block)
        rooms = to_float(specs.get("rooms")) or None
        image = re.search(rf'src="(/sites/default/files/styles/[^"]*/reo/images/{re.escape(oid)}[^"]*)"', page)
        out.append(listing(
            source=KEY, source_id=oid, url=f"{BASE}/fr/locataire/objet/{oid}", title=title,
            category=category_in(title, rooms), price=parse_chf(specs.get("labels")), rooms=rooms,
            surface=first_number(specs.get("surface", "")), floor=_floor(specs.get("bars")),
            available_from=_available(block, specs.get("calendar", "")),
            street=street, zipcode=zip_code, city=city, canton=canton(zip_code), geo_precision="zip",
            image_url=BASE + html_lib.unescape(image.group(1)) if image else "", agency="Livit",
        ))
    return out


def read_detail(fetcher: Fetcher, card: dict) -> dict:
    resp = fetcher.get(card["url"])
    if resp.status_code != 200:
        return {}
    text = page_text(resp.text)
    props = re.search(r"Properties\s*(.+?)\s*Référence", text)
    desc = re.search(r"Description\s*(.+?)\s*(?:Location Adresse|Contacter l'annonceur)", text)
    details = {
        "charges": money_after(text, r"Frais accessoires"),
        "total": money_after(text, r"Loyer brut"),
        "description": desc.group(1)[:3000] if desc else "",
        "features": {f for word, f in FEATURE_WORDS.items() if props and word in props.group(1)},
    }
    details = net_to_gross(details, money_after(text, r"Loyer net"))
    details.update(locate_address(card.get("street", ""), card.get("zipcode", ""), card.get("city", "")))
    return details


def search(criteria: dict, zones: list[ResolvedZone], ctx: RunContext) -> list[dict]:
    if not covers(zones, CANTONS, ctx, LABEL):
        return []
    if not zones:
        ctx.warn(f"{LABEL} : choisissez au moins une zone — ignoré")
        return []
    categories = sorted({CATEGORY_CODES[c] for c in criteria.get("categories") or [] if c in CATEGORY_CODES})
    params = {"sort": "changed", "order": "desc"}
    for ours, theirs in (("price_min", "price[min]"), ("price_max", "price[max]"), ("rooms_min", "rooms[min]"),
                         ("rooms_max", "rooms[max]"), ("surface_min", "surface[min]"), ("surface_max", "surface[max]")):
        if criteria.get(ours) is not None:
            params[theirs] = f"{criteria[ours]:g}"
    found, too_wide = circles(zones)
    if too_wide:
        ctx.warn(f"{LABEL} : rayon limité à {DISTANCES[-1]} km autour de {', '.join(too_wide)}")
    fetcher = Fetcher()
    try:
        candidates: dict[str, dict] = {}
        pages = 0
        for code in categories:
            for lat, lng, km in found:
                query = {**params, "category": code, "geo[value]": f"{lat},{lng}", "geo[distance][from]": km}
                for page in range(ctx.max_pages):
                    resp = fetcher.get(LIST, params={**query, **({"page": page} if page else {})})
                    if resp.status_code != 200:
                        raise SourceError(f"livit.ch a répondu {resp.status_code}")
                    pages += 1
                    cards = parse_cards(resp.text)
                    for c in cards:
                        if could_match(c, criteria, zones):
                            candidates.setdefault(c["source_id"], c)
                    ctx.progress(found=len(candidates), pages=pages, requests=fetcher.requests)
                    if len(cards) < PAGE_SIZE or ctx.caught_up([c["source_id"] for c in cards]):
                        break
        return enrich(fetcher, list(candidates.values()), ctx, LABEL, read_detail)
    finally:
        ctx.progress(requests=fetcher.requests)
        fetcher.close()
