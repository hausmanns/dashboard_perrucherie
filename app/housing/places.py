"""Swiss places: postcodes, communes and cantons, from swisstopo's official
directory of localities (`ch_localities.csv`, open data — opendata.swiss,
« Amtliches Ortschaftenverzeichnis », WGS84 export, Liechtenstein dropped).

Every housing source speaks its own location language (a bounding box for
Flatfox, geo slugs for Homegate, place codes for immobilier.ch) and many
listings only carry a postcode. This index is the common ground:

- `search()`       powers the zone picker (« Lausanne », « 1004 », « Vaud »);
- `resolve_zone()` turns a saved zone into postcodes + a bounding box;
- `in_zones()`     decides whether a listing falls inside a search's zones;
- `locate()` / `nearest_locality()` give a listing an approximate position or
                   its postcode when the source only gave half of it.

`geocode()` asks the federal geocoder (api3.geo.admin.ch — free, no key) for
a street address; used for ads pasted by hand.

A zone, as stored in a saved search:
  {"type": "canton",  "code": "VD",   "label": "Vaud"}
  {"type": "commune", "code": "5586", "label": "Lausanne"}        (BFS number)
  {"type": "zip",     "code": "1004", "label": "1004 Lausanne"}
  {"type": "radius",  "code": "5586", "base": "commune", "km": 5,
   "label": "Lausanne + 5 km", "lat": 46.52, "lng": 6.63}
"""
from __future__ import annotations

import csv
import math
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

import httpx

DATA_FILE = Path(__file__).with_name("ch_localities.csv")

CANTON_NAMES = {
    "AG": "Argovie", "AI": "Appenzell Rhodes-Intérieures", "AR": "Appenzell Rhodes-Extérieures",
    "BE": "Berne", "BL": "Bâle-Campagne", "BS": "Bâle-Ville", "FR": "Fribourg", "GE": "Genève",
    "GL": "Glaris", "GR": "Grisons", "JU": "Jura", "LU": "Lucerne", "NE": "Neuchâtel",
    "NW": "Nidwald", "OW": "Obwald", "SG": "Saint-Gall", "SH": "Schaffhouse", "SO": "Soleure",
    "SZ": "Schwytz", "TG": "Thurgovie", "TI": "Tessin", "UR": "Uri", "VD": "Vaud", "VS": "Valais",
    "ZG": "Zoug", "ZH": "Zurich",
}
# Names people actually type for a canton, besides the French one.
CANTON_ALIASES = {
    "GE": ["geneva", "genf", "ginevra"], "VD": ["waadt"], "VS": ["wallis"],
    "FR": ["freiburg"], "NE": ["neuenburg"], "BE": ["bern"], "ZH": ["zuerich"],
    "BS": ["basel-stadt", "basel"], "BL": ["basel-landschaft"], "TI": ["ticino"],
    "LU": ["luzern"], "SG": ["sankt gallen", "st. gallen", "st-gall"], "GR": ["graubunden", "graubuenden"],
    "SO": ["solothurn"], "SH": ["schaffhausen"], "TG": ["thurgau"], "AG": ["aargau"],
    "ZG": ["zug"], "SZ": ["schwyz"], "GL": ["glarus"], "NW": ["nidwalden"], "OW": ["obwalden"],
}

ZONE_TYPES = ("canton", "commune", "zip", "radius")
# Locality points are centroids: pad a commune/postcode box so its edges count too.
PAD_DEG = 0.02          # ≈ 2 km
ZIP_RADIUS_KM = 2.5     # how far a postcode's area plausibly reaches from its centroid


def normalize(text: str) -> str:
    """Accent-, case- and punctuation-free key: « Genève » → « geneve »."""
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class Locality:
    zip: str
    name: str
    commune: str
    bfs: str
    canton: str
    share: int
    lat: float
    lon: float
    key: str = ""


@dataclass
class Area:
    """A commune or a canton: its postcodes, centre and bounding box."""
    code: str
    name: str
    canton: str
    zips: set = field(default_factory=set)
    lat: float = 0.0
    lon: float = 0.0
    bbox: tuple = (0.0, 0.0, 0.0, 0.0)  # south, west, north, east
    key: str = ""


@dataclass
class ResolvedZone:
    type: str
    code: str
    label: str
    zips: Optional[set]          # None for a radius (distance decides)
    cantons: set
    bbox: tuple                  # south, west, north, east
    lat: float
    lon: float
    km: Optional[float] = None
    communes: set = field(default_factory=set)  # BFS numbers touched (radius: within reach)


class Index:
    def __init__(self, rows: list[Locality]):
        self.localities = rows
        self.by_zip: dict[str, list[Locality]] = {}
        self.communes: dict[str, Area] = {}
        self.cantons: dict[str, Area] = {}
        for loc in rows:
            self.by_zip.setdefault(loc.zip, []).append(loc)
            com = self.communes.setdefault(
                loc.bfs, Area(code=loc.bfs, name=loc.commune, canton=loc.canton, key=normalize(loc.commune)))
            com.zips.add(loc.zip)
            can = self.cantons.setdefault(
                loc.canton, Area(code=loc.canton, name=CANTON_NAMES.get(loc.canton, loc.canton),
                                 canton=loc.canton, key=normalize(CANTON_NAMES.get(loc.canton, loc.canton))))
            can.zips.add(loc.zip)
        for areas, pick in ((self.communes, lambda l: l.bfs), (self.cantons, lambda l: l.canton)):
            points: dict[str, list[Locality]] = {}
            for loc in rows:
                points.setdefault(pick(loc), []).append(loc)
            for code, pts in points.items():
                area = areas[code]
                # Centre on the localities named like the area itself: « Lausanne 25/26/27 »
                # up in the woods would pull Lausanne's centre 3 km north.
                core = [p for p in pts if p.key == area.key] or pts
                area.lat = sum(p.lat for p in core) / len(core)
                area.lon = sum(p.lon for p in core) / len(core)
                area.bbox = (min(p.lat for p in pts), min(p.lon for p in pts),
                             max(p.lat for p in pts), max(p.lon for p in pts))

    def zip_center(self, zip_code: str) -> Optional[tuple[float, float]]:
        locs = self.by_zip.get(str(zip_code or "").strip())
        if not locs:
            return None
        weight = sum(max(l.share, 1) for l in locs)
        return (sum(l.lat * max(l.share, 1) for l in locs) / weight,
                sum(l.lon * max(l.share, 1) for l in locs) / weight)

    def main_locality(self, zip_code: str) -> Optional[Locality]:
        locs = self.by_zip.get(str(zip_code or "").strip())
        return max(locs, key=lambda l: l.share) if locs else None


@lru_cache(maxsize=1)
def index() -> Index:
    rows = []
    with DATA_FILE.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(Locality(
                zip=r["zip"], name=r["locality"], commune=r["commune"], bfs=r["bfs"],
                canton=r["canton"], share=int(r["share"] or 0), lat=float(r["lat"]),
                lon=float(r["lon"]), key=normalize(r["locality"]),
            ))
    return Index(rows)


# ---------- Zone picker ----------

def _zone(type_: str, code: str, label: str, sublabel: str, canton: str, lat: float, lon: float) -> dict:
    return {"type": type_, "code": code, "label": label, "sublabel": sublabel,
            "canton": canton, "lat": round(lat, 5), "lng": round(lon, 5)}


def canton_zone(code: str) -> Optional[dict]:
    area = index().cantons.get(code.upper())
    if not area:
        return None
    return _zone("canton", area.code, area.name, f"Canton · {len(area.zips)} NPA", area.code, area.lat, area.lon)


def commune_zone(bfs: str) -> Optional[dict]:
    area = index().communes.get(str(bfs))
    if not area:
        return None
    zips = ", ".join(sorted(area.zips)[:4]) + ("…" if len(area.zips) > 4 else "")
    return _zone("commune", area.code, area.name, f"Commune · {area.canton} · {zips}", area.canton, area.lat, area.lon)


def zip_zone(zip_code: str) -> Optional[dict]:
    idx = index()
    main = idx.main_locality(zip_code)
    if not main:
        return None
    lat, lon = idx.zip_center(zip_code)
    names = sorted({l.name for l in idx.by_zip[main.zip]})
    return _zone("zip", main.zip, f"{main.zip} {main.name}",
                 f"NPA · {main.commune} · {main.canton}" + (f" · {', '.join(names[:3])}" if len(names) > 1 else ""),
                 main.canton, lat, lon)


def search(q: str, limit: int = 12) -> list[dict]:
    """Zones whose name or postcode matches `q` — cantons, communes, postcodes."""
    idx = index()
    key = normalize(q)
    if not key:
        return []

    if key.isdigit():
        return [zip_zone(z) for z in sorted(idx.by_zip) if z.startswith(key)][:limit]

    def rank(name_key: str) -> Optional[int]:
        if name_key == key:
            return 0
        if name_key.startswith(key):
            return 1
        if f" {key}" in f" {name_key}":
            return 2
        return None

    # (score, -size, name, zone): best match first, then the bigger place — « laus »
    # should offer Lausanne (14 NPA) before Lausen (1 NPA).
    ranked: list[tuple[int, int, str, dict]] = []
    for code, area in idx.cantons.items():
        keys = [area.key, code.lower()] + [normalize(a) for a in CANTON_ALIASES.get(code, [])]
        ranks = [r for r in (rank(k) for k in keys) if r is not None]
        if ranks:
            ranked.append((min(ranks) * 10, -len(area.zips), area.name, canton_zone(code)))
    seen_communes = set()
    for bfs, area in idx.communes.items():
        r = rank(area.key)
        if r is not None:
            seen_communes.add(area.key)
            ranked.append((r * 10 + 1, -len(area.zips), area.name, commune_zone(bfs)))
    # Localities that are not a commune of their own (« La Conversion » → NPA 1093).
    seen_zips = set()
    for loc in idx.localities:
        if loc.key in seen_communes or loc.zip in seen_zips:
            continue
        r = rank(loc.key)
        if r is not None:
            seen_zips.add(loc.zip)
            zone = zip_zone(loc.zip)
            zone["label"] = f"{loc.zip} {loc.name}"
            ranked.append((r * 10 + 2, 0, loc.name, zone))
    ranked.sort(key=lambda t: (t[0], t[1], len(t[2]), t[2]))
    return [z for *_, z in ranked][:limit]


# ---------- Zones ----------

def complete_zone(zone: dict) -> Optional[dict]:
    """Validate a zone coming from the API and fill its label / centre from the index.
    Returns None when its code is unknown. A radius keeps its own `km`."""
    type_ = (zone.get("type") or "").strip()
    code = str(zone.get("code") or "").strip()
    if type_ == "radius":
        # BFS numbers and postcodes are both up to 4 digits: trust `base`, else guess.
        base_type = zone.get("base") or ("commune" if code in index().communes else "zip")
        base = complete_zone({"type": base_type, "code": code}) if code else None
        lat = zone.get("lat") if zone.get("lat") is not None else (base or {}).get("lat")
        lng = zone.get("lng") if zone.get("lng") is not None else (base or {}).get("lng")
        if lat is None or lng is None:
            return None
        km = float(zone.get("km") or 5)
        base_label = (base or {}).get("label") or zone.get("label") or "Point"
        base_label = re.sub(r"\s*\+\s*[\d.,]+\s*km$", "", base_label)
        return {"type": "radius", "code": code, "base": base_type if base else None,
                "label": f"{base_label} + {km:g} km", "canton": (base or {}).get("canton", ""),
                "lat": round(float(lat), 5), "lng": round(float(lng), 5), "km": km}
    builder = {"canton": canton_zone, "commune": commune_zone, "zip": zip_zone}.get(type_)
    if not builder or not code:
        return None
    full = builder(code)
    if full:
        full.pop("sublabel", None)
    return full


def resolve_zone(zone: dict) -> Optional[ResolvedZone]:
    idx = index()
    type_, code = zone.get("type"), str(zone.get("code") or "")
    if type_ == "canton" and code in idx.cantons:
        a = idx.cantons[code]
        s, w, n, e = a.bbox
        return ResolvedZone("canton", code, a.name, set(a.zips), {code},
                            (s - PAD_DEG, w - PAD_DEG, n + PAD_DEG, e + PAD_DEG), a.lat, a.lon,
                            communes={b for b, c in idx.communes.items() if c.canton == code})
    if type_ == "commune" and code in idx.communes:
        a = idx.communes[code]
        s, w, n, e = a.bbox
        return ResolvedZone("commune", code, a.name, set(a.zips), {a.canton},
                            (s - PAD_DEG, w - PAD_DEG, n + PAD_DEG, e + PAD_DEG), a.lat, a.lon,
                            communes={code})
    if type_ == "zip" and code in idx.by_zip:
        locs = idx.by_zip[code]
        lat, lon = idx.zip_center(code)
        return ResolvedZone("zip", code, f"{code} {locs[0].name}", {code}, {l.canton for l in locs},
                            (min(l.lat for l in locs) - PAD_DEG, min(l.lon for l in locs) - PAD_DEG,
                             max(l.lat for l in locs) + PAD_DEG, max(l.lon for l in locs) + PAD_DEG),
                            lat, lon, communes={l.bfs for l in locs})
    if type_ == "radius" and zone.get("lat") is not None and zone.get("lng") is not None:
        lat, lon, km = float(zone["lat"]), float(zone["lng"]), float(zone.get("km") or 5)
        dlat = km / 111.0
        dlon = km / (111.320 * math.cos(math.radians(lat)))
        near = [l for l in idx.localities if haversine_km(lat, lon, l.lat, l.lon) <= km + ZIP_RADIUS_KM]
        return ResolvedZone("radius", code, zone.get("label") or f"{km:g} km", None,
                            {l.canton for l in near}, (lat - dlat, lon - dlon, lat + dlat, lon + dlon),
                            lat, lon, km=km, communes={l.bfs for l in near if haversine_km(lat, lon, l.lat, l.lon) <= km})
    return None


def resolve_zones(zones: list[dict]) -> list[ResolvedZone]:
    return [r for r in (resolve_zone(z) for z in zones or []) if r]


def locate(zip_code: str = "", city: str = "") -> Optional[tuple[float, float]]:
    """Approximate position: the postcode's centroid, else the town's."""
    idx = index()
    if zip_code:
        center = idx.zip_center(zip_code)
        if center:
            return center
    key = normalize(city)
    if key:
        locs = [l for l in idx.localities if l.key == key] or \
               [l for l in idx.localities if normalize(l.commune) == key]
        if locs:
            return (sum(l.lat for l in locs) / len(locs), sum(l.lon for l in locs) / len(locs))
    return None


def nearest_locality(lat: float, lon: float, city_hint: str = "") -> Optional[Locality]:
    """The closest locality to a point — among those named like `city_hint` when
    that narrows it down (« Lausanne » → one of the Lausanne postcodes)."""
    idx = index()
    hint = normalize(re.sub(r"\b(VD|GE|VS|FR|NE|JU|BE)\b", "", city_hint or ""))
    pool = []
    if hint:
        pool = [l for l in idx.localities if l.key == hint or l.key.startswith(hint + " ")
                or normalize(l.commune) == hint]
    pool = pool or idx.localities
    return min(pool, key=lambda l: (l.lat - lat) ** 2 + ((l.lon - lon) * 0.69) ** 2, default=None)


def canton_of_zip(zip_code: str) -> str:
    main = index().main_locality(zip_code)
    return main.canton if main else ""


def in_zones(listing: dict, zones: list[ResolvedZone]) -> bool:
    """Whether a listing (zipcode and/or lat/lng) lies in at least one zone.
    No zones, or a listing with neither postcode nor position, passes — the
    source already searched that area."""
    if not zones:
        return True
    zip_code = str(listing.get("zipcode") or "").strip()
    lat, lng = listing.get("lat"), listing.get("lng")
    if not zip_code and lat is not None and lng is not None:
        near = nearest_locality(lat, lng, listing.get("city") or "")
        zip_code = near.zip if near else ""
    if not zip_code and (lat is None or lng is None):
        return True
    for z in zones:
        if z.km is not None:
            pos = (lat, lng) if lat is not None and lng is not None else index().zip_center(zip_code)
            if pos and haversine_km(pos[0], pos[1], z.lat, z.lon) <= z.km:
                return True
        elif zip_code and zip_code in (z.zips or ()):
            return True
    return False


# ---------- Geocoding (federal geocoder) ----------

GEOCODER_URL = "https://api3.geo.admin.ch/rest/services/api/SearchServer"


def geocode(address: str) -> Optional[dict]:
    """Street address → {lat, lng, label, precision} via api3.geo.admin.ch.
    `precision` is « exact » for a building address, « zip » for a postcode or
    a town. None when nothing matches or the service is unreachable."""
    address = (address or "").strip()
    if len(address) < 3:
        return None
    try:
        resp = httpx.get(GEOCODER_URL, params={
            "searchText": address, "type": "locations", "limit": 1, "sr": 4326,
            "origins": "address,zipcode,gg25",
        }, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except (httpx.HTTPError, ValueError):
        return None
    if not results:
        return None
    attrs = results[0].get("attrs") or {}
    if attrs.get("lat") is None:
        return None
    label = re.sub(r"<[^>]+>", "", attrs.get("label") or "")
    return {"lat": float(attrs["lat"]), "lng": float(attrs["lon"]), "label": label,
            "precision": "exact" if attrs.get("origin") == "address" else "zip"}
