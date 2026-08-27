"""Identification via a vision LLM (OpenRouter plumbing lives in openrouter.py).

Sends a photo to a multimodal model (default: google/gemini-2.5-flash) and
asks for strict JSON, used to pre-fill forms:
- identify_plant → plant name/species/watering/notes
- identify_dish  → dish name/category/prep time/ingredients/notes
- identify_wish  → wished-for object name/category/price/description
"""
from __future__ import annotations

from . import openrouter
from .openrouter import LLMError

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


# Slugs de catégorie (sans accent) partagés avec le formulaire et l'API wishlist.
WISH_CATEGORIES = (
    "tech", "maison", "vetements", "livres", "sport", "loisirs",
    "beaute", "cuisine", "voyage", "experience", "autre",
)

WISH_PROMPT = """Tu es un expert en produits de consommation. Identifie l'objet sur cette \nphoto comme s'il s'agissait d'une idée cadeau et réponds UNIQUEMENT avec un objet JSON \n(sans markdown, sans texte autour) avec exactement ces clés :
{
  "name": "nom du produit en français, marque et modèle si visibles (ex. Casque Sony WH-1000XM5)",
  "category": une valeur parmi "tech", "maison", "vetements", "livres", "sport", "loisirs", "beaute", "cuisine", "voyage", "experience", "autre",
  "price": prix neuf estimé en francs suisses (nombre, sans devise ; null si inconnu),
  "shop": "type d'enseigne où on le trouve (ex. Digitec, librairie, magasin de sport) ou chaîne vide",
  "color": "couleur ou finition visible sur la photo, sinon chaîne vide",
  "description": "1-2 phrases en français décrivant l'objet et ce qui le rend intéressant à offrir"
}
Si la photo ne montre pas d'objet identifiable, réponds {"error": "raison en français"}."""


class IdentificationError(LLMError):
    """Raised when the model could not identify the photo."""


async def _chat_with_image(prompt: str, image_bytes: bytes, mime_type: str) -> dict:
    """Send an image + prompt to the vision model, return the parsed JSON object."""
    try:
        return await openrouter.achat_json(openrouter.image_message(prompt, image_bytes, mime_type))
    except LLMError as e:
        raise IdentificationError(str(e))


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


async def identify_wish(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Return {name, category, price, shop, color, description} for a wished-for object photo."""
    data = await _chat_with_image(WISH_PROMPT, image_bytes, mime_type)

    category = str(data.get("category") or "autre").strip().lower()
    if category not in WISH_CATEGORIES:
        category = "autre"

    try:
        price = round(float(str(data.get("price")).replace(",", ".")), 2)
    except (TypeError, ValueError):
        price = None

    return {
        "name": str(data.get("name") or "").strip(),
        "category": category,
        "price": price,
        "shop": str(data.get("shop") or "").strip(),
        "color": str(data.get("color") or "").strip(),
        "description": str(data.get("description") or "").strip(),
    }
