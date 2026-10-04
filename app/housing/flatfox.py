"""Flatfox (flatfox.ch) — the one Swiss portal with a public JSON API.

Two calls:
- `/api/v1/pin/` — every listing in a bounding box that passes the price /
  rooms / category filters, as a tiny pin (id, position, price). The whole
  set comes at once (no paging; `max_count` caps it), so a crowded box is
  split in four until each piece fits.
- `/api/v1/public-listing/?pk=…` — full details, up to 100 ids per call.
  Only asked for ids we don't have yet: a known listing is refreshed from its
  pin (price, last seen) without downloading its description again.

Flatfox belongs to the same group as Homegate / ImmoScout24: `smg_id` is the
listing's id over there, which is how the engine merges the two copies.
"""
from __future__ import annotations

from .common import RunContext, detect_features, listing, to_float, to_int
from .http import Fetcher, SourceError
from .places import ResolvedZone, canton_of_zip

KEY = "flatfox"
LABEL = "Flatfox"
COLOR = "#e8573f"
NOTE = "Toute la Suisse · API publique"
FULL_TEXT = True          # details carry the full description → feature/keyword checks are reliable

BASE = "https://flatfox.ch"
PIN_MAX = 400             # pins per request; a box that hits it is split
MAX_SPLIT_DEPTH = 3
DETAILS_BATCH = 100

# Flatfox `attributes` → our feature keys.
ATTRIBUTES = {
    "balconygarden": "balcony", "lift": "elevator", "parkingspace": "parking", "garage": "parking",
    "petsallowed": "pets", "accessiblewithwheelchair": "wheelchair", "washingmachine": "washing_machine",
    "minergie": "new_build", "newbuilding": "new_build", "view": "view",
}
CATEGORY_PARAM = {"apartment": "APARTMENT", "furnished": "APARTMENT", "house": "HOUSE", "room": "SHARED"}


def _category(d: dict) -> str:
    ocat, otype = d.get("object_category"), d.get("object_type")
    if ocat == "HOUSE":
        return "house"
    if ocat == "SHARED" or otype == "SINGLE_ROOM":
        return "room"
    if otype == "FURNISHED_FLAT" or d.get("is_furnished") or d.get("is_temporary"):
        return "furnished"
    if ocat == "APARTMENT":
        return "apartment"
    return "other"


def _image(img: dict | None) -> str:
    if not img:
        return ""
    path = img.get("url_listing_search") or img.get("url_thumb_m") or img.get("url") or ""
    return BASE + path if path.startswith("/") else path


def normalize_detail(d: dict) -> dict:
    features = {ATTRIBUTES[a["name"]] for a in d.get("attributes") or []
                if isinstance(a, dict) and a.get("name") in ATTRIBUTES}
    text = f"{d.get('description_title') or ''}\n{d.get('description') or ''}"
    features |= detect_features(text)
    moving = d.get("moving_date_type")
    available = "immediately" if moving == "imm" else (d.get("moving_date") if moving == "dat" else None)
    zip_code = str(d.get("zipcode") or "")
    url = BASE + (d.get("url") or f"/{d['pk']}/").replace("/en/", "/fr/", 1)
    images = [_image(i) for i in (d.get("images") or [])][:12]
    return listing(
        source=KEY, source_id=str(d["pk"]), smg_id=d.get("smg_id") or None, url=url,
        title=d.get("description_title") or d.get("short_title") or d.get("public_title") or "",
        description=d.get("description") or "",
        category=_category(d),
        price=to_int(d.get("rent_gross") or d.get("price_display")),
        charges=to_int(d.get("rent_charges")),
        rooms=to_float(d.get("number_of_rooms")),
        surface=to_int(d.get("surface_living") or d.get("space_display")),
        floor=to_int(d.get("floor")),
        street=d.get("street") or "", zipcode=zip_code, city=d.get("city") or "",
        canton=canton_of_zip(zip_code),
        lat=d.get("latitude"), lng=d.get("longitude"),
        geo_precision="exact" if d.get("latitude") else "zip",
        available_from=available, features=features, features_known=True,
        image_url=_image(d.get("cover_image")) or (images[0] if images else ""), images=images,
        agency=(d.get("agency") or {}).get("name") or "",
        published_at=d.get("published") or d.get("created"),
    )


def _pins(fetcher: Fetcher, bbox: tuple, params: dict, depth: int = 0) -> list[dict]:
    south, west, north, east = bbox
    pins = fetcher.get_json(f"{BASE}/api/v1/pin/", params={
        **params, "south": f"{south:.5f}", "west": f"{west:.5f}", "north": f"{north:.5f}",
        "east": f"{east:.5f}", "max_count": PIN_MAX,
    })
    if not isinstance(pins, list):
        raise SourceError("Flatfox : réponse inattendue de l'API")
    if len(pins) < PIN_MAX or depth >= MAX_SPLIT_DEPTH:
        return pins
    mid_lat, mid_lon = (south + north) / 2, (west + east) / 2
    out: dict[int, dict] = {}
    for box in ((south, west, mid_lat, mid_lon), (south, mid_lon, mid_lat, east),
                (mid_lat, west, north, mid_lon), (mid_lat, mid_lon, north, east)):
        for p in _pins(fetcher, box, params, depth + 1):
            out[p["pk"]] = p
    return list(out.values())


def _details(fetcher: Fetcher, pks: list[int]) -> list[dict]:
    out = []
    for i in range(0, len(pks), DETAILS_BATCH):
        chunk = pks[i:i + DETAILS_BATCH]
        data = fetcher.get_json(f"{BASE}/api/v1/public-listing/",
                                params=[("pk", p) for p in chunk] + [("limit", DETAILS_BATCH),
                                                                      ("expand", "cover_image,images")])
        out += data.get("results", []) if isinstance(data, dict) else data
    return out


def search(criteria: dict, zones: list[ResolvedZone], ctx: RunContext) -> list[dict]:
    if not zones:
        raise SourceError("Flatfox a besoin d'au moins une zone (commune, NPA, canton ou rayon)")
    params = {"offer_type": "RENT"}
    cats = {CATEGORY_PARAM[c] for c in criteria.get("categories") or [] if c in CATEGORY_PARAM}
    if len(cats) == 1:
        params["object_category"] = cats.pop()
    for ours, theirs in (("price_min", "min_price"), ("price_max", "max_price"),
                         ("rooms_min", "min_rooms"), ("rooms_max", "max_rooms")):
        if criteria.get(ours) is not None:
            params[theirs] = criteria[ours]

    fetcher = Fetcher()
    try:
        pins: dict[int, dict] = {}
        for z in zones:
            for p in _pins(fetcher, z.bbox, params):
                pins[p["pk"]] = p
            ctx.progress(found=len(pins), requests=fetcher.requests)

        known = ctx.known_rows([str(pk) for pk in pins])
        results = []
        for pk, pin in pins.items():
            row = known.get(str(pk))
            if row:
                if pin.get("price_display"):
                    row["price"] = to_int(pin["price_display"])
                results.append(row)
        fresh = [pk for pk in pins if str(pk) not in known]
        for d in _details(fetcher, fresh):
            if d.get("offer_type", "RENT") == "RENT":
                results.append(normalize_detail(d))
            ctx.progress(found=len(results), requests=fetcher.requests)
        ctx.progress(found=len(results), requests=fetcher.requests, pages=len(zones))
        return results
    finally:
        ctx.progress(requests=fetcher.requests)
        fetcher.close()
