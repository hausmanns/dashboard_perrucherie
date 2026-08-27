"""Storage module: what we own, in which box, whose it is, and what is currently out.

Replaces the old "cartons" spreadsheet:

- `storage_boxes` are the physical containers — a code written on the box plus a theme.
- `storage_items` are the things inside them (owner, category, status).
- Taking something out (`/out`) or putting it back (`/in`) is a single call that
  stamps the date automatically and appends to `storage_events`, so the history
  is kept without any manual bookkeeping.

The search endpoint (`GET /api/storage/items?q=...`) is accent- and case-insensitive
and matches across the item, its box and its owners — it is the fastest way to
answer "where is my X?".
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..database import get_conn, rows_to_dicts

router = APIRouter(prefix="/api/storage", tags=["storage"])

STATUSES = ("stored", "out")


# ---------- Schemas ----------

class BoxIn(BaseModel):
    code: str
    name: str = ""
    kind: str = ""
    location: str = ""
    notes: str = ""


class ItemIn(BaseModel):
    name: str
    box_id: Optional[int] = None
    description: str = ""
    owner: str = ""              # "Seb", "Lea", "Seb,Lea"
    category: str = ""           # vide -> repris du carton
    quantity: int = 1
    notes: str = ""
    status: str = "stored"
    out_since: Optional[str] = None
    out_note: str = ""


class MoveIn(BaseModel):
    box_id: Optional[int] = None
    note: str = ""


class OutIn(BaseModel):
    note: str = ""               # ou / chez qui : "Prete a Tom", "chambre"
    at: Optional[str] = None     # ISO date/datetime, defaut = maintenant


class InIn(BaseModel):
    note: str = ""
    box_id: Optional[int] = None  # ranger dans un autre carton au passage


# ---------- Helpers ----------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize(text: str) -> str:
    """Lowercase + accent-free, so "deguisement" matches the accented spelling.

    Public: the Telegram bot resolves box names and owners the same way.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _owners(raw: str) -> list[str]:
    return [o.strip() for o in (raw or "").split(",") if o.strip()]


def _clean_owner(raw: str) -> str:
    return ",".join(_owners(raw))


ITEM_SELECT = """
SELECT i.*,
       b.code     AS box_code,
       b.name     AS box_name,
       b.kind     AS box_kind,
       b.location AS box_location
FROM storage_items i
LEFT JOIN storage_boxes b ON b.id = i.box_id
"""


def _item_dict(row) -> dict:
    item = dict(row)
    item["owners"] = _owners(item.get("owner", ""))
    return item


def _get_item(conn, item_id: int) -> dict:
    row = conn.execute(ITEM_SELECT + " WHERE i.id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Objet introuvable")
    return _item_dict(row)


def _log(conn, item_id: int, action: str, note: str = "",
         box_id: Optional[int] = None, at: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO storage_events (item_id, action, at, note, box_id) VALUES (?, ?, ?, ?, ?)",
        (item_id, action, at or _now(), note, box_id),
    )


def _haystack(item: dict) -> str:
    """Everything a search query may reasonably match on."""
    parts = [
        item.get("name"), item.get("description"), item.get("notes"),
        item.get("category"), item.get("owner"), item.get("out_note"),
        item.get("box_name"), item.get("box_kind"), item.get("box_location"),
    ]
    code = item.get("box_code")
    if code:
        parts += [code, f"carton {code}"]
    return normalize(" ".join(p for p in parts if p))


def _matches(item: dict, query: str) -> bool:
    """All query words must appear somewhere on the item (AND semantics)."""
    hay = _haystack(item)
    return all(token in hay for token in normalize(query).split())


# "carton 2", "boite 12", "#7" ou simplement "2" -> on veut le contenu de ce carton,
# pas tous les objets dont le nom contient un 2.
_BOX_QUERY_RE = re.compile(r"^(?:carton|boite|box)?\s*#?\s*([0-9]+|[a-z]?[0-9]+)$", re.IGNORECASE)


def _box_code_query(query: str) -> Optional[str]:
    """Return the box code a query designates, or None if it is a normal search."""
    match = _BOX_QUERY_RE.match(normalize(query).strip())
    return match.group(1) if match else None


# ---------- Boxes ----------

@router.get("/boxes")
def list_boxes():
    """All boxes, with how many items they hold and how many of those are out."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT b.*,
                      COUNT(i.id)                                       AS item_count,
                      SUM(CASE WHEN i.status = 'out' THEN 1 ELSE 0 END) AS out_count
               FROM storage_boxes b
               LEFT JOIN storage_items i ON i.box_id = b.id
               GROUP BY b.id
               ORDER BY CAST(b.code AS INTEGER), b.code"""
        ).fetchall()
    boxes = []
    for row in rows:
        box = dict(row)
        box["out_count"] = box["out_count"] or 0
        boxes.append(box)
    return boxes


@router.post("/boxes", status_code=201)
def create_box(box: BoxIn):
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM storage_boxes WHERE code = ?", (box.code.strip(),)).fetchone():
            raise HTTPException(409, f"Le carton {box.code} existe deja")
        cur = conn.execute(
            "INSERT INTO storage_boxes (code, name, kind, location, notes) VALUES (?, ?, ?, ?, ?)",
            (box.code.strip(), box.name.strip(), box.kind.strip(), box.location.strip(), box.notes),
        )
        return dict(conn.execute("SELECT * FROM storage_boxes WHERE id = ?", (cur.lastrowid,)).fetchone())


@router.get("/boxes/{box_id}")
def get_box(box_id: int):
    """A box and everything inside it."""
    with get_conn() as conn:
        box = conn.execute("SELECT * FROM storage_boxes WHERE id = ?", (box_id,)).fetchone()
        if not box:
            raise HTTPException(404, "Carton introuvable")
        items = conn.execute(
            ITEM_SELECT + " WHERE i.box_id = ? ORDER BY i.name COLLATE NOCASE", (box_id,)
        ).fetchall()
    return {**dict(box), "items": [_item_dict(r) for r in items]}


@router.put("/boxes/{box_id}")
def update_box(box_id: int, box: BoxIn):
    with get_conn() as conn:
        clash = conn.execute(
            "SELECT 1 FROM storage_boxes WHERE code = ? AND id <> ?", (box.code.strip(), box_id)
        ).fetchone()
        if clash:
            raise HTTPException(409, f"Le carton {box.code} existe deja")
        cur = conn.execute(
            "UPDATE storage_boxes SET code=?, name=?, kind=?, location=?, notes=? WHERE id=?",
            (box.code.strip(), box.name.strip(), box.kind.strip(), box.location.strip(), box.notes, box_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Carton introuvable")
        return dict(conn.execute("SELECT * FROM storage_boxes WHERE id = ?", (box_id,)).fetchone())


@router.delete("/boxes/{box_id}", status_code=204)
def delete_box(box_id: int):
    """Delete a box. Its items are kept and become box-less (a trier)."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM storage_boxes WHERE id = ?", (box_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Carton introuvable")


# ---------- Items ----------

@router.get("/items")
def list_items(
    q: Annotated[str, Query(description="Recherche libre (insensible à la casse et aux accents)")] = "",
    owner: Annotated[str, Query(description="Filtre par propriétaire (Seb, Lea…)")] = "",
    status: Annotated[str, Query(description="stored | out")] = "",
    category: Annotated[str, Query(description="Filtre par collection")] = "",
    box_id: Annotated[Optional[int], Query(description="Filtre par carton")] = None,
    no_box: Annotated[bool, Query(description="Seulement les objets sans carton")] = False,
):
    """Search/filter items. Everything is optional — no parameter returns all items."""
    if status and status not in STATUSES:
        raise HTTPException(422, "status doit valoir 'stored' ou 'out'")

    sql, params, where = ITEM_SELECT, [], []
    if status:
        where.append("i.status = ?")
        params.append(status)
    if box_id is not None:
        where.append("i.box_id = ?")
        params.append(box_id)
    if no_box:
        where.append("i.box_id IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.name COLLATE NOCASE"

    with get_conn() as conn:
        items = [_item_dict(r) for r in conn.execute(sql, params).fetchall()]

    if owner:
        target = normalize(owner)
        items = [i for i in items if any(normalize(o) == target for o in i["owners"])]
    if category:
        target = normalize(category)
        items = [i for i in items if normalize(i["category"] or "") == target]
    if q.strip():
        code = _box_code_query(q)
        if code is not None and any(normalize(i["box_code"] or "") == code for i in items):
            items = [i for i in items if normalize(i["box_code"] or "") == code]
        else:
            items = [i for i in items if _matches(i, q)]
            # Un match sur le nom de l'objet est plus pertinent qu'un match sur son carton.
            tokens = normalize(q).split()
            items.sort(key=lambda i: (not all(t in normalize(i["name"]) for t in tokens), normalize(i["name"])))
    return items


@router.post("/items", status_code=201)
def create_item(item: ItemIn):
    """Add something to a box. `category` defaults to the box's theme."""
    if item.status not in STATUSES:
        raise HTTPException(422, "status doit valoir 'stored' ou 'out'")
    with get_conn() as conn:
        category = item.category.strip()
        if item.box_id is not None:
            box = conn.execute("SELECT * FROM storage_boxes WHERE id = ?", (item.box_id,)).fetchone()
            if not box:
                raise HTTPException(404, "Carton introuvable")
            category = category or box["name"]
        out_since = item.out_since or (_now() if item.status == "out" else None)
        cur = conn.execute(
            """INSERT INTO storage_items
                   (box_id, name, description, owner, category, quantity, notes,
                    status, out_since, out_note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item.box_id, item.name.strip(), item.description, _clean_owner(item.owner), category,
             max(1, item.quantity), item.notes, item.status, out_since, item.out_note),
        )
        _log(conn, cur.lastrowid, "created", box_id=item.box_id)
        return _get_item(conn, cur.lastrowid)


@router.get("/items/{item_id}")
def get_item(item_id: int):
    with get_conn() as conn:
        return _get_item(conn, item_id)


@router.put("/items/{item_id}")
def update_item(item_id: int, item: ItemIn):
    if item.status not in STATUSES:
        raise HTTPException(422, "status doit valoir 'stored' ou 'out'")
    with get_conn() as conn:
        current = conn.execute("SELECT * FROM storage_items WHERE id = ?", (item_id,)).fetchone()
        if not current:
            raise HTTPException(404, "Objet introuvable")
        if item.box_id is not None and not conn.execute(
            "SELECT 1 FROM storage_boxes WHERE id = ?", (item.box_id,)
        ).fetchone():
            raise HTTPException(404, "Carton introuvable")

        out_since = item.out_since
        if item.status == "out" and not out_since:
            out_since = current["out_since"] or _now()
        if item.status == "stored":
            out_since = None

        conn.execute(
            """UPDATE storage_items
               SET box_id=?, name=?, description=?, owner=?, category=?, quantity=?, notes=?,
                   status=?, out_since=?, out_note=?, updated_at=?
               WHERE id=?""",
            (item.box_id, item.name.strip(), item.description, _clean_owner(item.owner),
             item.category.strip(), max(1, item.quantity), item.notes,
             item.status, out_since, item.out_note if item.status == "out" else "", _now(), item_id),
        )
        if item.box_id != current["box_id"]:
            _log(conn, item_id, "moved", box_id=item.box_id)
        return _get_item(conn, item_id)


@router.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM storage_items WHERE id = ?", (item_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Objet introuvable")


@router.post("/items/{item_id}/out")
def take_out(item_id: int, payload: OutIn):
    """Take an item out of its box — the date is stamped automatically."""
    with get_conn() as conn:
        item = _get_item(conn, item_id)
        at = payload.at or _now()
        conn.execute(
            "UPDATE storage_items SET status='out', out_since=?, out_note=?, updated_at=? WHERE id=?",
            (at, payload.note, _now(), item_id),
        )
        _log(conn, item_id, "out", payload.note, item["box_id"], at)
        return _get_item(conn, item_id)


@router.post("/items/{item_id}/in")
def put_back(item_id: int, payload: InIn):
    """Put an item back in its box (optionally in a different one)."""
    with get_conn() as conn:
        item = _get_item(conn, item_id)
        box_id = payload.box_id if payload.box_id is not None else item["box_id"]
        if box_id is not None and not conn.execute(
            "SELECT 1 FROM storage_boxes WHERE id = ?", (box_id,)
        ).fetchone():
            raise HTTPException(404, "Carton introuvable")
        conn.execute(
            "UPDATE storage_items SET status='stored', out_since=NULL, out_note='', box_id=?, updated_at=? WHERE id=?",
            (box_id, _now(), item_id),
        )
        _log(conn, item_id, "in", payload.note, box_id)
        return _get_item(conn, item_id)


@router.post("/items/{item_id}/move")
def move_item(item_id: int, payload: MoveIn):
    """Move an item to another box (`box_id: null` -> no box, "a trier")."""
    with get_conn() as conn:
        _get_item(conn, item_id)
        if payload.box_id is not None and not conn.execute(
            "SELECT 1 FROM storage_boxes WHERE id = ?", (payload.box_id,)
        ).fetchone():
            raise HTTPException(404, "Carton introuvable")
        conn.execute(
            "UPDATE storage_items SET box_id=?, updated_at=? WHERE id=?",
            (payload.box_id, _now(), item_id),
        )
        _log(conn, item_id, "moved", payload.note, payload.box_id)
        return _get_item(conn, item_id)


@router.get("/items/{item_id}/history")
def item_history(item_id: int):
    """Every out/in/move recorded for an item, most recent first."""
    with get_conn() as conn:
        _get_item(conn, item_id)
        rows = conn.execute(
            """SELECT e.*, b.code AS box_code, b.name AS box_name
               FROM storage_events e LEFT JOIN storage_boxes b ON b.id = e.box_id
               WHERE e.item_id = ? ORDER BY e.at DESC, e.id DESC""",
            (item_id,),
        ).fetchall()
    return rows_to_dicts(rows)


# ---------- Overview ----------

@router.get("/summary")
def storage_summary():
    """Counters, the items currently out, and the values usable as filters."""
    with get_conn() as conn:
        total_items = conn.execute("SELECT COUNT(*) c FROM storage_items").fetchone()["c"]
        total_boxes = conn.execute("SELECT COUNT(*) c FROM storage_boxes").fetchone()["c"]
        unboxed = conn.execute("SELECT COUNT(*) c FROM storage_items WHERE box_id IS NULL").fetchone()["c"]
        out_rows = conn.execute(
            ITEM_SELECT + " WHERE i.status = 'out' ORDER BY i.out_since DESC, i.name COLLATE NOCASE"
        ).fetchall()
        all_rows = conn.execute("SELECT owner, category FROM storage_items").fetchall()

    owners: dict[str, int] = {}
    categories: dict[str, int] = {}
    for row in all_rows:
        for name in _owners(row["owner"]):
            owners[name] = owners.get(name, 0) + 1
        if row["category"]:
            categories[row["category"]] = categories.get(row["category"], 0) + 1

    out = [_item_dict(r) for r in out_rows]
    return {
        "total_items": total_items,
        "total_boxes": total_boxes,
        "unboxed_items": unboxed,
        "out_count": len(out),
        "out": out,
        "owners": [{"name": n, "count": c} for n, c in sorted(owners.items(), key=lambda kv: -kv[1])],
        "categories": [{"name": n, "count": c} for n, c in sorted(categories.items(), key=lambda kv: normalize(kv[0]))],
    }
