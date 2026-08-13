"""Dashboard de la Perrucherie — household dashboard (plants + meal prep).

Serves a REST API (fully documented at /docs, OpenAPI at /openapi.json)
and the static frontend. Designed to be reachable from any device on the LAN
and to be trivially driven by agents through the HTTP API.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import BASE_DIR, get_conn, init_db
from . import bot
from .routers import grocery, meals, plants

STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="Dashboard de la Perrucherie",
    description=(
        "Dashboard de gestion de la maison : suivi des plantes (arrosage, rappels) "
        "et planification des repas (meal prep). API REST complète, pensée pour "
        "être lue et écrite par des agents."
    ),
    version="0.1.0",
)

app.include_router(plants.router)
app.include_router(meals.router)
app.include_router(grocery.router)
app.include_router(bot.router)


@app.on_event("startup")
async def startup() -> None:
    init_db()
    bot.start()


@app.get("/api/summary", tags=["agents"])
def summary():
    """One-shot overview of the household — the ideal first endpoint for an agent.

    Returns due plants, today's meals (from the active plan), and counters.
    """
    due = plants.due_plants()

    today = date.today()
    meals_today = []
    with get_conn() as conn:
        plan = conn.execute("SELECT * FROM meal_plans WHERE active = 1 ORDER BY start_date DESC LIMIT 1").fetchone()
        total_plants = conn.execute("SELECT COUNT(*) c FROM plants").fetchone()["c"]
        total_dishes = conn.execute("SELECT COUNT(*) c FROM dishes").fetchone()["c"]
        total_grocery = conn.execute("SELECT COUNT(*) c FROM grocery_items").fetchone()["c"]
        total_fridge = conn.execute("SELECT COUNT(*) c FROM fridge_items").fetchone()["c"]

    if plan:
        start = date.fromisoformat(plan["start_date"])
        delta = (today - start).days
        if 0 <= delta < plan["weeks"] * 7:
            week_index = delta // 7 + 1
            day_index = delta % 7 + 1
            with get_conn() as conn:
                rows = conn.execute(
                    """SELECT e.slot_index, d.name AS dish, d.category
                       FROM meal_plan_entries e LEFT JOIN dishes d ON d.id = e.dish_id
                       WHERE e.plan_id = ? AND e.week_index = ? AND e.day_index = ?
                       ORDER BY e.slot_index""",
                    (plan["id"], week_index, day_index),
                ).fetchall()
            meals_today = [dict(r) for r in rows if r["dish"]]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "plants": {
            "total": total_plants,
            "due_count": len(due),
            "due": due,
        },
        "meals": {
            "active_plan": dict(plan) if plan else None,
            "today": meals_today,
            "dishes_in_library": total_dishes,
        },
        "grocery": {
            "items_on_list": total_grocery,
            "fridge_items": total_fridge,
        },
    }


# ---- Static frontend (must be mounted last) ----

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
