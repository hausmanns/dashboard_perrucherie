"""Plants module: tracking, watering schedules, due/overdue detection, history."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..database import get_conn, rows_to_dicts

router = APIRouter(prefix="/api/plants", tags=["plants"])


class PlantIn(BaseModel):
    name: str
    species: str = ""
    location: str = ""
    watering_frequency_days: int = Field(default=7, ge=1)
    last_watered_at: Optional[str] = None  # ISO datetime; None = jamais arrosée
    notes: str = ""


class WaterEvent(BaseModel):
    watered_at: Optional[str] = None  # default: now
    note: str = ""


def _enrich(plant: dict) -> dict:
    """Add computed watering status fields for UI and agents."""
    now = datetime.now()
    freq = plant["watering_frequency_days"]
    last = plant.get("last_watered_at")
    if last:
        last_dt = datetime.fromisoformat(last)
        next_due = last_dt + timedelta(days=freq)
        days_left = (next_due - now).total_seconds() / 86400
        plant["next_watering_at"] = next_due.isoformat(timespec="seconds")
        plant["days_until_watering"] = round(days_left, 2)
        if days_left < 0:
            plant["status"] = "overdue"
        elif days_left <= 1:
            plant["status"] = "due_soon"
        else:
            plant["status"] = "ok"
    else:
        plant["next_watering_at"] = None
        plant["days_until_watering"] = None
        plant["status"] = "never_watered"
    return plant


@router.get("")
def list_plants(status: Optional[str] = None):
    """List all plants, enriched with watering status. Filter with ?status=overdue|due_soon|ok|never_watered."""
    with get_conn() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM plants ORDER BY name").fetchall())
    plants = [_enrich(r) for r in rows]
    if status:
        plants = [p for p in plants if p["status"] == status]
    return plants


@router.post("", status_code=201)
def create_plant(plant: PlantIn):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO plants (name, species, location, watering_frequency_days, last_watered_at, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (plant.name, plant.species, plant.location, plant.watering_frequency_days,
             plant.last_watered_at, plant.notes),
        )
        row = conn.execute("SELECT * FROM plants WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _enrich(dict(row))


@router.get("/due")
def due_plants():
    """Plants needing attention: overdue, due within 24h, or never watered. Ideal endpoint for agents/notifications."""
    return [p for p in list_plants() if p["status"] in ("overdue", "due_soon", "never_watered")]


@router.get("/{plant_id}")
def get_plant(plant_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM plants WHERE id = ?", (plant_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Plante introuvable")
        history = rows_to_dicts(conn.execute(
            "SELECT * FROM watering_events WHERE plant_id = ? ORDER BY watered_at DESC LIMIT 50",
            (plant_id,),
        ).fetchall())
    plant = _enrich(dict(row))
    plant["watering_history"] = history
    return plant


@router.put("/{plant_id}")
def update_plant(plant_id: int, plant: PlantIn):
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE plants SET name=?, species=?, location=?, watering_frequency_days=?,
               last_watered_at=?, notes=? WHERE id=?""",
            (plant.name, plant.species, plant.location, plant.watering_frequency_days,
             plant.last_watered_at, plant.notes, plant_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Plante introuvable")
        row = conn.execute("SELECT * FROM plants WHERE id = ?", (plant_id,)).fetchone()
    return _enrich(dict(row))


@router.delete("/{plant_id}", status_code=204)
def delete_plant(plant_id: int):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM plants WHERE id = ?", (plant_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Plante introuvable")


@router.post("/{plant_id}/water", status_code=201)
def water_plant(plant_id: int, event: WaterEvent):
    """Record a watering; updates last_watered_at and appends to history."""
    watered_at = event.watered_at or datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM plants WHERE id = ?", (plant_id,)).fetchone():
            raise HTTPException(404, "Plante introuvable")
        conn.execute(
            "INSERT INTO watering_events (plant_id, watered_at, note) VALUES (?, ?, ?)",
            (plant_id, watered_at, event.note),
        )
        conn.execute("UPDATE plants SET last_watered_at = ? WHERE id = ?", (watered_at, plant_id))
        row = conn.execute("SELECT * FROM plants WHERE id = ?", (plant_id,)).fetchone()
    return _enrich(dict(row))
