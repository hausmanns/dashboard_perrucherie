"""General-purpose Telegram bot module, feature-agnostic.

Any feature can use this module without knowing anything about Telegram:
  * send_message()          — push a notification to the configured chat
  * register_command()      — make the bot answer a /command interactively
  * register_text_handler() — handle plain messages (no slash), for features
                              that understand natural language

Cron-style scheduling lives in app/bot.py (APScheduler), which is where
features register their daily jobs. Nothing here knows about plants or meals.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# handler(args, chat_id, user_id) -> reply text
_commands: dict[str, Callable[[str, int, int], Optional[str]]] = {}
# handler(text, chat_id, user_id, is_private) -> reply text or None to stay silent
_text_handlers: list[Callable[[str, int, int, bool], Optional[str]]] = []
_polling_task: Optional[asyncio.Task] = None


def is_configured() -> bool:
    """True when the bot token and a target chat are configured."""
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


MAX_MESSAGE_LEN = 4096  # Telegram's cap, in UTF-16 code units — anything longer is rejected (400)


def _tg_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Cut `text` on line breaks into pieces that each fit in one message."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while _tg_len(line) > limit:  # a single line longer than a whole message
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[: limit // 2])  # ≤ 2 UTF-16 units per character
            line = line[limit // 2:]
        candidate = f"{current}\n{line}" if current else line
        if current and _tg_len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text]


def send_message(text: str, chat_id: Optional[int] = None) -> bool:
    """Send a plain-text message to the configured chat. Returns True on success.

    Text over Telegram's length cap goes out as several messages, cut on line
    breaks — sent whole, it would be rejected outright, every single time."""
    if not is_configured():
        logger.info("Telegram not configured — message not sent")
        return False
    target = chat_id or TELEGRAM_CHAT_ID
    try:
        for chunk in split_message(text):
            r = httpx.post(f"{API}/sendMessage", json={"chat_id": target, "text": chunk}, timeout=10)
            r.raise_for_status()
        return True
    except httpx.HTTPError:
        logger.exception("Telegram sendMessage failed")
        return False


def register_command(command: str, handler: Callable[[str, int, int], Optional[str]]) -> None:
    """Register a command handler: handler(args, chat_id, user_id) -> reply text."""
    _commands[command] = handler


def register_text_handler(handler: Callable[[str, int, int, bool], Optional[str]]) -> None:
    """Handle messages that are not commands.

    handler(text, chat_id, user_id, is_private) -> reply text, or None to ignore
    the message entirely (the bot then stays silent, which is what you want in a
    busy group chat).

    Several features may register a handler: they are tried in registration
    order and the first one to return a reply wins, so each handler must return
    None for messages that are not its business.
    """
    if handler not in _text_handlers:
        _text_handlers.append(handler)


def _get_updates(offset: int) -> list[dict]:
    r = httpx.post(f"{API}/getUpdates", json={"offset": offset, "timeout": 25}, timeout=30)
    r.raise_for_status()
    return r.json().get("result", [])


async def _poll_loop() -> None:
    offset = 0
    while True:
        try:
            updates = await asyncio.to_thread(_get_updates, offset)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message or "text" not in message:
                    continue
                text = message["text"].strip()
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                user_id = (message.get("from") or {}).get("id", 0)
                if chat_id is None:
                    continue

                reply = None
                if text.startswith("/"):
                    command, _, args = text.partition(" ")
                    # In groups Telegram sends "/envies@MonBot" — drop the suffix.
                    handler = _commands.get(command.split("@")[0].lower())
                    if handler:
                        # Handlers hit SQLite and sometimes an LLM: keep the loop free.
                        reply = await asyncio.to_thread(handler, args.strip(), chat_id, user_id)
                else:
                    for handler in list(_text_handlers):
                        reply = await asyncio.to_thread(
                            handler, text, chat_id, user_id, chat.get("type") == "private"
                        )
                        if reply:
                            break
                if reply:
                    await asyncio.to_thread(send_message, reply, chat_id)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Telegram polling error; retrying")
            await asyncio.sleep(5)
        await asyncio.sleep(1)


def start_polling() -> None:
    """Start the command long-polling loop as a background task (idempotent).

    No-op when there is no running asyncio loop (e.g. a synchronous caller);
    the scheduler (cron) still works in that case.
    """
    global _polling_task
    if not is_configured() or _polling_task is not None:
        return
    try:
        _polling_task = asyncio.get_running_loop().create_task(_poll_loop())
    except RuntimeError:
        _polling_task = None