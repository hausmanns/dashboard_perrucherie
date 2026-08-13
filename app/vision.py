"""Identification via a vision LLM through OpenRouter.

Sends a photo to a multimodal model (default: google/gemini-2.5-flash) and
asks for strict JSON, used to pre-fill forms:
- identify_plant → plant name/species/watering/notes
- identify_dish  → dish name/category/prep time/ingredients/notes
"""
from __future__ import annotations

import base64
import json
import re

import httpx

from . import config

PLANT_PROMPT = """Tu es un expert en botanique et en plantes d'intérieur.
Identifie la plante sur cette photo et réponds UNIQUEMENT avec un objet JSON \
(sans markdown, sans texte autour) avec exactement ces clés :
{
  "name": "nom commun en français (ex. Monstera)",
  "species": "nom scientifique latin (ex. Monstera deliciosa)",
  "watering_frequency_days": nombre entier de jours entre deux arrosages recommandés,
  "notes": "conseils d'entretien très courts en français (lumière, humidité), 1-2 phrases"
}
Si la photo ne montre pas de plante identifiable, réponds {"error": "raison en français"}."""

DISH_PROMPT = """Tu es un chef cuisinier. Identifie le plat sur cette photo et réponds \
UNIQUEMENT avec un objet JSON (sans markdown, sans texte autour) avec exactement ces clés :
{
  "name": "nom du plat en français (ex. Curry de pois chiches)",
  "category": une valeur parmi "petit-dej", "dejeuner", "diner", "snack",
  "prep_time_minutes": temps de préparation estimé en minutes (nombre entier),
  "ingredients": ["liste des ingrédients principaux avec quantités pour 4 personnes, en français, ex. \\"400 g de riz basmati\\""],
  "notes": "1-2 phrases courtes en français (allergènes, conseil de préparation)"
}
Si la photo ne montre pas de plat identifiable, réponds {"error": "raison en français"}."""


class IdentificationError(Exception):
    """Raised when the model could not identify the photo."""


async def _chat_with_image(prompt: str, image_bytes: bytes, mime_type: str) -> dict:
    """Send an image + prompt to the vision model, return the parsed JSON object."""
    if not config.OPENROUTER_API_KEY:
        raise IdentificationError("OPENROUTER_API_KEY non configurée (voir .env.example)")

    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": config.OPENROUTER_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                ],
            }
        ],
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            config.OPENROUTER_URL,
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
            json=payload,
        )
    if resp.status_code != 200:
        raise IdentificationError(f"OpenRouter a répondu {resp.status_code} : {resp.text[:300]}")

    content = resp.json()["choices"][0]["message"]["content"]
    # The model may wrap JSON in ```json fences despite instructions.
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise IdentificationError(f"Réponse du modèle illisible : {content[:300]}")
    if "error" in data:
        raise IdentificationError(data["error"])
    return data


async def identify_plant(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Return {name, species, watering_frequency_days, notes} for a plant photo."""
    data = await _chat_with_image(PLANT_PROMPT, image_bytes, mime_type)
    try:
        freq = int(data.get("watering_frequency_days") or 7)
    except (TypeError, ValueError):
        freq = 7
    return {
        "name": str(data.get("name") or "").strip(),
        "species": str(data.get("species") or "").strip(),
        "watering_frequency_days": max(1, freq),
        "notes": str(data.get("notes") or "").strip(),
    }


async def identify_dish(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Return {name, category, prep_time_minutes, ingredients, notes} for a dish photo.

    `ingredients` is a newline-separated string (one "quantity + name" per line),
    ready to store in dishes.ingredients and to feed grocery-list generation.
    """
    data = await _chat_with_image(DISH_PROMPT, image_bytes, mime_type)

    category = str(data.get("category") or "diner").strip().lower()
    if category not in ("petit-dej", "dejeuner", "diner", "snack"):
        category = "diner"

    try:
        prep = int(data.get("prep_time_minutes") or 0) or None
    except (TypeError, ValueError):
        prep = None

    raw_ingredients = data.get("ingredients")
    if isinstance(raw_ingredients, list):
        lines = [str(i).strip() for i in raw_ingredients if str(i).strip()]
    else:
        lines = [l.strip() for l in str(raw_ingredients or "").splitlines() if l.strip()]

    return {
        "name": str(data.get("name") or "").strip(),
        "category": category,
        "prep_time_minutes": prep,
        "ingredients": "\n".join(lines),
        "notes": str(data.get("notes") or "").strip(),
    }
