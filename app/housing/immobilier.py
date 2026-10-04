"""immobilier.ch — the portal of the Romandie real-estate agencies (USPI).
Many régies publish there first, or only there. French-speaking cantons.

We read the results fragment the site loads over XHR
(`/Pages/Objects/ObjectsSearch/Results`) rather than the plain list page,
because only the fragment honours `sort=DateSortDesc` (newest first → a check
stops at the first page with nothing new). 24 cards per page, each with its
position (`data-latlng`), rent + charges, type, rooms, surface and photos —
but no free text: feature/keyword filters can't be verified here, so the
engine keeps those listings and flags what to check.

Query parameters (from the site's own search URLs): `t=rent`, `c=1|2|1;2`
(apartment / house), `p=<place>;<place>`, `pn`/`px` rent, `nrn`/`nrx` rooms,
`sn`/`sx` surface, `page=<n>`.

Places are the site's own codes — `s118` canton of Vaud, `t1779` commune of
Lausanne (matched on its BFS number), `c11119` postcode 1004 — looked up in
the places file its search box downloads (`/js-data/places/<version>-fr.json`),
kept for a day.
"""
from __future__ import annotations

import html as html_lib
import re
import time

from .common import RunContext, detect_features, listing, parse_chf, to_float, to_int
from .http import Fetcher, SourceError
from .places import CANTON_NAMES, ResolvedZone, canton_of_zip, nearest_locality, normalize

KEY = "immobilier"
LABEL = "immobilier.ch"
COLOR = "#1c7ed6"
NOTE = "Suisse romande · régies romandes"
FULL_TEXT = False         # no description on result cards

BASE = "https://www.immobilier.ch"
PAGE_SIZE = 24
COVERED_CANTONS = {"VD", "GE", "VS", "FR", "NE", "JU", "BE"}
CATEGORY_CODES = {"apartment": "1", "house": "2"}
PLACES_TTL_S = 24 * 3600

_places: dict = {"at": 0.0, "by_canton": {}, "by_bfs": {}, "by_zip": {}}


def _load_places(fetcher: Fetcher) -> dict:
    if _places["by_canton"] and time.time() - _places["at"] < PLACES_TTL_S:
        return _places
    page = fetcher.get(f"{BASE}/fr/louer/appartement/vaud/page-1")
    m = re.search(r'places-version="(\d+)"', page.text)
    if not m:
        raise SourceError("immobilier.ch : liste des lieux introuvable")
    data = fetcher.get_json(f"{BASE}/js-data/places/{m.group(1)}-fr.json")
    by_canton, by_bfs, by_zip = {}, {}, {}
    canton_by_name = {normalize(n): code for code, n in CANTON_NAMES.items()}
    for p in data.get("places", []):
        pid = str(p.get("id") or "")
        if pid.startswith("s"):
            code = canton_by_name.get(normalize(p.get("name") or ""))
            if code:
                by_canton[code] = pid
        elif pid.startswith("t") and p.get("officialCode"):
            by_bfs[str(p["officialCode"])] = pid
        elif pid.startswith("c") and p.get("code"):
            by_zip.setdefault(str(p["code"]), pid)
    _places.update(at=time.time(), by_canton=by_canton, by_bfs=by_bfs, by_zip=by_zip)
    return _places


def _place_codes(places: dict, zones: list[ResolvedZone]) -> list[str]:
    codes: list[str] = []
    for z in zones:
        if z.type == "canton":
            codes.append(places["by_canton"].get(z.code))
        elif z.type == "commune":
            codes.append(places["by_bfs"].get(z.code) or None)
            if not codes[-1]:
                codes += [places["by_zip"].get(p) for p in sorted(z.zips or [])]
        elif z.type == "zip":
            codes.append(places["by_zip"].get(z.code))
        elif z.type == "radius":
            if len(z.communes) <= 40:
                codes += [places["by_bfs"].get(b) for b in sorted(z.communes)]
            else:
                codes += [places["by_canton"].get(c) for c in sorted(z.cantons)]
    return list(dict.fromkeys(c for c in codes if c))


def _category_from_type(text: str) -> str:
    key = normalize(text)
    if re.search(r"\b(maison|villa|chalet|ferme)\b", key):
        return "house"
    if re.search(r"\b(meuble|meublee)\b", key):
        return "furnished"
    if re.search(r"\b(chambre|colocation)\b", key):
        return "room"
    return "apartment"


def parse_cards(fragment: str) -> list[dict]:
    starts = [m.start() for m in re.finditer(r'<div id="filter-item-\d+"', fragment)]
    out = []
    for i, pos in enumerate(starts):
        block = fragment[pos: starts[i + 1] if i + 1 < len(starts) else len(fragment)]
        lid = re.search(r'data-id="(\d+)"', block)
        if not lid:
            continue
        latlng = re.search(r'data-latlng="([\d.\-]+),([\d.\-]+)"', block)
        lat, lng = (to_float(latlng.group(1)), to_float(latlng.group(2))) if latlng else (None, None)
        href = re.search(r'<a id="link-result-item-\d+"[^>]*href="([^"]+)"', block)
        url = BASE + html_lib.unescape(href.group(1)) if href else f"{BASE}/fr/louer/{lid.group(1)}"
        price_txt = html_lib.unescape((re.search(r'<strong class="title">([^<]*)</strong>', block) or [None, ""])[1])
        rent = parse_chf(price_txt)
        charges_m = re.search(r"\+\s*([\d'’.]+)\s*[.-]*\s*charges", price_txt)
        charges = parse_chf(charges_m.group(1)) if charges_m else None
        obj_type = html_lib.unescape((re.search(r'<p class="object-type">([^<]*)</p>', block) or [None, ""])[1]).strip()
        address = re.search(r'<p class="object-type">[^<]*</p>\s*<p>([^<]*)</p>', block)
        address = html_lib.unescape(address.group(1)).strip() if address else ""
        city, _, street = address.partition(",")
        surface = re.search(r'<span class="space">\s*([\d\'’.]+)\s*m', block)
        rooms = re.search(r'<i class="icon-plan"[^>]*></i>\s*([\d.]+)', block)
        images = [BASE + html_lib.unescape(src) for src in re.findall(r'data-src="(/Medias/[^"]+/images/[^"]+)"', block)]
        agency = re.search(r'<div class="logo-box">.*?alt="([^"]*)"', block, re.S)
        # The URL slug is the only free text on a card: « …-attique-entierement-renove-1588522 ».
        slug_words = (href.group(1).rsplit("/", 1)[-1] if href else "").replace("-", " ")
        city = city.strip()
        near = nearest_locality(lat, lng, city) if lat is not None else None
        zip_code = near.zip if near else ""
        out.append(listing(
            source=KEY, source_id=lid.group(1), url=url,
            title=obj_type or "Logement", category=_category_from_type(f"{obj_type} {slug_words}"),
            price=(rent + charges) if rent and charges else rent, charges=charges,
            rooms=to_float(rooms.group(1)) if rooms else None,
            surface=to_int(re.sub(r"[’']", "", surface.group(1))) if surface else None,
            street=street.strip(), zipcode=zip_code, city=city, canton=canton_of_zip(zip_code),
            lat=lat, lng=lng, geo_precision="exact" if lat is not None else "zip",
            features=detect_features(f"{obj_type} {slug_words}"), features_known=False,
            image_url=images[0] if images else "", images=images[:12],
            agency=html_lib.unescape(agency.group(1)) if agency else "",
        ))
    return out


def search(criteria: dict, zones: list[ResolvedZone], ctx: RunContext) -> list[dict]:
    covered = [z for z in zones if z.cantons & COVERED_CANTONS]
    if not covered:
        ctx.warn("immobilier.ch ne couvre que la Suisse romande — ignoré pour ces zones")
        return []
    cats = [CATEGORY_CODES[c] for c in criteria.get("categories") or ["apartment"] if c in CATEGORY_CODES]
    if not cats:
        return []

    fetcher = Fetcher()
    results: dict[str, dict] = {}
    try:
        codes = _place_codes(_load_places(fetcher), covered)
        if not codes:
            ctx.warn("immobilier.ch : aucun lieu correspondant sur ce site")
            return []
        params = {"t": "rent", "c": ";".join(cats), "p": ";".join(codes), "nb": "false", "gr": "1"}
        for ours, theirs in (("price_min", "pn"), ("price_max", "px"), ("rooms_min", "nrn"),
                             ("rooms_max", "nrx"), ("surface_min", "sn"), ("surface_max", "sx")):
            if criteria.get(ours) is not None:
                params[theirs] = f"{criteria[ours]:g}" if isinstance(criteria[ours], float) else criteria[ours]
        params.update(lang="fr", sort="DateSortDesc", map="false", group="1",
                      lngLeft="0", latTop="0", lngRight="0", latBottom="0")
        pages = 0
        for page_no in range(1, ctx.max_pages + 1):
            resp = fetcher.get(f"{BASE}/Pages/Objects/ObjectsSearch/Results",
                               params={**params, "page": page_no},
                               headers={"X-Requested-With": "XMLHttpRequest"})
            pages += 1
            if resp.status_code != 200:
                raise SourceError(f"immobilier.ch a répondu {resp.status_code}")
            cards = parse_cards(resp.text)
            for c in cards:
                results.setdefault(c["source_id"], c)
            ctx.progress(found=len(results), pages=pages, requests=fetcher.requests)
            if len(cards) < PAGE_SIZE or ctx.caught_up([c["source_id"] for c in cards]):
                break
        return list(results.values())
    finally:
        ctx.progress(requests=fetcher.requests)
        fetcher.close()
