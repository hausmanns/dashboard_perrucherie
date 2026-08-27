"""Wishlist module: what each person of the household would love to own one day.

Two tables:
- `wishlist_people` — the people who have a list (name, emoji, birthday, notes
  a gifter needs: sizes, tastes, what *not* to buy).
- `wishlist_items`  — one wish per row, with everything needed to actually buy
  it (price, shop, link, size, colour, priority, occasion, deadline, photo).

The point of the feature is that **someone else** browses the list and buys the
present. Two mechanisms serve that:

- **Reservation** — `POST /items/{id}/reserve` claims a wish so two gifters
  don't buy the same thing; `/bought` then `/received` close the loop.
- **Spoiler-free view** — every read endpoint accepts `?hide_reservations=true`,
  which strips who reserved/bought what *and* rewinds the status to `wanted`.
  That is the view to show to the owner of the list; the plain view is the
  gifter's view.
"""
from __future__ import annotations

import unicodedata
import uuid
from datetime import date, datetime
from typing import Annotated, Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..database import PHOTOS_DIR, get_conn, rows_to_dicts
from ..photos import claim_photo, delete_photo, read_image_upload, save_tmp_photo
from ..vision import WISH_CATEGORIES, IdentificationError, identify_wish

router = APIRouter(prefix="/api/wishlist", tags=["wishlist"])

STATUSES = ("wanted", "reserved", "bought", "received", "archived")
# Statuses meaning "a gifter has already taken care of it".
TAKEN_STATUSES = ("reserved", "bought")
# Fields a spoiler-free view must never expose to the owner of the list.
SECRET_FIELDS = ("reserved_by", "reserved_at", "bought_by", "bought_at")


class PersonIn(BaseModel):
    name: str
    emoji: str = "🎁"
    birthday: Optional[str] = None  # ISO date (YYYY-MM-DD)
    notes: str = ""                 # tailles, goûts, ce qu'il ne faut pas offrir


class WishIn(BaseModel):
    person_id: int
    name: str
    description: str = ""
    category: str = "autre"
    price: Optional[float] = Field(default=None, ge=0)
    currency: str = "CHF"
    url: str = ""
    shop: str = ""
    size: str = ""
    color: str = ""
    quantity: int = Field(default=1, ge=1)
    priority: int = Field(default=2, ge=1, le=3)  # 1 = un jour, 2 = ça me plairait, 3 = j'en rêve
    occasion: str = ""
    target_date: Optional[str] = None             # ISO date : à offrir avant
    notes: str = ""
    status: Optional[str] = None                  # None = inchangé (création : 'wanted')
    photo: Optional[str] = None                   # token renvoyé par POST /identify


class ReserveIn(BaseModel):
    by: str = ""  # qui s'en occupe


class TelegramLinkIn(BaseModel):
    telegram_user_id: Optional[int] = None  # None = délier le compte


# ---------- Helpers ----------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_category(category: str) -> str:
    category = (category or "autre").strip().lower()
    return category if category in WISH_CATEGORIES else "autre"


def _clean_status(status: Optional[str], default: Optional[str] = None) -> Optional[str]:
    if status is None:
        return default
    status = status.strip().lower()
    if status not in STATUSES:
        raise HTTPException(422, f"Statut inconnu : {status} (attendu : {', '.join(STATUSES)})")
    return status


def _days_until(iso_date: Optional[str]) -> Optional[int]:
    """Whole days from today to an ISO date (negative = passé)."""
    if not iso_date:
        return None
    try:
        return (date.fromisoformat(iso_date[:10]) - date.today()).days
    except ValueError:
        return None


def _days_until_birthday(birthday: Optional[str]) -> Optional[int]:
    """Days until the next occurrence of a birthday (year-agnostic)."""
    if not birthday:
        return None
    try:
        bday = date.fromisoformat(birthday[:10])
    except ValueError:
        return None
    today = date.today()

    def _in_year(year: int) -> date:
        try:
            return bday.replace(year=year)
        except ValueError:  # 29 février sur une année non bissextile
            return date(year, 3, 1)

    next_one = _in_year(today.year)
    if next_one < today:
        next_one = _in_year(today.year + 1)
    return (next_one - today).days


def _enrich_person(person: dict) -> dict:
    person["days_until_birthday"] = _days_until_birthday(person.get("birthday"))
    return person


def _enrich_item(item: dict, hide_reservations: bool = False) -> dict:
    """Add computed fields; optionally strip everything that spoils the surprise."""
    item["days_until_target"] = _days_until(item.get("target_date"))
    item["is_taken"] = item["status"] in TAKEN_STATUSES
    if hide_reservations:
        for field in SECRET_FIELDS:
            item[field] = None
        if item["status"] in TAKEN_STATUSES:
            item["status"] = "wanted"
        item["is_taken"] = None
        item["reservations_hidden"] = True
    return item


def normalize_name(name: str) -> str:
    """Fold a first name to a comparison key: « Séb », « SEB » and « seb. » all → "seb"."""
    decomposed = unicodedata.normalize("NFD", name or "")
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(c for c in without_accents.casefold() if c.isalnum())


def match_people(hint: str) -> list[dict]:
    """People matching a free-form first name, ignoring case, accents and punctuation.

    Tried in order of confidence — exact key, then prefix either way (so « Séb »
    finds « Sébastien » and vice versa), then substring. Returns every candidate:
    more than one means the hint is ambiguous and the caller should ask.
    """
    key = normalize_name(hint)
    if not key:
        return []
    people = [(p, normalize_name(p["name"])) for p in list_people()]

    for candidates in (
        [p for p, k in people if k == key],
        [p for p, k in people if k.startswith(key) or key.startswith(k)],
        [p for p, k in people if key in k or k in key],
    ):
        if candidates:
            return candidates
    return []


def person_by_telegram_user(telegram_user_id: int) -> Optional[dict]:
    """The person a Telegram account was linked to with `/moi <prénom>`, if any."""
    if not telegram_user_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM wishlist_people WHERE telegram_user_id = ?", (telegram_user_id,)
        ).fetchone()
    return _enrich_person(dict(row)) if row else None


def _get_item_row(conn, item_id: int):
    row = conn.execute(
        """SELECT i.*, p.name AS person_name, p.emoji AS person_emoji
           FROM wishlist_items i JOIN wishlist_people p ON p.id = i.person_id
           WHERE i.id = ?""",
        (item_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Envie introuvable")
    return row


# ---------- People ----------

@router.get("/people")
def list_people():
    """Everyone who has a wishlist, with a countdown to their next birthday."""
    with get_conn() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM wishlist_people ORDER BY name").fetchall())
    return [_enrich_person(r) for r in rows]


@router.post("/people", status_code=201)
def create_person(person: PersonIn):
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM wishlist_people WHERE name = ?", (person.name,)).fetchone():
            raise HTTPException(409, f"« {person.name} » a déjà une liste d'envies")
        cur = conn.execute(
            "INSERT INTO wishlist_people (name, emoji, birthday, notes) VALUES (?, ?, ?, ?)",
            (person.name, person.emoji, person.birthday, person.notes),
        )
        row = conn.execute("SELECT * FROM wishlist_people WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _enrich_person(dict(row))


@router.get("/people/resolve")
def resolve_people(name: str):
    """Resolve a free-form first name to the person who owns that wishlist.

    Case-, accent- and punctuation-insensitive: « seb », « Séb » and « SEB. »
    all resolve to « Séb ». `resolved` is set only when exactly one person
    matches; otherwise inspect `matches` (empty = unknown, several = ambiguous).
    """
    matches = match_people(name)
    return {
        "query": name,
        "matches": matches,
        "resolved": matches[0] if len(matches) == 1 else None,
    }


@router.post("/people/{person_id}/telegram")
def link_telegram_user(person_id: int, payload: TelegramLinkIn):
    """Link a Telegram account to a person, so the bot knows who « je veux… » means.

    Passing `telegram_user_id: null` unlinks. A Telegram account can only point
    at one person, so linking steals it from whoever held it before.
    """
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM wishlist_people WHERE id = ?", (person_id,)).fetchone():
            raise HTTPException(404, "Personne introuvable")
        if payload.telegram_user_id is not None:
            conn.execute(
                "UPDATE wishlist_people SET telegram_user_id = NULL WHERE telegram_user_id = ?",
                (payload.telegram_user_id,),
            )
        conn.execute(
            "UPDATE wishlist_people SET telegram_user_id = ? WHERE id = ?",
            (payload.telegram_user_id, person_id),
        )
        row = conn.execute("SELECT * FROM wishlist_people WHERE id = ?", (person_id,)).fetchone()
    return _enrich_person(dict(row))


@router.put("/people/{person_id}")
def update_person(person_id: int, person: PersonIn):
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE wishlist_people SET name=?, emoji=?, birthday=?, notes=? WHERE id=?",
            (person.name, person.emoji, person.birthday, person.notes, person_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Personne introuvable")
        row = conn.execute("SELECT * FROM wishlist_people WHERE id = ?", (person_id,)).fetchone()
    return _enrich_person(dict(row))


@router.delete("/people/{person_id}", status_code=204)
def delete_person(person_id: int):
    """Delete a person *and* their whole wishlist (photos included)."""
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM wishlist_people WHERE id = ?", (person_id,)).fetchone():
            raise HTTPException(404, "Personne introuvable")
        photos = [
            r["photo"] for r in
            conn.execute("SELECT photo FROM wishlist_items WHERE person_id = ?", (person_id,)).fetchall()
        ]
        conn.execute("DELETE FROM wishlist_people WHERE id = ?", (person_id,))
    for photo in photos:
        delete_photo(photo)


# ---------- Overview (the gifter's starting point) ----------

@router.get("/overview")
def overview(hide_reservations: Annotated[bool, Query(description="Vue sans spoiler (pour le/la destinataire)")] = False):
    """Per-person digest: counters, budget of what is still to buy, top ideas.

    The best single call for an agent asked « qu'est-ce que je pourrais offrir à X ? ».
    """
    people = list_people()
    result = []
    with get_conn() as conn:
        for person in people:
            rows = rows_to_dicts(conn.execute(
                "SELECT * FROM wishlist_items WHERE person_id = ? ORDER BY priority DESC, created_at",
                (person["id"],),
            ).fetchall())
            # Sans spoiler, une envie réservée ou achetée compte encore comme
            # « à offrir » : sinon les compteurs eux-mêmes vendent la mèche.
            open_statuses = ("wanted",) + ((TAKEN_STATUSES) if hide_reservations else ())
            counts = {status: 0 for status in STATUSES}
            for row in rows:
                status = "wanted" if hide_reservations and row["status"] in TAKEN_STATUSES else row["status"]
                counts[status] = counts.get(status, 0) + 1
            open_items = [r for r in rows if r["status"] in open_statuses]
            result.append({
                **person,
                "counts": counts,
                "total_items": len(rows),
                "open_items": len(open_items),
                "estimated_budget": round(sum(r["price"] or 0 for r in open_items), 2),
                "currency": open_items[0]["currency"] if open_items else "CHF",
                "top_ideas": [_enrich_item(dict(r), hide_reservations) for r in open_items[:3]],
            })
    return result


# ---------- Items ----------

@router.get("/items")
def list_items(
    person_id: Optional[int] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    occasion: Optional[str] = None,
    hide_reservations: Annotated[bool, Query(description="Vue sans spoiler (pour le/la destinataire)")] = False,
):
    """List wishes, most wanted first. All filters are optional and combinable."""
    sql = """SELECT i.*, p.name AS person_name, p.emoji AS person_emoji
             FROM wishlist_items i JOIN wishlist_people p ON p.id = i.person_id"""
    where, params = [], []
    if person_id is not None:
        where.append("i.person_id = ?")
        params.append(person_id)
    if status:
        where.append("i.status = ?")
        params.append(_clean_status(status))
    if category:
        where.append("i.category = ?")
        params.append(_clean_category(category))
    if occasion:
        where.append("i.occasion = ?")
        params.append(occasion)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.priority DESC, i.created_at"

    with get_conn() as conn:
        rows = rows_to_dicts(conn.execute(sql, params).fetchall())
    return [_enrich_item(r, hide_reservations) for r in rows]


@router.post("/items", status_code=201)
def create_item(item: WishIn):
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM wishlist_people WHERE id = ?", (item.person_id,)).fetchone():
            raise HTTPException(404, "Personne introuvable")
        cur = conn.execute(
            """INSERT INTO wishlist_items
               (person_id, name, description, category, price, currency, url, shop, size, color,
                quantity, priority, occasion, target_date, status, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item.person_id, item.name, item.description, _clean_category(item.category),
             item.price, item.currency, item.url, item.shop, item.size, item.color,
             item.quantity, item.priority, item.occasion, item.target_date,
             _clean_status(item.status, "wanted"), item.notes),
        )
        item_id = cur.lastrowid
        photo = claim_photo(item.photo, "wish", item_id)
        if photo:
            conn.execute("UPDATE wishlist_items SET photo = ? WHERE id = ?", (photo, item_id))
        row = _get_item_row(conn, item_id)
    return _enrich_item(dict(row))


@router.post("/identify")
async def identify(file: UploadFile = File(...)):
    """Identify a wished-for object from a photo (camera or upload) via a vision LLM.

    Returns suggested fields to pre-fill the wish form, plus a `photo_token`:
    pass it back as `photo` when creating/updating the wish to attach the photo.
    """
    content, ext = await read_image_upload(file)

    try:
        suggestion = await identify_wish(content, file.content_type)
    except IdentificationError as e:
        raise HTTPException(502, str(e))

    token = uuid.uuid4().hex
    save_tmp_photo(content, ext, token)
    return {**suggestion, "photo_token": token}


@router.post("/photo")
async def upload_photo(file: UploadFile = File(...)):
    """Attach a photo to a wish **without** any AI identification.

    Returns `{photo_token}`: pass it back as `photo` when creating/updating
    the wish. Use this when the form is filled by hand.
    """
    content, ext = await read_image_upload(file)
    token = uuid.uuid4().hex
    save_tmp_photo(content, ext, token)
    return {"photo_token": token}


@router.get("/items/{item_id}")
def get_item(item_id: int, hide_reservations: bool = False):
    with get_conn() as conn:
        row = _get_item_row(conn, item_id)
    return _enrich_item(dict(row), hide_reservations)


@router.put("/items/{item_id}")
def update_item(item_id: int, item: WishIn):
    """Full update. A null/omitted `status` leaves the current one alone, so editing
    a wish never wipes a gifter's reservation."""
    with get_conn() as conn:
        current = _get_item_row(conn, item_id)
        if not conn.execute("SELECT 1 FROM wishlist_people WHERE id = ?", (item.person_id,)).fetchone():
            raise HTTPException(404, "Personne introuvable")
        conn.execute(
            """UPDATE wishlist_items SET person_id=?, name=?, description=?, category=?, price=?,
               currency=?, url=?, shop=?, size=?, color=?, quantity=?, priority=?, occasion=?,
               target_date=?, status=?, notes=?, updated_at=? WHERE id=?""",
            (item.person_id, item.name, item.description, _clean_category(item.category),
             item.price, item.currency, item.url, item.shop, item.size, item.color,
             item.quantity, item.priority, item.occasion, item.target_date,
             _clean_status(item.status, current["status"]), item.notes, _now(), item_id),
        )
        photo = claim_photo(item.photo, "wish", item_id)
        if photo:
            conn.execute("UPDATE wishlist_items SET photo = ? WHERE id = ?", (photo, item_id))
        row = _get_item_row(conn, item_id)
    return _enrich_item(dict(row))


@router.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int):
    with get_conn() as conn:
        row = _get_item_row(conn, item_id)
        conn.execute("DELETE FROM wishlist_items WHERE id = ?", (item_id,))
    delete_photo(row["photo"])


@router.get("/items/{item_id}/photo")
def item_photo(item_id: int):
    """Serve the wish's photo, if one was attached."""
    with get_conn() as conn:
        row = conn.execute("SELECT photo FROM wishlist_items WHERE id = ?", (item_id,)).fetchone()
    if not row or not row["photo"]:
        raise HTTPException(404, "Pas de photo pour cette envie")
    path = PHOTOS_DIR / row["photo"]
    if not path.exists():
        raise HTTPException(404, "Fichier photo introuvable")
    return FileResponse(path)


# ---------- Gifting workflow ----------

@router.post("/items/{item_id}/reserve", status_code=201)
def reserve_item(item_id: int, payload: ReserveIn):
    """Claim a wish so nobody else buys the same present. 409 if already taken."""
    with get_conn() as conn:
        row = _get_item_row(conn, item_id)
        if row["status"] in TAKEN_STATUSES:
            taker = row["bought_by"] or row["reserved_by"] or "quelqu'un"
            raise HTTPException(409, f"Déjà pris en charge par {taker}")
        if row["status"] == "received":
            raise HTTPException(409, "Cette envie a déjà été offerte")
        conn.execute(
            "UPDATE wishlist_items SET status='reserved', reserved_by=?, reserved_at=?, updated_at=? WHERE id=?",
            (payload.by, _now(), _now(), item_id),
        )
        row = _get_item_row(conn, item_id)
    return _enrich_item(dict(row))


@router.post("/items/{item_id}/unreserve", status_code=201)
def unreserve_item(item_id: int):
    """Put a reserved wish back in the pool (changed my mind)."""
    with get_conn() as conn:
        _get_item_row(conn, item_id)
        conn.execute(
            """UPDATE wishlist_items SET status='wanted', reserved_by='', reserved_at=NULL,
               bought_by='', bought_at=NULL, updated_at=? WHERE id=?""",
            (_now(), item_id),
        )
        row = _get_item_row(conn, item_id)
    return _enrich_item(dict(row))


@router.post("/items/{item_id}/bought", status_code=201)
def buy_item(item_id: int, payload: ReserveIn):
    """The present is bought and waiting to be given.

    `by` defaults to whoever had reserved it; reserving is not a prerequisite.
    """
    with get_conn() as conn:
        row = _get_item_row(conn, item_id)
        buyer = payload.by or row["reserved_by"] or ""
        now = _now()
        conn.execute(
            """UPDATE wishlist_items SET status='bought', bought_by=?, bought_at=?,
               reserved_by=COALESCE(NULLIF(reserved_by, ''), ?), reserved_at=COALESCE(reserved_at, ?),
               updated_at=? WHERE id=?""",
            (buyer, now, buyer, now, now, item_id),
        )
        row = _get_item_row(conn, item_id)
    return _enrich_item(dict(row))


@router.post("/items/{item_id}/received", status_code=201)
def receive_item(item_id: int):
    """The present has been given — the wish leaves the list of things to buy."""
    with get_conn() as conn:
        _get_item_row(conn, item_id)
        now = _now()
        conn.execute(
            "UPDATE wishlist_items SET status='received', received_at=?, updated_at=? WHERE id=?",
            (now, now, item_id),
        )
        row = _get_item_row(conn, item_id)
    return _enrich_item(dict(row))
