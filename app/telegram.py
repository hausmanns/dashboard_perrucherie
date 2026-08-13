"""General-purpose Telegram bot module, feature-agnostic.

Any feature can use this module without knowing anything about Telegram:
  * send_message()          — push a notification to the configured chat
  * register_command()      — make the bot answer a /command interactively
                              (long-polling, enabled via TELEGRAM_POLLING)

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

_commands: dict[str, Callable[[str, int], str]] = {}
_polling_task: Optional[asyncio.Task] = None


def is_configured() -> bool:
    """True when the bot token and a target chat are configured."""
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send_message(text: str, chat_id: Optional[int] = None) -> bool:
    """Send a plain-text message to the configured chat. Returns True on success."""
    if not is_configured():
        logger.info("Telegram not configured — message not sent")
        return False
    target = chat_id or TELEGRAM_CHAT_ID
    try:
        r = httpx.post(f"{API}/sendMessage", json={"chat_id": target, "text": text}, timeout=10)
        r.raise_for_status()
        return True
    except httpx.HTTPError:
        logger.exception("Telegram sendMessage failed")
        return False


def register_command(command: str, handler: Callable[[str, int], str]) -> None:
    """Register a command handler: handler(args, chat_id) -> reply text."""
    _commands[command] = handler


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
                if not text.startswith("/"):
                    continue
                command, _, args = text.partition(" ")
                handler = _commands.get(command.lower())
                if handler:
                    reply = handler(args.strip(), message["chat"]["id"])
                    if reply:
                        await asyncio.to_thread(send_message, reply, message["chat"]["id"])
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