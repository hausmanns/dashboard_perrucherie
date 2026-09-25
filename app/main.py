"""Dashboard de la Perrucherie — household dashboard (plants, meals, wishlist, storage).

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
from .routers import grocery, meals, plants, shows, storage, wishlist

STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="Dashboard de la Perrucherie",
    description=(
        "Dashboard de gestion de la maison : suivi des plantes (arrosage, rappels), "
        "planification des repas (meal prep), listes d'envies/cadeaux, inventaire "
        "des cartons de rangement et suivi des séries/films (TMDb). API REST "
        "complète, pensée pour être lue et écrite par des agents."
    ),
    version="0.1.0",
)

app.include_router(plants.router)
app.include_router(meals.router)
app.include_router(grocery.router)
app.include_router(wishlist.router)
app.include_router(storage.router)
app.include_router(shows.router)
app.include_router(bot.router)


@app.on_event("startup")
async def startup() -> None:
    init_db()
    bot.start()


@app.get("/api/summary", tags=["agents"])
def summary():
    """One-shot overview of the household — the ideal first endpoint for an agent.

    Returns due plants, today's meals (from the active plan), upcoming birthdays
    and counters.
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
        total_wishes = conn.execute("SELECT COUNT(*) c FROM wishlist_items").fetchone()["c"]
        open_wishes = conn.execute(
            "SELECT COUNT(*) c FROM wishlist_items WHERE status = 'wanted'"
        ).fetchone()["c"]
        wish_people = conn.execute("SELECT COUNT(*) c FROM wishlist_people").fetchone()["c"]
        stored_items = conn.execute("SELECT COUNT(*) c FROM storage_items").fetchone()["c"]
        storage_boxes = conn.execute("SELECT COUNT(*) c FROM storage_boxes").fetchone()["c"]

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

    # Ce qui est sorti des cartons : le seul état « en cours » du rangement.
    items_out = storage.list_items(status="out")

    # Séries/films suivis, et ceux qui ont un épisode diffusé mais pas encore vu.
    all_shows = shows.list_shows()
    watching_shows = [s for s in all_shows if s["status"] == "en_cours"]
    new_episode_shows = [
        s for s in all_shows
        if s["status"] in ("a_voir", "en_cours") and s["unwatched_aired_episodes"]
    ]

    # Anniversaires dans les 60 jours — le bon moment pour piocher dans une liste d'envies.
    upcoming_birthdays = [
        {"name": p["name"], "emoji": p["emoji"], "days_until_birthday": p["days_until_birthday"]}
        for p in sorted(
            (p for p in wishlist.list_people() if p["days_until_birthday"] is not None),
            key=lambda p: p["days_until_birthday"],
        )
        if p["days_until_birthday"] <= 60
    ]

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
        "storage": {
            "boxes": storage_boxes,
            "items": stored_items,
            "out_count": len(items_out),
            "out": items_out,                    # ce qui est sorti des cartons
        },
        "wishlist": {
            "people": wish_people,
            "total_wishes": total_wishes,
            "open_wishes": open_wishes,          # encore à offrir
            "upcoming_birthdays": upcoming_birthdays,
        },
        "shows": {
            "total": len(all_shows),
            "watching": len(watching_shows),
            "new_episodes_count": sum(s["unwatched_aired_episodes"] for s in new_episode_shows),
            "new_episodes": new_episode_shows,   # diffusés, pas encore vus
        },
    }


# ---- Static frontend (must be mounted last) ----

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
