"""Telegram bot feature wiring for the dashboard.

The generic Telegram plumbing lives in app/telegram.py; this module is the
feature registry. First feature: daily watering reminders (09:00 & 21:00,
server-local time) plus an on-demand `/plantes` command.

Future features should register their own cron jobs here (and in
main.startup) via `add_cron_job()`.
"""
from __future__ import annotations

import math
from datetime import datetime

from fastapi import APIRouter

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import telegram
from .config import TELEGRAM_TZ
from .routers.plants import due_plants

router = APIRouter(prefix="/api/bot", tags=["bot"])

WATERING_HOURS = (9, 21)  # daily checks, server-local time

_scheduler: BackgroundScheduler | None = None
_cron_jobs: list[tuple] = []  # (func, hour, minute)


def add_cron_job(func, hour: int, minute: int = 0) -> None:
    """Register a daily job to be started with the app (idempotent)."""
    job = (func, hour, minute)
    if job not in _cron_jobs:
        _cron_jobs.append(job)


def _status_label(plant: dict) -> str:
    if plant["status"] == "never_watered":
        return "🚫 jamais arrosée"
    if plant["status"] == "overdue":
        days = max(1, math.ceil(-plant["days_until_watering"]))
        return f"⚠️ en retard de {days} j"
    return "🔜 à arroser aujourd'hui"


def watering_message() -> str:
    """Build the French watering summary sent to the chat."""
    due = due_plants()
    now = datetime.now().strftime("%H:%M")
    if not due:
        return f"💧 Arrosage — {now}\n\nToutes les plantes sont arrosées ✅"
    lines = [f"💧 Arrosage — {now}", "", "Plantes à arroser :", ""]
    for p in due:
        location = f" ({p['location']})" if p.get("location") else ""
        lines.append(f"• {p['name']}{location}")
        lines.append(f"   {_status_label(p)}")
    lines.append("")
    lines.append(f"Total : {len(due)} plante(s)")
    return "\n".join(lines)


def send_watering_check() -> bool:
    """Send the watering summary now (used by the cron jobs and the test endpoint)."""
    return telegram.send_message(watering_message())


def _plants_command(args: str, chat_id: int) -> str:
    return watering_message()


def _help_command(args: str, chat_id: int) -> str:
    return "Commandes :\n/plantes — état de l'arrosage"


def start() -> None:
    """Register commands + cron jobs and start scheduler/polling. Idempotent."""
    if not telegram.is_configured():
        return
    telegram.register_command("/plantes", _plants_command)
    telegram.register_command("/help", _help_command)
    add_cron_job(send_watering_check, 9, 0)
    add_cron_job(send_watering_check, 21, 0)

    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(daemon=True)
        for func, hour, minute in _cron_jobs:
            _scheduler.add_job(
                func,
                CronTrigger(hour=hour, minute=minute, timezone=TELEGRAM_TZ or None),
                id=f"bot-{func.__name__}-{hour:02d}{minute:02d}",
            )
        _scheduler.start()
    telegram.start_polling()


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


@router.post("/watering-check", status_code=200)
def watering_check():
    """Trigger a watering check right now (same logic as the 09:00/21:00 cron)."""
    return {"sent": send_watering_check(), "configured": telegram.is_configured()}