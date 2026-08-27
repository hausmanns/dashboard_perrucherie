"""Natural-language understanding for the Telegram bot.

Turns a free-text chat message into structured wishlist data, so someone can
write « je veux le casque Sony, genre 350 balles chez Digitec » instead of
filling a form. Two calls:

- `parse_wish_message()` — a whole message → intent + target person + a list of
  wishes, each shaped like the `WishIn` payload of `/api/wishlist/items`.
- `parse_wish_details()` — a follow-up answer ("350 chez Digitec", "non merci")
  → the fields to patch on the wish the bot just asked about.

Both return plain dicts with keys the router already knows; anything the model
invents is dropped rather than trusted.
"""
from __future__ import annotations

from typing import Optional

from . import openrouter
from .vision import WISH_CATEGORIES

# Champs qu'une envie peut recevoir depuis le chat (sous-ensemble de WishIn).
WISH_FIELDS = (
    "name", "description", "category", "price", "currency", "url", "shop",
    "size", "color", "quantity", "priority", "occasion",
)
# Champs qu'une réponse de complément peut modifier (jamais la personne ni le statut).
DETAIL_FIELDS = ("price", "currency", "url", "shop", "size", "color", "occasion", "description")

_CATEGORY_LIST = ", ".join(f'"{c}"' for c in WISH_CATEGORIES)

WISH_PROMPT = """Tu lis les messages d'un chat familial pour alimenter une liste d'envies \
(des idées de cadeaux). Analyse le message ci-dessous et réponds UNIQUEMENT avec un objet \
JSON (sans markdown, sans texte autour) :
{{
  "intent": "wish" si le message exprime une ou plusieurs envies d'objets, sinon "other",
  "person": "le prénom de la personne à qui l'envie appartient, tel qu'écrit dans le message, sinon null",
  "for_sender": true si l'auteur parle de sa propre envie ("je veux", "j'aimerais", "il me faudrait"), sinon false,
  "items": [
    {{
      "name": "nom court et concret du produit (marque et modèle si donnés)",
      "description": "précisions utiles, sinon chaîne vide",
      "category": une valeur parmi {categories},
      "price": prix mentionné en nombre, sinon null,
      "currency": "CHF" sauf si une autre devise est explicite,
      "url": "lien s'il y en a un dans le message, sinon chaîne vide",
      "shop": "enseigne ou magasin mentionné, sinon chaîne vide",
      "size": "taille mentionnée, sinon chaîne vide",
      "color": "couleur mentionnée, sinon chaîne vide",
      "quantity": nombre d'exemplaires souhaités (1 par défaut),
      "priority": 3 si l'envie est forte ("j'en rêve", "absolument"), 1 si elle est vague \
("un jour", "pourquoi pas"), sinon 2,
      "occasion": "Noël, anniversaire… si le message le précise, sinon chaîne vide"
    }}
  ]
}}

Règles :
- Un message peut contenir PLUSIEURS envies : mets-en une par entrée de "items".
- N'invente jamais un prix, un lien ou un magasin qui n'est pas dans le message.
- Ne devine pas la personne : si aucun prénom n'est cité et que l'auteur ne parle pas de \
lui-même, mets "person": null et "for_sender": false.
- Si le message ne parle pas d'envie ni de cadeau (une question, une blague, autre chose), \
réponds {{"intent": "other", "person": null, "for_sender": false, "items": []}}.

Prénoms connus de la maison : {people}

Message :
{message}"""

DETAILS_PROMPT = """Tu complètes une envie déjà enregistrée dans une liste de cadeaux. \
Le bot vient de demander les informations manquantes et voici la réponse de l'utilisateur.

Envie concernée : {wish}
Informations manquantes demandées : {missing}

Réponds UNIQUEMENT avec un objet JSON (sans markdown, sans texte autour) :
{{
  "skip": true si la réponse décline ou n'apporte aucune information ("non", "je sais pas", \
"laisse tomber"), sinon false,
  "fields": {{
    "price": nombre ou null,
    "currency": "CHF" ou autre devise si précisée, sinon null,
    "url": "lien ou null",
    "shop": "enseigne ou null",
    "size": "taille ou null",
    "color": "couleur ou null",
    "occasion": "occasion ou null",
    "description": "précision utile ou null"
  }}
}}
Mets `null` pour tout ce que la réponse ne dit pas — n'invente rien.

Réponse de l'utilisateur :
{answer}"""


def _clean_str(value) -> str:
    return "" if value is None else str(value).strip()


def _clean_price(value) -> Optional[float]:
    try:
        return round(float(str(value).replace(",", ".").strip()), 2)
    except (TypeError, ValueError):
        return None


def _clean_int(value, default: int, minimum: int = 1, maximum: int = 999) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _clean_item(raw: dict) -> Optional[dict]:
    """Keep only WishIn-shaped keys, with sane types. Returns None if unusable."""
    if not isinstance(raw, dict):
        return None
    name = _clean_str(raw.get("name"))
    if not name:
        return None

    category = _clean_str(raw.get("category")).lower()
    item = {
        "name": name,
        "description": _clean_str(raw.get("description")),
        "category": category if category in WISH_CATEGORIES else "autre",
        "price": _clean_price(raw.get("price")),
        "currency": _clean_str(raw.get("currency")) or "CHF",
        "url": _clean_str(raw.get("url")),
        "shop": _clean_str(raw.get("shop")),
        "size": _clean_str(raw.get("size")),
        "color": _clean_str(raw.get("color")),
        "quantity": _clean_int(raw.get("quantity"), default=1),
        "priority": _clean_int(raw.get("priority"), default=2, minimum=1, maximum=3),
        "occasion": _clean_str(raw.get("occasion")),
    }
    return {k: v for k, v in item.items() if k in WISH_FIELDS}


def parse_wish_message(message: str, people: list[str]) -> dict:
    """Free-text message → {intent, person, for_sender, items}. Raises LLMError."""
    prompt = WISH_PROMPT.format(
        categories=_CATEGORY_LIST,
        people=", ".join(people) or "(aucun)",
        message=message,
    )
    data = openrouter.chat_json(openrouter.text_message(prompt))

    raw_items = data.get("items") if isinstance(data, dict) else None
    items = [i for i in (_clean_item(r) for r in (raw_items or [])) if i]
    intent = "wish" if data.get("intent") == "wish" and items else "other"
    return {
        "intent": intent,
        "person": _clean_str(data.get("person")) or None,
        "for_sender": bool(data.get("for_sender")),
        "items": items,
    }


def parse_wish_details(answer: str, wish: dict, missing: list[str]) -> dict:
    """Follow-up answer → {skip, fields} with only the fields actually given."""
    summary = ", ".join(
        f"{k}={wish[k]}" for k in ("name", "category", "price", "shop", "url", "size", "color")
        if wish.get(k)
    )
    prompt = DETAILS_PROMPT.format(
        wish=summary or wish.get("name", ""),
        missing=", ".join(missing) or "(aucune)",
        answer=answer,
    )
    data = openrouter.chat_json(openrouter.text_message(prompt))

    raw_fields = data.get("fields") if isinstance(data, dict) else None
    fields: dict = {}
    for key in DETAIL_FIELDS:
        value = (raw_fields or {}).get(key)
        if value is None or value == "":
            continue
        fields[key] = _clean_price(value) if key == "price" else _clean_str(value)
    fields = {k: v for k, v in fields.items() if v not in (None, "")}
    return {"skip": bool(data.get("skip")) or not fields, "fields": fields}


# ---------------- Rangement (cartons) ----------------

STORAGE_INTENTS = ("take_out", "put_back", "find", "add", "other")
# Champs qu'un objet peut recevoir depuis le chat (sous-ensemble de ItemIn).
STORAGE_FIELDS = ("name", "owner", "category", "description", "quantity")

STORAGE_PROMPT = """Tu lis les messages d'un chat familial pour tenir à jour l'inventaire des \
cartons de rangement de la maison. Analyse le message ci-dessous et réponds UNIQUEMENT avec un \
objet JSON (sans markdown, sans texte autour) :
{{
  "intent": "take_out" si l'auteur dit qu'il a sorti, pris, emporté ou prêté quelque chose \
qui était rangé,
            "put_back" s'il dit qu'il a rangé, remis, rapporté ou rendu quelque chose,
            "find" s'il demande où se trouve quelque chose,
            "add" s'il dit qu'il a mis un NOUVEL objet dans un carton,
            "other" dans tous les autres cas,
  "box": "le numéro du carton mentionné (ou son nom si aucun numéro n'est donné), sinon null",
  "note": "où va l'objet ou chez qui, pour take_out (« prêté à Tom », « dans la chambre »), \
sinon chaîne vide",
  "query": "ce que l'auteur cherche, uniquement pour intent=find, sinon chaîne vide",
  "items": [
    {{
      "name": "nom court de l'objet, tel qu'il serait écrit sur un inventaire",
      "owner": "prénom du propriétaire si le message le précise, sinon chaîne vide",
      "category": "thème / collection si le message le précise, sinon chaîne vide",
      "description": "précisions utiles, sinon chaîne vide",
      "quantity": nombre d'exemplaires (1 par défaut)
    }}
  ]
}}

Règles :
- Un message peut concerner PLUSIEURS objets : mets-en un par entrée de "items".
- Pour "find", laisse "items" vide et mets ce qui est cherché dans "query".
- N'invente jamais un numéro de carton : si l'auteur n'en cite aucun, mets "box": null.
- Recopie les noms des objets tels qu'ils sont dits, sans les traduire ni les enjoliver.
- Si le message ne parle pas de rangement, réponds \
{{"intent": "other", "box": null, "note": "", "query": "", "items": []}}.

Cartons existants : {boxes}
Prénoms connus de la maison : {people}

Message :
{message}"""


def _clean_storage_item(raw) -> Optional[dict]:
    """Keep only ItemIn-shaped keys, with sane types. Returns None if unusable."""
    if not isinstance(raw, dict):
        return None
    name = _clean_str(raw.get("name"))
    if not name:
        return None
    return {
        "name": name,
        "owner": _clean_str(raw.get("owner")),
        "category": _clean_str(raw.get("category")),
        "description": _clean_str(raw.get("description")),
        "quantity": _clean_int(raw.get("quantity"), default=1),
    }


def parse_storage_message(message: str, boxes: list[str], people: list[str]) -> dict:
    """Free-text message → {intent, box, note, query, items}. Raises LLMError.

    `boxes` are human labels like "2 · Nautique" so the model can resolve
    « le carton du nautique » to a code the bot then matches itself.
    """
    prompt = STORAGE_PROMPT.format(
        boxes=", ".join(boxes) or "(aucun)",
        people=", ".join(people) or "(aucun)",
        message=message,
    )
    data = openrouter.chat_json(openrouter.text_message(prompt))
    if not isinstance(data, dict):
        return {"intent": "other", "box": None, "note": "", "query": "", "items": []}

    items = [i for i in (_clean_storage_item(r) for r in (data.get("items") or [])) if i]
    intent = _clean_str(data.get("intent"))
    if intent not in STORAGE_INTENTS:
        intent = "other"
    query = _clean_str(data.get("query"))
    # Une intention qui n'a sur quoi porter n'en est pas une.
    if intent in ("take_out", "put_back", "add") and not items:
        intent = "other"
    if intent == "find" and not query:
        intent = "other"
    return {
        "intent": intent,
        "box": _clean_str(data.get("box")) or None,
        "note": _clean_str(data.get("note")),
        "query": query,
        "items": items,
    }
