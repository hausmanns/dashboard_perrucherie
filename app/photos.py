"""Shared helpers for photo uploads (plants + dishes).

Photos live in data/photos/ and are committed like the DB. Identification
endpoints save a temp file (tmp_<token>.<ext>); the token is later passed
back when creating/updating the entity, and claim_photo() renames it to its
final <prefix>_<id>.<ext> name.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import HTTPException, UploadFile

from .database import PHOTOS_DIR

ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_PHOTO_BYTES = 8 * 1024 * 1024  # 8 MB


async def read_image_upload(file: UploadFile) -> tuple[bytes, str]:
    """Validate an uploaded image and return (content, extension)."""
    ext = ALLOWED_IMAGE_TYPES.get(file.content_type or "")
    if not ext:
        raise HTTPException(415, "Format d'image non supporté (JPEG, PNG ou WebP attendu)")
    content = await file.read()
    if len(content) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Image trop lourde (8 Mo max)")
    return content, ext


def save_tmp_photo(content: bytes, ext: str, token: str) -> None:
    (PHOTOS_DIR / f"tmp_{token}{ext}").write_bytes(content)


def claim_photo(token: Optional[str], prefix: str, entity_id: int) -> Optional[str]:
    """Move a temp photo to its final <prefix>_<id> name. Returns filename or None."""
    if not token or not re.fullmatch(r"[0-9a-f]{32}", token):
        return None
    for ext in ALLOWED_IMAGE_TYPES.values():
        tmp = PHOTOS_DIR / f"tmp_{token}{ext}"
        if tmp.exists():
            final_name = f"{prefix}_{entity_id}{ext}"
            tmp.replace(PHOTOS_DIR / final_name)
            # Remove any previous photo with a different extension.
            for old in PHOTOS_DIR.glob(f"{prefix}_{entity_id}.*"):
                if old.name != final_name:
                    old.unlink()
            return final_name
    return None


def delete_photo(filename: Optional[str]) -> None:
    if filename:
        path = PHOTOS_DIR / filename
        if path.exists():
            path.unlink()
