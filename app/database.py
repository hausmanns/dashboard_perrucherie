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
    source     TEXT NOT NULL DEFAULT 'manual',  -- manual | plan | dish
    plan_id    INTEGER REFERENCES meal_plans(id) ON DELETE CASCADE,
    dish_id    INTEGER REFERENCES dishes(id) ON DELETE CASCADE,
    dishes     TEXT DEFAULT '',                 -- noms des plats concernés (items 'plan'/'dish')
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wishlist_people (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    emoji      TEXT DEFAULT '🎁',
    birthday   TEXT,                            -- ISO date (sert au compte à rebours cadeau)
    notes      TEXT DEFAULT '',                 -- tailles, goûts, allergies, ce qu'il ne faut pas offrir
    telegram_user_id INTEGER,                   -- compte Telegram lié (/moi <prénom>)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wishlist_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL REFERENCES wishlist_people(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    category    TEXT NOT NULL DEFAULT 'autre',  -- tech / maison / vetements / livres / sport / loisirs / beaute / cuisine / voyage / experience / autre (slugs sans accent)
    price       REAL,                           -- prix estimé
    currency    TEXT NOT NULL DEFAULT 'CHF',
    url         TEXT DEFAULT '',                -- lien produit / boutique en ligne
    shop        TEXT DEFAULT '',                -- où l'acheter (enseigne, ville)
    size        TEXT DEFAULT '',                -- taille (vêtements, chaussures)
    color       TEXT DEFAULT '',                -- couleur / variante souhaitée
    quantity    INTEGER NOT NULL DEFAULT 1,
    priority    INTEGER NOT NULL DEFAULT 2,     -- 1 = un jour, 2 = ça me plairait, 3 = j'en rêve
    occasion    TEXT DEFAULT '',                -- Noël, anniversaire, sans occasion…
    target_date TEXT,                           -- ISO date : à offrir avant cette date
    status      TEXT NOT NULL DEFAULT 'wanted', -- wanted | reserved | bought | received | archived
    reserved_by TEXT DEFAULT '',                -- qui s'en occupe (masqué pour le/la destinataire)
    reserved_at TEXT,
    bought_by   TEXT DEFAULT '',
    bought_at   TEXT,
    received_at TEXT,
    photo       TEXT,                           -- nom de fichier dans data/photos/
    notes       TEXT DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_wishlist_items_person ON wishlist_items(person_id);

CREATE TABLE IF NOT EXISTS storage_boxes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE,            -- identifiant écrit sur le carton ("0", "12", "A3")
    name       TEXT NOT NULL DEFAULT '',        -- thème du carton ("Nautique", "Electroniques")
    kind       TEXT DEFAULT '',                 -- type de contenant ("Carton Bananas", "Carton médium")
    location   TEXT DEFAULT '',                 -- où se trouve le carton (cave, grenier…)
    notes      TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS storage_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    box_id      INTEGER REFERENCES storage_boxes(id) ON DELETE SET NULL,  -- NULL = objet sans carton
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    owner       TEXT NOT NULL DEFAULT '',       -- propriétaire(s), séparés par des virgules : "Seb,Lea"
    category    TEXT DEFAULT '',                -- collection transverse ("Nautique"), souvent = box.name
    quantity    INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'stored', -- stored | out
    out_since   TEXT,                           -- ISO datetime de la sortie (NULL si rangé)
    out_note    TEXT DEFAULT '',                -- où / chez qui ("Prêté à Tom", "chambre")
    notes       TEXT DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_storage_items_box ON storage_items(box_id);
CREATE INDEX IF NOT EXISTS idx_storage_items_status ON storage_items(status);

CREATE TABLE IF NOT EXISTS storage_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES storage_items(id) ON DELETE CASCADE,
    action  TEXT NOT NULL,                      -- created | out | in | moved
    at      TEXT NOT NULL DEFAULT (datetime('now')),
    note    TEXT DEFAULT '',
    box_id  INTEGER                             -- carton concerné (actions 'in' / 'moved')
);

CREATE INDEX IF NOT EXISTS idx_storage_events_item ON storage_events(item_id);

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

    people_cols = {r["name"] for r in conn.execute("PRAGMA table_info(wishlist_people)")}
    if "telegram_user_id" not in people_cols:
        # Lie un compte Telegram a une personne : le bot sait alors qui dit « je veux… ».
        conn.execute("ALTER TABLE wishlist_people ADD COLUMN telegram_user_id INTEGER")

    grocery_cols = {r["name"] for r in conn.execute("PRAGMA table_info(grocery_items)")}
    if "dish_id" not in grocery_cols:
        conn.execute("ALTER TABLE grocery_items ADD COLUMN dish_id INTEGER REFERENCES dishes(id) ON DELETE CASCADE")


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
