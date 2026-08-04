# AGENTS.md — Dashboard de la Perrucherie

Guidance for AI agents working on or with this project.

## What this is

A self-hosted household dashboard ("Dashboard de la Perrucherie") running on a
local network. Current features:

1. **Plants** — track household plants, watering frequency, due/overdue
   detection, watering history, browser notifications.
2. **Meal prep** — a dish library + multi-week meal plans on a
   `weeks × 7 days × meals-per-day` grid.

The architecture is deliberately **agent-first**: everything the UI can do is
exposed through a plain REST/JSON API, so agents can read and write all data
without touching the frontend or the database directly.

## Stack

- **Backend**: Python 3.10+, FastAPI, SQLite (stdlib `sqlite3`, no ORM)
- **Frontend**: vanilla HTML/CSS/JS in `static/` (no build step)
- **Server**: uvicorn, bound to `0.0.0.0` so LAN devices can reach it
- **Database**: single file `data/dashboard.db`, **committed to the repo** so a
  `git pull` on another machine brings the data along. Never gitignore it.

## Run

```bash
./run.sh            # creates .venv, installs deps, serves on 0.0.0.0:8000
PORT=9000 ./run.sh  # custom port
```

Then open `http://<host-ip>:8000` from any device on the network.

## Project layout

```
app/
  main.py            FastAPI app, /api/summary, static serving
  database.py        SQLite connection + schema (init_db)
  routers/
    plants.py        /api/plants*  (CRUD, /water, /due, history)
    meals.py         /api/dishes*, /api/meal-plans* (grid entries)
static/
  index.html         SPA shell (3 views: Accueil / Plantes / Repas)
  style.css          theme (dark botanical)
  app.js             all frontend logic, fetch-based
data/dashboard.db    SQLite data (committed, portable)
run.sh               one-command launcher
```

## Agent integration (reading/writing data)

Always prefer the HTTP API over direct DB access. Base URL when running
locally: `http://localhost:8000`.

- **Interactive API docs**: `GET /docs` (Swagger UI)
- **Machine-readable spec**: `GET /openapi.json`
- **Best starting point**: `GET /api/summary` — one call returning due plants,
  today's meals from the active plan, and counters.

### Key endpoints

| Purpose | Method & path |
|---|---|
| Household overview | `GET /api/summary` |
| Plants needing water | `GET /api/plants/due` |
| List / create plants | `GET` / `POST /api/plants` |
| Get / update / delete plant | `GET` / `PUT` / `DELETE /api/plants/{id}` |
| Record a watering | `POST /api/plants/{id}/water` body `{}` or `{"watered_at": ..., "note": ...}` |
| Dish library | `GET` / `POST /api/dishes`, `PUT` / `DELETE /api/dishes/{id}` |
| Meal plans | `GET` / `POST /api/meal-plans`, `PUT` / `DELETE /api/meal-plans/{id}` |
| Full plan grid | `GET /api/meal-plans/{id}` |
| Assign a dish to a cell | `PUT /api/meal-plans/{id}/entry` body `{"week_index", "day_index", "slot_index", "dish_id"}` (`dish_id: null` clears) |

### Conventions agents must know

- **Watering status is computed**, not stored: `ok | due_soon | overdue |
  never_watered`, from `last_watered_at + watering_frequency_days`.
  `due_soon` = within 24 h of the next due date.
- All datetimes are **ISO 8601 strings, server-local time**.
- Meal-plan grid coordinates are 1-indexed: `week_index` 1..weeks,
  `day_index` 1..7 (1 = **lundi**), `slot_index` 1..meals_per_day.
- UI copy is in **French**; code, identifiers and docs in English.
- When adding an entry, use upsert semantics (`PUT .../entry`) — don't insert
  duplicates; the DB enforces uniqueness per cell.

## Coding conventions

- Keep it simple: no ORM, no build step, no JS framework unless explicitly
  requested.
- Backend changes: add/extend endpoints in the relevant router, keep Pydantic
  schemas for all inputs, update the schema in `database.py` with
  `CREATE TABLE IF NOT EXISTS` / additive migrations only (never destructive —
  the DB ships with user data).
- Any new feature must be exposed via the API **and** documented in this file's
  endpoint table.
- After backend changes, verify with `./run.sh` and a quick `curl` against the
  touched endpoints.

## Portability rules

- No machine-specific paths, no hardcoded IPs.
- Everything needed at runtime must be in the repo or installable from
  `requirements.txt` (`.venv` is created by `run.sh`).
- The SQLite DB travels with the repo — be careful with schema changes; they
  must be backward-compatible so a `git pull` on another machine keeps working.
