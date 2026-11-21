"""Cogestim (cogestim.ch) — Lausanne régie, Vaud and Fribourg.

The WordPress rental page carries its map data as JSON (`var items = {…}`):
every listing with title, town, position, **net** rent, street + postcode,
photo and link — one request for the whole catalogue. The link tells the kind
of object (`/louer/appartement/…`, or in German `/mieten/wohnung/…`). A
listing that could match gets its detail page read once: charges, surface,
floor, lift, balcony and the text.

The object number keying `items` is **not** stable: the site re-imports its
catalogue several times a day and every flat comes back under a new number.
A listing is known by the town and slug of its link instead (`stable_id`).
"""
from __future__ import annotations

import html as html_lib
import json
import re
from urllib.parse import unquote, urlsplit

from ..common import RunContext, listing, parse_chf, to_int
from ..http import Fetcher, SourceError
from ..places import ResolvedZone
from .common import (canton, category_in, could_match, covers, enrich, money_after, net_to_gross, page_text,
                     rooms_in, zip_for)

KEY = "cogestim"
LABEL = "Cogestim"
COLOR = "#e67700"
NOTE = "Vaud, Fribourg · tout le catalogue en une page"
FULL_TEXT = False
GROUP = "regie"
CANTONS = {"VD", "FR"}

URL = "https://cogestim.ch/louer/"
MARKER = "var items = "
LIVING = ("appartement", "maison", "villa", "studio", "duplex", "attique", "loft", "chambre",
          "wohnung", "haus", "zimmer")


def _link_parts(link: str) -> list[str]:
    return [unquote(p).lower() for p in urlsplit(link or "").path.strip("/").split("/")]


def stable_id(link: str) -> str | None:
    """« …/mieten/wohnung/pully/detail/bel-appartement-… » → « pully/bel-appartement-… »:
    the same in French and German, and across the site's re-imports."""
    parts = _link_parts(link)
    return f"{parts[-3]}/{parts[-1]}" if len(parts) >= 4 and parts[-2] == "detail" else None


def link_kind(link: str) -> str:
    """« …/louer/appartement/… » → « appartement », « …/mieten/geschäftsräume/… » → « geschäftsräume »."""
    parts = _link_parts(link)
    return parts[1] if len(parts) >= 4 and parts[-2] == "detail" else ""


def is_living(link: str) -> bool:
    kind = link_kind(link)
    return not kind or any(k in kind for k in LIVING)


def parse_items(page: str) -> list[dict]:
    start = page.find(MARKER)
    if start < 0:
        raise SourceError("cogestim.ch : la liste des biens a changé de forme")
    items, _ = json.JSONDecoder().raw_decode(page[start + len(MARKER):])
    out = []
    for oid, it in items.items():
        link = it.get("link") or ""
        title = html_lib.unescape(it.get("title") or "")
        rooms = rooms_in(title)
        category = category_in(f"{link_kind(link)} {title}", rooms) if is_living(link) else "other"
        lat, lng = (it.get("latlng") or [None, None])[:2]
        street, _, zip_code = (it.get("address") or "").rpartition(",")
        zip_code = re.sub(r"\D", "", zip_code)[:4] or zip_for(it.get("city", ""), lat, lng)
        out.append(listing(
            source=KEY, source_id=stable_id(link) or str(oid), url=link or URL, title=title, category=category,
            price=parse_chf(re.sub(r"<[^>]+>", "", it.get("price") or "")), rooms=rooms,
            street=street.strip().title() if street.isupper() else street.strip(),
            zipcode=zip_code, city=it.get("city") or "", canton=canton(zip_code),
            lat=lat, lng=lng, geo_precision="exact" if lat is not None else "zip",
            image_url=it.get("image") or "", agency="Cogestim",
        ))
    return out


def read_detail(fetcher: Fetcher, card: dict) -> dict:
    resp = fetcher.get(card["url"])
    if resp.status_code != 200:
        return {}
    text = page_text(resp.text)
    net = money_after(text, r"Loyer") or card.get("price")
    floor = re.search(r"Etage\s*(Rez|\d+)", text, re.I)
    available = re.search(r"Disponibilité\s*(De suite|Immédiatement|A convenir|\d{2}\.\d{2}\.\d{4})", text, re.I)
    features = set()
    if re.search(r"Ascenseur\s*oui", text, re.I):
        features.add("elevator")
    if re.search(r"Balcon\s*oui", text, re.I):
        features.add("balcony")
    title = card.get("title") or ""
    after = text.find(title, text.find("Nombre de chambres")) if title else -1
    description = text[after + len(title): after + len(title) + 3000].strip() if after >= 0 else ""
    details = {
        "charges": money_after(text, r"Charges"),
        "surface": to_int((re.search(r"Surface habitable\s*(\d+)\s*m", text) or [None, None])[1]),
        "floor": (0 if floor.group(1).lower() == "rez" else to_int(floor.group(1))) if floor else None,
        "available_from": _available(available.group(1)) if available else None,
        "features": features,
        "description": re.split(r"\s(?:Candidature en ligne|Contactez l'agence|Vous souhaitez)", description)[0],
    }
    return net_to_gross(details, net)


def _available(value: str):
    v = value.lower()
    if v.startswith(("de suite", "imm")):
        return "immediately"
    if v[0].isdigit():
        d, m, y = value.split(".")
        return f"{y}-{m}-{d}"
    return None


def search(criteria: dict, zones: list[ResolvedZone], ctx: RunContext) -> list[dict]:
    if not covers(zones, CANTONS, ctx, LABEL):
        return []
    fetcher = Fetcher()
    try:
        resp = fetcher.get(URL)
        if resp.status_code != 200:
            raise SourceError(f"cogestim.ch a répondu {resp.status_code}")
        cards = parse_items(resp.text)
        candidates = [c for c in cards if could_match(c, criteria, zones)]
        ctx.progress(found=len(candidates), pages=1, requests=fetcher.requests)
        return enrich(fetcher, candidates, ctx, LABEL, read_detail)
    finally:
        ctx.progress(requests=fetcher.requests)
        fetcher.close()
