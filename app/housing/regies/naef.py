"""Naef Immobilier (naef.ch) — one of the largest Romandie régies (Genève,
Vaud, Neuchâtel, Fribourg, Valais).

Its rental page embeds every listing as JSON (`var all_db_datas = [...]`):
type, rooms, net and gross rent, surface, postcode, title, photos, link and
position. One request covers all of Naef's ~1000 rentals, and the gross rent
is there — no detail page needed.
"""
from __future__ import annotations

import json
import re

from ..common import RunContext, detect_features, listing, parse_chf, to_float, to_int
from ..http import Fetcher, SourceError
from ..places import ResolvedZone, normalize
from .common import ROMANDIE, canton, covers

KEY = "naef"
LABEL = "Naef"
COLOR = "#0b7285"
NOTE = "Romandie · tout le catalogue en une page"
FULL_TEXT = False
GROUP = "regie"
CANTONS = ROMANDIE | {"BE"}

URL = "https://www.naef.ch/louer/appartements-maisons/"
MARKER = "var all_db_datas = "
TYPES = {"APP": "apartment", "MAI": "house"}
SMALL_WORDS = {"de", "du", "des", "la", "le", "les", "l", "d", "a", "au", "aux", "en", "sur"}


def _street(link: str, city: str) -> str:
    """« …/1201/rue-de-lyon-10-geneve/268546.7152/ » → « Rue de Lyon 10 »."""
    m = re.search(r"/location/[^/]+/\d{4}/([^/]+)/", link or "")
    if not m:
        return ""
    words = m.group(1).split("-")
    town = normalize(city).split()
    if town and words[-len(town):] == town:
        words = words[:-len(town)]
    return " ".join(w if (i and w in SMALL_WORDS) else w.capitalize() for i, w in enumerate(words))


def search(criteria: dict, zones: list[ResolvedZone], ctx: RunContext) -> list[dict]:
    if not covers(zones, CANTONS, ctx, LABEL):
        return []
    fetcher = Fetcher()
    try:
        resp = fetcher.get(URL)
        if resp.status_code != 200:
            raise SourceError(f"naef.ch a répondu {resp.status_code}")
        start = resp.text.find(MARKER)
        if start < 0:
            raise SourceError("naef.ch : la liste des biens a changé de forme")
        data, _ = json.JSONDecoder().raw_decode(resp.text[start + len(MARKER):])
        out = []
        for d in data:
            category = TYPES.get(d.get("type_code"))
            if not category:
                continue  # parkings, dépôts, commerces
            if "meubl" in normalize(f"{d.get('type_appartement')} {d.get('intitule_plaquette')}"):
                category = "furnished"
            lat, lng = (to_float(x) for x in (str(d.get("gmCordinate") or ",").split(",") + [None])[:2])
            gross = None if d.get("loyer_sur_demande") == "oui" else parse_chf(d.get("loyer_mensuel_brut"))
            net = parse_chf(d.get("loyer_mensuel_net"))
            rooms = to_float(d.get("nb_pieces")) or None
            title = d.get("intitule_plaquette") or f"{d.get('type_designation_fr') or 'Logement'} {rooms or ''} pièces".strip()
            zip_code = str(d.get("npa") or "")
            city = d.get("adresse_localite") or ""
            images = [u for u in d.get("imgs") or [] if isinstance(u, str)][:12]
            out.append(listing(
                source=KEY, source_id=str(d.get("no_dossier")), url=d.get("link") or URL,
                title=title, category=category, price=gross or net,
                charges=(gross - net) if gross and net and gross > net else None,
                rooms=rooms, surface=to_int(d.get("surface_habitable")) or None,
                street=_street(d.get("link"), city), zipcode=zip_code, city=city, canton=canton(zip_code),
                lat=lat, lng=lng, geo_precision="exact" if lat is not None else "zip",
                features=detect_features(title), image_url=images[0] if images else "", images=images,
                agency="Naef Immobilier", published_at=d.get("date_modification"),
            ))
        ctx.progress(found=len(out), pages=1, requests=fetcher.requests)
        return out
    finally:
        ctx.progress(requests=fetcher.requests)
        fetcher.close()
