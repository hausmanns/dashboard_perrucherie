"""Grocery module: grocery list (manual + generated from a meal plan) and fridge.

- Manual items (source='manual') make up the plan-independent grocery list.
- `POST /api/grocery/from-plan` regenerates plan-dependent items (source='plan')
  from the ingredients of every dish used in a meal plan, merging duplicates.
- Buying an item (`POST /api/grocery/{id}/buy`) moves it into the fridge.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import get_conn, rows_to_dicts

router = APIRouter(prefix="/api", tags=["grocery"])


class GroceryItemIn(BaseModel):
    name: str
    quantity: str = ""


class FromPlanIn(BaseModel):
    plan_id: int


# ---------- Ingredient parsing (dish ingredients → grocery items) ----------

# "400 g de riz basmati" → (400, "g", "riz basmati") ; "2 oignons" → (2, "", "oignons")
_NUM_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(.*)$")
_UNITS = {
    "g", "gr", "kg", "mg", "l", "cl", "ml", "dl",
    "cs", "càs", "cc", "càc", "c",
    "pièce", "pièces", "tranche", "tranches", "boîte", "boîtes",
    "sachet", "sachets", "botte", "bottes", "paquet", "paquets",
    "pot", "pots", "verre", "verres",
}


def _parse_ingredient(line: str) -> tuple[Optional[float], str, str]:
    """Split an ingredient line into (quantity, unit, name). Unparseable → (None, '', line)."""
    line = line.strip()
    m = _NUM_RE.match(line)
    if not m:
        return None, "", line
    try:
        qty = float(m.group(1).replace(",", "."))
    except ValueError:
        return None, "", line
    rest = m.group(2).strip()
    unit = ""
    if rest:
        first, _, remainder = rest.partition(" ")
        if first.lower().rstrip(".") in _UNITS:
            unit = first.lower().rstrip(".")
            rest = remainder.strip()
    rest = re.sub(r"^(?:de\s+|d')", "", rest, flags=re.IGNORECASE).strip()
    return qty, unit, rest or line


def _fmt_qty(qty: float, unit: str) -> str:
    num = int(qty) if qty == int(qty) else round(qty, 2)
    return f"{num} {unit}".strip()


def _plan_ingredients(conn, plan_id: int) -> list[dict]:
    """Aggregate the ingredients of all dishes placed on a meal plan.

    Returns one dict per distinct ingredient: {name, quantity, dishes}.
    Duplicates are merged by (name, unit); quantities are summed when numeric.
    """
    rows = conn.execute(
        """SELECT DISTINCT d.id, d.name, d.ingredients
           FROM meal_plan_entries e JOIN dishes d ON d.id = e.dish_id
           WHERE e.plan_id = ? AND e.dish_id IS NOT NULL""",
        (plan_id,),
    ).fetchall()

    merged: dict[tuple[str, str], dict] = {}
    for dish in rows:
        for line in (dish["ingredients"] or "").splitlines():
            line = line.strip()
            if not line:
                continue
            qty, unit, name = _parse_ingredient(line)
            key = (name.casefold(), unit)
            if key not in merged:
                merged[key] = {"name": name, "qty": qty, "unit": unit, "dishes": set()}
            elif qty is not None and merged[key]["qty"] is not None:
                merged[key]["qty"] += qty
            merged[key]["dishes"].add(dish["name"])

    return [
        {
            "name": item["name"],
            "quantity": _fmt_qty(item["qty"], item["unit"]) if item["qty"] is not None else "",
            "dishes": ", ".join(sorted(item["dishes"])),
        }
        for item in sorted(merged.values(), key=lambda i: i["name"].casefold())
    ]


# ---------- Grocery list ----------

@router.get("/grocery")
def list_grocery():
    """All grocery items, plan-generated first then manual, oldest first."""
    with get_conn() as conn:
        return rows_to_dicts(conn.execute(
            "SELECT * FROM grocery_items ORDER BY source DESC, created_at"
        ).fetchall())


@router.post("/grocery", status_code=201)
def add_grocery_item(item: GroceryItemIn):
    """Add a manual (plan-independent) grocery item."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO grocery_items (name, quantity, source) VALUES (?, ?, 'manual')",
            (item.name, item.quantity),
        )
        return dict(conn.execute("SELECT * FROM grocery_items WHERE id = ?", (cur.lastrowid,)).fetchone())


@router.put("/grocery/{item_id}")
def update_grocery_item(item_id: int, item: GroceryItemIn):
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE grocery_items SET name=?, quantity=? WHERE id=?",
            (item.name, item.quantity, item_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Article introuvable")
        return dict(conn.execute("SELECT * FROM grocery_items WHERE id = ?", (item_id,)).fetchone())


@router.delete("/grocery/{item_id}", status_code=204)
def delete_grocery_item(item_id: int):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM grocery_items WHERE id = ?", (item_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Article introuvable")


@router.post("/grocery/{item_id}/buy", status_code=201)
def buy_grocery_item(item_id: int):
    """Mark an item as bought: it leaves the grocery list and enters the fridge."""
    with get_conn() as conn:
        item = conn.execute("SELECT * FROM grocery_items WHERE id = ?", (item_id,)).fetchone()
        if not item:
            raise HTTPException(404, "Article introuvable")
        cur = conn.execute(
            "INSERT INTO fridge_items (name, quantity, source) VALUES (?, ?, 'grocery')",
            (item["name"], item["quantity"]),
        )
        conn.execute("DELETE FROM grocery_items WHERE id = ?", (item_id,))
        return dict(conn.execute("SELECT * FROM fridge_items WHERE id = ?", (cur.lastrowid,)).fetchone())


@router.post("/grocery/from-plan")
def grocery_from_plan(payload: FromPlanIn):
    """(Re)generate plan-dependent grocery items from a meal plan's dish ingredients.

    Replaces all existing items for that plan (source='plan', plan_id=…);
    manual items are left untouched.
    """
    with get_conn() as conn:
        plan = conn.execute("SELECT * FROM meal_plans WHERE id = ?", (payload.plan_id,)).fetchone()
        if not plan:
            raise HTTPException(404, "Plan introuvable")
        conn.execute(
            "DELETE FROM grocery_items WHERE source = 'plan' AND plan_id = ?", (payload.plan_id,)
        )
        items = _plan_ingredients(conn, payload.plan_id)
        conn.executemany(
            "INSERT INTO grocery_items (name, quantity, source, plan_id, dishes) VALUES (?, ?, 'plan', ?, ?)",
            [(i["name"], i["quantity"], payload.plan_id, i["dishes"]) for i in items],
        )
    return {
        "plan_id": payload.plan_id,
        "plan_name": plan["name"],
        "count": len(items),
        "items": items,
    }


# ---------- Fridge ----------

@router.get("/fridge")
def list_fridge():
    with get_conn() as conn:
        return rows_to_dicts(conn.execute(
            "SELECT * FROM fridge_items ORDER BY added_at DESC"
        ).fetchall())


@router.post("/fridge", status_code=201)
def add_fridge_item(item: GroceryItemIn):
    """Add something directly to the fridge (bought outside the list, leftovers…)."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO fridge_items (name, quantity, source) VALUES (?, ?, 'manual')",
            (item.name, item.quantity),
        )
        return dict(conn.execute("SELECT * FROM fridge_items WHERE id = ?", (cur.lastrowid,)).fetchone())


@router.put("/fridge/{item_id}")
def update_fridge_item(item_id: int, item: GroceryItemIn):
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE fridge_items SET name=?, quantity=? WHERE id=?",
            (item.name, item.quantity, item_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Article introuvable")
        return dict(conn.execute("SELECT * FROM fridge_items WHERE id = ?", (item_id,)).fetchone())


@router.delete("/fridge/{item_id}", status_code=204)
def delete_fridge_item(item_id: int):
    """Remove from the fridge (consumed, expired…)."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM fridge_items WHERE id = ?", (item_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Article introuvable")
