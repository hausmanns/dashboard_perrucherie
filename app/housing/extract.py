"""An apartment ad pasted by hand → structured fields, through the LLM.

For what no robot can read: Facebook groups and Marketplace (login wall),
WhatsApp, a régie's own website, word of mouth. The household pastes the
text (and the link); the model only *extracts* — it never writes — and
anything it invents outside the expected keys is dropped. When only a link
is given and the page is public, its text is fetched and used instead.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from .. import openrouter
from .common import CATEGORIES, FEATURES, html_text, parse_chf, to_float, to_int
from .http import Fetcher, SourceError

MAX_TEXT = 6000
# Sites where fetching the page is pointless: login wall or anti-bot wall.
UNREADABLE_HOSTS = ("facebook.com", "fb.com", "instagram.com", "whatsapp.com", "homegate.ch",
                    "immoscout24.ch", "comparis.ch", "newhome.ch")

PROMPT = """Tu lis une annonce de location de logement en Suisse, copiée depuis Facebook, \
un groupe WhatsApp, le site d'une régie ou un site d'annonces. Réponds UNIQUEMENT avec un objet \
JSON (sans markdown, sans texte autour) :
{{
  "title": "titre court et concret, ex. « 3.5 pièces lumineux avec balcon »",
  "category": une valeur parmi {categories},
  "price": loyer mensuel CHARGES COMPRISES en CHF (nombre) ou null,
  "charges": charges mensuelles en CHF (nombre) ou null,
  "rooms": nombre de pièces à la suisse (ex. 3.5) ou null,
  "surface": surface habitable en m² (nombre) ou null,
  "floor": étage (nombre, 0 = rez) ou null,
  "street": "rue et numéro si donnés, sinon chaîne vide",
  "zipcode": "NPA suisse à 4 chiffres si donné, sinon chaîne vide",
  "city": "localité, sinon chaîne vide",
  "available_from": "AAAA-MM-JJ", "immediately" si disponible tout de suite, sinon null,
  "features": liste parmi {features} (seulement ce que l'annonce dit vraiment),
  "description": "le texte de l'annonce, nettoyé (sans emojis répétés ni hashtags), 1500 caractères max"
}}

Règles :
- N'invente jamais un prix, une adresse ou une surface absents du texte.
- « 1800.- + 200.- de charges » → price 2000, charges 200. Un loyer sans mention des charges reste tel quel.
- Une « reprise de bail » ou une « sous-location » reste une annonce : décris-la dans le titre.
- Si le texte n'est pas une annonce de logement, réponds {{"error": "Ce texte ne ressemble pas à une annonce de logement"}}.

Annonce :
---
{text}
---
Lien : {url}"""


def source_label(url: str) -> str:
    """« Facebook », « WhatsApp », or the site's domain, for a hand-added ad."""
    host = (urlparse(url or "").netloc or "").lower().removeprefix("www.").removeprefix("m.")
    if not host:
        return "Ajout manuel"
    for needle, label in (("facebook", "Facebook"), ("fb.com", "Facebook"), ("instagram", "Instagram"),
                          ("whatsapp", "WhatsApp"), ("anibis", "Anibis"), ("tutti", "Tutti"),
                          ("ricardo", "Ricardo")):
        if needle in host:
            return label
    return host


def fetch_page_text(url: str) -> str:
    """Public page → its visible text (empty when the site can't be read)."""
    host = (urlparse(url).netloc or "").lower()
    if not host or any(h in host for h in UNREADABLE_HOSTS):
        return ""
    fetcher = Fetcher()
    try:
        resp = fetcher.get(url, retries=0)
        if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
            return ""
        body = re.search(r"<body[^>]*>(.*)</body>", resp.text, re.S | re.I)
        return html_text(body.group(1) if body else resp.text)[:MAX_TEXT]
    except SourceError:
        return ""
    finally:
        fetcher.close()


def clean(data: dict) -> dict:
    """Keep only the expected keys, with sane types."""
    category = data.get("category") if data.get("category") in CATEGORIES else "apartment"
    zip_code = re.sub(r"\D", "", str(data.get("zipcode") or ""))[:4]
    available = data.get("available_from")
    if available not in (None, "immediately") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(available)):
        available = None
    return {
        "title": str(data.get("title") or "").strip()[:160],
        "category": category,
        "price": parse_chf(data.get("price")) if data.get("price") is not None else None,
        "charges": parse_chf(data.get("charges")) if data.get("charges") is not None else None,
        "rooms": to_float(data.get("rooms")),
        "surface": to_int(data.get("surface")),
        "floor": to_int(data.get("floor")),
        "street": str(data.get("street") or "").strip()[:160],
        "zipcode": zip_code if len(zip_code) == 4 else "",
        "city": str(data.get("city") or "").strip()[:80],
        "available_from": available,
        "features": [f for f in data.get("features") or [] if f in FEATURES],
        "description": str(data.get("description") or "").strip()[:3000],
    }


async def extract(text: str, url: str = "") -> dict:
    """Pasted text (and/or link) → cleaned fields. Raises openrouter.LLMError."""
    text = (text or "").strip()
    if not text and url:
        text = await asyncio.to_thread(fetch_page_text, url)  # blocking HTTP: keep the event loop free
        if not text:
            raise openrouter.LLMError(
                "Ce site ne se laisse pas lire automatiquement — copiez le texte de l'annonce dans le champ.")
    if len(text) < 20:
        raise openrouter.LLMError("Collez le texte de l'annonce (quelques lignes au moins).")
    prompt = PROMPT.format(
        categories=", ".join(f'"{c}"' for c in CATEGORIES),
        features=", ".join(f'"{f}"' for f in FEATURES),
        text=text[:MAX_TEXT], url=url or "(aucun)",
    )
    return clean(await openrouter.achat_json(openrouter.text_message(prompt)))
