"""Homegate & ImmoScout24 listings, read through their sister sites.

homegate.ch and immoscout24.ch (like comparis.ch) sit behind DataDome, an
anti-bot wall that answers plain HTTP with a captcha page. home.ch and
immostreet.ch belong to the same group (SMG) and render the very same
listings server-side, positions included, as plain HTML — so that is what we
read. Listing ids are SMG ids: the same number on Homegate, on ImmoScout24
and in Flatfox's `smg_id`, which is how the engine merges the copies.

URL scheme, shared by both sites (worked out from home.ch's search form,
which 303-redirects to it):

  /fr/louer/<category>/<location>/<feature>…/prix-<min>-<max>/
      surfacehabitable-<min>-<max>/pieces-<min>-<max>?sort=dd&pageNum=<n>

- <location>: `canton-vaud`, `a-lausanne` (a town) or `a-1004` (a postcode).
  Town slugs come from home.ch's location suggest API, because they are not
  always the plain name (`a-renens-vd`).
- an open range drops one side: `prix--2500.0`, `pieces-3.5-`.
- `sort=dd` is newest first, which keeps checks cheap: paging stops at the
  first page with nothing new. 20 cards per page.

Caveat, measured in October 2026: both sites sit on a cache that now and
then serves a page that ignores the filters (the result count is right, the
cards are just the newest listings of the place) or a card with an older
price. Hence: the engine re-checks every card against the criteria, a page
that mostly fails them is retried once on the other site, and the feature
segments of the URL are never trusted — only the card's own text counts.
home.ch is read first because it isn't CDN-cached (fresher); immostreet.ch
is the fallback, page 1 only (its later pages misbehave more).
"""
from __future__ import annotations

import html as html_lib
import json
import re

from .common import RunContext, detect_features, listing, parse_chf, to_float, to_int
from .http import Fetcher, SourceError
from .places import CANTON_NAMES, ResolvedZone, canton_of_zip, index, nearest_locality, normalize

KEY = "homegate"
LABEL = "Homegate · ImmoScout24"
COLOR = "#d6336c"
NOTE = "Toute la Suisse · lu via home.ch / immostreet.ch (mêmes annonces)"
FULL_TEXT = False         # cards only carry a short excerpt

BASE = "https://www.home.ch"
FALLBACK = "https://www.immostreet.ch"
PAGE_SIZE = 20
MAX_LOCATIONS = 12        # a radius over many communes falls back to whole cantons
UNFILTERED_SHARE = 0.6    # a page where more cards than this fail the criteria came from the bad cache

CATEGORY_SLUGS = {"apartment": "appartement", "house": "maison", "furnished": "logement_meuble"}
# Our features → home.ch path segments (only the ones it can filter natively).
FEATURE_SLUGS = {
    "balcony": "propriete-balconterrasse", "elevator": "propriete-ascenseur",
    "new_build": "propriete-newbuild", "pets": "propriete-animaux",
    "wheelchair": "propriete-fauteuil-roulant",
}
FEATURE_ORDER = list(FEATURE_SLUGS)

CANTON_SLUGS = {
    "AG": "argovie", "AI": "appenzell-rhodes-interieures", "AR": "appenzell-rhodes-exterieures",
    "BE": "berne", "BL": "bale-campagne", "BS": "bale-ville", "FR": "fribourg", "GE": "geneve",
    "GL": "glaris", "GR": "grisons", "JU": "jura", "LU": "lucerne", "NE": "neuchatel",
    "NW": "nidwald", "OW": "obwald", "SG": "saint-gall", "SH": "schaffhouse", "SO": "soleure",
    "SZ": "schwyz", "TG": "thurgovie", "TI": "tessin", "UR": "uri", "VD": "vaud", "VS": "valais",
    "ZG": "zoug", "ZH": "zurich",
}

_slugs: dict[tuple, str | None] = {}   # ("commune", bfs) / ("canton", code) → slug, per process


def _range(name: str, lo, hi) -> str | None:
    if lo is None and hi is None:
        return None
    fmt = lambda v: "" if v is None else f"{float(v):.1f}"
    return f"{name}-{fmt(lo)}-{fmt(hi)}"


def _suggest(fetcher: Fetcher, name: str) -> list[dict]:
    """home.ch's location suggest API — the geo ids behind its URLs."""
    try:
        data = fetcher.get_json(
            f"{BASE}/fr/wp-json/home/v1/locations",
            params={"name": name, "lang": "fr"},
            headers={"Origin": BASE, "Referer": f"{BASE}/fr/"},
        )
    except SourceError:
        return []
    return [item.get("geoLocation") or {} for item in data.get("results") or []]


def _canton_slug(fetcher: Fetcher, code: str) -> str:
    key = ("canton", code)
    if key not in _slugs:
        slug = None
        for geo in _suggest(fetcher, CANTON_NAMES[code]):
            if (geo.get("id") or "").startswith("geo-canton-"):
                slug = "canton-" + geo["id"].removeprefix("geo-canton-")
                break
        _slugs[key] = slug or f"canton-{CANTON_SLUGS[code]}"
    return _slugs[key]


def _town_slug(fetcher: Fetcher, bfs: str) -> str | None:
    """`a-<slug>` for a commune (None when home.ch doesn't know it as a town)."""
    key = ("commune", bfs)
    if key in _slugs:
        return _slugs[key]
    area = index().communes.get(bfs)
    slug = None
    if area:
        want = normalize(area.name)
        for geo in _suggest(fetcher, area.name):
            gid = geo.get("id") or ""
            # « Renens VD » is listed as « Renens (VD) »: compare without the canton suffix.
            names = [re.sub(rf"\s+{area.canton.lower()}$", "", normalize(n))
                     for n in (geo.get("names") or {}).get("fr", [])]
            if gid.startswith("geo-city-") and want in names:
                slug = "a-" + gid.removeprefix("geo-city-")
                break
    _slugs[key] = slug
    return slug


def _locations(fetcher: Fetcher, zones: list[ResolvedZone]) -> list[str]:
    """Location path segments covering the zones. A radius becomes its
    communes (or its cantons when it spans too many); the engine then keeps
    only what lies inside it."""
    out: list[str] = []
    for z in zones:
        if z.type == "canton":
            out.append(_canton_slug(fetcher, z.code))
        elif z.type == "zip":
            out.append(f"a-{z.code}")
        elif z.type == "commune":
            slug = _town_slug(fetcher, z.code)
            out += [slug] if slug else [f"a-{p}" for p in sorted(z.zips or [])[:6]]
        elif z.type == "radius":
            if len(z.communes) <= 6:
                for bfs in sorted(z.communes):
                    slug = _town_slug(fetcher, bfs)
                    out += [slug] if slug else [f"a-{p}" for p in sorted(index().communes[bfs].zips)[:3]]
            else:
                out += [_canton_slug(fetcher, c) for c in sorted(z.cantons) if c in CANTON_SLUGS]
    deduped = list(dict.fromkeys(s for s in out if s))
    if len(deduped) > MAX_LOCATIONS:
        cantons = sorted({c for z in zones for c in z.cantons})
        deduped = [_canton_slug(fetcher, c) for c in cantons if c in CANTON_SLUGS]
    return deduped


def search_url(category: str, location: str, criteria: dict, features: list[str], site: str = BASE) -> str:
    parts = [site, "fr", "louer", CATEGORY_SLUGS[category], location]
    parts += [FEATURE_SLUGS[f] for f in FEATURE_ORDER if f in features]
    parts += [p for p in (
        _range("prix", criteria.get("price_min"), criteria.get("price_max")),
        _range("surfacehabitable", criteria.get("surface_min"), criteria.get("surface_max")),
        _range("pieces", criteria.get("rooms_min"), criteria.get("rooms_max")),
    ) if p]
    return "/".join(parts) + "?sort=dd"


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf"""{name}=(["'])(.*?)\1""", tag, re.S)
    return html_lib.unescape(m.group(2)) if m else None


def _json_attr(block: str, name: str) -> dict:
    raw = _attr(block, name)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _text(fragment: str | None) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def _card(lid: str, category: str, title: str, description: str, data: dict, lat, lng,
          rooms, surface, agency: str = "", images: list | None = None) -> dict:
    zip_code = str(data.get("zip") or "")
    city = data.get("city") or ""
    if not zip_code and lat is not None:
        near = nearest_locality(lat, lng, city)
        zip_code = near.zip if near else ""
    thumb = (data.get("thumbnail") or "").replace("\\/", "/").removeprefix("src:")
    return listing(
        source=KEY, source_id=lid, smg_id=lid,
        url=f"https://www.homegate.ch/louer/{lid}",
        title=title, description=description, category=category,
        price=parse_chf(data.get("price")), rooms=rooms, surface=surface,
        street=data.get("address") or data.get("headline") or "", zipcode=zip_code, city=city,
        canton=canton_of_zip(zip_code), lat=lat, lng=lng,
        geo_precision="exact" if lat is not None else "zip",
        # Only the card's own words: the URL's feature filter isn't reliable (see above).
        features=detect_features(f"{title} {description}"), features_known=False,
        image_url=thumb or (images[0] if images else ""), images=images or ([thumb] if thumb else []),
        agency=agency,
    )


_HOME_CARD = re.compile(r'<a [^>]*class="row search-results__list__item[^"]*"[^>]*>', re.S)


def parse_cards(page: str, category: str) -> list[dict]:
    """home.ch result cards (Tailwind markup, `data-listing-data` JSON)."""
    starts = [(m.start(), m.group(0)) for m in _HOME_CARD.finditer(page)]
    out = []
    for i, (pos, tag) in enumerate(starts):
        block = page[pos: starts[i + 1][0] if i + 1 < len(starts) else pos + 8000]
        lid = _attr(tag, "data-listing-id") or _attr(tag, "data-ad-id")
        if not lid:
            continue
        data = _json_attr(block, "data-listing-data")
        desc = re.search(r'<p class="text-gray-600 mb-2">(.*?)</p>', block, re.S)
        chips = [_text(c) for c in re.findall(r'rounded-full text-sm">([^<]+)</span>', block)]
        out.append(_card(
            lid, category, html_lib.unescape(data.get("title") or ""), _text(desc.group(1) if desc else ""),
            data, to_float(_attr(tag, "data-latitude")), to_float(_attr(tag, "data-longitude")),
            rooms=next((to_float(c.split()[0]) for c in chips if re.search(r"room|pi[eè]ce|zimmer", c, re.I)), None),
            surface=next((to_int(c.split()[0]) for c in chips if re.search(r"\d\s*m(²|2)(?!\w)", c)), None),
        ))
    return out


def parse_immostreet(page: str, category: str) -> list[dict]:
    """immostreet.ch result cards (`<article data-map-item>`, `data-bookmark-data` JSON)."""
    starts = [m.start() for m in re.finditer(r'<article\s+class="results-item', page)]
    out = []
    for i, pos in enumerate(starts):
        block = page[pos: starts[i + 1] if i + 1 < len(starts) else pos + 12000]
        lid = _attr(block[:400], "data-map-item")
        if not lid:
            continue
        loc = _json_attr(block[:600], "data-map-location")
        data = _json_attr(block, "data-bookmark-data")
        short = [_text(li) for li in re.findall(r'<li class="item -muted">(.*?)</li>', block, re.S)]
        title = re.search(r'<h2 class="title">(.*?)</h2>', block, re.S)
        desc = re.search(r'<div class="description">(.*?)</div>', block, re.S)
        agency = re.search(r'<div class="agency">.*?alt="([^"]*)"', block, re.S)
        images = re.findall(r'<div class="slide"><img src="([^"]+)"', block)
        out.append(_card(
            lid, category, _text(title.group(1) if title else ""), _text(desc.group(1) if desc else ""),
            data, to_float(loc.get("lat")), to_float(loc.get("lng")),
            rooms=next((to_float(c.split()[0]) for c in short if re.search(r"pi[eè]ce|room|zimmer", c, re.I)), None),
            surface=next((to_int(c.split()[0]) for c in short if re.search(r"\d\s*m\s*(²|2)", c)), None),
            agency=html_lib.unescape(agency.group(1)) if agency else "", images=images[:12],
        ))
    return out


def looks_unfiltered(cards: list[dict], criteria: dict) -> bool:
    """A page from the bad cache: most cards fail the very bounds the URL asked for."""
    bounds = [(f, criteria.get(lo), criteria.get(hi)) for f, lo, hi in (
        ("price", "price_min", "price_max"), ("rooms", "rooms_min", "rooms_max"))]
    if len(cards) < 8 or all(lo is None and hi is None for _, lo, hi in bounds):
        return False
    bad = sum(1 for c in cards if any(
        c[f] is not None and ((lo is not None and c[f] < lo) or (hi is not None and c[f] > hi))
        for f, lo, hi in bounds))
    return bad / len(cards) > UNFILTERED_SHARE


def search(criteria: dict, zones: list[ResolvedZone], ctx: RunContext) -> list[dict]:
    if not zones:
        raise SourceError("Homegate a besoin d'au moins une zone (commune, NPA, canton ou rayon)")
    wanted = [c for c in criteria.get("categories") or ["apartment"] if c in CATEGORY_SLUGS]
    if not wanted:
        ctx.warn("Homegate : pas de catégorie « chambre » sur ce site")
        return []
    features = [f for f in criteria.get("features") or [] if f in FEATURE_SLUGS]

    fetcher = Fetcher()
    results: dict[str, dict] = {}
    pages_read = 0

    def fetch(url: str, parser, category: str) -> list[dict] | None:
        nonlocal pages_read
        resp = fetcher.get(url)
        pages_read += 1
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise SourceError(f"{url.split('/')[2]} a répondu {resp.status_code}")
        return parser(resp.text, category)

    try:
        locations = _locations(fetcher, zones)
        for category in wanted:
            for location in locations:
                base_url = search_url(category, location, criteria, features)
                for page_no in range(1, ctx.max_pages + 1):
                    cards = fetch(base_url + (f"&pageNum={page_no}" if page_no > 1 else ""), parse_cards, category)
                    if cards is None:
                        ctx.warn(f"Homegate : lieu inconnu « {location} »")
                        break
                    if page_no == 1 and looks_unfiltered(cards, criteria):
                        # Still the newest listings of the place, so keep paging home.ch; the
                        # other site may hold a filtered copy of page 1 — take both.
                        ctx.warn("Homegate : le site renvoie en ce moment des pages non filtrées — "
                                 "le tri est fait ici, la recherche va un peu moins loin en arrière")
                        alt = fetch(search_url(category, location, criteria, features, FALLBACK),
                                    parse_immostreet, category)
                        for c in alt or []:
                            results.setdefault(c["source_id"], c)
                    for c in cards:
                        results.setdefault(c["source_id"], c)
                    ctx.progress(found=len(results), pages=pages_read, requests=fetcher.requests)
                    if len(cards) < PAGE_SIZE or ctx.caught_up([c["source_id"] for c in cards]):
                        break
        return list(results.values())
    finally:
        ctx.progress(requests=fetcher.requests)
        fetcher.close()
