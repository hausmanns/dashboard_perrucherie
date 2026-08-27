"""Meal prep module: dish library + multi-week meal plans on a days x slots grid."""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..database import PHOTOS_DIR, get_conn, rows_to_dicts
from ..photos import claim_photo, delete_photo, read_image_upload, save_tmp_photo
from ..vision import IdentificationError, identify_dish

router = APIRouter(prefix="/api", tags=["meals"])


class DishIn(BaseModel):
    name: str
    category: str = "diner"  # petit-dej / dejeuner / diner / snack
    prep_time_minutes: Optional[int] = None
    recipe_url: str = ""
    notes: str = ""
    ingredients: str = ""  # une ligne par ingrédient : "400 g de riz basmati"
    photo: Optional[str] = None  # token renvoyé par POST /dishes/identify


class MealPlanIn(BaseModel):
    name: str
    start_date: str  # ISO date, lundi de la semaine 1
    weeks: int = Field(default=1, ge=1, le=12)
    meals_per_day: int = Field(default=2, ge=1, le=6)
    active: bool = True


class EntryIn(BaseModel):
    week_index: int = Field(ge=1)
    day_index: int = Field(ge=1, le=7)
    slot_index: int = Field(ge=1)
    dish_id: Optional[int] = None  # None = case vide


# ---------- Dishes ----------

@router.get("/dishes")
def list_dishes():
    with get_conn() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM dishes ORDER BY name").fetchall())


@router.post("/dishes", status_code=201)
def create_dish(dish: DishIn):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO dishes (name, category, prep_time_minutes, recipe_url, notes, ingredients)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (dish.name, dish.category, dish.prep_time_minutes, dish.recipe_url, dish.notes, dish.ingredients),
        )
        dish_id = cur.lastrowid
        photo = claim_photo(dish.photo, "dish", dish_id)
        if photo:
            conn.execute("UPDATE dishes SET photo = ? WHERE id = ?", (photo, dish_id))
        return dict(conn.execute("SELECT * FROM dishes WHERE id = ?", (dish_id,)).fetchone())


@router.put("/dishes/{dish_id}")
def update_dish(dish_id: int, dish: DishIn):
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE dishes SET name=?, category=?, prep_time_minutes=?, recipe_url=?, notes=?, ingredients=?
               WHERE id=?""",
            (dish.name, dish.category, dish.prep_time_minutes, dish.recipe_url, dish.notes,
             dish.ingredients, dish_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Plat introuvable")
        photo = claim_photo(dish.photo, "dish", dish_id)
        if photo:
            conn.execute("UPDATE dishes SET photo = ? WHERE id = ?", (photo, dish_id))
        return dict(conn.execute("SELECT * FROM dishes WHERE id = ?", (dish_id,)).fetchone())


@router.delete("/dishes/{dish_id}", status_code=204)
def delete_dish(dish_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT photo FROM dishes WHERE id = ?", (dish_id,)).fetchone()
        cur = conn.execute("DELETE FROM dishes WHERE id = ?", (dish_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Plat introuvable")
    if row:
        delete_photo(row["photo"])


@router.post("/dishes/identify")
async def identify_dish_photo(file: UploadFile = File(...)):
    """Identify a dish from a photo via a vision LLM and pre-fill the dish form.

    Returns {name, category, prep_time_minutes, ingredients, notes} plus a
    `photo_token`: pass it back as `photo` when creating/updating the dish
    to attach the photo.
    """
    content, ext = await read_image_upload(file)

    try:
        suggestion = await identify_dish(content, file.content_type)
    except IdentificationError as e:
        raise HTTPException(502, str(e))

    token = uuid.uuid4().hex
    save_tmp_photo(content, ext, token)
    return {**suggestion, "photo_token": token}


@router.post("/dishes/photo")
async def upload_dish_photo(file: UploadFile = File(...)):
    """Attach a photo to a dish **without** any AI identification.

    Returns `{photo_token}`: pass it back as `photo` when creating/updating
    the dish. Use this when the user fills the dish form manually.
    """
    content, ext = await read_image_upload(file)
    token = uuid.uuid4().hex
    save_tmp_photo(content, ext, token)
    return {"photo_token": token}


@router.get("/dishes/{dish_id}/photo")
def dish_photo(dish_id: int):
    """Serve the dish's photo, if one was attached during identification."""
    with get_conn() as conn:
        row = conn.execute("SELECT photo FROM dishes WHERE id = ?", (dish_id,)).fetchone()
    if not row or not row["photo"]:
        raise HTTPException(404, "Pas de photo pour ce plat")
    path = PHOTOS_DIR / row["photo"]
    if not path.exists():
        raise HTTPException(404, "Fichier photo introuvable")
    return FileResponse(path)


# ---------- Meal plans ----------

@router.get("/meal-plans")
def list_meal_plans():
    with get_conn() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM meal_plans ORDER BY active DESC, start_date DESC").fetchall())


@router.post("/meal-plans", status_code=201)
def create_meal_plan(plan: MealPlanIn):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO meal_plans (name, start_date, weeks, meals_per_day, active) VALUES (?, ?, ?, ?, ?)",
            (plan.name, plan.start_date, plan.weeks, plan.meals_per_day, int(plan.active)),
        )
        return dict(conn.execute("SELECT * FROM meal_plans WHERE id = ?", (cur.lastrowid,)).fetchone())


@router.get("/meal-plans/{plan_id}")
def get_meal_plan(plan_id: int):
    """Plan with its full grid: entries include dish details. Empty cells simply have dish_id = null."""
    with get_conn() as conn:
        plan = conn.execute("SELECT * FROM meal_plans WHERE id = ?", (plan_id,)).fetchone()
        if not plan:
            raise HTTPException(404, "Plan introuvable")
        entries = rows_to_dicts(conn.execute(
            """SELECT e.*, d.name AS dish_name, d.category AS dish_category
               FROM meal_plan_entries e LEFT JOIN dishes d ON d.id = e.dish_id
               WHERE e.plan_id = ? ORDER BY e.week_index, e.day_index, e.slot_index""",
            (plan_id,),
        ).fetchall())
    result = dict(plan)
    result["entries"] = entries
    return result


@router.put("/meal-plans/{plan_id}")
def update_meal_plan(plan_id: int, plan: MealPlanIn):
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE meal_plans SET name=?, start_date=?, weeks=?, meals_per_day=?, active=? WHERE id=?",
            (plan.name, plan.start_date, plan.weeks, plan.meals_per_day, int(plan.active), plan_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Plan introuvable")
        return dict(conn.execute("SELECT * FROM meal_plans WHERE id = ?", (plan_id,)).fetchone())


@router.delete("/meal-plans/{plan_id}", status_code=204)
def delete_meal_plan(plan_id: int):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM meal_plans WHERE id = ?", (plan_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Plan introuvable")


@router.put("/meal-plans/{plan_id}/entry")
def set_entry(plan_id: int, entry: EntryIn):
    """Assign (or clear, with dish_id=null) a dish to a grid cell. Upsert semantics."""
    with get_conn() as conn:
        plan = conn.execute("SELECT * FROM meal_plans WHERE id = ?", (plan_id,)).fetchone()
        if not plan:
            raise HTTPException(404, "Plan introuvable")
        if entry.week_index > plan["weeks"] or entry.slot_index > plan["meals_per_day"]:
            raise HTTPException(400, "Case hors limites du plan")
        if entry.dish_id is not None and not conn.execute(
            "SELECT 1 FROM dishes WHERE id = ?", (entry.dish_id,)
        ).fetchone():
            raise HTTPException(404, "Plat introuvable")
        conn.execute(
            """INSERT INTO meal_plan_entries (plan_id, week_index, day_index, slot_index, dish_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (plan_id, week_index, day_index, slot_index)
               DO UPDATE SET dish_id = excluded.dish_id""",
            (plan_id, entry.week_index, entry.day_index, entry.slot_index, entry.dish_id),
        )
        row = conn.execute(
            """SELECT e.*, d.name AS dish_name FROM meal_plan_entries e
               LEFT JOIN dishes d ON d.id = e.dish_id
               WHERE e.plan_id=? AND e.week_index=? AND e.day_index=? AND e.slot_index=?""",
            (plan_id, entry.week_index, entry.day_index, entry.slot_index),
        ).fetchone()
        return dict(row)
