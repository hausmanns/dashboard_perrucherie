"""Gérance Robert Crot & Cie (gerance-crot.ch) — Lausanne régie.

A WordPress site (RealHomes theme) whose properties are public on the REST
API: `/wp-json/wp/v2/properties`. Each comes with its type and town
(`class_list`), price, size, rooms, address, position and photos
(`property_meta`), and a text that spells out « Loyer net : 3400 CHF Charges :
400 CHF » and the availability — so one request covers everything.
"""
from __future__ import annotations

import html as html_lib
import re

from ..common import RunContext, detect_features, html_text, listing, parse_chf, to_float, to_int
from ..http import Fetcher, SourceError
from ..places import ResolvedZone
from .common import canton, could_match, covers, money_after, net_to_gross, zip_for

KEY = "crot"
LABEL = "Gérance Robert Crot"
COLOR = "#5f3dc4"
NOTE = "Lausanne et environs"
FULL_TEXT = True   # the full ad text comes with every property
GROUP = "regie"
CANTONS = {"VD"}

API = "https://gerance-crot.ch/wp-json/wp/v2/properties"
# No `_fields=` filter: with it, the theme returns an empty `property_meta`.
TYPES = {"appartement": "apartment", "maison": "house", "villa": "house", "studio": "apartment",
         "meuble": "furnished", "chambre": "room"}


def _image(meta: dict) -> list[str]:
    out = []
    for img in meta.get("REAL_HOMES_property_images") or []:
        if not isinstance(img, dict):
            continue
        sizes = img.get("sizes") or {}
        best = (sizes.get("property-thumb-image") or sizes.get("medium_large") or {}).get("url")
        out.append(best or img.get("full_url") or img.get("url"))
    return [u for u in out if u][:12]


def to_listing(p: dict) -> dict | None:
    classes = p.get("class_list") or []
    if isinstance(classes, dict):
        classes = list(classes.values())
    if "property-feature-location" not in classes:
        return None  # à vendre
    kind = next((c.removeprefix("property-type-") for c in classes if c.startswith("property-type-")), "")
    category = next((v for k, v in TYPES.items() if k in kind), None)
    if not category:
        return None  # parkings, commerces
    meta = p.get("property_meta") or {}
    text = html_text(p.get("content", {}).get("rendered", ""))
    flat = re.sub(r"\s+", " ", text)
    loc = meta.get("REAL_HOMES_property_location") or {}
    lat, lng = to_float(loc.get("latitude")), to_float(loc.get("longitude"))
    address = meta.get("REAL_HOMES_property_address") or ""
    street, _, city = address.partition(",")
    city = city.strip() or next((c.removeprefix("property-city-").replace("-", " ").title()
                                  for c in classes if c.startswith("property-city-")), "")
    zip_code = zip_for(city, lat, lng)
    net = money_after(flat, r"Loyer net") or parse_chf(meta.get("REAL_HOMES_property_price"))
    details = net_to_gross({"charges": money_after(flat, r"Charges")}, net)
    floor = re.search(r"Etage\s*:\s*(Rez|\d+)", flat, re.I)
    available = re.search(r"Libre dès\s*:\s*(\d{2})[-.](\d{2})[-.](\d{4})", flat)
    title = html_lib.unescape(p.get("title", {}).get("rendered", "")) or address
    images = _image(meta)
    return listing(
        source=KEY, source_id=str(p["id"]), url=p.get("link") or "",
        title=f"{kind.replace('-', ' ').capitalize()} — {title}" if kind else title,
        description=text[:3000], category=category,
        price=details.get("price") or net, charges=details.get("charges"),
        rooms=to_float(meta.get("REAL_HOMES_property_bedrooms")) or None,
        surface=to_int(meta.get("REAL_HOMES_property_size")) or None,
        floor=(0 if floor.group(1).lower() == "rez" else to_int(floor.group(1))) if floor else None,
        street=street.strip(), zipcode=zip_code, city=city, canton=canton(zip_code),
        lat=lat, lng=lng, geo_precision="exact" if lat is not None else "zip",
        available_from=f"{available.group(3)}-{available.group(2)}-{available.group(1)}" if available else None,
        features=detect_features(f"{title}\n{text}"), features_known=False,
        image_url=images[0] if images else "", images=images,
        agency="Gérance Robert Crot & Cie", published_at=p.get("date"),
    )


def search(criteria: dict, zones: list[ResolvedZone], ctx: RunContext) -> list[dict]:
    if not covers(zones, CANTONS, ctx, LABEL):
        return []
    fetcher = Fetcher()
    try:
        out = []
        for page in range(1, 6):
            resp = fetcher.get(API, params={"per_page": 100, "page": page})
            if resp.status_code != 200:
                if page > 1:
                    break
                raise SourceError(f"gerance-crot.ch a répondu {resp.status_code}")
            for p in resp.json():
                item = to_listing(p)
                if item and could_match(item, criteria, zones):
                    out.append(item)
            ctx.progress(found=len(out), pages=page, requests=fetcher.requests)
            if page >= int(resp.headers.get("X-WP-TotalPages") or 1):
                break
        return out
    finally:
        ctx.progress(requests=fetcher.requests)
        fetcher.close()
