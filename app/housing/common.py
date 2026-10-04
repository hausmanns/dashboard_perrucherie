"""What every housing source shares: the listing shape, the feature catalog
(with the words that betray each feature in an ad, FR/DE/EN/IT), the
categories, and the run context a source receives.

A listing, as a source returns it (the engine stores these keys as columns):
  source, source_id, smg_id, url, title, description, category, price,
  charges, rooms, surface, floor, street, zipcode, city, canton, lat, lng,
  geo_precision, available_from, features (set), features_known (bool),
  image_url, images (list), agency, published_at
`price` is the monthly rent **charges included** whenever the source says so
(Flatfox `rent_gross`, immobilier.ch « 2'365.- (+250.- charges) » → 2615).
"""
from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .places import normalize

# key → (French label, words that mean the flat has it). Matched on the
# normalized text (no accents, lowercase), as whole words/phrases.
FEATURES: dict[str, tuple[str, list[str]]] = {
    "balcony": ("Balcon / terrasse", [
        "balcon", "balcons", "terrasse", "loggia", "veranda", "balkon", "sitzplatz", "dachterrasse",
        "balcony", "terrace", "balcone", "terrazza"]),
    "elevator": ("Ascenseur", ["ascenseur", "lift", "aufzug", "fahrstuhl", "elevator", "ascensore"]),
    "parking": ("Parking / garage", [
        "parking", "place de parc", "places de parc", "garage", "box", "stationnement", "parkplatz",
        "einstellplatz", "tiefgarage", "autoeinstellhallenplatz", "parcheggio", "autorimessa"]),
    "pets": ("Animaux acceptés", [
        "animaux acceptes", "animaux admis", "animaux bienvenus", "animaux autorises", "chats acceptes",
        "chat accepte", "chiens acceptes", "haustiere erlaubt", "haustiere willkommen", "katzen erlaubt",
        "pets allowed", "animali ammessi"]),
    "garden": ("Jardin", ["jardin", "jardin prive", "garten", "gartensitzplatz", "garden", "giardino"]),
    "washing_machine": ("Lave-linge privé", [
        "lave linge", "machine a laver", "colonne de lavage", "lave linge prive", "waschmaschine",
        "waschturm", "eigene waschmaschine", "washing machine", "lavatrice"]),
    "wheelchair": ("Accès fauteuil roulant", [
        "fauteuil roulant", "chaise roulante", "sans obstacles", "rollstuhl", "rollstuhlgangig",
        "rollstuhlgaengig", "barrierefrei", "hindernisfrei", "wheelchair"]),
    "new_build": ("Neuf / Minergie", [
        "immeuble neuf", "appartement neuf", "logement neuf", "construction neuve", "nouvelle construction",
        "premiere location", "premier locataire", "minergie", "neubau", "erstbezug", "erstvermietung",
        "new building", "nuova costruzione"]),
    "view": ("Vue dégagée", [
        "vue lac", "vue sur le lac", "vue degagee", "vue panoramique", "vue imprenable", "vue sur les alpes",
        "vue montagne", "vue sur les montagnes", "seesicht", "seeblick", "aussicht", "fernsicht",
        "bergsicht", "lake view", "vista lago"]),
}

# key → (French label, emoji). What a household looks for, not a site's taxonomy.
CATEGORIES: dict[str, tuple[str, str]] = {
    "apartment": ("Appartement", "🏢"),
    "house": ("Maison", "🏡"),
    "furnished": ("Meublé / temporaire", "🛋️"),
    "room": ("Chambre / colocation", "🚪"),
}

_FEATURE_RES = {
    key: re.compile(r"\b(?:" + "|".join(re.escape(normalize(w)) for w in words) + r")\b")
    for key, (_, words) in FEATURES.items()
}


def detect_features(text: str) -> set[str]:
    """Feature keys whose words appear in `text` (title + description)."""
    key = normalize(text)
    return {f for f, rx in _FEATURE_RES.items() if rx.search(key)}


def has_words(text: str, words: list[str]) -> bool:
    """Whether any of `words` appears in `text`, accent/case-insensitively, as a whole word."""
    hay = f" {normalize(text)} "
    return any(f" {normalize(w)} " in hay for w in words if normalize(w))


def parse_chf(text: str) -> Optional[int]:
    """« CHF 2'365.- », « 2,500 », « 1’850.– » → 2365, 2500, 1850. None if no number."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(round(text))
    m = re.search(r"\d[\d'’  ,.]*", str(text))
    if not m:
        return None
    raw = re.sub(r"[’'  ]", "", m.group(0)).rstrip(".,-")
    # « 2,500 » / « 2.500 » are thousands; « 1850.50 » is cents.
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", raw):
        raw = re.sub(r"[.,]", "", raw)
    else:
        raw = raw.replace(",", ".")
    try:
        return int(round(float(raw)))
    except ValueError:
        return None


def to_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except ValueError:
        return None


def to_int(value) -> Optional[int]:
    f = to_float(value)
    return int(round(f)) if f is not None else None


def html_text(fragment: str) -> str:
    """Strip tags and entities from an HTML fragment, collapse whitespace."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment or "", flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def listing(**fields) -> dict:
    """A listing dict with every key present (see the module docstring)."""
    base = {
        "source": "", "source_id": "", "smg_id": None, "url": "", "title": "", "description": "",
        "category": "apartment", "price": None, "charges": None, "rooms": None, "surface": None,
        "floor": None, "street": "", "zipcode": "", "city": "", "canton": "", "lat": None, "lng": None,
        "geo_precision": "none", "available_from": None, "features": set(), "features_known": False,
        "image_url": "", "images": [], "agency": "", "published_at": None,
    }
    base.update(fields)
    base["features"] = set(base["features"] or ())
    return base


@dataclass
class RunContext:
    """What the engine hands a source for one run.

    - `watermark`: the highest numeric listing id this source returned at the
      search's previous run. Listing ids only grow (Homegate/SMG,
      immobilier.ch), so on a newest-first list a page with no id above it
      means we've caught up — even when the site served the page unfiltered.
    - `known_before`: source ids already in the DB before this search's last
      run — the same test for sources whose ids aren't numbers.
    - `first_run`: the search never ran (or its criteria changed): no early stop,
      crawl up to `max_pages` per location.
    - `known_rows(ids)`: stored listings for these ids, as listing dicts —
      Flatfox uses it to skip re-downloading details it already has.
    - `progress(**kw)`: live status for the UI (pages read, listings found…).
    """
    known_before: set = field(default_factory=set)
    watermark: Optional[int] = None
    first_run: bool = True
    max_pages: int = 8
    known_rows: Callable[[list], dict] = lambda ids: {}
    progress: Callable[..., None] = lambda **kw: None
    warnings: list = field(default_factory=list)

    def caught_up(self, ids: list[str]) -> bool:
        """True when a newest-first page holds nothing newer than last time."""
        if self.first_run or not ids:
            return False
        if self.watermark is not None and all(str(i).isdigit() for i in ids):
            return all(int(i) <= self.watermark for i in ids)
        return all(i in self.known_before for i in ids)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
