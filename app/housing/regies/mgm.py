"""MGM Fiduciaire (mgm.ch) — régie of La Conversion (Lutry), Lavaux and Lausanne.

The WordPress page lists each object as a schema.org card: type, rooms,
surface, floor, availability, rent and a short summary, with the town in the
title (« Lausanne-Chailly: … »). The detail page of a card that could match
gives the street (placed by the federal geocoder), the full text and the
equipment list.
"""
from __future__ import annotations

import html as html_lib
import re

from ..common import RunContext, listing, parse_chf, to_float, to_int
from ..http import Fetcher, SourceError
from ..places import ResolvedZone
from .common import (canton, category_in, could_match, covers, enrich, first_number, locate_address,
                     money_after, net_to_gross, page_text, rooms_in, zip_for)

KEY = "mgm"
LABEL = "MGM Fiduciaire"
COLOR = "#1971c2"
NOTE = "Lavaux, Lausanne · quelques objets"
FULL_TEXT = False
GROUP = "regie"
CANTONS = {"VD"}

URL = "https://www.mgm.ch/locations-disponibles-appartements-et-surfaces-commerciales-a-louer/"


def _specs(block: str) -> dict:
    keys = re.findall(r'<span class="key">([^<]*)</span>\s*<span class="value">(.*?)</span>', block, re.S)
    return {html_lib.unescape(k).strip(): html_lib.unescape(re.sub(r"<[^>]+>", "", v)).strip() for k, v in keys}


def parse_cards(page: str) -> list[dict]:
    out = []
    for block in re.split(r'<article itemscope itemtype="http://schema.org/', page)[1:]:
        link = re.search(r'<h2 class="entry-title"[^>]*>\s*<a href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url, title = link.group(1), html_lib.unescape(re.sub(r"<[^>]+>", "", link.group(2))).strip()
        specs = _specs(block)
        kind = specs.get("Type", "")
        rooms = to_float(re.sub(r"[^\d.,]", "", specs.get("Pièces", ""))) or rooms_in(title)
        town = title.split(":", 1)[0] if ":" in title else ""
        summary = re.search(r'<div class="entry-summary">\s*<p>(.*?)</p>', block, re.S)
        image = re.search(r'<img[^>]+src="([^"]+)"', block)
        out.append(listing(
            source=KEY, source_id=url.rstrip("/").rsplit("/", 1)[-1], url=url, title=title,
            description=html_lib.unescape(re.sub(r"<[^>]+>", "", summary.group(1))).strip() if summary else "",
            category=category_in(title, rooms, kind), price=parse_chf(specs.get("Loyer")),
            rooms=rooms, surface=first_number(specs.get("Surface approx.", "")),
            city=town.split("-")[0].strip(), zipcode=zip_for(town.split("-")[0].strip()), geo_precision="zip",
            image_url=image.group(1) if image else "", agency="MGM Fiduciaire",
        ))
    return out


def read_detail(fetcher: Fetcher, card: dict) -> dict:
    resp = fetcher.get(card["url"])
    if resp.status_code != 200:
        return {}
    text = page_text(resp.text)
    street = re.search(r"Adresse\s*(.+?)\s*(?:Contact|Téléphone|Description)", text)
    city = re.search(r"Localité\s*(.+?)\s*Adresse", text)
    desc = re.search(r"Description de la location\s*(.+?)\s*(?:Galerie Photo|Informations générales)", text)
    features = {f for f, word in (("elevator", "Ascenseur"), ("balcony", "Terrasse"), ("balcony", "Balcon"),
                                  ("wheelchair", "Accès handicapés"), ("parking", "Parking"))
                if re.search(rf"{word}\s*Oui", text)}
    details = {
        "street": street.group(1) if street else "",
        "city": city.group(1) if city else card.get("city"),
        "description": desc.group(1)[:3000] if desc else card.get("description"),
        "charges": money_after(text, r"Charges"),
        "features": features,
    }
    details.update(locate_address(details["street"], "", details["city"] or ""))
    details["canton"] = canton(details.get("zipcode") or card.get("zipcode"))
    return net_to_gross(details, card.get("price"))


def search(criteria: dict, zones: list[ResolvedZone], ctx: RunContext) -> list[dict]:
    if not covers(zones, CANTONS, ctx, LABEL):
        return []
    fetcher = Fetcher()
    try:
        resp = fetcher.get(URL)
        if resp.status_code != 200:
            raise SourceError(f"mgm.ch a répondu {resp.status_code}")
        candidates = [c for c in parse_cards(resp.text) if could_match(c, criteria, zones)]
        ctx.progress(found=len(candidates), pages=1, requests=fetcher.requests)
        return enrich(fetcher, candidates, ctx, LABEL, read_detail)
    finally:
        ctx.progress(requests=fetcher.requests)
        fetcher.close()
