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
   **ingredient list**. The dish form asks how to fill it in: **manual entry
   (the default)** — you type the fields and the photo stays a plain photo —
   or **AI dish identification** (snap a photo of the dish; the vision LLM
   fills name, category, prep time, ingredients, notes and attaches the photo).
3. **Groceries & fridge** — a grocery list with two kinds of items:
   **manual** items (plan-independent list) and **generated** items built
   from the ingredients of every dish used in a meal plan, or from a single
   dish on demand (duplicates merged, quantities summed). Marking an item as
   bought moves it into the **fridge** inventory.
4. **Wishlist ("Envies")** — one list of material wishes per person of the
   household, so *someone else* can browse it and actually buy the present.
   Each wish carries what a gifter needs: price + currency, shop, product
   link, size, colour, quantity, priority (1-3), occasion, deadline, photo
   (plain upload **or** AI object identification). Gifters **reserve** a wish
   (`/reserve` → `/bought` → `/received`) so two people never buy the same
   thing, and every read endpoint has a **spoiler-free mode**
   (`?hide_reservations=true`) that hides all of that from the person the
   list belongs to.
5. **Storage ("Rangement")** — the inventory of what is packed away, which
   replaces the old cartons spreadsheet. **Boxes** carry the code written on
   them plus a theme, a container type and a location; **items** carry an
   owner list (`Seb`, `Lea`, `Seb,Lea`), a cross-box **collection**, and a
   status. Taking something out or putting it back is one call that stamps
   the date itself and appends to `storage_events`, so the history exists
   without anyone maintaining it. The search endpoint is accent- and
   case-insensitive and spans the item, its box and its owners — it is how
   "where is my X?" gets answered.
6. **Show tracker ("Séries")** — what the household watches, is watching, and
   what's next, backed by **TMDb** (search, trending, covers, synopsis,
   episode-level air dates — `app/tmdb.py`). Tracking is **household-wide**
   (one shared status per show/movie, like plants/storage — not per person
   like the wishlist). A tracked TV show gets a full **episode checklist**
   per season; a movie has a single watched/unwatched toggle. Each show
   carries our own **status** (`a_voir` → `en_cours` → `termine`, or
   `abandonne`), a **1-5 star rating** and free-text **notes** — TMDb never
   sees any of that, it is read-only source data. The **Calendrier** tab
   lists upcoming episodes of followed shows for the next 30 days; the
   **Découvrir** tab surfaces TMDb's trending titles and search. Marking an
   episode watched (or a whole season via « ✅ Tout marquer ») auto-promotes
   a show from `a_voir` to `en_cours`, and auto-closes it to `termine` once
   TMDb says it's over and every aired episode is watched.
7. **Telegram bot** — general-purpose notification channel. Daily **watering
   reminders** at 09:00 & 21:00 (server-local, `TELEGRAM_TZ`), a daily
   **show sync + new-episode digest** at 08:00 (re-pulls followed shows from
   TMDb, announces — once — any episode that just aired and isn't marked
   watched yet), the read-only `/plantes`, `/envies`, `/cartons`, `/sortis`,
   `/ou` and `/series` commands, and two **plain-French write paths**
   running through an LLM (`app/nlu.py`): **adding a wish** (« /envie un
   casque Sony vers 350.- pour Lea ») and **updating the storage inventory**
   (« j'ai sorti le wetsuit du carton 2, prêté à Tom »). Both **ask for
   whatever is missing or ambiguous** — who it is for, the price, *which*
   pair of goggles — one question at a time. Setup doc: `TELEGRAM_BOT.md`.

The architecture is deliberately **agent-first**: everything the UI can do is
exposed through a plain REST/JSON API, so agents can read and write all data
without touching the frontend or the database directly.

## Stack

- **Backend**: Python 3.10+, FastAPI, SQLite (stdlib `sqlite3`, no ORM)
- **External data**: [TMDb](https://www.themoviedb.org/) (`app/tmdb.py`) for
  the show tracker — search, trending, covers, synopsis, episode air dates
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
  openrouter.py      shared OpenRouter chat plumbing (strict-JSON, sync + async)
  vision.py          photo → plant, dish & wish identification
  nlu.py             free-text Telegram message → structured wish (+ follow-up answers)
  photos.py          shared photo upload/claim helpers (plants + dishes + wishes)
  tmdb.py            TMDb client (search, trending, tv/movie/season details)
  database.py        SQLite connection + schema (init_db, additive migrations)
  telegram.py        generic Telegram module (send, commands, polling)
  bot.py             feature wiring (watering reminders, /plantes, /envies, /envie,
                     /moi, /cartons, /ou, /sorti, /range, /series, natural-language
                     wish capture + storage updates) + APScheduler cron
  nlu.py             free-text Telegram message → structured wish / storage action
  routers/
    plants.py        /api/plants*  (CRUD, /water, /due, /identify, photo, history)
    meals.py         /api/dishes*, /api/meal-plans* (grid entries, dish /photo + /identify)
    grocery.py       /api/grocery*, /api/fridge* (list, /from-plan, /buy → fridge)
    wishlist.py      /api/wishlist* (people, items, /reserve → /bought → /received,
                     name resolution, Telegram account linking)
    storage.py       /api/storage* (boxes, items, search, /out → /in, /move, history)
    shows.py         /api/shows* (search, trending, calendar, CRUD, episode/season
                     watched, movie watched, refresh from TMDb)
static/
  index.html         SPA shell (7 views: Accueil / Plantes / Repas / Courses /
                     Envies / Rangement / Séries)
                     Chaque carte d'accueil branchée au bot porte un « (i) »
                     qui explique ses commandes Telegram (HOME_HELP dans app.js)
  style.css          theme (dark botanical)
  app.js             all frontend logic, fetch-based
data/dashboard.db    SQLite data (committed, portable)
data/photos/         plant, dish & wish photos (committed like the DB)
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
  today's meals from the active plan, upcoming birthdays and counters.

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
| Identify dish from photo (AI) | `POST /api/dishes/identify` multipart `file` → `{name, category, prep_time_minutes, ingredients, notes, photo_token}` |
| Attach a dish photo without AI | `POST /api/dishes/photo` multipart `file` (JPEG/PNG/WebP, ≤ 8 MB) → `{photo_token}` |
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
| What the bot can currently do | `GET /api/bot/status` → `{telegram_configured, ai_configured}` |
| **Wishlist** — who could I offer what? | `GET /api/wishlist/overview` (per person: counters, remaining budget, top 3 ideas, birthday countdown) |
| People with a wishlist | `GET` / `POST /api/wishlist/people`, `PUT` / `DELETE /api/wishlist/people/{id}` |
| Resolve a first name (accent/case-insensitive) | `GET /api/wishlist/people/resolve?name=seb` → `{matches, resolved}` |
| Link a Telegram account to a person | `POST /api/wishlist/people/{id}/telegram` body `{"telegram_user_id": 42}` (`null` unlinks) |
| List wishes | `GET /api/wishlist/items?person_id=&status=&category=&occasion=` |
| Create / read / update / delete a wish | `POST /api/wishlist/items`, `GET` / `PUT` / `DELETE /api/wishlist/items/{id}` |
| Identify a wished-for object from a photo (AI) | `POST /api/wishlist/identify` multipart `file` → `{name, category, price, shop, color, description, photo_token}` |
| Attach a wish photo without AI | `POST /api/wishlist/photo` multipart `file` → `{photo_token}` |
| Get a wish's photo | `GET /api/wishlist/items/{id}/photo` (404 if none) |
| Claim a wish (« je m'en occupe ») | `POST /api/wishlist/items/{id}/reserve` body `{"by": "Seb"}` — **409** if already taken |
| Release it again | `POST /api/wishlist/items/{id}/unreserve` |
| Present bought / given | `POST /api/wishlist/items/{id}/bought` body `{"by": ...}` (defaults to the reserver) → `POST .../received` |
| Spoiler-free read (owner's view) | add `?hide_reservations=true` to `/overview`, `/items`, `/items/{id}` |
| **Storage** — where is my X? | `GET /api/storage/items?q=kite` (accent-insensitive, spans item + box + owner) |
| Storage overview + filter values | `GET /api/storage/summary` (counters, what is out, owners, collections) |
| List / create boxes | `GET` / `POST /api/storage/boxes` |
| Get box with its contents | `GET /api/storage/boxes/{id}` |
| Update / delete a box | `PUT` / `DELETE /api/storage/boxes/{id}` (items survive, `box_id` → `NULL`) |
| Filter items | `GET /api/storage/items?owner=Seb&status=out&category=Nautique&box_id=3` |
| Create / update / delete an item | `POST /api/storage/items`, `PUT` / `DELETE /api/storage/items/{id}` |
| Take an item out of its box | `POST /api/storage/items/{id}/out` body `{}` or `{"note": "prêté à Tom", "at": ...}` |
| Put it back | `POST /api/storage/items/{id}/in` body `{}` or `{"box_id": ...}` to land it elsewhere |
| Move an item to another box | `POST /api/storage/items/{id}/move` body `{"box_id": 4}` (`null` → no box) |
| An item's out/in history | `GET /api/storage/items/{id}/history` |
| **Show tracker** — is TMDb reachable? | `GET /api/shows/status` → `{tmdb_configured}` |
| Search TMDb (to add something) | `GET /api/shows/search?q=&media_type=` |
| What's trending on TMDb | `GET /api/shows/trending?window=day\|week&media_type=all\|movie\|tv` |
| Tracked shows/movies | `GET /api/shows?status=&media_type=` |
| Add something to the tracker | `POST /api/shows` body `{"tmdb_id", "media_type", "status"?}` — 409 if already tracked |
| Upcoming episodes (followed shows) | `GET /api/shows/calendar?days=30` |
| Viewing stats (status/genre mix, watch time, monthly activity, ratings) | `GET /api/shows/stats` |
| Get one tracked show/movie (+ episodes) | `GET /api/shows/{id}` |
| Update status/rating/notes | `PUT /api/shows/{id}` body `{"status"?, "rating"?, "notes"?}` — only given fields change; `"rating": null` clears it |
| Remove from the tracker | `DELETE /api/shows/{id}` |
| Re-sync from TMDb now | `POST /api/shows/{id}/refresh?full=false` |
| Mark a movie watched/unwatched | `POST /api/shows/{id}/watch` body `{"watched": true, "at"?}` — 400 on a TV show |
| List a show's episodes | `GET /api/shows/{id}/episodes` |
| Mark one episode watched/unwatched | `POST /api/shows/{id}/episodes/{episode_id}/watched` body `{"watched": true, "at"?}` |
| Catch-up a whole season | `POST /api/shows/{id}/seasons/{season_number}/watched` |
| Trigger the show sync + Telegram digest | `POST /api/bot/shows-check` (same logic as the 08:00 cron) |

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
  `dish_<id>.<ext>`). For dishes there is also an **AI-free** upload,
  `POST /api/dishes/photo`, which stores the photo the same way and returns
  only a `photo_token` — no OpenRouter call, no pre-filled fields. The dish
  form asks which mode to use and **defaults to manual entry**. Wishes work
  exactly the same way (`POST /api/wishlist/identify` for the AI route,
  `POST /api/wishlist/photo` for the plain upload, `wish_<id>.<ext>`).
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
- **Wishlist statuses** are stored, not computed: `wanted` → `reserved` →
  `bought` → `received`, plus `archived` (an abandoned wish; hidden from the
  UI's default filter). `is_taken` is the computed shorthand for
  `reserved | bought`. `PUT /api/wishlist/items/{id}` with `status` omitted or
  `null` keeps the current status, so the owner editing their own wish never
  wipes a gifter's reservation — only the action endpoints move an item along
  the workflow.
- **The spoiler-free view is the safe default.** `?hide_reservations=true`
  blanks `reserved_by`/`reserved_at`/`bought_by`/`bought_at`, rewinds a
  `reserved`/`bought` item back to `wanted`, sets `is_taken` to `null` and adds
  `reservations_hidden: true`; in `/overview` the counters and the remaining
  budget are folded the same way so they can't leak either. The frontend starts
  in that mode and only reveals reservations when someone presses « Mode
  cadeau ». When an agent answers on a shared channel (the Telegram chat, a
  screen the whole household can see), prefer it too — `/envies` shows
  « 🔒 pris en charge » but never who.
- **Wish priority** is `1` (un jour) / `2` (ça me plairait) / `3` (j'en rêve),
  rendered as 1-3 stars; lists come back sorted by priority, highest first.
  Categories are accent-free slugs shared with the vision prompt
  (`app.vision.WISH_CATEGORIES`); an unknown one falls back to `autre` instead
  of erroring. Prices are plain numbers with a separate `currency` (default
  `CHF`).
- **First names are matched loosely** everywhere a human types one (Telegram,
  `/people/resolve`): `wishlist.normalize_name()` strips accents, case and
  punctuation, so « Séb », « seb » and « SEB. » collapse to the same key.
  `match_people()` then tries exact key → prefix either way (« Séb » finds
  « Sébastien ») → substring, and returns **every** candidate: more than one
  means ambiguous, and the caller must ask rather than guess. Never match on
  raw `name` with `in` or `==` — that is how « seb » stopped finding « Séb ».
- **Natural-language wish capture** (`app/nlu.py` + `bot._wish_text_handler`):
  the LLM only *extracts* (`intent`, `person`, `for_sender`, `items[]`); it
  never writes. Everything goes through the normal router functions, so the
  same validation applies, and unknown keys the model invents are dropped by
  `nlu._clean_item()`. Missing fields are asked one wish at a time through a
  per-chat pending state (in memory, 15 min TTL — a restart forgets pending
  questions, which beats answering the wrong one). Free text is parsed **only
  in private chats**; in a group it takes `/envie …`, otherwise every message
  in the room would hit OpenRouter. A message that is not a wish gets no reply
  at all.
- **Telegram handlers** take `(args, chat_id, user_id)` for commands and
  `(text, chat_id, user_id, is_private)` for the text handler, and run in a
  worker thread (`asyncio.to_thread`) — they may block on SQLite or an LLM
  call. Returning `None` from the text handler means "stay silent".
- **Storage status is stored, dates are automatic**: an item is `stored` or
  `out`; `POST .../out` sets `out_since` (now, or the `at` you pass) and
  `out_note` ("où / chez qui"), `POST .../in` clears both. Never write
  `out_since` by hand through `PUT /items/{id}` — it keeps whatever the item
  already had, because the status is meant to be driven by `/out` and `/in`.
  Every one of those calls appends to `storage_events`, which is the audit
  trail behind `GET /items/{id}/history`.
- **Storage owners** are a comma-separated list in one `owner` column
  (`"Seb,Lea"`), echoed back parsed as `owners: ["Seb", "Lea"]`. Filtering by
  `owner=` matches membership, not the raw string.
- **Storage `category`** is a *collection* that deliberately crosses boxes
  ("Nautique" lives in cartons 2 and 6). It defaults to the box's `name` when
  omitted on create.
- **Storage search** (`?q=`): all words must match (AND), accent- and
  case-insensitively, across the item, its box and its owners. A query that is
  just a box code — `2`, `carton 2`, `#7` — returns that box's contents
  instead of every name containing that digit.
- **Stats are computed locally, from `genres`/`runtime_minutes`/`vote_average`**
  (`GET /api/shows/stats`) — plain TMDb fields synced onto `shows`/
  `show_episodes` by `refresh_show()`. A show added before these columns
  existed, or never resynced since, has them empty until its next sync —
  `stats.watch_time.estimated_entries` counts how many watched entries fell
  back to a flat runtime estimate (`EPISODE_RUNTIME_FALLBACK_MIN` /
  `MOVIE_RUNTIME_FALLBACK_MIN`) because TMDb had no runtime for them.
- **Show tracker is TMDb-keyed, not locally owned**: `shows.tmdb_id` +
  `media_type` (`tv`/`movie`) is the unique key (`UNIQUE (tmdb_id,
  media_type)` — 409 on a duplicate add). Title, overview, poster/backdrop
  paths, TMDb's own status and the cached "next episode to air" are all
  **synced copies**, refreshed by `refresh_show()` — never hand-edit them.
  Only `status`, `rating` and `notes` are ours; `PUT /api/shows/{id}` only
  ever touches those three.
- **`refresh_show()` is selective, not a full re-pull**: it always refetches
  the show's own TMDb details (cheap), but only refetches episode lists for
  seasons not seen yet **plus** the current highest-numbered season — where
  new episodes actually appear. `POST .../refresh?full=true` forces every
  season, used once when a show is first added.
- **Episode watched-state drives the tracking status, in both directions**:
  marking an episode (or a whole season, `POST .../seasons/{n}/watched`)
  watched promotes a show from `a_voir` to `en_cours` automatically, and —
  once TMDb says the show is `Ended`/`Canceled` and every aired episode is
  watched — closes it to `termine` (`_maybe_autocomplete`). The reverse also
  happens: if TMDb later renews a `termine` show, or a sync pulls in an
  episode that isn't watched yet, it reopens to `en_cours`
  (`_maybe_reopen`) — otherwise a finished show would go stale forever the
  moment it got renewed. Nobody has to remember to flip the status by hand
  either way.
- **The daily sync covers every non-abandoned show, `termine` included**
  (`bot.sync_and_notify_shows` excludes only `abandonne`) — it has to, since
  `_maybe_reopen` (above) only fires *during* a sync. Only `abandonne` truly
  stops the bot from ever looking at a show again.
- **A Telegram "new episode" announcement fires exactly once per episode**:
  `show_episodes.notified_at` is set right after `telegram.send_message()`
  succeeds (never on failure, so a network hiccup retries next day), and the
  daily digest (08:00) only considers episodes with `air_date <= today AND
  watched_at IS NULL AND notified_at IS NULL` on a show with status
  `a_voir`/`en_cours` — reached by a `termine` show only via `_maybe_reopen`
  firing first, earlier in the same sync.
- **Telegram bot**: a generic module (`app/telegram.py`) + feature wiring
  (`app/bot.py`). Features register cron jobs with `bot.add_cron_job(func,
  hour, minute)` (daily, server-local time or `TELEGRAM_TZ`) and commands with
  `telegram.register_command("/cmd", handler)`. The scheduler lives inside the
  app process, so it starts/stops with the container. Commands so far:
  `/plantes`, `/envies [prénom]`, `/envie <texte>`, `/moi <prénom>`,
  `/cartons [recherche]`, `/ou <objet>`, `/sortis`, `/sorti <texte>`,
  `/range <texte>`, `/series`, `/help`. Setup: `TELEGRAM_BOT.md`.
- **Several text handlers can coexist**: `telegram.register_text_handler()`
  appends, and handlers are tried in registration order until one returns a
  reply — so each must return `None` for anything that is not its business.
  Storage is registered *before* the wishlist and only wakes the LLM when the
  message contains a storage word (`carton`, `sorti`, `rangé`, `prêté`…);
  everything else falls through to the wishlist. Both features share the
  per-chat `_pending` conversation slot, and each ignores a pending
  conversation whose `stage` is not its own.

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
