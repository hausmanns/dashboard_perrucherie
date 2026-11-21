"""Bernard Nicod (bernard-nicod.ch) — large Vaud and Romandie régie.

The Drupal site has two JSON endpoints behind its search page:
- `/fr/api/search-markers/residentiel/location` — every listing's id and
  position, in one call;
- `/fr/api/search-list/residentiel/location?page=N` — 12 cards per page (HTML
  inside JSON), newest first, filtered by `b_min`/`b_max` (net rent) and
  `s_min`/`s_max` (surface).
Cards give town, rooms, title and **net** rent; the detail page of a card
that could match adds charges, surface, floor, street and the full text.
"""
from __future__ import annotations

import html as html_lib
import re

from ..common import RunContext, listing, parse_chf, to_int
from ..http import Fetcher, SourceError
from ..places import ResolvedZone
from .common import (CHARGES_MARGIN, canton, category_in, could_match, covers, enrich, money_after,
                     net_to_gross, page_text, rooms_in, zip_for)

KEY = "nicod"
LABEL = "Bernard Nicod"
COLOR = "#b5495b"
NOTE = "Vaud, Genève, Valais, Fribourg, Neuchâtel"
FULL_TEXT = False
GROUP = "regie"
CANTONS = {"VD", "GE", "VS", "FR", "NE"}

BASE = "https://www.bernard-nicod.ch"
PAGE_SIZE = 12
XHR = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}


def _markers(fetcher: Fetcher) -> dict[str, tuple[float, float]]:
    data = fetcher.get_json(f"{BASE}/fr/api/search-markers/residentiel/location", headers=XHR)
    return {str(m["id"]): (m["lat"], m["lng"]) for m in data.get("markers", []) if m.get("lat")}


def parse_cards(fragment: str, positions: dict) -> list[dict]:
    starts = [m.start() for m in re.finditer(r'<article class="node node--type-objects-cp', fragment)]
    out = []
    for i, pos in enumerate(starts):
        block = fragment[pos: starts[i + 1] if i + 1 < len(starts) else len(fragment)]
        nid = re.search(r'data-search-entity-id="(\d+)"', block)
        if not nid:
            continue
        nid = nid.group(1)
        link = re.search(r'href="(/fr/annonces/[^"]+)"', block)
        image = re.search(r'data-src="([^"]+)"', block)
        town = re.search(r'field--name-field-cp-address.*?<div class="field__item">([^<]*)</div>', block, re.S)
        title = re.search(r'class="object-title[^"]*">\s*<p[^>]*>(.*?)</p>', block, re.S)
        infos = [html_lib.unescape(re.sub(r"<[^>]+>", "", p)).strip()
                 for p in re.findall(r'<p class="mb-0">(.*?)</p>', block, re.S)]
        title = html_lib.unescape(re.sub(r"\s+", " ", title.group(1))).strip() if title else ""
        rooms = next((rooms_in(t) for t in infos if rooms_in(t)), None) or rooms_in(title)
        price = next((parse_chf(t) for t in infos if t.startswith("CHF")), None)
        city = html_lib.unescape(town.group(1)).strip() if town else ""
        lat, lng = positions.get(nid, (None, None))
        out.append(listing(
            source=KEY, source_id=nid, url=BASE + (link.group(1) if link else f"/fr/node/{nid}"),
            title=title, category=category_in(title, rooms), price=price, rooms=rooms,
            city=city, zipcode=zip_for(city, lat, lng), lat=lat, lng=lng,
            geo_precision="exact" if lat is not None else "zip",
            image_url=BASE + html_lib.unescape(image.group(1)) if image else "",
            agency="Bernard Nicod",
        ))
    return out


def read_detail(fetcher: Fetcher, card: dict) -> dict:
    resp = fetcher.get(card["url"])
    if resp.status_code != 200:
        return {}
    text = page_text(resp.text)
    net = money_after(text, r"Loyer net") or card.get("price")
    details = {
        "charges": money_after(text, r"Charges"),
        "surface": to_int((re.search(r"Surface habitable\s*(\d+)\s*m", text) or [None, None])[1]),
        "floor": _floor(text),
        "available_from": _available(text),
        "description": _between(text, "Description", ("Biens à proximité", "Calculez", "Contacter")),
    }
    place = re.search(r"Emplacement\s*(.+?)\s*(?:Géolocalisation|Y aller|Description)", text)
    if place:
        zip_m = re.search(r"\b(\d{4})\b,?\s*(.*)$", place.group(1))
        if zip_m:
            details["zipcode"] = zip_m.group(1)
            details["street"] = _street(zip_m.group(2))
    details = net_to_gross(details, net)
    details["canton"] = canton(details.get("zipcode") or card.get("zipcode"))
    return details


def _street(raw: str) -> str:
    """« Cocarde, chemin de la 11A » → « chemin de la Cocarde 11A ». A bare
    place name (no house number) is not a street: left empty."""
    if not re.search(r"\d", raw or ""):
        return ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) == 2:
        number = re.search(r"\s(\d+\w?)$", parts[1])
        kind = parts[1][: number.start()] if number else parts[1]
        return f"{kind} {parts[0]}{' ' + number.group(1) if number else ''}".strip()
    return raw.strip()


def _floor(text: str):
    m = re.search(r"Étage\s*(Rez|\d+)", text, re.I)
    if not m:
        return None
    return 0 if m.group(1).lower().startswith("rez") else to_int(m.group(1))


def _available(text: str):
    m = re.search(r"Disponibilité\s*(?:Disponibilité\s*)?(De suite|Immédiatement|\d{2}\.\d{2}\.\d{4})", text, re.I)
    if not m:
        return None
    value = m.group(1)
    if not value[0].isdigit():
        return "immediately"
    d, mth, y = value.split(".")
    return f"{y}-{mth}-{d}"


def _between(text: str, start: str, ends: tuple) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    chunk = text[i + len(start):]
    cut = min([chunk.find(e) for e in ends if chunk.find(e) > 0] or [len(chunk)])
    return re.sub(r"^\s*body\s*", "", chunk[:cut]).strip()[:3000]


def search(criteria: dict, zones: list[ResolvedZone], ctx: RunContext) -> list[dict]:
    if not covers(zones, CANTONS, ctx, LABEL):
        return []
    params = {}
    if criteria.get("price_max") is not None:
        params["b_max"] = criteria["price_max"]  # net ≤ gross ≤ max
    if criteria.get("price_min") is not None:
        params["b_min"] = int(criteria["price_min"] / (1 + CHARGES_MARGIN))
    for ours, theirs in (("surface_min", "s_min"), ("surface_max", "s_max")):
        if criteria.get(ours) is not None:
            params[theirs] = criteria[ours]
    fetcher = Fetcher()
    try:
        positions = _markers(fetcher)
        candidates: dict[str, dict] = {}
        for page in range(ctx.max_pages):
            data = fetcher.get_json(f"{BASE}/fr/api/search-list/residentiel/location",
                                    params={**params, **({"page": page} if page else {})}, headers=XHR)
            cards = parse_cards(data.get("html") or "", positions)
            ctx.note_ids([c["source_id"] for c in cards])
            for c in cards:
                if could_match(c, criteria, zones):
                    candidates.setdefault(c["source_id"], c)
            ctx.progress(found=len(candidates), pages=page + 1, requests=fetcher.requests)
            if len(cards) < PAGE_SIZE or ctx.caught_up([c["source_id"] for c in cards]):
                break
        return enrich(fetcher, list(candidates.values()), ctx, LABEL, read_detail)
    finally:
        ctx.progress(requests=fetcher.requests)
        fetcher.close()
