"""Shared OpenRouter plumbing: one chat call, one strict-JSON reply.

Two callers sit on top of this module:
- `vision.py` — photo → plant / dish / wish identification (async, called from
  a FastAPI endpoint).
- `nlu.py`    — free-text Telegram message → structured wish (sync, called from
  the bot's worker thread).

The model is configurable via `OPENROUTER_MODEL` (default
`google/gemini-2.5-flash`) and the key via `OPENROUTER_API_KEY`.
"""
from __future__ import annotations

import base64
import json
import re

import httpx

from . import config


class LLMError(Exception):
    """OpenRouter is unconfigured, unreachable, or answered unusable JSON."""


def text_message(prompt: str) -> list[dict]:
    """A single user turn made of plain text."""
    return [{"role": "user", "content": prompt}]


def image_message(prompt: str, image_bytes: bytes, mime_type: str) -> list[dict]:
    """A single user turn made of a prompt + an inline base64 image."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
        ],
    }]


def _request(messages: list[dict]) -> tuple[str, dict, dict]:
    """(url, headers, payload) — raises before any network call if the key is missing."""
    if not config.OPENROUTER_API_KEY:
        raise LLMError("OPENROUTER_API_KEY non configurée (voir .env.example)")
    return (
        config.OPENROUTER_URL,
        {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
        {"model": config.OPENROUTER_MODEL, "messages": messages},
    )


def _decode(resp: httpx.Response) -> dict:
    if resp.status_code != 200:
        raise LLMError(f"OpenRouter a répondu {resp.status_code} : {resp.text[:300]}")
    content = resp.json()["choices"][0]["message"]["content"]
    # The model may wrap JSON in ```json fences despite instructions.
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise LLMError(f"Réponse du modèle illisible : {content[:300]}")
    if isinstance(data, dict) and "error" in data:
        raise LLMError(data["error"])
    return data


async def achat_json(messages: list[dict], timeout: int = 60) -> dict:
    """Async variant, for FastAPI endpoints."""
    url, headers, payload = _request(messages)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload)
    return _decode(resp)


def chat_json(messages: list[dict], timeout: int = 60) -> dict:
    """Blocking variant, for callers already running in a worker thread."""
    url, headers, payload = _request(messages)
    return _decode(httpx.post(url, headers=headers, json=payload, timeout=timeout))
