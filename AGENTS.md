# AGENTS.md — Dashboard de la Perrucherie

Guidance for AI agents working on or with this project.

## What this is

A self-hosted household dashboard ("Dashboard de la Perrucherie") running on a
local network. Current features:

1. **Plants** — track household plants, watering frequency, due/overdue
   detection, watering history, browser notifications, and **AI plant
   identification**: snap a photo (phone camera or upload) and a vision LLM
   (via OpenRouter) pre-fills the new-plant form; the photo is saved and
   shown on the plant card.
2. **Meal prep** — a dish library + multi-week meal plans on a
   `weeks × 7 days × meals-per-day` grid. Dishes can carry a **photo** and an
   **ingredient list**, both pre-fillable by an agentic **AI dish
   identification** button (snap a photo of the dish; the vision LLM fills
   name, category, prep time, ingredients, notes and attaches the photo).
3. **Groceries & fridge** — a grocery list with two kinds of items:
   **manual** items (plan-independent list) and **generated** items built
   from the ingredients of every dish used in a meal plan, or from a single
   dish on demand (duplicates merged, quantities summed). Marking an item as
   bought moves it into the **fridge** inventory.
4. **Telegram bot** — general-purpose notification channel. First feature:
   daily **watering reminders** at 09:00 & 21:00 (server-local, `TELEGRAM_TZ`)
   sent to a Telegram chat, plus an interactive `/plantes` command. Setup doc:
   `TELEGRAM_BOT.md`. No LLM involved — pure scheduled checks.

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

With Docker (preferred — no local Python needed):

```bash
docker compose up -d --build     # builds image, serves on 0.0.0.0:8000
PORT=9000 docker compose up -d   # custom host port
```

Without Docker:

```bash
./run.sh            # creates .venv, installs deps, serves on 0.0.0.0:8000
PORT=9000 ./run.sh  # custom port
```

Then open `http://<host-ip>:8000` from any device on the network.

### Docker specifics

- Single service, single port (`${PORT:-8000}:8000`). No other configuration.
- `./data` is **bind-mounted** into the container at `/app/data`: the SQLite
  DB stays a normal file in the repo checkout and keeps travelling with git.
  Never bake the DB into the image (`data/` is in `.dockerignore`).
- `restart: unless-stopped` is set, so the dashboard survives host reboots.
- After a `git pull`: `docker compose up -d --build` to apply code changes.

## Project layout

```
app/
  main.py            FastAPI app, /api/summary, static serving
  config.py          env/.env config (OPENROUTER_API_KEY, OPENROUTER_MODEL,
                     TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_TZ)
  vision.py          OpenRouter vision calls → plant & dish identification (strict JSON)
  photos.py          shared photo upload/claim helpers (plants + dishes)
  database.py        SQLite connection + schema (init_db, additive migrations)
  telegram.py        generic Telegram module (send, commands, polling)
  bot.py             feature wiring (watering reminders + /plantes) + APScheduler cron
  routers/
    plants.py        /api/plants*  (CRUD, /water, /due, /identify, photo, history)
    meals.py         /api/dishes*, /api/meal-plans* (grid entries, dish photo + /identify)
    grocery.py       /api/grocery*, /api/fridge* (list, /from-plan, /buy → fridge)
static/
  index.html         SPA shell (4 views: Accueil / Plantes / Repas / Courses)
  style.css          theme (dark botanical)
  app.js             all frontend logic, fetch-based
data/dashboard.db    SQLite data (committed, portable)
data/photos/         plant photos from identification (committed like the DB)
.env                 OpenRouter API key (gitignored — see .env.example)
Dockerfile           all-in-one image (python:3.12-slim + uvicorn)
docker-compose.yml   single service, port mapping, ./data bind mount, env passthrough
.dockerignore        keeps .venv/data/docs/.env out of the image
run.sh               one-command launcher (no-Docker fallback)
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
| Water all plants at once (shared timestamp) | `POST /api/plants/water-all` body `{}` or `{"watered_at": ..., "note": ...}` |
| Identify plant from photo | `POST /api/plants/identify` multipart `file` (JPEG/PNG/WebP, ≤ 8 MB) → `{name, species, watering_frequency_days, notes, photo_token}` |
| Get a plant's photo | `GET /api/plants/{id}/photo` (404 if none) |
| Dish library | `GET` / `POST /api/dishes`, `PUT` / `DELETE /api/dishes/{id}` |
| Identify dish from photo | `POST /api/dishes/identify` multipart `file` → `{name, category, prep_time_minutes, ingredients, notes, photo_token}` |
| Get a dish's photo | `GET /api/dishes/{id}/photo` (404 if none) |
| Meal plans | `GET` / `POST /api/meal-plans`, `PUT` / `DELETE /api/meal-plans/{id}` |
| Full plan grid | `GET /api/meal-plans/{id}` |
| Assign a dish to a cell | `PUT /api/meal-plans/{id}/entry` body `{"week_index", "day_index", "slot_index", "dish_id"}` (`dish_id: null` clears) |
| Grocery list | `GET` / `POST /api/grocery`, `PUT` / `DELETE /api/grocery/{id}` |
| Generate list from a meal plan | `POST /api/grocery/from-plan` body `{"plan_id"}` (replaces that plan's items, keeps manual ones) |
| Add a dish's ingredients to the list | `POST /api/grocery/from-dish` body `{"dish_id"}` (replaces that dish's items, keeps manual + plan ones) |
| Mark item as bought → fridge | `POST /api/grocery/{id}/buy` |
| Fridge inventory | `GET` / `POST /api/fridge`, `PUT` / `DELETE /api/fridge/{id}` |
| Trigger Telegram watering check | `POST /api/bot/watering-check` (sends the summary to the chat now) |

### Conventions agents must know

- **Watering status is computed**, not stored: `ok | due_soon | overdue |
  never_watered`, from `last_watered_at + watering_frequency_days`.
  `due_soon` = within 24 h of the next due date.
- All datetimes are **ISO 8601 strings, server-local time**.
- **Photo identification flow**: `POST /api/plants/identify` saves the photo as
  `data/photos/tmp_<token>.<ext>` and returns a `photo_token`; pass it as
  `photo` in `POST`/`PUT /api/plants` to attach it (renamed to
  `plant_<id>.<ext>`). Requires `OPENROUTER_API_KEY` (env or `.env`);
  the model is configurable via `OPENROUTER_MODEL`
  (default `google/gemini-2.5-flash`). Photos live in `data/photos/` and are
  committed like the DB; deleting a plant deletes its photo. The same flow
  applies to dishes (`POST /api/dishes/identify`, `photo` field on the dish,
  `dish_<id>.<ext>`).
- **Dish ingredients** are stored as a plain text field, one
  `"quantity + name"` line per ingredient (e.g. `400 g de riz basmati`) —
  this is what the dish identification returns and what
  `POST /api/grocery/from-plan` parses. Keep that format when writing
  ingredients.
- **Grocery items** have a `source`: `manual` (plan-independent), `plan`
  (regenerated from a meal plan's dish ingredients; `plan_id` is set and
  `dishes` lists which dishes need the item) or `dish` (regenerated from a
  single dish's ingredients; `dish_id` is set and `dishes` names the dish).
  `from-plan` only replaces items of its own plan and `from-dish` only those
  of its own dish — manual items are never touched. `POST
  /api/grocery/{id}/buy` deletes the grocery item and inserts it into
  `fridge_items` (source `grocery`).
- Meal-plan grid coordinates are 1-indexed: `week_index` 1..weeks,
  `day_index` 1..7 (1 = **lundi**), `slot_index` 1..meals_per_day.
- UI copy is in **French**; code, identifiers and docs in English.
- When adding an entry, use upsert semantics (`PUT .../entry`) — don't insert
  duplicates; the DB enforces uniqueness per cell.
- **Telegram bot**: a generic module (`app/telegram.py`) + feature wiring
  (`app/bot.py`). Features register cron jobs with `bot.add_cron_job(func,
  hour, minute)` (daily, server-local time or `TELEGRAM_TZ`) and commands with
  `telegram.register_command("/cmd", handler)`. The scheduler lives inside the
  app process, so it starts/stops with the container. Setup: `TELEGRAM_BOT.md`.

## Coding conventions

- Keep it simple: no ORM, no build step, no JS framework unless explicitly
  requested.
- Backend changes: add/extend endpoints in the relevant router, keep Pydantic
  schemas for all inputs, update the schema in `database.py` with
  `CREATE TABLE IF NOT EXISTS` / additive migrations only (never destructive —
  the DB ships with user data).
- Any new feature must be exposed via the API **and** documented in this file's
  endpoint table.
- **After implementing a change, always relaunch the Docker stack** so the new
  code is actually live: `docker compose up -d --build` (the container named
  `perrucherie` is the production instance on this machine — local `./run.sh`
  testing alone is not enough). Then verify with a quick `curl` against
  `http://localhost:8000` on the touched endpoints.
  - macOS gotcha: if the build fails with `docker-credential-osxkeychain:
    executable file not found`, prepend Docker's bin dir to PATH first:
    `export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"`.

## Portability rules

- No machine-specific paths, no hardcoded IPs.
- Everything needed at runtime must be in the repo or installable from
  `requirements.txt` (Docker build) or via `.venv` (`run.sh`).
- The SQLite DB travels with the repo — be careful with schema changes; they
  must be backward-compatible so a `git pull` on another machine keeps working.
- The DB must never be baked into the Docker image; it is bind-mounted from
  `./data` at runtime.
