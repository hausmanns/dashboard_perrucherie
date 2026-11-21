"""Publiaz immobilier (publiaz.ch) — Vaud régie: Renens, Pully, Montreux, Rolle.

The rental page is a small app over the site's own JSON API: one POST to
`/wp-json/valdev-publimmo/v1/objets` returns every object, sales included —
type, rooms, **net** rent and charges, town with its position, title, photos
and link. `GET /objets/{id}` adds what a card lacks: surface, floor, street
(placed by the federal geocoder) and the full text, read once for an object
that could match.
"""
from __future__ import annotations

import re

from ..common import RunContext, listing, to_float, to_int
from ..http import Fetcher, SourceError
from ..places import ResolvedZone, normalize
from .common import canton, category_in, could_match, covers, enrich, locate_address

KEY = "publiaz"
LABEL = "Publiaz immobilier"
COLOR = "#5c940d"
NOTE = "Ouest lausannois, Lavaux, Riviera, La Côte"
FULL_TEXT = False
GROUP = "regie"
CANTONS = {"VD"}

BASE = "https://publiaz.ch"
API = f"{BASE}/wp-json/valdev-publimmo/v1/objets"
TYPES = {"appartement": "apartment", "maison": "house", "villa": "house"}


def _photos(obj: dict) -> list[str]:
    return [u for u in (p.get("medium_url_export") or p.get("original_url_export")
                        for p in obj.get("photos") or [] if isinstance(p, dict)) if u][:12]


def to_listing(obj: dict) -> dict | None:
    if obj.get("transaction") != "Location":
        return None
    category = TYPES.get(normalize(obj.get("type") or ""))
    if not category:
        return None  # places de parc, commerces
    title = (obj.get("titre") or "").strip()
    rooms = to_float(obj.get("nb_piece")) or None
    if category == "apartment":
        category = category_in(title, rooms)
    net = to_int(obj.get("prix")) if obj.get("afficher_prix", 1) else None
    charges = to_int(obj.get("charges")) or None
    loc = obj.get("localite") or {}
    lat = to_float(obj.get("latitude")) or to_float(loc.get("latitude"))
    lng = to_float(obj.get("longitude")) or to_float(loc.get("longitude"))
    zip_code = str(loc.get("npa") or "")
    images = _photos(obj)
    return listing(
        source=KEY, source_id=str(obj.get("id")), url=f"{BASE}/{(obj.get('url') or '').lstrip('/')}",
        title=title, category=category, price=(net + charges) if net and charges else net, charges=charges,
        rooms=rooms, zipcode=zip_code, city=loc.get("nom") or "", canton=canton(zip_code),
        lat=lat, lng=lng, geo_precision="exact" if obj.get("latitude") else "zip",
        image_url=images[0] if images else "", images=images, agency="Publiaz immobilier",
    )


def _street(raw: str) -> str:
    """« 48, chemin de Clair-Joly » → « Chemin de Clair-Joly 48 »."""
    m = re.fullmatch(r"(\d+\s?\w?),\s*(.+)", (raw or "").strip())
    if not m:
        return (raw or "").strip()
    name = m.group(2).strip()
    return f"{name[:1].upper()}{name[1:]} {m.group(1).replace(' ', '')}"


def read_detail(fetcher: Fetcher, card: dict) -> dict:
    resp = fetcher.get(f"{API}/{card['source_id']}")
    if resp.status_code != 200:
        return {}
    try:
        obj = resp.json()
    except ValueError:
        return {}
    floor = str(obj.get("etage") or "").strip().lower()
    details = {
        "surface": to_int(obj.get("surface_habitable")) or None,
        "floor": 0 if floor.startswith("rez") else to_int(re.sub(r"\D", "", floor) or None),
        "street": _street(obj.get("adresse")),
        "description": (obj.get("desc") or "").strip()[:3000],
        "images": _photos(obj) or card.get("images"),
    }
    if details["street"]:
        details.update(locate_address(details["street"], card.get("zipcode", ""), card.get("city", "")))
    return details


def search(criteria: dict, zones: list[ResolvedZone], ctx: RunContext) -> list[dict]:
    if not covers(zones, CANTONS, ctx, LABEL):
        return []
    fetcher = Fetcher()
    try:
        resp = fetcher.request("POST", API, files={"lang": (None, "fr")})
        if resp.status_code != 200:
            raise SourceError(f"publiaz.ch a répondu {resp.status_code}")
        try:
            objects = resp.json()
        except ValueError:
            raise SourceError("publiaz.ch : réponse illisible")
        if not isinstance(objects, list):
            raise SourceError("publiaz.ch : la liste des biens a changé de forme")
        candidates = [c for c in (to_listing(o) for o in objects if isinstance(o, dict))
                      if c and could_match(c, criteria, zones)]
        ctx.progress(found=len(candidates), pages=1, requests=fetcher.requests)
        return enrich(fetcher, candidates, ctx, LABEL, read_detail)
    finally:
        ctx.progress(requests=fetcher.requests)
        fetcher.close()
