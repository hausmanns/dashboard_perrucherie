"""Régies (property managers) of the Vaud / Romandie region, read on their own
websites — some publish there before, or instead of, the portals.

Each module is a source like the portals (`KEY`, `LABEL`, `search(criteria,
zones, ctx)`), plus `GROUP = "regie"` and the `CANTONS` it covers: a search
looking elsewhere costs it no request. Régie sites mostly list the net rent;
the shared helpers in `common.py` read a matching card's detail page once to
get the rent charges included (and the text), which is also what lets the
engine merge the régie's copy with the portals' copies of the same flat.

Checked in October 2026, region of Lausanne:
- own feed, read here: Naef, Bernard Nicod, Retraites Populaires, Cogestim,
  Gérance Robert Crot, Gérance Borgeaud, MGM Fiduciaire, Livit, Rosset,
  Publiaz immobilier;
- no feed of their own: Régie Chamot & Cie (its rental page embeds
  immobilier.ch), Roland Savary (immobilier.ch and Homegate only), Wincasa and
  Apleona (their rental pages embed a Homegate widget), Régie Duboux (its
  site lists sales only; rentals on immobilier.ch and Homegate) — the portal
  sources already bring their ads, with the agency named;
- not a listing site: Service Immobilier Vaudois, a paid search service.
"""
from . import (borgeaud, cogestim, crot, livit, mgm, naef, nicod, publiaz, retraites_populaires,
               rosset)

SOURCES = [naef, nicod, retraites_populaires, cogestim, crot, borgeaud, mgm, livit, rosset, publiaz]

# Shown in the search form, so nobody wonders where they went.
ELSEWHERE = [
    {"label": "Régie Chamot & Cie", "note": "publie sur immobilier.ch — déjà inclus"},
    {"label": "Roland Savary Immobilier", "note": "publie sur immobilier.ch et Homegate — déjà inclus"},
    {"label": "Wincasa", "note": "publie sur Homegate — déjà inclus"},
    {"label": "Apleona", "note": "publie sur Homegate — déjà inclus"},
    {"label": "Régie Duboux", "note": "son site ne liste que des ventes ; locations sur immobilier.ch et Homegate — déjà incluses"},
    {"label": "Service Immobilier Vaudois", "note": "service de recherche payant, sans liste publique à lire"},
]
