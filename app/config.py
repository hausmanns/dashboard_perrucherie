"""Runtime configuration (env vars / .env file).

OPENROUTER_API_KEY  — required for every LLM feature: photo identification
                      (plants, dishes, wishes) and reading a wish written in
                      plain French on Telegram.
OPENROUTER_MODEL    — model used for both (default: google/gemini-2.5-flash).
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Telegram bot (general-purpose notification channel).
# TELEGRAM_BOT_TOKEN — token from @BotFather (required to enable the bot).
# TELEGRAM_CHAT_ID  — chat/user that receives the notifications.
# TELEGRAM_TZ       — IANA timezone for the cron jobs, e.g. Europe/Paris.
#                     Empty = server local time (Docker containers default to UTC).
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_TZ = os.environ.get("TELEGRAM_TZ", "")
