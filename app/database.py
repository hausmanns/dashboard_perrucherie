"""SQLite database layer for the Dashboard de la Perrucherie.

The database is a single file at data/dashboard.db, committed to the repo,
so a `git pull` on another machine brings the data along.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "dashboard.db"
PHOTOS_DIR = DATA_DIR / "photos"  # plant photos (committed like the DB)

SCHEMA = """
CREATE TABLE IF NOT EXISTS plants (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    name                    TEXT NOT NULL,
    species                 TEXT DEFAULT '',
    location                TEXT DEFAULT '',
    watering_frequency_days INTEGER NOT NULL DEFAULT 7,
    last_watered_at         TEXT,               -- ISO datetime, NULL = jamais arrosée
    notes                   TEXT DEFAULT '',
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS watering_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id   INTEGER NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
    watered_at TEXT NOT NULL DEFAULT (datetime('now')),
    note       TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dishes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    category          TEXT DEFAULT 'diner',     -- petit-dej / dejeuner / diner / snack
    prep_time_minutes INTEGER,
    recipe_url        TEXT DEFAULT '',
    notes             TEXT DEFAULT '',
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meal_plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    start_date    TEXT NOT NULL,                -- ISO date (lundi de la semaine 1)
    weeks         INTEGER NOT NULL DEFAULT 1,
    meals_per_day INTEGER NOT NULL DEFAULT 2,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meal_plan_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id    INTEGER NOT NULL REFERENCES meal_plans(id) ON DELETE CASCADE,
    week_index INTEGER NOT NULL,                -- 1..weeks
    day_index  INTEGER NOT NULL,                -- 1..7 (1 = lundi)
    slot_index INTEGER NOT NULL,                -- 1..meals_per_day
    dish_id    INTEGER REFERENCES dishes(id) ON DELETE SET NULL,
    UNIQUE (plan_id, week_index, day_index, slot_index)
);

CREATE TABLE IF NOT EXISTS grocery_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    quantity   TEXT DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'manual',  -- manual | plan
    plan_id    INTEGER REFERENCES meal_plans(id) ON DELETE CASCADE,
    dishes     TEXT DEFAULT '',                 -- noms des plats concernés (items 'plan')
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fridge_items (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    quantity TEXT DEFAULT '',
    source   TEXT NOT NULL DEFAULT 'manual',    -- manual | grocery (acheté depuis la liste)
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    PHOTOS_DIR.mkdir(exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn) -> None:
    """Additive-only migrations (the DB ships with user data — never destructive)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(plants)")}
    if "photo" not in cols:
        conn.execute("ALTER TABLE plants ADD COLUMN photo TEXT")  # filename in data/photos/

    dish_cols = {r["name"] for r in conn.execute("PRAGMA table_info(dishes)")}
    if "photo" not in dish_cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN photo TEXT")  # filename in data/photos/
    if "ingredients" not in dish_cols:
        # Une ligne par ingrédient, quantité incluse : "400 g de riz basmati"
        conn.execute("ALTER TABLE dishes ADD COLUMN ingredients TEXT DEFAULT ''")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]
