"""Apartment search ("Logement"): saved searches run against Swiss housing
sites, on demand or on a schedule, with the results on a map.

Layout:
- `places`     — Swiss postcodes / communes / cantons (swisstopo open data):
                 zone picker, zone matching, approximate positions, geocoding.
- `http`       — the polite HTTP client every scraper shares.
- `flatfox`, `homegate`, `immobilier` — one module per source, each exposing
                 `search(criteria, zones, ctx) -> list[listing dict]`.
- `engine`     — runs a search: ask the sources, filter, store, merge
                 duplicates, alert Telegram. Also the scheduler hook.
- `extract`    — an ad pasted by hand (Facebook…) → structured fields (LLM).

Which sites can be read, and how, was worked out by hand (October 2026):
Flatfox has a public JSON API; Homegate/ImmoScout24 block robots (DataDome)
but their sister site home.ch renders the same listings openly;
immobilier.ch serves HTML fragments; Comparis/Newhome block robots and are
mostly copies of the above; Facebook needs a login, hence the paste-an-ad
path. See AGENTS.md for the conventions.
"""
