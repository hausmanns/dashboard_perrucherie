"""Show tracker: what we watch, what we're watching, and what's next.

Backed by TMDb (`app/tmdb.py`) for search, trending, covers, synopsis and
episode-level data — nothing is stored locally except the household's own
tracking state (status, rating, notes, which episodes are watched).

Two tables:
- `shows`         — one row per tracked movie or TV show, keyed by
  `(tmdb_id, media_type)`. Carries our own status/rating/notes plus a cache
  of TMDb's "next episode to air" (used by the calendar and the Telegram
  digest without hitting TMDb on every read).
- `show_episodes` — one row per TV episode (movies have none), watched state
  and whether Telegram already announced it.

`refresh_show()` re-syncs a show from TMDb: it always refetches the show's
own details (cheap) but only refetches season episode lists for seasons not
seen yet plus the latest season (where new episodes actually show up) —
re-pulling a 10-season back catalogue on every sync would be wasteful.
`app.bot.sync_and_notify_shows` calls it daily for every followed show, then
tells Telegram about episodes that just aired and are not yet watched.

Tracking is a single household-wide state (not per person), matching the
rest of this dashboard's shared views (plants, storage) rather than the
per-person wishlist.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import tmdb
from ..database import get_conn, rows_to_dicts
from ..tmdb import TMDbError

router = APIRouter(prefix="/api/shows", tags=["shows"])

STATUSES = ("a_voir", "en_cours", "termine", "abandonne")
MEDIA_TYPES = ("tv", "movie")
# TMDb tv `status` values that mean "more episodes may still come".
ONGOING_TMDB_STATUSES = ("Returning Series", "Planned", "In Production", "Pilot")
# Used only when TMDb didn't give a runtime for a watched episode/movie, so the
# watch-time stat is a plausible estimate rather than silently undercounting.
EPISODE_RUNTIME_FALLBACK_MIN = 45
MOVIE_RUNTIME_FALLBACK_MIN = 100


class ShowAddIn(BaseModel):
    tmdb_id: int
    media_type: str
    status: Optional[str] = None  # None -> 'a_voir'


class ShowUpdateIn(BaseModel):
    """Partial update: only the fields provided are changed."""
    status: Optional[str] = None
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    notes: Optional[str] = None


class EpisodeWatchIn(BaseModel):
    watched: bool = True
    at: Optional[str] = None  # ISO datetime, defaults to now


class MovieWatchIn(BaseModel):
    watched: bool = True
    at: Optional[str] = None


# ---------- Helpers ----------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_media_type(media_type: str) -> str:
    media_type = (media_type or "").strip().lower()
    if media_type not in MEDIA_TYPES:
        raise HTTPException(422, f"Type inconnu : {media_type} (attendu : tv, movie)")
    return media_type


def _clean_status(status: Optional[str], default: Optional[str] = None) -> Optional[str]:
    if status is None:
        return default
    status = status.strip().lower()
    if status not in STATUSES:
        raise HTTPException(422, f"Statut inconnu : {status} (attendu : {', '.join(STATUSES)})")
    return status


def _get_show_row(conn, show_id: int):
    row = conn.execute("SELECT * FROM shows WHERE id = ?", (show_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Série ou film introuvable dans le suivi")
    return row


def _episode_counts(conn, show_id: int) -> dict:
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(watched_at IS NOT NULL) AS watched,
                  SUM(air_date IS NOT NULL AND air_date <= date('now') AND watched_at IS NULL) AS unwatched_aired
           FROM show_episodes WHERE show_id = ?""",
        (show_id,),
    ).fetchone()
    return {
        "total_episodes": row["total"] or 0,
        "watched_episodes": row["watched"] or 0,
        "unwatched_aired_episodes": row["unwatched_aired"] or 0,
    }


def _enrich_show(conn, row: dict) -> dict:
    row["poster_url"] = tmdb.poster_url(row["poster_path"])
    row["backdrop_url"] = tmdb.backdrop_url(row["backdrop_path"])
    row["is_ongoing"] = row["media_type"] == "tv" and row["tmdb_status"] in ONGOING_TMDB_STATUSES
    row["next_episode"] = (
        {
            "season_number": row["next_episode_season"],
            "episode_number": row["next_episode_number"],
            "name": row["next_episode_name"],
            "air_date": row["next_episode_air_date"],
        }
        if row["next_episode_air_date"] else None
    )
    if row["media_type"] == "tv":
        row.update(_episode_counts(conn, row["id"]))
    else:
        row["total_episodes"] = row["watched_episodes"] = row["unwatched_aired_episodes"] = 0
    return row


def _genre_names(details: dict) -> str:
    return ", ".join(g["name"] for g in details.get("genres", []) if g.get("name"))


def _upsert_episodes(conn, show_id: int, season_number: int, episodes: list[dict]) -> None:
    for ep in episodes:
        conn.execute(
            """INSERT INTO show_episodes (show_id, season_number, episode_number, name, overview, air_date, runtime_minutes)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (show_id, season_number, episode_number) DO UPDATE SET
                 name = excluded.name, overview = excluded.overview, air_date = excluded.air_date,
                 runtime_minutes = excluded.runtime_minutes""",
            (show_id, season_number, ep.get("episode_number"), ep.get("name", ""),
             ep.get("overview", ""), ep.get("air_date"), ep.get("runtime")),
        )


def _sync_tv(conn, show_id: int, tmdb_id: int, full: bool = False) -> dict:
    details = tmdb.tv_details(tmdb_id)
    next_ep = details.get("next_episode_to_air") or {}
    conn.execute(
        """UPDATE shows SET title=?, overview=?, poster_path=?, backdrop_path=?, first_air_date=?,
             tmdb_status=?, genres=?, vote_average=?, next_episode_air_date=?, next_episode_season=?,
             next_episode_number=?, next_episode_name=?, last_synced_at=?, updated_at=? WHERE id=?""",
        (details.get("name", ""), details.get("overview", ""), details.get("poster_path", ""),
         details.get("backdrop_path", ""), details.get("first_air_date", ""), details.get("status", ""),
         _genre_names(details), details.get("vote_average"),
         next_ep.get("air_date"), next_ep.get("season_number"), next_ep.get("episode_number"),
         next_ep.get("name", ""), _now(), _now(), show_id),
    )

    all_seasons = [s["season_number"] for s in details.get("seasons", [])]
    known = {r["season_number"] for r in
             conn.execute("SELECT DISTINCT season_number FROM show_episodes WHERE show_id = ?", (show_id,)).fetchall()}
    to_sync = set(all_seasons) if full else (set(all_seasons) - known)
    if all_seasons:
        to_sync.add(max(all_seasons))  # la saison en cours peut gagner de nouveaux épisodes

    for season_number in to_sync:
        try:
            season = tmdb.tv_season(tmdb_id, season_number)
        except TMDbError:
            continue
        _upsert_episodes(conn, show_id, season_number, season.get("episodes", []))

    return dict(_get_show_row(conn, show_id))


def _sync_movie(conn, show_id: int, tmdb_id: int) -> dict:
    details = tmdb.movie_details(tmdb_id)
    conn.execute(
        """UPDATE shows SET title=?, overview=?, poster_path=?, backdrop_path=?, first_air_date=?,
             tmdb_status=?, genres=?, vote_average=?, runtime_minutes=?, last_synced_at=?, updated_at=? WHERE id=?""",
        (details.get("title", ""), details.get("overview", ""), details.get("poster_path", ""),
         details.get("backdrop_path", ""), details.get("release_date", ""), details.get("status", ""),
         _genre_names(details), details.get("vote_average"), details.get("runtime"),
         _now(), _now(), show_id),
    )
    return dict(_get_show_row(conn, show_id))


def _maybe_reopen(conn, show_id: int, synced_before: Optional[str], last_known_episode_id: int) -> None:
    """A `termine` show reopens to `en_cours` once something new has actually
    aired: an unwatched regular-season episode whose air date passed since
    the previous sync (`synced_before`), or that this sync pulled in for the
    first time already aired (id above `last_known_episode_id`). The digest
    that runs right after the sync then announces it.

    Only *new* airings count. TMDb saying « Returning Series » doesn't — that
    can last a year with nothing to watch, and would undo a « Terminé » every
    morning — and neither do specials (season 0) or episodes that were
    already unwatched when the show was marked done. Until the new season
    airs, the show stays `termine` and its upcoming episodes show up in the
    calendar and on its card instead."""
    row = _get_show_row(conn, show_id)
    if row["status"] != "termine" or not synced_before:
        return
    fresh = conn.execute(
        """SELECT 1 FROM show_episodes
           WHERE show_id = ? AND season_number > 0 AND watched_at IS NULL
             AND air_date IS NOT NULL AND air_date <= date('now')
             AND (air_date > date(?) OR id > ?)
           LIMIT 1""",
        (show_id, synced_before, last_known_episode_id),
    ).fetchone()
    if fresh:
        conn.execute("UPDATE shows SET status='en_cours', updated_at=? WHERE id=?", (_now(), show_id))


def refresh_show(show_id: int, full: bool = False) -> dict:
    """Re-sync a tracked show/movie from TMDb. `full` re-pulls every season."""
    with get_conn() as conn:
        row = _get_show_row(conn, show_id)
        if row["media_type"] == "tv":
            last_known_episode_id = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM show_episodes WHERE show_id = ?", (show_id,)
            ).fetchone()[0]
            _sync_tv(conn, show_id, row["tmdb_id"], full=full)
            _maybe_reopen(conn, show_id, row["last_synced_at"], last_known_episode_id)
        else:
            _sync_movie(conn, show_id, row["tmdb_id"])
        return _enrich_show(conn, dict(_get_show_row(conn, show_id)))


# ---------- Discover (TMDb passthrough) ----------

@router.get("/status")
def tmdb_status():
    """Whether the show tracker can reach TMDb — the frontend uses this to warn."""
    return {"tmdb_configured": tmdb.is_configured()}


@router.get("/search")
def search(q: str, media_type: Optional[str] = None):
    """Search TMDb for something to add. `already_tracked` is our own show id, if any."""
    if not q.strip():
        return []
    try:
        results = tmdb.search_multi(q.strip())
    except TMDbError as e:
        raise HTTPException(502, str(e))
    if media_type:
        results = [r for r in results if r["media_type"] == _clean_media_type(media_type)]
    with get_conn() as conn:
        tracked = {
            (r["tmdb_id"], r["media_type"]): r["id"]
            for r in conn.execute("SELECT id, tmdb_id, media_type FROM shows").fetchall()
        }
    for r in results:
        r["already_tracked"] = tracked.get((r["tmdb_id"], r["media_type"]))
    return sorted(results, key=lambda r: r["popularity"], reverse=True)


@router.get("/trending")
def trending(window: str = "week", media_type: str = "all"):
    """What's trending right now on TMDb — the « Découvrir » tab."""
    try:
        results = tmdb.trending(media_type=media_type, window=window)
    except TMDbError as e:
        raise HTTPException(502, str(e))
    with get_conn() as conn:
        tracked = {
            (r["tmdb_id"], r["media_type"]): r["id"]
            for r in conn.execute("SELECT id, tmdb_id, media_type FROM shows").fetchall()
        }
    for r in results:
        r["already_tracked"] = tracked.get((r["tmdb_id"], r["media_type"]))
    return results


# ---------- Tracked shows ----------

@router.get("")
def list_shows(status: Optional[str] = None, media_type: Optional[str] = None):
    sql = "SELECT * FROM shows"
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(_clean_status(status))
    if media_type:
        where.append("media_type = ?")
        params.append(_clean_media_type(media_type))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC"
    with get_conn() as conn:
        rows = rows_to_dicts(conn.execute(sql, params).fetchall())
        return [_enrich_show(conn, r) for r in rows]


@router.post("", status_code=201)
def add_show(payload: ShowAddIn):
    """Add something to the tracker from a TMDb search/trending result."""
    media_type = _clean_media_type(payload.media_type)
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM shows WHERE tmdb_id = ? AND media_type = ?", (payload.tmdb_id, media_type)
        ).fetchone()
        if existing:
            raise HTTPException(409, "Déjà dans le suivi")
        cur = conn.execute(
            "INSERT INTO shows (tmdb_id, media_type, title, status) VALUES (?, ?, '', ?)",
            (payload.tmdb_id, media_type, _clean_status(payload.status, "a_voir")),
        )
        show_id = cur.lastrowid

    try:
        if media_type == "tv":
            with get_conn() as conn:
                _sync_tv(conn, show_id, payload.tmdb_id, full=True)
        else:
            with get_conn() as conn:
                _sync_movie(conn, show_id, payload.tmdb_id)
    except TMDbError as e:
        with get_conn() as conn:
            conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))
        raise HTTPException(502, str(e))

    with get_conn() as conn:
        return _enrich_show(conn, dict(_get_show_row(conn, show_id)))


@router.get("/stats")
def stats():
    """Aggregate viewing stats for the « Statistiques » panel: status/media-type
    mix, genre allocation, estimated watch time, monthly activity, ratings.

    Everything is computed from what's already stored locally (no TMDb calls,
    works even offline). Runtime is TMDb data and is often missing for an
    episode or an older movie; a missing value falls back to a flat estimate
    (`EPISODE_RUNTIME_FALLBACK_MIN` / `MOVIE_RUNTIME_FALLBACK_MIN`) rather than
    silently undercounting — `watch_time.estimated_entries` says how many
    watched entries relied on that fallback.
    """
    with get_conn() as conn:
        all_shows = rows_to_dicts(conn.execute("SELECT * FROM shows").fetchall())
        watched_episodes = rows_to_dicts(conn.execute(
            """SELECT e.watched_at, e.runtime_minutes FROM show_episodes e
               WHERE e.watched_at IS NOT NULL"""
        ).fetchall())

    by_status = {st: 0 for st in STATUSES}
    by_media_type = {"tv": 0, "movie": 0}
    genre_counts: dict[str, int] = {}
    rating_dist = {str(n): 0 for n in range(1, 6)}
    ratings: list[int] = []
    rated_shows: list[dict] = []
    vote_values: list[float] = []
    monthly: dict[str, dict] = {}

    movies_watched = 0
    movie_minutes = 0
    estimated_entries = 0

    for s in all_shows:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
        by_media_type[s["media_type"]] = by_media_type.get(s["media_type"], 0) + 1
        for g in (s["genres"] or "").split(","):
            g = g.strip()
            if g:
                genre_counts[g] = genre_counts.get(g, 0) + 1
        if s["rating"]:
            ratings.append(s["rating"])
            rating_dist[str(s["rating"])] += 1
            rated_shows.append(s)
        if s["vote_average"]:
            vote_values.append(s["vote_average"])
        if s["media_type"] == "movie" and s["watched_at"]:
            movies_watched += 1
            minutes = s["runtime_minutes"] or MOVIE_RUNTIME_FALLBACK_MIN
            if not s["runtime_minutes"]:
                estimated_entries += 1
            movie_minutes += minutes
            month = s["watched_at"][:7]
            bucket = monthly.setdefault(month, {"month": month, "episodes": 0, "movies": 0, "minutes": 0})
            bucket["movies"] += 1
            bucket["minutes"] += minutes

    episode_minutes = 0
    for e in watched_episodes:
        minutes = e["runtime_minutes"] or EPISODE_RUNTIME_FALLBACK_MIN
        if not e["runtime_minutes"]:
            estimated_entries += 1
        episode_minutes += minutes
        month = (e["watched_at"] or "")[:7]
        if month:
            bucket = monthly.setdefault(month, {"month": month, "episodes": 0, "movies": 0, "minutes": 0})
            bucket["episodes"] += 1
            bucket["minutes"] += minutes

    rated_shows.sort(key=lambda s: (-s["rating"], s["title"]))
    total_minutes = episode_minutes + movie_minutes

    return {
        "totals": {"tracked": len(all_shows), "by_status": by_status, "by_media_type": by_media_type},
        "genres": sorted(
            ({"genre": g, "count": c} for g, c in genre_counts.items()), key=lambda x: -x["count"]
        )[:12],
        "watch_time": {
            "total_minutes": total_minutes,
            "total_hours": round(total_minutes / 60, 1),
            "episode_minutes": episode_minutes,
            "movie_minutes": movie_minutes,
            "episodes_watched": len(watched_episodes),
            "movies_watched": movies_watched,
            "estimated_entries": estimated_entries,
        },
        "monthly_activity": sorted(monthly.values(), key=lambda b: b["month"])[-12:],
        "ratings": {
            "average": round(sum(ratings) / len(ratings), 2) if ratings else None,
            "count": len(ratings),
            "distribution": rating_dist,
            "top_rated": [
                {"id": s["id"], "title": s["title"], "media_type": s["media_type"], "rating": s["rating"],
                 "poster_url": tmdb.poster_url(s["poster_path"])}
                for s in rated_shows[:8]
            ],
        },
        "tmdb_quality": {
            "average_vote": round(sum(vote_values) / len(vote_values), 2) if vote_values else None,
            "count": len(vote_values),
        },
    }


@router.get("/calendar")
def calendar(days: int = Query(30, ge=1, le=180)):
    """Upcoming episodes of followed shows — the « Calendrier » tab.

    Every show except abandoned ones — `termine` included, so a finished show
    that TMDb renewed shows up here before its new season airs.
    """
    with get_conn() as conn:
        rows = rows_to_dicts(conn.execute(
            """SELECT e.id AS episode_id, e.season_number, e.episode_number, e.name, e.air_date,
                      s.id AS show_id, s.title, s.poster_path, s.status AS show_status
               FROM show_episodes e JOIN shows s ON s.id = e.show_id
               WHERE e.air_date IS NOT NULL AND e.air_date BETWEEN date('now') AND date('now', ?)
                 AND s.status != 'abandonne'
               ORDER BY e.air_date, s.title""",
            (f"+{days} days",),
        ).fetchall())
    for r in rows:
        r["poster_url"] = tmdb.poster_url(r.pop("poster_path"))
    return rows


@router.get("/{show_id}")
def get_show(show_id: int):
    with get_conn() as conn:
        row = dict(_get_show_row(conn, show_id))
        episodes = rows_to_dicts(conn.execute(
            "SELECT * FROM show_episodes WHERE show_id = ? ORDER BY season_number, episode_number",
            (show_id,),
        ).fetchall())
        result = _enrich_show(conn, row)
    result["episodes"] = episodes
    return result


@router.put("/{show_id}")
def update_show(show_id: int, payload: ShowUpdateIn):
    """Partial update. `rating: null` explicitly clears the rating; an omitted
    `rating` key leaves it alone — that distinction needs `model_fields_set`
    since both would otherwise decode to the same `None`."""
    with get_conn() as conn:
        current = _get_show_row(conn, show_id)
        rating = payload.rating if "rating" in payload.model_fields_set else current["rating"]
        conn.execute(
            "UPDATE shows SET status=?, rating=?, notes=?, updated_at=? WHERE id=?",
            (
                _clean_status(payload.status, current["status"]),
                rating,
                payload.notes if payload.notes is not None else current["notes"],
                _now(), show_id,
            ),
        )
        return _enrich_show(conn, dict(_get_show_row(conn, show_id)))


@router.delete("/{show_id}", status_code=204)
def delete_show(show_id: int):
    with get_conn() as conn:
        _get_show_row(conn, show_id)
        conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))


@router.post("/{show_id}/refresh")
def refresh(show_id: int, full: bool = False):
    """Re-sync from TMDb now (new seasons/episodes, updated air dates, status)."""
    try:
        return refresh_show(show_id, full=full)
    except TMDbError as e:
        raise HTTPException(502, str(e))


@router.post("/{show_id}/watch")
def watch_movie(show_id: int, payload: MovieWatchIn):
    """Mark a movie watched/unwatched. 400 on a TV show — use the episode endpoints."""
    with get_conn() as conn:
        row = _get_show_row(conn, show_id)
        if row["media_type"] != "movie":
            raise HTTPException(400, "Ceci est une série — marquez les épisodes un par un")
        watched_at = (payload.at or _now()) if payload.watched else None
        new_status = "termine" if payload.watched else "a_voir"
        conn.execute(
            "UPDATE shows SET watched_at=?, status=?, updated_at=? WHERE id=?",
            (watched_at, new_status, _now(), show_id),
        )
        return _enrich_show(conn, dict(_get_show_row(conn, show_id)))


# ---------- Episodes ----------

@router.get("/{show_id}/episodes")
def list_episodes(show_id: int):
    with get_conn() as conn:
        _get_show_row(conn, show_id)
        return rows_to_dicts(conn.execute(
            "SELECT * FROM show_episodes WHERE show_id = ? ORDER BY season_number, episode_number",
            (show_id,),
        ).fetchall())


def _maybe_autocomplete(conn, show_id: int) -> None:
    """After marking an episode watched, close out the show if TMDb says it's over
    and every aired episode is now watched — one less status to update by hand."""
    row = _get_show_row(conn, show_id)
    if row["status"] not in ("a_voir", "en_cours") or row["tmdb_status"] in ONGOING_TMDB_STATUSES:
        return
    counts = _episode_counts(conn, show_id)
    if counts["total_episodes"] and not counts["unwatched_aired_episodes"]:
        conn.execute("UPDATE shows SET status='termine', updated_at=? WHERE id=?", (_now(), show_id))
    elif row["status"] == "a_voir":
        conn.execute("UPDATE shows SET status='en_cours', updated_at=? WHERE id=?", (_now(), show_id))


@router.post("/{show_id}/episodes/{episode_id}/watched")
def mark_episode_watched(show_id: int, episode_id: int, payload: EpisodeWatchIn):
    with get_conn() as conn:
        _get_show_row(conn, show_id)
        episode = conn.execute(
            "SELECT * FROM show_episodes WHERE id = ? AND show_id = ?", (episode_id, show_id)
        ).fetchone()
        if not episode:
            raise HTTPException(404, "Épisode introuvable")
        watched_at = (payload.at or _now()) if payload.watched else None
        conn.execute("UPDATE show_episodes SET watched_at = ? WHERE id = ?", (watched_at, episode_id))
        if payload.watched:
            _maybe_autocomplete(conn, show_id)
        return _enrich_show(conn, dict(_get_show_row(conn, show_id)))


@router.post("/{show_id}/seasons/{season_number}/watched")
def mark_season_watched(show_id: int, season_number: int):
    """Catch-up: mark every already-aired episode of a season as watched."""
    with get_conn() as conn:
        _get_show_row(conn, show_id)
        conn.execute(
            """UPDATE show_episodes SET watched_at = ?
               WHERE show_id = ? AND season_number = ? AND watched_at IS NULL
                 AND air_date IS NOT NULL AND air_date <= date('now')""",
            (_now(), show_id, season_number),
        )
        _maybe_autocomplete(conn, show_id)
        return _enrich_show(conn, dict(_get_show_row(conn, show_id)))
