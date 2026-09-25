"""The Movie Database (TMDb) client: search, trending, and TV/movie details.

Powers the show tracker (app/routers/shows.py): finding what to add, covers,
synopsis, and the episode-level data (air dates) behind the calendar and the
Telegram "new episode" notification. Read-only, no auth beyond the API key.

TMDb answers in French (`language=fr-FR`) but a niche title's synopsis is
often untranslated; `tv_details`/`movie_details` fall back to English for the
overview alone rather than showing a blank one.
"""
from __future__ import annotations

import httpx

from . import config

BASE_URL = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p"


class TMDbError(Exception):
    """TMDb is unconfigured, unreachable, or answered an error."""


def is_configured() -> bool:
    return bool(config.TMDB_API_KEY)


def poster_url(path: str | None, size: str = "w342") -> str | None:
    return f"{IMAGE_BASE}/{size}{path}" if path else None


def backdrop_url(path: str | None, size: str = "w780") -> str | None:
    return f"{IMAGE_BASE}/{size}{path}" if path else None


def _get(path: str, **params) -> dict:
    if not config.TMDB_API_KEY:
        raise TMDbError("TMDB_API_KEY non configurée (voir .env.example)")
    params = {"api_key": config.TMDB_API_KEY, "language": "fr-FR", **params}
    try:
        resp = httpx.get(f"{BASE_URL}{path}", params=params, timeout=15)
    except httpx.HTTPError as e:
        raise TMDbError(f"TMDb injoignable : {e}")
    if resp.status_code == 404:
        raise TMDbError("Introuvable sur TMDb")
    if resp.status_code != 200:
        raise TMDbError(f"TMDb a répondu {resp.status_code} : {resp.text[:300]}")
    return resp.json()


def _with_overview_fallback(data: dict, path: str) -> dict:
    """Some titles have no French synopsis yet — fall back to English rather than blank."""
    if not data.get("overview"):
        try:
            en = _get(path, language="en-US")
        except TMDbError:
            return data
        data["overview"] = en.get("overview", "")
    return data


def _result_to_candidate(item: dict) -> dict | None:
    media_type = item.get("media_type")
    if media_type not in ("tv", "movie"):
        return None
    return {
        "tmdb_id": item["id"],
        "media_type": media_type,
        "title": item.get("title") or item.get("name") or "",
        "overview": item.get("overview", ""),
        "poster_url": poster_url(item.get("poster_path")),
        "backdrop_url": backdrop_url(item.get("backdrop_path")),
        "date": item.get("release_date") or item.get("first_air_date") or "",
        "popularity": item.get("popularity", 0),
    }


def search_multi(query: str) -> list[dict]:
    data = _get("/search/multi", query=query, include_adult="false")
    candidates = (_result_to_candidate(r) for r in data.get("results", []))
    return [c for c in candidates if c]


def trending(media_type: str = "all", window: str = "week") -> list[dict]:
    if media_type not in ("all", "movie", "tv"):
        media_type = "all"
    if window not in ("day", "week"):
        window = "week"
    data = _get(f"/trending/{media_type}/{window}")
    candidates = (_result_to_candidate(r) for r in data.get("results", []))
    return [c for c in candidates if c]


def tv_details(tmdb_id: int) -> dict:
    data = _get(f"/tv/{tmdb_id}")
    return _with_overview_fallback(data, f"/tv/{tmdb_id}")


def tv_season(tmdb_id: int, season_number: int) -> dict:
    return _get(f"/tv/{tmdb_id}/season/{season_number}")


def movie_details(tmdb_id: int) -> dict:
    data = _get(f"/movie/{tmdb_id}")
    return _with_overview_fallback(data, f"/movie/{tmdb_id}")
