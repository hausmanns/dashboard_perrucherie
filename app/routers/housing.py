"""Apartment search ("Logement"): saved searches, their runs, the listings
they found, and ads pasted by hand. The work itself lives in app/housing/.

- `housing_searches` — a name + criteria (JSON: zones, rent, rooms, surface,
  types, must-have features, keywords, sites) + the robot's schedule.
- `housing_listings` — one row per listing per site; copies of one flat on
  several sites point at the first one through `duplicate_of`, which carries
  the household's state: `seen_at` (NULL = new), `favorite`, `dismissed`.
  Every read endpoint returns canonical rows only, with all their `sources`.
- `housing_matches` — which search found which (canonical) listing.
- `housing_runs` — one row per check, with what each site answered.

The household state is shared, like plants and storage: a listing seen by
one person is seen for the whole house.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import telegram
from ..config import OPENROUTER_API_KEY
from ..database import get_conn, rows_to_dicts
from ..housing import engine, extract as extractor, places
from ..housing.common import CATEGORIES, FEATURES, detect_features, listing as make_listing
from ..openrouter import LLMError

router = APIRouter(prefix="/api/housing", tags=["housing"])

VIEWS = ("active", "new", "favorites", "dismissed")
SORTS = {
    "recent": "l.first_seen_at DESC, l.id DESC",
    "price_asc": "l.price IS NULL, l.price ASC",
    "price_desc": "l.price IS NULL, l.price DESC",
    "price_m2": "(l.price IS NULL OR l.surface IS NULL OR l.surface = 0), CAST(l.price AS REAL) / NULLIF(l.surface, 0) ASC",
    "surface_desc": "l.surface IS NULL, l.surface DESC",
    "rooms_desc": "l.rooms IS NULL, l.rooms DESC",
}
MANUAL_LABEL = "Ajout manuel"
MANUAL_COLOR = "#7048e8"


class ZoneIn(BaseModel):
    type: str
    code: str = ""
    label: str = ""
    base: Optional[str] = None          # radius: what the centre is (commune / zip)
    km: Optional[float] = Field(default=None, gt=0, le=50)
    lat: Optional[float] = None
    lng: Optional[float] = None


class CriteriaIn(BaseModel):
    zones: list[ZoneIn] = []
    categories: list[str] = ["apartment"]
    price_min: Optional[int] = Field(default=None, ge=0)
    price_max: Optional[int] = Field(default=None, ge=0)
    rooms_min: Optional[float] = Field(default=None, ge=0, le=20)
    rooms_max: Optional[float] = Field(default=None, ge=0, le=20)
    surface_min: Optional[int] = Field(default=None, ge=0)
    surface_max: Optional[int] = Field(default=None, ge=0)
    features: list[str] = []
    keywords_include: list[str] = []
    keywords_exclude: list[str] = []
    sources: list[str] = list(engine.SOURCES)


class SearchIn(BaseModel):
    name: str
    criteria: CriteriaIn = CriteriaIn()
    auto_run: bool = False
    interval_minutes: int = Field(default=60, ge=15, le=1440)
    notify: bool = True
    active_from: int = Field(default=7, ge=0, le=23)
    active_to: int = Field(default=22, ge=0, le=24)


class ListingUpdateIn(BaseModel):
    """Partial update of the household state: only the fields given change."""
    favorite: Optional[bool] = None
    dismissed: Optional[bool] = None
    seen: Optional[bool] = None


class SeenIn(BaseModel):
    ids: Optional[list[int]] = None      # these listings…
    search_id: Optional[int] = None      # …or every new result of this search (neither = all)


class ManualListingIn(BaseModel):
    url: str = ""
    title: str = ""
    description: str = ""
    category: str = "apartment"
    price: Optional[int] = Field(default=None, ge=0)
    charges: Optional[int] = Field(default=None, ge=0)
    rooms: Optional[float] = Field(default=None, ge=0, le=20)
    surface: Optional[int] = Field(default=None, ge=0)
    floor: Optional[int] = None
    street: str = ""
    zipcode: str = ""
    city: str = ""
    available_from: Optional[str] = None
    features: list[str] = []
    image_url: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None


class ExtractIn(BaseModel):
    text: str = ""
    url: str = ""


# ---------- Helpers ----------

def _now() -> str:
    return engine._now()


def _clean_criteria(c: CriteriaIn) -> dict:
    data = c.model_dump()
    for key, catalog, label in (("categories", CATEGORIES, "Type"), ("features", FEATURES, "Équipement"),
                                ("sources", engine.SOURCES, "Site")):
        unknown = [x for x in data[key] if x not in catalog]
        if unknown:
            raise HTTPException(422, f"{label} inconnu : {', '.join(unknown)} (attendu : {', '.join(catalog)})")
    zones = []
    for z in data["zones"]:
        full = places.complete_zone(z)
        if not full:
            raise HTTPException(422, f"Zone inconnue : {z.get('type')} « {z.get('code') or z.get('label')} »")
        zones.append(full)
    data["zones"] = zones
    if not data["sources"]:
        raise HTTPException(422, "Choisissez au moins un site")
    for lo, hi, what in (("price_min", "price_max", "loyer"), ("rooms_min", "rooms_max", "nombre de pièces"),
                         ("surface_min", "surface_max", "surface")):
        if data[lo] is not None and data[hi] is not None and data[lo] > data[hi]:
            raise HTTPException(422, f"Le minimum dépasse le maximum ({what})")
    return engine.normalize_criteria(data)


def describe(crit: dict) -> str:
    """« Lausanne, Pully · 1'500–2'500 CHF · 2.5–4.5 p. · ≥ 60 m² » — one line for a search card."""
    bits = []
    labels = [z.get("label") or z.get("code") for z in crit["zones"]]
    if labels:
        bits.append(", ".join(labels[:3]) + (f" +{len(labels) - 3}" if len(labels) > 3 else ""))

    def span(lo, hi, unit, fmt=lambda v: f"{v:g}"):
        if lo is not None and hi is not None:
            return f"{fmt(lo)}–{fmt(hi)}{unit}"
        if lo is not None:
            return f"≥ {fmt(lo)}{unit}"
        if hi is not None:
            return f"≤ {fmt(hi)}{unit}"
        return None

    bits += [b for b in (
        span(crit["price_min"], crit["price_max"], " CHF", engine.fmt_chf),
        span(crit["rooms_min"], crit["rooms_max"], " p."),
        span(crit["surface_min"], crit["surface_max"], " m²"),
    ) if b]
    if crit["categories"] != ["apartment"]:
        bits.append(" / ".join(CATEGORIES[c][0] for c in crit["categories"]))
    if crit["features"]:
        bits.append(" · ".join(FEATURES[f][0] for f in crit["features"]))
    return " · ".join(bits) or "Toute la Suisse"


def _source_meta(source: str, url: str = "") -> dict:
    src = engine.SOURCES.get(source)
    if src:
        return {"source": source, "label": src.LABEL, "color": src.COLOR, "url": url}
    return {"source": source, "label": extractor.source_label(url) if url else MANUAL_LABEL,
            "color": MANUAL_COLOR, "url": url}


def _get_search_row(conn, search_id: int):
    row = conn.execute("SELECT * FROM housing_searches WHERE id = ?", (search_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Recherche introuvable")
    return row


def _search_out(conn, row) -> dict:
    s = dict(row)
    s["criteria"] = engine.normalize_criteria(json.loads(s["criteria"] or "{}"))
    s["auto_run"], s["notify"] = bool(s["auto_run"]), bool(s["notify"])
    c = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(l.seen_at IS NULL AND l.dismissed = 0) AS new,
                  SUM(l.favorite = 1) AS favorites,
                  SUM(l.dismissed = 1) AS dismissed
           FROM housing_matches m JOIN housing_listings l ON l.id = m.listing_id
           WHERE m.search_id = ?""", (s["id"],)).fetchone()
    s["counts"] = {"total": c["total"] or 0, "new": c["new"] or 0,
                   "favorites": c["favorites"] or 0, "dismissed": c["dismissed"] or 0}
    live = next((engine.run_status(rid) for rid, p in list(engine._progress.items())
                 if p["search_id"] == s["id"]), None)
    last = conn.execute("SELECT * FROM housing_runs WHERE search_id = ? ORDER BY id DESC LIMIT 1",
                        (s["id"],)).fetchone()
    s["last_run"] = live or (engine.run_row_to_dict(dict(last)) if last else None)
    s["running"] = engine.is_running(s["id"])
    s["next_run_at"] = engine.next_auto_run(s)
    s["summary"] = describe(s["criteria"])
    return s


def _duplicates(conn, ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(
            f"SELECT duplicate_of, source, url, first_seen_at FROM housing_listings "
            f"WHERE duplicate_of IN ({','.join('?' * len(chunk))}) ORDER BY id", chunk
        ).fetchall():
            out.setdefault(r["duplicate_of"], []).append(dict(r))
    return out


def _listing_out(row: dict, dups: list[dict], to_check: Optional[list[str]] = None,
                 with_text: bool = False) -> dict:
    item = engine.row_to_listing(row)
    item["features"] = sorted(item["features"])
    item["price_history"] = json.loads(row.get("price_history") or "[]")
    item["favorite"], item["dismissed"] = bool(item["favorite"]), bool(item["dismissed"])
    item["is_new"] = item["seen_at"] is None and not item["dismissed"]
    item["price_per_m2"] = round(item["price"] / item["surface"], 1) if item["price"] and item["surface"] else None
    last = item["price_history"][-1] if item["price_history"] else None
    item["price_change"] = (last["to"] - last["from"]) if last else None
    main = _source_meta(item["source"], item["url"])
    item["source_label"], item["source_color"] = main["label"], main["color"]
    item["sources"] = [main] + [_source_meta(d["source"], d["url"]) for d in dups]
    if item["source"] == "homegate" or any(d["source"] == "homegate" for d in dups):
        smg = item["smg_id"] or next((d["url"].rsplit("/", 1)[-1] for d in dups if d["source"] == "homegate"), None)
        if smg:
            item["sources"].append({"source": "immoscout24", "label": "ImmoScout24", "color": "#f08c00",
                                    "url": f"https://www.immoscout24.ch/louer/{smg}"})
    item["to_check"] = to_check or []
    if not with_text:
        item.pop("description", None)
        item["images"] = item["images"][:1]
    return item


def _canonical_row(conn, listing_id: int) -> dict:
    row = conn.execute("SELECT * FROM housing_listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Annonce introuvable")
    if row["duplicate_of"]:
        row = conn.execute("SELECT * FROM housing_listings WHERE id = ?", (row["duplicate_of"],)).fetchone()
    return dict(row)


# ---------- Reference data ----------

@router.get("/status")
def status():
    """Sites, types, features and what is configured — the frontend builds its forms from this."""
    return {
        "sources": [{"key": k, "label": m.LABEL, "color": m.COLOR, "note": m.NOTE}
                    for k, m in engine.SOURCES.items()],
        "categories": [{"key": k, "label": v[0], "emoji": v[1]} for k, v in CATEGORIES.items()],
        "features": [{"key": k, "label": v[0]} for k, v in FEATURES.items()],
        "telegram_configured": telegram.is_configured(),
        "ai_configured": bool(OPENROUTER_API_KEY),
        "running": [engine.run_status(rid) for rid in list(engine._progress)],
        "now": _now(),  # server-local clock: stored timestamps are relative to it
    }


@router.get("/places")
def search_places(q: str = "", limit: int = Query(12, ge=1, le=30)):
    """Zone picker: cantons, communes and postcodes matching `q` (accent-insensitive)."""
    return places.search(q, limit)


# ---------- Saved searches ----------

@router.get("/searches")
def list_searches():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM housing_searches ORDER BY created_at, id").fetchall()
        return [_search_out(conn, r) for r in rows]


@router.post("/searches", status_code=201)
def create_search(payload: SearchIn):
    crit = _clean_criteria(payload.criteria)
    name = payload.name.strip() or describe(crit)[:60]
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO housing_searches (name, criteria, auto_run, interval_minutes, notify, active_from, active_to)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, json.dumps(crit, ensure_ascii=False), int(payload.auto_run), payload.interval_minutes,
             int(payload.notify), payload.active_from, payload.active_to),
        )
        return _search_out(conn, _get_search_row(conn, cur.lastrowid))


@router.get("/searches/{search_id}")
def get_search(search_id: int):
    with get_conn() as conn:
        return _search_out(conn, _get_search_row(conn, search_id))


@router.put("/searches/{search_id}")
def update_search(search_id: int, payload: SearchIn):
    """Full update. Changing the criteria drops the results that no longer
    match and makes the next run a baseline (stored, not announced)."""
    crit = _clean_criteria(payload.criteria)
    with get_conn() as conn:
        current = _get_search_row(conn, search_id)
        changed = engine.normalize_criteria(json.loads(current["criteria"] or "{}")) != crit
        conn.execute(
            """UPDATE housing_searches SET name = ?, criteria = ?, auto_run = ?, interval_minutes = ?, notify = ?,
                 active_from = ?, active_to = ?, last_run_at = CASE WHEN ? THEN NULL ELSE last_run_at END,
                 updated_at = ? WHERE id = ?""",
            (payload.name.strip() or current["name"], json.dumps(crit, ensure_ascii=False), int(payload.auto_run),
             payload.interval_minutes, int(payload.notify), payload.active_from, payload.active_to,
             int(changed), _now(), search_id),
        )
    if changed:
        engine.reevaluate_matches(search_id)
    with get_conn() as conn:
        return _search_out(conn, _get_search_row(conn, search_id))


@router.delete("/searches/{search_id}", status_code=204)
def delete_search(search_id: int):
    """Delete a search and its runs. Listings no other search found are
    forgotten too — favourites and hand-added ads stay."""
    if engine.is_running(search_id):
        raise HTTPException(409, "Un passage est en cours — réessayez dans un instant")
    with get_conn() as conn:
        _get_search_row(conn, search_id)
        conn.execute("DELETE FROM housing_searches WHERE id = ?", (search_id,))
        engine.prune_orphans(conn)


@router.post("/searches/{search_id}/run", status_code=202)
def run_search(search_id: int):
    """Check the sites now, in the background. Poll `GET /api/housing/runs/{run_id}`."""
    with get_conn() as conn:
        _get_search_row(conn, search_id)
    if engine.is_running(search_id):
        raise HTTPException(409, "Cette recherche tourne déjà")
    return {"run_id": engine.start_runs([search_id])[0]}


@router.post("/run-all", status_code=202)
def run_all():
    """Check every saved search now (one after the other, in the background)."""
    with get_conn() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM housing_searches ORDER BY id").fetchall()]
    return {"run_ids": engine.start_runs(ids)}


@router.get("/runs")
def list_runs(search_id: Optional[int] = None, limit: int = Query(20, ge=1, le=200)):
    sql, params = "SELECT * FROM housing_runs", []
    if search_id is not None:
        sql += " WHERE search_id = ?"
        params.append(search_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = rows_to_dicts(conn.execute(sql, params).fetchall())
    return [engine.run_status(r["id"]) if r["id"] in engine._progress else engine.run_row_to_dict(r) for r in rows]


@router.get("/runs/{run_id}")
def get_run(run_id: int):
    """A run's status — live while it runs: per site `state`, `found`, `pages`, `requests`, `error`."""
    status_ = engine.run_status(run_id)
    if not status_:
        raise HTTPException(404, "Passage introuvable")
    return status_


# ---------- Listings ----------

@router.get("/listings")
def list_listings(search_id: Optional[int] = None, view: str = "active", source: Optional[str] = None,
                  sort: str = "recent", q: str = "", limit: int = Query(500, ge=1, le=2000)):
    """Canonical listings, each with all its `sources`.

    - `search_id`: one search's results (with `to_check`: what its criteria
      couldn't verify on that card); omitted = every search + hand-added + favourites.
    - `view`: `active` (not set aside, default) | `new` | `favorites` | `dismissed`.
    - `sort`: recent | price_asc | price_desc | price_m2 | surface_desc | rooms_desc.
    - `counts` gives the size of every view for the same scope (for the filter chips)."""
    if view not in VIEWS:
        raise HTTPException(422, f"Vue inconnue : {view} (attendu : {', '.join(VIEWS)})")
    if sort not in SORTS:
        raise HTTPException(422, f"Tri inconnu : {sort} (attendu : {', '.join(SORTS)})")
    where, params = ["l.duplicate_of IS NULL"], []
    if search_id is not None:
        where.append("l.id IN (SELECT listing_id FROM housing_matches WHERE search_id = ?)")
        params.append(search_id)
    else:
        where.append("(l.id IN (SELECT listing_id FROM housing_matches) OR l.source = 'manual' OR l.favorite = 1)")
    if source:
        where.append("(l.source = ? OR EXISTS (SELECT 1 FROM housing_listings d WHERE d.duplicate_of = l.id AND d.source = ?))")
        params += [source, source]
    base_where = " AND ".join(where)
    view_sql = {"active": "l.dismissed = 0", "new": "l.seen_at IS NULL AND l.dismissed = 0",
                "favorites": "l.favorite = 1", "dismissed": "l.dismissed = 1"}
    with get_conn() as conn:
        c = conn.execute(
            f"""SELECT SUM(l.dismissed = 0) AS active, SUM(l.seen_at IS NULL AND l.dismissed = 0) AS new,
                       SUM(l.favorite = 1) AS favorites, SUM(l.dismissed = 1) AS dismissed
                FROM housing_listings l WHERE {base_where}""", params).fetchone()
        rows = rows_to_dicts(conn.execute(
            f"SELECT l.* FROM housing_listings l WHERE {base_where} AND {view_sql[view]} "
            f"ORDER BY {SORTS[sort]} LIMIT ?", (*params, limit)).fetchall())
        dups = _duplicates(conn, [r["id"] for r in rows])
        crit = zones = None
        if search_id is not None:
            srow = _get_search_row(conn, search_id)
            crit = engine.normalize_criteria(json.loads(srow["criteria"] or "{}"))
            zones = places.resolve_zones(crit["zones"])
    key = places.normalize(q)
    items = []
    for r in rows:
        if key and key not in places.normalize(f"{r['title']} {r['city']} {r['street']} {r['zipcode']} {r['description']}"):
            continue
        to_check = None
        if crit is not None:
            src = engine.SOURCES.get(r["source"])
            to_check = engine.evaluate(engine.row_to_listing(r), crit, zones,
                                       full_text=src.FULL_TEXT if src else True)[1]
        items.append(_listing_out(r, dups.get(r["id"], []), to_check))
    return {"total": len(items), "items": items, "now": _now(),
            "counts": {k: (c[k] or 0) for k in ("active", "new", "favorites", "dismissed")}}


@router.get("/listings/{listing_id}")
def get_listing(listing_id: int):
    """One listing with its full text, photos, every site it's on, price
    history and the searches that found it."""
    with get_conn() as conn:
        row = _canonical_row(conn, listing_id)
        dups = _duplicates(conn, [row["id"]]).get(row["id"], [])
        searches = rows_to_dicts(conn.execute(
            """SELECT s.id, s.name, m.matched_at FROM housing_matches m JOIN housing_searches s ON s.id = m.search_id
               WHERE m.listing_id = ? ORDER BY m.matched_at""", (row["id"],)).fetchall())
    out = _listing_out(row, dups, with_text=True)
    out["searches"] = searches
    return out


@router.put("/listings/{listing_id}")
def update_listing(listing_id: int, payload: ListingUpdateIn):
    """⭐ favourite, ✕ not for us, seen / unseen. Acts on the canonical row
    (the household state of a flat listed on several sites lives there)."""
    with get_conn() as conn:
        row = _canonical_row(conn, listing_id)
        sets, params = [], []
        if payload.favorite is not None:
            sets.append("favorite = ?")
            params.append(int(payload.favorite))
            if payload.favorite:  # a favourite can't stay set aside
                sets.append("dismissed = 0")
        if payload.dismissed is not None:
            sets.append("dismissed = ?")
            params.append(int(payload.dismissed))
            if payload.dismissed:
                sets += ["favorite = 0", "seen_at = COALESCE(seen_at, ?)"]
                params.append(_now())
        if payload.seen is not None:
            sets.append("seen_at = ?")
            params.append(_now() if payload.seen else None)
        if sets:
            conn.execute(f"UPDATE housing_listings SET {', '.join(sets)} WHERE id = ?", (*params, row["id"]))
    return get_listing(row["id"])


@router.post("/listings/seen")
def mark_seen(payload: SeenIn):
    """Mark listings seen in bulk: the given `ids`, or every new result of
    `search_id`, or (neither) every new listing."""
    sql = "UPDATE housing_listings SET seen_at = ? WHERE seen_at IS NULL AND duplicate_of IS NULL"
    params: list = [_now()]
    if payload.ids:
        sql += f" AND id IN ({','.join('?' * len(payload.ids))})"
        params += payload.ids
    elif payload.search_id is not None:
        sql += " AND id IN (SELECT listing_id FROM housing_matches WHERE search_id = ?)"
        params.append(payload.search_id)
    with get_conn() as conn:
        return {"updated": conn.execute(sql, params).rowcount}


@router.post("/extract")
async def extract_ad(payload: ExtractIn):
    """Pasted ad text (and/or link) → fields for the hand-add form, via the
    LLM (OPENROUTER_API_KEY). Nothing is saved — POST /listings does that."""
    try:
        fields = await extractor.extract(payload.text, payload.url)
    except LLMError as e:
        raise HTTPException(502, str(e))
    fields["source_label"] = extractor.source_label(payload.url)
    return fields


@router.post("/listings", status_code=201)
def create_manual_listing(payload: ManualListingIn):
    """Add an ad by hand (Facebook, WhatsApp, a régie's site…). It is placed
    on the map (federal geocoder, else the postcode's centre), attached to
    every search it fits, and counts as seen."""
    if payload.category not in CATEGORIES:
        raise HTTPException(422, f"Type inconnu : {payload.category}")
    if not (payload.title.strip() or payload.description.strip() or payload.url.strip()):
        raise HTTPException(422, "Donnez au moins un titre, un texte ou un lien")
    zip_code = "".join(ch for ch in payload.zipcode if ch.isdigit())[:4]
    lat, lng, precision = payload.lat, payload.lng, "exact" if payload.lat is not None else "none"
    if lat is None:
        address = ", ".join(p for p in (payload.street.strip(), f"{zip_code} {payload.city}".strip()) if p)
        found = places.geocode(address) if address else None
        if found:
            lat, lng, precision = found["lat"], found["lng"], found["precision"]
    city = payload.city.strip()
    if not zip_code and lat is not None:
        near = places.nearest_locality(lat, lng, city)
        zip_code = near.zip if near else ""
    if not city and zip_code:
        main = places.index().main_locality(zip_code)
        city = main.name if main else ""
    text = f"{payload.title}\n{payload.description}"
    item = make_listing(
        source="manual", source_id=uuid.uuid4().hex, url=payload.url.strip(),
        title=payload.title.strip() or (f"{payload.rooms:g} pièces" if payload.rooms else "Annonce"),
        description=payload.description.strip(), category=payload.category,
        price=payload.price, charges=payload.charges, rooms=payload.rooms, surface=payload.surface,
        floor=payload.floor, street=payload.street.strip(), zipcode=zip_code, city=city,
        canton=places.canton_of_zip(zip_code), lat=lat, lng=lng, geo_precision=precision,
        available_from=payload.available_from,
        features={f for f in payload.features if f in FEATURES} | detect_features(text),
        image_url=payload.image_url.strip(), images=[payload.image_url.strip()] if payload.image_url.strip() else [],
    )
    now = _now()
    with get_conn() as conn:
        listing_id, _ = engine.upsert_listing(conn, item, now)
        conn.execute("UPDATE housing_listings SET seen_at = ? WHERE id = ?", (now, listing_id))
    engine.match_against_searches(listing_id)
    return get_listing(listing_id)


@router.delete("/listings/{listing_id}", status_code=204)
def delete_listing(listing_id: int):
    """Only hand-added ads can be deleted: a site's listing would come back
    at the next check — set it aside instead (`PUT … {"dismissed": true}`)."""
    with get_conn() as conn:
        row = conn.execute("SELECT source FROM housing_listings WHERE id = ?", (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Annonce introuvable")
        if row["source"] != "manual":
            raise HTTPException(409, "Une annonce d'un site reviendrait au prochain passage — utilisez « Pas pour nous »")
        conn.execute("DELETE FROM housing_listings WHERE id = ?", (listing_id,))


def summary() -> dict:
    """The housing block of /api/summary: counters + the newest unseen results."""
    with get_conn() as conn:
        searches = conn.execute(
            "SELECT COUNT(*) c, SUM(auto_run) a, (SELECT MAX(finished_at) FROM housing_runs) last "
            "FROM housing_searches").fetchone()
        base = ("l.duplicate_of IS NULL AND (l.id IN (SELECT listing_id FROM housing_matches) "
                "OR l.source = 'manual' OR l.favorite = 1)")
        counts = conn.execute(
            f"SELECT SUM(l.seen_at IS NULL AND l.dismissed = 0) new, SUM(l.favorite = 1) fav "
            f"FROM housing_listings l WHERE {base}").fetchone()
        newest = rows_to_dicts(conn.execute(
            f"""SELECT l.id, l.title, l.price, l.rooms, l.surface, l.city, l.zipcode, l.image_url, l.source,
                       l.url, l.first_seen_at
                FROM housing_listings l WHERE {base} AND l.seen_at IS NULL AND l.dismissed = 0
                ORDER BY l.first_seen_at DESC, l.id DESC LIMIT 4""").fetchall())
    return {
        "searches": searches["c"] or 0,
        "auto_searches": searches["a"] or 0,
        "last_run_at": searches["last"],
        "new_count": counts["new"] or 0,
        "favorites": counts["fav"] or 0,
        "newest": newest,
    }
