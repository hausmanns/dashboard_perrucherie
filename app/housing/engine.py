"""Run a saved housing search: ask each site, keep what really matches,
store it, merge the copies of one flat, and tell Telegram what's new.

A run, step by step:
1. the sources run side by side (one thread each, each polite to its own
   site) with the search's criteria; they filter natively what they can;
2. every listing goes through `evaluate()` — the same rules for every
   source, plus the zone check on postcode / position;
3. matches are upserted into `housing_listings` (one row per site listing,
   `UNIQUE (source, source_id)`), a price change is appended to
   `price_history`, and a new row is merged into an existing one when it is
   the same flat on another site (same SMG id, or same rooms/price/surface
   within 80 m) — `duplicate_of` points at the row that carries the
   household's state (seen / favourite / not for us);
4. `housing_matches` links the search to the canonical listing; a new link is
   a new result. The first run of a search (or the first after its criteria
   changed) is a **baseline**: results are stored and shown as unseen, but
   not announced, so Telegram never gets a 150-line message;
5. a Telegram summary lists the new results not yet announced, once.

Runs are serialised (`_run_lock`): one at a time is gentler on the sites
and on SQLite. Their live progress lives in `_progress` while they run and
in `housing_runs` afterwards.
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import telegram
from ..config import TELEGRAM_TZ
from ..database import get_conn, rows_to_dicts
from . import flatfox, homegate, immobilier, places
from .common import CATEGORIES, FEATURES, RunContext, detect_features, has_words
from .http import SourceError

logger = logging.getLogger(__name__)

SOURCES = {m.KEY: m for m in (flatfox, homegate, immobilier)}
FIRST_RUN_PAGES = 10       # per location and source, on a baseline run
INCREMENTAL_PAGES = 4      # later runs usually stop after page 1 (see RunContext.caught_up)
AUTO_SLACK = timedelta(minutes=5)   # the auto check ticks every 10 min
ALERT_MAX_LINES = 15
DUP_DISTANCE_KM = 0.08
DUP_PRICE_TOLERANCE = 0.03

COLUMNS = (
    "smg_id", "url", "title", "description", "category", "price", "charges", "rooms", "surface",
    "floor", "street", "zipcode", "city", "canton", "lat", "lng", "geo_precision", "available_from",
    "features", "features_known", "image_url", "images", "agency", "published_at",
)

_run_lock = threading.Lock()
_progress: dict[int, dict] = {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def fmt_chf(n) -> str:
    return f"{int(n):,}".replace(",", "'") if n is not None else "?"


def fmt_rooms(r) -> str:
    return f"{r:g}" if r is not None else "?"


# ---------- Criteria ----------

def _num(v, kind=float):
    if v in (None, ""):
        return None
    try:
        return kind(v)
    except (TypeError, ValueError):
        return None


def normalize_criteria(raw: dict | None) -> dict:
    c = dict(raw or {})
    words = lambda key: [w.strip() for w in c.get(key) or [] if str(w).strip()]
    return {
        "zones": [z for z in c.get("zones") or [] if isinstance(z, dict)],
        "categories": [x for x in c.get("categories") or ["apartment"] if x in CATEGORIES] or ["apartment"],
        "price_min": _num(c.get("price_min"), int), "price_max": _num(c.get("price_max"), int),
        "rooms_min": _num(c.get("rooms_min")), "rooms_max": _num(c.get("rooms_max")),
        "surface_min": _num(c.get("surface_min"), int), "surface_max": _num(c.get("surface_max"), int),
        "features": [f for f in c.get("features") or [] if f in FEATURES],
        "keywords_include": words("keywords_include"),
        "keywords_exclude": words("keywords_exclude"),
        "sources": [s for s in c.get("sources") or list(SOURCES) if s in SOURCES],
    }


def evaluate(item: dict, crit: dict, zones: list, full_text: bool) -> tuple[bool, list[str]]:
    """Does a listing match the criteria? → (ok, things to check by hand).

    Missing data never rejects a listing (a card without its surface still
    shows up); it lands in the to-check list instead. A feature or an
    include-keyword only rejects when the source gives the whole picture —
    structured features (Flatfox) or the full ad text (`full_text`)."""
    to_check: list[str] = []
    category = item.get("category")
    if category == "other" or (category and category not in crit["categories"]):
        return False, []
    for field, lo, hi in (("price", "price_min", "price_max"), ("rooms", "rooms_min", "rooms_max"),
                          ("surface", "surface_min", "surface_max")):
        value, low, high = item.get(field), crit.get(lo), crit.get(hi)
        if value is None:
            if field == "surface" and (low is not None or high is not None):
                to_check.append("surface")
            continue
        if (low is not None and value < low) or (high is not None and value > high):
            return False, []
    if not places.in_zones(item, zones):
        return False, []
    text = f"{item.get('title') or ''}\n{item.get('description') or ''}"
    if crit["keywords_exclude"] and has_words(text, crit["keywords_exclude"]):
        return False, []
    known = set(item.get("features") or ()) | detect_features(text)
    for f in crit["features"]:
        if f in known:
            continue
        if item.get("features_known") or full_text:
            return False, []
        to_check.append(FEATURES[f][0].lower())
    if crit["keywords_include"] and not has_words(text, crit["keywords_include"]):
        if full_text:
            return False, []
        to_check.append("mots-clés")
    return True, to_check


# ---------- Listings in the DB ----------

def row_to_listing(row: dict) -> dict:
    item = dict(row)
    item["features"] = {f for f in (row.get("features") or "").split(",") if f}
    item["features_known"] = bool(row.get("features_known"))
    try:
        item["images"] = json.loads(row.get("images") or "[]")
    except ValueError:
        item["images"] = []
    return item


def _known_rows(source: str, ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with get_conn() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT * FROM housing_listings WHERE source = ? AND source_id IN ({marks})", (source, *chunk)
            ).fetchall():
                out[r["source_id"]] = row_to_listing(dict(r))
    return out


def _known_before(source: str, since: str | None) -> set[str]:
    if not since:
        return set()
    with get_conn() as conn:
        return {r[0] for r in conn.execute(
            "SELECT source_id FROM housing_listings WHERE source = ? AND first_seen_at < ?", (source, since)
        ).fetchall()}


def _watermarks(search_id: int) -> dict[str, int]:
    """Per source, the highest listing id its last successful pass returned."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT details FROM housing_runs WHERE search_id = ? AND status IN ('ok', 'partial') "
            "ORDER BY id DESC LIMIT 1", (search_id,)).fetchone()
    sources = json.loads(row["details"] or "{}").get("sources", {}) if row else {}
    return {k: s["max_id"] for k, s in sources.items() if s.get("state") == "done" and s.get("max_id")}


def _db_values(item: dict) -> dict:
    v = {k: item.get(k) for k in COLUMNS}
    v["features"] = ",".join(sorted(item.get("features") or ()))
    v["features_known"] = 1 if item.get("features_known") else 0
    v["images"] = json.dumps(item.get("images") or [])
    v["zipcode"] = str(v["zipcode"] or "")
    if v["lat"] is None or v["lng"] is None:
        pos = places.locate(v["zipcode"], v["city"] or "")
        if pos:
            v["lat"], v["lng"], v["geo_precision"] = round(pos[0], 6), round(pos[1], 6), "zip"
        else:
            v["geo_precision"] = "none"
    if not v["canton"] and v["zipcode"]:
        v["canton"] = places.canton_of_zip(v["zipcode"])
    return v


def upsert_listing(conn, item: dict, now: str) -> tuple[int, bool]:
    """Insert or refresh one site listing. → (row id, created?)"""
    v = _db_values(item)
    row = conn.execute(
        "SELECT id, price, price_history FROM housing_listings WHERE source = ? AND source_id = ?",
        (item["source"], item["source_id"]),
    ).fetchone()
    if row is None:
        cols = ", ".join(COLUMNS)
        cur = conn.execute(
            f"""INSERT INTO housing_listings (source, source_id, {cols}, first_seen_at, last_seen_at)
                VALUES (?, ?, {", ".join("?" * len(COLUMNS))}, ?, ?)""",
            (item["source"], item["source_id"], *[v[c] for c in COLUMNS], now, now),
        )
        return cur.lastrowid, True
    history = json.loads(row["price_history"] or "[]")
    if v["price"] and row["price"] and v["price"] != row["price"]:
        history.append({"at": now, "from": row["price"], "to": v["price"]})
    sets = ", ".join(f"{c} = ?" for c in COLUMNS)
    conn.execute(
        f"UPDATE housing_listings SET {sets}, last_seen_at = ?, price_history = ? WHERE id = ?",
        (*[v[c] for c in COLUMNS], now, json.dumps(history), row["id"]),
    )
    return row["id"], False


def _canonical(conn, listing_id: int, item: dict) -> int:
    """Merge a newly stored listing into an older copy of the same flat on
    another site, and return the id that carries the household's state."""
    canon = None
    if item.get("smg_id"):
        other = conn.execute(
            "SELECT id, duplicate_of FROM housing_listings WHERE smg_id = ? AND id != ? ORDER BY id LIMIT 1",
            (str(item["smg_id"]), listing_id),
        ).fetchone()
        if other:
            canon = other["duplicate_of"] or other["id"]
    lat, lng, price, rooms = item.get("lat"), item.get("lng"), item.get("price"), item.get("rooms")
    if canon is None and None not in (lat, lng, price, rooms) and item.get("geo_precision") == "exact":
        d = 0.0012  # ≈ 100 m box, refined with the real distance below
        for c in conn.execute(
            """SELECT id, duplicate_of, lat, lng, surface FROM housing_listings
               WHERE id != ? AND source != ? AND geo_precision = 'exact' AND rooms = ?
                 AND price BETWEEN ? AND ? AND lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?
               ORDER BY id""",
            (listing_id, item["source"], rooms, price * (1 - DUP_PRICE_TOLERANCE),
             price * (1 + DUP_PRICE_TOLERANCE), lat - d, lat + d, lng - d, lng + d),
        ).fetchall():
            same_size = not (c["surface"] and item.get("surface")) or abs(c["surface"] - item["surface"]) <= 3
            if same_size and places.haversine_km(lat, lng, c["lat"], c["lng"]) <= DUP_DISTANCE_KM:
                canon = c["duplicate_of"] or c["id"]
                break
    if canon and canon != listing_id:
        conn.execute("UPDATE housing_listings SET duplicate_of = ? WHERE id = ?", (canon, listing_id))
        conn.execute("UPDATE OR IGNORE housing_matches SET listing_id = ? WHERE listing_id = ?", (canon, listing_id))
        conn.execute("DELETE FROM housing_matches WHERE listing_id = ?", (listing_id,))
        return canon
    return listing_id


def prune_orphans(conn) -> int:
    """Forget site listings no search points at any more (manual ads and
    favourites stay)."""
    cur = conn.execute(
        """DELETE FROM housing_listings
           WHERE source != 'manual' AND favorite = 0
             AND COALESCE(duplicate_of, id) NOT IN (SELECT listing_id FROM housing_matches)
             AND COALESCE(duplicate_of, id) NOT IN
                 (SELECT id FROM housing_listings WHERE favorite = 1 OR source = 'manual')"""
    )
    return cur.rowcount


def reevaluate_matches(search_id: int) -> int:
    """After a criteria change: drop the results that no longer match (from
    the stored data, no network). Returns how many were dropped."""
    with get_conn() as conn:
        search = conn.execute("SELECT criteria FROM housing_searches WHERE id = ?", (search_id,)).fetchone()
        if not search:
            return 0
        crit = normalize_criteria(json.loads(search["criteria"] or "{}"))
        zones = places.resolve_zones(crit["zones"])
        rows = rows_to_dicts(conn.execute(
            """SELECT l.* FROM housing_matches m JOIN housing_listings l ON l.id = m.listing_id
               WHERE m.search_id = ?""", (search_id,)).fetchall())
        dropped = []
        for r in rows:
            src = SOURCES.get(r["source"])
            ok, _ = evaluate(row_to_listing(r), crit, zones, full_text=src.FULL_TEXT if src else True)
            if not ok:
                dropped.append(r["id"])
        conn.executemany("DELETE FROM housing_matches WHERE search_id = ? AND listing_id = ?",
                         [(search_id, i) for i in dropped])
        prune_orphans(conn)
    return len(dropped)


def match_against_searches(listing_id: int) -> list[int]:
    """Attach a hand-added listing to every search it fits (never announced:
    you added it yourself)."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM housing_listings WHERE id = ?", (listing_id,)).fetchone()
        if not row:
            return []
        item = row_to_listing(dict(row))
        hits = []
        for s in conn.execute("SELECT id, criteria FROM housing_searches").fetchall():
            crit = normalize_criteria(json.loads(s["criteria"] or "{}"))
            ok, _ = evaluate(item, crit, places.resolve_zones(crit["zones"]), full_text=True)
            if ok:
                conn.execute(
                    "INSERT OR IGNORE INTO housing_matches (search_id, listing_id, matched_at, notified_at) VALUES (?, ?, ?, ?)",
                    (s["id"], listing_id, _now(), _now()),
                )
                hits.append(s["id"])
        return hits


# ---------- Runs ----------

def recover_interrupted_runs() -> None:
    """At startup: runs left « running » by a restart will never finish."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE housing_runs SET status = 'error', finished_at = ?, error = 'Interrompue (redémarrage)' "
            "WHERE status IN ('queued', 'running')", (_now(),))


def is_running(search_id: int) -> bool:
    return any(p["search_id"] == search_id and p["status"] in ("queued", "running") for p in _progress.values())


def create_run(search_id: int, trigger: str) -> int:
    with get_conn() as conn:
        search = conn.execute("SELECT id, criteria FROM housing_searches WHERE id = ?", (search_id,)).fetchone()
        if not search:
            raise LookupError("Recherche introuvable")
        cur = conn.execute(
            "INSERT INTO housing_runs (search_id, trigger, started_at, status) VALUES (?, ?, ?, 'queued')",
            (search_id, trigger, _now()),
        )
        run_id = cur.lastrowid
    crit = normalize_criteria(json.loads(search["criteria"] or "{}"))
    _progress[run_id] = {
        "id": run_id, "search_id": search_id, "trigger": trigger, "status": "queued",
        "started_at": _now(), "found": 0, "new_count": 0, "requests": 0, "warnings": [],
        "sources": {k: {"label": SOURCES[k].LABEL, "state": "pending", "found": 0, "matched": 0,
                        "pages": 0, "requests": 0, "error": None} for k in crit["sources"]},
    }
    return run_id


def start_runs(search_ids: list[int], trigger: str = "manual") -> list[int]:
    """Queue runs and execute them one after the other in a background thread."""
    run_ids = [create_run(sid, trigger) for sid in search_ids if not is_running(sid)]
    if run_ids:
        threading.Thread(target=lambda: [execute_run(r) for r in run_ids], daemon=True,
                         name="housing-runs").start()
    return run_ids


def run_status(run_id: int) -> dict | None:
    live = _progress.get(run_id)
    if live:
        return {**live, "sources": {k: dict(v) for k, v in live["sources"].items()}}
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM housing_runs WHERE id = ?", (run_id,)).fetchone()
    return run_row_to_dict(dict(row)) if row else None


def run_row_to_dict(row: dict) -> dict:
    details = json.loads(row.get("details") or "{}")
    return {
        "id": row["id"], "search_id": row["search_id"], "trigger": row["trigger"], "status": row["status"],
        "started_at": row["started_at"], "finished_at": row["finished_at"], "found": row["found"],
        "new_count": row["new_count"], "requests": row["requests"], "error": row.get("error"),
        "warnings": details.get("warnings", []), "sources": details.get("sources", {}),
    }


def execute_run(run_id: int) -> None:
    live = _progress[run_id]
    with _run_lock:
        try:
            _run(run_id, live)
        except Exception as e:  # never leave a run « running »
            logger.exception("Housing run %s failed", run_id)
            live.update(status="error", error=str(e))
        finally:
            with get_conn() as conn:
                conn.execute(
                    """UPDATE housing_runs SET status = ?, finished_at = ?, found = ?, new_count = ?,
                         requests = ?, error = ?, details = ? WHERE id = ?""",
                    (live["status"], _now(), live["found"], live["new_count"], live["requests"],
                     live.get("error"), json.dumps({"sources": live["sources"], "warnings": live["warnings"]}),
                     run_id),
                )
            _progress.pop(run_id, None)


def _run(run_id: int, live: dict) -> None:
    started = _now()
    live.update(status="running", started_at=started)
    with get_conn() as conn:
        conn.execute("UPDATE housing_runs SET status = 'running', started_at = ? WHERE id = ?", (started, run_id))
        search = dict(conn.execute("SELECT * FROM housing_searches WHERE id = ?", (live["search_id"],)).fetchone())
    crit = normalize_criteria(json.loads(search["criteria"] or "{}"))
    zones = places.resolve_zones(crit["zones"])
    if not zones:
        live.update(status="error", error="Choisissez au moins une zone (commune, NPA, canton ou rayon)")
        return
    baseline = search["last_run_at"] is None
    watermarks = {} if baseline else _watermarks(search["id"])

    def run_source(key: str) -> list[dict]:
        src, stat = SOURCES[key], live["sources"][key]
        stat["state"] = "running"

        def progress(**kw):
            stat.update({k: v for k, v in kw.items() if k in ("found", "pages", "requests")})
            live["requests"] = sum(s["requests"] for s in live["sources"].values())

        ctx = RunContext(
            known_before=_known_before(key, search["last_run_at"]),
            watermark=watermarks.get(key),
            first_run=baseline,
            max_pages=FIRST_RUN_PAGES if baseline else INCREMENTAL_PAGES,
            known_rows=lambda ids: _known_rows(key, ids),
            progress=progress,
        )
        try:
            items = src.search(crit, zones, ctx)
            ids = [int(i["source_id"]) for i in items if str(i["source_id"]).isdigit()]
            stat["max_id"] = max(ids + [watermarks.get(key) or 0]) or None
            stat["state"] = "done"
            return items
        except SourceError as e:
            stat.update(state="error", error=str(e))
        except Exception as e:  # a parser surprise must not sink the other sources
            logger.exception("Housing source %s failed", key)
            stat.update(state="error", error=f"Erreur inattendue : {e}")
        finally:
            live["warnings"] += [w for w in ctx.warnings if w not in live["warnings"]]
        return []

    with ThreadPoolExecutor(max_workers=max(1, len(crit["sources"]))) as pool:
        batches = dict(zip(crit["sources"], pool.map(run_source, crit["sources"])))

    now = _now()
    with get_conn() as conn:
        for key, items in batches.items():
            stat = live["sources"][key]
            for item in items:
                ok, _ = evaluate(item, crit, zones, full_text=SOURCES[key].FULL_TEXT)
                if not ok:
                    continue
                stat["matched"] += 1
                listing_id, created = upsert_listing(conn, item, now)
                canon = _canonical(conn, listing_id, item) if created else (
                    conn.execute("SELECT COALESCE(duplicate_of, id) FROM housing_listings WHERE id = ?",
                                 (listing_id,)).fetchone()[0])
                cur = conn.execute(
                    "INSERT OR IGNORE INTO housing_matches (search_id, listing_id, matched_at, notified_at) VALUES (?, ?, ?, ?)",
                    (search["id"], canon, now, now if baseline else None),
                )
                live["new_count"] += cur.rowcount
        live["found"] = conn.execute(
            "SELECT COUNT(*) FROM housing_matches WHERE search_id = ?", (search["id"],)).fetchone()[0]
        conn.execute("UPDATE housing_searches SET last_run_at = ? WHERE id = ?", (started, search["id"]))

    states = [s["state"] for s in live["sources"].values()]
    live["status"] = "error" if states and all(s == "error" for s in states) else (
        "partial" if "error" in states else "ok")
    if live["status"] == "error":
        live["error"] = "Aucun site n'a répondu"
    # A check started by hand is watched on screen: only the robot's checks go to Telegram.
    notify_new(search, send=live["trigger"] == "auto")


# ---------- Telegram ----------

def alert_line(item: dict) -> str:
    bits = []
    if item.get("rooms"):
        bits.append(f"{fmt_rooms(item['rooms'])} p")
    if item.get("surface"):
        bits.append(f"{item['surface']} m²")
    if item.get("price"):
        bits.append(f"CHF {fmt_chf(item['price'])}")
    place = item.get("city") or ""
    if item.get("zipcode"):
        place += f" ({item['zipcode']})"
    head = " · ".join(bits) or (item.get("title") or "Annonce")
    return f"• {head} — {place}".rstrip(" —") + (f"\n  {item['url']}" if item.get("url") else "")


def notify_new(search: dict, send: bool = True) -> bool:
    """Telegram summary of this search's results not announced yet. Results
    already seen in the dashboard, or set aside, are skipped silently.
    `send=False` (a check run by hand) only marks them announced."""
    with get_conn() as conn:
        pending = rows_to_dicts(conn.execute(
            """SELECT l.*, m.listing_id AS match_id FROM housing_matches m
               JOIN housing_listings l ON l.id = m.listing_id
               WHERE m.search_id = ? AND m.notified_at IS NULL
               ORDER BY l.first_seen_at DESC, l.id DESC""", (search["id"],)).fetchall())
    if not pending:
        return False
    fresh = [p for p in pending if not p["dismissed"] and not p["seen_at"]]
    sent = True
    if fresh and send and search["notify"] and telegram.is_configured():
        n = len(fresh)
        head = f"🏠 {n} nouvelle{'s' if n > 1 else ''} annonce{'s' if n > 1 else ''} — « {search['name']} »"
        lines = [head, ""] + [alert_line(p) for p in fresh[:ALERT_MAX_LINES]]
        if n > ALERT_MAX_LINES:
            lines += ["", f"… et {n - ALERT_MAX_LINES} autre(s) dans l'onglet Logement du dashboard."]
        sent = telegram.send_message("\n".join(lines), disable_preview=True)
    if sent:  # a failed send stays pending and is retried after the next run
        with get_conn() as conn:
            conn.executemany(
                "UPDATE housing_matches SET notified_at = ? WHERE search_id = ? AND listing_id = ?",
                [(_now(), search["id"], p["match_id"]) for p in pending],
            )
    return sent and bool(fresh)


# ---------- Scheduler hook ----------

def in_active_hours(hour: int, start: int, end: int) -> bool:
    if start == end:
        return True
    return start <= hour < end if start < end else (hour >= start or hour < end)


def next_auto_run(search: dict) -> str | None:
    """When the auto check will next pick this search up (server-local ISO), or None."""
    if not search.get("auto_run"):
        return None
    due = datetime.now()
    if search.get("last_run_at"):
        due = max(due, datetime.fromisoformat(search["last_run_at"]) + timedelta(minutes=search["interval_minutes"]))
    local = (lambda d: d.astimezone(ZoneInfo(TELEGRAM_TZ))) if TELEGRAM_TZ else (lambda d: d.astimezone())
    for _ in range(48):  # skip forward out of the quiet hours
        if in_active_hours(local(due).hour, search["active_from"], search["active_to"]):
            break
        due = (due + timedelta(hours=1)).replace(minute=0, second=0)
    return due.isoformat(timespec="minutes")


def auto_run_check() -> int:
    """Every 10 min (app/bot.py scheduler): run the searches whose interval
    has elapsed, inside their active hours. Synchronous — the scheduler thread
    is ours. Returns how many ran. A machine that slept simply catches up at
    the next tick."""
    now_local = datetime.now(ZoneInfo(TELEGRAM_TZ)) if TELEGRAM_TZ else datetime.now().astimezone()
    with get_conn() as conn:
        searches = rows_to_dicts(conn.execute("SELECT * FROM housing_searches WHERE auto_run = 1").fetchall())
    ran = 0
    for s in searches:
        if not in_active_hours(now_local.hour, s["active_from"], s["active_to"]) or is_running(s["id"]):
            continue
        if s["last_run_at"]:
            elapsed = datetime.now() - datetime.fromisoformat(s["last_run_at"])
            if elapsed < timedelta(minutes=s["interval_minutes"]) - AUTO_SLACK:
                continue
        execute_run(create_run(s["id"], "auto"))
        ran += 1
    return ran
